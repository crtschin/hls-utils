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

        # Cross-GHC compile check for HLS. Self-contained: every supported GHC,
        # cabal and the C libraries the dependency tree needs are baked into the
        # wrapper, so it runs against any HLS checkout with no dev shell. Run it
        # from inside a checkout; it builds all non-test components (override
        # with COMPAT_TARGETS) against each GHC under +pedantic (-Werror).
        check-ghc-compat =
          let
            ghcs = {
              ghc96 = pkgs.haskell.packages.ghc96.ghc;
              ghc98 = pkgs.haskell.packages.ghc98.ghc;
              ghc910 = pkgs.haskell.packages.ghc910.ghc;
              ghc912 = pkgs.haskell.packages.ghc912.ghc;
              ghc914 = pkgs.haskell.packages.ghc914.ghc;
            };
            ghcBindings = pkgs.lib.concatStringsSep "\n  " (
              pkgs.lib.mapAttrsToList (k: v: "[${k}]=${v}/bin/ghc") ghcs
            );
          in
          pkgs.writeShellScriptBin "hls-check-ghc-compat" ''
            export PATH=${
              pkgs.lib.makeBinPath [
                pkgs.cabal-install
                pkgs.pkg-config
                pkgs.curl
              ]
            }:$PATH
            export LD_LIBRARY_PATH=${
              pkgs.lib.makeLibraryPath [
                pkgs.gmp
                pkgs.zlib
                pkgs.ncurses
              ]
            }''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
            declare -A GHC_BIN=(
              ${ghcBindings}
            )
            ${builtins.readFile ./check-ghc-compat.sh}
          '';

        # The collection. Add new HLS utilities here; each one becomes a
        # `nix run`/`nix build`/`nix profile install` target automatically.
        utils = {
          inherit check-ghc-compat;
        };

        # Formatting / linting hooks: installed into .git/hooks on `nix develop`
        # and enforced by `nix flake check`. nixfmt for Nix; shfmt (2-space
        # indent, matching the existing scripts) + shellcheck for shell.
        pre-commit-check = git-hooks.lib.${system}.run {
          src = ./.;
          hooks = {
            nixfmt-rfc-style = {
              enable = true;
              # pkgs.nixfmt-rfc-style is a deprecated alias for pkgs.nixfmt in
              # this nixpkgs; use the canonical attr to silence the eval warning.
              package = pkgs.nixfmt;
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
          default = check-ghc-compat;
        };

        # `nix flake check` runs every hook over the tree and fails on diffs.
        checks.pre-commit-check = pre-commit-check;

        # A shell that puts every utility on PATH at once (`nix develop`, or
        # `use flake` from a checkout's .envrc). Stays in sync with `utils`.
        # Entering it also installs the git pre-commit hook and adds the
        # nixfmt/shfmt/shellcheck binaries the hooks use.
        devShells.default = pkgs.mkShell {
          inherit (pre-commit-check) shellHook;
          packages = builtins.attrValues utils ++ pre-commit-check.enabledPackages;
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
