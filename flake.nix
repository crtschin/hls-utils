{
  description = "Nix-packaged utilities for working on Haskell Language Server, and HLS built from source";

  inputs = {
    # Matches the GHCs of the HLS dev shell, so both share the cabal store.
    nixpkgs.follows = "hls-master/nixpkgs";
    flake-utils.url = "github:numtide/flake-utils";
    git-hooks = {
      url = "github:cachix/git-hooks.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    hls-master.url = "github:haskell/haskell-language-server";
    hls-release = {
      url = "github:haskell/haskell-language-server/2.15.0.0";
      flake = false;
    };
    hie-bios = {
      url = "github:haskell/hie-bios/release-0.21.0";
      flake = false;
    };
  };

  outputs =
    {
      nixpkgs,
      flake-utils,
      git-hooks,
      hls-master,
      hls-release,
      hie-bios,
      ...
    }:
    let
      inherit (nixpkgs) lib;

      # Upstream HLS's tested-with list. Keep in sync with DEFAULT_VERSIONS in
      # hls_utils/_common.py.
      ghcs = [
        "ghc96"
        "ghc98"
        "ghc910"
        "ghc912"
        "ghc914"
      ];

      hls = import ./nix/hls.nix {
        inherit
          lib
          ghcs
          hls-master
          hls-release
          hie-bios
          ;
      };
    in
    {
      lib = {
        inherit (hls)
          mkHls
          hlsFor
          hlsMasterFor
          pinsFor
          ;
        sources = {
          master = hls-master;
          release = hls-release;
        };
      };

      overlays = {
        default = hls.mkOverlay hls.hlsFor;
        master = hls.mkOverlay hls.hlsMasterFor;
      };
    }
    // flake-utils.lib.eachSystem [ "x86_64-linux" "aarch64-linux" ] (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        ghcsFile = pkgs.writeText "hls-utils-ghcs.json" (
          builtins.toJSON (
            map (ghc: [
              ghc
              "${pkgs.haskell.packages.${ghc}.ghc}/bin/ghc"
            ]) ghcs
          )
        );
        cLibs = [
          pkgs.gmp
          pkgs.zlib
          pkgs.ncurses
        ];

        python = pkgs.python3.withPackages (ps: [ ps.pytest ]);

        src = pkgs.lib.fileset.toSource {
          root = ./.;
          fileset = pkgs.lib.fileset.unions [
            ./pyproject.toml
            ./hls_utils
            ./tests
          ];
        };

        hls-utils = pkgs.python3Packages.buildPythonApplication {
          pname = "hls-utils";
          version = "0.1.0";
          pyproject = true;
          inherit src;
          build-system = [ pkgs.python3Packages.setuptools ];
          postPatch = ''
            substituteInPlace hls_utils/_common.py --replace-fail '"@ghcs@"' '"${ghcsFile}"'
          '';
          # A C-FFI dependency that is not yet in the store needs LIBRARY_PATH to
          # link (-lz) and C_INCLUDE_PATH to compile its cbits (zlib.h).
          makeWrapperArgs = [
            "--prefix"
            "PATH"
            ":"
            (pkgs.lib.makeBinPath [
              pkgs.cabal-install
              pkgs.pkg-config
              pkgs.curl
              # hls-find-ghc-backports queries a GHC clone via git.
              pkgs.git
              # hs-gc-caches sizes cache entries with du.
              pkgs.coreutils
            ])
            "--prefix"
            "LIBRARY_PATH"
            ":"
            (pkgs.lib.makeLibraryPath cLibs)
            "--prefix"
            "C_INCLUDE_PATH"
            ":"
            (pkgs.lib.makeSearchPathOutput "dev" "include" cLibs)
          ];
        };

        # Runs pytest without pulling in the multi-GiB GHC closure of the launcher.
        hls-utils-tests =
          pkgs.runCommandLocal "hls-utils-tests"
            {
              nativeBuildInputs = [ python ];
            }
            ''
              cp -r ${src}/. .
              chmod -R +w .
              pytest
              touch "$out"
            '';

        hlsMatrix =
          prefix: build:
          lib.listToAttrs (
            map (ghc: lib.nameValuePair "${prefix}${ghc}" (build pkgs.haskell.packages.${ghc})) ghcs
          );
        hlsPackages = {
          hls = hls.hlsFor pkgs.haskellPackages;
        }
        // hlsMatrix "hls-" hls.hlsFor
        // hlsMatrix "hls-master-" hls.hlsMasterFor;

        pre-commit-check = git-hooks.lib.${system}.run {
          src = ./.;
          hooks = {
            nixfmt.enable = true;
            ruff.enable = true;
            ruff-format.enable = true;
            pyright = {
              enable = true;
              entry = "${pkgs.lib.getExe pkgs.pyright} --pythonpath ${pkgs.lib.getExe python}";
            };
          };
        };
      in
      {
        packages = {
          inherit hls-utils;
          default = hls-utils;
        }
        // hlsPackages;

        checks = {
          pre-commit-check = pre-commit-check;
          pytest = hls-utils-tests;
          # Instantiates every HLS build without building it, which catches a
          # stale pin in seconds.
          hls = pkgs.writeText "hls-instantiate" (
            lib.concatMapStringsSep "\n" (drv: builtins.unsafeDiscardOutputDependency drv.drvPath) (
              lib.attrValues hlsPackages
            )
          );
        };

        devShells.default = pkgs.mkShell {
          inherit (pre-commit-check) shellHook;
          packages = [
            hls-utils
          ]
          ++ pre-commit-check.enabledPackages
          ++ [
            python
          ];
        };

        # For the .envrc of another checkout. A shellHook here would install our
        # git hooks into that checkout.
        devShells.tools = pkgs.mkShell {
          packages = [ hls-utils ];
        };

        apps = {
          check-ghc-compat = {
            type = "app";
            program = "${hls-utils}/bin/hls-check-ghc-compat";
          };
          run-testsuites = {
            type = "app";
            program = "${hls-utils}/bin/hls-run-testsuites";
          };
          find-ghc-backports = {
            type = "app";
            program = "${hls-utils}/bin/hls-find-ghc-backports";
          };
          clear-sweep-caches = {
            type = "app";
            program = "${hls-utils}/bin/hls-clear-sweep-caches";
          };
          gc-caches = {
            type = "app";
            program = "${hls-utils}/bin/hs-gc-caches";
          };
          hls = {
            type = "app";
            program = "${hlsPackages.hls}/bin/haskell-language-server";
          };
          default = {
            type = "app";
            program = "${hls-utils}/bin/hls-check-ghc-compat";
          };
        };
      }
    );
}
