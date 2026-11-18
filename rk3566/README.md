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

## 7. Agent 模式

`/llama.htm` 头部有个「对话 / Agent」切换按钮。Agent 模式走 `/cgi-bin/agent`：

```
POST /cgi-bin/agent  {task, max_steps}  → 后台启动，返回 {"job":"a1b2c3d4"}
GET  /cgi-bin/agent?job=a1b2c3d4        → {"done":false,"output":"...第 1 步──"}
```

页面每 1.5 秒轮询，用步骤卡片显示每次工具调用。**同一时刻只允许一个任务**
（llama-server 是 np=1，并发只会互相排队，实测 5 个并发慢 8 倍）。

### 7.1 工具

| 工具 | 用途 |
|---|---|
| `list` / `du` | 列目录 / 按大小排序（小模型不会拼 shell 管道，给它一步到位的工具）|
| `read` / `write` | 读文本 / 写文本（拒二进制、自动建父目录、写完回读验证）|
| `pptx` | 生成真 .pptx（node+pptxgenjs，支持 cover/bullets/chart/table）|
| `midi` | 模板化音乐（style: calm/happy/sad/epic/jazz/fc_action）|
| `music_start` / `music_bar` / `music_notes` / `music_render` | 让模型自己写旋律，工具只渲染 |
| `music_import` | 导入现成 .mid → 用 FC 音色渲染成 MP3 |
| `sysinfo` | CPU 频率/负载/内存/温度/磁盘/运行时间，一次给全（省得模型拼 5 个命令）|
| `http_get` | 抓网页或 API（只读 GET，自动去 HTML 标签，600KB 上限）|
| `download` | 下载文件存到工作目录（20MB 上限，超了自动删并报错）|
| `grep` | 在目录里搜内容（`grep -rn` 的一步到位版）|
| `shell` | 只读命令 + `python3 xxx.py` / `node xxx.js` / `jq`（禁 -c/-e/-m，禁 rm/find -delete/sed -i/npm install）|
| `calc` | 安全表达式求值（3B 心算不可靠，算数一律走它）|
| `ask` | 反问用户（非 tty 环境自动降级成"请自行决定"）|

### 7.2 结构化输出用 schema 约束，不靠正则

llama.cpp 支持 `response_format` + `json_schema`，会把 schema 编译成 GBNF grammar，
**在采样阶段就禁掉非法 token**。agent 每次请求都带完整 schema（13 个工具枚举 +
参数类型 + slides 结构），所以模型物理上写不出坏 JSON。

比事后正则修复靠谱得多：正则只能救已知的错误形态，grammar 让错误不产生。
正则修复仍保留（兜底 9 类语法错误 + 字符串内容里的引号）。

### 7.3 12 层脚手架（每层对应一个真实失败）

```
basename 白名单   模型爱写 /bin/ls
高层工具          模型不会拼 ls|xargs|du
重复动作检测      连撞同一堵墙就拦下
JSON 自动修复     尾逗号/少括号/全角标点/裸换行/中文引号/尾巴多引号
answer 容错       final 字段混进工具调用（pptx 例外：先执行再收尾）
二进制检测        见 2GB 镜像不再灌 4000 个 \0
扩展名校验        写不出假 .ppt
文件存在性校验    final 声称写过的必须真存在
页数校验          要 3 页给 1 页，把差距告诉模型
写入回读验证      真实字节数+首行回给模型
超时重试         瞬时错误重试一次
ask 降级          非 tty 不阻塞
```

### 7.4 输出目录与清理

```
生成物    /tmp/llama_agent   （tmpfs，重启自动清空）
清理      最多 24 个文件 / 200 MB，超出按 mtime 删最旧
代码      /opt/llama/agent   （只有 .py + node_modules）
下载      /cgi-bin/agent?file=xxx
```

### 7.5 音乐（FC 风格）

用 numpy 自己合成 FC 音源，不依赖 MIDI 播放器的波表：

```
主旋律  50% 占空比方波 + 打击包络（a=1ms d=75ms s=42%），32kHz 采样
低音    三角波，二分音符
鼓      噪声通道 kick/snare
编曲    稀疏：每小节 ≤3 个旋律音，intro-A-B-A'-outro 结构
```

**关键教训：密集 ≠ 好听**。之前每小节塞 8 个十六分音符 + 和声 + hihat，
实测是噪音；改成每小节 2-3 个长音才听得清旋律。

默认只出 MIDI（几 KB）。要听自己合成的音色，加环境变量：

```bash
AGENT_AUDIO=1 python3 agent.py -f task.txt    # 额外渲染 MP3（ffmpeg 64kbps）
```

### 7.6 自测

```bash
cd /opt/llama/agent && python3 selftest.py    # 34 项检查，退出码 0 表示全绿
```

### 7.7 性能说明（重要）

**每步慢的真凶是 system prompt 太长**，不是模型本身：

