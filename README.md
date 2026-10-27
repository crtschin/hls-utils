# hls-utils

Nix-packaged tools for working on
[Haskell Language Server](https://github.com/haskell/haskell-language-server)
(HLS).

## Usage

Add the `tools` shell to the `.envrc` of your HLS checkout, and direnv puts the
tools on PATH:

```sh
# ~/src/haskell-language-server/.envrc
use flake github:crtschin/hls-utils#tools
```

```sh
direnv allow
hls-check-ghc-compat    # compile against every supported GHC under +pedantic
hls-run-testsuites      # run the test suites against every supported GHC
hls-find-ghc-backports  # find the GHC releases that ship a change
hls-clear-sweep-caches  # delete the build caches of the two sweep tools
hs-gc-caches            # reclaim Haskell caches whose owner is gone
```

Without direnv, run a tool with `nix run`, e.g.
`nix run github:crtschin/hls-utils#find-ghc-backports -- <sha>`.

## Sweeping GHC versions

`hls-check-ghc-compat` and `hls-run-testsuites` sweep every supported GHC by
default. Pass versions to narrow the sweep, e.g. `hls-run-testsuites ghc912`,
and `-t`/`--targets` to pick cabal targets.

Each tool keeps a cabal builddir per version under `dist-newstyle/`, so a
re-run recompiles only the local packages. Run `hls-clear-sweep-caches` from
the checkout to delete those builddirs, and add `-n`/`--dry-run` to preview. It
leaves the shared cabal store and your own `dist-newstyle/` state alone.

## Reclaiming disk from old caches

`hs-gc-caches` works on the whole machine. It finds the cabal store, ghcide,
hie-bios, script-builds and log caches whose compiler, project or script is
gone. `hs-gc-caches list` reports them, and `hs-gc-caches clean --delete`
removes them.

On Nix, run `nix-collect-garbage -d` first. Every GHC it removes turns its cabal
store into an orphan.
