# ai-desub —— RK3566 上做 AI 去字幕（实测版）

把这台板子（RK3566 / RKNPU2 / OpenWrt 21.02.5 musl）上跑 AI 去字幕的完整成果放这里：
能用什么、不能用什么、每个版本为什么这么写、踩过哪些坑。

---

## 一、结论先给

| 方案 | 速度 | 质量 | 状态 |
|---|---|---|---|
| **MI-GAN + NPU** | 1.97 秒/帧（10 秒片 8 分钟） | 字幕消失、画面基本自然 | ✅ 可用（`migan_run.py`） |
| LaMa + 板子 CPU | 10.1 秒/帧（10 秒片 42 分钟） | 更细腻 | ✅ 可用（`lama_desub.py`） |
| ffmpeg `fill` / `crop` | 毫秒级 | 遮挡或裁剪，不是修复 | ⚠️ 只能算兜底 |

**为什么 LaMa 上不了 NPU**：它的核心 FFC 模块用 FFT（`torch.fft.rfft2`），RKNPU2 没有这个算子，转 rknn 会在 build 阶段失败。
**所以 NPU 只能跑纯卷积模型** —— MI-GAN（6.0M 参数，94 个 Conv）是目前找到的唯一"能转 + 质量过关"的。

---

## 二、环境怎么搭的（不用编 torch、不用改 OpenWrt）

板子是 musl，PyTorch 只有 glibc 轮子。板上本来就有 `/opt/glibc`（ppocr 用的那套），所以：

```sh
# 1) 自包含 aarch64 Python（glibc 链接，24MB）
mkdir -p /opt/py && cd /opt/py
curl -sL -o py.tar.gz "https://github.com/astral-sh/python-build-standalone/releases/download/20240107/cpython-3.10.13%2B20240107-aarch64-unknown-linux-gnu-install_only.tar.gz"
tar xzf py.tar.gz

# 2) 运行时库路径（缺 libutil.so.1 之类都从 chrome-root 补）
L="/opt/glibc:/opt/chrome-root/lib/aarch64-linux-gnu:/opt/chrome-root/lib"
/opt/glibc/ld-linux-aarch64.so.1 --library-path $L /opt/py/python/bin/python3 -V

# 3) 装依赖（强制 manylinux aarch64 平台，否则 pip 会以为要 musl 轮子）
export PYTHONPATH=/opt/py/libs
/opt/glibc/ld-linux-aarch64.so.1 --library-path $L /opt/py/python/bin/python3 -m pip install \
  --target /opt/py/libs --platform manylinux2014_aarch64 --only-binary=:all: --python-version 3.10 \
  torch numpy pillow scipy opencv-python-headless rknn-toolkit-lite2==2.3.2 simple-lama-inpainting

# 4) rknnlite 只认 /usr/lib/librknnrt.so
ln -sf /opt/rknpu/librknnrt.so /usr/lib/librknnrt.so
```

**NPU 内存是硬约束**：非 IOMMU 模式要连续内存，llama-server 占 2GB 会让 `init_runtime` 失败。
所有 shell 脚本都做了「停 LLM → drop_caches → 跑 → 恢复 LLM」。

---

## 二点五、目录结构和"从零重建"

```
ai-desub/
├── README.md                  ← 本文
├── env/
│   ├── setup_env.sh           ← 一条命令重建板上运行环境（/opt/py）
│   └── requirements.txt       ← 实测通过的版本组合（锁死，别乱升）
├── models/
│   ├── migan512.rknn          ← 25MB  板子直接加载的 NPU 模型（fp16）
│   ├── migan512.onnx          ← 27MB  中间产物，重新转 rknn 用
│   └── migan_512_places2.pt   ← 29MB  原始 PyTorch 权重（GitHub 来源）
├── scripts/
│   ├── migan_run.py           ← v1 主力（插件 mode=npu 调这个）
│   ├── migan_run2.py          ← v2 实验（抗抖，有描边残影）
│   ├── migan_run3.py          ← v3 实验（YUV 直通，未跑通）
│   ├── migan_run4.py          ← v4 字幕不变则复用（本目录最新，见文末）
│   ├── lama_desub.py          ← LaMa 固定带
│   └── lama_full.py           ← LaMa 全屏文字检测
├── shell/                     ← 板上入口脚本（自动管 NPU 内存 + 恢复 LLM）
└── convert/
    ├── convert_migan.py       ← ONNX → rknn
    ├── convert_inpaint.py     ← 通用 ONNX → rknn
    └── export_aotgan.py       ← AOT-GAN 权重 → ONNX（含图优化）
```

