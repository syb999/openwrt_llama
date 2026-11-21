#!/usr/bin/env python3
"""全屏字幕/职员表去除（按帧检测文字位置，不用固定带）

和 lama_desub.py 的区别：
  · 遮罩不是固定的一条，而是每帧自己找"亮文字像素 + 膨胀"
  · 处理整帧（适合片头/片尾这种字散布全屏的素材）

用法：
  python3 lama_full.py 输入.mp4 输出.mp4 [--frames 5] [--white 200] [--dil 3]
  --frames N   只处理前 N 帧（预览用）
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys
import time

import numpy as np

def _pick_ffmpeg():
    import os as _os
    for c in ("/usr/bin/ffmpeg-rkrga", "/usr/bin/ffmpeg"):
        if _os.path.exists(c):
            return c
    return "ffmpeg"

FFMPEG = _pick_ffmpeg()
from PIL import Image
from scipy import ndimage


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        print("失败:", cmd[:120], r.stderr[-400:])
        sys.exit(1)
    return r.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--frames", type=int, default=0, help="只处理前 N 帧（0=全部）")
    ap.add_argument("--white", type=int, default=200)
    ap.add_argument("--dil", type=int, default=3)
    ap.add_argument("--size", type=int, default=512)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    work = "/tmp/lama_full"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work + "/in", exist_ok=True)
    os.makedirs(work + "/out", exist_ok=True)

    n = f"-frames:v {args.frames}" if args.frames else ""
    sh(f'{FFMPEG} -hide_banner -loglevel error -y -i "{args.src}" {n} {work}/in/%05d.png')
    files = sorted(glob.glob(work + "/in/*.png"))
    fps = sh(f'ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate '
             f'-of default=nw=1:nk=1 "{args.src}"').strip()
    print(f"帧数 {len(files)}, fps {fps}", flush=True)

    from simple_lama_inpainting import SimpleLama
    lama = SimpleLama()
    S = args.size
    t_all = time.time()
    for i, f in enumerate(files):
        im = Image.open(f).convert("RGB")
        W, H = im.size
        small = im.resize((S, S), Image.BILINEAR)
        g = np.array(small.convert("L"))
        m = g >= args.white
        if args.dil:
            m = ndimage.binary_dilation(m, iterations=args.dil)
        if m.sum() < 20:                       # 这帧没字
            Image.open(f).save(work + f"/out/{os.path.basename(f)}")
            continue
        out = np.array(lama(small, Image.fromarray((m * 255).astype(np.uint8))))
        Image.fromarray(out).resize((W, H), Image.LANCZOS).save(
            work + f"/out/{os.path.basename(f)}")
        if (i + 1) % 5 == 0 or i == 0:
            el = time.time() - t_all
            print(f"  {i+1}/{len(files)}  {el/(i+1):.1f}s/帧  剩余 {(len(files)-i-1)*el/(i+1)/60:.0f} 分钟",
                  flush=True)

    sh(f'{FFMPEG} -hide_banner -loglevel error -y -framerate {fps} -i {work}/out/%05d.png '
       f'-i "{args.src}" -map 0:v -map 1:a? -c:v h264_rkmpp -b:v 3M '
       f'-c:a copy -shortest "{args.dst}"')
    print(f"完成 {args.dst}  总耗时 {(time.time()-t_all)/60:.1f} 分钟", flush=True)
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
