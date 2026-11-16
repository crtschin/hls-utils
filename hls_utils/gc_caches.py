"""Report and delete the Haskell caches whose owner no longer exists.

Cabal, HLS, ghcide and hie-bios never collect their caches, and each one puts a
version in its cache names. Every GHC or HLS bump orphans the old generation.

Each target reads a file that names the owner of an entry. An entry is an orphan
only when that read succeeds and the owner is gone. A locked database, a missing
plan.json or an unreadable marker keeps the entry, so a wholesale failure keeps
everything.

We never prune single packages inside a live store. The only signal for an
unused package is the last plan.json of each project, which goes stale on every
branch or compiler switch.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

DU = "du"

UNIT_ID_RE = re.compile(r'"Project Unit Id"\s*,\s*"([^"]*)"')
VERSION_RE = re.compile(r'"Project Version"\s*,\s*"([^"]*)"')

# The .cradle.opts sidecar holds a shown Haskell record. Both spellings of this
# field occur on disk.
COMPONENT_ROOT_RE = re.compile(r'\b(?:cc)?[Cc]omponentRoot = "([^"]*)"')

# ghcide names an interface directory <unit-id>-inplace-<sha1>, or main-<sha1>
# for an executable.
OPTS_HASH_RE = re.compile(r"-[0-9a-f]{40}$")

COMPILER_GLOBS = (
    "/nix/store/*-ghc-*/bin/ghc",
    "~/.ghcup/ghc/*/bin/ghc",
    "~/.stack/programs/*/ghc-*/bin/ghc",
    "/usr/lib/ghc-*/bin/ghc",
)

INPLACE_SUFFIX = "-inplace"

NON_ENTRY_DIRS = frozenset({"package.db", "incoming"})

SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal", ".cradle.opts")

PRINCIPAL_SUFFIXES = (".hiedb", ".pkg-exports.sqlite")

TABLES_QUERY = "select name from sqlite_master where type='table'"

HLS_PROCESS_PREFIXES = ("haskell-language-server", "ghcide", "hls")

PROC = Path("/proc")

TARGET_NAMES = ("store", "ghcide", "hie-bios", "script-builds", "logs")

COMPILER_KEYED = frozenset({"store", "logs"})

REFUSAL_HINTS = {
    "store": "Check --ghc-glob and that ghc runs.",
    "logs": "Check --ghc-glob and that ghc runs.",
}

DEFAULT_HINT = "Check that the projects these caches belong to are still mounted."


@dataclass
class Entry:
    target: str
    name: str
    path: Path
    root: Path
    kib: int
    last_write: float
    entries: int
    owners: list[str] = field(default_factory=list)
    # Sidecar files with no evidence of their own, deleted with this entry.
    extras: list[Path] = field(default_factory=list)
    # The reason we could not judge the owner. A set reason means never an orphan.
    unreadable: str = ""

    @property
    def orphan(self) -> bool:
        return not self.owners and not self.unreadable

    @property
    def inplace(self) -> bool:
        return self.name.endswith(INPLACE_SUFFIX)


@dataclass(frozen=True)
class Compiler:
    unit_id: str
    version: str
    prefix: str


@dataclass(frozen=True)
class Roots:
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
    """The store that cabal uses. Cabal 3.12 moved it to the XDG state directory."""
    env = os.environ.get("CABAL_STORE_DIR")
    if env:
        return Path(env).expanduser()
    state = Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser()
    xdg = state / "cabal" / "store"
    return xdg if xdg.is_dir() else Path("~/.cabal/store").expanduser()


def default_cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or "~/.cache").expanduser()


def compiler_paths(extra_globs: tuple[str, ...]) -> list[str]:
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
    # GHC 9.6 and older report no unit id, and cabal names their store
    # ghc-<version>.
    return Compiler(
        unit_id=unit.group(1) if unit else f"ghc-{version}",
        version=version,
        prefix=str(Path(ghc).parent.parent),
    )


def discover_compilers(extra_globs: tuple[str, ...]) -> list[Compiler]:
    paths = compiler_paths(extra_globs)
    with ThreadPoolExecutor(max_workers=8) as pool:
        return [found for found in pool.map(probe, paths) if found]


def owners_by(compilers: list[Compiler], key: str) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for compiler in compilers:
        grouped.setdefault(getattr(compiler, key), []).append(compiler.prefix)
    return grouped


def du_kib(path: Path) -> int:
    """Disk usage in KiB. du beats os.walk on a tree of 100k files."""
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
    """Subdirectories without symlinks, so that no rmtree escapes the root."""
    if not root.is_dir():
        return []
    return [d for d in sorted(root.iterdir()) if d.is_dir() and not d.is_symlink()]


def common_root(paths: Iterable[str]) -> list[str]:
    kept = sorted(set(paths))
    if not kept:
        return []
    try:
        return [os.path.commonpath(kept)]
    except ValueError:
        return kept[:1]


def collect_store(roots: Roots, compilers: list[Compiler]) -> list[Entry]:
    owners = owners_by(compilers, "unit_id")
    entries = measure_all("store", child_dirs(roots.store), roots.store, NON_ENTRY_DIRS)
    for entry in entries:
        entry.owners = owners.get(entry.name, [])
    return entries


Rows = list[tuple[str, str]]


def query_mods(source: str) -> tuple[Rows | None, str]:
    """Every (hs_src, hieFile) pair, or None and the reason the read failed."""
    try:
        conn = sqlite3.connect(source, uri=source.startswith("file:"))
    except sqlite3.Error as exc:
        return None, str(exc)
    # A database that another process holds fails this first query, which is no
    # evidence. A database that ghcide never indexed has no mods table, which
    # proves that it holds nothing.
    try:
        if "mods" not in {r[0] for r in conn.execute(TABLES_QUERY)}:
            return [], ""
        return conn.execute("select hs_src, hieFile from mods").fetchall(), ""
    except sqlite3.Error as exc:
        return None, str(exc)
    finally:
        conn.close()


def query_recovered(db: Path) -> tuple[Rows | None, str]:
    """Query a copy of *db*, leaving the original untouched.

    sqlite rolls back the hot journal of a crashed session only on a writable open.
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
    root = roots.ghcide
    if not root.is_dir():
        return []
    principals, attached, stray = split_sidecars(root)
    entries = measure_all("ghcide", principals, root)
    referenced: dict[str, str] = {}
    blind: list[str] = []
    # A live session puts every entry off limits. Ask first, because the reads
    # can copy a gigabyte of database for nothing.
    held = session_holding(root)
    for entry in entries:
        entry.extras = attached.get(entry.path, [])
        if held:
            continue
        # The GHC version in a .hiedb name is the one ghcide was built with, so it
        # names no owner. A locally built HLS would lose live databases to it.
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
    try:
        return Path(os.path.normpath(hie_file)).relative_to(root).parts[0]
    except (ValueError, IndexError):
        return ""


