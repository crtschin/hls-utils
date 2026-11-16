"""Build HLS against every supported GHC under +pedantic, like the HLS CI flags job."""

import argparse
import os
import sys
from pathlib import Path

from hls_utils._common import (
    COMPAT_BUILDDIR_PREFIX,
    MAX_BACKJUMPS,
    run_logged,
    sweep,
)

PROJECT_NAME = ".hls-compat.project"


def resolve_targets(cli_targets: list[str] | None) -> tuple[list[str], list[str]]:
    """Cabal targets and extra flags. An explicit selection can name test suites."""
    if cli_targets:
        return cli_targets, []
    env = os.environ.get("COMPAT_TARGETS")
    if env:
        return env.split(), []
    return ["all"], ["--disable-tests"]


def run_for_version(
    version: str,
    ghc: str,
    hls_dir: Path,
    logs_dir: Path,
    targets: list[str],
    extra_flags: list[str],
) -> bool:
    cmd = [
        "cabal",
        "build",
        "-w",
        ghc,
        f"--project-file={PROJECT_NAME}",
        "--max-backjumps",
        str(MAX_BACKJUMPS),
        f"--builddir={COMPAT_BUILDDIR_PREFIX}{version}",
        "--constraint",
        "haskell-language-server +pedantic",
        *extra_flags,
        *targets,
    ]
    return run_logged(version, ghc, hls_dir, logs_dir, [cmd])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hls-check-ghc-compat",
        description=(
            "Build HLS components against every supported GHC under +pedantic "
            "to catch cross-version compat breakage."
        ),
    )
    parser.add_argument(
        "versions",
        nargs="*",
        help="GHC versions to check (default: every GHC this package provides).",
    )
    parser.add_argument(
        "-t",
        "--targets",
        nargs="+",
        help=(
            "cabal targets to build (default: all non-test components). "
            "Overrides COMPAT_TARGETS. Built verbatim, so may name test suites."
        ),
    )
    args = parser.parse_args(argv)
    targets, extra_flags = resolve_targets(args.targets)
    return sweep(
        args.versions,
        PROJECT_NAME,
        "compat",
        lambda version, ghc, hls_dir, logs_dir: run_for_version(
            version, ghc, hls_dir, logs_dir, targets, extra_flags
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
