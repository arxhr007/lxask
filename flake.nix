{
  description = "lxask - offline Linux command assistant (Rust program)";

  # Pinned to the same nixpkgs revision as the host system.
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/02c087f3fe673c9e317f206a627cb6b910336dbe";

  outputs =
    { self, nixpkgs }:
    let
      lib = nixpkgs.lib;
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAll = f: lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});

      hashes = builtins.fromJSON (builtins.readFile ./nix/hashes.json);
      mkPackages = pkgs: import ./nix/packages.nix { inherit lib pkgs hashes; };

      tools =
        pkgs: with pkgs; [
          cargo
          rustc
          rustfmt
          clippy
          rust-analyzer
          cmake
          clang
          libclang
          pkg-config
        ];
      shellEnv = pkgs: {
        LIBCLANG_PATH = "${pkgs.libclang.lib}/lib";
        RUST_SRC_PATH = "${pkgs.rustPlatform.rustLibSrc}";
      };
    in
    {
      # nix build .#lxask builds the program with the bundled model assets.
      packages = forAll mkPackages;

      # For NixOS / home-manager users: nixpkgs.overlays = [ lxask.overlays.default ];
      overlays.default =
        final: _prev:
        let
          p = mkPackages final;
        in
        {
          inherit (p) lxask lxask-unwrapped;
        };

      # nix develop: Rust toolchain; nix develop .#eval adds Python tooling.
      devShells = forAll (pkgs: {
        default = pkgs.mkShell (
          {
            nativeBuildInputs = tools pkgs;
          }
          // shellEnv pkgs
        );
        eval = pkgs.mkShell (
          {
            nativeBuildInputs =
              tools pkgs
              ++ (with pkgs; [
                python312
                uv
                maturin
                python312.pkgs.pytest
              ]);
          }
          // shellEnv pkgs
        );
      });

      # nix flake check builds the unwrapped binary without downloading models.
      checks = forAll (pkgs: {
        build = self.packages.${pkgs.stdenv.hostPlatform.system}.lxask-unwrapped;
      });
    };
}
