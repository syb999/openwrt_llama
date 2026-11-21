#!/usr/bin/env python3
"""Strip the clip(-256, 256) nodes that follow LeakyRelu*1.414, and optionally
fold the 1.414 scale into the consuming Conv weights.

  python3 graph_opt.py noclip in.onnx out.onnx
  python3 graph_opt.py foldscale in.onnx out.onnx
"""
import sys

import numpy as np
import onnx
from onnx import numpy_helper

mode, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
m = onnx.load(src)
g = m.graph

consts = {}
for n in g.node:
    if n.op_type == 'Constant':
        consts[n.output[0]] = numpy_helper.to_array(n.attribute[0].t)

cons = {}
for n in g.node:
    for i in n.input:
        cons.setdefault(i, []).append(n)

remove = set()
rewire = {}

if mode == 'noclip':
    for n in g.node:
        if n.op_type != 'Clip':
            continue
        lo = consts.get(n.input[1]) if len(n.input) > 1 else None
        hi = consts.get(n.input[2]) if len(n.input) > 2 else None
        if lo is None or hi is None:
            continue
        if float(np.ravel(lo)[0]) == -256.0 and float(np.ravel(hi)[0]) == 256.0:
            remove.add(n.name)
            rewire[n.output[0]] = n.input[0]
elif mode == 'foldscale':
    # clip(mul(x, s), lo, hi) == mul(clip(x, lo/s, hi/s), s)
    # 只处理"Clip 输出只喂 Conv"的情况，把 s 折进 Conv 权重（精确等价）
    init = {i.name: i for i in g.initializer}
    arrs = {i.name: numpy_helper.to_array(i) for i in g.initializer}
    new_bounds = {}
    for n in g.node:
        if n.op_type != 'Mul':
            continue
        s = consts.get(n.input[1])
        if s is None or np.ravel(s).size != 1:
            continue
        sv = float(np.ravel(s)[0])
        cl = [u for u in cons.get(n.output[0], []) if u.op_type == 'Clip']
        if len(cl) != 1:
            continue
        clip = cl[0]
        users = cons.get(clip.output[0], [])
        if not users or any(u.op_type != 'Conv' for u in users):
            continue
        lo = float(np.ravel(consts[clip.input[1]])[0])
        hi = float(np.ravel(consts[clip.input[2]])[0])
        new_bounds[clip.name] = (n.input[0], lo / sv, hi / sv)
        for u in users:
            w = u.input[1]
            arrs[w] = arrs[w] * np.float32(sv)
        remove.add(n.name)
    for name, (src_t, lo, hi) in new_bounds.items():
        for n in g.node:
            if n.name == name:
                lo_c = [k for k, v in consts.items() if np.ravel(v).size == 1
                        and float(np.ravel(v)[0]) == -256.0]
                hi_c = [k for k, v in consts.items() if np.ravel(v).size == 1
                        and float(np.ravel(v)[0]) == 256.0]
                n.input[0] = src_t
                n.input[1] = lo_c[0]
                n.input[2] = hi_c[0]
                for k in lo_c:
                    consts[k] = np.array([lo], np.float32)
                for k in hi_c:
                    consts[k] = np.array([hi], np.float32)
    for k, v in arrs.items():
        if k in init:
            pass
    new_init = []
    for i in g.initializer:
        a = numpy_helper.from_array(arrs[i.name], i.name)
        new_init.append(a)
    del g.initializer[:]
    g.initializer.extend(new_init)
else:
    sys.exit('unknown mode')

keep = [n for n in g.node if n.name not in remove]
del g.node[:]
g.node.extend(keep)

for n in g.node:
    for k, i in enumerate(n.input):
        seen = set()
        while i in rewire and i not in seen:
            seen.add(i)
            i = rewire[i]
        n.input[k] = i

onnx.checker.check_model(m)
onnx.save(m, dst)
print('removed %d nodes -> %s' % (len(remove), dst))
