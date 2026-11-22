# 在小板上跑本地 LLM（Qwen2.5 + llama.cpp）+ NPU 视频去字幕

全部跑在板子上，不依赖云端：本地 LLM 推理、免登录 Web 界面、C 语言 agent 插件框架，
以及**用 RK3566 的 NPU 做视频硬字幕去除**。

| 目录 | 目标 | 内容 |
|---|---|---|
| [rk3566/](rk3566/) | RK3566 + OpenWrt 21.02（4×Cortex-A55，4GB） | OpenWrt 包（llama.cpp / luci-app-llama / desub-rk3566）、固件改动、使用文档 |
| [rk3566/desub-rk3566/](rk3566/desub-rk3566/) | NPU 去字幕 | MI-GAN 转 RKNN 的全套流程、Python 流水线、模型 |
| [rk3566/kernel/](rk3566/kernel/) | 内核/启动参数 | cma=512M、THP=never（NPU 与 LLM 共存的关键） |

## 实测速度（Qwen2.5 Q4_K_M，-t 4，RK3566 @1.8GHz）

| 模型 | 内存 | pp512（预填充） | tg128（生成） |
|---|---|---|---|
| 0.5B | 0.4 GB | — | 8.0 t/s |
| 1.5B | 1.0 GB | — | 3.7 t/s |
| **3B** | **2.0 GB** | **5.28 t/s** | **2.33 t/s** |

3B 现在**长期跑稳定**（配合 cma=512M + THP=never，NPU 和 LLM 可以同时工作）。
上面的数字是 OpenWrt 工具链（GCC 8.4）编出来的版本，比之前手工用 GCC 11.2
交叉编译的还快 15~23% —— 关键在 `-O3 -mcpu=cortex-a55+dotprod` 和三个 GCC 8 补丁。

## 能力一览

```
LLM        llama-server（OpenAI 兼容 API）/ llama-cli / llama-bench，全部编进固件
界面       免登录 htm 页面：气泡对话、插件面板、视频播放器（原片/处理后对比）
Agent      14 个 C 插件（math / curl / jq / doc / sys / net / owrt / music /
           netcap / browser / batch / table / sysmon / video），可逐个开关
视频       转竖屏 / 压缩 / 水印 / 裁剪 / 合并 / 切片 / 抽帧 / 黑屏检测（ffmpeg + rkmpp）
AI 去字幕  NPU 跑 MI-GAN，逐帧重建被硬字幕盖住的画面，约 1 秒/帧
```

## 快速开始

```bash
# 1) 刷固件（含 llama.cpp 三件套 + desub-rk3566 + 全部界面）
scp openwrt-rockchip-armv8-panther_x2-squashfs-sysupgrade.img.gz root@10.80.1.125:/tmp/fw.img.gz
ssh root@10.80.1.125 'sysupgrade -v /tmp/fw.img.gz'

# 2) 放模型（固件不含模型，自己下）
curl -L -o qwen2.5-3b-q4_k_m.gguf \
  "https://modelscope.cn/models/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/master/qwen2.5-3b-instruct-q4_k_m.gguf"
cat qwen2.5-3b-q4_k_m.gguf | ssh root@10.80.1.125 'cat > /opt/llama/qwen2.5-3b-q4_k_m.gguf'

# 3) 打开 http://10.80.1.125/llama.htm
```

细节见 [rk3566/README.md](rk3566/README.md)。
