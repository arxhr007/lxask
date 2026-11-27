{
  lib,
  pkgs,
  hashes,
}:
let
  version = (builtins.fromTOML (builtins.readFile ../rust/Cargo.toml)).workspace.package.version;
  base = "https://github.com/arxhr007/lxask/releases/download/${hashes.release}";

  # Model assets are fetched once into the store and shared by the packages.
  models = pkgs.linkFarm "lxask-models" (
    lib.mapAttrsToList (name: hash: {
      inherit name;
      path = pkgs.fetchurl {
        url = "${base}/${name}";
        inherit hash;
      };
    }) hashes.model
  );

  # The CLI built from the rust/ workspace, without model files.
  unwrapped = pkgs.rustPlatform.buildRustPackage {
    pname = "lxask";
    inherit version;

    src = lib.fileset.toSource {
      root = ../.;
      fileset = lib.fileset.difference ../rust (lib.fileset.maybeMissing ../rust/target);
    };
    cargoRoot = "rust";
    buildAndTestSubdir = "rust";
    cargoLock.lockFile = ../rust/Cargo.lock;

    nativeBuildInputs = [
      pkgs.cmake
      pkgs.rustPlatform.bindgenHook
    ];
    dontUseCmakeConfigure = true;

    cargoBuildFlags = [
      "-p"
      "lxask"
    ];

    doCheck = false;

    meta = {
      description = "Offline Linux command assistant: prints commands, never runs them";
      homepage = "https://github.com/arxhr007/lxask";
      license = lib.licenses.mit;
      platforms = lib.platforms.linux;
      mainProgram = "lxask";
    };
  };

  # Reuses unwrapped and adds the model paths through a wrapper.
  wrapped = pkgs.symlinkJoin {
    name = "lxask-${version}";
    paths = [ unwrapped ];
    nativeBuildInputs = [ pkgs.makeWrapper ];
    postBuild = ''
      wrapProgram $out/bin/lxask \
        --set-default LXASK_MODEL       "${models}/lxask-round4-Q4_K_M.gguf" \
        --set-default LXASK_ADAPTER     "${models}/restrict-round4.gguf" \
        --set-default LXASK_PRECISION   "${models}/precision-round4.gguf" \
        --set-default LXASK_EMBED_MODEL "${models}/embed.gguf" \
        --set-default LXASK_INDEX       "${models}/docs.sqlite"
    '';
    meta = unwrapped.meta // {
      description = "${unwrapped.meta.description} (with bundled models)";
      license = [ lib.licenses.mit ];
    };
  };
in
{
  # Binary only; pair with LXASK_SERVER or a separate model install.
  lxask-unwrapped = unwrapped;
  # Binary plus the bundled model, adapters, encoder and docs index.
  lxask = wrapped;
  # nix build .#default produces the wrapped package.
  default = wrapped;
}
