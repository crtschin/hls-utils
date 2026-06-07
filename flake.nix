{
  description = "Nix-packaged utilities for working on Haskell Language Server";

  inputs = {
    # Pinned to the same revision haskell-language-server's flake uses, so the
    # GHC patch versions match HLS (and the cabal store / builddirs are shared).
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

        # version -> ghc. Shared by the Python launcher (which compiles under
        # each via `cabal -w`) and the hls-run-testsuites shell wrapper (which
        # pins each on PATH).
        ghcs = {
          ghc96 = pkgs.haskell.packages.ghc96.ghc;
          ghc98 = pkgs.haskell.packages.ghc98.ghc;
          ghc910 = pkgs.haskell.packages.ghc910.ghc;
          ghc912 = pkgs.haskell.packages.ghc912.ghc;
          ghc914 = pkgs.haskell.packages.ghc914.ghc;
        };
        # version -> ghc binary, handed to the CLI as a JSON file path
        # (passing the JSON inline trips makeWrapper's argument parsing).
        ghcsFile = pkgs.writeText "hls-compat-ghcs.json" (
          builtins.toJSON (pkgs.lib.mapAttrs (_: ghc: "${ghc}/bin/ghc") ghcs)
        );
        # C libraries the dependency tree links against (e.g. zlib's -lz),
        # exposed at runtime, link and compile time below.
        cLibs = [
          pkgs.gmp
          pkgs.zlib
          pkgs.ncurses
        ];

        # The hls_utils Python package: hls-check-ghc-compat and the
        # hls-run-testsuites-core launcher. Self-contained: every supported GHC,
        # cabal and the C libraries the dependency tree needs are baked into the
        # wrapper, so the tools run against any HLS checkout with no dev shell.
        # Run them from inside a checkout.
        hls-utils = pkgs.python3Packages.buildPythonApplication {
          pname = "hls-utils";
          version = "0.1.0";
          pyproject = true;
          src = pkgs.lib.fileset.toSource {
            root = ./.;
            fileset = pkgs.lib.fileset.unions [
              ./pyproject.toml
              ./hls_utils
              ./tests
            ];
          };
          build-system = [ pkgs.python3Packages.setuptools ];
          # Run the pytest suite in checkPhase, so `nix build` / `nix flake check`
          # fail if the tools' logic regresses. tests/ ships in src but not in the
          # wheel (the packages allowlist below excludes it).
          nativeCheckInputs = [ pkgs.python3Packages.pytestCheckHook ];
          # Bake the runtime tools, GHC map and C libraries into the launcher
          # so the binary works in a bare HLS checkout. The C libs go on three
          # paths: LD_LIBRARY_PATH for runtime, LIBRARY_PATH for the linker
          # (finds -lz) and C_INCLUDE_PATH for compiling cbits (finds zlib.h).
          # Without the latter two, building a not-yet-cached C-FFI dep fails.
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

        # User-facing test runner. A plain shell script (NOT a makeWrapper'd
        # binary) on purpose: each per-version `cabal test` must be launched
        # from a shell. The spawned test resolves its GHC libdir from PATH, and
        # that only reaches the test when no makeWrapper sits in the process
        # ancestry. So we loop here, pin each version's GHC first on PATH, and
        # call the core launcher (which runs cabal directly).
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

        # The collection. CLIs: hls-check-ghc-compat (Python) and
        # hls-run-testsuites (shell wrapper over the Python hls-run-testsuites-core).
        utils = {
          inherit hls-utils hls-run-testsuites;
        };

        # Formatting / linting hooks: installed into .git/hooks on `nix develop`
        # and enforced by `nix flake check`.
        pre-commit-check = git-hooks.lib.${system}.run {
          src = ./.;
          hooks = {
            nixfmt-rfc-style = {
              enable = true;
              # pkgs.nixfmt-rfc-style is a deprecated alias for pkgs.nixfmt in
              # this nixpkgs; use the canonical attr to silence the eval warning.
              package = pkgs.nixfmt;
            };
            ruff.enable = true;
            ruff-format.enable = true;
            pyright = {
              enable = true;
              # Scope to the package, matching pyproject's include = ["hls_utils"].
              # tests/ is deliberately not type-checked: it leans on pytest, which
              # isn't on the type-checker's path.
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

        # `nix flake check` runs every hook over the tree and fails on diffs.
        checks.pre-commit-check = pre-commit-check;

        # For developing hls-utils itself (`nix develop`). Entering it installs
        # this repo's git pre-commit hook. Stays in sync with `utils`.
        devShells.default = pkgs.mkShell {
          inherit (pre-commit-check) shellHook;
          packages =
            builtins.attrValues utils
            ++ pre-commit-check.enabledPackages
            ++ [
              pkgs.python3
              pkgs.python3Packages.pytest
            ];
        };

        # Binaries-only shell for layering onto another checkout's .envrc
        # (`use flake`). No shellHook.
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
          default = {
            type = "app";
            program = "${hls-utils}/bin/hls-check-ghc-compat";
          };
        };
      }
    );
}