def session_holding(root: Path) -> str:
    """The reason to leave the whole ghcide root alone, or "" when none applies."""
    running = hls_running()
    if running is None:
        return f"cannot check for a session holding {root}: no /proc"
    return f"{', '.join(running)} is running" if running else ""


def collect_ghcide_caches(
    root: Path, referenced: dict[str, str], blocked: str
) -> list[Entry]:
    """Interface directories, orphaned when no live database names them."""
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
        # Nix runs the wrapped binary as .haskell-language-server-9.10.3-unwrapped.
        name = os.path.basename(argv[0].decode("utf-8", "replace")).lstrip(".")
        if name.startswith(HLS_PROCESS_PREFIXES):
            found.add(name)
    return sorted(found)


def read_plan(dist: Path) -> tuple[list[str], str]:
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
    """Build directories whose project is gone, judged by the plan alone.

    The readable part of the name is a hint. The md5 hashes the cradle work directory.
    """
    root = roots.hie_bios
    dirs = [d for d in child_dirs(root) if d.name.startswith("dist-")]
    entries = measure_all("hie-bios", dirs, root)
    for entry in entries:
        entry.owners, entry.unreadable = read_plan(entry.path)
    return entries


def collect_script_builds(roots: Roots, compilers: list[Compiler]) -> list[Entry]:
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
    """Logs for a gone compiler. Cabal makes one directory per compiler it ran."""
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
        print(line)


def report_unreadable(entries: list[Entry]) -> None:
    reasons: dict[str, int] = {}
    for entry in entries:
        if entry.unreadable:
            reasons[entry.unreadable] = reasons.get(entry.unreadable, 0) + 1
    for reason, count in sorted(reasons.items()):
        print(f"kept {count} entries with no verdict: {reason}", file=sys.stderr)


def resolve_roots(
    store: Path | None, cache_home: Path | None, ghc_globs: tuple[str, ...]
) -> Roots:
    if store is not None and not store.is_dir():
        raise SystemExit(f"error: no cabal store at {store}")
    if cache_home is not None and not cache_home.is_dir():
        raise SystemExit(f"error: no cache directory at {cache_home}")
    return Roots(
        store=store or default_store(),
        cache_home=cache_home or default_cache_home(),
        ghc_globs=ghc_globs,
    )


def refusals(entries: list[Entry]) -> dict[str, str]:
    """Targets where no judged entry has an owner, which clean leaves alone.

    When every entry looks orphaned at once, the owner lookup most likely broke.
    """
    blocked: dict[str, str] = {}
    for target in TARGET_NAMES:
        # Entries with no verdict prove nothing, so a target made only of them
        # raises no alarm.
        judged = [e for e in entries if e.target == target and not e.unreadable]
        if judged and not any(e.owners for e in judged):
            blocked[target] = (
                f"no {target} entry matched an owner; refusing to treat all "
                f"{len(judged)} as garbage. {REFUSAL_HINTS.get(target, DEFAULT_HINT)}"
            )
    return blocked


