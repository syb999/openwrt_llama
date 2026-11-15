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

# 交互多轮
/opt/llama/llama-cli -m /opt/llama/qwen2.5-0.5b-q4_k_m.gguf -cnv -t 4

# 跑分
/opt/llama/llama-bench -m /opt/llama/qwen2.5-0.5b-q4_k_m.gguf -t 4 -p 128 -n 64 -r 3
```

常用参数：

| 参数 | 说明 |
|---|---|
| `-t N` | 线程数，4 核给 `-t 4`；板上有别的服务时用 `-t 3` |
| `-n N` | 生成 token 上限，0.5B 建议 64~256 |
| `-c N` | 上下文长度，默认 4096；短问答 `-c 1024` 更省内存 |
| `-st` | 单轮，生成完退出 |
| `-cnv` | 交互多轮 |

## 5. 注意

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
