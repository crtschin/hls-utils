"""Evict the per-version cabal build caches the cross-GHC sweep tools create.

``hls-check-ghc-compat`` and ``hls-run-testsuites`` each build under their own
per-version ``--builddir`` beneath ``dist-newstyle/`` (prefixes ``compat-`` and
``test-``), so re-runs only recompile local packages. Those builddirs are the
caches, and over a few GHCs they grow large. This removes them to force a clean
rebuild or reclaim disk.

It deliberately leaves two things alone: the *shared* cabal store (used by every
cabal invocation, not just these tools, and expensive to repopulate) and the
developer's own ``dist-newstyle/`` build state (these tools never write there).

The Nix wrapper bakes nothing this tool needs; it is pure filesystem work. Run
it from inside an HLS checkout, the same place the sweep tools run.
"""

import argparse
import shutil
import sys
from pathlib import Path

from hls_utils._common import (
    COMPAT_BUILDDIR_PREFIX,
    TEST_BUILDDIR_PREFIX,
    find_hls_checkout,
)


def find_caches(hls_dir: Path) -> list[Path]:
    """Per-version builddirs both sweep tools create under *hls_dir*, sorted.

    Globs the ``compat-*`` / ``test-*`` siblings under ``dist-newstyle/`` rather
    than enumerating known versions, so a builddir left by a custom version name
    (e.g. ``hls-check-ghc-compat ghc99``) is still found. Only directories match,
    so an unrelated file sharing the prefix is ignored.
    """
    caches: list[Path] = []
    for prefix in (COMPAT_BUILDDIR_PREFIX, TEST_BUILDDIR_PREFIX):
        parent = hls_dir / Path(prefix).parent
        stem = Path(prefix).name  # 'compat-' / 'test-'
        caches.extend(p for p in parent.glob(f"{stem}*") if p.is_dir())
    return sorted(caches)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hls-clear-caches",
        description=(
            "Remove the per-version cabal build caches that hls-check-ghc-compat "
            "and hls-run-testsuites create under dist-newstyle/. Leaves the shared "
            "cabal store and your own dist-newstyle build state untouched."
        ),
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="list what would be removed without deleting anything.",
    )
    args = parser.parse_args(argv)

    hls_dir = find_hls_checkout(Path.cwd())
    if hls_dir is None:
        print(
            "error: run this from inside an HLS checkout (no cabal.project found)",
            file=sys.stderr,
        )
        return 2

    caches = find_caches(hls_dir)
    if not caches:
        print("no compat/test build caches to clear.")
        return 0

    for cache in caches:
        rel = cache.relative_to(hls_dir)
        if args.dry_run:
            print(f"would remove {rel}")
        else:
            shutil.rmtree(cache)
            print(f"removed {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
