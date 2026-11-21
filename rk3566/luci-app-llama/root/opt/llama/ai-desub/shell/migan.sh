#!/bin/sh
# MI-GAN NPU 去字幕（v1 主力）
#
# 前提：固件已配 cma=512M 且 THP=never（/etc/config/thp）。
# 有了这两条，NPU 和 llama-server 可以共存 —— 之前要停 LLM 腾内存，
# 那会把模型上下文干掉，所以这里不再停任何服务。
L="/opt/glibc:/opt/chrome-root/lib/aarch64-linux-gnu:/opt/chrome-root/lib"
export PYTHONPATH=/opt/py/libs LD_LIBRARY_PATH=/opt/rknpu OMP_NUM_THREADS=4
exec /opt/glibc/ld-linux-aarch64.so.1 --library-path $L /opt/py/python/bin/python3 /opt/py/migan_run.py "$@"
