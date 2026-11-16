{
  lib,
  ghcs,
  hls-master,
  hls-release,
  hie-bios,
}:
let
  # cabal.project's packages minus the server and shake-bench. The unconditional
  # ghcide-bench-lib sublibrary depends on hls-test-utils.
  localPackages = [
    "hls-graph"
    "hls-plugin-api"
    "ghcide"
    "hls-test-utils"
  ];

  # cabal2nix drops these components, so their dependencies never enter the
  # build.
  cabal2nixFlags = lib.concatMapStringsSep " " (f: "--flag=-${f}") [
    "test-exe"
    "ghcide-bench"
    "plugin-tutorial"
  ];

  # Pins take compose, as an overlay's arguments can't reach haskellLib.
  pinsFor =
    let
      pins = compose: hself: _hsuper: {
        lsp = hself.lsp_2_8_0_0;
        lsp-types = hself.lsp-types_2_4_0_0;
        # Its example suite needs a demo server binary we don't build.
        lsp-test = compose.dontCheck hself.lsp-test_0_18_0_0;
        # Both sources need a hie-bios newer than nixpkgs has.
        hie-bios = compose.dontCheck (hself.callCabal2nix "hie-bios" hie-bios { });
        hiedb = compose.dontCheck hself.hiedb_0_8_0_0;
      };
    in
    {
      release = pins;
      master = pins;
    };

  # Takes the importer's own package set, so the server and the project share
  # one GHC derivation. ghc-check rejects anything else at run time.
  mkHls =
    {
      haskellPackages,
      src,
      pins ? (
        _: _: _:
        { }
      ),
      overrides ? (_: _: { }),
    }:
    let
      # A haskell package set doesn't export haskellLib, so reach it through a
      # package's scope.
      inherit (haskellPackages.haskell-language-server.scope.pkgs.haskell.lib) compose;

      ghcVersion = haskellPackages.ghc.version;
      # No cache holds a build of a git checkout, so profiling and haddock only
      # cost time. Tests stay off, as hls-plugin-api's own suite fails.
      lean =
        drv:
        lib.pipe drv [
          compose.dontCheck
          compose.dontHaddock
          compose.disableLibraryProfiling
        ];

      # ghcide asks for unordered-containers 0.2.21 ahead of using lookupKey, which
      # it doesn't call yet. Pinning that version rebuilds most of Hackage.
      relaxUnorderedContainers = compose.overrideCabal (old: {
        postPatch = (old.postPatch or "") + ''
          sed -i 's/unordered-containers *>=0\.2\.21/unordered-containers/' ghcide.cabal
        '';
      });

      sources =
        hself: _hsuper:
        let
          local = lib.genAttrs localPackages (
            name: lean (hself.callCabal2nixWithOptions name "${src}/${name}" cabal2nixFlags { })
          );
        in
        local // { ghcide = relaxUnorderedContainers local.ghcide; };

      # Resolving in nixpkgs' own HLS scope keeps Cabal-syntax matched to the
      # fourmolu and ormolu it ships. Importer overrides land last, so they win.
      scope =
        (haskellPackages.haskell-language-server.overrideScope (
          lib.composeManyExtensions [
            (pins compose)
            sources
            overrides
          ]
        )).scope;
    in
    lib.pipe (scope.callCabal2nixWithOptions "haskell-language-server" src cabal2nixFlags { }) [
      lean
      # No justStaticExecutables, as the `dynamic` flag links the dyn RTS, which
      # decides how Template Haskell loads external libraries.
      #
      # That flag also links the exe against the plugin sublibraries, so Cabal
      # has to know, or their RPATH stays in the build directory.
      compose.enableSharedExecutables
      (compose.overrideCabal (old: {
        # The wrapper and several editor configs look for the versioned name.
        postInstall = (old.postInstall or "") + ''
          for v in ${ghcVersion} ${lib.versions.majorMinor ghcVersion}; do
            ln -s haskell-language-server $out/bin/haskell-language-server-$v
          done
        '';
      }))
    ];

  hlsFor =
    haskellPackages:
    mkHls {
      inherit haskellPackages;
      src = hls-release;
      pins = pinsFor.release;
    };
  hlsMasterFor =
    haskellPackages:
    mkHls {
      inherit haskellPackages;
      src = hls-master;
      pins = pinsFor.master;
    };

  # Keyed by name, since probing each set for an HLS attribute forces all of
  # them.
  mkOverlay = build: _final: prev: {
    haskell = prev.haskell // {
      packages =
        prev.haskell.packages
        // lib.genAttrs (lib.intersectLists ghcs (lib.attrNames prev.haskell.packages)) (
          ghc:
          prev.haskell.packages.${ghc}.extend (
            _hself: _hsuper: {
              haskell-language-server = build prev.haskell.packages.${ghc};
            }
          )
        );
    };
  };
in
{
  inherit
    mkHls
    hlsFor
    hlsMasterFor
    pinsFor
    mkOverlay
    ;
}