### 新板子/重装后三步重建

```sh
# 1) 运行环境（Python + torch + rknnlite + 依赖，约 1.2GB）
scp -r ai-desub root@板子:/tmp/ && ssh root@板子
cd /tmp/ai-desub && sh env/setup_env.sh

# 2) 模型
cp models/migan512.rknn /opt/py/

# 3) 入口脚本
cp shell/migan.sh shell/migan2.sh /opt/py/ && chmod +x /opt/py/*.sh

# 4) 自检（跑 10 帧看看）
cd /opt/py && ./migan.sh /opt/mp4/03.mp4 /tmp/t.mp4 --band-y 383 --band-h 49
```

### 环境依赖（板上实测版本，锁死）

```
Python 3.10.13 (python-build-standalone aarch64-glibc, 24MB tarball)
torch 2.5.1              numpy 2.2.6            pillow 12.2.0
scipy 1.15.3             opencv-python-headless 5.1.0.95
rknn-toolkit-lite2 2.3.2 simple-lama-inpainting 0.1.2
系统侧：/opt/glibc（已有）、/opt/rknpu/librknnrt.so 2.3.2（已有）
软链：  /usr/lib/librknnrt.so → /opt/rknpu/librknnrt.so（setup_env.sh 会建）
```

**装的时候必须带 `--platform manylinux2014_aarch64 --only-binary=:all:`** —— 否则 pip 按 musl 找轮子，一个都找不到。

### 磁盘占用

```
/opt/py/           约 1.2GB（Python 24MB + torch 等 1.1GB）
models/migan512.rknn  25MB
models/big-lama.pt   196MB（没放仓库，按需下载，URL 见下）
```

LaMa 权重单独下（196MB 太大不适合进仓库）：

```sh
curl -sL -o /opt/py/big-lama.pt https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt
```

---

## 三、脚本清单

### scripts/

| 文件 | 说明 | 状态 |
|---|---|---|
| `migan_run.py` | **v1 主力**：整帧缩到 512×512 跑 MI-GAN，只回填遮罩像素 | ✅ 已验证（01m/03 都干净） |
| `migan_run2.py` | v2 实验：原生 512 块（不缩整帧）+ 细笔画检测 + 时间中位数/EMA 抗抖 | ⚠️ 抗抖 32%，但在 03.mp4 上有彩色描边残影 |
| `migan_run3.py` | v3 实验：全程 YUV 直通（不经过 PNG），想消除色彩损失 | ❌ 编码端喂不进去，未跑通 |
| `lama_desub.py` | LaMa 按固定字幕带处理（板子 CPU，10 秒/帧） | ✅ 质量最好但慢 |
| `lama_full.py` | LaMa 按帧检测文字位置（适合片头/职员表这种全屏字） | ✅ |

### shell/

| 文件 | 对应 |
|---|---|
| `migan.sh` | → `migan_run.py`（v1，插件 `mode=npu` 调的就是它） |
| `migan2.sh` | → `migan_run2.py`（v2，可传 `--ema 0.75 --tmedian 5`） |
| `migan3.sh` | → `migan_run3.py`（未跑通） |
| `lama.sh` | → `lama_desub.py` |

### convert/

| 文件 | 说明 |
|---|---|
| `convert_migan.py` | MI-GAN ONNX → rknn（**必须 `do_quantization=False`**，见坑 4） |
| `convert_inpaint.py` | 通用 ONNX → rknn（AOT-GAN 用过，带双输入归一化） |
| `export_aotgan.py` | AOT-GAN 权重 → ONNX（含把 ReflectionPad/双线性上采样改掉的优化，节点 1137→153） |

---

## 四、用法

### 板上命令行

```sh
cd /opt/py
./migan.sh 输入.mp4 输出.mp4 --band-y 383 --band-h 49
```

`--band-y/--band-h` 是字幕带的位置和高度（像素）。**宁可给大几像素，少 10 像素就会留淡残影。**

### 网页插件里（luci-app-llama ≥ 2.8.6）

```json
{"op":"desub","path":"/opt/mp4/03.mp4","mode":"npu","band_y":383,"band_h":49}
```

