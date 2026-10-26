r"""Report and reclaim the Haskell caches that nothing else ever collects.

Cabal has no ``store gc``, and HLS, ghcide, and hie-bios never collect anything
at all. Worse, each of them bakes a version into its cache filenames, so every
GHC or HLS bump silently orphans the previous generation and leaves it on disk
forever. On a working machine that adds up to tens of gigabytes.

Every target here proves liveness by reading a file that names its owner, which
makes orphan detection exact rather than a guess. One invariant holds
throughout: **absence of evidence is never evidence of death**. An entry dies
only when a file was read successfully *and* the owner it names is gone. A
locked database, a missing ``plan.json``, an unreadable marker: all mean keep.
That is what makes a wholesale failure (sqlite broken, home directory not
mounted) fail safe.

\b
Targets and the evidence each one reads:
  store          <store>/<unit-id>/ -- a GHC on this machine reports that unit
                 id as "Project Unit Id" (9.6 and older get plain ghc-<version>)
  ghcide         *.hiedb -- the mods table's hs_src column holds absolute source
                 paths; *-inplace-<sha1>/ directories are marked and swept from
                 the hieFile column of the databases that are still live
  hie-bios       dist-<name>-<md5>/ -- cache/plan.json names the local packages
  script-builds  <hash>/scriptlocation -- names the script it was built for
  logs           logs/ghc-<version>/ -- the version matches a live compiler

``list`` shows every entry with its size, last write, child count, and owner.
``clean`` removes the orphans, but only with ``--delete``; it reports and exits
otherwise, and ``--dry-run`` says so explicitly so that a stray ``--delete``
fails instead of deleting.

Things this deliberately does not do. It never prunes individual packages
*within* a live cabal store: entries are also registered in that store's
``package.db``, so removing one means dropping its ``.conf`` and recaching, and
the only available signal for "unused" is each project's last ``plan.json``,
which goes stale the moment you switch branch or compiler. It skips
``*-inplace`` stores by default, since those belong to compilers built in a
source tree, which no glob can enumerate, so absence of an owner proves nothing.
It leaves ``logs/build.log`` and ``cabal/packages`` alone, because nothing on
disk names an owner for either. And it never touches anything inside a live
``dist-`` directory, where most of the hie-bios footprint legitimately lives.

One place this departs from proven garbage. A ghcide interface directory is
named after a SHA-1 of the component's whole GHC flag list, which no external
tool can recompute, so the only evidence available is that no live database
still reaches it. A project you have not opened since deleting its database
therefore loses interface files it could have reused. The cost is
recompilation, never a wrong answer. Two things bound it: any database that
cannot be read aborts the sweep, since it alone might have named those
directories, and a running ``haskell-language-server`` puts the whole ghcide
cache off limits.

Should a whole target come back with nothing owned, that target is left alone
and the run exits non-zero. Everything there looks orphaned at once, and a
lookup that broke wholesale, an unmounted home or a ``ghc`` that will not run,
is far likelier than a cache that is genuinely all garbage.

The GHC version baked into a ``.hiedb`` filename is deliberately unused as a
kill signal: it is the GHC that ghcide was *compiled against*, not one cabal
ever invoked, so a locally built HLS would fail the compiler match and lose live
databases. Only ``logs`` keys on a version, because cabal itself made those
directories, one per compiler it ran.

Nix users: run ``nix-collect-garbage -d`` first. Every GHC it removes turns its
store into a fresh orphan.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import click

# DU_BIN overrides the du on PATH, for tests.
DU = os.environ.get("DU_BIN") or "du"

UNIT_ID_RE = re.compile(r'"Project Unit Id"\s*,\s*"([^"]*)"')
VERSION_RE = re.compile(r'"Project Version"\s*,\s*"([^"]*)"')

# The .cradle.opts sidecar is a shown Haskell record; this is the only field of
# it worth reading. Both spellings are live on disk, so accept either.
COMPONENT_ROOT_RE = re.compile(r'\b(?:cc)?[Cc]omponentRoot = "([^"]*)"')

# ghcide names every cache directory <something>-<sha1 of the component's ghc
# options>, covering both "<unit-id>-inplace-<hash>" and the "main-<hash>" form
# it uses for executables (Session/Ghc.hs, cacheDirsUnder).
OPTS_HASH_RE = re.compile(r"-[0-9a-f]{40}$")

# Every place a prebuilt GHC normally lands. Compilers built from a source tree
# are not here by design: their unit id is "<version>-inplace" whatever the
# tree, so pass --ghc-glob to name them.
COMPILER_GLOBS = (
    "/nix/store/*-ghc-*/bin/ghc",
    "~/.ghcup/ghc/*/bin/ghc",
    "~/.stack/programs/*/ghc-*/bin/ghc",
    "/usr/lib/ghc-*/bin/ghc",
)

INPLACE_SUFFIX = "-inplace"

# Bookkeeping cabal keeps alongside the package entries.
NON_ENTRY_DIRS = frozenset({"package.db", "incoming"})

# Files ghcide writes beside a database, which must die with it.
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal", ".cradle.opts")

PRINCIPAL_SUFFIXES = (".hiedb", ".pkg-exports.sqlite")

TABLES_QUERY = "select name from sqlite_master where type='table'"

# Enough of an argv[0] basename to recognise an editor session holding the
# ghcide cache open.
HLS_PROCESS_PREFIXES = ("haskell-language-server", "ghcide", "hls")

# Where the process table lives. A name so the tests need not fake a kernel.
PROC = Path("/proc")

TARGET_NAMES = ("store", "ghcide", "hie-bios", "script-builds", "logs")

# The two targets that need `ghc --info`, which is worth avoiding otherwise.
COMPILER_KEYED = frozenset({"store", "logs"})

# What to check first when a whole target came back unowned. The compiler-keyed
# pair break together on one bad `ghc --info`; the rest break together when the
# filesystem holding the projects is not mounted.
REFUSAL_HINTS = {
    "store": "Check --ghc-glob and that ghc runs.",
    "logs": "Check --ghc-glob and that ghc runs.",
}

DEFAULT_HINT = "Check that the projects these caches belong to are still mounted."


@dataclass
class Entry:
    """One reclaimable thing: a store, a database, a build directory."""

    target: str
    name: str
    path: Path
    root: Path
    kib: int
    last_write: float
    entries: int
    owners: list[str] = field(default_factory=list)
    # Siblings with no independent evidence, removed with this entry.
    extras: list[Path] = field(default_factory=list)
    # Why liveness could not be judged. Set means never an orphan.
    unreadable: str = ""

    @property
    def orphan(self) -> bool:
        return not self.owners and not self.unreadable

    @property
    def inplace(self) -> bool:
        return self.name.endswith(INPLACE_SUFFIX)


@dataclass(frozen=True)
class Compiler:
    """A GHC on this machine, and the two names caches key on."""

    unit_id: str
    version: str
    prefix: str


@dataclass(frozen=True)
class Roots:
    """Where every target lives, once the overrides have been applied."""

    store: Path
    cache_home: Path
    ghc_globs: tuple[str, ...]

    @property
    def ghcide(self) -> Path:
        return self.cache_home / "ghcide"

    @property
    def hie_bios(self) -> Path:
        env = os.environ.get("HIE_BIOS_CACHE_DIR")
        return Path(env).expanduser() if env else self.cache_home / "hie-bios"

    @property
    def script_builds(self) -> Path:
        return self.cache_home / "cabal" / "script-builds"

    @property
    def logs(self) -> Path:
        return self.cache_home / "cabal" / "logs"


def default_store() -> Path:
    """The store cabal would use, honouring the XDG move it made in 3.12."""
    env = os.environ.get("CABAL_STORE_DIR")
    if env:
        return Path(env).expanduser()
    state = Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser()
    xdg = state / "cabal" / "store"
    return xdg if xdg.is_dir() else Path("~/.cabal/store").expanduser()


def default_cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or "~/.cache").expanduser()


def compiler_paths(extra_globs: tuple[str, ...]) -> list[str]:
    """Every ghc executable worth interrogating, deduplicated."""
    found: list[str] = []
    for pattern in (*COMPILER_GLOBS, *extra_globs):
        found.extend(glob.glob(os.path.expanduser(pattern)))
    which = shutil.which("ghc")
    if which:
        found.append(which)
    seen: set[str] = set()
    unique = []
    for path in found:
        real = os.path.realpath(path)
        if real not in seen and os.access(real, os.X_OK):
            seen.add(real)
            unique.append(path)
    return unique


def probe(ghc: str) -> Compiler | None:
    """The store name and version this compiler owns, or None if unusable."""

    def run(*args: str) -> str | None:
        try:
            proc = subprocess.run(
                [ghc, *args], capture_output=True, text=True, timeout=120, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    info = run("--info")
    if info is None:
        return None
    found = VERSION_RE.search(info)
    version = found.group(1) if found else None
    if version is None:
        numeric = run("--numeric-version")
        version = numeric.strip() if numeric else None
    if not version:
        return None
    unit = UNIT_ID_RE.search(info)
    # No "Project Unit Id" field (GHC 9.6 and older): cabal falls back to the
    # bare compiler id, e.g. ghc-9.6.7.
    return Compiler(
        unit_id=unit.group(1) if unit else f"ghc-{version}",
        version=version,
        prefix=str(Path(ghc).parent.parent),
    )


def discover_compilers(extra_globs: tuple[str, ...]) -> list[Compiler]:
    """Interrogate every GHC on the machine, in parallel."""
    paths = compiler_paths(extra_globs)
    with ThreadPoolExecutor(max_workers=8) as pool:
        return [found for found in pool.map(probe, paths) if found]


def owners_by(compilers: list[Compiler], key: str) -> dict[str, list[str]]:
    """Group compiler install prefixes by unit id or by version."""
    grouped: dict[str, list[str]] = {}
    for compiler in compilers:
        grouped.setdefault(getattr(compiler, key), []).append(compiler.prefix)
    return grouped


def du_kib(path: Path) -> int:
    """Disk usage in KiB. Shelling out to du beats os.walk on a 100k-file tree."""
    try:
        proc = subprocess.run(
            [DU, "-sk", str(path)],
            capture_output=True,
            text=True,
            timeout=3600,
            check=False,
        )
        return int(proc.stdout.split()[0])
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return 0


def measure(
    target: str, path: Path, root: Path, skip: frozenset[str] = frozenset()
) -> Entry:
    """Size, child count, and the last time anything landed in this entry."""
    last_write = path.stat().st_mtime
    entries = 0
    if path.is_dir():
        for item in os.scandir(path):
            if item.name not in skip:
                entries += 1
            last_write = max(last_write, item.stat(follow_symlinks=False).st_mtime)
    else:
        entries = 1
    return Entry(
        target=target,
        name=path.name,
        path=path,
        root=root,
        kib=du_kib(path),
        last_write=last_write,
        entries=entries,
    )


def measure_all(
    target: str, paths: list[Path], root: Path, skip: frozenset[str] = frozenset()
) -> list[Entry]:
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(lambda p: measure(target, p, root, skip), paths))


def child_dirs(root: Path) -> list[Path]:
    """Immediate subdirectories, symlinks excluded so no rmtree escapes."""
    if not root.is_dir():
        return []
    return [d for d in sorted(root.iterdir()) if d.is_dir() and not d.is_symlink()]


def common_root(paths: Iterable[str]) -> list[str]:
    """One owner string covering every surviving path, for the table."""
    kept = sorted(set(paths))
    if not kept:
        return []
    try:
        return [os.path.commonpath(kept)]
    except ValueError:
        return kept[:1]


def collect_store(roots: Roots, compilers: list[Compiler]) -> list[Entry]:
    """Stores whose owning compiler no longer exists."""
    owners = owners_by(compilers, "unit_id")
    entries = measure_all("store", child_dirs(roots.store), roots.store, NON_ENTRY_DIRS)
    for entry in entries:
        entry.owners = owners.get(entry.name, [])
    return entries


Rows = list[tuple[str, str]]


def query_mods(source: str) -> tuple[Rows | None, str]:
    """Every (hs_src, hieFile) pair, or None and why it could not be read.

    Asking sqlite_master for the table list before touching mods is what
    separates the two outcomes that matter: a database another process holds
    raises on that first query, while one ghcide created but never indexed
    answers with an empty list. The first is no evidence at all; the second is
    a positive reading that it holds nothing.
    """
    try:
        conn = sqlite3.connect(source, uri=source.startswith("file:"))
    except sqlite3.Error as exc:
        return None, str(exc)
    try:
        if "mods" not in {r[0] for r in conn.execute(TABLES_QUERY)}:
            return [], ""
        return conn.execute("select hs_src, hieFile from mods").fetchall(), ""
    except sqlite3.Error as exc:
        return None, str(exc)
    finally:
        conn.close()


def query_recovered(db: Path) -> tuple[Rows | None, str]:
    """Replay a crashed session's journal into a copy, leaving the original be.

    sqlite will not roll a hot journal back through a read-only open, and one
    database we cannot read blocks the entire sweep, so the copy earns itself.
    """
    try:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / db.name
            for suffix in ("", *SIDECAR_SUFFIXES):
                sibling = db.with_name(db.name + suffix)
                if sibling.is_file():
                    shutil.copy2(sibling, copy.with_name(copy.name + suffix))
            return query_mods(str(copy))
    except OSError as exc:
        return None, str(exc)


def read_hiedb(db: Path) -> tuple[list[str], list[str], str]:
    """(surviving source roots, referenced hie files, why it was unreadable)."""
    rows, why = query_mods(f"file:{db}?mode=ro")
    if rows is None:
        rows, why = query_recovered(db)
    if rows is None:
        return [], [], why
    alive = [src for src, _ in rows if src and os.path.exists(src)]
    reached = [hie for _, hie in rows if hie]
    return common_root(os.path.dirname(src) for src in alive), reached, ""


def read_cradle_opts(sidecar: Path) -> tuple[list[str], str]:
    """The component root an experimental pkg-exports index was built for."""
    try:
        text = sidecar.read_text(errors="replace")
    except OSError as exc:
        return [], str(exc)
    found = COMPONENT_ROOT_RE.search(text)
    if not found:
        return [], f"no ccComponentRoot in {sidecar.name}"
    return ([found.group(1)] if os.path.exists(found.group(1)) else []), ""


def split_sidecars(root: Path) -> tuple[list[Path], dict[Path, list[Path]], list[Path]]:
    """Principal databases, the sidecars belonging to each, and stray sidecars."""
    files = [f for f in sorted(root.iterdir()) if f.is_file() and not f.is_symlink()]
    principals = [f for f in files if f.name.endswith(PRINCIPAL_SUFFIXES)]
    present = {f.name for f in files}
    attached: dict[Path, list[Path]] = {p: [] for p in principals}
    stray: list[Path] = []
    for item in files:
        for suffix in SIDECAR_SUFFIXES:
            if not item.name.endswith(suffix):
                continue
            base = root / item.name[: -len(suffix)]
            if base.name in present:
                attached.setdefault(base, []).append(item)
            else:
                stray.append(item)
            break
    return principals, attached, stray


def collect_ghcide(roots: Roots, compilers: list[Compiler]) -> list[Entry]:
    """Databases, their sidecars, and the interface directories they reach."""
    root = roots.ghcide
    if not root.is_dir():
        return []
    principals, attached, stray = split_sidecars(root)
    entries = measure_all("ghcide", principals, root)
    # Interface directory name -> the project whose database still reaches it.
    referenced: dict[str, str] = {}
    blind: list[str] = []
    # Settle this before reading anything: a session holding the cache open
    # puts every entry off limits, so the reads would only be wasted work, and
    # the recovery copy in query_recovered could be a gigabyte of it.
    held = session_holding(root)
    for entry in entries:
        entry.extras = attached.get(entry.path, [])
        if held:
            continue
        if entry.name.endswith(".hiedb"):
            entry.owners, reached, entry.unreadable = read_hiedb(entry.path)
        else:
            sidecar = entry.path.with_name(entry.path.name + ".cradle.opts")
            entry.owners, entry.unreadable = read_cradle_opts(sidecar)
            reached = []
        if entry.unreadable:
            blind.append(entry.name)
        elif entry.owners:
            for name in filter(None, (cache_dir_of(root, p) for p in reached)):
                referenced.setdefault(name, entry.owners[0])
    entries += measure_all("ghcide", stray, root)
    # An unreadable database contributes no hieFile rows, so sweeping past one
    # would delete directories it alone still reaches.
    blocked = f"{len(blind)} unreadable database(s), e.g. {blind[0]}" if blind else ""
    entries += collect_ghcide_caches(root, referenced, blocked)
    if held:
        for entry in entries:
            entry.unreadable = held
    return entries


def cache_dir_of(root: Path, hie_file: str) -> str:
    """The directory under the ghcide root a recorded .hie file sits in."""
    try:
        return Path(os.path.normpath(hie_file)).relative_to(root).parts[0]
    except (ValueError, IndexError):
        return ""


def session_holding(root: Path) -> str:
    """Why nothing under the ghcide root may be touched, or "" when it may.

    A running session owns every file here, not just the sweep: it holds
    databases open and writes interface files as it goes.
    """
    running = hls_running()
    if running is None:
        return f"cannot check for a session holding {root}: no /proc"
    return f"{', '.join(running)} is running" if running else ""


def collect_ghcide_caches(
    root: Path, referenced: dict[str, str], blocked: str
) -> list[Entry]:
    """Interface directories, orphaned when no live database names them.

    The directory name ends in a SHA-1 over the component's full GHC flag list,
    which no external tool can recompute without running the cradle. Marking
    from the databases is the only evidence available.
    """
    dirs = [d for d in child_dirs(root) if OPTS_HASH_RE.search(d.name)]
    entries = measure_all("ghcide", dirs, root)
    for entry in entries:
        owner = referenced.get(entry.name)
        if owner:
            entry.owners = [owner]
        else:
            entry.unreadable = blocked
    return entries


def hls_running() -> list[str] | None:
    """Names of any running HLS, or None when /proc cannot tell us."""
    if not PROC.is_dir():
        return None
    found: set[str] = set()
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if not argv or not argv[0]:
            continue
        # Nix runs the wrapped binary as .haskell-language-server-9.10.3-unwrapped,
        # so the leading dot has to go before the prefix means anything.
        name = os.path.basename(argv[0].decode("utf-8", "replace")).lstrip(".")
        if name.startswith(HLS_PROCESS_PREFIXES):
            found.add(name)
    return sorted(found)


def read_plan(dist: Path) -> tuple[list[str], str]:
    """The local packages a hie-bios build directory was made for."""
    plan = dist / "cache" / "plan.json"
    try:
        data = json.loads(plan.read_text())
    except (OSError, ValueError):
        return [], "no readable cache/plan.json"
    if not isinstance(data, dict):
        return [], "cache/plan.json is not a plan"
    local: list[str] = []
    for unit in data.get("install-plan") or []:
        src = unit.get("pkg-src") or {}
        # Cabal writes paths with /./ segments, e.g. /home/me/proj/./proj.
        if src.get("type") == "local" and src.get("path"):
            local.append(os.path.normpath(src["path"]))
    if not local:
        return [], "cache/plan.json names no local package"
    return common_root(path for path in local if os.path.isdir(path)), ""


def collect_hie_bios(roots: Roots, compilers: list[Compiler]) -> list[Entry]:
    """Cabal build directories whose project has gone.

    The md5 in the directory name is over the absolute cradle work directory,
    so the readable basename beside it is a hint and never a proof; the plan is.
    """
    root = roots.hie_bios
    dirs = [d for d in child_dirs(root) if d.name.startswith("dist-")]
    entries = measure_all("hie-bios", dirs, root)
    for entry in entries:
        entry.owners, entry.unreadable = read_plan(entry.path)
    return entries


def collect_script_builds(roots: Roots, compilers: list[Compiler]) -> list[Entry]:
    """Build directories for `cabal run` scripts that no longer exist."""
    root = roots.script_builds
    entries = measure_all("script-builds", child_dirs(root), root)
    for entry in entries:
        marker = entry.path / "scriptlocation"
        try:
            script = marker.read_text().strip()
        except OSError:
            entry.unreadable = "no readable scriptlocation"
            continue
        if not script:
            entry.unreadable = "empty scriptlocation"
        elif os.path.exists(script):
            entry.owners = [script]
    return entries


def collect_logs(roots: Roots, compilers: list[Compiler]) -> list[Entry]:
    """Build logs for a compiler that has gone.

    Safe to key on the version here, unlike the .hiedb names, because cabal
    itself made these directories, one per compiler it actually invoked.
    """
    root = roots.logs
    owners = owners_by(compilers, "version")
    dirs = [d for d in child_dirs(root) if d.name.startswith("ghc-")]
    entries = measure_all("logs", dirs, root)
    for entry in entries:
        entry.owners = owners.get(entry.name[len("ghc-") :], [])
    return entries


COLLECTORS = {
    "store": collect_store,
    "ghcide": collect_ghcide,
    "hie-bios": collect_hie_bios,
    "script-builds": collect_script_builds,
    "logs": collect_logs,
}


def gather(targets: tuple[str, ...], roots: Roots) -> list[Entry]:
    """Every entry across the chosen targets, sorted small to large."""
    compilers = (
        discover_compilers(roots.ghc_globs) if COMPILER_KEYED & set(targets) else []
    )
    entries: list[Entry] = []
    for target in targets:
        entries.extend(COLLECTORS[target](roots, compilers))
    return sorted(entries, key=lambda e: e.kib)


def human(kib: int) -> str:
    if kib < 1024:
        return f"{kib}K"
    mib = kib / 1024
    return f"{mib:.0f}M" if mib < 1024 else f"{mib / 1024:.1f}G"


def _local_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).astimezone().date().isoformat()


def table(entries: list[Entry], show_owner: bool) -> None:
    targets = {e.target for e in entries}
    tag = max((len(t) for t in targets), default=0) + 3 if len(targets) > 1 else 0
    width = max((len(e.name) for e in entries), default=0)
    for entry in entries:
        prefix = f"[{entry.target}]".ljust(tag) if tag else ""
        line = (
            f"{prefix}{entry.name:<{width}}  {human(entry.kib):>7}"
            f"  {_local_date(entry.last_write)}  {entry.entries:>6}"
        )
        if show_owner:
            owner = entry.owners[0] if entry.owners else "MISSING"
            if entry.unreadable:
                owner = "?"
            extra = len(entry.owners) - 1
            line += f"  {owner}" + (f" (+{extra})" if extra > 0 else "")
        click.echo(line)


def report_unreadable(entries: list[Entry]) -> None:
    """Say what was skipped, so a gap in coverage never reads as clean."""
    reasons: dict[str, int] = {}
    for entry in entries:
        if entry.unreadable:
            reasons[entry.unreadable] = reasons.get(entry.unreadable, 0) + 1
    for reason, count in sorted(reasons.items()):
        click.echo(f"kept {count} entries with no verdict: {reason}", err=True)


def resolve_roots(
    store: Path | None, cache_home: Path | None, ghc_globs: tuple[str, ...]
) -> Roots:
    """Apply the overrides, failing loudly when one names nothing."""
    if store is not None and not store.is_dir():
        raise click.ClickException(f"no cabal store at {store}")
    if cache_home is not None and not cache_home.is_dir():
        raise click.ClickException(f"no cache directory at {cache_home}")
    return Roots(
        store=store or default_store(),
        cache_home=cache_home or default_cache_home(),
        ghc_globs=ghc_globs,
    )


def refusals(entries: list[Entry]) -> dict[str, str]:
    """Targets where not one entry looked owned, which smells like a bad oracle.

    Everything there looks orphaned at once, and a lookup that broke wholesale
    is far likelier than a machine that genuinely owns none of it. Leave those
    targets alone and say so, rather than taking the whole cache with them.
    """
    blocked: dict[str, str] = {}
    for target in TARGET_NAMES:
        # Entries with no verdict prove nothing either way, so a target made
        # entirely of them is quiet rather than suspicious.
        judged = [e for e in entries if e.target == target and not e.unreadable]
        if judged and not any(e.owners for e in judged):
            blocked[target] = (
                f"no {target} entry matched an owner; refusing to treat all "
                f"{len(judged)} as garbage. {REFUSAL_HINTS.get(target, DEFAULT_HINT)}"
            )
    return blocked


def shared_options(command):
    command = click.option(
        "--ghc-glob",
        "ghc_globs",
        multiple=True,
        metavar="GLOB",
        help="Extra ghc executables to count as owners, e.g. "
        "'~/src/ghc*/_build/stage1/bin/ghc'. Repeatable.",
    )(command)
    command = click.option(
        "--cache-home",
        type=click.Path(path_type=Path),
        help="Cache root for every target but the store. Defaults to "
        "$XDG_CACHE_HOME, else ~/.cache.",
    )(command)
    command = click.option(
        "--store",
        type=click.Path(path_type=Path),
        help="Cabal store root. Defaults to $CABAL_STORE_DIR, else the XDG path.",
    )(command)
    return click.option(
        "--target",
        "targets",
        multiple=True,
        type=click.Choice(TARGET_NAMES),
        help="Cache to act on. Repeatable; defaults to all of them.",
    )(command)


@click.group()
def cli() -> None:
    """Inspect and reclaim the Haskell caches nothing else collects."""


@cli.command("list")
@shared_options
def list_cmd(
    targets: tuple[str, ...],
    store: Path | None,
    cache_home: Path | None,
    ghc_globs: tuple[str, ...],
) -> None:
    """Show every entry: size, last write, child count, owner.

    \b
    Examples:
        hs-gc-caches list
        hs-gc-caches list --target ghcide
        hs-gc-caches list --ghc-glob '~/personal/ghc*/_build/stage1/bin/ghc'
    """
    roots = resolve_roots(store, cache_home, ghc_globs)
    entries = gather(targets or TARGET_NAMES, roots)
    table(entries, show_owner=True)
    report_unreadable(entries)
    dead = [e for e in entries if e.orphan and not e.inplace]
    total = sum(e.kib for e in entries)
    click.echo(f"\n{human(total)} total across {len(entries)} entries.")
    if dead:
        click.echo(
            f"{human(sum(e.kib for e in dead))} in {len(dead)} orphan(s); "
            "preview with: hs-gc-caches clean --dry-run"
        )


@cli.command()
@shared_options
@click.option(
    "--include-inplace",
    is_flag=True,
    help="Also treat '*-inplace' stores as orphans. Only safe once you are sure "
    "no source tree still builds that compiler.",
)
@click.option("--delete", is_flag=True, help="Actually remove. Default is a dry run.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would go, remove nothing. Already the default; pass it to "
    "say so, and to make a stray --delete an error instead of a deletion.",
)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def clean(
    targets: tuple[str, ...],
    store: Path | None,
    cache_home: Path | None,
    ghc_globs: tuple[str, ...],
    include_inplace: bool,
    delete: bool,
    dry_run: bool,
    yes: bool,
) -> None:
    """Remove every entry whose owner no longer exists on this machine.

    \b
    Examples:
        hs-gc-caches clean --dry-run
        hs-gc-caches clean --target ghcide --delete
    """
    if dry_run and delete:
        raise click.UsageError("--dry-run and --delete contradict each other.")
    roots = resolve_roots(store, cache_home, ghc_globs)
    entries = gather(targets or TARGET_NAMES, roots)
    blocked = refusals(entries)

    orphans = [e for e in entries if e.orphan and e.target not in blocked]
    skipped = [] if include_inplace else [e for e in orphans if e.inplace]
    held = {id(e) for e in skipped}
    doomed = [e for e in orphans if id(e) not in held]
    report_unreadable(entries)
    for why in blocked.values():
        click.echo(why, err=True)
    if skipped:
        click.echo(
            f"skipping {len(skipped)} in-tree store(s) "
            f"({', '.join(s.name for s in skipped)}); pass --include-inplace to "
            "reclaim them.",
            err=True,
        )

    freeable = sum(e.kib for e in doomed)
    if not doomed:
        click.echo("nothing to reclaim.")
    elif not delete:
        table(doomed, show_owner=False)
        click.echo(
            f"\n{human(freeable)} reclaimable from {len(doomed)} entries. "
            "Dry run; pass --delete to remove."
        )
    else:
        table(doomed, show_owner=False)
        remove(doomed, freeable, yes)
    if blocked:
        raise SystemExit(1)


def remove(doomed: list[Entry], freeable: int, yes: bool) -> None:
    """Vet every path against its own root, then take them all."""
    for entry in doomed:
        for path in (entry.path, *entry.extras):
            if path.parent != entry.root or path.is_symlink():
                raise click.ClickException(f"refusing to delete {path}")
    if not yes:
        click.confirm(
            f"\ndelete {len(doomed)} entries, freeing {human(freeable)}?",
            abort=True,
        )
    for entry in doomed:
        if entry.path.is_dir():
            shutil.rmtree(entry.path)
        else:
            entry.path.unlink()
        for extra in entry.extras:
            extra.unlink(missing_ok=True)
        click.echo(f"removed {entry.name} ({human(entry.kib)})")
    click.echo(f"freed {human(freeable)}.")


def main() -> None:
    cli(prog_name="hs-gc-caches")
