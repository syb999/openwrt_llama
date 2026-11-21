#!/usr/bin/env python3
"""MI-GAN 在 RK3566 NPU 上去字幕（rknn-toolkit-lite2）

用法（板上）：
  ./migan.sh 输入.mp4 输出.mp4 [--band-y 383 --band-h 45] [--frames N]

输入布局：4×512×512 float32（ch0=mask±0.5，ch1..3=RGB[-1,1]）
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys
import time

import numpy as np
from PIL import Image
from scipy import ndimage

FFMPEG = "/usr/bin/ffmpeg-rkrga" if os.path.exists("/usr/bin/ffmpeg-rkrga") else "ffmpeg"
S = 512


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        print("失败:", cmd[:110], r.stderr[-300:])
        sys.exit(1)
    return r.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--model", default="/opt/py/migan512.rknn")
    ap.add_argument("--band-y", type=int, default=-1)
    ap.add_argument("--band-h", type=int, default=-1)
    ap.add_argument("--frames", type=int, default=0)
    ap.add_argument("--white", type=int, default=200)
    ap.add_argument("--dil", type=int, default=3)
    args = ap.parse_args()

    from rknnlite.api import RKNNLite
    rk = RKNNLite()
    if rk.load_rknn(args.model) != 0:
        sys.exit("加载模型失败")
    if rk.init_runtime() != 0:
        sys.exit("init_runtime 失败（内存不足？重启后再试）")
    print("NPU 就绪", flush=True)

    work = "/tmp/migan_work"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work + "/in", exist_ok=True)
    os.makedirs(work + "/out", exist_ok=True)
    n = f"-frames:v {args.frames}" if args.frames else ""
    sh(f'{FFMPEG} -hide_banner -loglevel error -y -i "{args.src}" {n} {work}/in/%05d.png')
    files = sorted(glob.glob(work + "/in/*.png"))
    fps = sh(f'ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate '
             f'-of default=nw=1:nk=1 "{args.src}"').strip()
    print(f"帧数 {len(files)} fps {fps}", flush=True)

    prog_path = os.environ.get("MIGAN_PROG", "")
    try:                                   # fps 形如 "25/1"
        _n, _d = fps.split("/")
        fps_v = float(_n) / float(_d or 1)
    except Exception:
        fps_v = 25.0
    total_us = 1000000.0 * (len(files) / max(1e-6, fps_v))   # 视频总时长（微秒）
    def write_prog(idx):
        if not prog_path:
            return
        try:
            with open(prog_path + ".tmp", "w") as fh:
                fh.write("out_time_us=%d\nprogress=%s\n"
                         % (int(total_us * idx / max(1, len(files))),
                            "end" if idx >= len(files) else "continue"))
            os.replace(prog_path + ".tmp", prog_path)
        except Exception:
            pass

    t_all = time.time()
    for i, f in enumerate(files):
        im = Image.open(f).convert("RGB")
        W, H = im.size
        small = np.array(im.resize((S, S), Image.BILINEAR), dtype=np.float32)
        g = np.array(im.resize((S, S), Image.BILINEAR).convert("L"))
        if args.band_y >= 0:
            hole = np.zeros((S, S), bool)
            y0 = int(args.band_y / H * S)
            y1 = int((args.band_y + args.band_h) / H * S)
            hole[y0:y1, :] = True
        else:
            hole = ndimage.binary_dilation(g >= args.white, iterations=args.dil)
        if hole.sum() < 20:
            Image.open(f).save(work + f"/out/{os.path.basename(f)}")
            continue

        rgb = small / 127.5 - 1.0
        rgb[hole] = 0.0                              # 洞填 0（灰）
        mask = (1.0 - hole.astype(np.float32)) - 0.5  # 1=保留 +0.5，0=洞 -0.5
        x = np.concatenate([mask[..., None], rgb], axis=2)[None].astype(np.float32)

        out = rk.inference(inputs=[x])
        o = np.asarray(out[0])
        if o.ndim == 4:
            o = o[0]
        if o.shape[0] == 3:
            o = o.transpose(1, 2, 0)          # NCHW → HWC
        o8 = np.clip((o + 1.0) * 127.5, 0, 255).astype(np.uint8)
        # 关键：回到原始分辨率再合成，只替换字幕像素；
        # 整帧缩放两次会把整个画面弄糊（之前用户看到的就是这个）
        o_full = np.array(Image.fromarray(o8).resize((W, H), Image.LANCZOS))
        hole_full = np.array(Image.fromarray((hole * 255).astype(np.uint8))
                             .resize((W, H), Image.NEAREST)) > 127
        res = np.array(im)
        res[hole_full] = o_full[hole_full]
        Image.fromarray(res).save(work + f"/out/{os.path.basename(f)}")
        write_prog(i + 1)
        if (i + 1) % 20 == 0 or i == 0:
            el = time.time() - t_all
            print(f"  {i+1}/{len(files)}  {el/(i+1):.2f}s/帧  剩余 {(len(files)-i-1)*el/(i+1)/60:.0f} 分钟",
                  flush=True)

    sh(f'{FFMPEG} -hide_banner -loglevel error -y -framerate {fps} -i {work}/out/%05d.png '
       f'-i "{args.src}" -map 0:v -map 1:a? -c:v h264_rkmpp -b:v 3M -c:a copy -shortest "{args.dst}"')
    print(f"完成 {args.dst}  {(time.time()-t_all)/60:.1f} 分钟", flush=True)


if __name__ == "__main__":
    main()
