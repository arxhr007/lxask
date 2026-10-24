#!/usr/bin/env bash
# Collect the files a release carries:
#
#   scripts/build_release.sh OUT_DIR       # the lxask programs and the Python wheel
#   scripts/release_files.sh OUT_DIR       # adds the model, the encoder, the index
#   gh release create v1.0.0 -R arxhr007/lxask OUT_DIR/*
#
# OUT_DIR ends up with lxask-x86_64-v3, lxask-x86_64, lxask-aarch64, the lxask._core wheel
# (for training and evaluation; users do not need it), the model (named after the file
# model.gguf points to), its restriction adapter (named after the file restrict.gguf
# points to, if there is one), embed.gguf, docs.sqlite, LICENSE, NOTICE, install.sh and
# SHA256SUMS.
# scripts/install.sh installs from exactly these, downloaded or copied (--from OUT_DIR).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT=${1:?usage: release_files.sh OUT_DIR}
DATA="${LXASK_HOME:-${LINU_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/lxask}}"
# installed as linu (0.8 and earlier) and not moved yet
[ -e "$DATA/model.gguf" ] || [ -n "${LXASK_HOME:-${LINU_HOME:-}}" ] || DATA="${XDG_DATA_HOME:-$HOME/.local/share}/linu"
for program in lxask-x86_64-v3 lxask-x86_64 lxask-aarch64; do
    [ -x "$OUT/$program" ] || { echo "$OUT/$program missing: run scripts/build_release.sh $OUT first" >&2; exit 1; }
done

MODEL=$(readlink -f "$DATA/model.gguf")
NAME=$(basename "$MODEL")
case $NAME in lxask-*.gguf) ;; linu-*.gguf) NAME="lxask-${NAME#linu-}" ;; *) NAME="lxask-$NAME" ;; esac  # linu: its name up to 0.8

cp "$MODEL" "$OUT/$NAME"
ADAPTER_NAME=""
if [ -e "$DATA/restrict.gguf" ]; then
    ADAPTER=$(readlink -f "$DATA/restrict.gguf")
    ADAPTER_NAME=$(basename "$ADAPTER")
    case $ADAPTER_NAME in restrict-*.gguf) ;; *) ADAPTER_NAME="restrict-$ADAPTER_NAME" ;; esac
    cp "$ADAPTER" "$OUT/$ADAPTER_NAME"
fi
cp "$DATA/embed.gguf" "$DATA/docs.sqlite" "$OUT/"
cp "$ROOT/scripts/install.sh" "$OUT/install.sh"
cp "$ROOT/LICENSE" "$ROOT/NOTICE" "$OUT/"
(cd "$OUT" && sha256sum lxask-x86_64-v3 lxask-x86_64 lxask-aarch64 lxask-*.whl "$NAME" $ADAPTER_NAME embed.gguf docs.sqlite LICENSE NOTICE install.sh > SHA256SUMS)
ls -la "$OUT"
cat "$OUT/SHA256SUMS"
