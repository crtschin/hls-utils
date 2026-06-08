"""Shared helpers for the hls-utils cross-GHC tools.

Both ``check_ghc_compat`` (compile check) and ``run_testsuites`` (test runner)
sweep the same set of GHCs supplied by the Nix wrapper, against an HLS checkout
located by walking up to a ``cabal.project``.
"""

import json
import os
from pathlib import Path

# Supported GHC series, matching the compilers the Nix wrapper bakes in and the
# versions HLS's CI exercises.
DEFAULT_VERSIONS = ["ghc96", "ghc98", "ghc910", "ghc912", "ghc914"]


def find_hls_checkout(start: Path) -> Path | None:
    """Return the nearest ancestor of *start* containing a cabal.project."""
    for directory in [start, *start.parents]:
        if (directory / "cabal.project").is_file():
            return directory
    return None


def load_ghc_map() -> dict[str, str]:
    """Parse the version -> ghc-binary map the Nix wrapper injects.

    HLS_GHCS_FILE points at a JSON file (what the Nix build sets). HLS_GHCS may
    hold the same JSON inline (handy for tests). A missing file or empty/absent
    env yields an empty map. Present-but-malformed JSON still raises.
    """
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
