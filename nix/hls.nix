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
  offAlways = [
    "test-exe"
    "ghcide-bench"
    "plugin-tutorial"
  ];

  # These plugins need a 9.12 ghc-lib-parser, and none builds against the
  # base 4.22 that GHC 9.14 ships.
  offOnGhc914 = [
    "fourmolu"
    "ormolu"
    "hlint"
    "stylishHaskell"
  ];

  mkFlags = lib.concatMapStringsSep " " (f: "--flag=-${f}");

  # nixpkgs hasn't raised their bounds for base 4.22, containers 0.8,
  # template-haskell 2.24 or tagged 0.8.10. We only jailbreak on 9.14, as an
  # override loses the binary cache hit on every other GHC.
  jailbreakOnGhc914 = [
    "binary-instances"
    "constraints-extras"
    "dec"
    "dependent-map"
    "ghc-trace-events"
    "hie-compat"
    "lucid"
    "lukko"
    "singleton-bool"
    "string-interpolate"
    # Only lsp-types' suite uses it, and we can't skip that suite. lsp_2_8_0_0
    # refers to lsp-types_2_4_0_0 directly, so overriding lsp-types gives two
    # builds of it and Cabal rejects the mix.
    "tasty-hspec"
  ];

  # Their suites pull in rebase or tasty-hspec, which exclude base 4.22. The
  # suites of enummapset and generic-lens don't compile on 9.14 at all.
  skipTestsOnGhc914 = [
    "deferred-folds"
    "enummapset"
    "focus"
    "generic-lens"
    "primitive-extras"
    "stm-containers"
    "stm-hamt"
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
      ghc914 = lib.versionAtLeast ghcVersion "9.14";

      cabal2nixFlags = mkFlags (offAlways ++ lib.optionals ghc914 offOnGhc914);

      ghc914Fixes =
        hself: hsuper:
        lib.optionalAttrs ghc914 (
          {
            # nixpkgs pins Cabal-syntax 3.14 for fourmolu and ormolu, and that
            # version excludes the containers and time of 9.14. With those
            # plugins off, the compiler's own works.
            Cabal-syntax = null;
            Cabal = null;
            # Jailbreaking 0.7 fails, as its edges clashes with the one that
            # Data.Tree gained in containers 0.8.
            algebraic-graphs = hself.algebraic-graphs_0_8;
          }
          // lib.genAttrs jailbreakOnGhc914 (name: compose.doJailbreak hsuper.${name})
          // lib.genAttrs skipTestsOnGhc914 (name: compose.dontCheck hsuper.${name})
        );

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
            ghc914Fixes
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
