"""Helpers shared by the tools that sweep an HLS checkout across GHC versions."""

import json
import os
import tempfile
from pathlib import Path

# Every version here needs a compiler in `ghcs` in flake.nix.
DEFAULT_VERSIONS = ["ghc96", "ghc98", "ghc910", "ghc912", "ghc914"]

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


def load_ghc_map() -> dict[str, str]:
    """Version-to-ghc map from HLS_GHCS_FILE, which Nix sets, or HLS_GHCS (tests)."""
    path = os.environ.get("HLS_GHCS_FILE")
    if path:
        try:
            raw = Path(path).read_text()
        except FileNotFoundError:
            return {}
    else:
        raw = os.environ.get("HLS_GHCS")
    if not raw:
        return {}
    data: object = json.loads(raw)
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}
