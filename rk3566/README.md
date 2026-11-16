# RK3566 + OpenWrt 跑 Qwen2.5-0.5B-Instruct (GGUF Q4_K_M)

纯 CPU 推理，静态编译，板上无依赖。

## 1. 环境

构建机：Linux x86_64，cmake >= 3.14，curl，tar。
交叉编译器：musl.cc GCC 11.2（**必须 >= 11**，见第 5 节）。
目标板：aarch64 / musl（OpenWrt 21.02+），4 核 Cortex-A55，RAM >= 2GB。

## 2. 编译

```bash
chmod +x build.sh
./build.sh                      # 产物到 ./out/
./build.sh --with-model         # 同时下载模型
./build.sh --push 10.80.1.125   # 编完推到板上 /opt/llama/
```

手动编译（等价步骤）：

```bash
# 工具链
curl -LO https://musl.cc/aarch64-linux-musl-cross.tgz
tar xzf aarch64-linux-musl-cross.tgz

# 源码
curl -L -o llama.cpp.tar.gz \
  https://codeload.github.com/ggml-org/llama.cpp/tar.gz/refs/heads/master
tar xzf llama.cpp.tar.gz && mv llama.cpp-master llama.cpp
```

`toolchain-aarch64-musl.cmake`：

```cmake
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)
set(PFX /绝对路径/aarch64-linux-musl-cross)
set(CMAKE_SYSROOT ${PFX}/aarch64-linux-musl)
set(TOOLCHAIN_PREFIX ${PFX}/bin/aarch64-linux-musl-)
set(CMAKE_C_COMPILER   ${TOOLCHAIN_PREFIX}gcc)
set(CMAKE_CXX_COMPILER ${TOOLCHAIN_PREFIX}g++)
set(CMAKE_AR      ${TOOLCHAIN_PREFIX}ar)
set(CMAKE_RANLIB  ${TOOLCHAIN_PREFIX}ranlib)
set(CMAKE_STRIP   ${TOOLCHAIN_PREFIX}strip)
set(CMAKE_FIND_ROOT_PATH ${PFX}/aarch64-linux-musl)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
```

```bash
cmake -S llama.cpp -B llama.cpp/build-musl \
  -DCMAKE_TOOLCHAIN_FILE=$PWD/toolchain-aarch64-musl.cmake \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_NATIVE=OFF \
  -DGGML_OPENMP=OFF \
  -DGGML_CPU_ARM_ARCH= \
  -DLLAMA_CURL=OFF \
  -DBUILD_SHARED_LIBS=OFF \
  -DLLAMA_BUILD_TESTS=OFF \
  -DLLAMA_BUILD_SERVER=ON \
  -DCMAKE_C_FLAGS="-O3 -mcpu=cortex-a55+dotprod" \
  -DCMAKE_CXX_FLAGS="-O3 -mcpu=cortex-a55+dotprod" \
  -DCMAKE_EXE_LINKER_FLAGS="-static"

cmake --build llama.cpp/build-musl --target llama-cli llama-bench -- -j$(nproc)
```

校验产物：

```bash
file llama.cpp/build-musl/bin/llama-cli
# 期望：ELF 64-bit LSB pie executable, ARM aarch64, static-pie linked
```

## 3. 模型

```bash
curl -L -o qwen2.5-0.5b-instruct-q4_k_m.gguf \
  "https://modelscope.cn/models/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/master/qwen2.5-0.5b-instruct-q4_k_m.gguf"
```

校验：`head -c 4` 为 `GGUF`，大小 491,400,032 字节。

## 4. 部署与运行

```bash
# 推文件（模型 469MiB，放 /opt 下，别放 /rom）
ssh root@10.80.1.125 'mkdir -p /opt/llama'
cat llama-cli   | ssh root@10.80.1.125 'cat > /opt/llama/llama-cli'
cat llama-bench | ssh root@10.80.1.125 'cat > /opt/llama/llama-bench'
cat qwen2.5-0.5b-instruct-q4_k_m.gguf | ssh root@10.80.1.125 'cat > /opt/llama/qwen2.5-0.5b-q4_k_m.gguf'
ssh root@10.80.1.125 'chmod +x /opt/llama/llama-*'
```

