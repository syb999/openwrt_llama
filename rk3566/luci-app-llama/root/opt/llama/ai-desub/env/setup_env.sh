#!/bin/sh
# 在这台 RK3566 板子（OpenWrt 21.02.5 / musl / aarch64）上重建 AI 去字幕的运行环境
#
# 背景：板子是 musl，PyTorch 只发 glibc 轮子，OpenWrt 也编不了 torch。
# 办法是用板上已有的 /opt/glibc（ppocr/chrome 带的）配一个自包含 aarch64 Python。
#
# 用法：sh setup_env.sh            （全部装到 /opt/py）
# 需要：板子能上网（github.com / pypi.org 都要能通），磁盘 1.5GB 可用
set -e

# 以脚本自身位置为基准（本脚本随固件放在 /opt/llama/ai-desub/env/，
# 模型在 ../models/，不依赖当前目录）
SELF_DIR=$(cd "$(dirname "$0")" && pwd)
PKG_DIR=$(cd "$SELF_DIR/.." && pwd)

PY_VER="3.10.13"
PY_TAG="20240107"
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PY_TAG}/cpython-${PY_VER}%2B${PY_TAG}-aarch64-unknown-linux-gnu-install_only.tar.gz"
L="/opt/glibc:/opt/chrome-root/lib/aarch64-linux-gnu:/opt/chrome-root/lib"

echo "== 1. 检查 glibc 运行时 =="
for f in /opt/glibc/ld-linux-aarch64.so.1 /opt/glibc/libc.so.6; do
    [ -f "$f" ] || { echo "!! 缺 $f —— 这块板子没有 glibc 运行时，本方案不适用"; exit 1; }
done
ls /opt/chrome-root/lib/aarch64-linux-gnu/libutil.so.1 >/dev/null 2>&1 || \
    echo "   (提示：没有 chrome-root 的 libutil，若 python 报缺库再想办法"

echo "== 2. 下载自包含 Python ${PY_VER} (aarch64/glibc) =="
mkdir -p /opt/py && cd /opt/py
[ -x /opt/py/python/bin/python3 ] || {
    curl -sL --max-time 600 -o py.tar.gz "$PY_URL"
    tar xzf py.tar.gz
}
/opt/glibc/ld-linux-aarch64.so.1 --library-path $L /opt/py/python/bin/python3 -V

echo "== 3. 装依赖（强制 manylinux2014_aarch64 平台）=="
export PYTHONPATH=/opt/py/libs
PIP="/opt/glibc/ld-linux-aarch64.so.1 --library-path $L /opt/py/python/bin/python3 -m pip"
$PIP install --target /opt/py/libs --upgrade pip >/dev/null

# 版本锁定：和实测通过的组合完全一致，别乱升级
$PIP install --target /opt/py/libs --platform manylinux2014_aarch64 \
    --only-binary=:all: --python-version 3.10 \
    torch==2.5.1 numpy==2.2.6 pillow==12.2.0 scipy==1.15.3 \
    opencv-python-headless==5.1.0.95 rknn-toolkit-lite2==2.3.2

# simple-lama-inpainting 是纯 python，不走二进制平台参数
$PIP install --target /opt/py/libs --no-deps simple-lama-inpainting==0.1.2

echo "== 4. rknnlite 需要的库软链 =="
[ -f /usr/lib/librknnrt.so ] || ln -sf /opt/rknpu/librknnrt.so /usr/lib/librknnrt.so
ls -l /usr/lib/librknnrt.so

echo "== 5. 放模型 =="
mkdir -p /opt/py
for m in migan512.rknn; do
    if [ -f "$PKG_DIR/models/$m" ] && [ ! -f "/opt/py/$m" ]; then
        cp "$PKG_DIR/models/$m" "/opt/py/$m"
        echo "   已放 /opt/py/$m"
    fi
done
# 脚本和模型统一放到 /opt/py —— 插件调的就是 /opt/py/migan.sh，
# 这样路径只有一处（run()) 不用改 C 代码
[ -f "$PKG_DIR/models/migan512.rknn" ] && cp -n "$PKG_DIR/models/migan512.rknn" /opt/py/ 2>/dev/null
for f in "$PKG_DIR"/scripts/*.py; do [ -f "$f" ] && cp -n "$f" /opt/py/ 2>/dev/null; done
for f in "$PKG_DIR"/shell/*.sh; do [ -f "$f" ] && cp -n "$f" /opt/py/ 2>/dev/null; done
chmod +x /opt/py/*.sh 2>/dev/null
echo "   脚本与模型已就位：$(ls /opt/py/*.sh 2>/dev/null | wc -l) 个入口脚本，$(ls /opt/py/*.py 2>/dev/null | wc -l) 个 py，模型 $(du -h /opt/py/migan512.rknn 2>/dev/null | cut -f1)"

# LaMa 权重 196MB 不便放仓库，用时自己下
if [ ! -f /opt/py/big-lama.pt ]; then
    echo "   (可选) LaMa: curl -sL -o /opt/py/big-lama.pt \\"
    echo "     https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt"
fi

echo "== 6. 自检 =="
/opt/glibc/ld-linux-aarch64.so.1 --library-path $L /opt/py/python/bin/python3 -c "
import torch, numpy, PIL, scipy, cv2
from rknnlite.api import RKNNLite
print('  torch', torch.__version__, '| numpy', numpy.__version__, '| cv2', cv2.__version__)
print('  rknnlite OK')
"

echo
echo "完成。跑一帧试试："
echo "  cd /opt/py && ./migan.sh /opt/mp4/03.mp4 /tmp/out.mp4 --band-y 383 --band-h 49"
