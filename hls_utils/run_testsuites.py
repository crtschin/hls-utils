"""Cross-GHC test-suite runner for haskell-language-server.

Runs the project's test suites against each supported GHC via ``cabal test``,
mirroring CI's per-version test job. Companion to ``hls-check-ghc-compat``,
which only type-checks under ``+pedantic``; this actually exercises the suites.

NB: Each version gets its own ``--builddir``, separate from the compat tool's,
since the test build is configured differently (tests enabled, no -Werror) and
sharing a builddir would force cabal to reconfigure on every alternating run.
The shared cabal store still caches dependencies, so re-runs only recompile the
local packages.

The Nix wrapper bakes the GHC compilers, cabal and the needed C libraries into
the environment. Run it from inside an HLS checkout.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from hls_utils._common import DEFAULT_VERSIONS, find_hls_checkout, load_ghc_map

PROJECT_NAME = ".hls-test.project"
# Inherit the checkout's cabal.project but skip the developer's
# cabal.project.local (profiling, dumps); cabal resolves package paths relative
# to this file, so it must live in the checkout. The name is stable and the
# content fixed, so cabal does not reconfigure between runs.
PROJECT_CONTENT = "import: cabal.project\n\njobs: 6\n"
MAX_BACKJUMPS = 10000
TAIL_LINES = 25


def resolve_targets(cli_targets: list[str] | None) -> list[str]:
    """Return the cabal test targets to run.

    An explicit selection (``--targets`` or ``TEST_TARGETS``) is run verbatim;
    with no selection we run ``all`` test suites in the project.
    """
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
    targets: list[str],
) -> bool:
    """Run *targets* under one GHC, logging to test-logs/<version>.log."""
    log_path = hls_dir / "test-logs" / f"{version}.log"
    print(f"=== {version} -> {log_path} ===")
    numeric = subprocess.run(
        [ghc, "--numeric-version"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    cmd = [
        "cabal",
        "test",
        "-w",
        ghc,
        f"--project-file={PROJECT_NAME}",
        "--max-backjumps",
        str(MAX_BACKJUMPS),
        f"--builddir=dist-newstyle/test-{version}",
        # Stream each suite's output into the log so a failure tail is useful;
        # the default ('failures') hides output for suites cabal deems passing.
        "--test-show-details=streaming",
        *targets,
    ]
    with log_path.open("w") as log:
        log.write(f"GHC: {numeric}\n")
        log.flush()
        result = subprocess.run(
            cmd,
            cwd=hls_dir,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode == 0:
        print("  OK")
        return True
    print(f"  FAIL (tail {log_path}):")
    for line in log_path.read_text().splitlines()[-TAIL_LINES:]:
        print(f"    {line}")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hls-run-testsuites",
        description=("Run HLS test suites against every supported GHC via cabal test."),
    )
    parser.add_argument(
        "versions",
        nargs="*",
        help=f"GHC versions to test (default: {' '.join(DEFAULT_VERSIONS)}).",
    )
    parser.add_argument(
        "-t",
        "--targets",
        nargs="+",
        help=(
            "cabal test targets to run (default: all test suites). "
            "Overrides TEST_TARGETS; run verbatim."
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
    versions: list[str] = args.versions or DEFAULT_VERSIONS
    targets = resolve_targets(args.targets)

    project_path = hls_dir / PROJECT_NAME
    project_path.write_text(PROJECT_CONTENT)
    (hls_dir / "test-logs").mkdir(parents=True, exist_ok=True)

    rc = 0
    try:
        for version in versions:
            ghc = ghc_map.get(version)
            if not ghc:
                known = " ".join(sorted(ghc_map)) or "(none)"
                print(f"=== {version}: unknown version (have: {known}) ===")
                rc = 1
                continue
            if not run_for_version(version, ghc, hls_dir, targets):
                rc = 1
    finally:
        project_path.unlink(missing_ok=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
