# Agent 任务示例（直接抄）

页面 http://10.80.1.125/llama.htm → 切到 [Agent] → 粘贴任务。

> 提醒：3B 在这块板上每步 60-180 秒，2-4 步的任务通常 2-6 分钟。
> 步骤越少越快，所以任务描述里把要素给全（路径、格式、数量）。

## 系统查询（1 步就完）

```
看看这台机器的 CPU 频率、内存、温度
磁盘还剩多少空间
系统跑了多久了，负载多少
```

对应工具：`sysinfo`（一次给全，不用模型拼命令）

## 文件管理

```
/tmp 下最大的文件是哪个，多大
列出 /tmp/llama_agent 目录里所有文件
读一下 /etc/config/llama 的内容（只读，不要改）
统计 /var/log 下有多少个 .log 文件
```

对应工具：`du` / `list` / `read` / `shell`

## 文档

```
做一份 3 页的 PPT，讲本地大模型的速度对比：
封面标题「速度对比」，第二页用柱状图（0.5B=8.0，1.5B=3.7，3B=1.8），第三页列三个要点
```

对应工具：`pptx`（真 .pptx，node+pptxgenjs，支持图表和表格）

## 代码 / 数据

```
写个 python 脚本，用 numpy 算两个矩阵相乘，然后跑给我看结果
用 jq 把这个 json 里的 name 字段全列出来
写个 node 脚本算斐波那契前 10 项并运行
```

对应工具：`write` + `shell`（`python3 xxx.py` / `node xxx.js` / `jq` 都放开了）

## 音乐

```
生成一段 32 小节的 FC 风格背景音乐（BPM 150）
导入 /tmp/llama_agent/某个.mid，用 FC 音色渲染成 MP3
```

对应工具：`midi`（模板，稀疏编曲）/ `music_import`（外部旋律 + FC 音色）

要点：**默认只出 MIDI（几 KB）**。想听合成音色要加环境变量
`AGENT_AUDIO=1`，或者直接调：

```bash
cd /opt/llama/agent && AGENT_AUDIO=1 python3 agent.py -f task.txt
```

## 网络

```
抓一下 http://10.80.1.125/ 这个页面的内容，告诉我标题是什么
```

对应工具：`http_get`（只读 GET，自动去 HTML 标签）

## 不该指望它做的

```
× 改 /etc 里的配置        → 会被拦，它会建议你自己动手
× rm 删文件               → 白名单里没有
× 一次写一首 16 小节的曲子 → 3B 作曲实测 11 分钟 4 小节且单调，这条路走不通
× 算复杂的多步数学        → calc 能算，但 3B 常列错式子（工具补不了"想"）
```

## 命令行等价用法

```bash
# 单次任务
cd /opt/llama/agent
python3 agent.py -f task.txt              # 任务写在文件里（避免 shell 转义中文）
python3 agent.py "1+1等于几"              # 简单任务可以直接给
python3 agent.py -f task.txt --readonly   # 只读模式（禁写文件/PPT）
python3 agent.py -i                       # 交互模式，一行一个任务

# 自检
python3 selftest.py                       # 34 项，退出码 0 = 全绿
```
