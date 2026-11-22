#!/usr/bin/env python3
"""把 realesr-animevideov3 导出成【任意输入尺寸】的 RKNN（fp16），给 sr-rk3566 用。

为什么需要这个：
    切块超分时，块越大 → 块数越少 → 推理面积冗余越少（越快），接缝也越少。
    模型输入尺寸是 RKNN 静态写死的，换尺寸必须重新转换（dynamic_input 在 RK3566 不可用）。

    288x216（现有）：576x432 的片子要 3x3=9 块（每块丢 32px 边缘），实推面积 2.25 倍
    576x432（本脚本）：**1 块**，零接缝，实推面积 1.0 倍  → 约 2.2 倍加速

用法（在有 AVX 的 x86_64 Linux 上）：
    pip install torch onnx onnxruntime rknn-toolkit2==2.3.2

    # 576x432（4:3 SD 整帧，推荐）
    python3 convert_bigtile.py --width 576 --height 432 \
        --ckpt resr_animevideov3.pth -o av3_576x432.rknn

    # 其它尺寸（4:3 / 16:9 各按源片挑）
    python3 convert_bigtile.py --width 640 --height 480 ...
    python3 convert_bigtile.py --width 640 --height 360 ...

    参数含义：--width / --height 是**模型输入尺寸**（源像素），输出是它的 4 倍。

产物用法（板上）：
    cp av3_576x432.rknn root@板:/opt/sr-rk3566/
    # 脚本的 MODELS 里加一行，或在命令前指定：
    SR_MODEL=/opt/sr-rk3566/av3_576x432.rknn python3 /opt/sr-rk3566/sr-rk3566.py in.mp4 out.mp4

耗时参考：这台 288x216 的模型在无 AVX 的机器上（qemu 模拟）约 50 分钟；
          有 AVX 的机器快 20 倍左右，576x432（约 4 倍算力）预计 3~10 分钟。
"""
import argparse
import os
import re
import sys


