#!/usr/bin/env python3
"""A minimal tool-using agent loop that works with small models (3B and down).

Why not native function calling: Qwen2.5-3B supports it in training data but
at 4-bit it frequently emits broken JSON or invents argument names. So this
agent uses a *strict JSON-per-turn protocol* with a single retry nudge, which
is far more reliable on small models than the OpenAI tools API.

Usage:
    ./agent.py "看看 /tmp 下有什么文件，告诉我最大的那个"
    ./agent.py --model qwen2.5-3b --max-steps 4 "..."

Env:
    LLAMA_API   default http://127.0.0.1:8090
    AGENT_TIMEOUT per request, default 240s
"""
import argparse
import datetime
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = os.environ.get("LLAMA_API", "http://127.0.0.1:8090")
TIMEOUT = int(os.environ.get("AGENT_TIMEOUT", "900"))

# Commands the agent may run. Keep it small: a small model + a wide shell is a
# foot-gun. Everything here is read-only.
# 注意：小模型爱写 /bin/ls 这种绝对路径，所以按 basename 匹配，正则只负责"这个
# 可执行文件名允不允许"，路径前缀在 _allowed() 里剥掉。
SHELL_ALLOW = re.compile(
    r"^(ls|cat|head|tail|wc|grep|find|du|df|free|uname|uptime|ps|date|echo|"
    r"stat|file|which|sort|uniq|cut|tr|awk|sed|id|pwd|tree|"
    r"jq|python|python3|node|npm|npx)$"
)
# 允许管道（du | sort | tail 很常用），但禁止命令分隔/重定向/替换：
#   ;  &  >  <  反引号  $()  和换行 都会被拒，防 "ls; rm -rf /" 这类拼接
SHELL_DANGER = re.compile(r"[;&<>`\n]|\$\(")
# find 的这几个参数等于任意命令执行/删除，单独拦掉
SHELL_DENY_ARGS = {
    "find": ("-delete", "-exec", "-execdir", "-ok", "-okdir"),
    "sed": ("-i", "--in-place"),
    "awk": ("system",),
}
# 系统目录：写入前必须让用户确认（agent 不擅自改 /etc）
SENSITIVE_DIRS = ("/etc", "/rom", "/usr", "/bin", "/sbin", "/lib",
                  "/root", "/overlay/upper", "/opt/llama")
MAX_TOOL_OUTPUT = 900          # characters fed back into the context
MAX_STEPS_DEFAULT = 4
# 动态步数：不同任务"几步能做完"差别很大，之前一刀切 4 步。
# 硬上限只防死循环（3B 会复读同一工具调用）。
MAX_STEPS_HARD = 8
# 单任务 token 预算（估算：prompt + 生成）。超了就收尾，别让一步吃光时间。
TOKEN_BUDGET = int(os.environ.get("AGENT_TOKEN_BUDGET", "20000"))

# 按任务类型的步数预算（能做完就不给多余步数）
STEP_BUDGET = {
    "doc": 2,        # read + final
    "table": 1,      # 预排序直接出结果
    "visual": 1,     # 直接拒绝
    "math": 2,       # math/solve + final
    "compute": 2,
    "solve": 2,
    "ppt": 1,        # 分批在工具层内部完成
    "music": 1,      # 直接生成
}

SYSTEM = """你只能输出一个 JSON 动作，不要解释、不要 markdown 代码块。
动作里可以带 "think" 字段写你的推理（尤其数学题必须先在 think 里列方程，
否则你只会瞎编式子算错）。think 会展示给用户看。

工具：
{"tool":"sysinfo"} 系统信息(CPU/内存/温度/磁盘)
{"tool":"list","path":"."} 列目录带大小
{"tool":"du","path":"."} 目录按大小排序
{"tool":"read","path":"f"} 读文件（用户上传的文档已转成文本，在 /tmp/llama_agent/uploads/ 下，直接 read）
{"tool":"write","path":"calc.py","content":"a = 1\nprint(a)"} 写文件。path 必给（不写会失败）；
                                           content 是纯代码/纯文本，不要 ``` 围栏，变量不要加 @
{"tool":"grep","pattern":"x","path":"."} 搜内容
{"tool":"table","path":"t.txt","sort_by":"播放量","limit":10} 表格：按列排序取前N
                                           （TOP/排名/最大/时长最长 都用它，别自己数）
{"tool":"table","path":"t.txt","group_by":"作者","limit":10} 表格：按列分组计次取前N组
                                           （"作者里视频数量最多"用这个）
{"tool":"table","path":"t.txt","group_by":"作者","sum_by":"播放量","limit":5} 表格：分组求和
{"tool":"http_get","url":"https://..."} 抓网页/API
{"tool":"download","url":"...","path":"f"} 下载文件
{"tool":"shell","cmd":"ls /tmp"} 执行命令(python3 x.py / node x.js / jq；禁 rm)
{"tool":"calc","expr":"sqrt(16)+2*3"} 精确算数
{"tool":"math","problem":"题目原文"} 数学/逻辑推理题：内部用自由推理（能设未知数、能化简）
{"tool":"solve","equations":["x+y=7","x-y=1"]} sympy 精确解方程/方程组，
                                           会代回原方程验证。能列方程就用它，别手算
{"tool":"pptx","path":"a.pptx","slides":[{"type":"cover","title":"T"},{"type":"bullets","title":"T","bullets":["a"]}]} 做PPT
                                           type 可选：cover封面 / section章节 / bullets要点 /
                                           two-col两栏对比(left,right) / quote金句(quote,by) /
                                           table表格 / chart图表 / closing结尾。
                                           一套好的PPT大概是：cover → section → bullets →
                                           two-col/quote → bullets → closing（别全是 bullets）。
                                           配色每次自动换（5 套主题随机）
{"tool":"midi","path":"a.mid","style":"fc_action","tempo":150,"bars":32} 生成整首MIDI（默认就用它，bars≥16）
{"tool":"music_start","path":"s","tempo":150} 逐小节手写起点（仅当用户明确要求自己创作旋律时用）
{"tool":"music_bar","chord":"Am","notes":[{"pitch":69,"dur":1}]} 逐小节：写一小节
{"tool":"music_notes","notes":[{"pitch":72,"dur":0.5}]} 逐小节：追加音符
{"tool":"music_render"} 逐小节：渲染成MP3
{"tool":"music_import","path":"a.mid"} 把现成 MIDI 转成 FC 音色 MP3
{"tool":"ask","question":"..."} 问用户
{"tool":"final","answer":"..."} 结束并回答

规则：
- 算数一律用 calc，不要心算
- 数学应用题：先设未知数、列方程，再交给工具
  （比例/倍分题设公比 k：用勾股或面积关系把 k 的平方解出来再代值，
    不要一上来就拉成小数硬算）
- 能符号化简就先化简（√a×√a=a；k 带根号时先算 k²，别拉成小数硬算）
- 找大文件用 du，不要拼 shell 管道
- 写脚本后用 shell 跑：python3 x.py / node x.js
- pitch 是 MIDI 号：C4=60 E4=64 G4=67 A4=69 C5=72
- /etc 等系统目录不能写；要改先 ask
- 数据已经在上面的工具结果里就别再调同一个工具，重复调用是浪费时间
- 一个任务通常 2-3 步：拿数据 → 算/写 → final。算完立刻 final
- 完成必须用 final 收尾，answer 里不要编造没做到的事
"""


# ----------------------------------------------------------------- protocol
def _repair_json(block):
    """修复小模型最常见的 JSON 语法错误，省掉一整轮重试。

    实测 3B 的典型写法：
      {"a":1,}                    尾逗号
      {"a":"x"}                   少右括号
      {"a"："中文冒号"}            全角标点
      {"a":"多行\n内容"}           真换行没转义
    """
    s = block
    # 全角标点 → 半角（模型混用中英文标点非常频繁）
    s = (s.replace("“", '"').replace("”", '"')
          .replace("‘", "'").replace("’", "'")
          .replace("：", ":").replace("，", ",")
          .replace("【", "[").replace("】", "]"))
    # 字符串里的裸换行 → \n（否则 JSON 直接非法）
    out, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            if esc:
                esc = False
                out.append(ch)
            elif ch == "\\":
                esc = True
                out.append(ch)
            elif ch == '"':
                in_str = False
                out.append(ch)
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\t":
                out.append("\\t")
            elif ch == "\r":
                pass
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_str = True
            out.append(ch)
    s = "".join(out)
    # 尾逗号
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    # 结构位置上的孤立单引号：3B 会把 "answer":"'内容"'} 混着写，
    # 字符串已经闭合了后面还多一个 '，JSON 直接非法
    s = re.sub(r'(?<=[}\]"])\s*\'(?=\s*[}\]])', "", s)
    s = re.sub(r'([{,]\s*)\'(?=")', r"\1", s)
    # 括号配平：必须在字符串外统计。3B 会写出 ...]}" 这种尾巴，
    # 字符串里的 ] 会让朴素 count() 看起来"平衡"，其实结构是缺的。
    curly = sq = 0
    in_str = esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            curly += 1
        elif ch == "}":
            curly -= 1
        elif ch == "[":
            sq += 1
        elif ch == "]":
            sq -= 1
    if in_str:
        # 3B 常写成 {"answer":"'内容'"} —— 引号没闭合，而且 } 被写进了字符串里。
        # 把尾部的 }/] 移到字符串外面，再补引号；同时同步扣掉计数，
        # 否则下面还会再补一个 }，变成 ...'"}} 又非法了。
        tail = ""
        while s and s[-1] in "}]":
            if s[-1] == "}":
                curly -= 1
            else:
                sq -= 1
            tail = s[-1] + tail
            s = s[:-1]
        s += '"' + tail
    if sq > 0:
        s += "]" * sq
    if curly > 0:
        s += "}" * curly
    return s


def _parse_loose(block):
    """尽量把一段文本解析成 JSON：先原样，再修复，最后从尾部往前试截断。

    实测 3B 会写出这种尾巴：{"values":[8,3.7,1.8]}"]}
    （多了一对引号），原样和常规修复都过不了，但截到最后一个 } 就行。
    """
    try:
        return json.loads(block)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_repair_json(block))
    except json.JSONDecodeError:
        pass
    for cut in range(len(block) - 1, max(len(block) // 2, 1), -1):
        if block[cut] in "}]":
            try:
                return json.loads(_repair_json(block[:cut + 1]))
            except json.JSONDecodeError:
                continue
    return None


def extract_jsons(text):
    """提取文本里所有平衡的 JSON 对象。

    3B 经常一口气输出多个动作：
      {"tool":"write","path":"a.md","content":".."} {"tool":"write","path":"b.md",...}
    只取第一个会丢掉后面的内容，所以全解析、按顺序执行。
    """
    results = []
    i = 0
    while i < len(text):
        start = text.find("{", i)
        if start == -1:
            break
        depth = 0
        in_str = False
        esc = False
        end = -1
        for j in range(start, len(text)):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end == -1:
            # 括号没闭合（模型少写 }）：拿剩余部分补括号再试一次
            parsed = _parse_loose(text[start:])
            if parsed is not None:
                results.append(parsed)
            break
        block = text[start:end + 1]
        parsed = _parse_loose(block)
        if parsed is not None:
            results.append(parsed)
        i = end + 1
    return results


def extract_json(text):
    """单个 JSON（保留旧调用点用）。"""
    objs = extract_jsons(text)
    return objs[0] if objs else None


# llama.cpp 的 response_format + json_schema 会把 schema 编译成 GBNF grammar，
# 在采样阶段就禁掉非法 token —— 模型物理上写不出坏 JSON。这比事后正则修复靠谱得多：
# 正则只能救已知的错误形态，grammar 从根上不产生错误。
AGENT_SCHEMA = {
    "type": "object",
    "properties": {
        "tool": {"type": "string",
                 "enum": ["list", "du", "read", "write", "pptx", "midi",
                          "music_start", "music_bar", "music_notes",
                          "music_render", "music_import",
                          "sysinfo", "http_get", "download", "grep",
                          "math", "solve", "table", "shell", "calc",
                          "ask", "final"]},
        "think": {"type": "string"},
        "problem": {"type": "string"},
        "equations": {"type": "array", "items": {"type": "string"}},
        "variables": {"type": "array", "items": {"type": "string"}},
        "seconds": {"type": "number"},
        "sort_by": {"type": "string"},
        "group_by": {"type": "string"},
        "sum_by": {"type": "string"},
        "sections": {"type": "array", "items": {"type": "object"}},
        "limit": {"type": "number"},
        "desc": {"type": "boolean"},
        "path": {"type": "string"},
        "content": {"type": "string"},
        "cmd": {"type": "string"},
        "expr": {"type": "string"},
        "question": {"type": "string"},
        "answer": {"type": "string"},
        "chord": {"type": "string"},
        "url": {"type": "string"},
        "max_chars": {"type": "integer"},
        "pattern": {"type": "string"},
        "max_hits": {"type": "integer"},
        "max_mb": {"type": "integer"},
        "notes": {"type": "array",
                  "items": {"type": "object",
                            "properties": {"pitch": {"type": "integer"},
                                           "dur": {"type": "number"}},
                            "required": ["pitch", "dur"]}},
        "style": {"type": "string",
                  "enum": ["calm", "happy", "sad", "epic", "jazz", "fc_action"]},
        "tempo": {"type": "integer"},
        "bars": {"type": "integer"},
        "tracks": {"type": "array",
                   "items": {"type": "string",
                             "enum": ["piano", "guitar", "bass", "strings",
                                      "organ", "pad", "flute", "lead",
                                      "drums", "melody"]}},
        "slides": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string",
                             "enum": ["cover", "bullets", "chart", "table"]},
                    "title": {"type": "string"},
                    "subtitle": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                    "chartType": {"type": "string",
                                  "enum": ["bar", "pie", "line", "doughnut"]},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "values": {"type": "array", "items": {"type": "number"}},
                    "headers": {"type": "array", "items": {"type": "string"}},
                    "rows": {"type": "array",
                             "items": {"type": "array",
                                       "items": {"type": "string"}}},
                },
            },
        },
    },
    "required": ["tool"],
    "additionalProperties": False,
}

USE_SCHEMA = os.environ.get("AGENT_JSON_SCHEMA", "1") not in ("0", "false", "no")


