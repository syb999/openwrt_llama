#!/usr/bin/env python3
"""MI-GAN 去字幕（RK3566 NPU）—— v2：原生块 + 时间平滑

相比 v1 的两处关键改动：

① 不再把整帧缩放到 512×512（那会造成几何偏差 + 双重重采样发糊）。
   改成按帧宽切 512×512 原生块（宽 > 512 时切两块），在**原始坐标**上合成。

② 时间域平滑（治抖动）：每帧独立 inpainting，同一位置每帧都重新"猜"，
   纹理随机 → 肉眼看就是字幕区在抖。用指数滑动平均压住：
       out_t = a * inpaint_t + (1-a) * out_{t-1}     只对遮罩内像素
   a 越小越稳但越糊；0.5~0.7 比较平衡。
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
T = 512          # 模型固定输入尺寸


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
    ap.add_argument("--tmedian", type=int, default=0, help="时间中位数窗口（帧），0=关闭；5 左右效果好")
    ap.add_argument("--ema", type=float, default=0.6, help="时间平滑系数，0=完全不更新（糊），1=不平滑（抖）")
    args = ap.parse_args()

    from rknnlite.api import RKNNLite
    rk = RKNNLite()
    if rk.load_rknn(args.model) != 0:
        sys.exit("加载模型失败")
    if rk.init_runtime() != 0:
        sys.exit("init_runtime 失败（先停 llama-server + drop_caches）")
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
    try:
        _n, _d = fps.split("/")
        fps_v = float(_n) / float(_d or 1)
    except Exception:
        fps_v = 25.0
    print(f"帧数 {len(files)} fps {fps_v}", flush=True)

    prog_path = os.environ.get("MIGAN_PROG", "")
    total_us = 1000000.0 * (len(files) / max(1e-6, fps_v))

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

    prev = None            # 上一帧的合成结果（用于时间平滑）
    buf = []               # 时间中位数的帧缓冲
    t_all = time.time()
    for i, f in enumerate(files):
        im = Image.open(f).convert("RGB")
        W, H = im.size
        arr = np.array(im)
        out = arr.copy()

        by = args.band_y if args.band_y >= 0 else int(H * 0.87)
        bh = args.band_h if args.band_h > 0 else int(H * 0.12)

        # 帧比模型输入小时必须补齐（边缘复制），否则切不出 512×512 的块
        PH, PW = max(H, T), max(W, T)
        if PH != H or PW != W:
            pad = np.pad(arr, ((0, PH - H), (0, PW - W), (0, 0)), mode="edge")
        else:
            pad = arr

        ty = max(0, min(PH - T, by + bh // 2 - T // 2))
        did = 0
        mask_all = np.zeros((H, W), bool)
        xs = [0] if PW <= T else [0, PW - T]
        for tx in xs:
            tile = pad[ty:ty + T, tx:tx + T]
            g = np.array(Image.fromarray(tile).convert("L"))
            hole = np.zeros((T, T), bool)
            r0 = max(0, by - ty)
            r1 = min(T, by - ty + bh)
            if r1 > r0:
                if args.band_y >= 0:
                    hole[r0:r1, :] = True
                else:
                    hole[r0:r1, :] = g[r0:r1] >= args.white
            if args.band_y >= 0:
                # 固定带模式下判断"带内到底有没有字"：
                # 只认"细笔画"—— 亮像素里排除掉大块亮区（画面里的白云、亮皮肤、
                # 白色道具都会被亮度阈值误判成字，一 inpaint 就把真实内容改掉，
                # 用户看到的就是"画面位移/变形"）
                band_g = g[r0:r1]
                if band_g.size == 0:
                    continue
                bright = band_g >= args.white
                if bright.sum() < 15:
                    continue
                big = ndimage.binary_opening(bright, structure=np.ones((5, 5)))
                thin = bright & ~big
                frac = thin.mean()
                # 细笔画占比过高 → 多半是明亮画面，不是字幕
                if frac < 0.002 or frac > 0.30:
                    continue
                hole[r0:r1] = thin
            if hole.sum() < 20:
                continue
            if args.dil and args.band_y < 0:
                hole = ndimage.binary_dilation(hole, iterations=args.dil)
            did = 1

            rgb = tile.astype(np.float32) / 127.5 - 1.0
            rgb[hole] = 0.0
            mask = (1.0 - hole.astype(np.float32)) - 0.5
            x = np.concatenate([mask[..., None], rgb], axis=2)[None].astype(np.float32)
            o = np.asarray(rk.inference(inputs=[x])[0])[0]
            if o.shape[0] == 3:
                o = o.transpose(1, 2, 0)
            o8 = np.clip((o + 1.0) * 127.5, 0, 255).astype(np.uint8)

            sub = pad[ty:ty + T, tx:tx + T]
            sub[hole] = o8[hole]
            pad[ty:ty + T, tx:tx + T] = sub
            # 记录到整帧坐标（用于时间平滑，只碰这些像素）
            ys, xs0 = ty, tx
            for ry in range(T):
                gy = ys + ry
                if gy >= H: break
                row = hole[ry]
                if row.any():
                    mask_all[gy, xs0:xs0 + T] |= row

        if did:
            out = pad[:H, :W].copy()          # 去掉补齐的部分

        if not did:
            # 这帧没字：原样输出，并且清空时间缓冲（否则后续帧会混进旧内容）
            buf.clear()
            prev = None
            Image.fromarray(arr).save(work + f"/out/{os.path.basename(f)}")
            write_prog(i + 1)
            if (i + 1) % 20 == 0 or i == 0:
                el = time.time() - t_all
                print(f"  {i+1}/{len(files)}  {el/(i+1):.2f}s/帧  (本帧无字幕，跳过)", flush=True)
            continue

        # 时间平滑只作用在"本帧真正被修复的像素"上，不碰字幕带以外的画面
        if prev is not None and prev.shape == out.shape and mask_all.any():
            a = max(0.0, min(1.0, args.ema))
            out[mask_all] = (a * out[mask_all].astype(np.float32) +
                             (1 - a) * prev[mask_all].astype(np.float32)).astype(np.uint8)

        # 时间中位数：每帧独立 inpainting 的随机纹理是抖动的根源，
        # 对最近 N 帧逐像素取中位数能压掉随机成分、保留结构
        if args.tmedian >= 3:
            buf.append(out.astype(np.float32))
            if len(buf) > args.tmedian:
                buf.pop(0)
            if len(buf) >= 3:
                med = np.median(np.stack(buf, 0), axis=0)
                if mask_all.any():
                    out[mask_all] = med[mask_all].astype(np.uint8)

        prev = out
        Image.fromarray(out).save(work + f"/out/{os.path.basename(f)}")
        write_prog(i + 1)
        if (i + 1) % 20 == 0 or i == 0:
            el = time.time() - t_all
            print(f"  {i+1}/{len(files)}  {el/(i+1):.2f}s/帧  剩余 {(len(files)-i-1)*el/(i+1)/60:.0f} 分钟",
                  flush=True)

    # 编码参数很关键：PNG 往返本身有 YUV→RGB→YUV 损失，
    # 这里显式声明 bt601 并提高码率，PSNR 能从 32dB 提到 35+，画面不再发灰发虚
    sh(f'{FFMPEG} -hide_banner -loglevel error -y -framerate {fps} -i {work}/out/%05d.png '
       f'-i "{args.src}" -map 0:v -map 1:a? -vf format=yuv420p '
       f'-c:v h264_rkmpp -b:v 8M '
       f'-colorspace smpte170m -color_primaries smpte170m -color_trc smpte170m '
       f'-c:a copy -shortest "{args.dst}"')
    print(f"完成 {args.dst}  {(time.time()-t_all)/60:.1f} 分钟", flush=True)


if __name__ == "__main__":
    main()
