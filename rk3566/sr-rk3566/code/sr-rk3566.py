#!/usr/bin/env python3
"""sr-rk3566.py —— 视频画面优化（2 倍超分，Real-ESRGAN animevideov3 on RKNPU）

用法: sr-rk3566.py <输入.mp4> <输出.mp4>

自动按分辨率选模型：
    宽高比接近 4:3  → av3_res.rknn     切块 288x216
    宽高比接近 16:9 → av3_320x180.rknn 切块 320x180
源片高于 720p 直接拒绝（已经是高清，跑超分只会更慢）。
切块全部是原图裁剪，不缩放；每块 4 倍，拼完缩 2 倍得到 2x 输出。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

DIR = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(DIR, 'anime4k_rknn')
LOADER = '/opt/glibc/ld-linux-aarch64.so.1'
LIBPATH = '/opt/glibc:/opt/rknpu'
FF = '/usr/bin/ffmpeg-rkrga' if os.path.exists('/usr/bin/ffmpeg-rkrga') else 'ffmpeg'

MODELS = [
    ('4:3', 4.0 / 3.0, os.path.join(DIR, 'av3_res.rknn'), 288, 216),
    ('16:9', 16.0 / 9.0, os.path.join(DIR, 'av3_320x180.rknn'), 320, 180),
]


def die(msg):
    sys.stderr.write('error: %s\n' % msg)
    sys.exit(1)


def probe(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                        '-show_entries', 'stream=width,height,r_frame_rate,sample_aspect_ratio,display_aspect_ratio',
                        '-of', 'json', path], capture_output=True, text=True)
    try:
        st = json.loads(r.stdout)['streams'][0]
    except Exception:
        die('ffprobe 失败: %s' % path)
    w, h = int(st['width']), int(st['height'])
    num, den = st.get('r_frame_rate', '25/1').split('/')
    fps = float(num) / float(den or 1)
    if fps < 1 or fps > 240:
        fps = 25.0
    dar = 0.0
    d = st.get('display_aspect_ratio', '') or ''
    if ':' in d:
        a, b = d.split(':')
        try:
            dar = float(a) / float(b)
        except Exception:
            dar = 0.0
    if dar <= 0:
        sar = st.get('sample_aspect_ratio', '') or ''
        if ':' in sar and sar != '0:1':
            a, b = sar.split(':')
            try:
                dar = w / float(h) * float(a) / float(b)
            except Exception:
                dar = 0.0
    if dar <= 0:
        dar = w / float(h)
    return w, h, fps, dar


def pick_model(w, h):
    ar = w / float(h)
    best = None
    for name, tar, path, tw, th in MODELS:
        nx = (w + tw - 1) // tw
        ny = (h + th - 1) // th
        waste = (nx * tw * ny * th - w * h) / float(w * h)
        score = abs(ar - tar) * 2.0 + waste
        if best is None or score < best[0]:
            best = (score, name, path, tw, th, nx, ny, waste)
    return best


def main():
    if len(sys.argv) < 3:
        die('用法: sr-rk3566.py <输入.mp4> <输出.mp4>')
    src, dst = sys.argv[1], sys.argv[2]
    if not os.path.exists(src):
        die('文件不存在: %s' % src)

    w, h, fps, dar = probe(src)
    if max(w, h) > 1280 and min(w, h) > 720:
        die('源片 %dx%d 已经是高清，不需要跑超分' % (w, h))
    score, mname, model, tw, th, nx, ny, waste = pick_model(w, h)
    if not os.path.exists(model):
        die('模型缺失: %s' % model)
    sys.stderr.write('源 %dx%d %.3ffps DAR=%.3f → 模型 %s（块 %dx%d，%dx%d=%d 块，补齐 %.1f%%）\n'
                     % (w, h, fps, dar, mname, tw, th, nx, ny, nx * ny, waste * 100))

    tmp = tempfile.mkdtemp(prefix='sr_', dir='/opt/llama/out' if os.path.isdir('/opt/llama/out') else '/tmp')
    raw_in = os.path.join(tmp, 'in.rgb')
    raw_out = os.path.join(tmp, 'out.rgb')
    env = dict(os.environ, LD_LIBRARY_PATH=LIBPATH)
    fsz = w * h * 3

    try:
        sys.stderr.write('解码...\n')
        subprocess.run([FF, '-y', '-v', 'error', '-i', src, '-f', 'rawvideo',
                        '-pix_fmt', 'rgb24', raw_in], check=True)

        tw_out, th_out = w * 2, h * 2
        fin = open(raw_in, 'rb')
        fout = open(raw_out, 'wb')
        n = 0
        # 每帧切块，块间不重叠；不足的用边缘复制补齐，最后裁掉
        while True:
            buf = fin.read(fsz)
            if len(buf) < fsz:
                break
            img = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 3)
            out = np.zeros((th_out, tw_out, 3), np.uint8)
            for iy in range(ny):
                for ix in range(nx):
                    y0, x0 = iy * th, ix * tw
                    y1, x1 = min(y0 + th, h), min(x0 + tw, w)
                    tile = img[y0:y1, x0:x1]
                    if tile.shape[0] < th or tile.shape[1] < tw:
                        tile = np.pad(tile, ((0, th - tile.shape[0]),
                                             (0, tw - tile.shape[1]), (0, 0)), mode='edge')
                    np.ascontiguousarray(tile).tofile(os.path.join(tmp, 't.rgb'))
                    r = subprocess.run([LOADER, BIN, model, os.path.join(tmp, 't.rgb'),
                                        os.path.join(tmp, 'o.rgb'), str(tw), str(th)],
                                       env=env, capture_output=True)
                    if r.returncode != 0:
                        die('推理失败（块 %d,%d）：%s' % (iy, ix, r.stderr[-200:]))
                    o = np.fromfile(os.path.join(tmp, 'o.rgb'), dtype=np.uint8).reshape(th * 4, tw * 4, 3)
                    half = o.reshape(th * 2, 2, tw * 2, 2, 3).mean(axis=(1, 3))
                    thh, tww = (y1 - y0) * 2, (x1 - x0) * 2
                    out[y0 * 2:y0 * 2 + thh, x0 * 2:x0 * 2 + tww] = \
                        np.clip(half[:thh, :tww] + 0.5, 0, 255).astype(np.uint8)
            fout.write(out.tobytes())
            n += 1
            if n % 25 == 0:
                sys.stderr.write('  %d frames\n' % n)
        fin.close()
        fout.close()
        if n == 0:
            die('没读到帧')

        sys.stderr.write('编码...\n')
        # 音频从原片流复制；-aspect 必须写，否则 SAR 为空、播放器按 16:9 拉伸
        subprocess.run([FF, '-y', '-v', 'error',
                        '-f', 'rawvideo', '-pix_fmt', 'rgb24',
                        '-s', '%dx%d' % (tw_out, th_out), '-r', '%.6f' % fps,
                        '-i', raw_out, '-i', src,
                        '-map', '0:v:0', '-map', '1:a?',
                        '-aspect', ('%d:%d' % (round(dar * 1000), 1000)),
                        '-c:v', 'h264_rkmpp', '-pix_fmt', 'nv12', '-b:v', '20M',
                        '-c:a', 'copy', '-shortest', dst], check=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    sz = os.path.getsize(dst) if os.path.exists(dst) else 0
    sys.stderr.write('done: %d frames -> %s (%.1f MB, %dx%d, 模型 %s)\n'
                     % (n, dst, sz / 1048576.0, tw_out, th_out, mname))


main()
