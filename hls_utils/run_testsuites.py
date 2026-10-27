"""Run the HLS test suites against one GHC with ``cabal test``.

This is hls-run-testsuites-core. It expects that GHC first on PATH, which the
hls-run-testsuites wrapper in flake.nix arranges.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from hls_utils._common import (
    TEST_BUILDDIR_PREFIX,
    find_hls_checkout,
    load_ghc_map,
    sweep_log_dir,
)

PROJECT_NAME = ".hls-test.project"
# See the comment on check_ghc_compat.PROJECT_CONTENT.
PROJECT_CONTENT = "import: cabal.project\n\njobs: 6\n"
MAX_BACKJUMPS = 10000
TAIL_LINES = 25


def resolve_targets(cli_targets: list[str] | None) -> list[str]:
    if cli_targets:
        return cli_targets
    env = os.environ.get("TEST_TARGETS")
    if env:
        return env.split()
    return ["all"]


def run_for_version(
    version: str,
    ghc: str,
    hls_dir: Path,
    logs_dir: Path,
    targets: list[str],
) -> bool:
    log_path = logs_dir / f"{version}.log"
    print(f"=== {version} -> {log_path} ===")
    numeric = subprocess.run(
        [ghc, "--numeric-version"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    base_cmd = [
        "cabal",
        "test",
        "-w",
        ghc,
        f"--project-file={PROJECT_NAME}",
        "--max-backjumps",
        str(MAX_BACKJUMPS),
        f"--builddir={TEST_BUILDDIR_PREFIX}{version}",
        # Stream every suite's output so the failure tail is useful. The default,
        # `failures`, hides the output of suites that cabal considers passing.
        "--test-show-details=streaming",
    ]
    # The C libraries that the wrapper puts on LD_LIBRARY_PATH leak into the
    # `ghc` that a cradle spawns and break the session load. Every test that
    # needs a cradle then fails with `Rule Failed: GetParsedModuleWithComments`.
    # A C-FFI dependency needs LIBRARY_PATH and C_INCLUDE_PATH to build, so
    # those two stay.
    env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
    ok = True
    with log_path.open("w") as log:
        log.write(f"GHC: {numeric}\n")
        log.flush()
        # Several sublibrary-dependent suites in one call trip Cabal-9341, "failed
        # to find the installed unit ...-inplace-<sublib>".
        for target in targets:
            log.write(f"\n=== cabal test {target} ===\n")
            log.flush()
            result = subprocess.run(
                [*base_cmd, target],
                cwd=hls_dir,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
            if result.returncode != 0:
                ok = False
    if ok:
        print("  OK")
        return True
    print(f"  FAIL (tail {log_path}):")
    for line in log_path.read_text().splitlines()[-TAIL_LINES:]:
        print(f"    {line}")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hls-run-testsuites-core",
        description=(
            "Run HLS test suites against the GHC first on PATH via cabal test. "
            "Normally invoked per version by the hls-run-testsuites wrapper."
        ),
    )
    parser.add_argument(
        "version",
        help=(
            "GHC version to test. Must already be first on PATH. The "
            "hls-run-testsuites wrapper arranges this per version."
        ),
    )
    parser.add_argument(
        "-t",
        "--targets",
        nargs="+",
        help=(
            "cabal test targets to run (default: all test suites). "
            "Overrides TEST_TARGETS. Run verbatim."
        ),
    )
    args = parser.parse_args(argv)

    hls_dir = find_hls_checkout(Path.cwd())
    if hls_dir is None:
        print(
            "error: run this from inside an HLS checkout (no cabal.project found)",
            file=sys.stderr,
        )
        return 2

    ghc_map = load_ghc_map()
    version = args.version
    ghc = ghc_map.get(version)
    if not ghc:
        known = " ".join(sorted(ghc_map)) or "(none)"
        print(f"=== {version}: unknown version (have: {known}) ===")
        return 1
    targets = resolve_targets(args.targets)

    project_path = hls_dir / PROJECT_NAME
    project_path.write_text(PROJECT_CONTENT)
    logs_dir = sweep_log_dir("test")

    try:
        ok = run_for_version(version, ghc, hls_dir, logs_dir, targets)
    finally:
        project_path.unlink(missing_ok=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
