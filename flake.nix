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

        # Cross-GHC compile check for HLS, as a Python CLI (hls_utils). Self-
        # contained, every supported GHC, cabal and the C libraries the
        # dependency tree needs are baked into the wrapper, so it runs against
        # any HLS checkout with no dev shell.
        #
        # Run it from inside a checkout.
        check-ghc-compat =
          let
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
          in
          pkgs.python3Packages.buildPythonApplication {
            pname = "hls-check-ghc-compat";
            version = "0.1.0";
            pyproject = true;
            src = pkgs.lib.fileset.toSource {
              root = ./.;
              fileset = pkgs.lib.fileset.unions [
                ./pyproject.toml
                ./hls_utils
              ];
            };
            build-system = [ pkgs.python3Packages.setuptools ];
            # Bake the runtime tools, C libraries and GHC map into the launcher
            # so the binary works in a bare HLS checkout.
            makeWrapperArgs = [
              "--prefix"
              "PATH"
              ":"
              (pkgs.lib.makeBinPath [
                pkgs.cabal-install
                pkgs.pkg-config
                pkgs.curl
              ])
              "--prefix"
              "LD_LIBRARY_PATH"
              ":"
              (pkgs.lib.makeLibraryPath [
                pkgs.gmp
                pkgs.zlib
                pkgs.ncurses
              ])
              "--set"
              "HLS_COMPAT_GHCS_FILE"
              "${ghcsFile}"
            ];
          };

        # The collection. Add new HLS utilities here; each one becomes a
        # `nix run`/`nix build`/`nix profile install` target automatically.
        utils = {
          inherit check-ghc-compat;
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
            pyright.enable = true;
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
          default = check-ghc-compat;
        };

        # `nix flake check` runs every hook over the tree and fails on diffs.
        checks.pre-commit-check = pre-commit-check;

        # For developing hls-utils itself (`nix develop`). Entering it installs
        # this repo's git pre-commit hook. Stays in sync with `utils`.
        devShells.default = pkgs.mkShell {
          inherit (pre-commit-check) shellHook;
          packages = builtins.attrValues utils ++ pre-commit-check.enabledPackages ++ [ pkgs.python3 ];
        };

        # Binaries-only shell for layering onto another checkout's .envrc
        # (`use flake`). No shellHook.
        devShells.tools = pkgs.mkShell {
          packages = builtins.attrValues utils;
        };

        apps = {
          check-ghc-compat = {
            type = "app";
            program = "${check-ghc-compat}/bin/hls-check-ghc-compat";
          };
          default = {
            type = "app";
            program = "${check-ghc-compat}/bin/hls-check-ghc-compat";
          };
        };
      }
    );
}
