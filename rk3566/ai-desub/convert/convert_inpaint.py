#!/usr/bin/env python3
"""把任意 ONNX 图像修复模型转成 RK3566 能跑的 .rknn

用法：
    python3 convert_inpaint.py model.onnx out.rknn [--w 576 --h 64]

为什么脚本长这样：
  · RK3566 是 RKNPU2（0.8 TOPS），运行时 librknnrt 2.3.2（板上实测），
    能吃 rknn-toolkit2 v2.x 导出的模型
  · 但 NPU 没有 FFT / 复杂 Attention 算子：
        LaMa / Big-LaMa  → FFC 用 FFT，转不了
        ZITS / MAT       → Transformer，转不了
        AOT-GAN / MI-GAN → 纯卷积 + 门控卷积，可以转
  · 只需要修字幕带（576x31），所以输入裁到 576x64 就够，算力降一个数量级
  · int8 量化需要校准图：默认用 video_band/*.png（自己片子抠出来的字幕带帧），
    没有就退到随机数据（精度会差，但能跑通验证）
"""
import argparse
import glob
import os
import sys

import numpy as np


def collect_calib(w, h, pattern, limit=32):
    """校准数据：优先用真实字幕带帧，其次用随机噪声。"""
    files = sorted(glob.glob(pattern))[:limit]
    if not files:
        print(f"[!] 没找到校准图 {pattern}，用随机数据代替（精度会降，仅验证可跑）")
        return [np.random.randint(0, 255, (h, w, 3), dtype=np.uint8) for _ in range(8)]

    try:
        from PIL import Image
    except ImportError:
        print("[!] 没装 Pillow，无法读校准图")
        return [np.random.randint(0, 255, (h, w, 3), dtype=np.uint8) for _ in range(8)]

    out = []
    for f in files:
        im = Image.open(f).convert("RGB").resize((w, h))
        out.append(np.array(im, dtype=np.uint8))
    print(f"[i] 校准集 {len(out)} 张：{files[0]} …")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("onnx")
    ap.add_argument("rknn")
    ap.add_argument("--w", type=int, default=576)
    ap.add_argument("--h", type=int, default=64)
    ap.add_argument("--target", default="rk3566")
    ap.add_argument("--calib", default="video_band/*.png")
    ap.add_argument("--fp16", action="store_true", help="不做 int8（调试用，慢但精度高）")
    args = ap.parse_args()

    from rknn.api import RKNN

    rk = RKNN(verbose=True)
    print(f"[1/5] 配置 target={args.target}")
    rk.config(
        # 图像：uint8 0..255 → [-1,1]（模型训练时的归一化，之前没给，等于喂错尺度）
        # mask：0/1 原样（之前只给了第一个输入，mask 用了默认 0/1 std，警告里能看到）
        mean_values=[[127.5, 127.5, 127.5], [0]],
        std_values=[[127.5, 127.5, 127.5], [1]],
        target_platform=args.target,
        quantized_dtype="w8a8",
        quantized_algorithm="normal",
        quantized_method=os.environ.get("RKNN_QM", "layer"),
        optimization_level=int(os.environ.get("RKNN_OPT", "3")),
    )

    print(f"[2/5] 载入 {args.onnx}")
    if rk.load_onnx(model=args.onnx) != 0:
        sys.exit("ONNX 载入失败（模型是否含 NPU 不支持的算子？）")

    print("[3/5] 构建（这一步最慢，几分钟）")
    dataset = None
    if not args.fp16:
        # rknn 要的是"列表文件"，不是通配符（传 *.png 会直接报 not found）
        files = sorted(glob.glob(args.calib))[:6]
        w, h = args.w, args.h
        if not files:
            print(f"[!] 校准图 {args.calib} 一张都没有，转成不用量化会有精度损失")
            dataset = None
        else:
            # 这个模型是双输入（image + mask），dataset.txt 每行要给两个文件
            from PIL import Image
            mdir = "calib_masks"
            os.makedirs(mdir, exist_ok=True)
            dataset = "dataset.txt"
            with open(dataset, "w") as f:
                for i, p in enumerate(files):
                    mp = os.path.join(mdir, "m_%02d.png" % i)
                    if True:   # 每次重生成，旧的 0/255 版本不能再用
                        # 黑底 + 中间横条白（模拟字幕带的位置）
                        im = Image.new("L", (w, h), 0)
                        for yy in range(int(h * 0.62), int(h * 0.92)):
                            for xx in range(w):
                                im.putpixel((xx, yy), 1)
                        im.save(mp)
                    f.write(os.path.abspath(p) + " " + os.path.abspath(mp) + "\n")
            print("[i] 校准列表 %s：%d 张" % (dataset, len(files)))
    if rk.build(do_quantization=dataset is not None, dataset=dataset) != 0:
        sys.exit("build 失败 —— 多半是算子不支持，把日志里的 unsupported op 行贴出来")

    print(f"[4/5] 导出 {args.rknn}")
    if rk.export_rknn(args.rknn) != 0:
        sys.exit("导出失败")

    print("[5/5] 完成")
    print(f"    推到板上：cat {args.rknn} | ssh root@板子 'cat > /opt/llama/models/inpaint.rknn'")
    print("    然后把 video 插件的 desub 切到 mode=npu 就会用它")
    rk.release()


if __name__ == "__main__":
    main()
