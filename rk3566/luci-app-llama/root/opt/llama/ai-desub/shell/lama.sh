#!/bin/sh
L="/opt/glibc:/opt/chrome-root/lib/aarch64-linux-gnu:/opt/chrome-root/lib"
export PYTHONPATH=/opt/py/libs
export LAMA_MODEL=/opt/py/big-lama.pt
export OMP_NUM_THREADS=4
exec /opt/glibc/ld-linux-aarch64.so.1 --library-path $L /opt/py/python/bin/python3 /opt/py/lama_desub.py "$@"
