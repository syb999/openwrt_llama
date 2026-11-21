#!/usr/bin/env python3
"""MI-GAN 去字幕 v3 —— 全程 YUV 原生，不经过 PNG

为什么重写：
  v2 走的是 视频→PNG(RGB)→处理→PNG→视频，YUV→RGB→YUV 往返两次，
  PSNR 从 52dB 掉到 32~35dB，画面看着发灰、边缘发虚，用户会以为是"位移/变形"。
  v3 直接读写 yuv420p 裸流，只有字幕带内的像素做 RGB 换算，其余像素原样搬运。

另外保留 v2 的三个修正：
  ① 只认"细笔画"（亮像素里排除大块亮区），避免把明亮画面当字幕 inpaint
  ② 无字幕的帧完全跳过
  ③ 时间中位数 + EMA 压抖动，且只作用于被修复的像素
"""
import argparse
import os
import subprocess
import sys
import time

import numpy as np
from scipy import ndimage

FFRGA = "/usr/bin/ffmpeg-rkrga" if os.path.exists("/usr/bin/ffmpeg-rkrga") else "ffmpeg"
FFSV = "/usr/bin/ffmpeg"
T = 512


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        print("失败:", cmd[:110], r.stderr[-300:])
        sys.exit(1)
    return r.stdout


PROBE = "/usr/bin/ffprobe" if os.path.exists("/usr/bin/ffprobe") else FFV

def probe(path, key):
    return sh(f'{PROBE} -v error -select_streams v:0 -show_entries stream={key} '
              f'-of default=nw=1:nk=1 "{path}"').strip()


