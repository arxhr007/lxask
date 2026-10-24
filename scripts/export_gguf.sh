#!/usr/bin/env bash
# Convert the merged fine-tuned model to GGUF and quantize it.
#
#   scripts/export_gguf.sh [merged_dir] [quant ...]
#
# Writes $LXASK_HOME/models/lxask-<quant>.gguf for each quant (default: Q4_K_M Q6_K;
# LXASK_HOME defaults to ~/.local/share/lxask). Needs git and a C++ compiler; llama.cpp
# is cloned and built under ~/.cache/lxask on first use. Uses uv when it is installed
# (the WSL setup), plain python and cmake otherwise (Colab). LXASK_PYTHON names a
# Python that already has torch and transformers, to use instead of either.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -n "${LXASK_PYTHON:-}" ]; then
    PYTHON=("$LXASK_PYTHON")
    if command -v cmake >/dev/null 2>&1; then CMAKE=(cmake); else CMAKE=("$HOME/.local/bin/uvx" cmake); fi
elif command -v uv >/dev/null 2>&1 || [ -x "$HOME/.local/bin/uv" ]; then
    source "$ROOT/scripts/env.sh"
    PYTHON=(uv run --group train python)
    CMAKE=(uvx cmake)
else
    PYTHON=(python)
    CMAKE=(cmake)
fi

MERGED="${1:-$ROOT/runs/lora/merged}"
shift || true
QUANTS=("$@")
[ ${#QUANTS[@]} -eq 0 ] && QUANTS=(Q4_K_M Q6_K)

LLAMA="${LLAMA_CPP_DIR:-$HOME/.cache/lxask/llama.cpp}"
OUT="${LXASK_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/lxask}/models"
mkdir -p "$OUT"

if [ ! -d "$LLAMA" ]; then
    git clone --depth 1 https://github.com/ggml-org/llama.cpp "$LLAMA"
fi
if [ ! -x "$LLAMA/build/bin/llama-quantize" ]; then
    "${CMAKE[@]}" -S "$LLAMA" -B "$LLAMA/build" -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF
    "${CMAKE[@]}" --build "$LLAMA/build" --target llama-quantize -j "$(nproc)"
fi

F16="$OUT/lxask-f16.gguf"
cd "$ROOT"
PYTHONPATH="$LLAMA/gguf-py" "${PYTHON[@]}" "$LLAMA/convert_hf_to_gguf.py" \
    "$MERGED" --outfile "$F16" --outtype f16

for quant in "${QUANTS[@]}"; do
    "$LLAMA/build/bin/llama-quantize" "$F16" "$OUT/lxask-$quant.gguf" "$quant"
    ls -la "$OUT/lxask-$quant.gguf"
done
rm -f "$F16"
