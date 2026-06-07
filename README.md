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
