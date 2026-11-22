#!/usr/bin/env python3
"""realesr-animevideov3 -> ONNX（含 1/255 归一化 + 残差上采样）

结构（从 checkpoint 实测反推）：
    x  = input * (1/255)                      # fp16 RKNN 不套 mean/std，写进图里
    net= PReLU 链 × 17 + conv(64->48) + pixel_shuffle(4)
    up = DepthToSpace(Conv1x1(x 复制 16 份), 4)   # 等价最近邻 4x
    out= net + up                              # 这个权重是残差模型，必须加
"""
import re
import sys

import torch
import torch.nn as nn
import torch.nn.functional as F


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
                mods.append(nn.Conv2d(w.shape[1], w.shape[0], (w.shape[2], w.shape[3]), 1, 1))
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


out = sys.argv[1] if len(sys.argv) > 1 else '/tmp/animevideov3_res.onnx'
th = int(sys.argv[2]) if len(sys.argv) > 2 else 216
tw = int(sys.argv[3]) if len(sys.argv) > 3 else 288

d = torch.load('/tmp/resr_animevideov3.pth', map_location='cpu')
sd = d['params'] if 'params' in d else d
m = Net(sd).eval()
for mod, n in zip(m.body, m.idx):
    if isinstance(mod, nn.Conv2d):
        mod.weight.data = sd['body.%d.weight' % n]
        mod.bias.data = sd['body.%d.bias' % n]
    else:
        mod.weight.data = sd['body.%d.weight' % n]

x = torch.randn(1, 3, th, tw) * 255.0
with torch.no_grad():
    y = m(x)
print('trace', tuple(x.shape), '->', tuple(y.shape), 'mean %.3f' % y.mean())
torch.onnx.export(m, x, out, opset_version=13, input_names=['input'], output_names=['output'],
                  do_constant_folding=True, dynamo=False)
print('saved', out)
