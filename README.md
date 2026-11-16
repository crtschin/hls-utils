# hls-utils

Nix-packaged tools for working on
[Haskell Language Server](https://github.com/haskell/haskell-language-server)
(HLS), and a Nix build of HLS from upstream source against the GHC your project
already uses.

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

## Building HLS from source

Add the input.

```nix
inputs.hls-utils = {
  url = "github:crtschin/hls-utils";
  inputs.nixpkgs.follows = "nixpkgs";
};
```

Put the server in your dev shell.

```nix
devShells.default = pkgs.mkShell {
  buildInputs = [
    pkgs.haskell.packages.ghc910.ghc
    (hls-utils.lib.hlsFor pkgs.haskell.packages.ghc910)
  ];
};
```

### Sources

- `lib.hlsFor` builds the `hls-release` input, currently the 2.15.0.0 tag.
- `lib.hlsMasterFor` builds the `hls-master` input.

Retarget either one from your own flake.

```nix
inputs.hls-utils.inputs.hls-release.url = "github:haskell/haskell-language-server/<rev>";
```

For any other tree, including a local checkout, call `mkHls` directly.

```nix
hls-utils.lib.mkHls {
  haskellPackages = pkgs.haskell.packages.ghc910;
  src = /home/you/src/haskell-language-server;
  pins = hls-utils.lib.pinsFor.master;
  overrides = _hself: hsuper: { some-package = hsuper.some-package_1_2_3; };
}
```

`overrides` is applied last, for easily overriding anything.

### Overlay

Replaces `haskell-language-server` in the ghc96 through ghc914 sets.

```nix
pkgs = import nixpkgs {
  inherit system;
  overlays = [ hls-utils.overlays.default ]; # or hls-utils.overlays.master
};
```

### Outputs

- `lib.hlsFor`, `lib.hlsMasterFor`, `lib.mkHls`, `lib.pinsFor`, `lib.sources`
- `overlays.default`, `overlays.master`
- `packages.hls`, `packages.hls-<ghc>`, `packages.hls-master-<ghc>`
- `apps.hls`
- `checks.hls`, which instantiates every HLS package without building it

-------------------------------------------------------------------------------

Disclaimer: co-produced with a coding agent.
