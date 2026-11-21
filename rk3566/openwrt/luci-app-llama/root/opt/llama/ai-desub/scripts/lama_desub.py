#!/usr/bin/env python3
"""用 LaMa 真正去除硬字幕（逐帧 AI 修复）

流程：
  1. 用 ffmpeg 把字幕带（上下各留 40 行）单独抽成 PNG 序列
  2. 每帧喂 LaMa，遮罩 = 字幕带整条矩形（漏不掉黑描边）
  3. 修复结果拼回原帧 → ffmpeg 合成 + 保留原音轨

为什么只处理条带：面积只有整帧 25%，实测 13.8 秒/帧（整帧 62 秒），效果一样。

用法：
  LAMA_MODEL=/path/big-lama.pt python3 lama_desub.py 输入.mp4 输出.mp4 [--band-y 383] [--band-h 37]
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


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        print("命令失败:", cmd)
        print(r.stderr[-800:])
        sys.exit(1)
    return r.stdout


def probe(path, key):
    out = sh(f'ffprobe -v error -select_streams v:0 -show_entries stream={key} '
             f'-of default=nw=1:nk=1 "{path}"').strip().split("\n")
    return out[0].split()[0] if out else ""


def detect_band(path, w, h, dur):
    """自动找字幕带：抽 12 帧，在下方 45% 里找"亮像素最多"的连续横带"""
    tmp = "/tmp/bandprobe"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    sh(f'{FFMPEG} -hide_banner -loglevel error -i "{path}" -vf "fps=12/{max(dur,1)},format=gray" '
       f'-y {tmp}/%03d.png')
    files = sorted(glob.glob(tmp + "/*.png"))
    if not files:
        return int(h * 0.88), int(h * 0.07)
    rows = None
    for f in files[:12]:
        a = np.array(Image.open(f)).astype(np.int16)
        bright = (a >= 200).sum(axis=1)
        rows = bright if rows is None else rows + bright
    rows = rows.astype(float) / len(files[:12])
    start = int(h * 0.55)
    best_y, best_v = int(h * 0.88), -1
    for y in range(start, h - 8):
        v = rows[y:y + 8].sum()
        if v > best_v:
            best_v, best_y = v, y
    # 向上向下各扩展，直到亮像素明显减少
    thr = max(rows[best_y:best_y + 8].mean() * 0.35, 1.0)
    y0 = best_y
    while y0 > start and rows[y0 - 1] > thr:
        y0 -= 1
    y1 = best_y + 8
    while y1 < h - 1 and rows[y1] > thr:
        y1 += 1
    return max(0, y0 - 2), min(h - y0, y1 - y0 + 4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--band-y", type=int, default=-1)
    ap.add_argument("--band-h", type=int, default=-1)
    ap.add_argument("--pad", type=int, default=40, help="条带上下各留多少行做上下文")
    ap.add_argument("--keep-frames", action="store_true")
    ap.add_argument("--scale", type=float, default=1.0, help="条带先缩到该比例再喂 LaMa（0.5 快 4 倍）")
    args = ap.parse_args()

    w = int(probe(args.src, "width"))
    h = int(probe(args.src, "height"))
    dur = float(probe(args.src, "duration") or 0) if False else None
    d = sh(f'ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "{args.src}"').strip()
    dur = float(d) if d else 10.0
    fps = sh(f'ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate '
             f'-of default=nw=1:nk=1 "{args.src}"').strip()

    if args.band_y < 0:
        by, bh = detect_band(args.src, w, h, dur)
    else:
        by, bh = args.band_y, args.band_h
    print(f"视频 {w}x{h} {dur:.1f}s，字幕带 y={by} h={bh}", flush=True)

    Y0 = max(0, by - args.pad)
    Y1 = min(h, by + bh + args.pad)
    print(f"处理条带 y={Y0}..{Y1}（{Y1 - Y0} 行，占整帧 {100*(Y1-Y0)//h}%）", flush=True)

    # 1) 抽条带
    work = "/tmp/lama_work"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work + "/in", exist_ok=True)
    sh(f'{FFMPEG} -hide_banner -loglevel error -i "{args.src}" -y {work}/in/%05d.png')
    files = sorted(glob.glob(work + "/in/*.png"))
    print(f"抽帧 {len(files)} 张", flush=True)

    from simple_lama_inpainting import SimpleLama
    lama = SimpleLama()

    # 遮罩：整条字幕带矩形（只覆盖字幕那几十行，不含 pad 区）
    os.makedirs(work + "/out", exist_ok=True)
    t_all = time.time()
    for i, f in enumerate(files):
        full = np.array(Image.open(f).convert("RGB"))      # 整帧
        band = full[Y0:Y1].copy()                           # 只取条带
        bh_band = band.shape[0]
        # 可选降采样：LaMa 耗时和面积成正比，缩一半→快 4 倍
        sc = args.scale
        if sc < 1.0:
            nw, nh = max(8, int(w * sc) // 8 * 8), max(8, int(bh_band * sc) // 8 * 8)
            small = np.array(Image.fromarray(band).resize((nw, nh), Image.BILINEAR))
        else:
            nw, nh, small = w, bh_band, band
        m = np.zeros((nh, nw), np.uint8)
        ry0 = int(max(0, by - Y0 - 2) * sc) if sc < 1.0 else max(0, by - Y0 - 2)
        ry1 = int(min(bh_band, by - Y0 + bh + 2) * sc) if sc < 1.0 else min(bh_band, by - Y0 + bh + 2)
        m[ry0:ry1, :] = 255
        fixed = np.array(lama(Image.fromarray(small), Image.fromarray(m)))
        if sc < 1.0:
            fixed = np.array(Image.fromarray(fixed).resize((w, bh_band), Image.BILINEAR))
        # 只把"字幕带"那几十行贴回整帧，其余像素原样
        sy0 = max(0, by - Y0 - 2)
        sy1 = min(bh_band, by - Y0 + bh + 2)
        band[sy0:sy1] = fixed[sy0:sy1]
        full[Y0:Y1] = band
        Image.fromarray(full).save(work + f"/out/{os.path.basename(f)}")
        if (i + 1) % 10 == 0 or i == 0:
            el = time.time() - t_all
            eta = el / (i + 1) * (len(files) - i - 1)
            print(f"  {i+1}/{len(files)}  {el/(i+1):.1f}s/帧  剩余 {eta/60:.0f} 分钟", flush=True)

    # 2) 合成
    sh(f'{FFMPEG} -hide_banner -loglevel error -y -framerate {fps} -i {work}/out/%05d.png '
       f'-i "{args.src}" -map 0:v -map 1:a? -c:v h264_rkmpp -b:v 3M '
       f'-c:a copy -shortest "{args.dst}"')
    print("完成:", args.dst, f"总耗时 {(time.time()-t_all)/60:.1f} 分钟", flush=True)
    if not args.keep_frames:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
