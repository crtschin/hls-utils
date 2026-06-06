# Logic for the hls-check-ghc-compat binary (wrapped by flake.nix). The wrapper
# sets PATH, LD_LIBRARY_PATH and a GHC_BIN[<ver>] map of baked-in compilers
# before this fragment runs, so the script needs no dev shell of its own.
#
# Builds every local component except test suites against each supported GHC to
# catch cross-version compat-shim breakage. Uses +pedantic (-Werror) to mirror
# the CI `flags` job. Each version gets its own --builddir; the global cabal
# store caches dependencies, so re-runs only recompile the local packages.
#
# Usage (from inside an HLS checkout):
#   nix run ~/personal/hls-utils                  # all versions
#   nix run ~/personal/hls-utils -- ghc96 ghc912  # a subset
# Override the build targets with COMPAT_TARGETS="pkg:comp ..." (built as-is, so
# it may include test targets).
#
# This file is a fragment embedded into a bash `writeShellScriptBin` wrapper, so
# it carries no shebang; name the target shell so shellcheck analyses it as bash.
# shellcheck shell=bash
set -uo pipefail

# Locate the HLS checkout: nearest ancestor of $PWD with a cabal.project.
hls_dir="$PWD"
while [ "$hls_dir" != "/" ] && [ ! -f "$hls_dir/cabal.project" ]; do
  hls_dir="$(dirname "$hls_dir")"
done
if [ ! -f "$hls_dir/cabal.project" ]; then
  echo "error: run this from inside an HLS checkout (no cabal.project found)" >&2
  exit 2
fi

# Default to a full cross-GHC sweep of every local component except test suites
# (--disable-tests, added to the build below, is what drops tests from `all`).
# An explicit COMPAT_TARGETS is honoured verbatim, so it may name test targets.
if [ -n "${COMPAT_TARGETS:-}" ]; then
  read -ra targets <<<"$COMPAT_TARGETS"
  tests_flag=()
else
  targets=(all)
  tests_flag=(--disable-tests)
fi

versions=("$@")
if [ "${#versions[@]}" -eq 0 ]; then
  versions=(ghc96 ghc98 ghc910 ghc912 ghc914)
fi

# Hermetic project: inherit the checkout's cabal.project but skip the
# developer's cabal.project.local (profiling, dumps). cabal resolves package
# paths relative to the project file's directory, so it lives in the checkout;
# its '.local' sibling does not exist, so the dev local is ignored. The name is
# stable and the content fixed, so cabal does not reconfigure between runs.
proj_name=".hls-compat.project"
cat >"$hls_dir/$proj_name" <<'EOF'
import: cabal.project

jobs: 6
EOF
trap 'rm -f "$hls_dir/$proj_name"' EXIT

mkdir -p "$hls_dir/compat-logs"
rc=0
for v in "${versions[@]}"; do
  ghc="${GHC_BIN[$v]:-}"
  if [ -z "$ghc" ]; then
    echo "=== $v: unknown version (have: ${!GHC_BIN[*]}) ==="
    rc=1
    continue
  fi
  log="$hls_dir/compat-logs/$v.log"
  echo "=== $v -> $log ==="
  if (
    cd "$hls_dir" || exit 1
    echo "GHC: $("$ghc" --numeric-version)"
    cabal build -w "$ghc" --project-file="$proj_name" \
      --max-backjumps 10000 --builddir="dist-newstyle/compat-$v" \
      --constraint 'haskell-language-server +pedantic' \
      "${tests_flag[@]}" "${targets[@]}"
  ) >"$log" 2>&1; then
    echo "  OK"
  else
    echo "  FAIL (tail $log):"
    tail -n 25 "$log" | sed 's/^/    /'
    rc=1
  fi
done
exit $rc
