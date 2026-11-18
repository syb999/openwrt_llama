# C Agent — RK3566 本地 AI 助手

纯 C 写的 Agent 框架，跑在 RK3566 (Panther X2) / OpenWrt 21.02.5 上，
后端是 llama.cpp 的 `llama-server`（OpenAI 兼容接口，纯 CPU）。

Python 版已废弃删除，这里只有 C 实现。

## 目录

```
agent.c        主程序：HTTP 客户端 / JSON / 插件加载 / 任务分块 / 路由
cgi.c          Web 后端：?task ?job ?env ?file ?upload ?stop_job ?auto*
json.c json.h  轻量 JSON 解析
plugin.h       插件接口（plugin_entry / plugin_auto_entry）
memcheck.c     内存记账（--memcheck 打开）
memstat.sh     扫日志里的 [mem] 行做汇总
Makefile       编译规则

plugins/       11 个插件，见下表
docwrite.py    文档写入脚本的源（生成 py_write.h）
py_write.h     docwrite.py 转成的 C 字符串，doc.so 内嵌执行
doc_read.inc   文档读取：docx/xlsx/pptx 的 zip+xml 解析（纯 C）
```

## 编译

本机编出来是 x86_64，**必须到板上用交叉工具链重编**：

```sh
make CC=aarch64-openwrt-linux-musl-gcc LDFLAGS=
```

- `LDFLAGS=` 是必需的：默认的 `-static` 在 musl 上会让 `dlopen` 失效
- 编译选项里带 `-D_FORTIFY_SOURCE=0`（musl + 老 glibc 头文件的组合会报错）
- 改了 `.inc` 或 `py_write.h` 之后要 `rm -f plugins/xxx.so` 强制重编，
  板上时钟和主机有偏差时 `make` 会误判「不需要重建」

## 部署

```sh
cp agent          /opt/llama/agent/agent-c
cp cgi            /www/cgi-bin/agent
cp plugins/*.so   /opt/llama/agent/plugins/
chmod 755 /opt/llama/agent/agent-c /www/cgi-bin/agent /opt/llama/agent/plugins/*.so
```

板上 `/opt/llama/agent/` 需要：

```
agent-c       主程序

memstat.sh    可选，分析内存记账
plugins/      11 个 .so
```

## 插件

| 插件 | 干什么 |
|---|---|
| `math` | 数学：算式 / 方程 / 方程组 / 统计 / 数论 / 行程 / 鸡兔 / 工程 |
| `curl` | 解析 DevTools「Copy as cURL」直接发请求，响应按 JSON/HTML/二进制分类落盘 |
| `jq` | 本地 JSON：probe 探测结构 / get 分批取值 / table 导表格+CSV / query 自定义表达式 |
| `doc` | 文档读写：PDF/Word/Excel/PPT → 文本；生成 docx/xlsx/pptx/pdf |
| `sys` | 文件与系统：sysinfo / shell / list / du / grep / read / write / http_get |
| `net` | 网络诊断：ip / route / dns / arp / ping / port / trace / wifi |
| `owrt` | OpenWrt 配置：uci_* / iwinfo / iw / service / logread |
| `music` | MIDI 生成 / FC 音色合成 MP3 / 导入 MIDI 转 MP3 |
| `batch` | 大任务切小 JSON 分批生成再合并 |
| `table` | 按列排序取 TOP/BOTTOM |
| `sysmon` | 系统快照 + 自动巡检（`sysmon-auto`） |

后两个是自动化插件：默认停用，支持前台跑和后台守护，网页可启停。

## 依赖

`jq` 和 `curl` 是可执行文件，不是 Python 库：

- 缺 `jq` → 不加载 `jq.so`，环境面板里 `libs.jq = 0`
- 缺 `curl` → 不加载 `curl.so`

依赖表在 `agent.c` 的 `PLUG_DEPS[]`，加新依赖加一行即可，插件本身不用改。

**读取**文档（板上不需要任何 `.py`）：

| 格式 | 实现 | 效果 |
|---|---|---|
| `.docx` | 解 zip → `word/document.xml` → `<w:t>` | 纯 C，段落换行正确 |
| `.xlsx` | 解 zip → sheet xml + sharedStrings/inlineStr | 纯 C，中文数字都对 |
| `.pptx` | 逐页解 `ppt/slides/slideN.xml` → `<a:t>` | 纯 C，按页分段 |
| `.pdf` | pypdf（脚本运行时生成到 `/tmp/.doc_pdf.py`） | 完整，21 页文档验证过 |
| 纯文本 | 直接读（限 8MB） | 完整 |