```bash
# 单轮对话
/opt/llama/llama-cli -m /opt/llama/qwen2.5-0.5b-q4_k_m.gguf \
    -st -p "用一句话解释什么是NPU" -n 40 -t 4

# 交互多轮（默认就是多轮，不带 -st 即可；新版没有 -cnv 参数）
/opt/llama/llama-cli -m /opt/llama/qwen2.5-0.5b-q4_k_m.gguf -t 4

# 跑分
/opt/llama/llama-bench -m /opt/llama/qwen2.5-0.5b-q4_k_m.gguf -t 4 -p 128 -n 64 -r 3
```

常用参数：

| 参数 | 说明 |
|---|---|
| `-t N` | 线程数，4 核给 `-t 4`；板上有别的服务时用 `-t 3` |
| `-n N` | 生成 token 上限，0.5B 建议 64~256 |
| `-c N` | 上下文长度，默认 4096；短问答 `-c 1024` 更省内存 |
| `-st` | 单轮，生成完退出；不加它就是交互多轮（`-cnv` 已在新版删除） |

## 5. Web 界面（可选）

`luci-app-llama/` 里是一个 LuCI app，同时提供一个免登录页面。两者共用同一套聊天 UI。

```bash
# 装到设备
cd luci-app-llama && tar czf - . | ssh root@<设备IP> 'tar xzf - -C /'
# 把 llama-server 放到 /opt/llama/llama-server，模型放 /opt/llama/
/etc/init.d/llama enable
/etc/init.d/llama start
```

- 免登录页面：`http://<设备IP>/llama.htm`（静态文件，不走 LuCI 会话）
- LuCI：`服务 → Llama AI`（LuCI 需要 root 密码；设备没设密码时连 LuCI 自己都进不去）
- 配置：`/etc/config/llama`，LuCI 的 Settings 页改完自动重启服务

架构：`llama-server` 监听 `0.0.0.0:<port>`（默认 8090），开 `--cors-origins "*"`；
页面用 `fetch` 走 `/v1/chat/completions`（`stream:true`）做流式输出。

### 5.1 移植到其他设备

app 的 Lua/JS/CGI 部分与架构无关，只有二进制和路径要换。全部通过 UCI 配置，改前端：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `binary` | `/opt/llama/llama-server` | 每台设备自己的 llama-server |
| `model` | `/opt/llama/qwen2.5-0.5b-q4_k_m.gguf` | 任意 .gguf |
| `port` | `8090` | 页面通过 `/cgi-bin/llama` 自动发现，不用改 htm |
| `threads` | `4` | 按核数调 |
| `ctx` | `2048` | 按内存调；KV cache 随它涨 |
| `key_file` | `/etc/llama.key` | 访问口令文件，空=不启用 |

换架构重编 `llama-server`：改 `build.sh` 里的 `TC_URL` 与 `-mcpu`（本仓库 `out/` 里的是
aarch64/cortex-a55）。32 位 ARM 可用 `musl.cc` 的 `armv7l-linux-musleabihf-cross`；
MIPS 设备基本没戏（llama.cpp 要 NEON 和大内存）。

**配置只有一个来源：`/etc/config/llama`。** init 脚本、CGI、网页都不带内置默认路径/端口：

- `binary` / `model` / `port` 缺任何一项 → 服务拒绝启动并写日志
  （`missing required option(s): port in /etc/config/llama`），
  页面显示"配置缺失"并把 CGI 返回的原因弹出来
- 只有 `enabled`(1) / `threads`(4) / `ctx`(2048) 有默认值
- 这样改端口/换模型只改 UCI 一处，不会出现"页面连不上但服务其实在跑"的假象
  （以前 CGI 里写死 8090 就会这样）

### 5.2 中文界面（i18n）

`po/zh-cn/llama.po` 是翻译源。LuCI 运行时读的是编译后的 `.lmo`，只放 po 不生效，要编一次：

```bash
po2lmo po/zh-cn/llama.po llama.zh-cn.lmo
scp llama.zh-cn.lmo root@<设备IP>:/usr/lib/lua/luci/i18n/
```

`po2lmo` 来自 luci-base 的 host 工具（OpenWrt 树里 `staging_dir/hostpkg/bin/po2lmo`，
必须是 x86_64 版；错架构会报 "No such file or directory"）。

验证（在设备上跑，直接看翻译表）：

```bash
lua -e 'local i=require("luci.i18n"); i.setlanguage("zh-cn"); print(i.translate("Settings"))'
# 期望输出：设置
```

已翻译的串：菜单/对话框标题、设置页全部字段、页面状态（运行中/模型加载中/已禁用/未响应）。