```
最初版 prompt  3357 字符 ≈ 2300 token
3B prefill 约 13 t/s（板上负载 4 时）
→ 光"读 prompt"就要 177 秒，加上生成，每步 3 分钟以上
→ 实测过：旧 prompt 跑 8 分钟连第一步都没出来

压缩后        1235 字符 ≈ 850 token（省 63%）
→ 同一个"查系统信息"任务：2 步 / 296 秒完成，答案正确
  （CPU 1800MHz、内存 738MB、温度 78.1°C —— 全部对上 sysinfo 原始输出）
```

改 prompt 时注意：`SYSTEM` 现在是纯字符串，代码里**不能再做 `%` 格式化**
（之前 `SYSTEM % max_steps` 会因为新 prompt 没有占位符而抛 TypeError）。

另外这块板子是"兼职"的——同时跑着 Docker / Gitea / Caddy / MySQL / dss-web，
load average 常年在 3-4（4 核接近满载），会把推理速度再砍一半。
agent 启动时会打印负载提示：

```
提示：负载 4.1（4 核接近满载），可用内存只有 680 MB —— 推理会明显变慢，不是卡死。
```

想更快只有三条路：给 llama-server 提 nice 优先级（挤压其他服务）、
砍掉板子上无关的服务、或者换专机。

### 7.8 数学应用题（math 路径）

agent 主循环强制输出 JSON（schema → grammar），模型没有地方写推理。
实测同一道题 3B 在纯对话模式能算对，进 agent 模式只会瞎编式子：

```
长方形 长宽比5:2 对角线29    对话模式 290 ✓   agent 模式 260.41 / 72.5 / 1219.45 ✗
```

解法是 `math` 工具内部再走一次"无 JSON 约束"的自由生成：

```
第 1 步  模型选 math，把题目原文传进来
   （工具内部）chat(use_schema=False) → 完整推理 + 答案
第 2 步  模型把结论 final 出去
```

自动路由 `_looks_like_math()`：匹配应用题特征词（面积/比例/速度/路程/
单独做/合作/完成/分之/鸡兔/浓度/折扣/相遇…），且不含文件/系统/PPT 这类
操作词，就在任务末尾注入【强制】指令。**不注入模型不会用 math**（实测它照旧选 calc）。

实测通过：

```
长方形  长宽比5:2 对角线29     → 290     ✓（2 步 341 秒）
工程    甲12天 乙15天 完成3/5   → 4 天    ✓（2 步 308 秒）
行程    300km 60+40 相向        → 3 小时  ✓
```

代价：math 内部要生成 500 字完整推理，整题 3-5 分钟。**准确率优先，慢可以接受**。

LaTeX 清理：模型推导常带 `\[ \]`、`\frac{}{}`、`\sqrt{}`、`^{2}`，
`run_math` 返回前统一转可读符号（`(3/5)/(3/20)`、`√(29)`、`x²`）。
坑：模型有时不写反斜杠（只写 `frac{...}`），正则要用 `\*` 兼容零个反斜杠。

### 7.9 题型分流（实测出来的四条路）

| 题型 | 判定 | 走向 | 实测 |
|---|---|---|---|
| 方程题（球数/鸡兔/和差倍/年龄）| `_looks_like_math` + `_math_kind=equation` | `solve`（sympy 精确解+代回验证）| 球题 55 秒，红10黄11白9 ✓ |
| 多步计算（物理/行程/浓度/折扣/利润）| `_looks_like_math` + `_math_kind=compute` | `math`（无约束自由推理）| 平均速度 337 秒，4.8 ✓ |
| 图形/空间推理（火柴/拼图/折纸/涂色）| `_looks_like_visual` | 直接 final 说做不到 | 70 秒，诚实回答 |
| 操作类（文件/系统/PPT/音乐/网络）| 其他 | 各自工具 | 几十秒到几分钟 |

为什么同一道题走错路就废（都是实测）：

```
平均速度题（12m，前 4m/s 后 6m/s）
  对话/math 自由推理  → 4.8 ✓（分段求时间 → 总路程÷总时间）
  solve              → 列出 (x/2)^2+(3x/2)^2=12^2（勾股定理！）✗
  write+shell 写脚本 → 代码里 \n 写成字面、变量名对不上 → NameError ✗

球数题（三色球）
  solve              → R=10,W=9,Y=11 ✓（55 秒）
  math 自由推理      → 8/13/9 ✗（把 Y=20-W 代错了方程，还不自检）

火柴题
  不拦截             → calc 编式子 + write 垃圾文件 + read 错路径，4 步全废
  拦截后             → 1 步说"看不了图形"，70 秒
```

所以**方程题和计算题要用不同工具**：方程题靠 sympy 精确，计算题靠自由推理（模型分段算反而对）。

### 7.10 分批生成（小模型写不完的大任务）

3B 一次写长的结构化输出会失败：5 页带表格/图表的 slides JSON 实测 15 分钟没结果，
逐小节调工具 4 步只做出 2 小节。做法是 **拆成小 JSON 批 → 每批一次调用 → 工具合并**：

