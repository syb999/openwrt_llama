#!/bin/sh
# MI-GAN NPU 去字幕（v1：整帧处理，实测在 01m/03 上都不留残影）
L="/opt/glibc:/opt/chrome-root/lib/aarch64-linux-gnu:/opt/chrome-root/lib"
export PYTHONPATH=/opt/py/libs LD_LIBRARY_PATH=/opt/rknpu OMP_NUM_THREADS=4
LLAMA_WAS=0
if ps w | grep -q "[l]lama-server"; then LLAMA_WAS=1; /etc/init.d/llama stop >/dev/null 2>&1; sleep 2; fi
sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null
/opt/glibc/ld-linux-aarch64.so.1 --library-path $L /opt/py/python/bin/python3 /opt/py/migan_run.py "$@"
RC=$?
[ "$LLAMA_WAS" = "1" ] && /etc/init.d/llama start >/dev/null 2>&1
exit $RC