返回后台任务 ID，用 `{"op":"job","id":"npu_xxxx"}` 或 `?vprog=npu_xxxx` 查进度（实测 pct 正常）。

---

## 五、踩过的坑（按重要性排）

**1. MI-GAN 的 I/O 约定（官方文档没写，靠对照实验反推）**

```
输入 4 通道 float32 [1,4,512,512]
  ch0 = mask - 0.5   →  +0.5 表示"保留"，-0.5 表示"要修"
  ch1..3 = RGB，(x/127.5 - 1)
  洞内的 RGB 在归一化空间填 0（灰），不是 -1（黑）
输出 [1,3,512,512]（NCHW），(x+1)*127.5 还原
```

**2. 不要整帧缩放**

```
✗ 整帧 576×432 → 512×512 → 处理 → 缩回 576×432
  整帧被重采样两次，全画面发糊，用户一眼看出"画面变了"
✓ 模型输出缩回原分辨率后，只把遮罩像素写回，其余保持原样
```

**3. 字幕检测不能只看亮度**

```
只看亮度 → 白云/亮皮肤/白色道具都会被当成字去 inpaint
        → 真实画面被改掉，看起来就是"位移/变形"
正确做法：亮像素里排除大块亮区，只认细笔画
   big  = binary_opening(bright, 5x5)
   thin = bright & ~big
   细笔画占比 <0.2% 或 >30% 都判为"这帧没字幕"，直接跳过
```

**4. rknn 的两个硬限制**

```
① 量化只接受 3 通道图片做校准 → 4 通道输入只能 fp16（do_quantization=False）
② config 里给 mean_values/std_values 会按 3 通道校验 → 4 通道模型不要给，前处理自己做
```

**5. PNG 往返有色彩损失**

```
视频(YUV) → PNG(RGB) → 处理 → PNG → 视频(YUV)
PSNR 从 52dB 掉到 32~35dB（发灰、边缘发虚）
显式 bt601（-colorspace smpte170m）+ 8M 码率能到 35dB，但消不干净
根治要全程 YUV 直通（migan_run3.py 未跑通）
```

**6. NPU 初始化的脾气**

```
- 停 llama-server + echo 3 > drop_caches 后才大概率成功
- 进程被 kill -9 后，NPU 内存不回收，必须重启板子
- core_mask= 参数在这版 runtime 会抛异常，用裸 init_runtime()
```

**7. 时间平滑的两面性**

```
逐帧独立 inpainting → 同一位置每帧重新"猜"……纹理随机 → 抖动
时间中位数 5 帧 + EMA 0.75 → 相邻帧差异 1.54 → 1.04（降 32%）
但字幕本身在动时，中位数会把不同帧的字混在一起 → 彩色描边残影
所以 v2 的平滑只在 03 上就翻车了，v1 更稳
```

---

## 六、权重从哪来

```
MI-GAN   https://raw.githubusercontent.com/manuelrucci7/people-remove/main/code/models/migan_512_places2.pt
         （Picsart 官方 6.0M 参数，29MB，GitHub 直链可下）
         模型代码：github.com/Picsart-AI-Research/MI-GAN 的 lib/model_zoo/migan_inference.py
         （这个文件自包含，不依赖 stylegan 的 torch_utils）

LaMa     https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt
         （197MB TorchScript，配 simple-lama-inpainting 包使用）
```

AOT-GAN 那份（`TT113/aotgan-app` 的 `G0000000.pt`）**不要用**：纯色能填，真实画面全是噪点，权重是坏的。

---

## 七、速度参考（板上实测）

| 素材 | MI-GAN NPU | LaMa CPU |
|---|---|---|
| 10 秒（250 帧） | 8 分钟 | 42 分钟 |
| 1 分钟（1500 帧） | 50 分钟 | 4.2 小时 |
| 22 分钟（33,600 帧） | 18 小时 | 94 小时 |

---

## 八、还没做完的（TODO）

1. **抖动根治**：同一句台词期间复用同一块修复结果（文本不变就只算一次）。
   既治抖又提速 —— 同一句台词停 2 秒，只需算 1 帧，其余复用。
2. **YUV 直通**：`migan_run3.py` 的思路对（不经 PNG 就没有色彩损失），卡在编码端管道。
3. **条带模型**：MI-GAN 是固定 512×512，若训练/转换一个 512×128 的条带模型，
   算力省 4 倍、细节更准，10 秒片能到 2 分钟。
