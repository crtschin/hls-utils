"""Helpers shared by the tools that sweep an HLS checkout across GHC versions."""

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

# Nix substitutes a JSON list of [version, ghc] pairs, in sweep order.
GHCS_FILE = "@ghcs@"

# We write our own project file to skip the cabal.project.local of the developer.
# cabal resolves package paths relative to this file, so it must live in the
# checkout.
PROJECT_CONTENT = "import: cabal.project\n\njobs: 6\n"
MAX_BACKJUMPS = 10000
TAIL_LINES = 25

# hls-clear-sweep-caches deletes every directory that matches these prefixes, so
# keep them clear of the names cabal itself uses under dist-newstyle/. The two
# tools configure cabal differently, and a shared builddir reconfigures on every
# switch between them.
COMPAT_BUILDDIR_PREFIX = "dist-newstyle/compat-"
TEST_BUILDDIR_PREFIX = "dist-newstyle/test-"


def sweep_log_dir(name: str) -> Path:
    directory = Path(tempfile.gettempdir()) / "hls-utils-logs" / name
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def find_hls_checkout(start: Path) -> Path | None:
    for directory in [start, *start.parents]:
        if (directory / "cabal.project").is_file():
            return directory
    return None


def load_ghc_map(path: str = GHCS_FILE) -> dict[str, str]:
    try:
        raw = Path(path).read_text()
    except FileNotFoundError:
        return {}
    return {str(version): str(ghc) for version, ghc in json.loads(raw)}


def run_logged(
    version: str,
    ghc: str,
    hls_dir: Path,
    logs_dir: Path,
    commands: list[list[str]],
) -> bool:
    log_path = logs_dir / f"{version}.log"
    print(f"=== {version} -> {log_path} ===")
    numeric = subprocess.run(
        [ghc, "--numeric-version"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    # HLS testdata uses direct cradles, which take the GHC libdir from the ghc on
    # PATH. Any other GHC fails every session load on an ABI mismatch.
    path = os.pathsep.join([str(Path(ghc).parent), os.environ.get("PATH", "")])
    env = {**os.environ, "PATH": path}
    ok = True
    with log_path.open("w") as log:
        log.write(f"GHC: {numeric}\n")
        for cmd in commands:
            log.write(f"\n$ {' '.join(cmd)}\n")
            log.flush()
            result = subprocess.run(
                cmd,
                cwd=hls_dir,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
            ok = ok and result.returncode == 0
    if ok:
        print("  OK")
        return True
    print(f"  FAIL (tail {log_path}):")
    for line in log_path.read_text().splitlines()[-TAIL_LINES:]:
        print(f"    {line}")
    return False


def sweep(
    versions: list[str],
    project_name: str,
    logs_name: str,
    run_one: Callable[[str, str, Path, Path], bool],
) -> int:
    """Call run_one for each version, defaulting to every GHC that Nix provides."""
    hls_dir = find_hls_checkout(Path.cwd())
    if hls_dir is None:
        print(
            "error: run this from inside an HLS checkout (no cabal.project found)",
            file=sys.stderr,
        )
        return 2

    ghc_map = load_ghc_map()
    project_path = hls_dir / project_name
    project_path.write_text(PROJECT_CONTENT)
    logs_dir = sweep_log_dir(logs_name)

    rc = 0
    try:
        for version in versions or list(ghc_map):
            ghc = ghc_map.get(version)
            if not ghc:
                known = " ".join(ghc_map) or "(none)"
                print(f"=== {version}: unknown version (have: {known}) ===")
                rc = 1
                continue
            if not run_one(version, ghc, hls_dir, logs_dir):
                rc = 1
    finally:
        project_path.unlink(missing_ok=True)
    return rc
