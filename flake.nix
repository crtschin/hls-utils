{
  description = "Nix-packaged utilities for working on Haskell Language Server";

  inputs = {
    # The revision that the haskell-language-server flake pins, so the GHC patch
    # versions match and both share the cabal store.
    nixpkgs.url = "github:NixOS/nixpkgs/ed142ab1b3a092c4d149245d0c4126a5d7ea00b0";
    flake-utils.url = "github:numtide/flake-utils";
    git-hooks = {
      url = "github:cachix/git-hooks.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      nixpkgs,
      flake-utils,
      git-hooks,
      ...
    }:
    flake-utils.lib.eachSystem [ "x86_64-linux" "x86_64-darwin" "aarch64-linux" "aarch64-darwin" ] (
      system:
      let
        pkgs = import nixpkgs {
          inherit system;
          config = {
            allowBroken = true;
          };
        };

        # Keep in sync with DEFAULT_VERSIONS in hls_utils/_common.py.
        ghcs = {
          ghc96 = pkgs.haskell.packages.ghc96.ghc;
          ghc98 = pkgs.haskell.packages.ghc98.ghc;
          ghc910 = pkgs.haskell.packages.ghc910.ghc;
          ghc912 = pkgs.haskell.packages.ghc912.ghc;
          ghc914 = pkgs.haskell.packages.ghc914.ghc;
        };
        # A file, because makeWrapper mangles the JSON when we pass it inline.
        ghcsFile = pkgs.writeText "hls-compat-ghcs.json" (
          builtins.toJSON (pkgs.lib.mapAttrs (_: ghc: "${ghc}/bin/ghc") ghcs)
        );
        cLibs = [
          pkgs.gmp
          pkgs.zlib
          pkgs.ncurses
        ];

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
          dependencies = [ pkgs.python3Packages.click ];
          nativeCheckInputs = [ pkgs.python3Packages.pytestCheckHook ];
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
            "LD_LIBRARY_PATH"
            ":"
            (pkgs.lib.makeLibraryPath cLibs)
            "--prefix"
            "LIBRARY_PATH"
            ":"
            (pkgs.lib.makeLibraryPath cLibs)
            "--prefix"
            "C_INCLUDE_PATH"
            ":"
            (pkgs.lib.makeSearchPathOutput "dev" "include" cLibs)
            "--set"
            "HLS_GHCS_FILE"
            "${ghcsFile}"
          ];
        };

        # Runs pytest without pulling in the multi-GiB GHC closure of the launcher.
        hls-utils-tests =
          pkgs.runCommandLocal "hls-utils-tests"
            {
              nativeBuildInputs = [
                (pkgs.python3.withPackages (ps: [
                  ps.click
                  ps.pytest
                ]))
              ];
            }
            ''
              cp -r ${src}/. .
              chmod -R +w .
              pytest
              touch "$out"
            '';

        # A plain shell script, because PATH reaches the spawned test only when
        # no makeWrapper sits in the process ancestry. HLS testdata uses a direct
        # cradle, so a test finds its GHC libdir through the ghc on PATH. The
        # wrong GHC fails every session-loading test on an ABI mismatch.
        hls-run-testsuites = pkgs.writeShellScriptBin "hls-run-testsuites" ''
            set -euo pipefail
            versions=""
            targets=""
            while [ "$#" -gt 0 ]; do
              case "$1" in
                -t | --targets)
                  shift
                  targets="$*"
                  break
                  ;;
                *)
                  versions="$versions $1"
                  shift
                  ;;
              esac
            done
            [ -z "$versions" ] && versions="${pkgs.lib.concatStringsSep " " (builtins.attrNames ghcs)}"
            rc=0
            for v in $versions; do
              bindir=""
              case "$v" in
          ${pkgs.lib.concatStringsSep "\n" (
            pkgs.lib.mapAttrsToList (v: ghc: "          ${v}) bindir=${ghc}/bin ;;") ghcs
          )}
                *) echo "=== $v: unknown version ==="; rc=1; continue ;;
              esac
              if [ -n "$targets" ]; then
                PATH="$bindir:$PATH" ${hls-utils}/bin/hls-run-testsuites-core "$v" -t $targets || rc=1
              else
                PATH="$bindir:$PATH" ${hls-utils}/bin/hls-run-testsuites-core "$v" || rc=1
              fi
            done
            exit "$rc"
        '';

        utils = {
          inherit hls-utils hls-run-testsuites;
        };

        pre-commit-check = git-hooks.lib.${system}.run {
          src = ./.;
          hooks = {
            nixfmt-rfc-style = {
              enable = true;
              # nixfmt-rfc-style is a deprecated alias in this nixpkgs.
              package = pkgs.nixfmt;
            };
            ruff.enable = true;
            ruff-format.enable = true;
            pyright = {
              enable = true;
              # pytest is not on the path of the type checker, so tests/ stays out.
              files = "^hls_utils/";
            };
            shfmt = {
              enable = true;
              args = [
                "-i"
                "2"
              ];
            };
            shellcheck.enable = true;
          };
        };
      in
      {
        packages = utils // {
          default = hls-utils;
        };

        checks = {
          pre-commit-check = pre-commit-check;
          pytest = hls-utils-tests;
        };

        devShells.default = pkgs.mkShell {
          inherit (pre-commit-check) shellHook;
          packages =
            builtins.attrValues utils
            ++ pre-commit-check.enabledPackages
            ++ [
              (pkgs.python3.withPackages (ps: [
                ps.click
                ps.pytest
              ]))
            ];
        };

        # For the .envrc of another checkout. A shellHook here would install our
        # git hooks into that checkout.
        devShells.tools = pkgs.mkShell {
          packages = builtins.attrValues utils;
        };

        apps = {
          check-ghc-compat = {
            type = "app";
            program = "${hls-utils}/bin/hls-check-ghc-compat";
          };
          run-testsuites = {
            type = "app";
            program = "${hls-run-testsuites}/bin/hls-run-testsuites";
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
          default = {
            type = "app";
            program = "${hls-utils}/bin/hls-check-ghc-compat";
          };
        };
      }
    );
}
