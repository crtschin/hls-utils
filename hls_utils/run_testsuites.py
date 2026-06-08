"""Cross-GHC test-suite core runner for haskell-language-server.

Runs the project's test suites against a GHC via ``cabal test``, mirroring CI's
per-version test job. Companion to ``hls-check-ghc-compat``, which only
type-checks under ``+pedantic``. This actually exercises the suites.

This is the ``hls-run-testsuites-core`` launcher. It assumes the GHC it builds
and runs against is already first on PATH, which the ``hls-run-testsuites``
shell wrapper arranges per version before invoking us. The pinning has to happen
in a shell with no makeWrapper in the process ancestry: HLS testdata uses a
direct cradle, so a running test resolves its GHC libdir by invoking ``ghc`` off
PATH, and that PATH only reaches the spawned test under that condition. Building
under the wrong GHC makes every session-loading test fail on an ABI mismatch.

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

from hls_utils._common import find_hls_checkout, load_ghc_map

PROJECT_NAME = ".hls-test.project"
# Inherit the checkout's cabal.project but skip the developer's
# cabal.project.local (profiling, dumps). cabal resolves package paths relative
# to this file, so it must live in the checkout. The name is stable and the
# content fixed, so cabal does not reconfigure between runs.
PROJECT_CONTENT = "import: cabal.project\n\njobs: 6\n"
MAX_BACKJUMPS = 10000
TAIL_LINES = 25


def resolve_targets(cli_targets: list[str] | None) -> list[str]:
    """Return the cabal test targets to run.

    An explicit selection (``--targets`` or ``TEST_TARGETS``) is run verbatim.
    With no selection we run ``all`` test suites in the project.
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
    """Run *targets* under one GHC, logging to test-logs/<version>.log.

    Each target is its own ``cabal test`` invocation. Requesting several
    sublibrary-dependent suites in one call trips a cabal unit-registration bug
    (Cabal-9341, 'failed to find the installed unit ...-inplace-<sublib>'), so
    they must be run one at a time.
    """
    log_path = hls_dir / "test-logs" / f"{version}.log"
    print(f"=== {version} -> {log_path} ===")
    numeric = subprocess.run(
        [ghc, "--numeric-version"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    # Shared by every target. Only the trailing target name differs per run.
    base_cmd = [
        "cabal",
        "test",
        "-w",
        ghc,
        f"--project-file={PROJECT_NAME}",
        "--max-backjumps",
        str(MAX_BACKJUMPS),
        f"--builddir=dist-newstyle/test-{version}",
        # Stream each suite's output into the log so a failure tail is useful.
        # The default ('failures') hides output for suites cabal deems passing.
        "--test-show-details=streaming",
    ]
    ok = True
    with log_path.open("w") as log:
        log.write(f"GHC: {numeric}\n")
        log.flush()
        for target in targets:
            log.write(f"\n=== cabal test {target} ===\n")
            log.flush()
            result = subprocess.run(
                [*base_cmd, target],
                cwd=hls_dir,
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
    (hls_dir / "test-logs").mkdir(parents=True, exist_ok=True)

    try:
        ok = run_for_version(version, ghc, hls_dir, targets)
    finally:
        project_path.unlink(missing_ok=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