PDF 为什么没做纯 C：它要解析对象树 → 每页资源 → 字体 `/Subtype`
→ 各自的 `/ToUnicode` CMap → 文本流里 `/F1 Tf` 切换编码。子集嵌入字体
+ 混合编码的真实 PDF 上，几百行的 C 实现还原不出文字（pypdf 是几千行）。
Office 三种是纯 C，PDF 交给 pypdf —— 但脚本内嵌在插件里生成，
不依赖板上任何 `.py` 文件。

**写**文档走纯 C + python3 库：doc.so 里 fork 出 `python3 -c <脚本> <op>`，
数据从 stdin 进、结果从 stdout 出。那段脚本内嵌在 `py_write.h`（C 字符串），
板上不需要任何 `.py` 文件。

写操作依赖 `python-docx` / `openpyxl` / `python-pptx`（PDF 还要 `pypdf`）。

## 改文档写入逻辑

`py_write.h` 是一段 Python 脚本转义成 C 字符串的产物。仓库里没有原始的 `.py`
（保持"板上零 .py"），所以改的逻辑是这样：

1. 写一份新的 Python 脚本，接口照旧：
   - `sys.argv[1]` 是 op（`text` / `xlsx` / `docx` / `pptx` / `pdf` …）
   - 数据从 stdin 读（JSON）
   - 结果以 JSON 打到 stdout
2. 转成 `py_write.h`：

```sh
python3 - <<'EOF'
src = open('new_write.py', encoding='utf-8').read().split('
')
out = ['/* 自动生成：文档写入脚本内嵌在这里 */', '#ifndef PY_WRITE_SRC', '#define PY_WRITE_SRC \\']
for i, ln in enumerate(src):
    esc = ln.replace('\\', '\\\\').replace('"', '\\"')
    out.append('    "%s\\n"%s' % (esc, ' \\' if i < len(src) - 1 else ''))
out.append('#endif')
open('py_write.h', 'w', encoding='utf-8').write('\n'.join(out) + '\n')
EOF
```

3. `make clean && make`（`doc.so` 依赖 `py_write.h`，会重编）

加 PPT 版式、改 Excel 样式都在这一步做——`python-pptx` / `openpyxl`
的排版能力用 C 重写不值得。

## 几个关键设计

**任务分块**（`CHUNKS[]`）

一句话里有多个动作时，按关键词在任务里的位置切成有序步骤依次执行，
后一步自动吃前一步的产出：

```
"输出表格，再转成 xlsx"
  → 第 1 步 jq.table（主表 + 各子表 CSV）
  → 第 2 步 doc.xlsx（读上一步的 CSV，打包成多 sheet）
```

加新动作在表里加一行。

**插件联动**（`PIPES[]`）

一个插件的产出是另一个插件的输入时自动接力：

```c
{"curl", "type", "json", "jq", "{\"op\":\"probe\",\"file\":\"%f\"}"}
```

curl 抓到 JSON 就自动让 jq 报一下结构（多少条、有哪些字段），非 JSON 不触发。

**插件路由**（`route_plugins()`）

按关键词只把相关的 1~3 个插件写进 prompt。没命中时全列兜底。
`AGENT_DEBUG=1` 会打印 `[路由] ...`。

**纯对话预判**（`is_pure_chat()`）

路由一个都没命中、也没有强动作词 → 不给插件表，prompt 从 1.5KB 降到 150 字节。
先跑路由再判断，否则 `帮我算一下` 这种会被误判成闲聊。

**生成必须非流式**

`llama-server` 用 `-np 1` 单 slot。流式请求会让它卡在写阻塞
（4 核满载、不出结果、后续请求全排队）。所以 `"stream":false`，
前端靠 `?job=` 轮询日志 + "运行中 XX 秒"看进度。

**停止按钮要杀进程**

前端 abort 自己的 fetch 不够，还得调 `?stop_job=1`：
读 `.running` 的 pid → SIGTERM → 2 秒不退 SIGKILL → 清 `.running`。

**输出格式**

模型必须输出单个 JSON 对象，`tool` 字段决定调哪个插件，`final` 表示直接回答。
框架侧有三道容错：多余反斜杠、数字后多余引号、截断的 JSON 补齐。
