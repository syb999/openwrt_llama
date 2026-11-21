#!/usr/bin/env python3
"""MI-GAN 去字幕 v4 —— 字幕不变则复用同一块修复结果（治抖 + 提速）

为什么要这么做：
  逐帧独立 inpainting 时，同一位置每帧都重新"猜"，纹理随机 → 肉眼看字幕区在抖。
  而实际素材里，一句台词往往停 1~3 秒（25~75 帧），这几帧的背景几乎不变。
  所以：文本没变 + 背景没变 → 直接复用上一帧修好的那块，一个像素都不重算。

  好处有两层：
    ① 抖动消失（同一句台词期间画面完全静止）
    ② 提速（同一句台词只算 1 帧，实测能省 50~70% 的推理）

判定规则（三项都满足才复用）：
  · 本帧文字遮罩 与 上一帧遮罩 的差异 < mask_tol（默认 3%）
  · 本帧背景（带内非文字区）与上一帧的平均差 < bg_tol（默认 6）
  · 上一帧确实修过（有 patch 可用）

其余情况照常跑 NPU，并把结果存成新 patch。

用法：
  ./migan4.sh 输入.mp4 输出.mp4 --band-y 383 --band-h 49 [--mask-tol 0.03 --bg-tol 6]
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

import numpy as np
from PIL import Image
from scipy import ndimage

FFMPEG = "/usr/bin/ffmpeg-rkrga" if os.path.exists("/usr/bin/ffmpeg-rkrga") else "ffmpeg"
PROBE = "/usr/bin/ffprobe" if os.path.exists("/usr/bin/ffprobe") else FFMPEG
T = 512


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        print("失败:", cmd[:110], r.stderr[-300:])
        sys.exit(1)
    return r.stdout


def probe(path, key):
    return sh(f'{PROBE} -v error -select_streams v:0 -show_entries stream={key} '
              f'-of default=nw=1:nk=1 "{path}"').strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--model", default="/opt/py/migan512.rknn")
    ap.add_argument("--band-y", type=int, default=-1)
    ap.add_argument("--band-h", type=int, default=-1)
    ap.add_argument("--white", type=int, default=200)
    ap.add_argument("--dil", type=int, default=3)
    ap.add_argument("--mask-tol", type=float, default=0.03, help="遮罩变化超过这个比例就重算")
    ap.add_argument("--bg-tol", type=float, default=6.0, help="背景平均差超过这个就重算")
    args = ap.parse_args()

    from rknnlite.api import RKNNLite
    rk = RKNNLite()
    if rk.load_rknn(args.model) != 0:
        sys.exit("加载模型失败")
    if rk.init_runtime() != 0:
        sys.exit("init_runtime 失败（先停 llama-server + drop_caches）")
    print("NPU 就绪", flush=True)

    work = "/tmp/migan_work4"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work + "/in", exist_ok=True)
    os.makedirs(work + "/out", exist_ok=True)
    sh(f'{FFMPEG} -hide_banner -loglevel error -y -i "{args.src}" {work}/in/%05d.png')
    files = sorted(os.listdir(work + "/in"))
    fps_s = probe(args.src, "r_frame_rate")
    try:
        _n, _d = fps_s.split("/")
        fps = float(_n) / float(_d or 1)
    except Exception:
        fps = 25.0
    print(f"帧数 {len(files)} fps {fps}", flush=True)

    prog_path = os.environ.get("MIGAN_PROG", "")
    total_us = 1000000.0 * (len(files) / max(1e-6, fps))

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

    last_mask = None      # 上一帧的文字遮罩（带内，原生分辨率）
    last_patch = None     # 上一帧修好的带内 RGB（原生分辨率）
    last_bg = None        # 上一帧带内非文字区（判定背景有没有变）
    reused = 0
    computed = 0
    t_all = time.time()

    for i, fn in enumerate(files):
        path_in = os.path.join(work + "/in", fn)
        im = Image.open(path_in).convert("RGB")
        W, H = im.size
        arr = np.array(im)
        out = arr.copy()

        by = args.band_y if args.band_y >= 0 else int(H * 0.88)
        bh = args.band_h if args.band_h > 0 else int(H * 0.10)
        y0, y1 = max(0, by), min(H, by + bh)
        band = arr[y0:y1]
        g = np.array(Image.fromarray(band).convert("L"))

        # —— 文字检测：只认细笔画 ——
        bright = g >= args.white
        mask = np.zeros_like(bright)
        if bright.sum() >= 15:
            big = ndimage.binary_opening(bright, structure=np.ones((5, 5)))
            thin = bright & ~big
            frac = thin.mean()
            if 0.002 <= frac <= 0.30:
                mask = ndimage.binary_dilation(thin, iterations=args.dil) if args.dil else thin

        if not mask.any():
            # 这帧没字幕：原样输出，清空复用状态
            last_mask = last_patch = last_bg = None
            Image.fromarray(arr).save(work + f"/out/{fn}")
            write_prog(i + 1)
            continue

        bg = band[~mask].astype(np.int16)
        can_reuse = (last_patch is not None
                     and last_mask is not None
                     and mask.shape == last_mask.shape
                     and last_patch.shape == band.shape)
        if can_reuse:
            dm = np.logical_xor(mask, last_mask).mean()
            db = np.abs(bg - last_bg).mean() if last_bg is not None and bg.shape == last_bg.shape else 999
            if dm > args.mask_tol or db > args.bg_tol:
                can_reuse = False

        if can_reuse:
            # ★ 直接复用上一帧修好的那块：字幕区完全静止 → 不抖，也不花算力
            out[y0:y1] = last_patch
            reused += 1
        else:
            # 跑 NPU：512×512 原生块（帧高不足则边缘补齐），只回填文字像素
            PH, PW = max(H, T), max(W, T)
            pad = np.pad(arr, ((0, PH - H), (0, PW - W), (0, 0)), mode="edge") if (PH != H or PW != W) else arr
            ty = max(0, min(PH - T, (y0 + y1) // 2 - T // 2))
            xs = [0] if PW <= T else [0, PW - T]
            new_band = band.copy()
            for tx in xs:
                tile = pad[ty:ty + T, tx:tx + T]
                if tile.shape[0] < T or tile.shape[1] < T:
                    continue
                ty0 = max(0, y0 - ty)
                ty1 = min(T, y1 - ty)
                hm = np.zeros((T, T), bool)
                hm[ty0:ty1, :min(T, W - tx)] = mask[ty0 - (y0 - ty):ty0 - (y0 - ty) + (ty1 - ty0),
                                                    tx:tx + min(T, W - tx)]
                if hm.sum() < 20:
                    continue
                rgb = tile.astype(np.float32) / 127.5 - 1.0
                rgb[hm] = 0.0
                mch = (1.0 - hm.astype(np.float32)) - 0.5
                x = np.concatenate([mch[..., None], rgb], axis=2)[None].astype(np.float32)
                o = np.asarray(rk.inference(inputs=[x])[0])[0]
                if o.shape[0] == 3:
                    o = o.transpose(1, 2, 0)
                o8 = np.clip((o + 1.0) * 127.5, 0, 255).astype(np.uint8)
                # 回填到整帧坐标
                gy0, gy1 = ty + ty0, ty + ty1
                gx0, gx1 = tx, tx + min(T, W - tx)
                sub = out[gy0:gy1, gx0:gx1]
                hm2 = hm[ty0:ty1, :gx1 - gx0]
                sub[hm2] = o8[ty0:ty1, :gx1 - gx0][hm2]
                out[gy0:gy1, gx0:gx1] = sub
            new_band = out[y0:y1]
            last_patch = new_band.copy()
            last_mask = mask.copy()
            last_bg = bg.copy()
            computed += 1

        Image.fromarray(out).save(work + f"/out/{fn}")
        write_prog(i + 1)
        if (i + 1) % 25 == 0 or i == 0:
            el = time.time() - t_all
            eta = el / (i + 1) * (len(files) - i - 1) / 60
            print(f"  {i+1}/{len(files)}  {el/(i+1):.2f}s/帧  剩余 {eta:.0f} 分钟  "
                  f"(复用 {reused} / 重算 {computed})", flush=True)

    sh(f'{FFMPEG} -hide_banner -loglevel error -y -framerate {fps} -i {work}/out/%05d.png '
       f'-i "{args.src}" -map 0:v -map 1:a? -vf format=yuv420p '
       f'-c:v h264_rkmpp -b:v 6M '
       f'-colorspace smpte170m -color_primaries smpte170m -color_trc smpte170m '
       f'-c:a copy -shortest "{args.dst}"')
    print(f"完成 {args.dst}  {(time.time()-t_all)/60:.1f} 分钟  "
          f"复用 {reused} 帧 / 重算 {computed} 帧", flush=True)


if __name__ == "__main__":
    main()