### 5.3 界面

- 免登录页与 LuCI 页共用一套 UI；LuCI 页顶部有状态卡片（运行状态/模型/端口）+ 独立页入口
- 流式输出、等待三点动画、消息淡入、代码块「复制」按钮、长对话「回到底部」
- 模型吐 LaTeX 时前端会转成普通字符（`\sqrt{2}` → `√2`），不显示原始反斜杠命令
- 页面不含任何凭据；端口和模型名由 `/cgi-bin/llama` 下发



### 5.4 访问口令（可选，默认关）

- 在 LuCI 的设置页填「访问口令」，或手动 `printf '你的口令\n' > /etc/llama.key; chmod 600 /etc/llama.key`
- 启动参数用 `--api-key-file /etc/llama.key`，所以口令**不在 argv 里**，`ps`/`top` 看不到
- LuCI 保存时把口令写进 0600 文件并从 UCI 里删掉，`uci show` 也看不到
- 网页端：401 时点右上角「口令」输入，只存浏览器 `localStorage`，**htm 源码里没有任何凭据**
- 清空 `key_file` 内容 = 服务恢复开放

多轮对话的注意事项：模型本身无状态，历史由前端 `messages` 数组每次带上去（页面保留最近 6 条），
所以 shell 里的 `llama-cli -st` 是无记忆的，而页面/API 有。

## 6. 解锁 1.8GHz（RK3566 专用，可选但强烈建议）

OpenWrt 21.02 的内核 5.4 在 RK3566 上 cpufreq 不工作，armclk 一直锁在 816MHz。
修好后 1.5B 从 1.90 → 3.79 t/s（2 倍），0.5B 从 3.74 → 8.04。

`kernel/` 里是全部改动，拷到树的对应位置：

```bash
P=target/linux/rockchip
cp kernel/33*.patch $P/patches-5.4/
cp kernel/rk3566-panther-x2.dts $P/files/arch/arm64/boot/dts/rockchip/
```

| 文件 | 作用 |
|---|---|
| `332-rockchip-cpufreq-dt-rk3566.patch` | cpufreq-dt 的 allowlist 加 rk3566/rk3568，否则 platform device 不创建 |
| `333-fan53555-tcs4525.patch` | fan53555 支持 `tcs,tcs4525`（backport v5.15），否则 vdd_cpu 没有驱动、ARMCLK 无法调压 |
| `334-rockchip-cpufreq-dt-late-init.patch` | device 创建从 `device_initcall` 改 `late_initcall_sync`，否则 deferred probe 永不重试 |
| `rk3566-panther-x2.dts` | 删掉 OPP 表里同频率的 speed-bin 变体、去掉 `opp-supported-hw`、CPU clocks 由 SCMI 改成 cru ARMCLK |

这四件事**缺一不可**，任何一层没修 cpufreq 都不起来。每层怎么用一条命令证伪，
见技能 `local-llm-on-arm-sbc` 的「cpufreq 六层调试链」。

验证（刷机后）：

```bash
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_frequencies
# 期望：408000 600000 816000 1104000 1416000 1608000 1800000

# 空闲时低频是正常的（schedutil），要这样看它会不会真升频：
yes >/dev/null & yes >/dev/null & yes >/dev/null & yes >/dev/null &
sleep 4; cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq   # 1800000
killall yes
```

## 7. 注意

- 交叉编译器必须 GCC >= 11：OpenWrt 21.02 树内 gcc 8.4 缺 `vld1q_s8_x4`/`vld1q_u8_x4`，
  且不支持 new 表达式里的 C++17 CTAD，编不过。
- 必须 `-static`，否则板上找不到 musl 的 `ld-musl-aarch64.so.1`。
- `GGML_NATIVE=OFF` 必须加，否则按构建机 CPU 生成指令。
- 只编 `llama-cli` 和 `llama-bench`。编 `llama-app` 需要完整 server 链，会报
  `cannot find -lllama-server-impl`。
- `-mcpu=cortex-a55+dotprod` 依据板上 `/proc/cpuinfo` 的 `asimddp`；不要用 `i8mm`（A55 没有）。
- 新版参数里没有 `-no-cnv`，单轮用 `-st`。
- HF 直连和 hf-mirror 都不通，模型走 ModelScope。
- 二进制带调试信息约 29MB，`aarch64-linux-musl-strip` 后可缩到约 5MB（`STRIP=1 ./build.sh`）。