```
任务："每小节4个音，共8小节"
  → 识别 per_bar=4, bars=8 → 2 批（每批 4 小节）
  → 每批返回 [ [{"pitch":69,"dur":1}, ...], ... ]（≤220 token）
  → 工具按小节顺序拼成一个 MIDI
结果：8/8 小节 32 个音符，2 批 80-200 秒
```

两个必须注意的点：

```
use_schema=False   默认 True 时模型以为自己在调 write 工具，
                   返回 {"tool":"write","content":"{...}"}，解析全失败
json.loads         不要用 extract_json（它只抠第一个内层对象，
                   顶层数组会被拆坏）
```

提示词要给具体示例、写明数量、禁止键名（否则输出 `{"[0,1]": [...]}`）。
每批的 system + 数据摘要保持完全相同，可命中 prompt cache（4 批比一次写完快 3 倍以上）。

### 7.11 步数预算与 token 预算

步数按任务类型给，不再一刀切：

```
文档 read + final          2 步
排序/分组（工具预排序）      1 步
数学 solve + final         2 步
音乐 / PPT（工具内完成）     1 步
通用任务                   4 步
硬上限                     8 步（只防死循环）
```

同时按 token 计成本（每步累计 `回复字数/1.6 + 600`），默认 20000
（`AGENT_TOKEN_BUDGET` 可调）。**超预算不是失败**：把已完成的工具结果提炼成
进度摘要、清空上下文重开一局继续：

```
第1步 calc 2^10 → 1024
第2步 累计 1249 > 900 → 存进度（保留"calc = 1024"）、重置上下文
第3步 直接 final 1024（没有重复调 calc）
```

### 7.12 PPT（版式与配色）

```
版式  cover 封面 / section 章节 / bullets 要点 / two-col 两栏对比 /
      quote 金句 / table 表格 / chart 图表 / closing 结尾
主题  商务深蓝 / 科技青 / 暖橙 / 墨紫 / 暗夜 —— 每次随机，spec.theme 可指定
```

模型只负责内容（分批写 4 个章节的要点），排版由工具做：

```
服务端预处理数据摘要（字段/条数/各列 TOP3/样例 5 行）
→ 4 批 × 1 节 {title, bullets}（约 145 秒）
→ 工具补齐版式、随机配色、填表格和柱状图
```

### 7.13 音乐风格

```
calm 90     happy 120    sad 70      epic 100
jazz 110    fc_action 150
classical 88（大键琴+弦乐，I-IV-V-I）
electronic 128（Pad+合成器，四踩）
guqin 60（五声音阶，长音留白）
```

每种风格有自己的和弦进行、固定节奏型、GM 音色；六种随机轮转（状态存
`/tmp/llama_agent/.last_style`，一轮不重复）。

每小节少于 4 个音会显得太稀（`_midi_batched` 里下限已抬到 4）。

### 7.14 数学题模板层（3B 算不动，工具直接解）

小模型把「还差 1200 米时提前 3 天」这类复合条件翻译成方程，成功率实测 **0/3**
（出现过 t=-12、无解、t=3/2）。但题型结构固定，从题干抠量套模板就稳了：

```
题型                       模板方程
─────────────────────────────────────────────────────────────
A 多修+还差+提前            a*t = (a+b)*(t-n) + c
B 少修+还差+延后            a*t = (a-b)*(t+n) + c
C 只提前（无剩余）           a*t = (a+b)*(t-n)
D 修了N天+还剩              a*t = a*N + c
E 完成m/n+还剩              a*t*m/n + c = a*t
E2 完成一半                 a*t/2 + c = a*t
F 前N天按计划+之后变速       a*N + (a±b)*(t-N-n) = a*t
I 纯天数差                  t ∓ m = 实际天数
G 甲/乙合作                 t*(1/a + 1/b) = 1
G2 合作+完成m/n             t*(1/a + 1/b) = m/n
H 合作中途离开              k*(1/a+1/b) + (t-k)*(1/a) = 1
```

题干数字同时支持阿拉伯和中文写法：

```
720 / 3.5              直接读
七百二十 / 一千二百       _cn_num_all（支持百、千）
五分之三 / 三分之二       _cn_frac（注意分母在前）
一半                    1/2
```

命中模板时 **0 次模型调用**：sympy 解 + 代回验证 + Fraction 算最终量，秒级出结果。
非整数结果同时给分数和近似小数（`32680/3 米（约 10893.33 米）`）。

没命中的题退回 `solve`（模型翻译方程 + sympy 验证）或 `compute`（math 自由推理）。

**改模板时的坑**：用 negative lookbehind 之类要回归全部旧题 ——
`(?<!提)前` 就是为了别让「提前 3 天」被 `前 N 天` 分支吃掉（曾把原题算错）。

## 8. 注意

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
