#!/bin/sh
# MI-GAN NPU 去字幕（v4 实验版）
# 前提同 migan.sh：cma=512M + THP=never，NPU 与 llama-server 共存，无需停服务。
L="/opt/glibc:/opt/chrome-root/lib/aarch64-linux-gnu:/opt/chrome-root/lib"
export PYTHONPATH=/opt/py/libs LD_LIBRARY_PATH=/opt/rknpu OMP_NUM_THREADS=4
exec /opt/glibc/ld-linux-aarch64.so.1 --library-path $L /opt/py/python/bin/python3 /opt/py/migan_run4.py "$@"