# ----------------------------------------------------------------------------
# 网络结构（从 checkpoint 实测反推；和 export_av3_res.py 保持一致）
#   x   = input * (1/255)                    # fp16 RKNN 不套 mean/std，归一化写进图里
#   net = PReLU 链 x17 + conv(64->48) + pixel_shuffle(4)
#   up  = DepthToSpace(Conv1x1(x 复制 16 份), 4)   # 等价最近邻 4x
#   out = net + up                           # 权重是残差式，必须加，否则一片黑
# 注意：残差分支要单独走 1x1 卷积 + DepthToSpace，不能用 Resize ——
#       Resize 会让 rknn-toolkit2 的 fold_constant 死循环。
# ----------------------------------------------------------------------------
def build_net(torch, nn, F):
    class DupUp(nn.Module):
        def __init__(self, c, up=4):
            super().__init__()
            self.conv = nn.Conv2d(c, c * up * up, 1, bias=False)
            w = torch.zeros(c * up * up, c, 1, 1)
            for i in range(c):
                for k in range(up * up):
                    w[i * up * up + k, i, 0, 0] = 1.0
            with torch.no_grad():
                self.conv.weight.copy_(w)
            self.conv.weight.requires_grad = False
            self.up = up

        def forward(self, x):
            return F.pixel_shuffle(self.conv(x), self.up)

    class Net(nn.Module):
        def __init__(self, sd):
            super().__init__()
            idx = sorted({int(re.match(r'body\.(\d+)\.', k).group(1)) for k in sd})
            mods = []
            for n in idx:
                w = sd['body.%d.weight' % n]
                if w.dim() == 4:
                    mods.append(nn.Conv2d(w.shape[1], w.shape[0],
                                          (w.shape[2], w.shape[3]), 1, 1))
                else:
                    mods.append(nn.PReLU(num_parameters=len(w)))
            self.body = nn.ModuleList(mods)
            self.idx = idx
            self.up = DupUp(3, 4)

        def forward(self, x):
            x = x * (1.0 / 255.0)
            y = x
            for m in self.body:
                y = m(y)
            y = F.pixel_shuffle(y, 4)
            return y + self.up(x)

    return Net


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--width', type=int, required=True, help='模型输入宽（源像素）')
    ap.add_argument('--height', type=int, required=True, help='模型输入高（源像素）')
    ap.add_argument('--ckpt', default='resr_animevideov3.pth', help='realesr-animevideov3 权重')
    ap.add_argument('-o', '--out', default=None, help='输出 .rknn 路径')
    ap.add_argument('--onnx', default=None, help='中间 ONNX 路径（默认同目录）')
    ap.add_argument('--keep-onnx', action='store_true', help='保留 ONNX')
    ap.add_argument('--target', default='rk3566', help='rknn target_platform')
    ap.add_argument('--opt', type=int, default=2, help='optimization_level')
    a = ap.parse_args()

    out = a.out or 'av3_%dx%d.rknn' % (a.width, a.height)
    onnx_path = a.onnx or os.path.splitext(out)[0] + '.onnx'

    if not os.path.exists(a.ckpt):
        sys.exit('找不到权重 %s（先从 realesr-animevideov3 官方 release 取 .pth）' % a.ckpt)

    # ---------------- 1. 导出 ONNX ----------------
    print('=== [1/3] 导出 ONNX %dx%d ===' % (a.width, a.height))
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    d = torch.load(a.ckpt, map_location='cpu')
    sd = d['params'] if isinstance(d, dict) and 'params' in d else d
    m = build_net(torch, nn, F)(sd).eval()
    for mod, n in zip(m.body, m.idx):
        if isinstance(mod, nn.Conv2d):
            mod.weight.data = sd['body.%d.weight' % n]
            mod.bias.data = sd['body.%d.bias' % n]
        else:
            mod.weight.data = sd['body.%d.weight' % n]

    x = torch.randn(1, 3, a.height, a.width) * 255.0
    with torch.no_grad():
        y = m(x)
    print('  形状 %s -> %s  均值 %.4f（残差式，约 0.5 才正常）'
          % (tuple(x.shape), tuple(y.shape), float(y.mean())))
    torch.onnx.export(m, x, onnx_path, opset_version=13,
                      input_names=['input'], output_names=['output'],
                      do_constant_folding=True, dynamo=False)
    print('  ->', onnx_path, os.path.getsize(onnx_path), 'bytes')

    # ---------------- 2. ONNX -> RKNN (fp16) ----------------
    print('=== [2/3] 转 RKNN（fp16, target=%s）===' % a.target)
    import onnx
    import onnx.helper as _oh
    from onnx import TensorProto as _TP
    if not hasattr(onnx, 'mapping'):           # rknn-toolkit2 依赖老的 onnx.mapping
        _t2np = {}
        for _n, _v in _TP.DataType.items():
            try:
                _t2np[_v] = _oh.tensor_dtype_to_np_dtype(_v)
            except Exception:
                pass

        class _M:
            TENSOR_TYPE_TO_NP_TYPE = _t2np
            NP_TYPE_TO_TENSOR_TYPE = {v: k for k, v in _t2np.items()}

        onnx.mapping = _M

    from rknn.api import RKNN
    rk = RKNN(verbose=True)
    print('  config:', rk.config(target_platform=a.target,
                                mean_values=[[0, 0, 0]],
                                std_values=[[1, 1, 1]],
                                optimization_level=a.opt))
    print('  load_onnx:', rk.load_onnx(model=onnx_path))
    print('  build:', rk.build(do_quantization=False))   # fp16
    print('  export:', rk.export_rknn(out))
    rk.release()
    print('  ->', out, os.path.getsize(out), 'bytes')

    if not a.keep_onnx:
        os.remove(onnx_path)

    # ---------------- 3. 完成 ----------------
    print('=== [3/3] 完成 ===')
    print('''
下一步：
  1) 传到板子：scp %s root@<板IP>:/opt/sr-rk3566/
  2) 直接试：  SR_MODEL=/opt/sr-rk3566/%s python3 /opt/sr-rk3566/sr-rk3566.py in.mp4 out.mp4
  3) 长期用：把它写进 sr-rk3566.py 的 MODELS：
        ('4:3', 4.0/3.0, os.path.join(DIR, '%s'), %d, %d),
     位置放在 288x216 那条**前面**，同尺寸的源片就会优先选它（waste=0）。

提醒：模型输入是 %dx%d，源片更大的话脚本会自动按块切（每块仍丢 32px 边缘），
      小于等于 %dx%d 的源片则只有 1 块 —— 零接缝、速度也是最快的。
''' % (out, os.path.basename(out), os.path.basename(out), a.width, a.height,
       a.width, a.height, a.width, a.height))


if __name__ == '__main__':
    main()
