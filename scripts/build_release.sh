#!/usr/bin/env bash
# Portable lxask binaries and the Python wheel, for a release.
#
#   scripts/build_release.sh OUT_DIR [variant ...]
#
# Variants: x86_64-v3 (AVX2/FMA, nearly every x86 PC since 2015), x86_64 (SSE4.2, the
# older ones), aarch64 (ARMv8-A with NEON: Raspberry Pi 4 and 5, ARM servers), wheel
# (lxask._core for training and evaluation, abi3 for Python 3.10+). Default: all four.
#
# Needs: rustup with the aarch64-unknown-linux-gnu target, cargo-zigbuild and zig
# (pip install ziglang), maturin, and libclang for bindgen (pip install libclang). Each
# binary links glibc 2.28 or newer and nothing else but libm and libc; llama.cpp uses
# its own thread pool (no OpenMP) and zig links the C++ runtime in.
set -euo pipefail
OUT=${1:?usage: build_release.sh OUT_DIR [variant ...]}
shift
VARIANTS=${*:-x86_64-v3 x86_64 aarch64 wheel}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
PY=${PYTHON:-python3}
export LIBCLANG_PATH=${LIBCLANG_PATH:-$("$PY" -c 'import clang, os; print(os.path.join(os.path.dirname(clang.__file__), "native"))')}
export BINDGEN_EXTRA_CLANG_ARGS="-I$(gcc -print-file-name=include) ${BINDGEN_EXTRA_CLANG_ARGS:-}"
export CMAKE_BUILD_PARALLEL_LEVEL=${CMAKE_BUILD_PARALLEL_LEVEL:-2} CARGO_BUILD_JOBS=${CARGO_BUILD_JOBS:-2}
GLIBC=2.28

build() {  # build NAME TARGET CPU [extra cargo env]
    local name=$1 target=$2 cpu=$3
    echo "== $name ($target, $cpu)"
    CARGO_TARGET_DIR=${TARGET_BASE:-$HOME/.cache/lxask-release-target}/$name \
    RUSTFLAGS="-C target-cpu=$cpu" \
        cargo zigbuild --release --manifest-path "$ROOT/rust/Cargo.toml" -p lxask --target "$target.$GLIBC"
    cp "${TARGET_BASE:-$HOME/.cache/lxask-release-target}/$name/$target/release/lxask" "$OUT/lxask-$name"
}

for variant in $VARIANTS; do
    case $variant in
        x86_64-v3) build x86_64-v3 x86_64-unknown-linux-gnu x86-64-v3 ;;
        x86_64) build x86_64 x86_64-unknown-linux-gnu x86-64-v2 ;;
        aarch64) GGML_CPU_ARM_ARCH=armv8-a build aarch64 aarch64-unknown-linux-gnu generic ;;
        wheel)
            echo "== wheel"
            (cd "$ROOT" && CARGO_TARGET_DIR=${TARGET_BASE:-$HOME/.cache/lxask-release-target}/wheel \
                maturin build --release --zig --compatibility manylinux_2_28 --target x86_64-unknown-linux-gnu --out "$OUT")
            ;;
        *) echo "unknown variant $variant" >&2; exit 2 ;;
    esac
done
ls -la "$OUT"
