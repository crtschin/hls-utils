from __future__ import annotations

import contextlib
import io
import shutil
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from hls_utils import gc_caches
from hls_utils.gc_caches import (
    default_store,
    # Bound here because the isolated fixture stubs the module attribute out.
    hls_running,
    human,
    main,
    probe,
)

DU_PATH = shutil.which("du") or "/usr/bin/du"

MODS_SCHEMA = (
    "create table mods (hieFile text, mod text, unit text, is_boot int, "
    "hs_src text, is_real int, hash text)"
)

INFO_WITH_UNIT_ID = """\
 [("GHC extra libraries",""),("Project Unit Id","ghc-9.10.3-4c5b")
 ,("Booter version","9.6.7")
 ]
"""

INFO_WITHOUT_UNIT_ID = """\
 [("GHC extra libraries",""),("Booter version","9.4.8")
 ]
"""

INFO_WITH_VERSION = """\
 [("Project Unit Id","ghc-9.10.3-4c5b"),("Project Version","9.10.3")
 ]
"""


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Keep the GHCs and editor sessions of this machine out of every test."""
    monkeypatch.setattr(gc_caches, "COMPILER_GLOBS", ())
    monkeypatch.setattr(gc_caches, "hls_running", list)
    monkeypatch.delenv("HIE_BIOS_CACHE_DIR", raising=False)
    empty = tmp_path / "empty-path"
    empty.mkdir()
    (tmp_path / "cache").mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(gc_caches, "DU", DU_PATH)


@pytest.fixture
def cache(tmp_path) -> Path:
    return tmp_path / "cache"


def fake_ghc(path: Path, info: str, version: str) -> Path:
    """A stand-in ghc in shell builtins alone, since PATH is empty in these tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    quoted = info.replace("'", "'\\''")
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--numeric-version" ]; then\n'
        f"  printf '%s\\n' '{version}'\n"
        "else\n"
        f"  printf '%s' '{quoted}'\n"
        "fi\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def make_store(root: Path, names: list[str]) -> Path:
    store = root / "store"
    for name in names:
        (store / name / "package.db").mkdir(parents=True)
        (store / name / "some-pkg-0.1-abcdef").mkdir(parents=True)
    return store


