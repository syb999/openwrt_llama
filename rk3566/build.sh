#!/bin/bash
# Cross-build llama.cpp for Rockchip RK3566/RK3568 running OpenWrt (musl, aarch64).
# Produces statically linked llama-cli / llama-bench that need no runtime libs.
#
# Usage:
#   ./build.sh                      build only
#   ./build.sh --with-model         also download the Qwen2.5-0.5B Q4_K_M GGUF
#   ./build.sh --push 10.80.1.125   build and copy everything to /opt/llama on the board
#   ./build.sh --clean              wipe build/ and out/ first
#
# Env overrides: WORK, OUT, LLAMA_REF, JOBS, MCU, MODEL, STRIP=1

set -euo pipefail

WORK="${WORK:-$(pwd)/build}"
OUT="${OUT:-$(pwd)/out}"
LLAMA_REF="${LLAMA_REF:-master}"
JOBS="${JOBS:-$(nproc)}"
MCU="${MCU:-cortex-a55+dotprod}"
STRIP="${STRIP:-0}"

TC_URL="https://musl.cc/aarch64-linux-musl-cross.tgz"
MODEL="qwen2.5-0.5b-instruct-q4_k_m.gguf"
MODEL_URL="https://modelscope.cn/models/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/master/${MODEL}"
TC_DIR="$WORK/aarch64-linux-musl-cross"

WITH_MODEL=0
PUSH_HOST=""

while [ $# -gt 0 ]; do
    case "$1" in
        --with-model) WITH_MODEL=1; shift ;;
        --push)       PUSH_HOST="${2:-}"; shift 2 ;;
        --clean)      rm -rf "$WORK" "$OUT"; echo "cleaned"; exit 0 ;;
        -h|--help)    sed -n '2,14p' "$0"; exit 0 ;;
        *)            echo "unknown argument: $1" >&2; exit 1 ;;
    esac
done

log() { printf '\n==> %s\n' "$*"; }

# ---------------------------------------------------------------- toolchain
fetch_toolchain() {
    if [ -x "$TC_DIR/bin/aarch64-linux-musl-gcc" ]; then
        log "toolchain present: $($TC_DIR/bin/aarch64-linux-musl-gcc -dumpversion)"
        return
    fi
    log "downloading musl.cc toolchain (108MB, resumable)"
    mkdir -p "$WORK"
    curl -L --retry 3 -C - -o "$WORK/musl-cross.tgz" "$TC_URL"
    tar xzf "$WORK/musl-cross.tgz" -C "$WORK"
    "$TC_DIR/bin/aarch64-linux-musl-gcc" --version | head -1
}

# ------------------------------------------------------------------ source
fetch_llama() {
    if [ -f "$WORK/llama.cpp/CMakeLists.txt" ]; then
        log "llama.cpp present"
        return
    fi
    log "downloading llama.cpp ($LLAMA_REF)"
    mkdir -p "$WORK"
    if [ "$LLAMA_REF" = "master" ]; then
        url="https://codeload.github.com/ggml-org/llama.cpp/tar.gz/refs/heads/master"
    else
        url="https://codeload.github.com/ggml-org/llama.cpp/tar.gz/refs/tags/$LLAMA_REF"
    fi
    curl -L --retry 3 -o "$WORK/llama.cpp.tar.gz" "$url"
    tar xzf "$WORK/llama.cpp.tar.gz" -C "$WORK"
    rm -rf "$WORK/llama.cpp"
    mv "$WORK"/llama.cpp-* "$WORK/llama.cpp"
}

# --------------------------------------------------------------- cmake file
write_toolchain_file() {
    cat > "$WORK/toolchain-aarch64-musl.cmake" <<EOF
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)

set(PFX $TC_DIR)
set(CMAKE_SYSROOT \${PFX}/aarch64-linux-musl)

set(TOOLCHAIN_PREFIX \${PFX}/bin/aarch64-linux-musl-)
set(CMAKE_C_COMPILER   \${TOOLCHAIN_PREFIX}gcc)
set(CMAKE_CXX_COMPILER \${TOOLCHAIN_PREFIX}g++)
set(CMAKE_AR      \${TOOLCHAIN_PREFIX}ar)
set(CMAKE_RANLIB  \${TOOLCHAIN_PREFIX}ranlib)
set(CMAKE_STRIP   \${TOOLCHAIN_PREFIX}strip)