def yuv2rgb(Y, U, V, w, h, ys, cs, x0, y0, bw, bh):
    """只转换需要的一块：返回 (bh,bw,3) uint8"""
    out = np.zeros((bh, bw, 3), np.uint8)
    for j in range(bh):
        gy = y0 + j
        row_y = Y[gy, x0:x0 + bw].astype(np.int16)
        cu = U[gy // 2, x0 // 2:(x0 + bw) // 2].astype(np.int16) - 128
        cv = V[gy // 2, x0 // 2:(x0 + bw) // 2].astype(np.int16) - 128
        cu = np.repeat(cu, 2)[:bw]
        cv = np.repeat(cv, 2)[:bw]
        r = row_y + 1.402 * cv
        g = row_y - 0.344136 * cu - 0.714136 * cv
        b = row_y + 1.772 * cu
        out[j, :, 0] = np.clip(r, 0, 255)
        out[j, :, 1] = np.clip(g, 0, 255)
        out[j, :, 2] = np.clip(b, 0, 255)
    return out


def rgb2yuv_into(rgb, Y, U, V, w, x0, y0, cs):
    """把 rgb (h,w,3) 写回 Y/U/V 的对应位置（只写这些像素）"""
    h, bw = rgb.shape[:2]
    r = rgb[:, :, 0].astype(np.float32)
    g = rgb[:, :, 1].astype(np.float32)
    b = rgb[:, :, 2].astype(np.float32)
    yy = np.clip(0.299 * r + 0.587 * g + 0.114 * b, 0, 255)
    Y[y0:y0 + h, x0:x0 + bw] = yy.astype(np.uint8)
    uu = np.clip(-0.168736 * r - 0.331264 * g + 0.5 * b + 128, 0, 255)
    vv = np.clip(0.5 * r - 0.418688 * g - 0.081312 * b + 128, 0, 255)
    # 4:2:0，取 2x2 平均
    uu2 = uu.reshape(h // 2, 2, bw // 2, 2).mean(axis=(1, 3)) if h % 2 == 0 and bw % 2 == 0 else None
    vv2 = vv.reshape(h // 2, 2, bw // 2, 2).mean(axis=(1, 3)) if h % 2 == 0 and bw % 2 == 0 else None
    if uu2 is not None:
        U[y0 // 2:(y0 + h) // 2, x0 // 2:(x0 + bw) // 2] = uu2.astype(np.uint8)
        V[y0 // 2:(y0 + h) // 2, x0 // 2:(x0 + bw) // 2] = vv2.astype(np.uint8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--model", default="/opt/py/migan512.rknn")
    ap.add_argument("--band-y", type=int, default=-1)
    ap.add_argument("--band-h", type=int, default=-1)
    ap.add_argument("--white", type=int, default=200)
    ap.add_argument("--dil", type=int, default=3)
    ap.add_argument("--ema", type=float, default=0.75)
    ap.add_argument("--tmedian", type=int, default=3)
    args = ap.parse_args()

    from rknnlite.api import RKNNLite
    rk = RKNNLite()
    if rk.load_rknn(args.model) != 0:
        sys.exit("加载模型失败")
    if rk.init_runtime() != 0:
        sys.exit("init_runtime 失败（先停 llama-server + drop_caches / 重启）")
    print("NPU 就绪", flush=True)

    W = int(probe(args.src, "width"))
    H = int(probe(args.src, "height"))
    fps_s = probe(args.src, "r_frame_rate")
    try:
        _n, _d = fps_s.split("/")
        fps = float(_n) / float(_d or 1)
    except Exception:
        fps = 25.0
    nframe = int(sh(f'ffprobe -v error -count_frames -select_streams v:0 '
                    f'-show_entries stream=nb_read_frames -of default=nw=1:nk=1 "{args.src}"').strip() or 0)
    ysz, csz = W * H, (W // 2) * (H // 2)
    fsz = ysz + 2 * csz
    print(f"{W}x{H} {fps}fps {nframe} 帧", flush=True)

    by = args.band_y if args.band_y >= 0 else int(H * 0.88)
    bh = args.band_h if args.band_h > 0 else int(H * 0.10)

    dec = subprocess.Popen(f'{FFSV} -hide_banner -loglevel error -i "{args.src}" '
                           f'-f rawvideo -pix_fmt yuv420p -', shell=True,
                           stdout=subprocess.PIPE, bufsize=fsz)
    # 编码用 stock ffmpeg + libx264：YUV 进 YUV 出，只有字幕区做过 RGB 换算，
    # 其余像素逐字节原样，PSNR 接近无损（rkmp 管道版在这台机器上喂不进去）
    enc = subprocess.Popen(f'{FFSV} -hide_banner -loglevel error -y -f rawvideo '
                           f'-pix_fmt yuv420p -s {W}x{H} -framerate {fps} -i pipe:0 '
                           f'-i "{args.src}" -map 0:v -map 1:a? '
                           f'-c:v libx264 -preset veryfast -crf 18 '
                           f'-pix_fmt yuv420p -c:a copy -shortest "{args.dst}"',
                           shell=True, stdin=subprocess.PIPE)

    prev = None
    buf = []
    t_all = time.time()
    idx = 0
    while True:
        raw = dec.stdout.read(fsz)
        if not raw or len(raw) < fsz:
            break
        fr = np.frombuffer(raw, np.uint8).copy()
        Y = fr[:ysz].reshape(H, W)
        U = fr[ysz:ysz + csz].reshape(H // 2, W // 2)
        V = fr[ysz + csz:].reshape(H // 2, W // 2)

        # 只取字幕带（含左右边界留白）判断有没有字
        y0 = max(0, by - 2)
        y1 = min(H, by + bh + 2)
        bandY = Y[y0:y1, :]
        bright = bandY >= args.white
        did = 0
        mask_all = np.zeros((H, W), bool)
        if bright.sum() >= 15:
            big = ndimage.binary_opening(bright, structure=np.ones((5, 5)))
            thin = bright & ~big
            frac = thin.mean()
            if 0.002 <= frac <= 0.30:
                did = 1
                hole_band = ndimage.binary_dilation(thin, iterations=args.dil) if args.dil else thin

        if did:
            # 按 512 宽切块（帧宽 > 512 时两块），块高不足 512 用边缘补齐
            ty = max(0, min(max(0, H - T), by + bh // 2 - T // 2))
            xs = [0] if W <= T else [0, W - T]
            for tx in xs:
                bw_ = min(T, W - tx)
                bh_ = min(T, H - ty)
                tile = yuv2rgb(Y, U, V, W, H, W, W // 2, tx, ty, bw_, bh_)
                pad = np.pad(tile, ((0, T - bh_), (0, T - bw_), (0, 0)), mode="edge")
                g = (0.299 * pad[:, :, 0] + 0.587 * pad[:, :, 1] + 0.114 * pad[:, :, 2])
                hole = np.zeros((T, T), bool)
                ry0 = max(0, y0 - ty)
                ry1 = min(T, y1 - ty)
                hole[ry0:ry1, :bw_] = hole_band[max(0, ty - y0):max(0, ty - y0) + (ry1 - ry0), :bw_]
                if hole.sum() < 20:
                    continue
                rgb = pad.astype(np.float32) / 127.5 - 1.0
                rgb[hole] = 0.0
                mask = (1.0 - hole.astype(np.float32)) - 0.5
                x = np.concatenate([mask[..., None], rgb], axis=2)[None].astype(np.float32)
                o = np.asarray(rk.inference(inputs=[x])[0])[0]
                if o.shape[0] == 3:
                    o = o.transpose(1, 2, 0)
                o8 = np.clip((o + 1.0) * 127.5, 0, 255).astype(np.uint8)

                # 写回：只写 hole 覆盖的像素（在原始坐标上，不缩放画面）
                sub = o8[:bh_, :bw_]
                hm = hole[:bh_, :bw_]
                base = tile.copy()
                base[hm] = sub[hm]
                rgb2yuv_into(base, Y, U, V, W, tx, ty, W // 2)
                mask_all[ty:ty + bh_, tx:tx + bw_] |= hm

            # 时间平滑：只碰被修复的像素
            cur = np.stack([Y, U, V])
            if args.tmedian >= 3:
                buf.append(cur.copy())
                if len(buf) > args.tmedian:
                    buf.pop(0)
                if len(buf) >= 3 and mask_all.any():
                    med = np.median(np.stack(buf, 0), axis=0).astype(np.uint8)
                    Y[mask_all] = med[0][mask_all]
            if prev is not None and mask_all.any():
                a = max(0.0, min(1.0, args.ema))
                Y[mask_all] = (a * Y[mask_all].astype(np.float32) +
                               (1 - a) * prev[mask_all].astype(np.float32)).astype(np.uint8)
            prev = Y.copy()
        else:
            buf.clear()
            prev = None

        enc.stdin.write(Y.tobytes() + U.tobytes() + V.tobytes())
        idx += 1
        if idx % 50 == 0:
            el = time.time() - t_all
            eta = el / idx * (nframe - idx) / 60 if nframe else 0
            print(f"  {idx}/{nframe}  {el/idx:.2f}s/帧  剩余 {eta:.0f} 分钟", flush=True)

    enc.stdin.close()
    dec.wait()
    enc.wait()
    print(f"完成 {args.dst}  {(time.time()-t_all)/60:.1f} 分钟", flush=True)


if __name__ == "__main__":
    main()
