"""Cross-GHC compile check for haskell-language-server.

Builds all components against each supported GHC to catch cross-version
compat-shim breakage, mirroring the CI ``flags`` job (``+pedantic`` /
``-Werror``).

NB: Each version gets its own ``--builddir``. The shared cabal store caches
dependencies, so re-runs only recompile the local packages.

The Nix wrapper bakes the GHC compilers, cabal and the needed C libraries into
the environment. Run it from inside an HLS checkout.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from hls_utils._common import DEFAULT_VERSIONS, find_hls_checkout, load_ghc_map

PROJECT_NAME = ".hls-compat.project"
# Inherit the checkout's cabal.project but skip the developer's
# cabal.project.local (profiling, dumps). cabal resolves package paths relative
# to this file, so it must live in the checkout. The name is stable and the
# content fixed, so cabal does not reconfigure between runs.
PROJECT_CONTENT = "import: cabal.project\n\njobs: 6\n"
MAX_BACKJUMPS = 10000
TAIL_LINES = 25


def resolve_targets(cli_targets: list[str] | None) -> tuple[list[str], list[str]]:
    """Return the cabal targets and the extra build flags to use.

    An explicit selection (``--targets`` or ``COMPAT_TARGETS``) is built
    verbatim, so it may name test suites. With no selection we sweep ``all``
    local components and disable test suites.
    """
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
    targets: list[str],
    extra_flags: list[str],
) -> bool:
    """Build *targets* with one GHC, logging to compat-logs/<version>.log."""
    log_path = hls_dir / "compat-logs" / f"{version}.log"
    print(f"=== {version} -> {log_path} ===")
    numeric = subprocess.run(
        [ghc, "--numeric-version"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    cmd = [
        "cabal",
        "build",
        "-w",
        ghc,
        f"--project-file={PROJECT_NAME}",
        "--max-backjumps",
        str(MAX_BACKJUMPS),
        f"--builddir=dist-newstyle/compat-{version}",
        "--constraint",
        "haskell-language-server +pedantic",
        *extra_flags,
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
        prog="hls-check-ghc-compat",
        description=(
            "Build HLS components against every supported GHC under +pedantic "
            "to catch cross-version compat breakage."
        ),
    )
    parser.add_argument(
        "versions",
        nargs="*",
        help=f"GHC versions to check (default: {' '.join(DEFAULT_VERSIONS)}).",
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

    hls_dir = find_hls_checkout(Path.cwd())
    if hls_dir is None:
        print(
            "error: run this from inside an HLS checkout (no cabal.project found)",
            file=sys.stderr,
        )
        return 2

    ghc_map = load_ghc_map()
    versions: list[str] = args.versions or DEFAULT_VERSIONS
    targets, extra_flags = resolve_targets(args.targets)

    project_path = hls_dir / PROJECT_NAME
    project_path.write_text(PROJECT_CONTENT)
    (hls_dir / "compat-logs").mkdir(parents=True, exist_ok=True)

    rc = 0
    try:
        for version in versions:
            ghc = ghc_map.get(version)
            if not ghc:
                known = " ".join(sorted(ghc_map)) or "(none)"
                print(f"=== {version}: unknown version (have: {known}) ===")
                rc = 1
                continue
            if not run_for_version(version, ghc, hls_dir, targets, extra_flags):
                rc = 1
    finally:
        project_path.unlink(missing_ok=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