def chat(model, messages, max_tokens=None, use_schema=True):
    if max_tokens is None:
        # 3B 生成慢（1.8 t/s），普通工具调用只需几十 token，给多了纯浪费。
        # 但 256 太紧：final 里要列 10 条视频标题就会写到一半被截断，
        # 变成不完整 JSON 直接解析失败（实测 "…| 231." 就断了）。
        head = " ".join(str(m.get("content") or "")[:400] for m in messages[:2])
        # 文档任务只要复述表格 → 512 够了；768 在 load 4 的板子上会跑到 400 秒+
        # 直接撞客户端 timeout（实测 "请求失败: timed out"）
        heavy = any(k in head for k in ("ppt", "PPT", "幻灯片", "图表", "chart",
                                        "表格", "uploads/", "总结", "列出", "TOP",
                                        "BOTTOM", "最低", "最高", "排名"))
        max_tokens = 512 if heavy else 448
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2,          # agent 要确定性
        "top_p": 0.9,
        "repeat_penalty": 1.15,
    }
    if USE_SCHEMA and use_schema:
        payload["response_format"] = {"type": "json_object", "schema": AGENT_SCHEMA}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        API + "/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # 老版本 llama.cpp 不认 response_format：去掉约束重试一次
        if USE_SCHEMA and exc.code == 400:
            payload.pop("response_format", None)
            req = urllib.request.Request(
                API + "/v1/chat/completions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read())
        else:
            raise
    return data["choices"][0]["message"]["content"]


# ------------------------------------------------------------------- tools
def _allowed(cmd):
    """检查每段管道的第一个可执行文件名（按 basename，容忍 /bin/ls），
    并拒绝命令拼接/重定向/替换字符。"""
    if SHELL_DANGER.search(cmd):
        return False, "命令里不能有 ; & < > 反引号 $() 或换行"
    for seg in cmd.split("|"):
        words = seg.strip().split()
        if not words:
            continue
        base = os.path.basename(words[0])
        # 危险的参数组合（find -delete / sed -i / awk system 都等于任意破坏）
        for bad in SHELL_DENY_ARGS.get(base, ()):
            if any(bad in w for w in words[1:]):
                return False, "%s 不允许 %s 参数" % (base, bad)
        # python 允许跑 .py 脚本（板上 numpy 已装，要拿它算矩阵/做数据处理）。
        # 但不许 -c / -m：那等于任意代码/任意模块，白名单就形同虚设。
        if base in ("python", "python3"):
            rest = words[1:]
            if any(a.startswith("-") for a in rest):
                return False, "python 只允许执行 .py 文件，不支持 -c/-m"
            if not any(a.endswith(".py") for a in rest):
                return False, "python 后面要跟 .py 文件名"
            continue
        # node 同理：只跑 .js 文件，不给 -e/-p（等于任意代码）
        if base in ("node", "npx"):
            rest = words[1:]
            if any(a.startswith("-") for a in rest):
                return False, "node 只允许执行 .js 文件，不支持 -e/-p"
            if not any(a.endswith((".js", ".mjs", ".cjs")) for a in rest):
                return False, "node 后面要跟 .js 文件名"
            continue
        # npm 只放只读子命令（install 会写 node_modules）
        if base == "npm":
            rest = words[1:]
            if not rest or rest[0] not in ("ls", "list", "view", "outdated",
                                           "--version", "-v", "root"):
                return False, "npm 只允许 ls/view/outdated 这类只读子命令"
            continue
        if not SHELL_ALLOW.match(base):
            return False, "不允许的命令 %r" % words[0]
    return True, ""


def run_shell(cmd):
    cmd = (cmd or "").strip()
    if not cmd:
        return "错误：cmd 为空"
    ok, why = _allowed(cmd)
    if not ok:
        return ("错误：%s。可用：ls cat head tail wc grep find du df free uname "
                "uptime ps date echo stat file which sort uniq cut tr awk sed id "
                "pwd tree + python3 xxx.py / node xxx.js / jq，可以用 | 管道。"
                "直接写命令名（ls /tmp，不要写 /bin/ls）。" % why)
    try:
        _ensure_workdir()
        # cwd 必须是工作目录：模型先前用 write 写的脚本落在 WORK_DIR，
        # 这里若用 agent 自己的 cwd，python3 xxx.py 就会报文件不存在。
        r = subprocess.run(["sh", "-c", cmd], capture_output=True,
                           text=True, timeout=30, cwd=WORK_DIR)
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        return out.strip() or "(无输出)"
    except subprocess.TimeoutExpired:
        return "错误：命令超时（20s）"
    except Exception as exc:                                   # noqa: BLE001
        return "错误：%s" % exc


def run_download(url, path=None, max_mb=20):
    """下载文件到工作目录（http_get 是"看内容"，这个是"存下来"）。

    用途：抓图片、下 json/csv、拉小模型文件。
    限制：默认 20MB 上限（板上内存紧，别下大文件）。
    """
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "错误：只支持 http:// 或 https:// 开头的地址"
    try:
        limit = max(1, min(int(max_mb or 20), 100)) * 1024 * 1024
    except (TypeError, ValueError):
        limit = 20 * 1024 * 1024

    import urllib.error
    import urllib.request
    name = str(path or "").strip()
    if not name:
        name = os.path.basename(url.split("?")[0].rstrip("/")) or "download.bin"
    dst = _resolve(name)
    err = _guard_write(dst)
    if err:
        return err
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (agent)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = 0
            with open(dst, "wb") as fh:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        fh.close()
                        try:
                            os.unlink(dst)
                        except OSError:
                            pass
                        return ("错误：文件超过 %d MB 上限（板上内存紧，"
                                "真要大文件请在电脑上下）" % (limit // 1048576))
                    fh.write(chunk)
            status = resp.getcode()
    except urllib.error.HTTPError as exc:
        return "错误：HTTP %s（%s）" % (exc.code, url)
    except Exception as exc:                                   # noqa: BLE001
        return "错误：下载失败 %s" % exc
    _prune_workdir()
    return ("已下载 %s（%d 字节，HTTP %s）→ %s"
            % (os.path.basename(dst), total, status, dst))


def run_grep(pattern, path=".", max_hits=30):
    """在目录里搜内容（模型不会拼 grep -rn，给个一步到位的）。"""
    pattern = str(pattern or "").strip()
    if not pattern:
        return "错误：pattern 为空"
    try:
        p = _resolve(path or ".")
    except Exception:                                          # noqa: BLE001
        p = WORK_DIR
    if not os.path.exists(p):
        return "错误：路径不存在 %s" % p
    try:
        r = subprocess.run(["grep", "-rn", "--binary-files=without-match",
                            pattern, p],
                           capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return "错误：搜索超时（20s），换个更具体的关键词或子目录"
    except Exception as exc:                                   # noqa: BLE001
        return "错误：%s" % exc
    lines = (r.stdout or "").splitlines()
    if not lines:
        return "没有匹配内容（grep %s in %s）" % (pattern, p)
    try:
        cap = max(5, min(int(max_hits or 30), 100))
    except (TypeError, ValueError):
        cap = 30
    shown = lines[:cap]
    return ("%d 处匹配（显示前 %d）:\n%s%s"
            % (len(lines), len(shown),
               "\n".join(x[:180] for x in shown),
               " …" if len(lines) > len(shown) else ""))


def run_sysinfo():
    """一块板子最常被问到的信息，一次给全（比让模型拼 5 个命令省 4 轮）。"""
    out = []
    try:
        import glob
        freqs = []
        for p in sorted(glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq")):
            with open(p) as fh:
                freqs.append(int(fh.read().strip()) // 1000)
        if freqs:
            out.append("CPU 频率: %s MHz（%d 核）"
                       % ("/".join(str(x) for x in freqs), len(freqs)))
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/loadavg") as fh:
            out.append("负载: %s" % " ".join(fh.read().split()[:3]))
    except OSError:
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2:
                    mem[parts[0].rstrip(":")] = int(parts[1])
        if mem:
            out.append("内存: %d MB 总 / %d MB 可用"
                       % (mem.get("MemTotal", 0) // 1024,
                          mem.get("MemAvailable", 0) // 1024))
    except (OSError, ValueError):
        pass
    try:
        import glob
        temps = []
        for p in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
            with open(p) as fh:
                v = int(fh.read().strip())
            if v > 0:
                temps.append(v / 1000.0)
        if temps:
            out.append("温度: %s" % ", ".join("%.1fC" % t for t in temps[:4]))
    except (OSError, ValueError):
        pass
    for mount, label in (("/", "系统盘"), ("/tmp", "/tmp"), ("/overlay", "overlay")):
        try:
            st = os.statvfs(mount)
            out.append("%s: %.1f GB 总 / %.1f GB 可用"
                       % (label, st.f_blocks * st.f_frsize / 1e9,
                          st.f_bavail * st.f_frsize / 1e9))
        except OSError:
            pass
    try:
        with open("/proc/uptime") as fh:
            out.append("已运行: %.1f 小时" % (float(fh.read().split()[0]) / 3600.0))
    except (OSError, ValueError):
        pass
    return "\n".join(out) if out else "错误：读不到系统信息"


def run_http_get(url, max_chars=1500):
    """抓网页或 API（只读 GET）。

    板上只有 wget，这里用 urllib 更可控（能设 UA、能截断、能报状态码）。
    """
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "错误：只支持 http:// 或 https:// 开头的地址"
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (agent)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read(600 * 1024)          # 最多读 600KB
            status = resp.getcode()
            ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return "错误：HTTP %s（%s）" % (exc.code, url)
    except Exception as exc:                                   # noqa: BLE001
        return "错误：抓取失败 %s" % exc
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:                                          # noqa: BLE001
        return "错误：内容无法解码（%s）" % ctype
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text,
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    n = max(200, min(int(max_chars or 1500), 4000))
    return ("HTTP %s  %s  %d 字节\n%s%s"
            % (status, ctype or "-", len(raw), text[:n],
               " …（已截断）" if len(text) > n else ""))


def run_list(path="."):
    """列目录，带大小。小模型不擅长 ls|xargs|du 这种组合，给个一步到位的工具。"""
    try:
        p = os.path.expanduser(path or ".")
        # 模型常把文件当目录列（实测 list 一个 .txt 直接报 "Not a directory"，
        # 用户就看到一串乱码错误）→ 自动转成读文件
        if os.path.isfile(p):
            return ("（%s 是文件不是目录，已自动改用 read）\n%s"
                    % (os.path.basename(p), run_read(p)))
        items = []
        for name in os.listdir(p):
            fp = os.path.join(p, name)
            try:
                is_dir = os.path.isdir(fp)
                items.append((os.path.getsize(fp) if not is_dir else 0,
                              name + ("/" if is_dir else ""),
                              "dir" if is_dir else "file"))
            except OSError:
                pass
        items.sort(reverse=True)
        if not items:
            return "(空目录)"
        return "\n".join("%-9s %10d  %s" % (t, s, n) for s, n, t in items[:40])
    except Exception as exc:                                   # noqa: BLE001
        return "错误：%s" % exc


def run_du(path):
    """目录内容按总大小排序，直接回答"哪个最大"。"""
    try:
        p = os.path.expanduser(path or ".")
        items = []
        for name in os.listdir(p):
            fp = os.path.join(p, name)
            try:
                if os.path.isdir(fp):
                    total = 0
                    for root, _dirs, files in os.walk(fp):
                        for f in files:
                            try:
                                total += os.path.getsize(os.path.join(root, f))
                            except OSError:
                                pass
                    items.append((total, name + "/", "dir"))
                else:
                    items.append((os.path.getsize(fp), name, "file"))
            except OSError:
                pass
        items.sort(reverse=True)
        if not items:
            return "(空目录)"
        return "按大小排序（前 20）：\n" + "\n".join(
            "%-9s %10d  %s" % (t, s, n) for s, n, t in items[:20])
    except Exception as exc:                                   # noqa: BLE001
        return "错误：%s" % exc


def run_read(path):
    try:
        p = _find_upload(path)
        with open(p, "rb") as fh:
            head = fh.read(4096)
        # 小模型看到大文件就去 read，把 NUL 字节灌满上下文。直接拒绝二进制。
        if b"\0" in head[:2048]:
            return ("错误：%s 是二进制文件，不要 read。如果只想知道大小，"
                    "用 list 或 du 就够了。" % p)
        with open(p, "r", errors="replace") as fh:
            full = fh.read()
        data = full[:4000]
        if len(full) > 4000:
            data += ("\n\n…（文件共 %d 字，只显示前 4000 字。"
                     "想看后面的用 grep 搜关键词，或分段读）" % len(full))
        return data or "(空文件)"
    except Exception as exc:                                   # noqa: BLE001
        return "错误：%s" % exc


# 这些是二进制/容器格式，模型只能往里写纯文本 → 必然产出垃圾文件。直接拒绝。
BINARY_EXT = {
    ".ppt", ".pptx", ".doc", ".docx", ".xls", ".xlsx", ".pdf",
    ".zip", ".gz", ".tar", ".7z", ".rar", ".png", ".jpg", ".jpeg",
    ".gif", ".mp3", ".mp4", ".mkv", ".gguf", ".img", ".bin", ".so",
}


WORK_DIR = os.environ.get("AGENT_WORKDIR", "/tmp/llama_agent")
MAX_WORK_FILES = 24              # 工作目录最多留这么多个文件
MAX_WORK_MB = 200                # 以及总大小上限


def _ensure_workdir():
    try:
        os.makedirs(WORK_DIR, exist_ok=True)
    except OSError:
        pass


def _classify_task(task):
    """任务类型分类（统一入口）。

    之前判定散在 6 个 _looks_like_* 里，顺序靠 if/elif 隐式保证，
    出问题时看不出"它到底认成了什么"。这里集中判定并把结果打出来。
    顺序是有意的：专用工具优先（音乐/PPT 有独立产物），
    文档其次（要读上传），图形直接拒，数学分方程/计算两路，最后才是通用。
    """
    t = str(task or "")
    if _looks_like_music(t):
        return "music", "音乐"
    if _looks_like_ppt(t):
        return "ppt", "PPT"
    if _looks_like_doc_task(t):
        return "doc", "文档/表格"
    if _looks_like_visual(t):
        return "visual", "图形推理"
    if _looks_like_math(t):
        kind = _math_kind(t)
        return ("compute" if kind == "compute" else "solve"), \
               ("多步计算" if kind == "compute" else "方程应用题")
    return "general", "通用"


_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100, "千": 1000}

# 题干里的数字：阿拉伯或中文（"七百二十"、八十、三天）
_NUM_PAT = r"(\d+(?:\.\d+)?|[一二两三四五六七八九十百千零]{1,8})"


def _cn_num_all(s):
    """中文/阿拉伯数字统一转 float。

    '七百二十' → 720，'一百零五' → 105，'十二' → 12，'3' → 3，'3.5' → 3.5。
    用户口述或语音输入时数字常是中文写法，只认 \\d+ 会整题漏掉。
    """
    s = str(s or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    total = section = num = 0
    for ch in s:
        if "0" <= ch <= "9":
            num = num * 10 + int(ch)
        elif ch in _CN_DIGIT:
            d = _CN_DIGIT[ch]
            if d >= 10:
                # 十/百/千 都是累加，不能拿 section 去乘 ——
                # 之前写成 section = (section + num) * d，
                # "一千二百" 会变成 (1000+2)*100 = 100200
                section += (num or 1) * d
                num = 0
            else:                              # 零~九
                num = d
        else:
            return None
    return float(section + num)


def _num_at(m, grp=1):
    """从正则匹配对象里取数字（兼容阿拉伯/中文）。grp 指定第几个分组。"""
    if not m:
        return None
    try:
        return _cn_num_all(m.group(grp))
    except (IndexError, TypeError):
        return None


def _pct_at(s):
    """'百分之十' / '20%' → 0.1 / 0.2。"""
    t = str(s or "")
    m = re.search(r"百分之\s*([一二两三四五六七八九十百零\d]+)", t)
    if m:
        v = _cn_num_all(m.group(1))
        return v / 100.0 if v else None
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
    if m:
        return float(m.group(1)) / 100.0
    return None


def _solve_plan_vs_actual(task):
    """「计划 vs 实际」类题的模板方程（覆盖几种常见变体）。

    3B 翻译这类题的方程实测 0/3（"还差 X 时提前 N 天"这个复合条件必错），
    但题型结构固定，从题干抠量就能直接写方程：

      A 原计划每天 a，实际多 b，还差 c 时提前 n 天 → a*t = (a+b)*(t-n) + c
      B 原计划每天 a，实际少 b，还差 c 时延后 n 天 → a*t = (a-b)*(t+n) + c
      C 原计划每天 a，实际多 b，提前 n 天完成      → a*t = (a+b)*(t-n)
      D 原计划每天 a，做了 n 天后还剩 c            → a*t = a*n + c

    返回 (方程, 原计划每天量, 说明) 或 None。
    """
    t = str(task or "")
    m1 = re.search(r"原计划\s*每天\s*(?:修|做|生产|加工|铺|种|读|写)?\s*" + _NUM_PAT, t)
    if not m1:
        return None
    a = _num_at(m1)

    # 纯天数差（必须在"多/少"之前判："比计划少 3 天"里的"少"不是"少修"）
    m_used = re.search(r"实际(?:用|花|需要)?了?\s*(\d+(?:\.\d+)?)\s*天", t)
    m_diff = re.search(r"比计划\s*(少|多)\s*(?!修|做|生产|加工)(?:用|花|需要)?了?\s*"
                       r"(\d+(?:\.\d+)?)\s*天", t)
    if m_used and m_diff:
        sign2 = "-" if m_diff.group(1) == "少" else "+"
        eq = "t %s %s = %s" % (sign2, m_diff.group(2), m_used.group(1))
        return eq, a, "实际 %s 天，比计划%s %s 天" % (
            m_used.group(1), m_diff.group(1), m_diff.group(2))

    m_dir = re.search(r"(多|少)\s*(?:修|做|生产|加工|铺|种|读|写)?\s*" + _NUM_PAT, t)
    b = _num_at(m_dir, 2)
    sign = "+" if (m_dir and m_dir.group(1) == "多") else "-"

    m_left = re.search(r"(?:还差|还剩|剩下|剩余|剩)\s*" + _NUM_PAT, t)
    c = _num_at(m_left)

    m_day = re.search(r"(提前|延后|晚|晚完)\s*" + _NUM_PAT + r"\s*天", t)
    n = _num_at(m_day, 2)
    late = bool(m_day and m_day.group(1) in ("延后", "晚", "晚完"))

    m_ran = re.search(r"(?:已经)?(?:修|做|生产|加工)了?\s*" + _NUM_PAT + r"\s*天", t)

    # E：完成了计划的 m/n，还剩 c（不用"多/少"）
    m_frac = re.search(r"完成(?:了)?(?:计划|全部|任务|工程)?的?\s*(\d+)\s*/\s*(\d+)", t)
    if m_frac and c is not None:
        mm, nn = m_frac.group(1), m_frac.group(2)
        eq = "%g*t*%s/%s + %g = %g*t" % (a, mm, nn, c, a)
        return eq, a, "完成 %s/%s 时还剩 %g" % (mm, nn, c)
    # E1b：中文分数「五分之三」
    if c is not None:
        m_cnf = re.search(r"完成(?:了)?(?:计划|全部任务|全部|任务|工程)?的?\s*"
                          r"([一二两三四五六七八九十]分之[一二两三四五六七八九十])", t)
        _fr = _cn_frac(m_cnf.group(1)) if m_cnf else None
        if _fr:
            eq = "%g*t*%d/%d + %g = %g*t" % (a, _fr[0], _fr[1], c, a)
            return eq, a, "完成 %d/%d 时还剩 %g" % (_fr[0], _fr[1], c)

    # E2："完成一半"（口语写法，不是 1/2）
    if c is not None and re.search(r"(?:完成|做完|修完)(?:了)?(?:计划|全部|任务|工程)?的?一半", t):
        eq = "%g*t*1/2 + %g = %g*t" % (a, c, a)
        return eq, a, "完成一半时还剩 %g" % c

    # 只有天数差已在前面判定（要早于"多/少"）

    # F：前 k 天按原计划，之后每天多/少 b，提前/延后 n 天
    # (?<!提) 是必须的："提前 3 天"里也含"前 3 天"，不加排除会把原题 A 算成 F
    m_first = re.search(r"(?<!提)前\s*" + _NUM_PAT + r"\s*天", t)
    if m_first and b is not None and n is not None:
        k = _num_at(m_first)
        if late:
            tail = "(t-%g+%g)" % (k, n)
        else:
            tail = "(t-%g-%g)" % (k, n)
        eq = "%g*%g + (%g%s%g)*%s = %g*t" % (a, k, a, sign, b, tail, a)
        return eq, a, "前 %g 天按计划，之后每天%s %g，%s %g 天" % (
            k, "多" if sign == "+" else "少", b, "延后" if late else "提前", n)

    # D：做/修了 n 天还剩 c（最直接，不需要"多/少"）
    if m_ran and c is not None and b is None:
        k = _num_at(m_ran)
        eq = "%g*t = %g*%g + %g" % (a, a, k, c)
        return eq, a, "做了 %g 天还剩 %g" % (k, c)

    if b is None:
        return None

    # A/B：有"还差/还剩 c" + 提前/延后 n
    if c is not None and n is not None:
        tpart = "(t+%g)" % n if late else "(t-%g)" % n
        eq = "%g*t = (%g%s%g)*%s + %g" % (a, a, sign, b, tpart, c)
        return eq, a, "%s %g 天，还剩 %g" % ("延后" if late else "提前", n, c)

    # C：只提前/延后，没有剩余量
    if n is not None and c is None:
        tpart = "(t+%g)" % n if late else "(t-%g)" % n
        eq = "%g*t = (%g%s%g)*%s" % (a, a, sign, b, tpart)
        return eq, a, "%s %g 天完成" % ("延后" if late else "提前", n)

    return None


_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_frac(s):
    """「五分之三」→ (3, 5)，「三分之二」→ (2, 3)，「二分之一」→ (1, 2)。

    中文分数的说法是"分母分之分子"，和 3/5 的顺序正好反过来。
    用户实测用"五分之三"提问，只认阿拉伯 3/5 会漏，于是算成"合作完成全部"。
    """
    m = re.search(r"([一二两三四五六七八九十])\s*分之\s*([一二两三四五六七八九十])",
                  str(s or ""))
    if not m:
        return None
    d = _CN_NUM.get(m.group(1))
    n = _CN_NUM.get(m.group(2))
    if d and n:
        return n, d
    return None


def _solve_collab(task):
    """合作/单独完成类：甲 a 天、乙 b 天，合作需 t 天 → t*(1/a + 1/b) = 1。

    3B 对这类题常把时间相加或取平均（实测错），sympy 一步到位。
    支持"完成 3/5"（→ = 3/5）。
    """
    t = str(task or "")
    m1 = re.search(r"甲[^，。；\d]{0,8}?" + _NUM_PAT + r"\s*天", t)
    m2 = re.search(r"乙[^，。；\d]{0,8}?" + _NUM_PAT + r"\s*天", t)
    if not (m1 and m2):
        return None
    a, b = _num_at(m1), _num_at(m2)
    if not (a and b):
        return None
    # 整数别带 .0（"甲 12.0 天"看着别扭）
    if a == int(a):
        a = int(a)
    if b == int(b):
        b = int(b)
    m_f = re.search(r"完成(?:了)?(?:全部|任务|工程)?的?\s*(\d+)\s*/\s*(\d+)", t)
    if m_f:
        mm, nn = m_f.group(1), m_f.group(2)
    else:
        # 中文分数："完成全部任务的五分之三"（分母在前，和 3/5 反过来）
        m_cn = re.search(r"完成(?:了)?(?:全部任务|全部|任务|工程)?的?\s*"
                         r"([一二两三四五六七八九十]分之[一二两三四五六七八九十])", t)
        _fr = _cn_frac(m_cn.group(1)) if m_cn else None
        if _fr:
            mm, nn = str(_fr[0]), str(_fr[1])
        else:
            return ("t*(1/%s + 1/%s) = 1" % (a, b),
                    "甲 %s 天、乙 %s 天，合作完成全部" % (a, b))
    return ("t*(1/%s + 1/%s) = %s/%s" % (a, b, mm, nn),
            "甲 %s 天、乙 %s 天，合作完成 %s/%s" % (a, b, mm, nn))


def _solve_collab_segmented(task):
    """合作中途有人离开：合作 N 天后乙走，甲独自做完。

        N*(1/a + 1/b) + (t-N)*(1/a) = 1

    这类题 3B 必错（它会把两段工作量的分母搞混）。
    """
    t = str(task or "")
    m1 = re.search(r"甲[^，。；\d]{0,8}?(\d+(?:\.\d+)?)\s*天", t)
    m2 = re.search(r"乙[^，。；\d]{0,8}?(\d+(?:\.\d+)?)\s*天", t)
    m3 = re.search(r"合作\s*(\d+(?:\.\d+)?)\s*天\s*(?:后)?[^，。；]{0,8}?"
                   r"(乙|甲)\s*(?:离开|走|退出|调走)", t)
    if not (m1 and m2 and m3):
        return None
    a, b, k = m1.group(1), m2.group(1), m3.group(1)
    left = m3.group(2)
    stay, gone = (a, b) if left == "乙" else (b, a)
    eq = "%s*(1/%s + 1/%s) + (t-%s)*(1/%s) = 1" % (k, a, b, k, stay)
    return (eq, "甲 %s 天、乙 %s 天，合作 %s 天后%s离开，剩一人做完"
            % (a, b, k, left))


def _fix_mojibake(s):
    """修复"中文被当 latin-1 解码"的乱码。

    实测用户输入偶尔变成 "ÇëÐ´Ò»¸ö mid bgm"（GBK 字节被按 latin-1 解），
    这时所有关键词识别全废 —— 音乐任务会被当成通用任务、表格任务识别不到列名。
    这里尝试 latin-1 → gbk 还原，还原后中文明显变多才采用。
    """
    t = str(s or "")
    if not t:
        return t
    # 只有出现 latin-1 高位字符序列才值得尝试
    if not re.search(r"[\u00c0-\u00ff]{2,}", t):
        return t
    try:
        fixed = t.encode("latin-1", "ignore").decode("gbk", "ignore")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return t
    cn_before = len(re.findall(r"[\u4e00-\u9fff]", t))
    cn_after = len(re.findall(r"[\u4e00-\u9fff]", fixed))
    if cn_after > cn_before + 1:
        return fixed
    # 再试 utf-8（另一种常见错法）
    try:
        fixed2 = t.encode("latin-1", "ignore").decode("utf-8", "ignore")
        if len(re.findall(r"[\u4e00-\u9fff]", fixed2)) > cn_before + 1:
            return fixed2
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass
    return t


def _today_dir():
    """输出按日期分目录（YYYYMMDD），方便回看某天做了什么。

    上传文件例外：仍放 WORK_DIR/uploads/，不分日期 ——
    任务可能跨日引用昨天的上传（第二天接着问同一份表）。
    """
    d = os.path.join(WORK_DIR, datetime.date.today().strftime("%Y%m%d"))
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return WORK_DIR
    return d


def _resolve(path):
    """相对路径统一落到工作目录。

    工作目录默认 /tmp/llama_agent —— /tmp 是 tmpfs，重启自然清空，
    不会像以前那样把音频/PPT 堆在 overlay 上越积越多。
    绝对路径保持不变（调用方明确指定就尊重）。
    """
    p = os.path.expanduser(str(path or "").strip())
    if not p:
        return WORK_DIR
    if os.path.isabs(p):
        return p
    _ensure_workdir()
    # uploads/ 跨日共享（任务可能引用昨天的上传），不进日期目录
    if p == "uploads" or p.startswith("uploads/") or p.startswith("uploads" + os.sep):
        return os.path.join(WORK_DIR, p)
    # 其它输出按日期分目录，方便回看
    return os.path.join(_today_dir(), p)


def _prune_workdir():
    """按数量和总大小清理工作目录，防止几百 MB 的 wav 把 tmpfs 吃光。

    现在输出按 YYYYMMDD 分目录，所以要扫两层：根目录的文件 +
    各日期目录里的文件（统一按数量/总量收敛）。
    """
    try:
        dirs = [WORK_DIR]
        try:
            for d in os.listdir(WORK_DIR):
                dp = os.path.join(WORK_DIR, d)
                if (os.path.isdir(dp) and len(d) == 8 and d.isdigit()):
                    dirs.append(dp)
        except OSError:
            pass
        entries = []
        for base in dirs:
            try:
                for name in os.listdir(base):
                    fp = os.path.join(base, name)
                    if os.path.isfile(fp):
                        entries.append((os.path.getmtime(fp), os.path.getsize(fp), fp))
            except OSError:
                pass
        entries.sort(reverse=True)                 # 最新的在前
        total = sum(e[1] for e in entries)
        for i, (_mt, sz, fp) in enumerate(entries):
            # 在配额内的保留（continue），超出的删掉（原版这里写成 break，
            # 第一个文件就退出循环，等于永远不清理）
            if i < MAX_WORK_FILES and total <= MAX_WORK_MB * 1024 * 1024:
                continue
            try:
                os.unlink(fp)
                total -= sz
            except OSError:
                pass
    except OSError:
        pass
    _prune_uploads()          # uploads/ 子目录从不在这层扫，单独清一遍


def _prune_uploads(max_keep=20):
    """清理 uploads/ 子目录。

    原来的 _prune_workdir 只扫 WORK_DIR 根目录的 isfile，
    uploads/ 这一层从来没被清过（实测堆到 14 个且只增不减）。
    /tmp 是 tmpfs，占的是内存，所以上传文件也要按数量收敛。
    """
    try:
        d = os.path.join(WORK_DIR, "uploads")
        items = []
        for f in os.listdir(d):
            p = os.path.join(d, f)
            try:
                items.append((os.path.getmtime(p), p))
            except OSError:
                pass
        items.sort(reverse=True)                   # 最新的在前
        for _, p in items[max_keep:]:
            try:
                os.unlink(p)
            except OSError:
                pass
    except OSError:
        pass


def _compress_wav(wav_path, bitrate=None):
    """WAV 压成 MP3 并删掉 WAV。

    FC 音源是简单方波/噪声，32~64kbps 单声道足够，实测 2.1MB → 385KB(64k)，
    编码 1.2 秒。没有 ffmpeg 就保留 WAV（音质无损失但占空间）。
    """
    if not wav_path or not os.path.isfile(wav_path):
        return wav_path, False
    if not shutil.which("ffmpeg"):
        return wav_path, False
    bitrate = bitrate or os.environ.get("AGENT_AUDIO_BITRATE", "64k")
    mp3 = os.path.splitext(wav_path)[0] + ".mp3"
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", wav_path,
             "-ac", "1", "-codec:a", "libmp3lame", "-b:a", bitrate, mp3],
            capture_output=True, timeout=240)
        if r.returncode == 0 and os.path.isfile(mp3) and os.path.getsize(mp3) > 0:
            try:
                os.unlink(wav_path)        # 删掉大文件
            except OSError:
                pass
            return mp3, True
    except (OSError, subprocess.SubprocessError):
        pass
    return wav_path, False


def _is_sensitive(path):
    """判断是否落在系统目录（/etc 等）。写这些地方必须先让用户确认。"""
    try:
        p = os.path.abspath(str(path))
    except (TypeError, ValueError):
        return False
    for d in SENSITIVE_DIRS:
        if p == d or p.startswith(d.rstrip("/") + "/"):
            return True
    return False


def _guard_write(path):
    """写文件前的系统目录检查，返回错误文案；None 表示可以写。

    /etc 这类地方 agent 一律不擅自动手 —— 要改配置，得让模型先 ask 用户，
    或者由用户自己执行。这条是用户明确要求的。
    """
    if _is_sensitive(path):
        return ("错误：%s 在系统目录（/etc /usr /rom /root 等），agent 不能直接写。"
                "如果这个修改确实必要，先用 ask 工具向用户确认，"
                "或者建议用户自己执行。" % path)
    return None


def run_write(path, content):
    try:
        path = str(path or "").strip()
        body = str(content or "")
        # 模型经常省略 path（schema 里不是必填），这时 _resolve("") 会落到工作目录本身，
        # 报 "Is a directory: /tmp/llama_agent"，后面 shell 再跑就找不到文件。
        if not path:
            # 注意 (?m) 必须放在正则最前面，Python 3.9 不认中间的 inline flag
            path = ("calc.py" if re.search(r"(?m)\bprint\(|^\s*[A-Za-z_]\w*\s*=", body)
                    else "out.txt")
        # 剥掉 markdown 围栏：模型爱把 ```python ... ``` 整段塞进 .py 文件
        body = re.sub(r"^\s*```[a-zA-Z0-9_+-]*\n", "", body)
        body = re.sub(r"\n```\s*$", "", body)
        # 模型偶尔写 @var（模仿占位符风格），Python 不认这种前缀；
        # 只去掉「@ + 字母」形式的，行中行首都要处理（print(@a+@b) 也会出现）
        body = re.sub(r"@([A-Za-z_]\w*)", r"\1", body)
        path = _resolve(path)
        err = _guard_write(path)
        if err:
            return err
        ext = os.path.splitext(path)[1].lower()
        if ext in BINARY_EXT:
            return ("错误：%s 是二进制/容器格式，不能用 write 塞纯文本。"
                    "要生成 PPT 请用 pptx 工具；.doc/.docx/.xls/.xlsx/.pdf 没有对应"
                    "工具，直接告诉用户做不到。" % ext)
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)      # 模型爱写 "目录/2.md" 这种路径
        with open(path, "w") as fh:
            fh.write(body)
        # 自验证：回读确认，把真实字节数和首行回给模型，
        # 免得它下一轮凭空编造"文件已写入 3000 字"
        try:
            size = os.path.getsize(path)
            with open(path, "r", errors="replace") as fh:
                head = fh.readline().strip()[:40]
            return "已写入 %s（%d 字节），回读首行: %s" % (
                path, size, head or "(空)")
        except OSError as exc:
            return "已写入 %s 但回读失败: %s" % (path, exc)
    except Exception as exc:                                   # noqa: BLE001
        return "错误：%s" % exc


def _enrich_slides(slides):
    """模型（尤其 3B）只会 cover + bullets，做出来永远一个套路。

    这里只在它**完全没用**丰富版式时补两页（章节页 + 结尾页），
    让整体结构有起伏；一旦它用了 section/two-col/quote 就不插手。
    """
    if not isinstance(slides, list) or not slides:
        return slides
    kinds = {str(s.get("type") or "").lower() for s in slides if isinstance(s, dict)}
    if kinds & {"section", "two-col", "twocol", "twocolumn", "quote"}:
        return slides

    out = []
    rest = slides
    if str(slides[0].get("type") or "").lower() == "cover":
        out.append(slides[0])
        rest = slides[1:]
    if rest:
        first_title = (rest[0].get("title") if isinstance(rest[0], dict) else "") or "正文"
        out.append({"type": "section", "title": first_title})
    out.extend(rest)
    if not any(str(s.get("type") or "").lower() in ("closing", "end", "thanks")
               for s in out if isinstance(s, dict)):
        out.append({"type": "closing", "title": "谢谢"})
    return out


def _midi_batched(model, path, bars, per_bar, tempo, say=None):
    """分批让模型写旋律（每批 4 小节），最后合并成一个 MIDI。

    为什么分批：原来让模型逐小节调 music_bar，每小节吃 2 步，
    4 步上限只做出 2 小节 8 个音。32 小节 × 6 音 = 192 个音符，
    一次写完必然超长；拆成 8 批（每批 4 小节 = 24 个音符）就都在短 JSON 里，
    工具负责把 8 批拼成一首完整的曲子。
    """
    from midiutil import MIDIFile

    total = max(1, int(bars))
    # 每小节 2 音实测太稀疏（用户："根本不好听"），下限抬到 4；
    # 上限 8，再密就是噪音了。
    per_bar = max(4, min(int(per_bar), 8))
    batch = 4
    got = {}
    for start in range(0, total, batch):
        n = min(batch, total - start)
        if say:
            say("  （第 %d-%d 小节，每小节 %d 音）" % (start + 1, start + n, per_bar))
        prompt = ("写 %d 小节旋律，每小节 %d 个音，A 小调。\n"
                  "只输出一个 JSON 数组，共 %d 个元素（每个元素是一小节）。\n"
                  "每个元素是该小节的音符数组，音符写成 {\"pitch\":音高,\"dur\":拍数}。\n"
                  "音高用 MIDI 编号（57=A3, 60=C4, 64=E4, 69=A4）。\n"
                  "例：[ [{\"pitch\":69,\"dur\":1},{\"pitch\":72,\"dur\":1}], "
                  "[{\"pitch\":67,\"dur\":2}] ]\n"
                  "不要用对象、不要用键名、不要解释。"
                  % (n, per_bar, n))
        raw = ""
        try:
            raw = chat(model, [
                {"role": "system", "content": "你只输出 JSON 数组。"},
                {"role": "user", "content": prompt},
            ], max_tokens=420, use_schema=False)
            # 直接 json.loads：模型已经把数组写对了。
            # 不能用 extract_json —— 它是"从混杂文本里抠第一个 JSON 对象"，
            # 遇到顶层数组时会只抠出第一个内层 {"pitch":...}（实测）。
            _txt = str(raw or "").strip()
            obj = json.loads(_txt)
        except Exception:                                  # noqa: BLE001
            m = re.search(r"\[[\s\S]*\]", str(raw or ""))
            try:
                obj = json.loads(m.group(0)) if m else None
            except Exception:                              # noqa: BLE001
                obj = None
        # 兼容几种形状：直接数组 / {"bars":[...]} / {"bars":{"0":[...]}}
        bars_obj = None
        if isinstance(obj, list):
            bars_obj = obj
        elif isinstance(obj, dict):
            bars_obj = obj.get("bars")
            if isinstance(bars_obj, dict):
                bars_obj = list(bars_obj.values())
        if not isinstance(bars_obj, list):
            continue
        for i, b in enumerate(bars_obj[:n]):
            notes_iter = b
            if isinstance(b, dict):
                # 模型给过 {"[0,1]": [...]} 这种，把里层数组拍平
                flat = []
                for v in b.values():
                    flat.extend(v if isinstance(v, list) else [v])
                notes_iter = flat
            if not isinstance(notes_iter, list):
                continue
            seq = []
            for note in notes_iter:
                if isinstance(note, str):
                    try:
                        note = extract_json(note)
                    except Exception:                      # noqa: BLE001
                        continue
                if not isinstance(note, dict):
                    continue
                try:
                    p = int(note.get("pitch"))
                    d = float(note.get("dur") or 0.5)
                except (TypeError, ValueError):
                    continue
                if 36 <= p <= 96 and 0.1 <= d <= 4:
                    seq.append((p, d))
            if seq:
                got[start + i] = seq

    if not got:
        return "错误：分批都没有拿到可用音符，未生成"

    mf = MIDIFile(1)
    mf.addTempo(0, 0, int(tempo or 120))
    t = 0.0
    n_notes = 0
    for bar in range(total):
        seq = got.get(bar) or []
        if not seq:
            t += 4.0
            continue
        step = 4.0 / len(seq)          # 把这一小节的音均摊到 4 拍
        for pitch, dur in seq:
            mf.addNote(0, 0, pitch, t, max(0.1, min(dur, step)), 92)
            n_notes += 1
            t += step
    outp = _resolve(path)
    parent = os.path.dirname(outp)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(outp, "wb") as fh:
        mf.writeFile(fh)
    return ("已生成 MIDI：%s（%d/%d 小节有内容，共 %d 个音符，"
            "分批创作后合并；%d BPM）"
            % (outp, len(got), total, n_notes, int(tempo or 120)))


def _make_checkpoint(messages):
    """把这一轮已完成的工具结果压成进度摘要。

    token 超预算不是任务失败，是上下文太满。清空 messages、只带上这份摘要
    重新开始，等于"开新一局但继承进度"。
    """
    got = []
    for m in messages:
        if m.get("role") != "user":
            continue
        c = str(m.get("content") or "").strip()
        if not c:
            continue
        if (c.startswith("工具") or c.startswith("[") or c.startswith("（")
                or "已生成" in c[:60] or "错误" in c[:20] or "结果" in c[:10]):
            got.append(c[:260])
    if not got:
        return "\n\n【上一轮没有产出可用结果，请直接重新完成任务】"
    return ("\n\n【上一轮已经完成的工作 —— 这些结果已经有了，不要重做，"
            "在此基础上继续把任务做完】\n"
            + "\n".join("- " + g.replace("\n", " ")[:200] for g in got[-6:]))


def _is_num(s):
    try:
        float(str(s).strip().replace(",", ""))
        return True
    except (TypeError, ValueError):
        return False


def _ppt_outline(task, data_path=None):
    """规则生成 PPT 大纲。

    为什么不让模型写：3B 写 5 页带 chart/table 的 slides JSON，实测 15 分钟
    都没跑完（长 JSON + grammar 约束互相拖死）。大纲结构其实很固定，
    数据页可以从文件直接提炼，只有"要点"措辞需要人味，而那个模型也写不准。
    """
    t = str(task or "")
    m = re.search(r"(?:关于|做一份|生成一份|做个|写个|整理成)?\s*"
                  r"([^\s，。]{2,20}?)\s*的?\s*(?:ppt|幻灯片|演示文稿)", t, re.I)
    title = (m.group(1).strip() if m else "") or "演示文稿"
    slides = [{"type": "cover", "title": title, "subtitle": "自动生成"}]

    header, rows = [], []
    if data_path and os.path.isfile(data_path):
        try:
            with open(data_path, "r", errors="replace") as fh:
                lines = [ln for ln in fh.read().split("\n") if "|" in ln]
            if lines:
                header = [c.strip() for c in lines[0].split("|")]
                rows = lines[1:]
        except Exception:                                  # noqa: BLE001
            pass

    if header and rows:
        slides.append({"type": "section", "title": "数据概览",
                       "subtitle": "共 %d 条记录，%d 个字段"
                                   % (len(rows), len([h for h in header if h]))})
        # 样例表
        cols = [i for i, h in enumerate(header) if h][:4]
        sample = []
        for r in rows[:6]:
            cells = r.split("|")
            sample.append([(cells[i].strip() if i < len(cells) else "") for i in cols])
        if cols:
            slides.append({"type": "table", "title": "数据样例（前 6 条）",
                           "headers": [header[i] for i in cols], "rows": sample})
        # 挑一列数字做 TOP8 柱状图
        best = None
        for i, h in enumerate(header):
            if not h:
                continue
            vals = []
            for r in rows:
                cells = r.split("|")
                if i < len(cells) and _is_num(cells[i]):
                    vals.append((cells[i].strip(), float(cells[i].strip().replace(",", ""))))
            if len(vals) >= max(3, len(rows) // 3):
                if best is None or len(vals) > len(best[2]):
                    best = (h, i, vals)
        if best:
            h, i, vals = best
            top = sorted(vals, key=lambda kv: kv[1], reverse=True)[:8]
            name_idx = 0 if i != 0 else 1
            labels = []
            for r in rows:
                cells = r.split("|")
                if i < len(cells) and _is_num(cells[i]):
                    nm = cells[name_idx].strip() if name_idx < len(cells) else ""
                    labels.append((cells[i].strip(), float(cells[i].strip().replace(",", "")), nm))
            labels.sort(key=lambda x: x[1], reverse=True)
            slides.append({"type": "chart", "title": "%s TOP8" % h,
                           "chartType": "bar",
                           "labels": [(x[2] or "—")[:12] for x in labels[:8]],
                           "values": [x[1] for x in labels[:8]]})
        numeric_fields = []
        for i, h in enumerate(header):
            if not h:
                continue
            ok = sum(1 for r in rows[:20]
                     if i < len(r.split("|")) and _is_num(r.split("|")[i]))
            if ok >= 3:
                numeric_fields.append(h)
        slides.append({"type": "bullets", "title": "数据要点",
                       "bullets": ["共 %d 条记录" % len(rows),
                                   "主要字段：%s" % "、".join([h for h in header if h][:6]),
                                   "数值字段：%s" % ("、".join(numeric_fields[:4]) or "无")]})
    else:
        slides.append({"type": "section", "title": "简介"})
        slides.append({"type": "bullets", "title": "任务描述",
                       "bullets": [t[:80] or "（无）"]})
        slides.append({"type": "quote", "quote": "把数据和要点补充进来，这一页会更具体",
                       "by": ""})
    slides.append({"type": "closing", "title": "谢谢"})
    return slides


def _ppt_topics(summary):
    """从数据摘要里推断 PPT 章节主题（每节一次模型调用，短 JSON）。"""
    s = str(summary or "")
    topics = ["数据概览"]
    for kw in ("播放量", "点赞", "弹幕", "收藏", "投币", "评论", "分享", "时长"):
        if kw in s and len(topics) < 3:
            topics.append("%s分析" % kw)
    while len(topics) < 4:
        topics.append(["数据特征", "对比与差异", "总结与建议"][len(topics) - 1]
                      if len(topics) < 4 else "总结")
    return topics[:4]


def _ppt_sections_batched(model, summary, topics, say=None):
    """分批让模型写 sections：每节一次调用，只要 200 token 的短 JSON。

    为什么分批：一次性写 4 节的嵌套 JSON，3B 在 schema 约束下实测 11 分钟
    都出不来（长结构 + grammar 互相拖死）。拆成 4 次短请求后每节几十秒，
    而且单节失败只丢那一节，不影响整体。
    """
    sys_msg = ("你负责写 PPT 的单个章节。只输出一个 JSON 对象，不要解释、不要代码块：\n"
               '{"title":"章节标题","bullets":["要点1","要点2"]}')
    secs = []
    for i, topic in enumerate(topics or []):
        if say:
            say("  （第 %d/%d 节：%s）" % (i + 1, len(topics), topic))
        user = ("数据摘要：\n%s\n\n请写「%s」这一节，2-3 条要点，"
                "每条不超过 20 字，数字必须来自摘要。" % (summary or "", topic))
        try:
            # 必须 use_schema=False：带 agent 的 tool schema 时，
            # 模型会把"只输出 JSON"理解成"调 write 工具写 JSON"，
            # 实际吐出 {"tool":"write","content":"{...}"}（实测）。
            raw = chat(model, [{"role": "system", "content": sys_msg},
                               {"role": "user", "content": user}],
                       max_tokens=220, use_schema=False)
        except Exception:                                  # noqa: BLE001
            continue
        obj = None
        try:
            obj = extract_json(raw)
        except Exception:                                  # noqa: BLE001
            obj = None
        if isinstance(obj, dict) and obj.get("bullets"):
            bl = [str(b).strip() for b in obj["bullets"] if str(b).strip()][:4]
            if bl:
                secs.append({"title": str(obj.get("title") or topic).strip(),
                             "bullets": bl})
    return secs


def _ppt_title(task):
    """从任务里取演示标题。"生成一份PPT" 这种没有主题的别硬凑。"""
    t = str(task or "")
    m = re.search(r"关于\s*([^\s，。]{2,20}?)\s*的?\s*(?:pptx?|幻灯片|演示文稿)", t, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"(?:生成一份|做一份|做个|写个|整理成)\s*([^\s，。]{2,20}?)\s*的?\s*"
                  r"(?:pptx?|幻灯片|演示文稿)", t, re.I)
    if m:
        cand = m.group(1).strip()
        if cand not in ("PPT", "ppt", "一份", "个", "的"):
            return cand
    m = re.search(r"([^\s，。]{2,16}?)\s*的\s*(?:pptx?|幻灯片|演示文稿)", t, re.I)
    if m:
        return m.group(1).strip()
    return "演示文稿"


def _parse_outline(text):
    """解析模型写的纯文本大纲：

        # 数据概览
        - 共 300 条记录
        - 字段有播放量/点赞数

    为什么不用 JSON：3B 在 schema 约束下生成嵌套的 sections 结构实测 11 分钟
    都出不来；同样内容写成纯文本只要几十秒，工具解析反而更稳。
    """
    secs, cur = [], None
    for raw in str(text or "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            cur = {"title": line.lstrip("#").strip(), "bullets": []}
            secs.append(cur)
            continue
        if line[:1] in ("-", "*", "•"):
            body = re.sub(r"^[-*•]\s*", "", line).strip()
            if body and cur is not None:
                cur["bullets"].append(body)
            continue
        if cur is None:
            cur = {"title": line[:24], "bullets": []}
            secs.append(cur)
    return [s for s in secs if s["bullets"]]


def _ppt_summary(path):
    """给 PPT 任务用的数据摘要：字段 + 条数 + 数值列 TOP3 + 样例几行。

    目的：模型不用 read 全文（4000 字 ≈ 177 秒 prefill，这块比生成还贵），
    但仍然能看到真实数字，写出来的要点才不是编的。
    """
    try:
        with open(path, "r", errors="replace") as fh:
            lines = [ln for ln in fh.read().split("\n") if "|" in ln]
    except Exception:                                      # noqa: BLE001
        return ""
    if len(lines) < 2:
        return ""
    header = [c.strip() for c in lines[0].split("|")]
    rows = lines[1:]
    out = ["【数据摘要（服务端已预处理，不需要再 read 文件）】",
           "字段：%s" % "、".join(h for h in header if h),
           "共 %d 条记录" % len(rows)]
    for i, h in enumerate(header):
        if not h:
            continue
        vals = []
        for r in rows:
            c = r.split("|")
            if i < len(c) and _is_num(c[i]):
                try:
                    nm = c[0].strip() if c else ""
                    vals.append((float(c[i].strip().replace(",", "")), nm))
                except ValueError:
                    pass
        if len(vals) >= 3:
            vals.sort(key=lambda kv: kv[0], reverse=True)
            top3 = "；".join("%s %g" % ((n[:16] or "—"), v) for v, n in vals[:3])
            out.append("%s：最高3 = %s（%d 个数值）" % (h, top3, len(vals)))
    out.append("样例（前 5 条）：")
    out.extend(r[:180] for r in rows[:5])
    return "\n".join(out)


def _sections_to_slides(title, sections):
    """把模型给的「标题 + 每节要点」转成完整 slides（版式/主题由工具决定）。

    模型只负责内容（它擅长的是读数据、提炼要点），排版不该让它操心 ——
    它写完整 slides JSON 会写到一半断掉，写要点则稳稳当当。
    """
    out = [{"type": "cover", "title": title or "演示文稿"}]
    secs = [s for s in (sections or []) if isinstance(s, dict)]
    if secs:
        out.append({"type": "section", "title": secs[0].get("title") or "正文"})
    for i, s in enumerate(secs):
        t = s.get("title") or ("第 %d 部分" % (i + 1))
        b = s.get("bullets") or s.get("points") or []
        b = [str(x).strip() for x in b if str(x).strip()]
        if not b:
            continue
        # 每三节里挑一节做成两栏（有 4 条以上要点时），避免全是 bullets
        if i % 3 == 2 and len(b) >= 4:
            half = (len(b) + 1) // 2
            out.append({"type": "two-col", "title": t,
                        "leftTitle": "要点", "left": b[:half],
                        "rightTitle": "补充", "right": b[half:]})
        else:
            out.append({"type": "bullets", "title": t, "bullets": b})
    out.append({"type": "closing", "title": "谢谢"})
    return out


def run_pptx(path, slides, title=None, sections=None):
    """生成真正的 .pptx。

    优先用 node + pptxgenjs（支持 chart/table/图片），没有 node 环境时退回
    纯标准库 make_pptx（只能 cover/bullets）。
    slides 里每页用 type 区分：cover / bullets / chart / table
    """
    if not path:
        path = "slides.pptx"
    if not str(path).lower().endswith(".pptx"):
        path = str(path) + ".pptx"
    path = _resolve(path)
    err = _guard_write(path)
    if err:
        return err
    # 简化 spec：模型只给 title + sections（推荐路径），版式在这里补齐
    if sections and not (isinstance(slides, list) and slides):
        slides = _sections_to_slides(title, sections)
    if not isinstance(slides, list) or not slides:
        return ("错误：slides 必须是非空数组，格式 "
                '[{"type":"cover","title":"标题"},'
                '{"type":"bullets","title":"页2","bullets":["要点1"]}]')
    slides = _enrich_slides(slides)   # 全是 cover+bullets 时补章节页/结尾页
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    node_js = os.path.join(here, "pptx.js")
    has_node = (os.path.isfile(node_js)
                and os.path.isdir(os.path.join(here, "node_modules", "pptxgenjs"))
                and shutil.which("node"))

    if has_node:
        import tempfile
        spec = {"slides": slides}
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        try:
            json.dump(spec, tmp, ensure_ascii=False)
            tmp.close()
            r = subprocess.run(["node", node_js, path, "@" + tmp.name],
                               capture_output=True, text=True, timeout=120, cwd=here)
            if r.returncode != 0:
                return "错误：node 生成失败 %s" % ((r.stderr or r.stdout) or "")[:300]
            kinds = [s.get("type", "bullets") for s in slides]
            return ("已生成真正的 PowerPoint：%s（%d 页，pptxgenjs，页面类型 %s）"
                    % (path, len(slides), "/".join(kinds)))
        except Exception as exc:                               # noqa: BLE001
            return "错误：%s" % exc
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    # 退路：纯标准库（没有图表）
    try:
        if here not in sys.path:
            sys.path.insert(0, here)
        import make_pptx as mk
        mk.build_pptx(path, slides)
        return ("已生成 PowerPoint：%s（%d 页，标准库版，无图表；"
                "本机没装 node/pptxgenjs）" % (path, len(slides)))
    except Exception as exc:                                   # noqa: BLE001
        return "错误：%s" % exc


def run_calc(expr):
    """安全计算数学表达式。

    3B 心算能力极差（同一道长方形题，它能列出 x=√29 却把 5√29 × 2√29 算成 285，
    正确是 290）。给它一个计算器，把"算"这件事从模型脑子里搬走。
    用 ast 白名单求值，eval 只拿得到纯数字运算，碰不到任何 builtins。
    """
    import ast as _ast
    import math as _math
    import operator as _op

    expr = str(expr or "").strip()
    if not expr:
        return "错误：expr 为空"

    ops = {_ast.Add: _op.add, _ast.Sub: _op.sub, _ast.Mult: _op.mul,
           _ast.Div: _op.truediv, _ast.Pow: _op.pow, _ast.Mod: _op.mod,
           _ast.FloorDiv: _op.floordiv, _ast.USub: _op.neg, _ast.UAdd: _op.pos}
    funcs = {"sqrt": _math.sqrt, "sin": _math.sin, "cos": _math.cos,
             "tan": _math.tan, "log": _math.log, "log10": _math.log10,
             "log2": _math.log2, "exp": _math.exp, "abs": abs,
             "round": round, "min": min, "max": max, "pow": pow}
    consts = {"pi": _math.pi, "e": _math.e}

    def ev(node):
        if isinstance(node, _ast.Expression):
            return ev(node.body)
        if isinstance(node, _ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, _ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, _ast.UnaryOp) and type(node.op) in ops:
            return ops[type(node.op)](ev(node.operand))
        if isinstance(node, _ast.Name) and node.id in consts:
            return consts[node.id]
        if (isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name)
                and node.func.id in funcs):
            return funcs[node.func.id](*[ev(a) for a in node.args])
        if isinstance(node, _ast.Tuple):
            return tuple(ev(x) for x in node.elts)
        raise ValueError("表达式里有不支持的写法")

    try:
        # 允许模型写 ^ 当幂、×÷ 当乘除
        cleaned = (expr.replace("^", "**").replace("×", "*").replace("÷", "/")
                   .replace("（", "(").replace("）", ")"))
        value = ev(_ast.parse(cleaned, mode="eval"))
        if isinstance(value, float):
            # 去掉浮点尾巴：sqrt(29)**2*10 会算出 289.99999999999994，人看着就是 290
            if abs(value - round(value)) < 1e-9:
                value = int(round(value))
            elif abs(value) < 1e15:
                value = round(value, 10)
        return "%s = %s" % (expr, value)
    except ZeroDivisionError:
        return "错误：除数为 0"
    except Exception as exc:                                   # noqa: BLE001
        return "错误：算不出来（%s）。expr 只能包含数字、+ - * / ** ( )、sqrt/log/sin/cos 等" % exc


# 风格 → 和弦进行（绝对音高；每小节一个和弦，4/4 拍）
MIDI_CHORDS = {
    "calm":  [[60, 64, 67], [57, 60, 64], [53, 57, 60], [55, 59, 62]],
    "happy": [[60, 64, 67], [55, 59, 62], [57, 60, 64], [53, 57, 60]],
    "sad":   [[57, 60, 64], [53, 57, 60], [60, 64, 67], [55, 59, 62]],
    "epic":  [[48, 51, 55], [44, 48, 51], [51, 55, 58], [46, 50, 53]],
    "jazz":  [[60, 64, 67, 71], [50, 53, 57, 60], [55, 59, 62, 65], [60, 64, 67, 71]],
}

# 轨道名 → GM 音色号
MIDI_INSTR = {
    "piano": 0, "guitar": 24, "bass": 33, "strings": 48, "organ": 16,
    "pad": 89, "flute": 73, "lead": 80,
}


# ---- FC 风格：旋律素材在 fc_synth.py 里（SPARSE_THEME / SPARSE_B / SPARSE_OUT）----
# 早期那套「密集十六分 + 音阶随机跑动」的素材已删除：用户实测听着是噪音，
# 真正能听的是稀疏版（每小节 ≤3 个长音）。旋律必须两处共用同一份，
# 否则 MIDI 版和音频版会不一致。


def _run_fc_midi(path, tempo, bars, want, jitter=True):
    """稀疏风格 MIDI（旋律与 render_sparse_song 一致，只是不带波形信息）。

    注意：MIDI 只记录"乐器号"，音色取决于播放器的波表。
    本函数里旋律用 program 80(Square Lead) / 节奏用 channel 9，
    但听起来像不像 FC，得看你播放器装的是什么音源。
    """
    from midiutil import MIDIFile

    # 旋律素材和音频版共用同一份（fc_synth 里定义），避免两处不一致
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import fc_synth
    SPARSE_THEME = fc_synth.SPARSE_THEME
    SPARSE_B = fc_synth.SPARSE_B
    SPARSE_OUT = fc_synth.SPARSE_OUT

    beat = 60.0 / tempo
    layout = [("intro", 4), ("A", 8), ("B", 8), ("A2", 8), ("outro", 4)]
    seq = []
    for name, n in layout:
        seq.extend([name] * n)
    while len(seq) < bars:
        seq.extend(["A"] * 8 + ["B"] * 8)
    seq = seq[:bars]

    root_map = {"Am": 57, "F": 53, "G": 55, "E": 52}
    tracks = want or ["lead", "bass", "drums"]
    # 内容随机化：整体移调 + 速度微调；但用户点名了时长时不抖速度（否则时长不准）
    trans = random.randint(-5, 5)
    tempo = max(40, min(220, int(round(
        tempo * (random.uniform(0.94, 1.06) if jitter else 1.0)))))
    m = MIDIFile(len(tracks) + 1, adjust_origin=False)
    m.addTempo(0, 0, tempo)

    for ti, name in enumerate(tracks):
        if name == "drums":
            m.addProgramChange(ti, 9, 0, 0)
        elif name == "bass":
            m.addProgramChange(ti, 0, 0, 38)           # Synth Bass
        else:
            m.addProgramChange(ti, 0, 0, 80)           # Square Lead

    for bi, sec in enumerate(seq):
        idx = bi % 4
        t_bar = bi * 4.0
        if sec == "B":
            chord, phrase = SPARSE_B[idx]
        elif sec == "outro":
            chord, phrase = SPARSE_OUT[idx]
        elif sec == "intro":
            chord, phrase = SPARSE_THEME[idx][0], []
        else:
            chord, phrase = SPARSE_THEME[idx]
        root = root_map.get(chord, 57)

        for ti, name in enumerate(tracks):
            if name == "drums":
                for b in (0, 1, 2, 3):
                    if sec == "outro" and b > 0:
                        continue
                    if b % 2 == 0:
                        m.addNote(ti, 9, 36, t_bar + b, 0.28, 100)   # kick
                    else:
                        m.addNote(ti, 9, 38, t_bar + b, 0.28, 96)    # snare
            elif name == "bass":
                m.addNote(ti, 0, max(0, root - 12 + trans), t_bar, 1.9, 90)
                m.addNote(ti, 0, max(0, root - 12 + trans), t_bar + 2, 1.9, 84)
            else:
                t = t_bar
                for pitch, dur in phrase:
                    m.addNote(ti, 0, int(pitch) + trans, t, dur * 0.95, 100)
                    t += dur

    with open(path, "wb") as fh:
        m.writeFile(fh)
    notes = sum(len(p) for _c, p in SPARSE_THEME) * max(1, len(seq) // 4)
    return os.path.getsize(path), notes


STYLE_TEMPO = {"calm": 90, "happy": 120, "sad": 70,
               "epic": 100, "jazz": 110, "fc_action": 150,
               "classical": 88, "electronic": 128, "guqin": 60}
_RAND_STYLES = ["calm", "happy", "sad", "epic", "jazz", "fc_action",
                "classical", "electronic", "guqin"]

# 每种风格的音色（General MIDI program number）
#   piano / melody 走旋律轨，bass 走低音轨
STYLE_INSTR = {
    "calm":       {"piano": 0,   "bass": 33, "melody": 0},     # 原声钢琴
    "happy":      {"piano": 0,   "bass": 33, "melody": 0},
    "sad":        {"piano": 0,   "bass": 33, "melody": 0},
    "epic":       {"piano": 48,  "bass": 33, "melody": 48},    # Strings Ensemble
    "jazz":       {"piano": 4,   "bass": 33, "melody": 4},     # Electric Piano
    "classical":  {"piano": 6,   "bass": 42, "melody": 48},    # Harpsichord + Cello + Strings
    "electronic": {"piano": 89,  "bass": 38, "melody": 80},    # Pad + Synth Bass + Lead
    "guqin":      {"piano": 107, "bass": 46, "melody": 107},   # Koto + Harp
}


def _task_has_style(task):
    """任务里点名了具体风格吗（FC/游戏/平静/欢快/悲伤/史诗/爵士…）。"""
    return bool(re.search(r"fc|FC|游戏|平静|舒缓|安静|轻音乐|欢快|开心|快乐|"
                          r"悲伤|忧伤|抒情|史诗|激昂|热血|燃|爵士|"
                          r"calm|happy|sad|epic|jazz", str(task or "")))


STYLE_STATE = "/tmp/llama_agent/.last_style"   # 跨进程记住上次风格


def _pick_style():
    """随机挑风格，保证"连着发也不会撞"。

    每次 agent 任务都是全新进程，进程内变量会重置，所以状态必须落盘：
    文件里存「本轮还没用过的风格」+「最近用过的」，用完一轮才重新洗牌。
    只记"上次"是不够的——实测 calm 可能隔一次就再来一遍。
    """
    state = {}
    try:
        with open(STYLE_STATE, "r") as fh:
            state = json.load(fh)
        if not isinstance(state, dict):
            state = {}
    except Exception:                                          # noqa: BLE001
        pass

    left = [s for s in (state.get("left") or []) if s in _RAND_STYLES]
    if not left:
        left = _RAND_STYLES[:]
        random.shuffle(left)
        used = state.get("used") or []
        last = used[-1] if used else None
        if last in left and len(left) > 1:
            left.remove(last)          # 新一轮开头别跟上一轮末尾撞
    style = left.pop(0)
    used = ((state.get("used") or []) + [style])[-6:]
    try:
        with open(STYLE_STATE, "w") as fh:
            json.dump({"left": left, "used": used}, fh)
    except Exception:                                          # noqa: BLE001
        pass
    return style


# 完整和弦进行：每个风格 4 个和弦（原来每风格只有 2 个和弦在 8 小节里来回循环，
# 这就是"听着单调"的真正原因 —— 节奏型怎么变，和声都一样）。
FULL_PROGS = {
    # calm：原来结尾是 E 大三和弦（A 小调的 V 级，收不住，听着"悬"）。
    # 改成回到主和弦 Am，整句闭合，才是"平静"的感觉。
    "calm":  [[57, 60, 64], [53, 57, 60], [55, 59, 62], [57, 60, 64]],      # Am F G Am
    "happy": [[60, 64, 67], [55, 59, 62], [57, 60, 64], [53, 57, 60]],      # C G Am F
    "sad":   [[57, 60, 64], [50, 53, 57], [52, 56, 59], [57, 60, 64]],      # Am Dm E Am
    "epic":  [[45, 48, 52], [41, 45, 48], [43, 47, 50], [40, 43, 47]],      # Am F G E（低音区）
    # jazz：原来 Cmaj7 Am7 Fmaj7 G7 是"三个 I 级来回"，没有 ii-V 张力。
    # 换成标准 ii-V-I-vi（Dm7 G7 Cmaj7 Am7），爵士味才对。
    "jazz":  [[50, 53, 57, 60], [55, 59, 62, 65], [60, 64, 67, 71], [57, 60, 64, 67]],
    # classical：C 大调 I-IV-V-I（最标准的古典终止式，收得住）
    "classical": [[60, 64, 67], [53, 57, 60], [55, 59, 62], [60, 64, 67]],   # C F G C
    # electronic：Am F C G（EDM 万能进行，四小节一个循环）
    "electronic": [[57, 60, 64], [53, 57, 60], [60, 64, 67], [55, 59, 62]],  # Am F C G
    # guqin：五声音阶（C D E G A）拼的空心和弦，刻意避开半音（古琴无半音）
    "guqin": [[60, 64, 67], [62, 67, 69], [64, 67, 72], [60, 64, 67]],       # C 五声
}


# 旋律节奏模板：每种是 4 小节的节奏骨架（单位：拍，每小节合计 4 拍）。
# 上一版固定用同一套节奏型，所以"每次不同"只是换了调和音高 —— 听着单调。
# 现在随机挑节奏型，音高仍取自当前和弦（保证不跑调）。
RHYTHM_TEMPLATES = [
    [[2, 2], [1, 1, 2], [2, 2], [1, 1, 2]],            # 二分 + 四分（原 SPARSE 节奏）
    [[4], [2, 2], [4], [2, 2]],                         # 全音符长音，最抒情
    [[1.5, 0.5, 2], [2, 2], [1.5, 0.5, 2], [2, 2]],     # 附点
    [[1, 1, 1, 1], [2, 2], [1, 1, 1, 1], [2, 2]],       # 四平八稳
    [[3, 1], [2, 2], [3, 1], [2, 2]],                   # 长 + 短
    [[1, 1, 2], [1, 1, 2], [1, 1, 2], [1, 1, 2]],       # 均匀三音
    [[0.5, 0.5, 1, 2], [2, 2], [0.5, 0.5, 1, 2], [2, 2]],  # 短促开头
]

# 每种风格配一套固定节奏型（不随机）：抒情的用长音，爵士用附点才有 swing 感，
# 之前所有风格共用随机模板，calm 抽到附点、jazz 抽到四平八稳都会很难听。
STYLE_RHYTHM = {
    "calm": [[4], [2, 2], [4], [2, 2]],                          # 全音符，最舒缓
    "sad":  [[4], [3, 1], [4], [2, 2]],                          # 长音 + 拖尾
    "jazz": [[1.5, 0.5, 2], [2, 2], [1.5, 0.5, 2], [2, 2]],      # 附点 = swing 感
    "happy": [[2, 2], [1, 1, 2], [2, 2], [1, 1, 2]],             # 轻快
    "epic": [[1, 1, 2], [2, 2], [1, 1, 2], [2, 2]],              # 推进
    "classical": [[1, 1, 2], [1, 1, 2], [1, 1, 2], [1, 1, 2]],   # 均匀八分，古典流动感
    "electronic": [[1, 1, 1, 1], [1, 1, 1, 1],
                   [1, 1, 1, 1], [1, 1, 1, 1]],                  # 四踩，每拍一个
    "guqin": [[4], [2, 2], [4], [2, 2]],                          # 长音留白
}


def _vary_notes(notes, rng):
    """在给定旋律骨架上做受控变化：整体移调 + 少量音高/时值微调。

    绝不做"每步随机游走"——实测那样出来的东西既没有节奏骨架也没有动机，
    听起来就是乱弹琴（用户原话）。这里保持原旋律的音符个数、位置和时值轮廓，
    只在局部做小改动，所以每次不同但仍然"是首歌"。
    """
    trans = rng.randint(-5, 5)
    out = []
    for pitch, dur in notes:
        p = pitch + trans
        if rng.random() < 0.16:                # 少量级进（±2 半音），保持旋律轮廓
            p += rng.choice([-2, 2])
        d = dur
        if rng.random() < 0.10:                # 少量节奏微调，但不打乱骨架
            d = max(0.5, dur + rng.choice([-0.5, 0.5]))
        out.append((max(45, min(92, p)), d))
    return out


def _extract_top_n(task):
    """从任务里取条数（"top10" / "bottom 5" / "前五" / "后三"）。默认 10。"""
    t = str(task or "")
    m = re.search(r"(?:top|bottom|前|后|最大|最小|最短|最长|最多|最少)\s*的?\s*(\d+)", t, re.I)
    if m:
        try:
            return max(1, min(int(m.group(1)), 50))
        except ValueError:
            pass
    cn = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    m = re.search(r"(?:前|后)([一二三四五六七八九十])", t)
    if m:
        return cn.get(m.group(1), 10)
    return 10


def _looks_like_ppt(task):
    """要做 PPT 的请求（要交 pptx 工具，不能被文档分支吞掉）。

    注意别用 \b：中文和拉丁字母之间不算词边界，
    "生成一份PPT" 用 \bpptx?\b 匹配不到（实测导致又掉回模型路径）。
    """
    return bool(re.search(r"pptx?|幻灯片|演示文稿|slides?|汇报材料|做个演示",
                          str(task or ""), re.I))


def _looks_like_doc_task(task):
    """基于上传文档的任务。

    判据两条，满足任一即可：
      1) 任务里带了上传路径（页面自动附加）
      2) uploads/ 里有文件，且任务在问表格/文档相关的东西
         —— 用户手动输入「按照作者累计播放量排序 top10」时没有路径，
         只靠 1) 会漏判，然后掉进通用 agent 路径（让模型自己想办法，慢且错）。
    """
    t = str(task or "")
    # 做 PPT 的请求交给 pptx 工具，别当成"读文档 summarize"
    # （实测"生成一份PPT"+上传路径会被判成文档任务，输出一段数据总结就结束）
    if _looks_like_ppt(t):
        return False
    if "uploads/" in t:
        return True
    try:
        updir = os.path.join(WORK_DIR, "uploads")
        if os.path.isdir(updir) and any(f.lower().endswith(".txt")
                                        for f in os.listdir(updir)):
            if re.search(r"表格|文档|文件|数据|统计|排序|排名|top|bottom|作者|播放|"
                         r"点赞|时长|弹幕|收藏|投币|评论|分享|合计|累计|汇总",
                         t, re.I):
                return True
    except Exception:                                          # noqa: BLE001
        pass
    return False


# 风格词表：每组给足同义词。
# 之前只写了"欢快"，用户说"轻快"就认不出（反馈"我明明说了轻快"），
# 于是随机到了 electronic。自然语言里同一个意思有很多说法，词表必须覆盖。
_STYLE_WORDS = {
    "happy":      r"欢快|轻快|明快|活泼|愉悦|快乐|开心|阳光|爽朗|欢愉|雀跃|俏皮|轻巧|happy",
    "calm":       r"平静|平和|舒缓|安静|轻音乐|放松|温柔|柔和|治愈|恬静|安详|calm",
    "sad":        r"悲伤|难过|忧伤|伤感|凄凉|委屈|惆怅|低落|emo|sad|哀",
    "epic":       r"史诗|激昂|燃|热血|战斗|气势|壮阔|宏大|震撼|澎湃|epic",
    "jazz":       r"爵士|jazz|摇摆|蓝调|慵懒|即兴",
    "classical":  r"古典|classical|巴洛克|钢琴曲|交响|奏鸣|优雅|典雅|弦乐|宫廷",
    "electronic": r"电音|electronic|edm|电子乐|舞曲|蹦迪|合成器|打碟|dj|techno",
    "guqin":      r"古琴|古筝|琵琶|中国风|古风|民乐|国风|二胡|笛|guqin",
    "fc_action":  r"fc|游戏|8.?bit|像素|红白机|小霸王|电子游戏|8位",
}


def _style_from_task(task):
    """从任务原文提取音乐风格，None = 用户没提。

    顺序按"专有名词优先"：古琴/古典/电音这类一出现就不该被"轻"字带走。
    """
    t = str(task or "")
    for style in ("guqin", "classical", "electronic", "fc_action",
                  "jazz", "sad", "epic", "calm", "happy"):
        pat = _STYLE_WORDS.get(style)
        if pat and re.search(pat, t, re.I):
            return style
    return None


def _cn_num(s):
    """中文数字转 int（一~九十九）。_CN_DIGIT 用文件前面那份（带百/千）。"""
    if not s:
        return None
    if s in _CN_DIGIT:
        return _CN_DIGIT[s]
    if "十" in s:
        a, _, b = s.partition("十")
        tens = _CN_DIGIT.get(a, 1) if a else 1        # "十五" → 1*10+5
        ones = _CN_DIGIT.get(b, 0) if b else 0
        return tens * 10 + ones
    return None


def _task_seconds(task):
    """任务里点了时长吗（"30 秒" / "三十秒" / "1.5 分钟" / "半分钟"）。

    原来只认阿拉伯数字，用户说"三十秒的bgm""一分钟的"全部返回 None，
    回落到默认 32 小节（≈65 秒）→ 用户看到的就是"每次时长都不准"。
    """
    t = str(task or "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:秒钟?|sec(?:ond)?s?|s\b)", t, re.I)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:分钟|min(?:ute)?s?|m\b)", t, re.I)
    if m:
        return float(m.group(1)) * 60.0
    # 中文数字：三十秒 / 一分钟 / 两分钟 / 十五秒
    m = re.search(r"([一二三四五六七八九十两]+)\s*秒钟?", t)
    if m:
        n = _cn_num(m.group(1))
        if n:
            return float(n)
    m = re.search(r"([一二三四五六七八九十两]+)\s*分钟", t)
    if m:
        n = _cn_num(m.group(1))
        if n:
            return float(n) * 60.0
    if "半分钟" in t or "半分" in t:
        return 30.0
    return None


def run_midi(path, style="happy", tempo=120, bars=8, tracks=None, seconds=None):
    """用 MIDIUtil 生成多轨 BGM。

    设计上不要求模型写出一个个音符（3B 写不了那么长的 JSON），
    只给它「风格 / 速度 / 小节数 / 要哪些轨」四个高层参数，
    具体的和弦进行、琶音、鼓点、旋律由这里按乐理生成。

    风格 fc_action（赤影战士/忍者龙剑传那类 FC 动作游戏）走独立生成器：
    A 小调、十六分音符跑动、驱动鼓、三度叠置、intro-A-B-A'-outro 结构。
    """
    try:
        from midiutil import MIDIFile
    except ImportError:
        return ("错误：板上没有 MIDIUtil。装法：pip3 install MIDIUtil")

    # 风格随机模式：用户说"随意/随机"或模型没指定时，由这里挑 ——
    # 保证每次出来的风格不同（排除上一次用过的）。
    style = str(style or "").strip().lower()
    if style in ("", "random", "rand", "any", "auto", "none",
                 "随机", "随意", "随便", "都行", "不同", "不同风格"):
        style = _pick_style()
        tempo = STYLE_TEMPO.get(style, tempo)

    # 用户点名了时长 → 按 tempo 反推小节数（每小节 4 拍），并且不再抖速度，
    # 否则时长必然不准（之前 bars 由模型瞎填，要 30 秒它给 32 小节 = 64 秒）。
    _jitter = random.uniform(0.92, 1.08)
    if seconds:
        try:
            secs_f = float(seconds)
            if secs_f > 0:
                bars = max(1, int(round(secs_f * tempo / 60.0 / 4.0)))
                _jitter = 1.0                  # 点名时长就不抖速度
                # 小节数只能取整（每小节固定 4 拍），用 tempo 反向微调把时长补准：
                # bars×4拍 ÷ (tempo/60) 秒 = 目标秒数 → tempo = bars×240/秒数
                tempo = max(40, min(220, int(round(bars * 240.0 / secs_f))))
        except (TypeError, ValueError):
            pass

    # 旋律骨架来自 fc_synth（用户认可的那套稀疏长音主题），
    # 非 FC 风格也用它做底，只是织体和速度不同。
    try:
        import fc_synth
        _theme = fc_synth.SPARSE_THEME
    except Exception:                                          # noqa: BLE001
        _theme = [("Am", [(69, 2), (72, 2)]), ("F", [(65, 1), (69, 1), (72, 2)]),
                  ("G", [(67, 2), (71, 2)]), ("E", [(64, 1), (68, 1), (71, 2)])]

    if not path:
        path = "bgm.mid"
    if not str(path).lower().endswith((".mid", ".midi")):
        path = str(path) + ".mid"
    path = _resolve(path)
    err = _guard_write(path)
    if err:
        return err

    style = str(style or "calm").lower()
    try:
        tempo = max(40, min(int(tempo), 220))
    except (TypeError, ValueError):
        tempo = 90
    try:
        bars = max(2, min(int(bars), 128))
    except (TypeError, ValueError):
        bars = 8

    want = [str(t).lower() for t in (tracks or [])][:8]

    # FC 动作游戏风格：独立生成器
    if style in ("fc_action", "fc", "ninja", "kage", "shadow"):
        if not want:
            want = ["lead", "bass", "drums"]      # 稀疏版不需要单独的和声轨
        if tempo == 90:
            tempo = 156                # 这类曲子默认快
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        try:
            size, notes = _run_fc_midi(path, tempo, bars, want, jitter=(not seconds))
        except Exception as exc:                               # noqa: BLE001
            return "错误：生成失败 %s" % exc

        # 默认只出 MIDI（几 KB）。MIDI 不含音色信息，播放效果取决于播放器波表；
        # 要听我们自己合成的 FC 音色，设 AGENT_AUDIO=1 才会额外渲染音频。
        audio_msg = ""
        if os.environ.get("AGENT_AUDIO", "0") in ("1", "true", "yes"):
            try:
                here = os.path.dirname(os.path.abspath(__file__))
                if here not in sys.path:
                    sys.path.insert(0, here)
                import fc_synth
                wav = os.path.splitext(path)[0] + ".wav"
                info = fc_synth.render_sparse_song(wav, tempo)
                audio, compressed = _compress_wav(wav)
                audio_msg = ("；音频 %s（%.0f 秒，%s）"
                             % (os.path.basename(audio), info["seconds"],
                                "MP3 %.0fKB" % (os.path.getsize(audio) / 1024.0)
                                if compressed else "WAV"))
            except Exception as exc:                           # noqa: BLE001
                audio_msg = "（音频合成失败：%s）" % exc

        # 时长自己算出来给模型，否则它会瞎猜（实测 32 小节 150BPM 报了"4 分钟"，
        # 实际 32×4拍÷150BPM = 51 秒）
        secs = int(round(bars * 4 * 60.0 / max(tempo, 1)))
        return ("已生成 FC 风格 MIDI：%s（%d 字节，%d 轨：%s，A小调，%d BPM，"
                "%d 小节，时长约 %d 秒，稀疏编曲 intro-A-B-A'-outro）%s"
                % (path, size, len(want), "/".join(want), tempo, bars, secs, audio_msg))

    prog = FULL_PROGS.get(style, FULL_PROGS["calm"])
    if not want:
        want = ["piano", "bass", "drums", "melody"]
    midi = MIDIFile(len(want) + 1)
    midi.addTempo(0, 0, tempo)

    def note(tr, ch, pitch, t, dur, vol=88):
        midi.addNote(tr, ch, max(0, min(127, int(pitch))), float(t),
                     float(dur), max(1, min(127, int(vol))))

    # ===== 内容随机化 =====
    # 同一风格两次必须是不同曲子。原来这里是死模板：和弦按固定顺序循环、
    # 旋律是固定的 6 音序列、鼓点每拍一模一样 ——
    # 实测同风格两次 md5 完全相同（134e16d3...），"换风格"只是换了套模板而已。
    trans = random.randint(-5, 5)                        # 整体移调
    prog = [tuple(p + trans for p in ch) for ch in prog]
    # 不 shuffle：会破坏和声功能走向（听着像没调性）。
    # 改成随机起点轮转，进行顺序还是对的，但每次从不同和弦开始。
    if prog:
        _start = random.randrange(len(prog))
        prog = prog[_start:] + prog[:_start]
    tempo = max(40, min(220, int(round(tempo * _jitter))))
    # 节奏型按风格固定（calm 长音、jazz 附点），抽不到风格不匹配的模板
    rhythm_tpl = STYLE_RHYTHM.get(style) or random.choice(RHYTHM_TEMPLATES)

    _instr = STYLE_INSTR.get(style, {})
    for idx, name in enumerate(want):
        if name == "drums":
            midi.addProgramChange(idx, 9, 0, 0)      # channel 9 = 打击乐
        elif name in ("piano", "melody"):
            midi.addProgramChange(idx, 0, 0, _instr.get(name, 0))
        elif name == "bass":
            midi.addProgramChange(idx, 0, 0, _instr.get("bass", 33))
        else:
            midi.addProgramChange(idx, 0, 0, MIDI_INSTR.get(name, 0))

        for bar in range(bars):
            # 结构 A-A-B-A：后半段和弦进行倒序，避免整首一个样（单调的来源之一）
            if bars >= 8 and (bar // 4) % 2 == 1:
                chord = prog[3 - (bar % 4)]
            else:
                chord = prog[bar % 4]
            t0 = bar * 4.0
            if name == "drums":
                pat = random.choice([0, 1, 2])
                for b in range(4):
                    if pat == 0:                              # 四踩 + 二四拍军鼓
                        note(idx, 9, 36, t0 + b, 0.4, 96)
                        if b % 2 == 1:
                            note(idx, 9, 38, t0 + b, 0.4, 88)
                    elif pat == 1:                            # 一三踩
                        if b in (0, 2):
                            note(idx, 9, 36, t0 + b, 0.4, 96)
                        if b in (1, 3):
                            note(idx, 9, 38, t0 + b, 0.4, 88)
                    else:                                     # 八分驱动 + 切分军鼓
                        note(idx, 9, 36, t0 + b * 0.5, 0.3, 92)
                        if b % 2 == 0:
                            note(idx, 9, 38, t0 + b + 0.75, 0.3, 86)
                    for hh in (0, 0.5):                       # 踩镲密度随机
                        if random.random() < 0.72:
                            note(idx, 9, 42, t0 + b + hh, 0.18,
                                 55 + random.randint(0, 22))
            elif name == "bass":
                pat = random.choice(["quarter", "skip", "walk"])
                if pat == "quarter":                      # 每拍根音
                    for b in range(4):
                        note(idx, 0, chord[0] - 24, t0 + b, 0.9, 84)
                elif pat == "skip":                       # 切分
                    for b in (0, 1.5, 2, 3.5):
                        note(idx, 0, chord[0] - 24, t0 + b, 0.6, 84)
                else:                                     # 走句（和弦音）
                    for b, p in enumerate([chord[0], chord[1], chord[2], chord[0]]):
                        note(idx, 0, p - 24, t0 + b, 0.9, 82)
            elif name == "melody":
                # 节奏用随机挑的模板（7 种节拍骨架之一），音高取自当前和弦 ——
                # 这样每首曲子的节奏线条都不同，但不会跑调。
                rhythm = rhythm_tpl[bar % 4]
                tones = [p + trans for p in list(chord) + list(chord)[::-1]]
                t = t0
                for i, dur in enumerate(rhythm):
                    p = tones[i % len(tones)]
                    while p < 62:                              # 放到中高音区
                        p += 12
                    while p > 88:
                        p -= 12
                    note(idx, 0, p, t, max(0.3, dur * 0.92),
                         86 + random.randint(0, 10))
                    t += dur
            else:
                # 铺底琶音：方向、步长都随机
                tones = list(chord) + list(chord)[::-1]
                if random.random() < 0.5:
                    tones = tones[::-1]
                step = random.choice([0.5, 1.0, 1.0])     # 只用八分/四分，别用十六分
                for i in range(int(4.0 / step)):
                    note(idx, 0, tones[i % len(tones)], t0 + i * step,
                         step * 0.9, 64 + random.randint(0, 16))

    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as fh:
        midi.writeFile(fh)

    secs = int(round(bars * 4 * 60.0 / max(tempo, 1)))
    return ("已生成 MIDI：%s（%d 字节，%d 轨：%s，%s 风格，%d BPM，%d 小节，时长约 %d 秒）"
            % (path, os.path.getsize(path), len(want), "/".join(want),
               style, tempo, bars, secs))


SONG_FILE = "/tmp/agent-song.json"


def _song_load():
    try:
        with open(SONG_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("bars", [])
            return data
    except (OSError, ValueError):
        pass
    return {"path": "song", "tempo": 160, "bars": []}


def _song_save(song):
    try:
        with open(SONG_FILE, "w", encoding="utf-8") as fh:
            json.dump(song, fh, ensure_ascii=False)
    except OSError:
        pass


def run_music_start(path, tempo=160):
    """开一首新曲，清空之前的小节。"""
    s = {"path": (str(path) if path else "song"),
         "tempo": max(40, min(int(tempo or 160), 240)), "bars": []}
    _song_save(s)
    return ("新曲开始：%s，%d BPM。流程：music_bar 开一小节 → music_notes 写"
            "这一小节的音符（可多次）→ 再 music_bar 开下一小节 → 最后 "
            "music_render 渲染。pitch 用 MIDI 音高号：C4=60 D4=62 E4=64 "
            "F4=65 G4=67 A4=69 B4=71 C5=72。dur 单位是拍（0.5=八分，1=四分）。"
            % (s["path"], s["tempo"]))


def run_music_bar(chord="Am", notes=None):
    """开一个小节；可以顺带把这一小节的音符一起写进来（省一轮调用）。"""
    s = _song_load()
    s["bars"].append({"chord": (str(chord) if chord else "Am"), "notes": []})
    bar = s["bars"][-1]
    if notes:
        for n in (notes or [])[:16]:
            if not isinstance(n, dict):
                continue
            try:
                pitch = int(n.get("pitch", 69))
                dur = float(n.get("dur", 0.5))
            except (TypeError, ValueError):
                continue
            if 0 < pitch < 128 and 0 < dur <= 4.0:
                bar["notes"].append({"pitch": pitch, "dur": round(dur, 3)})
    _song_save(s)
    n = len(s["bars"])
    used = sum(x["dur"] for x in bar["notes"])
    if bar["notes"]:
        return ("第 %d 小节（和弦 %s）已写入 %d 个音，共 %.1f 拍：%s"
                % (n, bar["chord"], len(bar["notes"]), used,
                   " ".join(str(x["pitch"]) for x in bar["notes"][:10])))
    return ("第 %d 小节（和弦 %s）已就绪，用 music_notes 写旋律。"
            "一小节 4 拍，音符 dur 加起来别超过 4。" % (n, bar["chord"]))


def run_music_notes(notes):
    """把模型写的音符追加到当前小节。"""
    s = _song_load()
    if not s["bars"]:
        return "错误：先调 music_bar 开一个小节，再写音符"
    bar = s["bars"][-1]
    added = 0
    for n in (notes or [])[:16]:
        if not isinstance(n, dict):
            continue
        try:
            pitch = int(n.get("pitch", 69))
            dur = float(n.get("dur", 0.5))
        except (TypeError, ValueError):
            continue
        if 0 < pitch < 128 and 0 < dur <= 4.0:
            bar["notes"].append({"pitch": pitch, "dur": round(dur, 3)})
            added += 1
    _song_save(s)
    used = sum(x["dur"] for x in bar["notes"])
    return ("第 %d 小节现在有 %d 个音（共 %.1f 拍，上限 4）：%s%s"
            % (len(s["bars"]), len(bar["notes"]), used,
               " ".join(str(x["pitch"]) for x in bar["notes"][:10]),
               " …" if len(bar["notes"]) > 10 else ""))


def run_music_render(path=None):
    """把所有小节渲染成 WAV（旋律完全是模型写的，工具只加伴奏）。"""
    s = _song_load()
    bars = s.get("bars") or []
    if not bars:
        return "错误：还没写任何小节，先 music_start / music_bar / music_notes"
    written = sum(len(b.get("notes") or []) for b in bars)
    if not written:
        return "错误：所有小节都是空的，先用 music_notes 写音符"
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import fc_synth
    except ImportError as exc:
        return "错误：fc_synth 不可用（%s）" % exc

    base = os.path.splitext(str(path or s.get("path") or "song"))[0]
    wav = _resolve(base + ".wav")
    err = _guard_write(wav)
    if err:
        return err
    parent = os.path.dirname(os.path.abspath(wav))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    try:
        info = fc_synth.render_notes(wav, bars, int(s.get("tempo") or 160))
    except Exception as exc:                                   # noqa: BLE001
        return "错误：渲染失败 %s" % exc
    audio, compressed = _compress_wav(wav)
    return ("已渲染 %s（%s 秒，%d 小节，%d 个音符 —— 旋律全部来自模型，"
            "工具只补了低音和鼓；%s）"
            % (os.path.basename(audio), info["seconds"], len(bars), written,
               "MP3 %.0fKB" % (os.path.getsize(audio) / 1024.0)
               if compressed else "WAV"))


def _parse_midi_melody(path):
    """解析 MIDI，取出旋律声部的音符，按小节分组。

    跳过 channel 9（打击乐）和低音声部 —— 那些由 fc_synth 自己补，
    否则会和我们生成的伴奏打架。
    """
    import struct as _struct

    with open(path, "rb") as fh:
        data = fh.read()
    if data[:4] != b"MThd":
        raise ValueError("不是 MIDI 文件（缺少 MThd 头）")
    _fmt, _ntr, div = _struct.unpack(">HHH", data[8:14])
    if div <= 0:
        div = 480

    events = []          # (start_tick, pitch, dur_ticks, channel)
    pos = 14
    while pos < len(data) and data[pos:pos + 4] == b"MTrk":
        ln = _struct.unpack(">I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + ln]
        i, t, active = 0, 0, {}
        while i < len(body):
            delta = 0
            while True:
                b = body[i]
                i += 1
                delta = (delta << 7) | (b & 0x7F)
                if not b & 0x80:
                    break
            t += delta
            if i >= len(body):
                break
            st = body[i]
            if st == 0xFF:                       # meta
                i += 2
                ml = 0
                while True:
                    b = body[i]
                    i += 1
                    ml = (ml << 7) | (b & 0x7F)
                    if not b & 0x80:
                        break
                i += ml
            elif st & 0xF0 == 0x90:              # note on
                p, v = body[i + 1], body[i + 2]
                i += 3
                ch = st & 0x0F
                if v > 0:
                    active[p] = t
                elif p in active:
                    events.append((active.pop(p), p, t - active.get(p, t), ch))
            elif st & 0xF0 == 0x80:              # note off
                p = body[i + 1]
                i += 3
                if p in active:
                    s0 = active.pop(p)
                    events.append((s0, p, t - s0, st & 0x0F))
            elif st & 0xF0 in (0xA0, 0xB0, 0xE0):
                i += 3
            elif st & 0xF0 in (0xC0, 0xD0):
                i += 2
            else:
                i += 1
        pos += 8 + ln

    # 只保留旋律通道（非 9 号鼓通道，且音高不在低音区）
    mel = [e for e in events if e[3] != 9 and 48 <= e[1] <= 96]
    if not mel:
        mel = [e for e in events if e[3] != 9]
    if not mel:
        raise ValueError("这个 MIDI 里没有可用的旋律音符")

    bar_ticks = div * 4
    bars = []
    for start, pitch, dur, _ch in sorted(mel):
        bi = int(start // bar_ticks)
        while len(bars) <= bi:
            bars.append({"chord": "Am", "notes": []})
        bars[bi]["notes"].append({
            "pitch": int(pitch),
            "dur": round(max(0.25, min(4.0, dur / float(div))), 2),
        })
    # 丢掉空小节（有些 MIDI 前面有大段空白）
    return [b for b in bars if b["notes"]]


def run_music_import(src, path=None, tempo=150):
    """导入现成 MIDI → 用 FC 音色渲染成 MP3。

    这是"旋律外供"的路子：好听的旋律来自真实音乐（网上的转录 MIDI、
    你自己画的、别人写的），我这边只负责把它染成 FC 的方波音色。
    音色参数是用户 A/B 试听选定的（50% 方波 + 打击包络 + 32k）。
    """
    if not src:
        return "错误：需要给一个 .mid 路径"
    src = _resolve(src)
    if not os.path.isfile(src):
        return "错误：找不到 %s" % src
    if not str(src).lower().endswith((".mid", ".midi")):
        return "错误：只支持 .mid / .midi"

    try:
        bars = _parse_midi_melody(src)
    except Exception as exc:                                   # noqa: BLE001
        return "错误：解析失败 %s" % exc
    if not bars:
        return "错误：解析出来没有音符"

    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import fc_synth
    except ImportError as exc:
        return "错误：fc_synth 不可用（%s）" % exc

    base = os.path.splitext(str(path or os.path.basename(src)))[0]
    wav = _resolve(base + ".wav")
    err = _guard_write(wav)
    if err:
        return err
    try:
        info = fc_synth.render_notes(wav, bars, int(tempo or 150))
    except Exception as exc:                                   # noqa: BLE001
        return "错误：渲染失败 %s" % exc
    audio, compressed = _compress_wav(wav)
    notes = sum(len(b["notes"]) for b in bars)
    return ("已从 %s 导入并渲染：%s（%s 秒，%d 小节，%d 个旋律音符，%s；"
            "伴奏由工具补）"
            % (os.path.basename(src), os.path.basename(audio), info["seconds"],
               len(bars), notes,
               "MP3 %.0fKB" % (os.path.getsize(audio) / 1024.0)
               if compressed else "WAV"))


def _busy_hint():
    """启动时先看一眼系统忙不忙。

    这块板子是"兼职"的：Docker/Gitea/Caddy/MySQL 都在跑，4 核很容易满载。
    3B 本身就 1.9 t/s，再被抢 CPU 会出现"每步三五分钟"的情况，
    先提示一句，免得误以为程序卡死。
    """
    load = avail = None
    try:
        with open("/proc/loadavg") as fh:
            load = float(fh.read().split()[0])
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable"):
                    avail = int(line.split()[1]) // 1024
                    break
    except (OSError, ValueError):
        pass
    hints = []
    if load is not None and load > 3.0:
        hints.append("负载 %.1f（4 核接近满载）" % load)
    if avail is not None and avail < 700:
        hints.append("可用内存只有 %d MB" % avail)
    if hints:
        return "提示：%s —— 推理会明显变慢，不是卡死。" % "，".join(hints)
    return None


CURRENT_MODEL = os.environ.get("AGENT_MODEL", "qwen2.5-3b-q4_k_m.gguf")


def _looks_like_math(task):
    """判断是不是数学/逻辑应用题（需要推理，不是简单算术）。"""
    t = str(task or "")
    if not t:
        return False
    # 有应用题特征词，且不是纯文件/系统操作。
    # 工程问题（甲做12天乙做15天）、行程、浓度、折扣这些都要算进来，
    # 否则会走普通路径让 3B 自己硬算 —— 实测必错。
    has_math = re.search(r"计算|求解|解方程|面积|周长|体积|比例|多少平方|"
                         r"长方形|矩形|正方形|梯形|三角形|对角线|半径|直径|"
                         r"多少立方|多少厘米|多少米|多少钱|平均|总价|打折|"
                         r"速度|路程|工作时间|分配|倍|商|被除数|"
                         r"单独做|单独干|合作|完成|每天|每天做|几天|"
                         r"分之[一二三四五六七八九十\d]|小时|分钟|"
                         r"工作效率|相遇|追及|浓度|盈利|亏损|利润|"
                         r"水池|注水|放水|年龄|方程|鸡.{0,2}兔|"
                         r"各多少|多少只|多少头|多少个|几只|几头|"
                         r"共有|总共|一共|合计|之和|之差|"
                         r"各几|分别是多少|求.{0,6}多少", t)
    has_ops = re.search(r"文件|目录|磁盘|系统|进程|日志|PPT|幻灯片|音乐|MIDI|"
                        r"网页|下载|抓取|编译|安装|配置", t)
    return bool(has_math) and not has_ops


def _looks_like_music(task):
    """音乐/MIDI 生成任务（要出曲子，不是查找音乐文件）。"""
    t = str(task or "")
    if not t:
        return False
    # 先剥掉页面注入的「【用户已上传的文件…】/tmp/llama_agent/uploads/xxx」——
    # 里面的"文件"两个字会把音乐任务误判成"文件操作"，
    # 于是掉进文档分支让模型参与，时长/风格全失控（用户实测两次都是 60 秒）。
    t2 = re.sub(r"【[^】]{0,24}上传[^】]*】", "", t)
    t2 = re.sub(r"/tmp/llama_agent/\S+", "", t2)
    if re.search(r"文件|目录|磁盘|进程|日志", t2):
        return False
    return bool(re.search(r"midi|音乐|曲子|配乐|背景音乐|bgm|旋律|伴奏|"
                          r"作曲|来首歌|来段|来一首|生成.{0,4}歌", t2, re.I))


def _math_kind(task):
    """细分数学题该用哪个工具。

    'compute'  多步计算（平均速度、浓度、折扣、利润、效率）——
               这类题列不出简洁方程，必须分步算。实测模型会把它当几何题
               列出 (x/2)^2+(3x/2)^2=12^2 这种鬼东西，所以要走 python 脚本。
    'equation' 能列方程的（球数、年龄、鸡兔、和差倍）→ sympy solve。
    """
    t = str(task or "")
    if re.search(r"平均|速度|速率|浓度|折扣|利润|盈利|亏损|工作效率|"
                 r"密度|压强|功率|百分比|增长率|利息|税率|"
                 # 几何题（长方形/圆/对角线）要设比例 k、勾股、代回，分步算才对；
                 # 走 sympy solve 会列成 (x/2)^2+(3x/2)^2=12^2 这种（实测）
                 r"长方形|矩形|正方形|梯形|三角形|圆|对角线|半径|直径|"
                 r"面积|周长|体积|勾股", t):
        return "compute"
    # 计划 vs 实际（原计划/提前/还差）这类能列一元方程 → 走 solve，
    # 不要归到 compute：3B 分步会算出 10800/2391≈45 然后编个 43200（实测）
    return "equation"


def _looks_like_visual(task):
    """图形/空间推理题（火柴棍、七巧板、折纸、立方体涂色、镜像旋转…）。

    3B 没有任何视觉/图形理解能力，硬做只会乱调工具：实测一道火柴题
    （"数字 9 移动几根火柴得到最大数"）它编了 calc 10^2-(5+3)、
    写了个内容为 92 的文件、又 read 一个打错路径的文件，四步全废。
    这类题的正确处理是直接承认做不到，而不是假装在算。
    """
    t = str(task or "")
    if not t:
        return False
    return bool(re.search(r"火柴|七巧板|折纸|展开图|拼图|剪纸|镜像|"
                          r"拼成.{0,6}(数字|图形|形状|正方形|三角形)|"
                          r"移动.{0,4}根|立方体.{0,8}涂|正方体.{0,8}涂|"
                          r"方块.{0,4}拼|表面涂色|切成.{0,6}块", t))


def run_math(problem):
    """数学/逻辑推理题：单独走一次"无 JSON 约束"的自由生成。

    为什么必须单独开一路：agent 主循环强制输出 JSON 动作（schema → grammar），
    模型没有地方写推理，只能憋出一个式子。实测同一道题
    「长宽比 5:2、对角线 29」，对话模式答 290 全对，agent 模式答 1219.45 全错。
    这里绕开约束让它完整推理，把过程和结论带回主循环。
    """
    problem = str(problem or "").strip()
    if not problem:
        return "错误：problem 为空"
    prompt = (problem + "\n\n请一步一步推理（该设未知数就设，"
              "能符号化简就化简，别一上来就拉小数硬算），"
              "最后单独一行写「答案：<结果>」。")
    try:
        text = chat(CURRENT_MODEL, [{"role": "user", "content": prompt}],
                    600, use_schema=False)
    except Exception as exc:                                   # noqa: BLE001
        return "错误：推理失败 %s" % exc
    text = (text or "").strip()
    if not text:
        return "错误：模型没有输出"
    # LaTeX 标记对终端/页面不友好。这里做一次性清理。
    # 坑：模型有时写 \frac、有时只写 frac（没反斜杠），所以正则都用 \+ 兼容零个或多个；
    #     \text{共同效率} 这种也要剥掉外壳只留内容。
    text = re.sub(r"\\+[\[\]()]", "", text)
    text = re.sub(r"\\*text\{([^{}]*)\}", r"\1", text)
    text = re.sub(r"\\*sqrt\{([^{}]*)\}", r"√(\1)", text)
    text = re.sub(r"\\*frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", text)
    text = re.sub(r"\\+(times|cdot|div|pm)\b",
                  lambda m: {"times": "×", "cdot": "·",
                             "div": "÷", "pm": "±"}[m.group(1)], text)
    super_map = {"2": "²", "3": "³", "4": "⁴", "5": "⁵",
                 "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹"}
    text = re.sub(r"\^\{?(\d)\}?",
                  lambda m: super_map.get(m.group(1), m.group(0)), text)
    text = text.replace("\\", "")
    # 收尾：剩下的裸花括号（{3/5} 之类）直接去掉，别让用户看到源码
    text = re.sub(r"[{}]", "", text)
    return "模型推理过程：\n" + clip(text, 900)


def run_solve(equations, variables=None):
    """用 sympy 精确解方程/方程组。

    为什么不能让模型自己解：实测它会把「Y=20-W 代入 R+Y=21」错做成
    「代入 R+W=19」，推出 R-W=-1（正确是 +1），答案 8/13/9 连自己列的
    第二个方程都不满足，而且它不会回头验证。
    正确分工：模型只负责把题目翻译成方程（这一步它做得好），
    sympy 负责解 + 把自己代的解回原方程验证。
    """
    try:
        import sympy as sp
    except ImportError:
        return "错误：sympy 未安装（python3 -m pip install sympy）"

    if isinstance(equations, str):
        equations = [equations]
    if not isinstance(equations, list) or not equations:
        return ('错误：equations 必须是非空数组，例如 '
                '["R+Y=21","Y+W=20","R+W=19"]')

    exprs, syms = [], set()
    for raw in equations[:8]:
        s = str(raw or "").strip()
        if not s:
            continue
        # sympy 不认 "2x+3y" 这种隐式乘法（人写方程常这么写），补成 "2*x+3*y"。
        # 只补「数字×字母」和「)×字母/括号」，不碰「字母×字母」——
        # 否则 sqrt( / sin( 这类函数名会被拆成 s*q*r*t(。
        s = re.sub(r"(\d)\s*([a-zA-Z(])", r"\1*\2", s)
        s = re.sub(r"(\))\s*([a-zA-Z(])", r"\1*\2", s)
        try:
            if "=" in s:
                lhs, rhs = s.split("=", 1)
                expr = sp.sympify(lhs) - sp.sympify(rhs)
            else:
                expr = sp.sympify(s)
        except Exception as exc:                               # noqa: BLE001
            return "错误：方程 %r 解析失败（%s）" % (s, exc)
        exprs.append(expr)
        syms |= expr.free_symbols
    if not exprs:
        return "错误：没有可用方程"

    var_list = sorted(syms, key=lambda x: x.name)
    if variables:
        wanted = [str(v).strip() for v in (variables if isinstance(variables, list)
                                           else [variables]) if str(v).strip()]
        if wanted:
            var_list = [sp.Symbol(w) for w in wanted]

    try:
        sol = sp.solve(exprs, var_list, dict=True)
    except Exception as exc:                                   # noqa: BLE001
        return "错误：求解失败 %s" % exc
    if not sol:
        return "无解或方程不相容：%s" % "；".join(str(e) for e in equations[:8])

    lines = []
    for s in sol[:4]:
        lines.append("，".join("%s = %s" % (v.name, s.get(v, "?")) for v in var_list))

    # 代回原方程验证：任何一条不满足，就说明模型列的方程本身有问题
    bad = []
    for expr in exprs:
        for s in sol[:1]:
            try:
                residual = expr.subs(s)
                if residual.free_symbols:
                    continue                      # 自由变量，跳过
                if abs(complex(residual)) > 1e-9:
                    bad.append(str(expr))
            except Exception:                                  # noqa: BLE001
                pass

    verdict = "代回原方程全部满足 ✓" if not bad else (
        "代回后有方程不满足：%s —— 说明列出的方程本身有误，请重新核对题目"
        % "；".join(bad[:3]))
    return "解：%s\n验证：%s" % (" | ".join(lines), verdict)


def _find_upload(path):
    """把模型给的文件名解析到 uploads/ 下真实存在的文件。

    3B 经常把 bilibili_videos.xlsx.txt 猜成 bilibili_videos.txt（吞掉一截），
    直接 open 就是 No such file。这里按「去扩展名后同一 stem」模糊匹配一次。
    """
    p = _resolve(path)
    if os.path.isfile(p):
        return p
    updir = os.path.join(WORK_DIR, "uploads")
    base = os.path.basename(str(path or "").strip())
    if not base:
        return p
    cands = [base, os.path.splitext(base)[0],
             os.path.splitext(os.path.splitext(base)[0])[0]]
    try:
        # 只在「文本类」文件里找。绝对不能匹配到原始 .xlsx/.pdf（那是 ZIP/二进制，
        # 读出来是 PK\x03\x04 开头的乱码 —— 实测删掉 .txt 后就落到了 .xlsx 上）
        files = [f for f in os.listdir(updir)
                 if os.path.splitext(f)[1].lower() in
                 (".txt", ".md", ".csv", ".json", ".log")]
    except OSError:
        return p
    for cand in cands:
        if not cand:
            continue
        for f in files:
            if f == cand or os.path.splitext(f)[0] == cand \
                    or os.path.splitext(os.path.splitext(f)[0])[0] == cand:
                return os.path.join(updir, f)
    # 最后再按前缀匹配（模型可能把后缀全丢了）
    stem = os.path.splitext(base)[0]
    for f in files:
        if stem and f.startswith(stem.split(".")[0]):
            return os.path.join(updir, f)
    return p


def run_table_top(path, column=None, limit=10, desc=True):
    """从「| 分隔的文本表格」里按某列排序，返回前 N 行。

    为什么要单做这个工具：3B 不会写 awk/sort，read 又只能看到前 4000 字
    （整张表读不完），所以「播放量 TOP10 / 排名」这类任务它只能编。
    这里直接读文件、按数值列排序、只回前 N 行。
    """
    try:
        p = _find_upload(path)
        with open(p, "rb") as fh:
            head = fh.read(2048)
        if b"\0" in head[:1024]:
            return ("错误：%s 看起来是二进制文件（不是转换后的文本）。"
                    "请重新上传，或改用 uploads/ 下的 .txt" % os.path.basename(p))
        with open(p, "r", errors="replace") as fh:
            raw = fh.read()
    except Exception as exc:                                   # noqa: BLE001
        return "错误：%s" % exc

    rows = [ln for ln in raw.split("\n") if "|" in ln]
    if len(rows) < 2:
        return "错误：这个文件里没有 | 分隔的表格（前几行不像表头+数据）"

    # 只取第一个工作表的数据：
    # 新格式（第一个表不带标题行）→ blocks[0] 就是第一个表；
    # 旧格式（每个表都有 "── 工作表 ──" 标题）→ blocks[0] 为空，取后面的。
    if "── 工作表" in raw:
        _blocks = re.split(r"── 工作表[^─]*──\n", raw)
        _first = next((b for b in _blocks if b.strip()), "")
        rows = [ln for ln in _first.split("\n") if "|" in ln]

    header = [c.strip() for c in rows[0].split("|")]
    data = rows[1:]

    # 选排序列：优先用户给的名字，否则挑第一个「大部分是数字」的列
    idx = None
    if column:
        col = str(column).strip()
        for i, h in enumerate(header):
            if col and (col in h or h in col):
                idx = i
                break
    if idx is None:
        for i in range(len(header)):
            nums = 0
            for ln in data[:30]:
                cells = ln.split("|")
                if i < len(cells):
                    v = cells[i].strip().replace(",", "")
                    try:
                        float(v)
                        nums += 1
                    except ValueError:
                        pass
            if nums >= min(5, len(data[:30])):
                idx = i
                break
    if idx is None:
        idx = 0

    def key(ln):
        cells = ln.split("|")
        v = cells[idx].strip().replace(",", "") if idx < len(cells) else ""
        try:
            return float(v)
        except ValueError:
            pass
        # "1:28" / "12:03" → 秒；"1:02:03" → 秒。
        # 时长列是这种格式，float() 全部失败会退化成 -inf，等于没排序
        # （用户看到的现象就是"时长列不识别"）。
        m = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})", v)
        if m:
            hh = int(m.group(1) or 0)
            return hh * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        if v.endswith("万"):
            try:
                return float(v[:-1]) * 10000
            except ValueError:
                pass
        return float("-inf") if desc else float("inf")

    data.sort(key=key, reverse=bool(desc))
    try:
        n = max(1, min(int(limit or 10), 50))
    except (TypeError, ValueError):
        n = 10

    # 只输出「名称列 + 排序列」两列。8 列全给 3B 它会抄错列 ——
    # 实测把 BV 号当视频名输出（"BV19tej6oEi5 | 387.3万"）。
    # 优先用「易读」列（387.3万 这种），模型不用自己换算。
    # 优先用「同名列的易读版本」（播放量 → 播放量(易读)：387.3万 这种）。
    # 注意必须同名，否则按"弹幕数"排序时会把"播放量(易读)"当成值列输出。
    val_idx = idx
    for i, h in enumerate(header):
        if "易读" in h and header[idx] and header[idx] in h:
            val_idx = i
            break
    name_idx = 0 if idx != 0 else (1 if len(header) > 1 else 0)

    # 输出 markdown 表格，而不是 "名称 | 数值" 拼一行 —— 后者在页面上就是
    # 一段对齐不了的纯文本（用户原话："没有明显的分列展示"）。
    # 名称里的半角竖线会破坏表格，换成全角。
    out = ["按「%s」%s排序，%s %d 条："
           % (header[idx], "降序" if desc else "升序",
              "最高" if desc else "最低", n), ""]
    out.append("| %s | %s |" % (header[name_idx], header[val_idx]))
    out.append("|---|---|")
    for ln in data[:n]:
        cells = ln.split("|")
        name = cells[name_idx].strip() if name_idx < len(cells) else ""
        val = cells[val_idx].strip() if val_idx < len(cells) else ""
        if len(name) > 40:
            name = name[:40] + "…"
        name = name.replace("|", "｜")
        out.append("| %s | %s |" % (name, val))
    return "\n".join(out)


def run_table_group(path, group_col, agg_col=None, top=10, desc=True):
    """按某列分组统计，取前 N 组（两层筛选）。

    例：
      "作者里视频数量最多"   → group_col="作者", agg_col=None（计数）
      "作者的总播放量最高"   → group_col="作者", agg_col="播放量"（求和）
    """
    try:
        p = _find_upload(path)
        with open(p, "r", errors="replace") as fh:
            raw = fh.read()
    except Exception as exc:                                   # noqa: BLE001
        return "错误：%s" % exc

    rows = [ln for ln in raw.split("\n") if "|" in ln]
    if "── 工作表" in raw:
        _blocks = re.split(r"── 工作表[^─]*──\n", raw)
        _first = next((b for b in _blocks if b.strip()), "")
        rows = [ln for ln in _first.split("\n") if "|" in ln]
    if len(rows) < 2:
        return "错误：文件里没有 | 分隔的表格"

    header = [c.strip() for c in rows[0].split("|")]
    data = rows[1:]

    gi = None
    if group_col:
        gc = str(group_col).strip()
        for i, h in enumerate(header):
            if gc and (gc in h or h in gc):
                gi = i
                break
    if gi is None:
        return ("错误：找不到分组列「%s」。文件里的列有：%s"
                % (group_col, "、".join(h for h in header if h)))

    ai = None
    if agg_col:
        ac = str(agg_col).strip()
        for i, h in enumerate(header):
            if ac and (ac in h or h in ac):
                ai = i
                break

    counts = {}
    for ln in data:
        cells = ln.split("|")
        if gi >= len(cells):
            continue
        key = cells[gi].strip()
        if not key:
            continue
        if ai is None:
            counts[key] = counts.get(key, 0) + 1
        else:
            v = cells[ai].strip().replace(",", "") if ai < len(cells) else ""
            try:
                counts[key] = counts.get(key, 0.0) + float(v)
            except ValueError:
                continue

    items = sorted(counts.items(), key=lambda kv: kv[1], reverse=bool(desc))
    try:
        n = max(1, min(int(top or 10), 50))
    except (TypeError, ValueError):
        n = 10

    label = "视频数量" if ai is None else "%s合计" % header[ai]
    out = ["按「%s」统计%s，%s %d 组："
           % (header[gi], label, "最高" if desc else "最低", n), ""]
    out.append("| %s | %s |" % (header[gi], label))
    out.append("|---|---|")
    for k, v in items[:n]:
        if ai is None:
            vs = "%d" % v
        else:
            # 别用 %g（会出 4.59902e+06 这种科学计数法，看着像乱码）
            vs = "{:,.0f}".format(v) if v >= 1000 else ("%g" % v)
        out.append("| %s | %s |" % (k.replace("|", "｜"), vs))
    return "\n".join(out)


def run_table(path, sort_by=None, group_by=None, sum_by=None,
              limit=10, desc=True):
    """表格查询统一入口（合并了原来的 table_top / table_group）。

    三种用法，靠参数区分：
      sort_by="播放量"                     → 按该列排序取前 N 行
      group_by="作者"                      → 按该列分组计数，取前 N 组
      group_by="作者", sum_by="播放量"      → 按该列分组求和，取前 N 组
    """
    if group_by:
        return run_table_group(path, group_by, sum_by, limit, desc)
    return run_table_top(path, sort_by, limit, desc)


def run_ask(question, options=None):
    """向用户提问。3B 经常在信息不足时瞎猜，给它一个反问的口子。

    非交互环境（页面/脚本调用）不能阻塞。这里不能返回"错误"——
    实测模型拿到"错误"就懵，用户只看到一条报错。
    正确做法：告诉它「直接 final，把问题当成回答」。
    """
    if not sys.stdin.isatty():
        q = str(question or "").strip() or "请补充更多信息"
        return ("（用户此刻不能即时回复）立刻调用 final，"
                "在 answer 里直接把这个问题问给用户：%s —— "
                "不要再调 ask，也不要自己编造答案。" % q)
    print("\n[提问] %s" % (question or "(无问题)"))
    if isinstance(options, list) and options:
        for i, o in enumerate(options[:6]):
            print("  %d) %s" % (i + 1, o))
    try:
        ans = input("回答> ").strip()
    except (EOFError, KeyboardInterrupt):
        return "用户中断了回答"
    return "用户回答：%s" % (ans or "(空)")


def clip(text, n=MAX_TOOL_OUTPUT):
    text = str(text)
    if len(text) <= n:
        return text
    return text[:n] + "\n…（截断，原长 %d 字符）" % len(text)


# -------------------------------------------------------------------- loop
def _interactive(args):
    """连续任务模式：一行一个任务。用子进程跑，保证每个任务状态干净
    （3B 的上下文很短，跨任务复用历史反而会串味）。"""
    print("交互模式：一行一个任务，空行或 Ctrl-D 退出。")
    while True:
        try:
            line = input("\n任务> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            break
        cmd = [sys.executable, os.path.abspath(__file__), line,
               "--model", args.model, "--max-steps", str(args.max_steps)]
        if args.readonly:
            cmd.append("--readonly")
        try:
            subprocess.run(cmd)
        except KeyboardInterrupt:
            print("\n（已中断当前任务）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task", nargs="?", default=None,
                    help="任务描述；含中文时建议改用 -f 传文件，避免 shell 把中文转成 \\uXXXX")
    ap.add_argument("-f", "--task-file", default=None,
                    help="从 UTF-8 文件读任务（推荐，避免命令行编码问题）")
    ap.add_argument("--model", default="qwen2.5-3b")
    ap.add_argument("--max-steps", type=int, default=MAX_STEPS_DEFAULT)
    ap.add_argument("--readonly", action="store_true",
                    help="只读模式：禁用 write/pptx 和写类 shell 命令")
    ap.add_argument("-i", "--interactive", action="store_true",
                    help="交互模式：一行一个任务，连续执行")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    # math 工具内部要用同一个模型做自由推理
    global CURRENT_MODEL
    CURRENT_MODEL = args.model

    if args.interactive:
        _interactive(args)
        return 0

    if args.task_file:
        with open(args.task_file, "r", encoding="utf-8") as fh:
            task = fh.read().strip()
    elif args.task:
        task = args.task
    else:
        ap.error("需要任务描述：直接给字符串，或用 -f 指定 UTF-8 文件")

    # 命令行偶尔会传来字面的 \uXXXX 转义（不是中文），转回来
    if "\\u" in task:
        try:
            task = task.encode("utf-8").decode("unicode_escape")
        except Exception:                                      # noqa: BLE001
            pass
    # 修复乱码（中文被当 latin-1 解码，"ÇëÐ´" 这种）：不修的话关键词识别全废
    _fixed = _fix_mojibake(task)
    if _fixed != task:
        say("（任务文本检测到乱码，已还原为中文）")
        task = _fixed

    # "继续 / 再来一个" 这类没有内容的任务：接上一次。
    # agent 每次都是全新进程，内存里没有任何上下文，模型看到"继续"只会犯傻
    # （实测直接乱调工具）。这里把上一次的任务取回来重做一遍。
    LAST_TASK = "/tmp/llama_agent/.last_task"
    if re.fullmatch(r"\s*(继续|接着|再来|再来一个|再生成|再做|下一个|同上|"
                    r"continue|again)[。.!！?？]?\s*", task or "", re.I):
        try:
            with open(LAST_TASK, "r") as fh:
                prev = fh.read().strip()
        except Exception:                                      # noqa: BLE001
            prev = ""
        if prev:
            task = prev
            if not args.quiet:
                print("（“继续”理解为重做上一次任务：%s —— 换一个结果）"
                      % clip(prev, 50), flush=True)

    def say(*a):
        if not args.quiet:
            print(*a, flush=True)

    _step_budget = args.max_steps      # 各分支按任务类型下调，硬上限 MAX_STEPS_HARD
    _preloaded = False       # 是否已经把文件内容/排序结果预置进上下文
    _preloaded_answer = ""   # 预置的数据本身（模型不听话时直接拿它当答案）
    seen = {}            # 工具调用签名 -> 次数，用来打断小模型的复读
    repeats = 0
    doc_read = _preloaded   # 已经预置了文件内容/排序结果就视为"已读"
    t_start = time.time()
    n_tools = 0

    say("任务: %s" % task)
    _kind, _kind_cn = _classify_task(task)
    say("（识别为：%s）" % _kind_cn)
    say("模型: %s   步数预算 %d（硬上限 %d）"
        % (args.model, min(_step_budget, MAX_STEPS_HARD), MAX_STEPS_HARD))
    hint = _busy_hint()
    if hint:
        say(hint)
    say("")

    # 应用题必须走 math（主循环的 JSON 约束会掐死推理，calc 只能算"已经列好的式子"）。
    # prompt 里的建议 3B 不听，实测它照样选 calc 然后瞎编式子；
    # 所以在任务后面硬塞一条指令，实测这样它才会换工具。
    task_for_model = task
    # 音乐任务：生成前把旧文件清掉。实测模型看到 bgm.mid 已存在，
    # 就会一直去 music_render/music_import 那个旧文件（被拦了也不改），
    # 既不生成新曲子，还谎报时长。把干扰源清掉它才会正常选 midi。
    if _looks_like_music(task):
        # 音乐任务：清理旧文件 + 直接生成，全程不经过模型。
        # 注意这里必须是 if 的第一个分支并且 return：midi 的真实生成逻辑全在
        # run_midi 里（和弦/节奏模板/移调），模型只负责选风格和时长两个参数。
        # 走工具循环没有任何收益 —— 3B 会时不时去调 music_render/import，
        # 然后 5 步卡死（用户实测"连续生成几次后又变成 5 步失败"）。
        # （之前这个 if 只做清理没 return，导致后面那个 elif 永远进不去，
        #   实际每次都掉回 dispatch 的代执行分支。）
        for _f in ("bgm.mid", "bgm.mp3", "agent-song.json"):
            try:
                os.remove(os.path.join(WORK_DIR, _f))
            except Exception:                              # noqa: BLE001
                pass
        say("（音乐任务：直接生成，不经过模型）")
        _st = _style_from_task(task) or _pick_style()
        _secs = _task_seconds(task)
        # "要有和声"这类要求：模板本来就带和弦进行 + 钢琴琶音 + 贝斯根音，
        # 明确说出来，避免用户以为被忽略了。
        if re.search(r"和声|和弦|伴奏|harmony|chord", task, re.I):
            say("  （要求里有和声：已用和弦进行 + 钢琴琶音 + 贝斯根音实现）")
        # 用户明确给了"每小节 N 个音 + 共 N 小节"→ 分批创作（模型写音符，工具合并）。
        # 这种要求必须让模型真的写音符，不能用模板；32 小节拆成 8 批才写得完。
        _pb = re.search(r"每小节\s*(\d+)\s*(?:个)?音", task)
        _bs = re.search(r"(\d+)\s*小节", task)
        if _pb and _bs:
            say("（分批创作：%s 小节 × 每小节 %s 音，最后合并）"
                % (_bs.group(1), _pb.group(1)))
            _res = _midi_batched(args.model, "bgm.mid", int(_bs.group(1)),
                                 int(_pb.group(1)),
                                 STYLE_TEMPO.get(_st, 120), say=say)
        else:
            _res = run_midi("bgm.mid", _st, STYLE_TEMPO.get(_st, 90),
                            32, None, _secs)
        say("\n最终回答: %s" % _res)
        say("[统计] 0 步 / 0 次工具调用 / 总耗时 %.1f 秒"
            % (time.time() - t_start))
        try:
            with open(LAST_TASK, "w") as fh:
                fh.write(task)
        except Exception:                                  # noqa: BLE001
            pass
        return 0
    elif _looks_like_ppt(task):
        # PPT：模型写「纯文本大纲」（不走 JSON schema），工具解析成 slides。
        # 数据由服务端预处理成摘要 —— 模型不用 read 全文，只负责提炼要点。
        say("（PPT 任务：模型写大纲 → 工具解析排版）")
        _pp = re.findall(r"/tmp/llama_agent/uploads/[\w.\-\u4e00-\u9fff]+", task)
        if not _pp:
            try:
                _up2 = os.path.join(WORK_DIR, "uploads")
                _fs2 = sorted([f for f in os.listdir(_up2)
                               if f.lower().endswith(".txt")],
                              key=lambda f: os.path.getmtime(os.path.join(_up2, f)),
                              reverse=True)
                if _fs2:
                    _pp = [os.path.join(_up2, _fs2[0])]
            except Exception:                              # noqa: BLE001
                pass
        _psum = _ppt_summary(_pp[0]) if _pp else ""
        # 分批写：每节一次短 JSON 调用（几十分钟的长 JSON 写不出来）
        _topics = _ppt_topics(_psum)
        _secs = _ppt_sections_batched(args.model, _psum, _topics, say=say)
        if not _secs:
            say("  ! 分批没拿到内容，退回纯文本大纲")
            try:
                _outline = chat(args.model, [
                    {"role": "system",
                     "content": "你负责写 PPT 大纲。只输出大纲文本，格式为 "
                                "「# 章节标题」下一行起用「- 要点」。"
                                "不要输出 JSON、解释或代码块。"},
                    {"role": "user",
                     "content": "根据下面数据写 4 个章节的 PPT 大纲，"
                                "每条要点不超过 20 字，数字必须来自数据：\n\n"
                                + (_psum or task)},
                ], max_tokens=400, use_schema=False)
                _secs = _parse_outline(_outline)
            except Exception:                              # noqa: BLE001
                _secs = []
        if not _secs:
            _secs = [{"title": "数据概览",
                      "bullets": [((_psum or task)[:80] or "（无内容）")]}]
            say("  ! 模型两次都没给出内容，用数据摘要兜底")
        _res = run_pptx("out.pptx", None, _ppt_title(task), _secs)
        say("\n最终回答: %s" % _res)
        say("[统计] %.1f 秒（%d 次模型调用 + 工具排版）"
            % (time.time() - t_start, len(_topics)))
        try:
            with open(LAST_TASK, "w") as fh:
                fh.write(task)
        except Exception:                                  # noqa: BLE001
            pass
        return 0
    elif _looks_like_doc_task(task):
        _step_budget = STEP_BUDGET["doc"]          # read + final = 2 步够了
        say("（文档任务：read 文本后直接回答，不调 calc/write）")
        # 要 TOP / 排名 → 工具层先把排序结果算好塞进上下文。
        # 原因：3B 不会主动用 table_top（实测只 read 前 4000 字就自己数），
        # 而 read 只能看到前 4000 字，整张表根本数不全。
        _inject = ""
        _paths = re.findall(r"/tmp/llama_agent/uploads/[\w.\-\u4e00-\u9fff]+", task)
        # 兜底：任务里没带路径（用户手动输入 / 刷新后上传记录丢了）→
        # 用 uploads/ 里最近修改的那个 .txt，并明确打出来用了哪个文件。
        if not _paths:
            try:
                _up = os.path.join(WORK_DIR, "uploads")
                _fs = sorted([f for f in os.listdir(_up) if f.lower().endswith(".txt")],
                             key=lambda f: os.path.getmtime(os.path.join(_up, f)),
                             reverse=True)
                if _fs:
                    _paths = [os.path.join(_up, _fs[0])]
                    say("  （任务里没写文件，自动用最新的上传文件：%s）" % _fs[0])
            except Exception:                              # noqa: BLE001
                pass
        # 两层筛选：先分组统计，再取前 N 组。
        # 例："作者里视频数量最多的 top 10" → group_col=作者, 计数, 取前10。
        # 必须放在单列排序之前判断，因为这类任务同时含 "top 10"。
        _hdrs_all = []
        try:
            with open(_paths[0], "r", errors="replace") as _fh:
                for _ in range(4):
                    _line = _fh.readline()
                    if not _line:
                        break
                    if _line.count("|") >= 2:
                        _hdrs_all = [c.strip() for c in _line.split("|")]
                        break
        except Exception:                                  # noqa: BLE001
            pass

        # 分组（双层筛选）判据，满足任一即分组：
        #   ① 任务里有「数量/统计/每个/哪个/分别」这类词
        #   ② 任务里点了「分类列」——作者/分区/来源 本身不是数值，
        #      说"作者里视频最多的"必然是分组，"作者"三个字就是信号。
        #      之前只认 ①，所以"作者里视频最多的 top10" 掉进了单层排序，
        #      最后按播放量排了一遍（用户说的"双层筛选没被识别"）。
        _count_sig = re.search(r"数量|多少条|几条|几个|统计|分组|每个|各个|哪个|谁的|分别",
                               task)
        _cat_sig = re.search(r"作者|分区|来源|分类|发布者|主播|UP主|标签", task)
        _gcol = None
        if _paths and _hdrs_all and (_count_sig or _cat_sig):
            # 优先：任务里出现的「分类列」
            for _h in _hdrs_all:
                if _h and re.search(r"作者|分区|来源|分类|发布者|主播|UP主|标签", _h) \
                        and _h in task:
                    _gcol = _h
                    break
            # 其次：任务里出现的任意列（配合"数量/统计"这类词）
            if not _gcol and _count_sig:
                _gcol = next((h for h in _hdrs_all
                              if h and len(h) >= 2 and h in task and "名称" not in h), None)
        if _gcol:
                _acol = next((h for h in _hdrs_all
                              if h and h != _gcol and h in task
                              and "名称" not in h and re.search(r"量|金额|价", h)), None)
                _gdesc = not re.search(r"最少|最低|最小|最短|倒数|bottom",
                                       task, re.I)
                _gtxt = run_table(_paths[0], None, _gcol, _acol,
                                  _extract_top_n(task), _gdesc)
                _preloaded_answer = _gtxt
                _inject = ("\n\n【工具已按「%s」分组统计好%s（无需自己数），"
                           "直接用这些数据回答】\n%s"
                           % (_gcol, ("，聚合列 %s" % _acol) if _acol else "（计数）",
                              _gtxt))
                say("  （table 分组：按「%s」统计%s）"
                    % (_gcol, ("，聚合 %s" % _acol) if _acol else ""))
                _preloaded = True
                doc_read = True
        elif _paths and re.search(r"top\s*\d*|bottom\s*\d*|排名|排行|排序|最多|最高|最大|"
                                r"最少|最低|最小|最短|最长|前\s*\d|后\s*\d|倒数|"
                                r"前[一二三四五六七八九十]|后[一二三四五六七八九十]",
                                task, re.I):
            _col = None
            # 1) 先读文件表头，用真实列名匹配任务文本 —— 这个最可靠。
            #    注意：表头不一定在第一行！xlsx 转出来的文本第一行是
            #    "── 工作表 汇总 ──"，真正的表头在第二行。
            #    之前只 readline() 第一行 → 表头永远匹配不上，
            #    于是所有列都退化到"自动挑数字列"（用户说"根本没读列名"）。
            try:
                with open(_paths[0], "r", errors="replace") as _fh:
                    _hdr = ""
                    for _ in range(4):
                        _line = _fh.readline()
                        if not _line:
                            break
                        if _line.count("|") >= 2:       # 含多个列分隔符 = 表头行
                            _hdr = _line
                            break
                    for _cand in [c.strip() for c in _hdr.split("|")]:
                        if len(_cand) >= 2 and _cand in task and "名称" not in _cand:
                            _col = _cand
                            break
                    # 双向模糊匹配：用户常只说"弹幕""收藏""时长"，
                    # 而表头是"弹幕数""收藏数"→ 精确匹配失败会退化到自动挑列
                    # （用户看到的就是"我明明说了时长，它却按播放量排"）。
                    if not _col:
                        for _word in re.findall(r"[\u4e00-\u9fff]{2,8}", task):
                            for _h in [c.strip() for c in _hdr.split("|")]:
                                if not _h or "名称" in _h or len(_h) < 2:
                                    continue
                                if _word in _h or _h in _word:
                                    _col = _h
                                    break
                            if _col:
                                break
            except Exception:                              # noqa: BLE001
                pass
            # 2) 表头没命中，再用常见词兜底
            if not _col:
                for kw in ("播放量", "点赞", "投币", "收藏", "弹幕", "评论",
                           "分享", "销量", "金额", "数量", "人数",
                           "得分", "分数", "收入", "价格"):
                    if kw in task:
                        _col = kw
                        break
            # BOTTOM/最少/最短/倒数 → 升序
            _desc = not re.search(r"bottom|最少|最低|最小|最短|倒数|后\s*\d|"
                                  r"后[一二三四五六七八九十]", task, re.I)
            try:
                _top_text = run_table(_paths[0], _col, None, None,
                                      _extract_top_n(task), _desc)
                _preloaded_answer = _top_text
                _inject = ("\n\n【工具已按「%s」排序算好%s %d 条（格式：名称 | 数值），"
                           "直接用这些数据回答，不要自己再数，也不要改名称】\n%s"
                           % (_col or "数值列", "最高" if _desc else "最低",
                              _extract_top_n(task), _top_text))
                say("  （table 排序：按「%s」，%s %d 条）"
                    % (_col or "自动挑列", "最高" if _desc else "最低",
                       _extract_top_n(task)))
                _preloaded = True
                doc_read = True     # 数据已经给了，不要求它再 read
            except Exception:                                  # noqa: BLE001
                _inject = ""
        task_for_model = (task + _inject + "\n\n【强制】这是基于上传文档的任务。流程：\n"
                          "上面已经给了「工具已排序算好」的数据 → 你的第一个动作必须直接调用 final，"
                          "把那位数据原样列出来（保留换行），不要调任何工具（table_top/read 都不要）；\n"
                          "如果上面没给数据，才先用 read 读那个 .txt；\n"
                          "2) 如果任务要 TOP N / 排名 / 最大最小，直接用 table_top 工具"
                          '（{"tool":"table_top","path":"<那个.txt>","column":"<列名>","limit":10}），'
                          "它会读完整张表按数值排序 —— read 只能看到前 4000 字，你自己数不准；\n"
                          "3) 用 final 回答（总结 / 翻译 / 回答问题）。\n"
                          "禁止用 calc、write、shell —— 需要的信息全在文件里，"
                          "不用计算、不用写脚本（实测你会莫名其妙写个 calc.py 再算 1.9*30，纯浪费）。\n"
                          "数字和单位照抄原文：表格里写的是 1.9 t/s 就写 1.9 t/s，"
                          "别翻译成「每秒 1.9 秒」这种错话。\n"
                          "回答要简短：TOP10 这类只写「序号. 名称（截到 20 字） | 播放量」，"
                          "一行一条。不要画 markdown 表格、不要抄 BV 号、不要复述作者和日期 —— "
                          "写长了 JSON 会被截断，标题里的引号还会把 JSON 弄坏。")
    elif _looks_like_visual(task):
        _step_budget = STEP_BUDGET["visual"]
        say("（图形/空间推理题：3B 没有图形理解能力，直接说明做不到）")
        task_for_model = (task + "\n\n【硬性要求】这是图形/空间推理题（火柴棍/拼图/折纸一类），"
                          "你没有图形和视觉理解能力。\n"
                          "禁止调用 calc / write / shell / read / solve / math / ask —— "
                          "一个都不要调（上次编了个跟题目无关的表达式还乱写文件，全是垃圾）。\n"
                          "必须直接调用 final。answer 就按这个意思写（用你自己的话）："
                          "「这类题需要看懂图形/火柴的具体排布，我是纯文本模型，"
                          "没有图形理解能力，无法可靠解答。如果你能把图形用文字描述清楚"
                          "（比如每根火柴的位置），我可以再试试。」")
    elif _solve_collab_segmented(task):
        # 合作中途有人离开：3B 会把两段的分母搞混，工具直接解
        _seq, _sdesc = _solve_collab_segmented(task)
        say("（合作中途离开：工具直接解）")
        say("  方程: %s" % _seq)
        _sr = run_solve([_seq])
        say("  %s" % _sr)
        _sm = re.search(r"t\s*=\s*(-?\d+(?:\.\d+)?(?:/\d+)?)", _sr)
        if _sm:
            try:
                from fractions import Fraction
                say("\n最终回答: 共需要 %s 天（%s）" % (Fraction(_sm.group(1)), _sdesc))
            except Exception:                              # noqa: BLE001
                say("\n最终回答: 见上面的方程解（%s）" % _sr)
        say("[统计] 0 步 / 0 次工具调用 / 总耗时 %.1f 秒" % (time.time() - t_start))
        return 0
    elif _solve_collab(task):
        # 合作/单独完成类：3B 会把时间相加或取平均（实测错），sympy 一步到位
        _ceq, _cdesc = _solve_collab(task)
        say("（合作类题：工具直接解，不让模型算）")
        say("  方程: %s" % _ceq)
        _cr = run_solve([_ceq])
        say("  %s" % _cr)
        _cm = re.search(r"t\s*=\s*(-?\d+(?:\.\d+)?(?:/\d+)?)", _cr)
        if _cm:
            try:
                from fractions import Fraction
                _ct = Fraction(_cm.group(1))
                say("\n最终回答: 合作需要 %s 天（%s）" % (_ct, _cdesc))
            except Exception:                              # noqa: BLE001
                say("\n最终回答: 见上面的方程解（%s）" % _cr)
        say("[统计] 0 步 / 0 次工具调用 / 总耗时 %.1f 秒" % (time.time() - t_start))
        return 0
    elif _solve_plan_vs_actual(task):
        # 「计划 vs 实际」类：3B 翻译方程实测 0/3，工具直接用模板方程解。
        # 模型完全不参与算术 —— 它只会把 a±b、(t-n)、还差量这几处搞错。
        _tpl = _solve_plan_vs_actual(task)
        _eq, _a, _desc = _tpl
        say("（计划vs实际题：工具直接解，不让模型翻译方程）")
        say("  模板方程: %s" % _eq)
        _r = run_solve([_eq])
        say("  %s" % _r)
        _mv = re.search(r"t\s*=\s*(-?\d+(?:\.\d+)?(?:/\d+)?)", _r)
        if _mv:
            try:
                from fractions import Fraction
                _tv = Fraction(_mv.group(1))
                # 必须用 Fraction(a) 乘：float(760.0) * Fraction(43/3) 会退化成
                # 10893.333333333334 这种浮点，显示很难看
                _total = Fraction(str(_a)) * _tv
                if _total == int(_total):
                    _total = int(_total)          # 10800.0 → 10800
                    say("\n最终回答: 这条公路全长为 %s 米"
                        "（原计划每天 %g 米 × 计划 %s 天）" % (_total, _a, _tv))
                else:
                    # 非整数结果（如 32680/3）同时给分数和近似小数，
                    # 只给分数的话用户看不出量级，只给小数又丢了精确值
                    _dec = float(_total)
                    say("\n最终回答: 这条公路全长为 %s 米（约 %.2f 米）"
                        "（原计划每天 %g 米 × 计划 %s 天）"
                        % (_total, _dec, _a, _tv))
            except Exception:                              # noqa: BLE001
                say("\n最终回答: 见上面的方程解（%s）" % _r)
        say("[统计] 0 步 / 0 次工具调用 / 总耗时 %.1f 秒" % (time.time() - t_start))
        return 0
    elif _looks_like_math(task) and _math_kind(task) == "compute":
        _step_budget = STEP_BUDGET["compute"]
        say("（多步计算题：走 math 自由推理；solve 会列错方程）")
        task_for_model = (task + "\n\n【强制】这是多步计算题（物理/行程/工程/浓度/折扣一类）。\n"
                          "你的第一个动作必须是调用 math 工具：problem 字段填本题的原始文字"
                          "（就是把上面那段题目一字不改地放进去，不要留占位符、不要写问号）。\n"
                          "它内部会一步步算，不受 JSON 约束。\n"
                          "算的时候必须分清题干里的相对量（实测这里最容易错）：\n"
                          "  「还差 X」「还剩 X」= 未完成的部分；总量 = 已完成 + 剩余，"
                          "剩余只加一次，不要再额外加；\n"
                          "  「提前 N 天」= 实际用时 = 计划用时 − N；「延后 N 天」= 计划 + N；\n"
                          "  「每天多修 X」= 实际每天 = 原计划每天 + X。\n"
                          "禁止用 solve（这类题列不出方程，你上次列了个跟题目无关的方程，是错的），\n"
                          "禁止用 write+shell 写脚本（你写的代码常带语法错），禁止用 calc，禁止心算。\n"
                          "拿到 math 的推理后再用 final 报答案和单位。")
    elif _looks_like_math(task):
        task_for_model = (task + "\n\n【强制】这是数学应用题。\n"
                          "你的第一个动作必须是调用 solve，把题目翻译成方程。\n"
                          "equations 数组里放真实的方程字符串，例如两个量之和是 10、"
                          "之差是 2，就写 [\"a+b=10\",\"a-b=2\"]。\n"
                          "绝对不要留占位符、不要写问号、不要写\"方程1\"这种字样 —— "
                          "必须换成题目里的真实数字和字母。\n"
                          "变量名必须对应题目里的量（用题目里的真实字母，或按含义起名），"
                          "不要用 a/b/c 这种无意义的名字，也不要套用别的题目的变量，"
                          "比如鸡兔题、球数题、修路题各用各自的量名。"
                          "不要用 math（5-10 分钟的自然语言推理，会算错），"
                          "不要用 calc，不要写脚本，不要自己心算。\n"
                          "如果题目是「计划 vs 实际」类型（原计划每天/实际每天/提前/还差），"
                          "就设计划用 t 天，两条式子都等于总量："
                          "计划每天 × t = 实际每天 × (t − 提前天数) + 还差的量。\n"
                          "注意「实际每天比原计划多修 80 米」= 实际每天 = 原计划每天 + 80"
                          "（不是每天 80！实测这里写错过）。\n"
                          "求出未知数后，如果最终答案还要再算一步（比如全长 = 每天 × 天数），"
                          "就用 calc 工具算那个乘法，不要心算 —— 你心算 720×15 写成过 10920（实测错）。\n"
                          "拿到 solve 的解之后，再用 final 把每个未知数解释清楚。")
        say("（检测到应用题：强制先列方程走 solve）")

    # 纯排名任务（"BOTTOM 10，列出名称和播放量"）→ 排序好的表就是答案本身。
    # 让 3B 复述只会抄错字（实测 "快讯"→"快快讯"），load 高时还要烧 400 秒。
    # 只有要求"分析/原因/特点"这类才需要模型参与。
    _pure_rank = (_preloaded and bool(_preloaded_answer)
                  and not re.search(r"分析|为什么|原因|特点|规律|总结|解释|建议|"
                                    r"对比|评价|洞察", task))
    if _pure_rank:
        say("\n（排序结果就是答案，直接输出，不经过模型）")
        say("\n最终回答: %s" % _preloaded_answer)
        say("[统计] 0 步 / 0 次工具调用 / 总耗时 %.1f 秒"
            % (time.time() - t_start))
        try:
            with open(LAST_TASK, "w") as fh:
                fh.write(task)
        except Exception:                                  # noqa: BLE001
            pass
        return 0

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": task_for_model},
    ]

    # 动态步数：按任务类型给预算，硬上限只防死循环
    _limit = min(_step_budget, MAX_STEPS_HARD)
    if _limit != args.max_steps:
        say("（本任务预算 %d 步 / 硬上限 %d）" % (_limit, MAX_STEPS_HARD))
    used_tok = 0
    for step in range(1, _limit + 1):
        say("── 第 %d 步 ──" % step)
        # 每步的 prefill+生成要 10-60 秒（板子忙时更久），期间屏幕一片空白，
        # 先给个提示，免得被当成卡死。
        say("  （模型生成中…）")
        t_step = time.time()
        try:
            reply = chat(args.model, messages)
            # 粗估 token 消耗（中英混排约 1.6 字/token）。超预算就收尾 ——
            # 按 token 比按步数更贴近真实成本（一步可能只有 50 token，也可能 800）。
            used_tok += int(len(str(reply)) / 1.6) + 600
            if used_tok > TOKEN_BUDGET:
                # 不是失败：把已完成的工具结果存成进度摘要，清空上下文重开一局继续。
                # 已完成的部分不再重做（摘要里明确写了），只把剩下的做完。
                _ckpt = _make_checkpoint(messages)
                say("  ! token 已用约 %d（预算 %d）→ 存进度、重置上下文继续"
                    % (used_tok, TOKEN_BUDGET))
                if _ckpt.count("\n- ") > 0:
                    say("    已保留 %d 条上轮结果" % _ckpt.count("\n- "))
                messages = [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": task_for_model + _ckpt},
                ]
                used_tok = 0
                continue
        except urllib.error.URLError as exc:
            print("连不上 llama-server (%s)，先确认 %s 在跑" % (exc, API))
            return 1
        except Exception as exc:                               # noqa: BLE001
            print("请求失败: %s" % exc)
            return 1

        step_sec = time.time() - t_step
        say("模型 (%.1fs): %s" % (step_sec, reply.strip()[:300]))

        objs = extract_jsons(reply)
        if not objs:
            say("  ✗ 解析不出 JSON，提醒它按格式重来\n")
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content":
                             "格式错误：只输出一个 JSON 对象，例如 "
                             '{"tool":"shell","cmd":"ls /tmp"}'})
            continue

        # 一轮里可能有多个动作（3B 常一口气 write 两个文件），按顺序全执行；
        # 一旦出现 final / answer 就收尾。
        outcomes = []
        answer = None
        for obj in objs:
            tool = (obj.get("tool") or "").strip().lower()
            # think 是模型的推理草稿（数学题必须先列方程），既能让它"想"，
            # 也能让用户看到思路。放在动作前面显示。
            think = str(obj.get("think") or "").strip()
            if think:
                say("  [思考] %s" % clip(think, 240).replace("\n", " "))
            # 已经把排序结果预置进上下文了，模型还去调工具 → 直接拿预置数据收尾。
            # 3B 拿到数据仍然会去 table_top/read（实测连压两次也不改），
            # 继续让它绕只会浪费几百秒还到不了 final。
            if _preloaded and tool != "final":
                say("  ! 数据已给出，跳过 %s，直接收尾" % tool)
                answer = _preloaded_answer
                break
            if tool == "final":
                # 文档任务：没读过上传文件就想回答 → 直接驳回。
                # 实测 3B 会跳过 read 直接编（"视频 A - 播放量 23456789"），
                # 1 步 0 工具调用就给你一份假数据。
                if _looks_like_doc_task(task) and not doc_read:
                    outcomes.append("（系统）你还没读用户上传的文件就想回答，"
                                    "这必然是编的。必须先调用 read 读那个 .txt 文件。")
                    say("  ! 未读文件就要作答，已驳回")
                    continue
                ans = obj.get("answer", "")
                # 模型有时把换行写成字面的 \n（1.5B 尤其常见），显示出来就是
                # 一整行挤在一起。这里转成真换行。
                if isinstance(ans, str) and "\\n" in ans:
                    ans = ans.replace("\\n", "\n")
                # 模型爱给答案包引号，而且首尾还经常不对称（'内容"）。
                # 逐层剥掉首尾的引号字符，别只判断首尾相同。
                if isinstance(ans, str):
                    ans = ans.strip()
                    while len(ans) > 1 and ans[0] in "'\"“”‘’":
                        ans = ans[1:].strip()
                    while len(ans) > 1 and ans[-1] in "'\"“”‘’":
                        ans = ans[:-1].strip()
                answer = ans
                break
            # 小模型把 final 字段混进工具调用：{"tool":"du",...,"answer":"..."}
            # 但 pptx 例外——它可能真的带 slides，要先执行再收尾，否则文件不会被写
            mixed_answer = obj.get("answer")
            if mixed_answer and tool not in ("write", "pptx"):
                answer = obj["answer"]
                break

            sig = (tool + ":" +
                   json.dumps(obj, sort_keys=True, ensure_ascii=False))
            # 音乐生成任务里，模型反复调"逐小节手写/渲染旧文件"那套是常态
            # （3B 改不过来），这些不算"无效复读"，否则会把它挡在重复检测上死循环。
            _music_legacy = (_looks_like_music(task)
                             and tool in ("music_start", "music_bar",
                                          "music_notes", "music_render",
                                          "music_import"))
            if sig in seen and not _music_legacy:
                repeats += 1
                say("  ! 重复同一动作（第 %d 次），不执行\n" % repeats)
                if repeats >= 2:
                    say("  ! 连续复读，强制要求收尾\n")
                    outcomes.append("（系统）你重复同一个动作 %d 次了，必须给 final。"
                                    % repeats)
                    continue
                outcomes.append("错误：这个动作刚刚做过，结果就在上面，不要重复。")
                continue

            seen[sig] = 1
            repeats = 0
            # 慢工具执行前先报一声。math 内部要生成几百 token 的推理，
            # 期间完全没有输出，用户会以为卡死了。
            _hint = {"math": "模型推理中，可能 3-10 分钟（要生成完整推导，实测有到 9 分钟的）",
                     "solve": "精确求解中，几秒就好",
                     "pptx": "生成 PPT 中",
                     "midi": "生成 MIDI 中",
                     "music_render": "渲染音频中",
                     "music_import": "导入渲染中",
                     "download": "下载中",
                     "http_get": "抓取中"}.get(tool)
            if _hint:
                say("  （%s…）" % _hint)
            # 音乐任务里模型若选「逐小节手写 / 渲染旧文件」那套，工具层直接按正确方式
            # 生成整首。3B 改不过来：拦它只会死循环（实测连拦两次仍然继续调 music_render），
            # 清理旧文件也没用。它的意图就是"要一个音乐文件"，这里用 midi 满足它。
            if (_looks_like_music(task)
                    and tool in ("music_start", "music_bar", "music_notes",
                                 "music_render", "music_import")):
                _orig = tool
                _secs = _task_seconds(task) or obj.get("seconds")
                _st = _style_from_task(task) or (
                    obj.get("style") if _task_has_style(task) else "random")
                _st = str(_st or "random").lower()
                if _st not in STYLE_TEMPO:
                    _st = "random"
                say("  （模型选了 %s，改成直接生成整首曲子）" % _orig)
                result = run_midi("bgm.mid", _st, STYLE_TEMPO.get(_st, 90),
                                  32, None, _secs)
                tool = "midi"
                say("  [midi] %s\n" % clip(result, 900))
                # 生成完直接收尾：3B 会一直重试 music_render/import，
                # 继续循环只会反复覆盖同一个文件，最后还到不了 final。
                answer = "已生成音乐：%s" % result
                n_tools += 1
                break
            elif (_looks_like_math(task) and _math_kind(task) == "equation"
                    and tool in ("calc", "write", "math")
                    # 已经 solve 过了 → 允许 calc 算最终量（全长 = 每天 × 天数）。
                    # 否则"720×15"这种乘法它心算会写成 10920（实测），
                    # 而之前一律拦 calc 导致它算不出结果、直接卡到步数上限。
                    and not any("solve" in str(m.get("content") or "")[:60]
                                for m in messages if m.get("role") == "assistant")):
                result = ("错误：这是能列方程的应用题，必须用 "
                          '{"tool":"solve","equations":["<方程1>","<方程2>",...]} '
                          "（sympy 精确求解并代回验证）。不要用 calc / write / math。")
            elif args.readonly and tool in ("write", "pptx"):
                result = ("错误：当前是只读模式（--readonly），禁止写文件和生成 pptx。"
                          "只做查询，并在 final 里说明需要写权限。")
            elif tool == "shell":
                result = run_shell(obj.get("cmd"))
            elif tool == "math":
                # 能列方程的应用题拦到 solve（math 要几百 token 且会代错方程）；
                # 但多步计算题（compute）必须放行 —— 实测它走 math 能算对 4.8，
                # 走 solve 反而列成勾股定理。
                if _looks_like_math(task) and _math_kind(task) == "equation":
                    result = ("错误：这道题能列方程，请改用 "
                              '{"tool":"solve","equations":["<方程1>","<方程2>",...]}。'
                              "solve 是 sympy 精确求解，几秒出结果还会代回验证。")
                else:
                    result = run_math(obj.get("problem") or obj.get("content") or "")
            elif tool == "solve":
                result = run_solve(obj.get("equations"), obj.get("variables"))
            elif tool in ("table", "table_top", "table_group"):
                # 统一入口：sort_by → 排序；group_by → 分组（sum_by 填了就求和）。
                # table_top / table_group 作为旧名兼容（模型有习惯，prompt 里还写着）。
                _path = obj.get("path", "")
                _sort = obj.get("sort_by") or obj.get("column")
                _group = obj.get("group_by") or obj.get("group_col")
                _sum = obj.get("sum_by") or obj.get("agg_col")
                _lim = obj.get("limit") or obj.get("top") or 10
                _content = str(obj.get("content") or "")
                if _content:
                    if not _sort and not _group:
                        _m = re.search(r"(?:sort_by|column)\s*[:=]\s*['\"]?([^'\",}\n]+)",
                                       _content)
                        if _m:
                            _sort = _m.group(1).strip()
                    _m2 = re.search(r"limit\s*[:=]\s*(\d+)", _content)
                    if _m2:
                        try:
                            _lim = int(_m2.group(1))
                        except ValueError:
                            pass
                result = run_table(_path, _sort, _group, _sum, _lim,
                                   obj.get("desc", True))
            elif tool == "sysinfo":
                result = run_sysinfo()
            elif tool == "download":
                result = run_download(obj.get("url") or "", obj.get("path"),
                                      obj.get("max_mb", 20))
            elif tool == "grep":
                result = run_grep(obj.get("pattern"), obj.get("path", "."),
                                  obj.get("max_hits", 30))
            elif tool == "http_get":
                result = run_http_get(obj.get("url") or obj.get("path") or "",
                                      obj.get("max_chars", 1500))
            elif tool == "list":
                result = run_list(obj.get("path", "."))
            elif tool == "du":
                result = run_du(obj.get("path", "."))
            elif tool == "read":
                result = run_read(obj.get("path", ""))
                if _looks_like_doc_task(task) and "uploads/" in str(obj.get("path", "")):
                    doc_read = True
            elif tool == "write":
                result = run_write(obj.get("path", ""), obj.get("content", ""))
            elif tool == "ask":
                result = run_ask(obj.get("question", ""), obj.get("options"))
            elif tool == "calc":
                result = run_calc(obj.get("expr", "") or obj.get("expression", ""))
            elif tool == "midi":
                # 风格优先从任务原文提取（模型传的不可信），提取不到才随机
                st = _style_from_task(task) or (
                    obj.get("style") if _task_has_style(task) else "random")
                # 用户点名了时长（"30 秒"）→ 交给工具换算小节数，比让模型瞎填准
                secs = _task_seconds(task) or obj.get("seconds")
                result = run_midi(obj.get("path", ""), st,
                                  obj.get("tempo", 90), obj.get("bars", 8),
                                  obj.get("tracks"), secs)
            elif tool == "music_start":
                result = run_music_start(obj.get("path", ""), obj.get("tempo", 160))
            elif tool == "music_bar":
                result = run_music_bar(obj.get("chord", "Am"), obj.get("notes"))
            elif tool == "music_notes":
                result = run_music_notes(obj.get("notes"))
            elif tool == "music_render":
                result = run_music_render(obj.get("path"))
            elif tool == "music_import":
                result = run_music_import(obj.get("path", ""))
            elif tool == "pptx":
                result = run_pptx(obj.get("path", ""), obj.get("slides"),
                                  obj.get("title"), obj.get("sections"))
                # 页数校验：任务说"N页"就核对，差多少直接说清楚，
                # 否则小模型只会重复同一个调用而不会补页
                want = re.search(r"([0-9一二三四五六七八九十]+)\s*页", task)
                got = len(obj.get("slides") or [])
                if want:
                    num = want.group(1)
                    cn = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                          "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
                    want_n = int(num) if num.isdigit() else cn.get(num, 0)
                    if want_n and got < want_n:
                        result += ("\n注意：任务要求 %d 页，这次只生成了 %d 页。"
                                   "请重新调用 pptx，在 slides 数组里补到 %d 页"
                                   "（不要重复原来的内容），然后再 final。"
                                   % (want_n, got, want_n))
                # 要点数量校验：任务说"三个要点"就核对 bullets 总数。
                # 实测 3B 会把"三个要点"只写一个，页数够了内容却空。
                want_pts = re.search(
                    r"([0-9一二三四五六七八九十]+)\s*个?\s*(?:要点|条|项|小点)", task)
                if want_pts:
                    num = want_pts.group(1)
                    cn = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                          "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
                    want_p = int(num) if num.isdigit() else cn.get(num, 0)
                    got_p = sum(len(s.get("bullets") or [])
                                for s in (obj.get("slides") or [])
                                if str(s.get("type")) == "bullets")
                    if want_p and got_p < want_p:
                        result += ("\n注意：任务要求 %d 个要点，这次只写了 %d 个。"
                                   "请重新调用 pptx，把 bullets 数组补到 %d 条"
                                   "（每条一句话，不要重复），然后再 final。"
                                   % (want_p, got_p, want_p))
                # pptx 自带 answer 且页数已满足 → 执行完就收尾
                if mixed_answer and "注意：" not in result:
                    answer = mixed_answer
                    break
            else:
                result = ("错误：未知工具 %r，可用 list/du/read/write/pptx/shell/final"
                          % tool)

            # shell 跑挂了（Traceback / 语法错）→ 明确告诉模型这个结果不能用。
            # 实测它会拿 NameError 的输出直接 final 一个错答案（"平均速度 2m/s"）。
            if tool == "shell" and ("Traceback" in result or "[stderr]" in result):
                result += ("\n（上面是执行失败，这个结果不能用。"
                           "请修正脚本后重新 write + shell 运行，不要拿失败的结果作答。）")
            # 内容型工具（math 的推理、read 的正文、grep/http_get 的命中）
            # 是给人看的，别按喂模型那个 160 字截断，否则推理过程直接被砍掉。
            if tool in ("math", "read", "grep", "http_get", "sysinfo", "table_top"):
                shown = clip(result, 900)
            else:
                shown = clip(result, 160).replace("\n", " | ")
            say("  [%s] %s\n" % (tool, shown))
            n_tools += 1
            # 生成类工具跑完清理工作目录，防止 wav/pptx 堆满 tmpfs
            if tool in ("write", "pptx", "midi", "music_render"):
                _prune_workdir()
            # 瞬时失败（超时、临时 IO）重试一次，永久错误（命令不允许、路径不存在）不重试
            if (isinstance(result, str) and result.startswith("错误")
                    and ("超时" in result or "timed out" in result.lower())):
                say("  ↻ %s 超时，重试一次\n" % tool)
                if tool == "shell":
                    result = run_shell(obj.get("cmd"))
                elif tool == "list":
                    result = run_list(obj.get("path", "."))
                elif tool == "du":
                    result = run_du(obj.get("path", "."))
                elif tool == "read":
                    result = run_read(obj.get("path", ""))
            outcomes.append("工具 %s 结果：\n%s" % (tool, clip(result)))

        if answer is not None:
            # 防幻觉：3B 会声称"已生成 1.md 和 2.md"而其实一个都没写。
            # final 里提到的文本文件名必须真的存在，否则打回去重做。
            claimed = re.findall(
                r"[^\s，。；：、\"'（）()【】\[\]]+\.(?:md|txt|html|htm|sh|csv)\b",
                answer)
            missing = [f for f in claimed if not os.path.exists(f)]
            if missing:
                if step < _limit:
                    say("  ! final 声称写了 %s，但这些文件不存在，打回重做\n"
                        % ", ".join(missing[:3]))
                    messages.append({"role": "assistant", "content": reply})
                    messages.append({"role": "user", "content":
                                     "你声称生成了这些文件，但它们不存在：%s。\n"
                                     "请真的调用 write 工具写出来（content 要放进 JSON 的"
                                     "字符串里，换行写成 \\n），然后再给 final。"
                                     % ", ".join(missing[:3])})
                    continue
                # 步数用完还是假的 → 明确失败，不要假装成功
                say("\n⚠ 任务未完成：模型声称写过的文件不存在 —— %s"
                    % ", ".join(missing[:3]))
                say("[统计] %d 步 / %d 次工具调用 / 总耗时 %.1f 秒（未完成）"
                    % (step, n_tools, time.time() - t_start))
                return 3
            say("\n最终回答: %s" % answer)
            say("[统计] %d 步 / %d 次工具调用 / 总耗时 %.1f 秒"
                % (step, n_tools, time.time() - t_start))
            # 记住这次任务，供下次「继续 / 再来一个」使用
            try:
                with open(LAST_TASK, "w") as fh:
                    fh.write(task)
            except Exception:                              # noqa: BLE001
                pass
            return 0

        messages.append({"role": "assistant", "content": reply})
        messages.append({"role": "user",
                         "content": "\n\n".join(outcomes) or "（无结果）"})

        # 上下文别爆：3B 的 ctx 通常只有 4096。把中间的往返压成摘要而不是直接砍掉，
        # 否则模型会忘记自己已经做过什么，又从头开始
        if len(messages) > 12:
            mid = messages[2:-8]
            notes = []
            for m in mid:
                c = (m.get("content") or "").replace("\n", " ").strip()
                if m.get("role") == "assistant" and c.startswith("{"):
                    notes.append("调用 " + c[:70])
                elif m.get("role") == "user" and c.startswith("工具"):
                    notes.append("结果 " + c[:80])
            digest = "[之前的操作（细节已省略）]\n" + "\n".join(notes[-6:])
            messages = (messages[:2]
                        + [{"role": "user", "content": digest}]
                        + messages[-8:])

    say("\n⚠ 达到 %d 步上限，未完成。最后一条工具结果在上面。" % _limit)
    say("[统计] %d 步 / %d 次工具调用 / 总耗时 %.1f 秒（未完成）"
        % (_limit, n_tools, time.time() - t_start))
    return 2


if __name__ == "__main__":
    sys.exit(main())
