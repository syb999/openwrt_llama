#!/usr/bin/env python3
"""AOT-GAN 生成器 → ONNX

权重来自 github.com/TT113/aotgan-app（G0000000.pt，58MB，raw 直链可下），
网络定义来自 researchmm/AOT-GAN-for-Inpainting 的 src/model/aotgan.py。

AOT-GAN 是纯卷积 + 门控（AOT block：多膨胀率卷积 + 通道门控），
没有 FFT、没有 Self-Attention —— 这是它能转 RKNPU2 的原因
（LaMa 的 FFC 用 FFT、ZITS 用 Transformer，都转不了）。
"""
import argparse
import os
import sys
import types

import torch
import torch.nn as nn


def build_model(rates, block_num):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
    from model.aotgan import InpaintGenerator

    args = types.SimpleNamespace(rates=rates, block_num=block_num)
    net = InpaintGenerator(args)
    return net


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default="G0000000.pt")
    ap.add_argument("--out", default="aotgan.onnx")
    ap.add_argument("--w", type=int, default=576)
    ap.add_argument("--h", type=int, default=64)
    ap.add_argument("--rates", default="3,6,12")
    ap.add_argument("--blocks", type=int, default=8)
    args = ap.parse_args()

    rates = [int(x) for x in args.rates.split(",")]
    # 权重里有多少个 block 就用多少个（官方有 [3,6,12] 和 [3,6,12,24] 两种）
    try:
        import re
        sd0 = torch.load(args.weights, map_location="cpu", weights_only=False)
        idx = set(int(m.group(1)) for k in sd0 for m in [re.search(r"middle\.0\.block(\d+)\.", k)]
                  if m)
        if idx:
            need = max(idx) + 1
            pool = [3, 6, 12, 24, 36]
            rates = pool[:need]
            print(f"     权重里有 {need} 个膨胀率分支 → rates={rates}")
    except Exception as e:
        print("     推断 rates 失败，用默认:", e)
    print(f"[1/4] 构建 InpaintGenerator(rates={rates}, block_num={args.blocks})")
    net = build_model(rates, args.blocks)

    print(f"[2/4] 载入权重 {args.weights}")
    sd = torch.load(args.weights, map_location="cpu", weights_only=False)
    if isinstance(sd, dict) and "model" in sd:
        sd = sd["model"]
    missing, unexpected = net.load_state_dict(sd, strict=True) if False else (None, None)
    try:
        net.load_state_dict(sd, strict=True)
    except RuntimeError as e:
        print("     严格加载失败，做前缀清理后重试：", str(e)[:120])
        clean = {k.replace("module.", ""): v for k, v in sd.items()}
        net.load_state_dict(clean, strict=True)
    net.eval()

    n = sum(p.numel() for p in net.parameters()) / 1e6
    print(f"     参数量 {n:.1f} M")

    print(f"[3/4] 导出 ONNX（输入 3x{args.h}x{args.w} + mask 1x{args.h}x{args.w}）")
    img = torch.randn(1, 3, args.h, args.w)
    msk = torch.zeros(1, 1, args.h, args.w)
    msk[..., args.h // 4: args.h * 3 // 4, args.w // 6: args.w * 5 // 6] = 1.0

    torch.onnx.export(
        net,
        (img, msk),
        args.out,
        input_names=["image", "mask"],
        output_names=["out"],
        opset_version=13,
        do_constant_folding=True,
        dynamo=False,          # 用旧导出器，免得依赖 onnxscript
    )
    print(f"[4/4] 完成 {args.out}（{os.path.getsize(args.out)/1048576:.1f} MB）")
    print("\n下一步：")
    print(f"  python3 convert_inpaint.py {args.out} aotgan.rknn --w {args.w} --h {args.h}")
    print("  （若 build 报不支持的算子，把日志里 unsupported 那一行贴出来，"
          "常见是 ReflectionPad2d → 可改成零填充或裁掉）")


if __name__ == "__main__":
    main()
