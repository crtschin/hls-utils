"""Run the HLS test suites against each supported GHC with ``cabal test``."""

import argparse
import os
import sys
from pathlib import Path

from hls_utils._common import (
    MAX_BACKJUMPS,
    TEST_BUILDDIR_PREFIX,
    run_logged,
    sweep,
)

PROJECT_NAME = ".hls-test.project"


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
    # Several sublibrary-dependent suites in one call trip Cabal-9341, "failed
    # to find the installed unit ...-inplace-<sublib>".
    commands = [[*base_cmd, target] for target in targets]
    return run_logged(version, ghc, hls_dir, logs_dir, commands)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hls-run-testsuites",
        description="Run the HLS test suites against every supported GHC.",
    )
    parser.add_argument(
        "versions",
        nargs="*",
        help="GHC versions to test (default: every GHC this package provides).",
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
    targets = resolve_targets(args.targets)
    return sweep(
        args.versions,
        PROJECT_NAME,
        "test",
        lambda version, ghc, hls_dir, logs_dir: run_for_version(
            version, ghc, hls_dir, logs_dir, targets
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
