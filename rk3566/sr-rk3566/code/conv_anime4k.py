#!/usr/bin/env python3
"""Anime4K ONNX -> RKNN (fp16). 输入 uint8 0-255，runtime 按 std=255 归一化到 0-1。"""
import os
import sys

import onnx
import onnx.helper as _oh
from onnx import TensorProto as _TP

if not hasattr(onnx, 'mapping'):
    _t2np = {}
    for _n, _v in _TP.DataType.items():
        try:
            _t2np[_v] = _oh.tensor_dtype_to_np_dtype(_v)
        except Exception:
            pass

    class _M:
        TENSOR_TYPE_TO_NP_TYPE = _t2np
        NP_TYPE_TO_TENSOR_TYPE = {v: k for k, v in _t2np.items()}

    onnx.mapping = _M

from rknn.api import RKNN  # noqa: E402

src = sys.argv[1]
dst = sys.argv[2]

rknn = RKNN(verbose=True)
print('config:', rknn.config(target_platform='rk3566',
                             mean_values=[[0, 0, 0]],
                             std_values=[[1, 1, 1]],

                             optimization_level=int(os.environ.get('OPT','2'))))
print('load:', rknn.load_onnx(model=src))
print('build:', rknn.build(do_quantization=False))
print('export:', rknn.export_rknn(dst))
rknn.release()
print('size:', os.path.getsize(dst))
