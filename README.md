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
# now hls-check-ghc-compat is on PATH inside the checkout
hls-check-ghc-compat
```