def add_shared_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--target",
        dest="targets",
        action="append",
        choices=TARGET_NAMES,
        help="Cache to act on. Repeatable. Defaults to all of them.",
    )
    parser.add_argument(
        "--store",
        type=Path,
        help="Cabal store root. Defaults to $CABAL_STORE_DIR, else the XDG path.",
    )
    parser.add_argument(
        "--cache-home",
        type=Path,
        help="Cache root for every target but the store. Defaults to "
        "$XDG_CACHE_HOME, else ~/.cache.",
    )
    parser.add_argument(
        "--ghc-glob",
        dest="ghc_globs",
        action="append",
        default=[],
        metavar="GLOB",
        help="Extra ghc executables to count as owners, e.g. "
        "'~/src/ghc*/_build/stage1/bin/ghc'. Repeatable.",
    )


def list_cmd(args: argparse.Namespace) -> None:
    roots = resolve_roots(args.store, args.cache_home, tuple(args.ghc_globs))
    entries = gather(args.targets or TARGET_NAMES, roots)
    table(entries, show_owner=True)
    report_unreadable(entries)
    dead = [e for e in entries if e.orphan and not e.inplace]
    total = sum(e.kib for e in entries)
    print(f"\n{human(total)} total across {len(entries)} entries.")
    if dead:
        print(
            f"{human(sum(e.kib for e in dead))} in {len(dead)} orphan(s); "
            "preview with: hs-gc-caches clean --dry-run"
        )


def clean(args: argparse.Namespace) -> None:
    roots = resolve_roots(args.store, args.cache_home, tuple(args.ghc_globs))
    entries = gather(args.targets or TARGET_NAMES, roots)
    blocked = refusals(entries)

    orphans = [e for e in entries if e.orphan and e.target not in blocked]
    skipped = [] if args.include_inplace else [e for e in orphans if e.inplace]
    held = {id(e) for e in skipped}
    doomed = [e for e in orphans if id(e) not in held]
    report_unreadable(entries)
    for why in blocked.values():
        print(why, file=sys.stderr)
    if skipped:
        print(
            f"skipping {len(skipped)} in-tree store(s) "
            f"({', '.join(s.name for s in skipped)}); pass --include-inplace to "
            "reclaim them.",
            file=sys.stderr,
        )

    freeable = sum(e.kib for e in doomed)
    if not doomed:
        print("nothing to reclaim.")
    elif not args.delete:
        table(doomed, show_owner=False)
        print(
            f"\n{human(freeable)} reclaimable from {len(doomed)} entries. "
            "Dry run; pass --delete to remove."
        )
    else:
        table(doomed, show_owner=False)
        remove(doomed, freeable, args.yes)
    if blocked:
        raise SystemExit(1)


def remove(doomed: list[Entry], freeable: int, yes: bool) -> None:
    """Validate every path before deleting any."""
    for entry in doomed:
        for path in (entry.path, *entry.extras):
            if path.parent != entry.root or path.is_symlink():
                raise SystemExit(f"error: refusing to delete {path}")
    if not yes:
        answer = input(
            f"\ndelete {len(doomed)} entries, freeing {human(freeable)}? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            raise SystemExit("aborted.")
    for entry in doomed:
        if entry.path.is_dir():
            shutil.rmtree(entry.path)
        else:
            entry.path.unlink()
        for extra in entry.extras:
            extra.unlink(missing_ok=True)
        print(f"removed {entry.name} ({human(entry.kib)})")
    print(f"freed {human(freeable)}.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="hs-gc-caches",
        description="Inspect and reclaim the Haskell caches nothing else collects.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    list_parser = commands.add_parser(
        "list",
        help="Show every entry: size, last write, child count, owner.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  hs-gc-caches list
  hs-gc-caches list --target ghcide
  hs-gc-caches list --ghc-glob '~/personal/ghc*/_build/stage1/bin/ghc'""",
    )
    add_shared_options(list_parser)
    list_parser.set_defaults(run=list_cmd)

    clean_parser = commands.add_parser(
        "clean",
        help="Remove every entry whose owner no longer exists on this machine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  hs-gc-caches clean --dry-run
  hs-gc-caches clean --target ghcide --delete""",
    )
    add_shared_options(clean_parser)
    clean_parser.add_argument(
        "--include-inplace",
        action="store_true",
        help="Also treat '*-inplace' stores as orphans. Only safe once you are "
        "sure no source tree still builds that compiler.",
    )
    clean_parser.add_argument(
        "--delete", action="store_true", help="Actually remove. Default is a dry run."
    )
    clean_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would go and remove nothing. This is the default. "
        "Passing it turns a stray --delete into an error.",
    )
    clean_parser.add_argument(
        "--yes", action="store_true", help="Skip the confirmation prompt."
    )
    clean_parser.set_defaults(run=clean)

    args = parser.parse_args(argv)
    if args.command == "clean" and args.dry_run and args.delete:
        clean_parser.error("--dry-run and --delete contradict each other.")
    args.run(args)
