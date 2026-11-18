/* doc.c — 文档解析插件：PDF / Word / Excel / PPT / 文本 → 纯文本
 *
 * 为什么独立成插件：这一路完全依赖外部运行时（python3 + pypdf +
 * python-docx + openpyxl + python-pptx）。环境里缺哪个库，这个插件就干不了
 * 对应的活 —— 放在框架里会让整个 agent 显得"上传坏了"，拆出来就能明确报
 * "缺 pypdf，PDF 转不了"，环境面板也能按库点亮。
 *
 * 入参：{"path":"/tmp/llama_agent/uploads/report.pdf"}
 * 输出：{"text":"...","chars":1234} 或 {"error":"缺少 pypdf，PDF 无法解析"}
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include "plugin.h"

#define OUTMAX 200000

static int jget(const char *js, const char *key, char *out, size_t cap) {
    char pat[96];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(js, pat);
    if (!p) return -1;
    p = strchr(p + strlen(pat), ':');
    if (!p) return -1;
    p++;
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') p++;
    if (*p != '"') return -1;
    p++;
    size_t i = 0;
    while (*p && *p != '"' && i + 1 < cap) {
        if (*p == '\\' && p[1]) { p++; out[i++] = *p++; }
        else out[i++] = *p++;
    }
    out[i] = 0;
    return 0;
}

/* 跑一条命令，收集 stdout */
static char *sh(const char *cmd) {
    FILE *p = popen(cmd, "r");
    if (!p) return NULL;
    char *buf = malloc(OUTMAX);
    if (!buf) { pclose(p); return NULL; }
    size_t n = fread(buf, 1, OUTMAX - 1, p);
    buf[n] = 0;
    pclose(p);
    return buf;
}

/* 该后缀需要哪个 python 库 */
static const char *need_lib(const char *path) {
    const char *dot = strrchr(path, '.');
    if (!dot) return NULL;
    if (!strcasecmp(dot, ".pdf")) return "pypdf";
    if (!strcasecmp(dot, ".docx")) return "docx";
    if (!strcasecmp(dot, ".xlsx") || !strcasecmp(dot, ".xls")) return "openpyxl";
    if (!strcasecmp(dot, ".pptx")) return "pptx";
    return NULL;                      /* .txt/.md/.csv 不需要库 */
}

static char *json_escape(const char *in, size_t *outlen) {
    size_t cap = strlen(in) * 6 + 64;
    char *o = malloc(cap);
    if (!o) return NULL;
    size_t i = 0;
    for (const char *p = in; *p; p++) {
        unsigned char c = (unsigned char)*p;
        if (c == '"' || c == '\\') { o[i++] = '\\'; o[i++] = (char)c; }
        else if (c == '\n') { o[i++] = '\\'; o[i++] = 'n'; }
        else if (c == '\r') { o[i++] = '\\'; o[i++] = 'r'; }
        else if (c == '\t') { o[i++] = '\\'; o[i++] = 't'; }
        else if (c < 0x20) { i += (size_t)snprintf(o + i, cap - i, "\\u%04x", c); }
        else o[i++] = (char)c;
    }
    o[i] = 0;
    *outlen = i;
    return o;
}

static char *run(const char *json_in) {
    char path[512] = {0};
    jget(json_in, "path", path, sizeof(path));
    if (!path[0]) return strdup("{\"error\":\"缺少 path\"}");

    /* 1. 依赖自检：缺库就直说，别给个空文本让模型瞎猜 */
    const char *lib = need_lib(path);
    if (lib) {
        char chk[256];
        snprintf(chk, sizeof(chk), "python3 -c 'import %s' 2>/dev/null", lib);
        if (system(chk) != 0) {
            char *o = malloc(320);
            if (!o) return NULL;
            snprintf(o, 320,
                "{\"error\":\"缺少 Python 库 %s，无法解析 %s。"
                "板上装：pip install %s\"}", lib, path, lib);
            return o;
        }
    }

    /* 2. 交给 extract.py（python 侧已经处理了 encodings/表格/去重这些细节） */
    char cmd[1024];
    snprintf(cmd, sizeof(cmd),
        "python3 /opt/llama/agent/extract.py '%s' 2>&1", path);
    char *txt = sh(cmd);
    if (!txt) return strdup("{\"error\":\"无法执行 python3\"}");
    if (!txt[0]) { free(txt); return strdup("{\"error\":\"extract.py 没有输出\"}"); }

    /* 明显是 python 报错（Traceback / ModuleNotFound）→ 原样返回错误 */
    if (strstr(txt, "Traceback") || strstr(txt, "ModuleNotFoundError")
        || strstr(txt, "No such file")) {
        size_t n = 0;
        char *esc = json_escape(txt, &n);
        free(txt);
        if (!esc) return NULL;
        char *o = malloc(n + 64);
        if (!o) { free(esc); return NULL; }
        snprintf(o, n + 64, "{\"error\":\"%s\"}", esc);
        free(esc);
        return o;
    }

    /* 3. 正常：包成 JSON 返回 */
    size_t n = 0;
    char *esc = json_escape(txt, &n);
    size_t chars = strlen(txt);
    free(txt);
    if (!esc) return NULL;
    char *o = malloc(n + 96);
    if (!o) { free(esc); return NULL; }
    snprintf(o, n + 96, "{\"text\":\"%s\",\"chars\":%zu}", esc, chars);
    free(esc);
    return o;
}

static plugin_desc_t desc = {
    PLUGIN_API_VERSION,
    "doc",
    "文档解析：PDF/Word/Excel/PPT/文本 → 纯文本（依赖 python3 + 对应库，缺库会明确报错）",
    "{\"path\":\"/tmp/llama_agent/uploads/report.pdf\"}",
    run
};

plugin_desc_t *plugin_entry(void) { return &desc; }
