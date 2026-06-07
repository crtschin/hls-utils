# hls-utils

Nix-packaged utilities for working on [Haskell Language Server](https://github.com/haskell/haskell-language-server).

## Usage

The intended usage is via something like nix-direnv to bring these utilities
into scope. Add the `tools` shell to your HLS checkout's `.envrc`:

```sh
# ~/src/haskell-language-server/.envrc
use flake github:crtschin/hls-utils#tools
```

```sh
direnv allow
# now the tools are on PATH inside the checkout
hls-check-ghc-compat    # cross-GHC compile check (+pedantic / -Werror)
hls-run-testsuites      # run the test suites against every supported GHC
hls-find-ghc-backports  # find which released GHC versions contain backports
```

Both default to sweeping every supported GHC; pass versions to narrow it
(e.g. `hls-run-testsuites ghc912`) or `-t/--targets` to pick cabal targets.

### `hls-find-ghc-backports`

Which released GHC versions contain a change (including backports)? Given a
commit SHA, an issue or a merge request, it reports the first release per series
that has the fix, tracing cherry-picks onto older branches.

You'll need to point this at a local git clone of ghc.

```sh
hls-find-ghc-backports --repo ~/src/ghc <sha>      # most reliable
hls-find-ghc-backports --repo ~/src/ghc '#26092'   # by issue (quote the #)
hls-find-ghc-backports --repo ~/src/ghc '!12345'   # by MR (heuristic; prefer the SHA)
export GHC_REPO=~/src/ghc                          # or set the repo via env
hls-find-ghc-backports --json <sha>                # machine-readable
```

It works fully offline against a local GHC clone (gitlab.haskell.org's REST API
is behind bot-protection). A treeless clone is enough — commit graph and tags,
no blobs — and you keep it current yourself:

```sh
git clone --filter=tree:0 --mirror https://gitlab.haskell.org/ghc/ghc.git ~/src/ghc
git -C ~/src/ghc fetch --prune --tags
```

Limits: a later revert is invisible to `--contains`; `!MR` lookup is heuristic
(marge-bot rebases drop the MR number from commit text); identical commit
subjects can collide.