def make_hiedb(
    path: Path, mods: list[tuple[str, str]], *, indexed: bool = True
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    if indexed:
        conn.execute(MODS_SCHEMA)
        conn.executemany("insert into mods (hieFile, hs_src) values (?, ?)", mods)
    else:
        conn.execute("pragma user_version = 1")
    conn.commit()
    conn.close()
    return path


def make_live_db(root: Path, tmp_path: Path) -> Path:
    """A database that a live project owns. Without one, clean refuses the target."""
    src = tmp_path / "alive" / "Main.hs"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("main = pure ()")
    return make_hiedb(root / "00-alive-9.10.3-2.hiedb", [("", str(src))])


def make_cache_dir(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "Main.hie").write_text("hie")
    return path


def make_dist(root: Path, name: str, srcs: list[str] | None) -> Path:
    dist = root / name
    (dist / "cache").mkdir(parents=True)
    (dist / "build").mkdir()
    if srcs is not None:
        plan = {
            "install-plan": [
                {"pkg-src": {"type": "local", "path": src}} for src in srcs
            ]
        }
        (dist / "cache" / "plan.json").write_text(_json(plan))
    return dist


def _json(value) -> str:
    import json

    return json.dumps(value)


def make_script_build(root: Path, name: str, script: str | None) -> Path:
    build = root / name
    build.mkdir(parents=True)
    (build / "build").mkdir()
    if script is not None:
        (build / "scriptlocation").write_text(script + "\n")
    return build


@dataclass
class Result:
    exit_code: int
    output: str


def invoke(argv: list[str]) -> Result:
    """Run the CLI with stdout and stderr captured together."""
    out = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            main(argv)
        except SystemExit as exc:
            if isinstance(exc.code, str):
                print(exc.code, file=out)
                code = 1
            else:
                code = exc.code or 0
    return Result(code, out.getvalue())


def run(store: Path, ghc_bin: Path | None, *args: str):
    globs = ["--ghc-glob", str(ghc_bin)] if ghc_bin else []
    return invoke(
        [
            args[0],
            "--store",
            str(store),
            "--cache-home",
            str(store.parent / "cache"),
            *globs,
            *args[1:],
        ],
    )


def run_target(cache: Path, target: str, *args: str):
    return invoke([args[0], "--cache-home", str(cache), "--target", target, *args[1:]])


def test_probe_reads_project_unit_id(tmp_path):
    ghc = fake_ghc(tmp_path / "bin/ghc", INFO_WITH_UNIT_ID, "9.10.3")
    found = probe(str(ghc))
    assert found is not None
    assert found.unit_id == "ghc-9.10.3-4c5b"
    assert found.version == "9.10.3"


def test_probe_prefers_project_version_over_a_second_call(tmp_path):
    ghc = fake_ghc(tmp_path / "bin/ghc", INFO_WITH_VERSION, "wrong")
    found = probe(str(ghc))
    assert found is not None
    assert found.version == "9.10.3"


def test_probe_falls_back_to_compiler_id(tmp_path):
    ghc = fake_ghc(tmp_path / "bin/ghc", INFO_WITHOUT_UNIT_ID, "9.6.7")
    found = probe(str(ghc))
    assert found is not None
    assert found.unit_id == "ghc-9.6.7"


def test_probe_none_when_unrunnable(tmp_path):
    assert probe(str(tmp_path / "nope")) is None


def test_default_store_prefers_xdg(tmp_path, monkeypatch):
    monkeypatch.delenv("CABAL_STORE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    (tmp_path / "cabal/store").mkdir(parents=True)
    assert default_store() == tmp_path / "cabal/store"


def test_default_store_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CABAL_STORE_DIR", str(tmp_path / "elsewhere"))
    assert default_store() == tmp_path / "elsewhere"


def test_list_marks_missing_compilers(tmp_path):
    store = make_store(tmp_path, ["ghc-9.10.3-4c5b", "ghc-9.8.4-dead"])
    ghc = fake_ghc(tmp_path / "bin/ghc", INFO_WITH_UNIT_ID, "9.10.3")
    result = run(store, ghc, "list")
    assert result.exit_code == 0, result.output
    lines = {line.split()[0]: line for line in result.output.splitlines() if line}
    assert "MISSING" in lines["ghc-9.8.4-dead"]
    assert "MISSING" not in lines["ghc-9.10.3-4c5b"]
    assert "1 orphan" in result.output


def test_clean_dry_run_keeps_everything(tmp_path):
    store = make_store(tmp_path, ["ghc-9.10.3-4c5b", "ghc-9.8.4-dead"])
    ghc = fake_ghc(tmp_path / "bin/ghc", INFO_WITH_UNIT_ID, "9.10.3")
    result = run(store, ghc, "clean")
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert (store / "ghc-9.8.4-dead").is_dir()


def test_clean_explicit_dry_run_keeps_everything(tmp_path):
    store = make_store(tmp_path, ["ghc-9.10.3-4c5b", "ghc-9.8.4-dead"])
    ghc = fake_ghc(tmp_path / "bin/ghc", INFO_WITH_UNIT_ID, "9.10.3")
    result = run(store, ghc, "clean", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert (store / "ghc-9.8.4-dead").is_dir()


def test_clean_rejects_dry_run_with_delete(tmp_path):
    store = make_store(tmp_path, ["ghc-9.10.3-4c5b", "ghc-9.8.4-dead"])
    ghc = fake_ghc(tmp_path / "bin/ghc", INFO_WITH_UNIT_ID, "9.10.3")
    result = run(store, ghc, "clean", "--dry-run", "--delete", "--yes")
    assert result.exit_code != 0
    assert "contradict" in result.output
    assert (store / "ghc-9.8.4-dead").is_dir()


def test_clean_delete_removes_only_orphans(tmp_path):
    store = make_store(tmp_path, ["ghc-9.10.3-4c5b", "ghc-9.8.4-dead"])
    ghc = fake_ghc(tmp_path / "bin/ghc", INFO_WITH_UNIT_ID, "9.10.3")
    result = run(store, ghc, "clean", "--delete", "--yes")
    assert result.exit_code == 0, result.output
    assert not (store / "ghc-9.8.4-dead").exists()
    assert (store / "ghc-9.10.3-4c5b").is_dir()


def test_clean_skips_inplace_by_default(tmp_path):
    store = make_store(tmp_path, ["ghc-9.10.3-4c5b", "ghc-9.12.3-inplace"])
    ghc = fake_ghc(tmp_path / "bin/ghc", INFO_WITH_UNIT_ID, "9.10.3")
    result = run(store, ghc, "clean", "--delete", "--yes")
    assert result.exit_code == 0, result.output
    assert (store / "ghc-9.12.3-inplace").is_dir()
    assert "--include-inplace" in result.output

    forced = run(store, ghc, "clean", "--delete", "--yes", "--include-inplace")
    assert forced.exit_code == 0, forced.output
    assert not (store / "ghc-9.12.3-inplace").exists()


def test_clean_refuses_when_no_compiler_matches(tmp_path):
    store = make_store(tmp_path, ["ghc-9.10.3-4c5b"])
    result = run(store, None, "clean", "--delete", "--yes")
    assert result.exit_code != 0
    assert "refusing" in result.output
    assert (store / "ghc-9.10.3-4c5b").is_dir()


def test_clean_errors_on_missing_store(tmp_path):
    result = run(tmp_path / "absent", None, "clean")
    assert result.exit_code != 0
    assert "no cabal store" in result.output


def test_human_units():
    assert human(512) == "512K"
    assert human(2048) == "2M"
    assert human(3 * 1024 * 1024) == "3.0G"


def test_scan_counts_entries_excluding_bookkeeping(tmp_path):
    store = make_store(tmp_path, ["ghc-9.10.3-4c5b"])
    (store / "ghc-9.10.3-4c5b" / "incoming").mkdir()
    ghc = fake_ghc(tmp_path / "bin/ghc", INFO_WITH_UNIT_ID, "9.10.3")
    result = run(store, ghc, "list")
    assert result.exit_code == 0, result.output
    # package.db and incoming do not count as entries.
    assert result.output.split("\n")[0].split()[3] == "1"


def test_multiple_owners_are_counted(tmp_path):
    store = make_store(tmp_path, ["ghc-9.10.3-4c5b"])
    fake_ghc(tmp_path / "a/bin/ghc", INFO_WITH_UNIT_ID, "9.10.3")
    fake_ghc(tmp_path / "b/bin/ghc", INFO_WITH_UNIT_ID, "9.10.3")
    result = invoke(
        [
            "list",
            "--target",
            "store",
            "--store",
            str(store),
            "--ghc-glob",
            str(tmp_path / "*/bin/ghc"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "(+1)" in result.output
    assert str(tmp_path) in result.output


def test_ghcide_keeps_database_naming_live_sources(tmp_path, cache):
    src = tmp_path / "proj" / "Main.hs"
    src.parent.mkdir(parents=True)
    src.write_text("main = pure ()")
    db = make_hiedb(cache / "ghcide" / "aa-proj-9.10.3-2.hiedb", [("", str(src))])
    result = run_target(cache, "ghcide", "clean", "--delete", "--yes")
    assert result.exit_code == 0, result.output
    assert db.exists()


def test_ghcide_drops_database_naming_gone_sources(tmp_path, cache):
    root = cache / "ghcide"
    make_live_db(root, tmp_path)
    gone = str(tmp_path / "vanished" / "Main.hs")
    db = make_hiedb(root / "bb-vanished-9.10.3-2.hiedb", [("", gone)])
    result = run_target(cache, "ghcide", "clean", "--delete", "--yes")
    assert result.exit_code == 0, result.output
    assert not db.exists()


def test_ghcide_drops_database_ghcide_never_indexed(tmp_path, cache):
    root = cache / "ghcide"
    make_live_db(root, tmp_path)
    db = make_hiedb(root / "cc-extra-dir-1-9.10.3-2.hiedb", [], indexed=False)
    result = run_target(cache, "ghcide", "clean", "--delete", "--yes")
    assert result.exit_code == 0, result.output
    assert not db.exists()


def test_query_recovered_reads_without_touching_the_original(tmp_path):
    """Uses a healthy database, since sqlite rejects a hand-made hot journal."""
    db = make_hiedb(tmp_path / "ff-proj-9.10.3-2.hiedb", [("/hie/Main.hie", "/a.hs")])
    journal = db.with_name(db.name + "-journal")
    journal.write_bytes(b"\0" * 512)
    before = {p: p.read_bytes() for p in (db, journal)}
    rows, why = gc_caches.query_recovered(db)
    assert why == ""
    assert rows == [("/a.hs", "/hie/Main.hie")]
    assert {p: p.read_bytes() for p in (db, journal)} == before


def test_ghcide_keeps_unreadable_database(cache):
    db = cache / "ghcide" / "dd-broken-9.10.3-2.hiedb"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"this is not a database")
    result = run_target(cache, "ghcide", "clean", "--delete", "--yes")
    assert result.exit_code == 0, result.output
    assert db.exists()
    assert "no verdict" in result.output


def test_ghcide_sweeps_cache_dirs_no_live_database_reaches(tmp_path, cache):
    root = cache / "ghcide"
    src = tmp_path / "proj" / "Main.hs"
    src.parent.mkdir(parents=True)
    src.write_text("main = pure ()")
    live = "proj-1.0-inplace-" + "a" * 40
    dead = "proj-1.0-inplace-" + "b" * 40
    make_cache_dir(root, live)
    make_cache_dir(root, dead)
    make_hiedb(
        root / "aa-proj-9.10.3-2.hiedb",
        [(str(root / live / "Main.hie"), str(src))],
    )
    result = run_target(cache, "ghcide", "clean", "--delete", "--yes")
    assert result.exit_code == 0, result.output
    assert (root / live).is_dir()
    assert not (root / dead).exists()


def test_ghcide_sweep_blocked_by_an_unreadable_database(tmp_path, cache):
    root = cache / "ghcide"
    make_live_db(root, tmp_path)
    orphan = "proj-1.0-inplace-" + "b" * 40
    make_cache_dir(root, orphan)
    (root / "dd-broken-9.10.3-2.hiedb").write_bytes(b"not a database")
    gone = str(tmp_path / "vanished" / "Main.hs")
    dead = make_hiedb(root / "bb-vanished-9.10.3-2.hiedb", [("", gone)])
    result = run_target(cache, "ghcide", "clean", "--delete", "--yes")
    assert result.exit_code == 0, result.output
    assert (root / orphan).is_dir()
    assert not dead.exists()


def test_hls_running_sees_through_the_nix_wrapper(monkeypatch, tmp_path):
    proc = tmp_path / "proc"
    (proc / "77258").mkdir(parents=True)
    (proc / "77258" / "cmdline").write_bytes(
        b"/nix/store/x-hls/bin/.haskell-language-server-9.10.3-unwrapped\0--lsp\0"
    )
    (proc / "cpuinfo").write_text("not a pid")
    monkeypatch.setattr(gc_caches, "PROC", proc)
    assert hls_running() == ["haskell-language-server-9.10.3-unwrapped"]


def test_hls_running_none_without_proc(monkeypatch, tmp_path):
    monkeypatch.setattr(gc_caches, "PROC", tmp_path / "absent")
    assert hls_running() is None


def test_ghcide_holds_everything_while_hls_runs(tmp_path, cache, monkeypatch):
    monkeypatch.setattr(gc_caches, "hls_running", lambda: ["haskell-language-server"])
    root = cache / "ghcide"
    orphan = "proj-1.0-inplace-" + "b" * 40
    make_cache_dir(root, orphan)
    gone = str(tmp_path / "vanished" / "Main.hs")
    dead = make_hiedb(root / "bb-vanished-9.10.3-2.hiedb", [("", gone)])
    result = run_target(cache, "ghcide", "clean", "--delete", "--yes")
    assert result.exit_code == 0, result.output
    assert (root / orphan).is_dir()
    assert dead.exists()
    assert "haskell-language-server is running" in result.output


def test_ghcide_journal_dies_with_its_database(tmp_path, cache):
    root = cache / "ghcide"
    make_live_db(root, tmp_path)
    gone = str(tmp_path / "vanished" / "Main.hs")
    db = make_hiedb(root / "bb-vanished-9.10.3-2.hiedb", [("", gone)])
    wal = root / (db.name + "-wal")
    wal.write_text("journal")
    result = run_target(cache, "ghcide", "clean", "--delete", "--yes")
    assert result.exit_code == 0, result.output
    assert not db.exists()
    assert not wal.exists()


def test_ghcide_stray_journal_dies_alone(tmp_path, cache):
    root = cache / "ghcide"
    make_live_db(root, tmp_path)
    stray = root / "ee-gone-9.10.3-2.hiedb-shm"
    stray.write_text("left behind")
    result = run_target(cache, "ghcide", "clean", "--delete", "--yes")
    assert result.exit_code == 0, result.output
    assert not stray.exists()


def test_ghcide_pkg_exports_owned_by_its_cradle_opts(tmp_path, cache):
    root = cache / "ghcide"
    root.mkdir(parents=True)
    project = tmp_path / "proj"
    project.mkdir()
    for name, component in (("live", project), ("dead", tmp_path / "gone")):
        db = root / f"{name}-9.10.3.pkg-exports.sqlite"
        db.write_text("index")
        (root / (db.name + ".cradle.opts")).write_text(
            f'CradleCache {{ccComponentRoot = "{component}"}}'
        )
    result = run_target(cache, "ghcide", "clean", "--delete", "--yes")
    assert result.exit_code == 0, result.output
    assert (root / "live-9.10.3.pkg-exports.sqlite").exists()
    assert not (root / "dead-9.10.3.pkg-exports.sqlite").exists()
    assert not (root / "dead-9.10.3.pkg-exports.sqlite.cradle.opts").exists()


def test_hie_bios_keeps_dist_whose_plan_still_resolves(tmp_path, cache):
    project = tmp_path / "proj"
    project.mkdir()
    dist = make_dist(cache / "hie-bios", "dist-proj-" + "a" * 32, [f"{project}/./."])
    result = run_target(cache, "hie-bios", "clean", "--delete", "--yes")
    assert result.exit_code == 0, result.output
    assert dist.is_dir()


def test_hie_bios_drops_dist_whose_project_is_gone(tmp_path, cache):
    alive = tmp_path / "alive"
    alive.mkdir()
    make_dist(cache / "hie-bios", "dist-alive-" + "a" * 32, [str(alive)])
    dist = make_dist(
        cache / "hie-bios", "dist-gone-" + "b" * 32, [str(tmp_path / "vanished")]
    )
    result = run_target(cache, "hie-bios", "clean", "--delete", "--yes")
    assert result.exit_code == 0, result.output
    assert not dist.exists()


def test_hie_bios_keeps_dist_with_no_plan(cache):
    dist = make_dist(cache / "hie-bios", "dist-failed-" + "c" * 32, None)
    result = run_target(cache, "hie-bios", "clean", "--delete", "--yes")
    assert result.exit_code == 0, result.output
    assert dist.is_dir()
    assert "no verdict" in result.output


def test_script_builds_follow_scriptlocation(tmp_path, cache):
    script = tmp_path / "run.hs"
    script.write_text("main = pure ()")
    root = cache / "cabal" / "script-builds"
    live = make_script_build(root, "a" * 52, str(script))
    dead = make_script_build(root, "b" * 52, str(tmp_path / "gone.hs"))
    result = run_target(cache, "script-builds", "clean", "--delete", "--yes")
    assert result.exit_code == 0, result.output
    assert live.is_dir()
    assert not dead.exists()


def test_script_builds_keep_dirs_with_no_marker(cache):
    build = make_script_build(cache / "cabal" / "script-builds", "c" * 52, None)
    result = run_target(cache, "script-builds", "clean", "--delete", "--yes")
    assert result.exit_code == 0, result.output
    assert build.is_dir()


def test_logs_die_with_their_compiler(tmp_path, cache):
    root = cache / "cabal" / "logs"
    for name in ("ghc-9.10.3", "ghc-9.8.4"):
        (root / name).mkdir(parents=True)
        (root / name / "pkg-0.1.log").write_text("built")
    (root / "build.log").write_text("append only")
    ghc = fake_ghc(tmp_path / "bin/ghc", INFO_WITH_UNIT_ID, "9.10.3")
    result = invoke(
        [
            "clean",
            "--cache-home",
            str(cache),
            "--target",
            "logs",
            "--ghc-glob",
            str(ghc),
            "--delete",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (root / "ghc-9.10.3").is_dir()
    assert not (root / "ghc-9.8.4").exists()
    assert (root / "build.log").exists()
    assert "build.log" not in result.output


def test_logs_refuse_when_no_compiler_matches(cache):
    root = cache / "cabal" / "logs"
    (root / "ghc-9.8.4").mkdir(parents=True)
    result = run_target(cache, "logs", "clean", "--delete", "--yes")
    assert result.exit_code != 0
    assert "refusing" in result.output
    assert (root / "ghc-9.8.4").is_dir()


def test_target_column_appears_only_across_targets(tmp_path, cache):
    make_dist(cache / "hie-bios", "dist-gone-" + "b" * 32, [str(tmp_path / "vanished")])
    make_script_build(cache / "cabal" / "script-builds", "c" * 52, str(tmp_path / "no"))
    both = invoke(["list", "--cache-home", str(cache)])
    assert both.exit_code == 0, both.output
    assert "[hie-bios]" in both.output
    assert "[script-builds]" in both.output

    one = run_target(cache, "hie-bios", "list")
    assert one.exit_code == 0, one.output
    assert "[hie-bios]" not in one.output
