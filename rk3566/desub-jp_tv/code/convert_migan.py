#!/usr/bin/env python3
"""Convert MI-GAN (4ch inpainting net) to RKNN for rk3566."""
import os
import subprocess
import sys

import cv2
import numpy as np

# rknn-toolkit2 2.3.2 还在用 onnx.mapping（新 onnx 已删除），补一个兼容层
import onnx
import onnx.helper as _oh
from onnx import TensorProto as _TP

if not hasattr(onnx, 'mapping'):
    _t2np = {}
    for _name, _val in _TP.DataType.items():
        try:
            _t2np[_val] = _oh.tensor_dtype_to_np_dtype(_val)
        except Exception:
            pass

    class _Mapping:
        TENSOR_TYPE_TO_NP_TYPE = _t2np
        NP_TYPE_TO_TENSOR_TYPE = {v: k for k, v in _t2np.items()}

    onnx.mapping = _Mapping

OUT = '/tmp/migan_calib'
os.makedirs(OUT, exist_ok=True)

W, H = 576, 432
SZ = 512

# --- calibration samples: real frames from the target video, mask in alpha ---
raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', '/tmp/03.mp4',
                      '-vf', 'fps=2', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                     capture_output=True).stdout
frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, H, W, 3)
print('frames:', frames.shape)

idx = np.linspace(0, len(frames) - 1, 12).astype(int)
paths = []
for k, i in enumerate(idx):
    img = frames[i]
    g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    m = np.zeros((H, W), np.uint8)
    m[int(H * 0.72):, :] = (g[int(H * 0.72):, :] > 185).astype(np.uint8) * 255
    mf = cv2.resize((m > 127).astype(np.float32), (SZ, SZ), interpolation=cv2.INTER_LINEAR)
    mf = (mf > 0.5).astype(np.float32)
    erased = cv2.resize(img, (SZ, SZ)).astype(np.float32) * (1.0 - mf[..., None])
    ch0 = (255.0 * (1.0 - mf))
    rgba = np.dstack([ch0, erased]).astype(np.uint8)
    p = os.path.join(OUT, 'c%02d.png' % k)
    cv2.imwrite(p, rgba)          # 4-channel PNG
    paths.append(p)

with open(os.path.join(OUT, 'dataset.txt'), 'w') as f:
    f.write('\n'.join(paths) + '\n')

# --- convert ---
from rknn.api import RKNN  # noqa: E402

quant = '--fp16' not in sys.argv
rknn = RKNN(verbose=True)
rknn.config(
    target_platform='rk3566',
    mean_values=[[127.5, 127.5, 127.5, 127.5]],
    std_values=[[255.0, 127.5, 127.5, 127.5]],
    optimization_level=int(os.environ.get('OPT_LEVEL','3')),
)
print('load_onnx:', rknn.load_onnx(model=os.environ.get('MODEL','/tmp/migan.onnx')))
print('build:', rknn.build(do_quantization=quant,
                           dataset=os.path.join(OUT, 'dataset.txt')))
out = os.environ.get('OUT_RKNN','/tmp/migan_rk3566.rknn')
print('export:', rknn.export_rknn(out))
rknn.release()
print('size:', os.path.getsize(out))