set(CMAKE_FIND_ROOT_PATH \${PFX}/aarch64-linux-musl)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
EOF
}

# -------------------------------------------------------------------- build
build() {
    log "configuring"
    cmake -S "$WORK/llama.cpp" -B "$WORK/llama.cpp/build-musl" \
        -DCMAKE_TOOLCHAIN_FILE="$WORK/toolchain-aarch64-musl.cmake" \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_NATIVE=OFF \
        -DGGML_OPENMP=OFF \
        -DGGML_CPU_ARM_ARCH= \
        -DLLAMA_CURL=OFF \
        -DBUILD_SHARED_LIBS=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_SERVER=ON \
        -DCMAKE_C_FLAGS="-O3 -mcpu=$MCU" \
        -DCMAKE_CXX_FLAGS="-O3 -mcpu=$MCU" \
        -DCMAKE_EXE_LINKER_FLAGS="-static" \
        2>&1 | grep -E "HAVE_DOTPROD|HAVE_MATMUL_INT8|Configuring done|Configuring incomplete" || true

    log "building llama-cli + llama-bench (-j$JOBS)"
    cmake --build "$WORK/llama.cpp/build-musl" --target llama-cli llama-bench -- -j"$JOBS"

    mkdir -p "$OUT"
    cp "$WORK/llama.cpp/build-musl/bin/llama-cli" "$OUT/"
    cp "$WORK/llama.cpp/build-musl/bin/llama-bench" "$OUT/"

    if [ "$STRIP" = "1" ]; then
        "$TC_DIR/bin/aarch64-linux-musl-strip" "$OUT/llama-cli" "$OUT/llama-bench"
    fi

    log "verify"
    file "$OUT/llama-cli" | cut -c1-120
    case "$(file -b "$OUT/llama-cli")" in
        *"static-pie"*|*"statically linked"*) echo "OK: statically linked" ;;
        *) echo "WARNING: not statically linked, the board will not find the loader" >&2 ;;
    esac
    ls -lh "$OUT"
}

# --------------------------------------------------------------------- model
fetch_model() {
    if [ -s "$WORK/$MODEL" ]; then
        log "model present: $(stat -c%s "$WORK/$MODEL") bytes"
        return
    fi
    log "downloading model from ModelScope (469 MiB)"
    curl -L --retry 3 -C - -o "$WORK/$MODEL" "$MODEL_URL"
    printf 'magic: '; head -c 4 "$WORK/$MODEL"; echo
    stat -c 'size: %s bytes' "$WORK/$MODEL"
}

# ---------------------------------------------------------------------- push
push() {
    [ -n "$PUSH_HOST" ] || return 0
    log "pushing to $PUSH_HOST:/opt/llama"
    ssh "root@$PUSH_HOST" 'mkdir -p /opt/llama'
    cat "$OUT/llama-cli"   | ssh "root@$PUSH_HOST" 'cat > /opt/llama/llama-cli'
    cat "$OUT/llama-bench" | ssh "root@$PUSH_HOST" 'cat > /opt/llama/llama-bench'
    ssh "root@$PUSH_HOST" 'chmod +x /opt/llama/llama-cli /opt/llama/llama-bench'
    if [ -s "$WORK/$MODEL" ]; then
        cat "$WORK/$MODEL" | ssh "root@$PUSH_HOST" "cat > /opt/llama/$MODEL"
    fi
    ssh "root@$PUSH_HOST" 'ls -l /opt/llama/'

    log "run it"
    cat <<EOF
ssh root@$PUSH_HOST '/opt/llama/llama-cli -m /opt/llama/$MODEL -st -p "你好" -n 40 -t 4'
ssh root@$PUSH_HOST '/opt/llama/llama-bench -m /opt/llama/$MODEL -t 4 -p 128 -n 64'
EOF
}

fetch_toolchain
fetch_llama
write_toolchain_file
build
[ "$WITH_MODEL" = "1" ] && fetch_model
push

log "done -> $OUT"
