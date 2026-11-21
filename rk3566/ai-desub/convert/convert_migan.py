#!/usr/bin/env python3
"""MI-GAN → RK3566 rknn

和 AOT-GAN 的区别：
  · 单输入、4 通道（ch0=mask±0.5, ch1..3=RGB[-1,1]）
  · 输入用 float32（mask 只有 ±0.5 两个值，uint8 量化会把它压坏）
  · 6M 参数、94 个 Conv，纯卷积 → NPU 友好
"""
import argparse
import glob
import os
import sys

import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("--onnx", default="migan512.onnx")
ap.add_argument("--rknn", default="migan512.rk3566.rknn")
ap.add_argument("--size", type=int, default=512)
ap.add_argument("--calib", default="video_band/*.png")
ap.add_argument("--target", default="rk3566")
args = ap.parse_args()

from rknn.api import RKNN

rk = RKNN(verbose=True)
rk.config(
    # 4 通道输入不能给 mean/std（rknn 会按 3 通道校验并报错），
    # 归一化在前处理里做完，直接喂 float32
    target_platform=args.target,
    optimization_level=3,
)
print(f"[1/4] 载入 {args.onnx}")
if rk.load_onnx(model=args.onnx) != 0:
    sys.exit("ONNX 载入失败")

# 校准集：RGBA PNG（A=mask，0=洞 255=保留）
from PIL import Image
os.makedirs("migan_calib", exist_ok=True)
lines = []
for i, f in enumerate(sorted(glob.glob(args.calib))[:6]):
    rgb = Image.open(f).convert("RGB").resize((args.size, args.size))
    a = Image.new("L", (args.size, args.size), 255)          # 全保留
    rgba = Image.merge("RGBA", (*rgb.split(), a))
    out = f"migan_calib/c{i}.png"
    rgba.save(out)
    lines.append(os.path.abspath(out))
with open("migan_dataset.txt", "w") as fh:
    for p2 in lines:
        fh.write(p2 + "\n")
print(f"[2/4] 校准 {len(lines)} 张（RGBA）")

print("[3/4] build")
# 4 通道输入无法用图片做量化校准（rknn 只接受 3 通道图片）→ 走 fp16
if rk.build(do_quantization=False, dataset=None) != 0:
    sys.exit("build 失败")
print("[4/4] 导出", args.rknn)
if rk.export_rknn(args.rknn) != 0:
    sys.exit("导出失败")
print("完成:", args.rknn)
rk.release()
