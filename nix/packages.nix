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
      pkgs.pkg-config
      pkgs.rustPlatform.bindgenHook
    ];
    buildInputs = [ pkgs.sqlite ];

    # rusqlite enables the bundled sqlite feature; this opts back into linking
    # the system sqlite from buildInputs instead (libsqlite3-sys honours it).
    env.LIBSQLITE3_SYS_USE_PKG_CONFIG = "1";

    dontUseCmakeConfigure = true;

    # The llama.cpp cmake build otherwise uses one worker per CPU regardless of
    # what the nix build allocated, which can OOM small builders.
    preBuild = ''
      export CMAKE_BUILD_PARALLEL_LEVEL="$NIX_BUILD_CORES"
    '';

    # The model is Apache-2.0 and the docs index carries CC BY content; both
    # licences require their notices to travel with the distribution. The
    # fileset above excludes the repo root, so install them from the flake.
    postInstall = ''
      install -Dm644 ${../LICENSE} $out/share/doc/lxask/LICENSE
      install -Dm644 ${../NOTICE} $out/share/doc/lxask/NOTICE
    '';

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
      # See NOTICE: the model is Apache-2.0, the encoder MIT, and the docs index
      # carries CC BY 4.0 content plus man page excerpts under their own licences.
      license = with lib.licenses; [
        mit
        asl20
        cc-by-40
      ];
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
