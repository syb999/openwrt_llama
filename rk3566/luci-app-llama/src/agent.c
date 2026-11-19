#define _GNU_SOURCE
/* agent.c — C 版 agent 框架
 *
 * 和 Python 版做同样的事：任务丢给 llama-server，模型输出 {"tool":...}，
 * 框架找到同名插件执行、结果回灌，直到模型给 final。
 *
 * 结构差异：每个功能都是独立 .so 插件，框架不认识任何具体功能。
 * 新增能力 = 新增一个 .so，框架零改动。
 *
 * 无第三方依赖：自带最小 JSON 取值 + 裸 socket HTTP。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <errno.h>
#include <unistd.h>
#include <dirent.h>
#include <dlfcn.h>
#include <time.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <sys/time.h>
#include "plugin.h"
#include "json.h"

#define MAX_PLUGINS 32
#define WORK_DIR "/tmp/llama_agent"
#define BIG 262144
#define SMALL 8192

static plugin_desc_t *g_plug[MAX_PLUGINS];
static int g_n = 0;
/* 插件启停：名单落在 /etc/llama.plugoff，加载时跳过被停用的插件。
 * 放在文件最前面是因为 env_json 也要读（它比 load_plugins 早）。 */
static char g_plugoff[512] = {0};
static char g_disabled[32][32];
static int  g_ndis = 0;

/* 自动化实例的运行状态落盘在这：/tmp/agent-auto/<name>.pid
 * 页面靠它判断"在跑 / 没跑"，停止也靠它找 pid。
 * 放 /tmp 是有意的：tmpfs，重启自动清空，不会留下脏 pid 文件。 */
#define AUTO_DIR "/tmp/agent-auto"

static int auto_pid_of(const char *name, pid_t *out) {
    char pf[256];
    snprintf(pf, sizeof(pf), AUTO_DIR "/%s.pid", name);
    FILE *f = fopen(pf, "r");
    if (!f) return 0;
    int pid = 0;
    int ok = (fscanf(f, "%d", &pid) == 1);
    fclose(f);
    if (!ok || pid <= 0) return 0;
    if (kill((pid_t)pid, 0) != 0) return 0;      /* 进程没了，脏 pid */
    if (out) *out = (pid_t)pid;
    return 1;
}

static int auto_state(const char *name, pid_t *pid) {
    return auto_pid_of(name, pid);
}

static void auto_pid_write(const char *name) {
    mkdir(AUTO_DIR, 0755);
    char pf[256];
    snprintf(pf, sizeof(pf), AUTO_DIR "/%s.pid", name);
    FILE *f = fopen(pf, "w");
    if (f) { fprintf(f, "%d\n", (int)getpid()); fclose(f); }
}

static void auto_pid_clear(const char *name) {
    char pf[256];
    snprintf(pf, sizeof(pf), AUTO_DIR "/%s.pid", name);
    unlink(pf);
}

/* 自动化插件（第二类，导出 plugin_auto_entry 的那些） */
#define MAX_AUTO 16
static plugin_auto_t *g_auto[MAX_AUTO];
static int g_nauto = 0;

static plugin_desc_t *find_plugin(const char *name);

/* --memcheck 模式下每步打印分配记账（memcheck.c，链接期 --wrap 实现） */
void memcheck_report(const char *where);
static int g_memcheck = 0;
#define MEMCHK(where) do { if (g_memcheck) memcheck_report(where); } while (0)

/* ============ JSON 取值 ============ */
/* llama-server 响应的 content：走正规解析 choices[0].message.content，
 * 取不到再退回顶层 content。不再用 strstr 找 key（字符串里出现
 * "content" 字样会取错位置）。 */
static char *extract_content(const char *resp) {
    char err[128];
    jval *v = json_parse(resp, err, sizeof(err));
    if (!v) return NULL;
    const char *c = NULL;
    jval *msg = json_get(json_at(json_get(v, "choices"), 0), "message");
    c = json_str(json_get(msg, "content"));
    if (!c) c = json_str(json_get(v, "content"));
    char *out = c ? strdup(c) : NULL;
    json_free(v);
    return out;
}

/* 旧的 strstr 版取值，留着给插件参数这种扁平结构用（新代码别用） */
__attribute__((unused))
static const char *skip_ws(const char *p) {
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') p++;
    return p;
}

static int flat_get(const char *js, const char *key, char *out, size_t cap) {
    char pat[128];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    /* 只认「后面紧跟冒号」的匹配：字段值恰好等于某个 key 名时会误命中
     * （{"op":"query",...,"query":"x"} 找 query 会先撞上 op 的值），
     * 那样解析出来的就是隔壁字段。 */
    const char *p = js, *found = NULL;
    while ((p = strstr(p, pat)) != NULL) {
        const char *q = p + strlen(pat);
        while (*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') q++;
        if (*q == ':') { found = q; break; }
        p = q;
    }
    if (!found) return -1;
    p = found;
    p = skip_ws(p + 1);
    if (*p != '"') return -1;
    p++;
    size_t i = 0;
    while (*p && *p != '"' && i + 1 < cap) {
        if (*p == '\\' && p[1]) {
            p++;
            switch (*p) {
            case 'n': out[i++] = '\n'; break;
            case 't': out[i++] = '\t'; break;
            case 'r': out[i++] = '\r'; break;
            case 'b': out[i++] = '\b'; break;
            case 'f': out[i++] = '\f'; break;
            case '"': out[i++] = '"'; break;
            case '\\': out[i++] = '\\'; break;
            case '/': out[i++] = '/'; break;
            case 'u':
                if (p[1] && p[2] && p[3] && p[4]) {
                    char hex[5] = { p[1], p[2], p[3], p[4], 0 };
                    unsigned cp = (unsigned)strtoul(hex, NULL, 16);
                    if (cp < 0x80) {
                        out[i++] = (char)cp;
                    } else if (cp < 0x800) {
                        out[i++] = (char)(0xC0 | (cp >> 6));
                        out[i++] = (char)(0x80 | (cp & 0x3F));
                    } else {
                        out[i++] = (char)(0xE0 | (cp >> 12));
                        out[i++] = (char)(0x80 | ((cp >> 6) & 0x3F));
                        out[i++] = (char)(0x80 | (cp & 0x3F));
                    }
                    p += 4;
                }
                break;
            default: out[i++] = *p; break;
            }
            p++;
        } else {
            out[i++] = *p++;
        }
    }
    out[i] = 0;
    return 0;
}

static void json_escape(const char *in, char *out, size_t cap) {
    size_t i = 0;
    for (const char *p = in; *p && i + 8 < cap; p++) {
        switch (*p) {
        case '"':  i += (size_t)snprintf(out + i, cap - i, "\\\""); break;
        case '\\': i += (size_t)snprintf(out + i, cap - i, "\\\\"); break;
        case '\n': i += (size_t)snprintf(out + i, cap - i, "\\n"); break;
        case '\r': i += (size_t)snprintf(out + i, cap - i, "\\r"); break;
        case '\t': i += (size_t)snprintf(out + i, cap - i, "\\t"); break;
        default:
            if ((unsigned char)*p < 0x20) {
                i += (size_t)snprintf(out + i, cap - i, "\\u%04x", *p);
            } else {
                out[i++] = *p;
            }
        }
    }
    out[i] = 0;
}

/* ============ HTTP ============ */
static char *http_post(const char *host, int port, const char *path,
                       const char *body, int timeout_s) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return NULL;
    struct timeval tv;
    tv.tv_sec = timeout_s;
    tv.tv_usec = 0;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_port = htons((unsigned short)port);
    sa.sin_addr.s_addr = inet_addr(host);
    if (connect(fd, (struct sockaddr *)&sa, sizeof(sa)) < 0) {
        if (getenv("AGENT_DEBUG")) fprintf(stderr, "  [http] connect 失败: %s\n", strerror(errno));
        close(fd);
        return NULL;
    }
    if (getenv("AGENT_DEBUG")) fprintf(stderr, "  [http] 已连接 %s:%d body=%zu 字节\n", host, port, strlen(body));
    size_t blen = strlen(body);
    size_t rlen = blen + 512;
    char *req = malloc(rlen);
    if (!req) { close(fd); return NULL; }
    int hlen = snprintf(req, rlen,
        "POST %s HTTP/1.1\r\nHost: %s:%d\r\nContent-Type: application/json\r\n"
        "Content-Length: %zu\r\nConnection: close\r\n\r\n",
        path, host, port, blen);
    int ok = (send(fd, req, (size_t)hlen, 0) == hlen)
          && (send(fd, body, blen, 0) == (ssize_t)blen);
    free(req);
    if (!ok) {
        if (getenv("AGENT_DEBUG")) fprintf(stderr, "  [http] send 失败: %s (hlen=%d)\n", strerror(errno), hlen);
        close(fd);
        return NULL;
    }

    char *resp = malloc(BIG);
    if (!resp) { close(fd); return NULL; }
    size_t got = 0;
    ssize_t k;
    while ((k = recv(fd, resp + got, BIG - 1 - got, 0)) > 0) {
        got += (size_t)k;
        if (got >= BIG - 1) break;
    }
    resp[got] = 0;
    close(fd);

    char *bodyp = strstr(resp, "\r\n\r\n");
    if (!bodyp) {
        /* 没有头部结束标记 → 不是合法 HTTP 响应 */
        free(resp);
        return NULL;
    }
    /* 状态码检查：4xx/5xx 直接把响应体当错误返回，让上层报出来 */
    if (strncmp(resp, "HTTP/1.", 7) == 0) {
        int code = atoi(resp + 9);
        if (code >= 400) {
            char *out = strdup(bodyp + 4);
            free(resp);
            return out;
        }
    }
    /* chunked 传输：把各个 chunk 拼起来 */
    char hdr_end = 0;
    (void)hdr_end;
    char *te = NULL;
    {
        /* 只在头部范围内搜，避免误命中 body */
        char save = *bodyp;
        *bodyp = 0;
        te = strcasestr(resp, "transfer-encoding: chunked");
        *bodyp = save;
    }
    if (te) {
        char *src = bodyp + 4;
        size_t cap = strlen(src) + 1;
        char *out = malloc(cap);
        if (!out) { free(resp); return NULL; }
        size_t o = 0;
        for (;;) {
            unsigned long sz = strtoul(src, &src, 16);
            if (sz == 0) break;
            while (*src == '\r' || *src == '\n') src++;
            if (o + sz >= cap) break;
            memcpy(out + o, src, sz);
            o += sz;
            src += sz;
            /* 数据后面还有 CRLF，不跳过的话下一次 strtoul 读到 '\r'
             * 直接返回 0，多 chunk 的响应只会解出第一块 */
            while (*src == '\r' || *src == '\n') src++;
        }
        out[o] = 0;
        free(resp);
        return out;
    }

    char *out = strdup(bodyp + 4);
    free(resp);
    return out;
}

/* 按 UTF-8 字符边界截断：printf("%.400s") 是按字节截的，会切出半个汉字，
 * 页面上显示成 "�"。这里退到最后一个完整字符的边界。 */
static void utf8_cut(char *s, size_t max_bytes) {
    size_t n = strlen(s);
    if (n <= max_bytes) return;
    size_t i = max_bytes;
    /* 往回退到非续字节（0b10xxxxxx）的位置 */
    while (i > 0 && ((unsigned char)s[i] & 0xC0) == 0x80) i--;
    s[i] = 0;
}


/* SSE 解析用的两个小工具（curl.c 里也有一份，插件各自独立不共享符号） */
static int hexval(int c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}
static int utf8_emit(char *out, unsigned cp) {
    if (cp < 0x80) { out[0] = (char)cp; return 1; }
    if (cp < 0x800) {
        out[0] = (char)(0xC0 | (cp >> 6));
        out[1] = (char)(0x80 | (cp & 0x3F));
        return 2;
    }
    if (cp < 0x10000) {
        out[0] = (char)(0xE0 | (cp >> 12));
        out[1] = (char)(0x80 | ((cp >> 6) & 0x3F));
        out[2] = (char)(0x80 | (cp & 0x3F));
        return 3;
    }
    out[0] = (char)(0xF0 | (cp >> 18));
    out[1] = (char)(0x80 | ((cp >> 12) & 0x3F));
    out[2] = (char)(0x80 | ((cp >> 6) & 0x3F));
    out[3] = (char)(0x80 | (cp & 0x3F));
    return 4;
}

/* ============ 流式请求 ============
 * 3B 在 RK3566 上生成 300 token 要三分钟。一次性返回的话用户盯着空屏幕
 * 等三分钟，以为卡死了。这里边收边打印：每收到一段就把增量写到 stdout
 * （= job 日志），前端轮询就能看到字一个个冒出来。
 *
 * 只处理 llama-server 的 SSE 格式：
 *   data: {"choices":[{"delta":{"content":"字"}}]}
 *   data: [DONE]
 * 返回累积的完整文本（malloc），失败 NULL。
 */
static char *http_post_sse(const char *host, int port, const char *path,
                           const char *body, int timeout_s,
                           int emit_stdout) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return NULL;
    struct timeval tv;
    /* 单次 read 最多等 90 秒。原来是 900 秒 —— 一旦服务端不关连接，
     * 用户要干等 15 分钟（实测卡死过一次，后面的任务全被挡）。 */
    tv.tv_sec = timeout_s > 90 ? 90 : timeout_s;
    tv.tv_usec = 0;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_port = htons((unsigned short)port);
    sa.sin_addr.s_addr = inet_addr(host);
    if (connect(fd, (struct sockaddr *)&sa, sizeof(sa)) < 0) { close(fd); return NULL; }

    size_t blen = strlen(body);
    size_t rlen = blen + 512;
    char *req = malloc(rlen);
    if (!req) { close(fd); return NULL; }
    snprintf(req, rlen,
        /* 必须 HTTP/1.1：流式响应靠 chunked 编码分隔，1.0 没有 chunked，
         * llama-server 会卡在"怎么结束响应"上（实测占满 4 核不出结果）。
         * chunked 的解码在下面的 read 循环里做。 */
        "POST %s HTTP/1.1\r\nHost: %s:%d\r\nConnection: close\r\n"
        "Content-Type: application/json\r\nContent-Length: %zu\r\n\r\n%s",
        path, host, port, blen, body);
    size_t sent = 0, rl = strlen(req);
    while (sent < rl) {
        ssize_t w = write(fd, req + sent, rl - sent);
        if (w <= 0) { free(req); close(fd); return NULL; }
        sent += (size_t)w;
    }
    free(req);

    char *acc = malloc(BIG);
    if (!acc) { close(fd); return NULL; }
    size_t alen = 0;
    size_t shown_off = 0;        /* 已经打出去的 answer 字符数（每次调用从 0 开始） */
    char headbuf[8192];
    size_t hn = 0;
    int in_body = 0, done = 0;
    int chunked = 0;
    char line[8192];
    /* chunked 解码状态：0=读长度行 1=读数据 2=吃掉数据后的 CRLF */
    int cstate = 0;
    long cremain = 0;
    char clen[32];
    int cln = 0;

    while (!done) {
        char buf[4096];
        ssize_t r = read(fd, buf, sizeof(buf) - 1);
        if (r <= 0) {
            /* 超时或对端不关连接：已经收到的内容照样返回，
             * 别让模型白生成一遍（用户至少能看到已有的部分） */
            if (alen > 0) fprintf(stderr, "（流式读取中断，已收到 %zu 字节）\n", alen);
            break;
        }
        buf[r] = 0;

        if (!in_body) {
            /* 先吃掉 HTTP 头 */
            size_t need = sizeof(headbuf) - hn - 1;
            size_t cp = (size_t)r < need ? (size_t)r : need;
            memcpy(headbuf + hn, buf, cp);
            hn += cp;
            headbuf[hn] = 0;
            char *sep = strstr(headbuf, "\r\n\r\n");
            if (!sep) continue;
            in_body = 1;
            chunked = (strstr(headbuf, "chunked") != NULL);
            /* 头后面可能已经带了正文，把它接着处理（以前直接 continue 丢掉，
             * 小响应会整段丢失） */
            size_t bodyoff = (size_t)(sep + 4 - headbuf);
            if (bodyoff < hn) {
                memmove(buf, headbuf + bodyoff, hn - bodyoff);
                buf[hn - bodyoff] = 0;
            } else {
                continue;
            }
        }

        /* chunked 解码：<hex长度>\r\n<数据>\r\n...0\r\n\r\n */
        char plain[8192];
        const char *p;
        if (chunked) {
            size_t pn = 0;
            for (char *q = buf; *q && pn < sizeof(plain) - 1; q++) {
                if (cstate == 0) {
                    if (*q == '\n') {
                        clen[cln] = 0;
                        cremain = strtol(clen, NULL, 16);
                        cln = 0;
                        cstate = (cremain > 0) ? 1 : 3;   /* 0 = 最后一个 chunk */
                    } else if (*q != '\r' && cln < (int)sizeof(clen) - 1) {
                        clen[cln++] = *q;
                    }
                } else if (cstate == 1) {
                    plain[pn++] = *q;
                    if (--cremain <= 0) cstate = 2;
                } else if (cstate == 2) {
                    if (*q == '\n') cstate = 0;          /* 吃掉数据后的 CRLF */
                } else {                                   /* cstate == 3: 结束 */
                    break;
                }
            }
            plain[pn] = 0;
            p = plain;
        } else {
            p = buf;
        }

        /* 按行切 data: */
        while (*p) {
            const char *nl = strchr(p, '\n');
            size_t ll = nl ? (size_t)(nl - p) : strlen(p);
            if (ll > sizeof(line) - 1) ll = sizeof(line) - 1;
            memcpy(line, p, ll);
            line[ll] = 0;
            p = nl ? nl + 1 : p + ll;

            char *d = line;
            while (*d == ' ' || *d == '\r') d++;
            if (strncmp(d, "data:", 5)) continue;
            d += 5;
            while (*d == ' ') d++;
            /* 行尾的 \r 必须去掉：SSE 是 \r\n 结尾，不去掉
             * strcmp("[DONE]") 永远失败 → 生成完了还在等 → 卡到超时 */
            {
                size_t dl = strlen(d);
                while (dl > 0 && (d[dl-1] == '\r' || d[dl-1] == '\n' || d[dl-1] == ' '))
                    d[--dl] = 0;
            }
            if (!strcmp(d, "[DONE]")) { done = 1; break; }

            /* 取 delta.content（流式响应里只有这一处 content） */
            char *c = strstr(d, "\"content\":\"");
            if (!c) continue;
            c += 11;
            while (*c && *c != '"' && alen + 8 < BIG) {
                if (*c == '\\' && c[1]) {
                    c++;
                    switch (*c) {
                    case 'n': acc[alen++] = '\n'; break;
                    case 'r': acc[alen++] = '\r'; break;
                    case 't': acc[alen++] = '\t'; break;
                    case 'u': {
                        unsigned cp2 = 0;
                        int ok2 = 1;
                        for (int k = 1; k <= 4; k++) {
                            int hv = hexval((unsigned char)c[k]);
                            if (hv < 0) { ok2 = 0; break; }
                            cp2 = cp2 * 16 + (unsigned)hv;
                        }
                        if (ok2) { c += 4; alen += (size_t)utf8_emit(acc + alen, cp2); }
                        else acc[alen++] = *c;
                        break;
                    }
                    default: acc[alen++] = *c; break;
                    }
                    c++;
                } else acc[alen++] = *c++;
            }
            acc[alen] = 0;
            if (emit_stdout && alen) {
                /* 只显示 answer 字段的内容，别把 {"tool":"final",...} 这种
                 * 协议 JSON 漏给用户 —— 他看到一屏花括号会以为出错了。 */
                const char *a = strstr(acc, "\"answer\":\"");
                if (a) {
                    size_t begin = (size_t)(a - acc) + 10;     /* 跳过 "answer":" */
                    if (alen > begin) {
                        /* 增量输出（JSON 转义在这里就地还原） */
                        const char *p2 = acc + begin + shown_off;
                        const char *end2 = acc + alen;
                        char obuf[4096];
                        size_t oi = 0;
                        while (p2 < end2 && oi < sizeof(obuf) - 4) {
                            if (*p2 == '\\') {
                                /* 转义序列还没到齐（\ 是这一块的最后一个字节）→
                                 * 不消费，等下一块拼上再处理，否则会打印出字面 \n */
                                if (p2 + 1 >= end2) break;
                                p2++;
                                switch (*p2) {
                                case 'n': obuf[oi++] = '\n'; break;
                                case 'r': obuf[oi++] = '\r'; break;
                                case 't': obuf[oi++] = '\t'; break;
                                case '"': obuf[oi++] = '"'; break;
                                case '\\': obuf[oi++] = '\\'; break;
                                default: obuf[oi++] = *p2; break;
                                }
                                p2++;
                            } else if (*p2 == '"') {
                                break;                          /* 答案结束 */
                            } else obuf[oi++] = *p2++;
                        }
                        if (oi) {
                            fwrite(obuf, 1, oi, stdout);
                            fflush(stdout);
                            shown_off = (size_t)(p2 - (acc + begin));
                        }
                    }
                }
            }
        }
    }
    close(fd);
    acc[alen] = 0;
    return acc;
}

/* ============ 环境检测 ============ */
#include <sys/stat.h>

/* PATH 里找可执行的 name */
static int have_cmd(const char *name) {
    const char *path = getenv("PATH");
    if (!path || !*path) path = "/usr/bin:/bin:/usr/sbin:/sbin";
    char buf[512];
    const char *p = path;
    while (*p) {
        const char *e = strchr(p, ':');
        size_t n = e ? (size_t)(e - p) : strlen(p);
        if (n < sizeof(buf) - 128) {
            snprintf(buf, sizeof(buf), "%.*s/%s", (int)n, p, name);
            struct stat st;
            if (stat(buf, &st) == 0 && S_ISREG(st.st_mode)
                && (st.st_mode & 0111)) {
                return 1;
            }
        }
        if (!e) break;
        p = e + 1;
    }
    return 0;
}

/* 输出环境能力 + 插件清单（页面顶部那个按钮点开要看的东西） */
/* 判断 python 模块在不在：直接看 site-packages 目录，不起 python 进程。
 * 原来是 system("python3 -c 'import xxx'")，6 个库 = 6 次 python3 启动；
 * RK3566 上一次几百毫秒满载，页面每刷新一次就烧掉几秒 CPU。
 * 装了的模块一定有目录/文件，查目录足够判断。 */
static int have_pymod(const char *mod) {
    static const char *dirs[] = {
        "/usr/lib/python3.13/site-packages", "/usr/lib/python3.12/site-packages",
        "/usr/lib/python3.11/site-packages", "/usr/lib/python3.10/site-packages",
        "/usr/lib/python3.9/site-packages",  "/usr/lib/python3.8/site-packages",
        "/usr/lib/python3/dist-packages",    "/usr/lib/python3/site-packages",
        "/usr/lib/python3.11/dist-packages", "/usr/lib/python3.10/dist-packages",
        "/usr/lib/python3.9/dist-packages",
        "/usr/local/lib/python3.11/dist-packages",
        "/usr/local/lib/python3.10/dist-packages",
        "/usr/local/lib/python3.9/dist-packages",
        "/usr/local/lib/python3/site-packages",
        NULL
    };
    char p[320];
    for (int i = 0; dirs[i]; i++) {
        snprintf(p, sizeof(p), "%s/%s", dirs[i], mod);
        if (access(p, F_OK) == 0) return 1;
        snprintf(p, sizeof(p), "%s/%s.py", dirs[i], mod);   /* 单文件模块 */
        if (access(p, F_OK) == 0) return 1;
    }
    return 0;
}

/* 环境结果缓存：--env 结果 60 秒内复用，避免反复刷新页面反复探库 */
#define ENV_CACHE "/tmp/.agent-env.cache"
#define ENV_TTL   60

static int env_from_cache(char *out, size_t cap) {
    struct stat st;
    if (stat(ENV_CACHE, &st) != 0) return 0;
    if (time(NULL) - st.st_mtime > ENV_TTL) return 0;
    FILE *f = fopen(ENV_CACHE, "r");
    if (!f) return 0;
    size_t n = fread(out, 1, cap - 1, f);
    fclose(f);
    out[n] = 0;
    return n > 1;
}

static void env_to_cache(const char *s) {
    FILE *f = fopen(ENV_CACHE, "w");
    if (!f) return;
    fputs(s, f);
    fclose(f);
}

static void env_json(char *out, size_t cap) {
    /* 缓存只存"探测结果位串"（python3/node/ffmpeg/sympy/midiutil/pypdf/docx/openpyxl/pptx）。
     * 不能连插件列表一起缓存——running/pid 是实时的，缓存会导致
     * 已经停掉的自动化在页面上还显示"运行中"。 */
    char bits[16] = {0};
    size_t bl = 0;
    if (env_from_cache(bits, sizeof(bits))) bl = strlen(bits);
    /* 校验格式：必须是 9 个 0/1。旧版本缓存里存的是 JSON，要认出来重算 */
    int bits_ok = (bl >= 9);
    for (size_t k = 0; bits_ok && k < 9; k++)
        if (bits[k] != '0' && bits[k] != '1') bits_ok = 0;
    if (!bits_ok) {
        int py0 = have_cmd("python3");
        snprintf(bits, sizeof(bits), "%d%d%d%d%d%d%d%d%d",
                 py0, have_cmd("node"), have_cmd("ffmpeg"),
                 py0 ? have_pymod("sympy") : 0,
                 py0 ? have_pymod("midiutil") : 0,
                 py0 ? have_pymod("pypdf") : 0,
                 py0 ? have_pymod("docx") : 0,
                 py0 ? have_pymod("openpyxl") : 0,
                 py0 ? have_pymod("pptx") : 0);
        env_to_cache(bits);
    }
    int py     = bits[0] == '1';
    int f_sympy = bits[3] == '1', f_midi = bits[4] == '1', f_pypdf = bits[5] == '1';
    int f_docx  = bits[6] == '1', f_xlsx = bits[7] == '1', f_pptx  = bits[8] == '1';
    /* jq / curl / ppocr 是外部命令：有没有直接看可执行文件在不在 */
    int f_jq    = access("/usr/bin/jq", X_OK) == 0;
    int f_curl  = access("/usr/bin/curl", X_OK) == 0;
    int f_ppocr = access("/usr/bin/ppocr", X_OK) == 0;

    /* 分两组：runtimes = 底层运行时（谁都得有），libs = 专用库（特定插件才用） */
    size_t off = (size_t)snprintf(out, cap,
        "{\"runtimes\":{\"python3\":%d,\"node\":%d,\"ffmpeg\":%d},"
        "\"libs\":{\"jq\":%d,\"curl\":%d,\"ppocr\":%d,\"sympy\":%d,\"midiutil\":%d,\"pypdf\":%d,\"docx\":%d,"
        "\"openpyxl\":%d,\"pptx\":%d},\"plugins\":[",
        /* 顺序必须跟格式串一致：runtimes 三个（python3/node/ffmpeg），
         * 然后 libs 九个（jq/curl/ppocr/sympy/midiutil/pypdf/docx/openpyxl/pptx） */
        py, bits[1] == '1', bits[2] == '1',
        f_jq, f_curl, f_ppocr,
        f_sympy, f_midi, f_pypdf, f_docx, f_xlsx, f_pptx);
    for (int i = 0; i < g_n && off < cap - 620; i++) {
        /* desc 必须转义：插件描述里出现 " 或 \ （比如 curl 的 "\uXXXX"）
         * 会把整个 env JSON 打断，前端 JSON.parse 失败 → 面板全空。 */
        char ne[256], de[4096];
        json_escape(g_plug[i]->name ? g_plug[i]->name : "", ne, sizeof(ne));
        json_escape(g_plug[i]->description ? g_plug[i]->description : "", de, sizeof(de));
        off += (size_t)snprintf(out + off, cap - off,
            "%s{\"name\":\"%s\",\"desc\":\"%s\",\"kind\":\"tool\"}",
            i ? "," : "", ne, de);
    }
    for (int i = 0; i < g_nauto && off < cap - 700; i++) {
        pid_t apid = 0;
        int running = auto_state(g_auto[i]->name, &apid);
        char ne[256], de[4096], te[256];
        json_escape(g_auto[i]->name ? g_auto[i]->name : "", ne, sizeof(ne));
        json_escape(g_auto[i]->description ? g_auto[i]->description : "", de, sizeof(de));
        json_escape(g_auto[i]->triggers ? g_auto[i]->triggers : "manual", te, sizeof(te));
        off += (size_t)snprintf(out + off, cap - off,
            "%s{\"name\":\"%s\",\"desc\":\"%s\",\"kind\":\"auto\",\"triggers\":\"%s\","
            "\"running\":%d,\"pid\":%d}",
            (g_n + i) ? "," : "", ne, de, te,
            running ? 1 : 0, running ? (int)apid : 0);
    }
    /* 被停用的插件也要报出来，否则页面上看不到它们、没法重新打开 */
    off += (size_t)snprintf(out + off, cap - off, "],\"disabled\":[");
    for (int i = 0; i < g_ndis && off < cap - 80; i++) {
        char de[64];
        json_escape(g_disabled[i], de, sizeof(de));
        off += (size_t)snprintf(out + off, cap - off, "%s\"%s\"", i ? "," : "", de);
    }
    snprintf(out + off, cap - off, "]}");
}

/* ============ 插件依赖 ============
 * 有些插件靠外部命令干活（curl 靠 /usr/bin/curl，jq 靠 /usr/bin/jq）。
 * 命令不在就直接不加载 —— 加载了也是每次调用报"找不到 jq"，
 * 不如让面板里干脆看不到这个插件，同时环境库那边显示缺什么。 */
typedef struct {
    const char *plugin;      /* 插件名 */
    const char *need;        /* 需要的可执行文件（绝对路径） */
    const char *label;       /* 面板里显示的名字 */
} plug_dep_t;

static const plug_dep_t PLUG_DEPS[] = {
    {"curl", "/usr/bin/curl", "curl"},
    {"jq",   "/usr/bin/jq",   "jq"},
    {"browser", "/opt/chrome-root/cdrv.sh", "chromedriver"},
    {NULL, NULL, NULL}
};

/* 这个插件缺不缺依赖；缺的话返回缺的东西，不缺返回 NULL */
static const char *plugin_missing_dep(const char *name) {
    for (int i = 0; PLUG_DEPS[i].plugin; i++) {
        if (strcmp(PLUG_DEPS[i].plugin, name)) continue;
        if (access(PLUG_DEPS[i].need, X_OK) != 0) return PLUG_DEPS[i].need;
        return NULL;
    }
    return NULL;
}

/* ============ 插件 ============ */
static plugin_desc_t *find_plugin(const char *name) {
    /* 先按插件名精确匹配 */
    for (int i = 0; i < g_n; i++) {
        if (strcmp(g_plug[i]->name, name) == 0) return g_plug[i];
    }
    /* 再按别名匹配：模型常把子功能名（calc/solve）当工具名写 */
    for (int i = 0; i < g_n; i++) {
        const char *a = g_plug[i]->aliases;
        if (!a || !*a) continue;
        const char *p = a;
        while (*p) {
            const char *e = strchr(p, ',');
            size_t L = e ? (size_t)(e - p) : strlen(p);
            if (L && strlen(name) == L && !strncmp(p, name, L)) return g_plug[i];
            /* 前缀匹配：tool=uci_show / iw_station 这种"别名_子操作"的写法，
             * 也归到这个插件，具体 op 由插件自己从 tool 名里认。 */
            if (L && strlen(name) > L && !strncmp(p, name, L) && name[L] == '_')
                return g_plug[i];
            if (!e) break;
            p = e + 1;
        }
    }
    return NULL;
}

/* 按名字排序：readdir 的顺序不保证，排序后工具清单每次一致，
 * 模型看到的选择顺序稳定，行为可复现。 */
static int cmp_plug(const void *a, const void *b) {
    const plugin_desc_t *x = *(const plugin_desc_t *const *)a;
    const plugin_desc_t *y = *(const plugin_desc_t *const *)b;
    return strcmp(x->name, y->name);
}

/* ============ 插件启停 ============
 * 插件一多，prompt 里的工具表就长，3B 选错率上升、每步都慢。
 * 允许在页面上关掉不用的插件：名单落在 /etc/llama.plugoff（一行一个名字），
 * 加载时直接不 dlopen，连内存都省了。
 * 被关掉的插件仍然会出现在环境面板里（标 disabled），方便重新打开。 */

static void read_plugoff(void) {
    FILE *f = fopen("/etc/llama.plugoff", "r");
    if (!f) return;
    char line[64];
    size_t off = 0;
    while (fgets(line, sizeof(line), f) && off + 64 < sizeof(g_plugoff)) {
        /* 一行一个，顺手转成逗号分隔 */
        size_t L = strcspn(line, "\r\n");
        if (L == 0) continue;
        line[L] = 0;
        off += (size_t)snprintf(g_plugoff + off, sizeof(g_plugoff) - off,
                                "%s%s", off ? "," : "", line);
    }
    fclose(f);
}

/* 名字是否在停用名单里（逗号分隔精确匹配） */
static int plugin_disabled(const char *name) {
    if (!g_plugoff[0] || !name || !name[0]) return 0;
    size_t nl = strlen(name);
    const char *p = g_plugoff;
    while (*p) {
        const char *c = strchr(p, ',');
        size_t n = c ? (size_t)(c - p) : strlen(p);
        if (n == nl && !strncmp(p, name, n)) return 1;
        if (!c) break;
        p = c + 1;
    }
    return 0;
}

static void mark_disabled(const char *name) {
    if (!name || g_ndis >= MAX_PLUGINS) return;
    snprintf(g_disabled[g_ndis], sizeof(g_disabled[0]), "%s", name);
    g_ndis++;
}

static void load_plugins(const char *dir) {
    DIR *d = opendir(dir);
    if (!d) {
        fprintf(stderr, "插件目录打不开: %s\n", dir);
        return;
    }
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        size_t L = strlen(e->d_name);
        if (L < 4 || strcmp(e->d_name + L - 3, ".so") != 0) continue;
        char path[512];
        snprintf(path, sizeof(path), "%s/%s", dir, e->d_name);
        void *h = dlopen(path, RTLD_NOW);
        if (!h) {
            fprintf(stderr, "跳过 %s: %s\n", e->d_name, dlerror());
            continue;
        }

        int loaded = 0;

        /* ---- 被动工具：plugin_entry() ---- */
        plugin_desc_t *(*entry)(void) =
            (plugin_desc_t *(*)(void))dlsym(h, "plugin_entry");
        if (entry) {
            plugin_desc_t *p = entry();
            if (!p || p->api_version != PLUGIN_API_VERSION) {
                fprintf(stderr, "跳过 %s 的工具接口: API 版本不符\n", e->d_name);
            } else if (plugin_missing_dep(p->name)) {
                fprintf(stderr, "跳过 %s: 缺 %s（这个插件靠它干活）\n",
                        p->name, plugin_missing_dep(p->name));
                loaded = -1;                 /* 标记"因缺依赖跳过"，后面别再注册 */
            } else if (plugin_disabled(p->name)) {
                fprintf(stderr, "跳过 %s: 页面上停用了\n", p->name);
                mark_disabled(p->name);
                loaded = -1;
            } else if (find_plugin(p->name)) {
                fprintf(stderr, "跳过 %s: 工具名重复(%s)\n", e->d_name, p->name);
            } else if (g_n < MAX_PLUGINS) {
                g_plug[g_n++] = p;
                fprintf(stderr, "已加载: %-8s %s\n", p->name, p->description);
                loaded = 1;
            }
        }

        /* ---- 自动化能力：plugin_auto_entry()（可选，独立符号）---- */
        plugin_auto_t *(*aentry)(void) =
            (plugin_auto_t *(*)(void))dlsym(h, "plugin_auto_entry");
        if (aentry) {
            plugin_auto_t *a = aentry();
            if (!a || a->api_version != PLUGIN_AUTO_API_VERSION) {
                fprintf(stderr, "跳过 %s 的自动化接口: 版本不符\n", e->d_name);
            } else if (g_nauto < MAX_AUTO) {
                int dup = 0;
                for (int i = 0; i < g_nauto; i++)
                    if (!strcmp(g_auto[i]->name, a->name)) dup = 1;
                if (!dup) {
                    g_auto[g_nauto++] = a;
                    fprintf(stderr, "自动化: %-8s %s [%s]\n",
                            a->name, a->description, a->triggers ? a->triggers : "manual");
                    loaded = 1;
                }
            }
        }

        if (!loaded) fprintf(stderr, "跳过 %s: 既没有可用的工具接口也没有自动化接口\n", e->d_name);
    }
    closedir(d);
    /* 排序：让工具清单顺序与 readdir 无关 */
    if (g_n > 1) qsort(g_plug, (size_t)g_n, sizeof(g_plug[0]), cmp_plug);
}

/* 热加载：每次任务开始前重扫插件目录。
 * 新增 .so → 自动生效；删掉 .so → 下次重扫就不再出现在清单里。
 * 旧 .so 的句柄不主动 dlclose（可能正被别的请求用着，进程退出时自然回收）。 */
static void reload_plugins(const char *dir) {
    g_n = 0;
    g_nauto = 0;
    load_plugins(dir);
}

/* 按名字找自动化能力 */
static plugin_auto_t *find_auto(const char *name) {
    for (int i = 0; i < g_nauto; i++)
        if (!strcmp(g_auto[i]->name, name)) return g_auto[i];
    return NULL;
}

/* ---------- 纯对话预判 ----------
 * 3B 在 A55 上读 1.5KB 插件描述要 40~70 秒。纯知识问答不需要工具，
 * 把插件表整个省掉，prompt 掉到 150 字节，第一步能快一半。
 * 只认强动作词：宁可漏判（多带插件、慢一点）也别误判（模型手里没工具只能瞎答）。
 * 漏判不死：模型写出没列出的插件名，find_plugin() 仍是全局搜索。 */
static int is_pure_chat(const char *task) {
    static const char *actions[] = {
        "文件", "目录", "磁盘", "内存", "进程", "命令", "shell", "下载",
        "搜索", "grep", "读取", "写入", "负载", "温度", "系统信息",
        "ping", "路由", "dns", "端口", "网卡", "curl", "http", "接口", "api",
        "uci", "wifi", "无线", "ssid", "信道", "logread", "iwinfo",
        "表格", "csv", "xlsx", "excel", "docx", "pdf", "pptx", "排序", "排行",
        "计算", "解方程", "方程组", "求平均", "中位数", "方差", "质因数",
        "生成", "创建", "做一个", "做个", "写个", "脚本", "转成", "导出",
        "midi", "mp3", "旋律", "监控", "巡检",
        NULL
    };
    if (!task || !task[0]) return 0;
    for (int i = 0; actions[i]; i++)
        if (strstr(task, actions[i])) return 0;
    return 1;
}

/* ---------------- 任务 → 相关插件 ----------------
 * 带文档扩展名的任务只给 doc：以前 ".xlsx" 同时命中 doc 和 sys（sys 有
 * read 操作），模型看到两个能读文件的插件就挑错，去列目录了。
 *
 * 3B 的上下文很宝贵：把 10 个插件全塞进 system prompt，光读就要一两百秒，
 * 而且选择面越宽越容易选错。这里先按任务特征挑出 1~3 个相关的，
 * 模型只在这几个里挑。
 *
 * 安全性：框架的 find_plugin() 仍是全局搜索 —— 模型就算写出没列出的插件名
 * 也能执行，只是 prompt 里没提示它。所以"筛错"最坏是多花几步，不会死。
 * 一个关键词都不命中就全列（宁可多带，别让模型手里没工具）。
 */
/* 框架已经自己跑过 curl（用户粘了命令）→ 数据就在上下文里，
 * 不需要再列别的插件让模型去"取数据"。 */
static int g_prefetched = 0;
/* tools_prompt 判定这个任务是纯对话（不需要任何工具）时置 1。
 * 主循环据此换一套 system prompt —— 带 op 归属表和少样本的那套会被
 * 小模型当成"必须调工具"的证据，问"解释一下 NPU"它真会去调 jq。 */
static int g_pure_chat_task = 0;
/* 框架直连登录失败时的诊断，会注入给模型判断 */
static char g_login_diag[1600] = {0};

typedef struct {
    const char *plugin;
    const char *keys;        /* 用 | 分隔；中文直接 strstr */
} route_t;

static const route_t ROUTES[] = {
    {"math",  "算|解方程|方程式|方程组|多少次|平均值|平均数|中位数|方差|标准差|"
              "质因数|阶乘|排列|组合|斐波那契|相遇|追及|环形跑道|火车|过桥|工程|合作|鸡兔|"
              "百分|比例|面积|周长|体积|浓度|利润|打折|原题|应用题|求.*的值"},
    {"curl",  "curl|http|https|接口|api|请求|抓取|抓包|爬|网页数据|token|认证|header|"
              "cookie|接口返回|调接口"},
    {"sys",   "文件|目录|磁盘|内存|CPU|cpu|温度|进程|命令|shell|下载|搜索|grep|"
              "读取|写入|系统信息|负载|占用|最大|最小的.*文件|查看.*内容|文本"},
    {"net",   "网络|ping|路由|dns|端口|网卡|连通|丢包|延迟|路由表|域名|解析"},
    {"owrt",  "uci|openwrt|wifi|无线|ssid|信道|服务|重载|reload|日志|logread|"
              "路由器|配置.*网络|init\\.d|iwinfo"},
    {"doc",   "pdf|PDF|word|Word|excel|Excel|ppt|PPT|文档|xlsx|docx|pptx|"
              "导出|生成.*报告|做个.*表|幻灯片|演示"},
    {"music", "音乐|midi|MIDI|旋律|作曲|编曲|mp3|MP3|节奏|伴奏|曲子|歌曲|音色"},
    {"browser", "需要登录|登录后|cookie|session|鉴权|自动化浏览器|selenium"},
    {"table", "排序|排行|top|TOP|筛选|名次|前.*名"},
    {"batch", "分批|大量|很多(页|章|节)|长文档|多章节|整套"},
    {"sysmon","监控|巡检|告警|阈值|采样"},
};

/* 返回选中插件的下标数组，返回个数；0 = 没命中（调用方应该全列） */
/* ---------- 插件命令直连 ----------
 * 用户直接把插件命令贴进来（形如 {"op":"probe","url":"..."}）时，别丢给
 * 模型猜：3B 模型不认识这种输入，会当普通文本乱答（实测 {"op":"probe",...}
 * 被答成 "Goofish / [统计] 12 个插件 / C 框架"）。这里查表找到插件直接执行，
 * 结果原样返回 —— 跟登录场景的"框架直连"是同一个思路。
 */
static int json_str_field(const char *js, const char *key, char *out, size_t cap) {
    char pat[64];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    /* 只认「后面紧跟冒号」的匹配：字段值恰好等于某个 key 名时会误命中
     * （{"op":"query",...,"query":"x"} 找 query 会先撞上 op 的值），
     * 那样解析出来的就是隔壁字段。 */
    const char *p = js, *found = NULL;
    while ((p = strstr(p, pat)) != NULL) {
        const char *q = p + strlen(pat);
        while (*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') q++;
        if (*q == ':') { found = q; break; }
        p = q;
    }
    if (!found) return -1;
    p = found;
    p++;
    while (*p == ' ' || *p == '\t') p++;
    if (*p != '"') return -1;
    p++;
    size_t i = 0;
    while (*p && *p != '"' && i + 1 < cap) {
        if (*p == '\\' && p[1]) p++;
        out[i++] = *p++;
    }
    out[i] = 0;
    return 0;
}

static const char *op_to_plugin(const char *js, const char *op) {
    /* 显式指定优先：{"plugin":"jq","op":"probe","path":"..."} */
    static char pe[32];        /* static：这个指针要返回出去，不能放栈上 */
    pe[0] = 0;
    if (json_str_field(js, "plugin", pe, sizeof(pe)) == 0 && pe[0]) return pe;

    const int has_url  = strstr(js, "\"url\"")  != NULL;
    const int has_path = strstr(js, "\"path\"") != NULL || strstr(js, "\"file\"") != NULL;

    /* read / write 有歧义：Office/PDF 走 doc，纯文本走 sys。
     * 注意别把 .txt/.md/.csv 算给 doc —— doc 没有 write 这个 op（它只认
     * text/docx/xlsx/pptx/pdf），路由过去会掉进读逻辑报"读不出内容"。
     * sys 自己就能读写纯文本。 */
    if (!strcmp(op, "read") || !strcmp(op, "write")) {
        static const char *EXT[] = {".pdf", ".docx", ".xlsx", ".pptx",
                                    ".doc", ".xls", ".ppt", NULL};
        for (int i = 0; EXT[i]; i++)
            if (strstr(js, EXT[i])) return "doc";
        return "sys";
    }
    /* probe 也有歧义：带 url 是 browser 探测页面，带 path 是 jq 探测 JSON */
    if (!strcmp(op, "probe")) return (has_path && !has_url) ? "jq" : "browser";
    /* plan 也有歧义：带 chunks 是 batch 的分片规划，否则是 math 的计划类应用题 */
    if (!strcmp(op, "plan")) return strstr(js, "\"chunks\"") ? "batch" : "math";

    static const struct { const char *op, *plug; } M[] = {
        {"autologin","browser"}, {"login","browser"}, {"hunt","browser"},
        {"drag","browser"}, {"captcha","browser"}, {"cookie","browser"},
        {"shot","browser"},
        {"ping","net"}, {"port","net"}, {"resolve","net"}, {"route","net"},
        {"arp","net"}, {"link","net"}, {"wifi","net"}, {"trace","net"},
        {"hostname","net"}, {"stat","net"},
        {"calc","math"}, {"solve","math"}, {"system","math"}, {"num","math"},
        {"meet","math"}, {"ring","math"}, {"train","math"}, {"work","math"},
        {"chicken","math"}, {"plan","math"},
        {"get","jq"}, {"query","jq"}, {"table","jq"}, {"keys","jq"},
        {"csv","jq"}, {"extract","jq"}, {"page","jq"},
        {"sort","table"},        /* table 插件的排序；jq 的 table 是导出 CSV，不冲突 */
        /* doc 的写操作：源码 is_write_op() 认这几个，不是 "write" */
        {"docx","doc"}, {"xlsx","doc"}, {"pptx","doc"}, {"pdf","doc"},
        {"text","doc"}, {"docx_append","doc"}, {"xlsx_edit","doc"},
        {"xlsx_read","doc"},
        {"sysinfo","sys"}, {"shell","sys"}, {"list","sys"}, {"du","sys"},
        {"grep","sys"}, {"http_get","sys"}, {"download","sys"},
        {"uci_get","owrt"}, {"uci_set","owrt"}, {"uci_commit","owrt"},
        {"uci_show","owrt"}, {"uci_changes","owrt"}, {"uci_delete","owrt"},
        {"uci_add_list","owrt"}, {"uci_del_list","owrt"}, {"uci_revert","owrt"},
        {"iwinfo","owrt"}, {"iw","owrt"}, {"service","owrt"}, {"logread","owrt"},
        {"passwd","owrt"}, {"reboot","owrt"}, {"opkg_list","owrt"},
        {"midi","music"}, {"render","music"}, {"import","music"}, {"wav","music"},
        {"parse","curl"},        /* curl 插件的 op 名是 parse（吃 cURL 原文） */
        {"snapshot","sysmon"},
        {"start","sysmon-auto"}, {"stop","sysmon-auto"}, {"status","sysmon-auto"},
        {NULL, NULL}
    };
    for (int i = 0; M[i].op; i++)
        if (!strcmp(M[i].op, op)) return M[i].plug;
    return NULL;
}

/* 认出是插件命令就直接执行，返回 1（结果已打到 stdout） */
static int try_direct_plugin(const char *task) {
    if (!task || task[0] != '{') return 0;
    char op[48] = {0};
    if (json_str_field(task, "op", op, sizeof(op)) != 0 || !op[0]) return 0;
    const char *pw = op_to_plugin(task, op);
    if (!pw) return 0;
    plugin_desc_t *pl = find_plugin(pw);
    if (!pl) return 0;
    char *res = pl->run(task);
    if (res) {
        printf("%s\n", res);
        free(res);
    }
    return 1;
}

static int route_plugins(const char *task, int *idx, int maxn) {
    /* 任务里带网址、并且是要"抓/看/访问页面" → 只给 sys(能 http_get) 和
     * browser(能真正打开页面)。否则模型会去挑 curl，而 curl 要的是浏览器
     * 复制的整段命令，不是网址，一调就报"缺少 url"，白烧步数。 */
    if (maxn > 0 && task && (strstr(task, "http://") || strstr(task, "https://"))
        && (strstr(task, "抓取") || strstr(task, "爬取") || strstr(task, "抓")
            || strstr(task, "访问") || strstr(task, "打开") || strstr(task, "页面")
            || strstr(task, "网页") || strstr(task, "404") || strstr(task, "状态码"))) {
        int n = 0;
        for (int i = 0; i < g_n && n < maxn; i++)
            if (!strcmp(g_plug[i]->name, "sys") || !strcmp(g_plug[i]->name, "browser")
                || !strcmp(g_plug[i]->name, "curl"))
                idx[n++] = i;
        if (n > 0) return n;
    }
    /* 文档扩展名 → 只给 doc，不给别的选择 */
    /* 提到登录/验证码/抓请求头 → 只给 browser。curl 只能发请求，处理不了登录框，
     * 也拿不到 JS 现算的签名头（那得让浏览器自己跑一遍）。 */
    if (maxn > 0 && task && (strstr(task, "登录") || strstr(task, "登陆")
                             || strstr(task, "验证码") || strstr(task, "扫码")
                             || strstr(task, "请求头") || strstr(task, "抓包")
                             || strstr(task, "接口签名") || strstr(task, "签名参数")
                             || strstr(task, "login") || strstr(task, "signin"))) {
        for (int i = 0; i < g_n; i++)
            if (!strcmp(g_plug[i]->name, "browser")) { idx[0] = i; return 1; }
    }
    if (maxn > 0 && task && (strstr(task, ".xlsx") || strstr(task, ".xlsm")
                 || strstr(task, ".docx") || strstr(task, ".pptx")
                 || strstr(task, ".pdf"))) {
        for (int i = 0; i < g_n; i++)
            if (!strcmp(g_plug[i]->name, "doc")) { idx[0] = i; return 1; }
    }
    if (!task || !task[0]) return 0;
    int n = 0;
    int nroute = (int)(sizeof(ROUTES) / sizeof(ROUTES[0]));
    for (int i = 0; i < nroute && n < maxn; i++) {
        int pk = -1;
        for (int k = 0; k < g_n; k++)
            if (!strcmp(g_plug[k]->name, ROUTES[i].plugin)) { pk = k; break; }
        if (pk < 0) continue;                     /* 这个插件没装，跳过 */

        char keys[2048];
        snprintf(keys, sizeof(keys), "%s", ROUTES[i].keys);
        char *save = NULL;
        int hit = 0;
        for (char *kw = strtok_r(keys, "|", &save); kw; kw = strtok_r(NULL, "|", &save)) {
            if (*kw && strstr(task, kw)) { hit = 1; break; }
        }
        if (hit) idx[n++] = pk;
    }
    return n;
}

static char *tools_prompt(const char *task) {
    char *s = malloc(SMALL);
    if (!s) return NULL;
    size_t off = 0;

    /* 框架刚刚自己试过登录但没成 —— 把原因告诉模型，让它判断该建议什么，
     * 而不是让它一头雾水地重试一遍 */
    if (g_login_diag[0]) {
        off += (size_t)snprintf(s + off, SMALL - off,
            "【重要背景】框架刚替你尝试过自动登录，结果失败了：\n%s\n"
            "请判断原因，给用户明确结论和建议。不要自己反复重试登录"
            "（验证码认得出来早成功了）。\n\n", g_login_diag);
    }

    int idx[8];
    int nsel = 0;

    g_pure_chat_task = 0;
    /* 纯对话：路由一个都没命中，也没有强动作词 → 不给插件表。
     * 顺序很重要：先跑路由（math 的关键词里有"算"，"帮我算一下"能命中），
     * 命中就说明要动手，别当纯对话。 */
    if (!g_prefetched) {
        nsel = route_plugins(task, idx, 8);
        if (nsel == 0 && is_pure_chat(task)) {
            off += (size_t)snprintf(s + off, SMALL - off,
                "这个问题不需要任何工具，直接回答。\n"
                "格式：{\"tool\":\"final\",\"answer\":\"你的回答\"}\n"
                "回答用中文，条理清楚，别超过 200 字。\n");
            g_pure_chat_task = 1;
            if (getenv("AGENT_DEBUG")) fprintf(stderr, "  [路由] 纯对话，不带插件\n");
            return s;
        }
    }

    if (g_prefetched) {
        /* 数据已经抓好放上下文里了，只留 curl（要 jq 分批）和 sys（可能读本地文件） */
        for (int k = 0; k < g_n && nsel < 2; k++) {
            if (!strcmp(g_plug[k]->name, "curl") || !strcmp(g_plug[k]->name, "sys"))
                idx[nsel++] = k;
        }
    } else {
        nsel = route_plugins(task, idx, 8);
    }
    if (getenv("AGENT_DEBUG")) {
        fprintf(stderr, "  [路由] ");
        if (nsel > 0) {
            for (int i = 0; i < nsel; i++) fprintf(stderr, "%s ", g_plug[idx[i]]->name);
        } else {
            fprintf(stderr, "没命中关键词，全列 %d 个", g_n);
        }
        fprintf(stderr, "\n");
        fflush(stderr);      /* 重定向到文件时 stderr 可能被缓冲，调试信息会丢 */
    }

    if (nsel > 0) {
        off += (size_t)snprintf(s + off, SMALL - off, "可用工具：\n");
        for (int i = 0; i < nsel && off < SMALL - 400; i++) {
            plugin_desc_t *p = g_plug[idx[i]];
            char dshort[256];
            snprintf(dshort, sizeof(dshort), "%s", p->description ? p->description : "");
            utf8_cut(dshort, 150);
            off += (size_t)snprintf(s + off, SMALL - off, "- %s：%s\n", p->name, dshort);
        }
    } else {
        /* 没识别出领域：全列。宁可多带，别让模型手里没工具。 */
        off += (size_t)snprintf(s + off, SMALL - off, "可用工具：\n");
        for (int i = 0; i < g_n && off < SMALL - 400; i++) {
            /* 只给名字 + 描述开头。参数示例（params）动辄几百字，10 个插件加起来
             * 能把 system prompt 撑到 4KB —— RK3566 上 3B 一步要多跑两三百秒。 */
            char dshort[256];
            snprintf(dshort, sizeof(dshort), "%s", g_plug[i]->description ? g_plug[i]->description : "");
            utf8_cut(dshort, 150);
            off += (size_t)snprintf(s + off, SMALL - off, "- %s：%s\n",
                                    g_plug[i]->name, dshort);
        }
    }
    off += (size_t)snprintf(s + off, SMALL - off,
        "- final：最终答案  参数 {\"answer\":\"...\"}\n");
    return s;
}

/* 把最近产出的文件塞进上下文。
 * 用户常说"读取上面获取到的 json 文件"—— 但每个任务都是独立进程，
 * 上一轮的 resp_xxx.json 根本不在上下文里，模型只能瞎猜一个假路径。 */
static void inject_recent_files(char *log, size_t cap) {
    typedef struct { char name[400]; time_t mt; } fent_t;
    static fent_t f[64];
    int n = 0;

    static const char *dirs[] = { WORK_DIR "/downloads", WORK_DIR "/gen", NULL };
    for (int di = 0; dirs[di] && n < 60; di++) {
        DIR *d = opendir(dirs[di]);
        if (!d) continue;
        struct dirent *e;
        while ((e = readdir(d)) != NULL && n < 60) {
            if (e->d_name[0] == '.') continue;
            char p[400];
            snprintf(p, sizeof(p), "%s/%s", dirs[di], e->d_name);
            struct stat st;
            if (stat(p, &st) != 0 || !S_ISREG(st.st_mode)) continue;
            snprintf(f[n].name, sizeof(f[n].name), "%s", p);
            f[n].mt = st.st_mtime;
            n++;
        }
        closedir(d);
    }
    if (n == 0) return;

    /* 按修改时间倒序（n 很小，冒泡够用） */
    for (int i = 0; i < n; i++)
        for (int j = i + 1; j < n; j++)
            if (f[j].mt > f[i].mt) { fent_t t = f[i]; f[i] = f[j]; f[j] = t; }

    size_t off = strlen(log);
    if (off + 600 >= cap) return;
    off += (size_t)snprintf(log + off, cap - off,
        "\n【最近产出的文件（按时间倒序，用户说\"上面的文件\"一般指第一个）】\n");
    for (int i = 0; i < n && i < 5 && off + 260 < cap; i++)
        off += (size_t)snprintf(log + off, cap - off, "  %s\n", f[i].name);
}

/* 往对话记录里追加一行（结果回灌） */
static void log_append(char *log, size_t cap, const char *fmt, const char *a, const char *b) {
    size_t off = strlen(log);
    if (off + 2048 >= cap) return;
    snprintf(log + off, cap - off, fmt, a, b);
}

/* ============ 插件联动 ============
 * 一个插件的产出是另一个插件的输入时，别让用户再补一句话。
 * 规则表化：from 插件结果里的 field 等于 equals，就自动调 to，
 * 把结果里的 file 填进 args 的 %f。
 *
 * 现在一条：curl 抓到 JSON → jq 探测结构（有哪些字段、多少条）。
 * 响应不是 JSON（type != "json"）自然不触发。 */
typedef struct {
    const char *from;
    const char *field;
    const char *equals;
    const char *to;
    const char *args;
} pipe_t;

static const pipe_t PIPES[] = {
    {"curl", "type", "json", "jq", "{\"op\":\"probe\",\"file\":\"%f\"}"},
    {NULL, NULL, NULL, NULL, NULL}
};

/* ============ 任务分块 ============
 * 一句话里常有好几个动作（"输出表格，再转成 xlsx"）。以前是在一个 if 里
 * 把表格和 xlsx 写死串起来，用户加第三个动作就得改代码，而且后一句很容易
 * 被忽略。这里改成规则表：按关键词在任务里的位置切成有序步骤依次执行，
 * 后一步从上一步的产出里取文件继续。 */
typedef struct {
    const char *keys;      /* 触发关键词，| 分隔 */
    const char *plugin;    /* 执行插件 */
    const char *op;        /* 插件 op */
    int use_prev;          /* 1 = 要吃上一步的产出（file/sheets） */
} chunk_rule_t;

static const chunk_rule_t CHUNKS[] = {
    {"表格|table",              "jq",  "table", 0},
    {"xlsx|excel|电子表格",     "doc", "xlsx",  1},
    /* 系统状态类：确定性查询，直接调 sysinfo，别让模型先 list 再瞎算 */
    {"磁盘空间|剩余空间|磁盘占用|内存占用|系统负载|芯片温度|系统信息|load average|df|free -m",
                                "sys", "sysinfo", 0},
    {NULL, NULL, NULL, 0}
};

/* 按顺序执行任务里的各个动作块；返回执行了几块（0 = 没命中，走模型） */
static int run_task_chunks(const char *task) {
    typedef struct { size_t pos; int ri; } hit_t;
    hit_t hits[8];
    int nh = 0;

    for (int ri = 0; CHUNKS[ri].keys; ri++) {
        char keys[512];
        snprintf(keys, sizeof(keys), "%s", CHUNKS[ri].keys);
        char *sv = NULL;
        size_t best = (size_t)-1;
        for (char *k = strtok_r(keys, "|", &sv); k; k = strtok_r(NULL, "|", &sv)) {
            if (!k[0]) continue;
            const char *q = strstr(task, k);
            if (q && (size_t)(q - task) < best) best = (size_t)(q - task);
        }
        if (best != (size_t)-1 && nh < 8) { hits[nh].pos = best; hits[nh].ri = ri; nh++; }
    }
    if (nh == 0) return 0;

    /* 按在任务里出现的先后排序 */
    for (int i = 0; i < nh; i++)
        for (int j = i + 1; j < nh; j++)
            if (hits[j].pos < hits[i].pos) { hit_t t = hits[i]; hits[i] = hits[j]; hits[j] = t; }

    char prev_file[900] = {0};        /* 上一步产出的主 csv */
    char prev_sheets[4096] = {0};     /* 上一步产出的子表，预拼成 ,{"name":..,"path":..} */
    int ran = 0;

    for (int hi = 0; hi < nh; hi++) {
        const chunk_rule_t *cr = &CHUNKS[hits[hi].ri];
        plugin_desc_t *pl = find_plugin(cr->plugin);
        if (!pl) continue;

        char args[8192];
        if (cr->use_prev) {
            if (!prev_file[0]) continue;         /* 没有数据源，跳过这一步 */
            snprintf(args, sizeof(args),
                "{\"op\":\"%s\",\"path\":\"%s/gen/multi_%ld.xlsx\",\"csv_files\":["
                "{\"name\":\"主表\",\"path\":\"%s\"}",
                cr->op, WORK_DIR, (long)time(NULL), prev_file);
            if (prev_sheets[0])
                strncat(args, prev_sheets, sizeof(args) - strlen(args) - 1);
            strncat(args, "]}", sizeof(args) - strlen(args) - 1);
        } else {
            snprintf(args, sizeof(args), "{\"op\":\"%s\",\"limit\":15}", cr->op);
        }

        char *res = pl->run(args);
        if (!res) continue;
        if (strstr(res, "\"error\"")) { free(res); continue; }

        printf("\n【第 %d 步：%s.%s（框架按任务顺序执行）】\n", ++ran, cr->plugin, cr->op);
        printf("  [%s] %s\n\n", cr->plugin, res);

        /* 把产出留给下一步 */
        char err5[128];
        jval *rv = json_parse(res, err5, sizeof(err5));
        if (rv) {
            const char *f = json_str(json_get(rv, "file"));
            if (f && f[0]) snprintf(prev_file, sizeof(prev_file), "%s", f);
            jval *sh = json_get(rv, "sheets");
            if (sh) {
                size_t si = 0;
                prev_sheets[0] = 0;
                for (int i = 0; si + 500 < sizeof(prev_sheets); i++) {
                    jval *it = json_at(sh, i);
                    if (!it) break;
                    const char *nm = json_str(json_get(it, "name"));
                    const char *pt = json_str(json_get(it, "path"));
                    if (!nm || !pt) continue;
                    si += (size_t)snprintf(prev_sheets + si, sizeof(prev_sheets) - si,
                        ",{\"name\":\"%s\",\"path\":\"%s\"}", nm, pt);
                }
            }
            json_free(rv);
        }
        free(res);
    }
    return ran;
}


/* ============ 登录流程直连 ============
 * 任务形如："账号=xxx 密码=yyy 登录 https://x.com/login"
 * 3B 组不出 {"op":"probe",...} 这种参数（实测两次传空 url），
 * 而登录本身是确定性流程：开页面 → 填账号密码 → 点登录 → 拿 cookie。
 * 框架自己做，几秒钟出结果。 */

/* 从任务里抠出账号和密码 */
static int extract_cred(const char *task, char *user, size_t ucap,
                        char *pass, size_t pcap) {
    static const char *ukeys[] = {"账号=", "用户名=", "帐号=", "用户=", NULL};
    static const char *pkeys[] = {"密码=", "口令=", NULL};
    for (int i = 0; ukeys[i]; i++) {
        const char *a = strstr(task, ukeys[i]);
        if (!a) continue;
        a += strlen(ukeys[i]);
        size_t n = 0;
        while (*a && !isspace((unsigned char)*a) && *a != '，' && *a != ',' && n + 1 < ucap) {
            if ((unsigned char)*a < 0x80 || (unsigned char)*a >= 0xe0) {
                /* ASCII 或中文首字节 → 收；中文账号少见，遇到标点就停 */
            }
            user[n++] = *a++;
        }
        user[n] = 0;
        for (int k = 0; pkeys[k]; k++) {
            const char *b = strstr(task, pkeys[k]);
            if (!b) continue;
            b += strlen(pkeys[k]);
            size_t m = 0;
            while (*b && !isspace((unsigned char)*b) && *b != '，' && *b != ',' && m + 1 < pcap)
                pass[m++] = *b++;
            pass[m] = 0;
            if (user[0] && pass[0]) return 1;
        }
    }
    return 0;
}

/* 返回 1 表示已处理（打印了结果），0 表示不是登录任务 */
static int run_login_flow(const char *task) {
    if (!task) return 0;
    if (!(strstr(task, "登录") || strstr(task, "登陆") || strstr(task, "login")))
        return 0;

    const char *u = strstr(task, "http");
    if (!u) return 0;
    char url[512];
    size_t ul = 0;
    while (u[ul] && ul + 1 < sizeof(url)) {
        unsigned char c = (unsigned char)u[ul];
        if (c <= 0x20) break;                          /* 空白 */
        if (c >= 0x80) break;                          /* 中文标点 */
        url[ul++] = (char)c;
    }
    url[ul] = 0;
    /* 去掉尾部可能粘上的标点 */
    while (ul > 0 && strchr(".,;:!?)]}>", url[ul-1])) url[--ul] = 0;
    if (ul < 8) return 0;

    char user[128] = {0}, pass[128] = {0};
    if (!extract_cred(task, user, sizeof(user), pass, sizeof(pass))) return 0;

    plugin_desc_t *bp = find_plugin("browser");
    if (!bp) return 0;

    char args[1400];
    snprintf(args, sizeof(args),
             "{\"op\":\"autologin\",\"url\":\"%s\",\"user\":\"%s\",\"pass\":\"%s\"}",
             url, user, pass);

    printf("\n【框架自动登录（不用模型）】\n  目标 %s\n", url);
    fflush(stdout);
    char *res = bp->run(args);
    if (!res) return 0;
    printf("  [browser] %s\n\n", res);

    /* 失败（验证码认不出 / 没登进去）→ 不截走这次任务，
     * 把诊断信息交给模型判断下一步（模型能看到 next_steps / diagnosis） */
    int failed = (!strstr(res, "\"ok\": true") && !strstr(res, "\"ok\":true"))
                 || strstr(res, "\"captcha_failed\"")
                 || strstr(res, "\"likely_ok\": false")
                 || strstr(res, "\"likely_ok\":false");
    if (failed) {
        /* 把插件返回的诊断（error / next_steps / diagnosis）摘给模型 */
        char jerr[128] = {0};
        jval *j = json_parse(res, jerr, sizeof(jerr));
        const char *emsg = j ? json_str(json_get(j, "error")) : NULL;
        const char *hint = j ? json_str(json_get(j, "hint")) : NULL;
        snprintf(g_login_diag, sizeof(g_login_diag),
                 "原因：%s%s%s\n原始返回：%.600s",
                 emsg ? emsg : "登录没通过",
                 hint ? "（" : "", hint ? hint : "");
        if (hint) strncat(g_login_diag, "）", sizeof(g_login_diag) - strlen(g_login_diag) - 1);
        if (j) json_free(j);
    }
    free(res);
    if (failed) {
        printf("【登录没成，交给模型判断】\n\n");
        fflush(stdout);
        return 0;                     /* 继续走模型流程，它能读诊断给建议 */
    }
    return 1;
}

/* 执行联动：返回下游插件的结果（malloc，调用方 free），没有联动返回 NULL */
static char *pipe_followup(const char *tool, const char *out, const char *why) {
    for (int pi = 0; PIPES[pi].from; pi++) {
        if (strcmp(tool, PIPES[pi].from)) continue;
        char perr[128];
        jval *rv = json_parse(out, perr, sizeof(perr));
        if (!rv) return NULL;
        const char *fv = json_str(json_get(rv, PIPES[pi].field));
        const char *filev = json_str(json_get(rv, "file"));
        int hit = fv && PIPES[pi].equals && !strcmp(fv, PIPES[pi].equals)
                  && filev && filev[0];
        /* file 必须复制出来：json_free 之后 filev 就悬空了，
         * 后面拼参数会拿到垃圾（表现为"没有可用的 json 文件"） */
        char filecpy[800] = {0};
        if (hit) snprintf(filecpy, sizeof(filecpy), "%s", filev);
        json_free(rv);
        if (!hit) return NULL;

        plugin_desc_t *nx = find_plugin(PIPES[pi].to);
        if (!nx) return NULL;
        char nargs[1400];
        size_t oi = 0;
        for (const char *tp = PIPES[pi].args; *tp && oi + 2 < sizeof(nargs); tp++) {
            if (tp[0] == '%' && tp[1] == 'f') {
                size_t fl = strlen(filecpy);
                if (oi + fl + 2 >= sizeof(nargs)) break;
                memcpy(nargs + oi, filecpy, fl);
                oi += fl;
                tp++;
            } else nargs[oi++] = *tp;
        }
        nargs[oi] = 0;
        (void)why;
        printf("  ［联动］%s → %s\n", tool, nx->name);
        return nx->run(nargs);
    }
    return NULL;
}

/* ============ main ============ */
/* ---------- 应用题直连 math ----------
 *
 * 工程/行程/鸡兔/环形这类题，math 插件能确定性解（毫秒级），而让 3B 先
 * 判断"该调哪个 op"再算，实测要 140 秒以上，小模型还经常列错式子。
 *
 * 判据直接用 ROUTES 里 math 那条的关键词（框架本来就有，不是新造的词表）。
 * 插件认不出题型就返回 error → 这里原样回落给模型，不影响开放式问题。
 */
static int try_math_direct(const char *task) {
    if (!task || task[0] == '{') return 0;
    size_t L = strlen(task);
    if (L < 8 || L > 320) return 0;

    static const char *K[] = {
        "工程", "合作", "合做", "相遇", "追及", "环形跑道", "火车", "过桥",
        "鸡兔", "多少次", "平均值", "平均数", "中位数", "方差", "标准差",
        "质因数", "阶乘", "排列", "组合", "斐波那契", "百分", "比例",
        "面积", "周长", "体积", "浓度", "利润", "打折", NULL
    };
    int hit = 0;
    for (int i = 0; K[i]; i++)
        if (strstr(task, K[i])) { hit = 1; break; }
    if (!hit) return 0;

    plugin_desc_t *pl = find_plugin("math");
    if (!pl) return 0;

    char *esc = malloc(strlen(task) * 6 + 16);
    if (!esc) return 0;
    json_escape(task, esc, strlen(task) * 6 + 16);

    char *call = malloc(strlen(esc) + 64);
    if (!call) { free(esc); return 0; }
    snprintf(call, strlen(esc) + 64, "{\"op\":\"plan\",\"task\":\"%s\"}", esc);
    char *res = pl->run(call);
    free(call);
    free(esc);
    if (!res) return 0;
    if (strstr(res, "\"error\"")) { free(res); return 0; }

    printf("%s\n", res);
    free(res);
    return 1;
}

/* ---------- 生成 BGM 直连 music ----------
 *
 * "生成一段 mid bgm，风格随意" 这类，交给模型是自己给自己找麻烦：3B 每次
 * 编出来的音符都不一样，一段 8 秒 BGM 要调三四次 music，五六分钟还不一定
 * 好听。风格→音阶/节奏/配器 是固定的乐理套路，插件用模板 + 固定种子生成，
 * 秒级出结果。这里只负责认出"这是要 BGM"，风格词交给插件自己解析。
 */
static int try_music_direct(const char *task) {
    if (!task || task[0] == '{') return 0;
    int want = strstr(task, "bgm") || strstr(task, "BGM") || strstr(task, "背景音乐")
            || strstr(task, "配乐") || strstr(task, "一段音乐") || strstr(task, "一段曲子")
            || strstr(task, "来段音乐") || strstr(task, "生成音乐");
    if (!want) return 0;
    if (!(strstr(task, "生成") || strstr(task, "来") || strstr(task, "做")
          || strstr(task, "写") || strstr(task, "给我") || strstr(task, "整")))
        return 0;

    plugin_desc_t *pl = find_plugin("music");
    if (!pl) return 0;

    char *esc = malloc(strlen(task) * 6 + 16);
    if (!esc) return 0;
    json_escape(task, esc, strlen(task) * 6 + 16);
    char *call = malloc(strlen(esc) + 48);
    if (!call) { free(esc); return 0; }
    snprintf(call, strlen(esc) + 48, "{\"op\":\"bgm\",\"task\":\"%s\"}", esc);
    char *res = pl->run(call);
    free(call);
    free(esc);
    if (!res) return 0;
    if (strstr(res, "\"error\"")) { free(res); return 0; }
    printf("%s\n", res);
    free(res);
    return 1;
}

int main(int argc, char **argv) {
    const char *task = NULL;
    const char *model = "qwen2.5-3b-q4_k_m.gguf";
    const char *host = "127.0.0.1";
    const char *plugdir = "/opt/llama/agent/plugins";
    int port = 8090, max_steps = 4, env_only = 0, auto_list = 0, daemon_mode = 0;
    int auto_state_only = 0;
    const char *auto_name = NULL;
    const char *auto_stop_name = NULL;
    const char *auto_args = "{}";

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--task") && i + 1 < argc)          task = argv[++i];
        else if (!strcmp(argv[i], "--model") && i + 1 < argc)     model = argv[++i];
        else if (!strcmp(argv[i], "--plugins") && i + 1 < argc)   plugdir = argv[++i];
        else if (!strcmp(argv[i], "--port") && i + 1 < argc)      port = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--max-steps") && i + 1 < argc) max_steps = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--host") && i + 1 < argc)     host = argv[++i];
        else if (!strcmp(argv[i], "--env")) env_only = 1;
        else if (!strcmp(argv[i], "--auto-list")) auto_list = 1;
        else if (!strcmp(argv[i], "--auto-state")) auto_state_only = 1;
        else if (!strcmp(argv[i], "--auto") && i + 1 < argc) auto_name = argv[++i];
        else if (!strcmp(argv[i], "--auto-stop") && i + 1 < argc) auto_stop_name = argv[++i];
        else if (!strcmp(argv[i], "--daemon")) daemon_mode = 1;
        else if (!strcmp(argv[i], "--args") && i + 1 < argc) auto_args = argv[++i];
        else if (!strcmp(argv[i], "--memcheck")) g_memcheck = 1;
        else task = argv[i];
    }
    if (!task && !env_only && !auto_list && !auto_name && !auto_stop_name && !auto_state_only) {
        fprintf(stderr,
            "用法:\n"
            "  agent-c --task '任务' [--max-steps N] [--plugins DIR]   跑一次 agent 任务\n"
            "  agent-c --env                                          环境 + 插件清单\n"
            "  agent-c --auto-list                                    列出自动化能力\n"
            "  agent-c --auto NAME [--args '{...}'] [--daemon]        启动一个自动化流程\n"
            "  agent-c --auto-stop NAME                               停止正在运行的自动化\n");
        return 1;
    }

    read_plugoff();            /* 用户在页面上停用的插件名单 */
    reload_plugins(plugdir);   /* 每次启动重扫：新增/删除 .so 都生效，不用改框架 */

    /* 任务本身就是一条插件命令（{"op":"probe",...}）→ 直接执行，不惊动模型。
     * 3B 模型不认识这种输入，会当普通文本乱答。 */
    if (task && try_direct_plugin(task)) return 0;
    if (task && try_math_direct(task)) return 0;
    if (task && try_music_direct(task)) return 0;
    if (auto_state_only) {
        /* 高频轮询专用：不探测环境、不输出描述，只回状态，几十字节。
         * 页面打开面板时每 2 秒拉一次，用不着重建整个面板。 */
        printf("{\"auto\":[");
        for (int i = 0; i < g_nauto; i++) {
            pid_t apid = 0;
            int running = auto_state(g_auto[i]->name, &apid);
            printf("%s{\"name\":\"%s\",\"running\":%d,\"pid\":%d}",
                   i ? "," : "", g_auto[i]->name,
                   running ? 1 : 0, running ? (int)apid : 0);
        }
        printf("]}\n");
        return 0;
    }

    if (auto_list) {
        printf("{\"auto\":[");
        for (int i = 0; i < g_nauto; i++) {
            /* params 本身是 JSON 片段，里面的引号必须转义，否则外层 JSON 就坏了 */
            char pe[512] = {0};
            size_t pi = 0;
            const char *ps = g_auto[i]->params ? g_auto[i]->params : "{}";
            for (const char *q = ps; *q && pi + 3 < sizeof(pe); q++) {
                if (*q == '"') { pe[pi++] = '\\'; pe[pi++] = '"'; }
                else pe[pi++] = *q;
            }
            pid_t apid = 0;
            int running = auto_state(g_auto[i]->name, &apid);
            printf("%s{\"name\":\"%s\",\"desc\":\"%s\",\"triggers\":\"%s\",\"params\":\"%s\","
                   "\"running\":%d,\"pid\":%d}",
                   i ? "," : "", g_auto[i]->name, g_auto[i]->description,
                   g_auto[i]->triggers ? g_auto[i]->triggers : "manual", pe,
                   running ? 1 : 0, running ? (int)apid : 0);
        }
        printf("]}\n");
        return 0;
    }

    if (auto_stop_name) {
        pid_t apid = 0;
        if (!auto_state(auto_stop_name, &apid)) {
            printf("{\"ok\":1,\"stopped\":0,\"note\":\"%s 当前没有在运行\"}\n", auto_stop_name);
            return 0;
        }
        if (kill(apid, SIGTERM) == 0) {
            /* 等它自己清理退出（最多 3 秒），插件收到 SIGTERM 会结束当前轮次 */
            for (int w = 0; w < 30; w++) {
                usleep(100000);
                if (kill(apid, 0) != 0) break;
            }
            auto_pid_clear(auto_stop_name);
            printf("{\"ok\":1,\"stopped\":1,\"pid\":%d}\n", (int)apid);
        } else {
            printf("{\"ok\":0,\"error\":\"发信号失败\"}\n");
        }
        return 0;
    }

    if (auto_name) {
        plugin_auto_t *a = find_auto(auto_name);
        if (!a) {
            printf("{\"error\":\"没有自动化能力: %s\"}\n", auto_name);
            return 1;
        }
        if (!a->start) {
            printf("{\"error\":\"%s 没有实现 start\"}\n", auto_name);
            return 1;
        }
        /* 已经在跑就别再起一个：否则新进程覆盖 pid 文件，
         * 老进程没人管，成了孤儿还在后台转。 */
        pid_t old = 0;
        if (auto_state(auto_name, &old)) {
            printf("{\"ok\":1,\"already\":1,\"running\":1,\"pid\":%d,"
                   "\"answer\":\"%s 已经在运行（pid %d）\"}\n",
                   (int)old, auto_name, (int)old);
            return 0;
        }
        /* 告诉插件这一次是"前台一次性跑"还是"后台守护"：
         * 一次性跑的插件应该自己收敛（比如巡检只跑几轮就结束），
         * 守护的才允许一直循环。用环境变量传，不污染参数 JSON。 */
        if (!daemon_mode) setenv("AGENT_AUTO_FG", "1", 1);
        else unsetenv("AGENT_AUTO_FG");

        /* 守护模式：把 pid 落盘，页面才知道在跑、才停得掉。
         * 非守护（一次性）也写，这样运行期间页面能显示"运行中"。 */
        auto_pid_write(auto_name);
        char *r = a->start(auto_args);
        auto_pid_clear(auto_name);
        printf("%s\n", r ? r : "{\"error\":\"start 返回空\"}");
        free(r);
        return 0;
    }

    if (env_only) {
        /* 10 个插件的描述加起来三四千字，4096 会把 JSON 截断 */
        char envbuf[16384];
        /* 缓存逻辑在 env_json 内部（只缓存库探测结果，插件状态永远实时）。
         * 这里不要再套一层缓存，否则页面拿到的是旧的 running 状态。 */
        env_json(envbuf, sizeof(envbuf));
        printf("%s\n", envbuf);
        return 0;
    }
    if (g_n == 0) {
        fprintf(stderr, "没有加载到插件，检查目录 %s\n", plugdir);
        return 1;
    }

    /* task 转义提前做：预执行（curl）和 prompt 都要用 */
    size_t tcap = strlen(task) * 6 + 16;
    char *task_esc = malloc(tcap);
    if (!task_esc) return 1;
    json_escape(task, task_esc, tcap);

    /* ===== 预处理：用户直接粘了一条 curl 命令 =====
     * 不让模型转述：1.7KB 的 curl 原文（十几条 -H、长 cookie、各种引号）
     * 一是每步都要重读一遍，3B 在 A55 上一步两分钟；二是模型转述必然走样。
     * 必须放在 tools_prompt 之前 —— 跑成功就置 g_prefetched，
     * 路由随后只带 curl/sys，不再把 doc/math 之类塞给模型去"取数据"。 */
    char *pre_result = NULL;
    if (g_n > 0 && strstr(task, "curl ") && strstr(task, "http")) {
        plugin_desc_t *cp = find_plugin("curl");
        if (cp) {
            size_t rcap = strlen(task) * 2 + 256;
            char *req = malloc(rcap);
            if (req) {
                snprintf(req, rcap, "{\"op\":\"send\",\"curl\":\"%s\"}", task_esc);
                printf("\n【框架直接执行了粘贴的 curl，不让模型转述】\n");
                pre_result = cp->run(req);
                if (pre_result) g_prefetched = 1;
                free(req);
            }
        }
    }

    /* ===== 预处理 2：按任务内容分块执行 =====
     * "输出表格，再转成 xlsx" 拆成两步：jq 出表格 → doc 打包 xlsx，
     * 上一步的 CSV 自动喂给下一步。以前这里是一个写死的 if（表格+xlsx），
     * 后一句常被忽略，加第三个动作还得改代码。 */
    /* 登录任务：框架直连（比模型两轮快得多，也不怕 3B 组不出参数） */
    if (!g_prefetched && g_n > 0) {
        if (run_login_flow(task)) { free(task_esc); return 0; }
    }

    if (!g_prefetched && g_n > 0 && strlen(task) < 120) {
        int nran = run_task_chunks(task);
        if (nran > 0) {
            free(task_esc);
            return 0;
        }
    }

    char *tools = tools_prompt(task);
    if (!tools) { free(task_esc); free(pre_result); return 1; }

    /* system prompt */
    char sys_raw[BIG];
    if (g_pure_chat_task) {
        /* 纯对话：不给工具表、不给 op 示例，否则它会挑一个工具去调 */
        snprintf(sys_raw, sizeof(sys_raw),
            "你是运行在路由器上的本地 AI 助手。用户问的是知识性 / 解释性问题，"
            "**不需要任何工具**，直接凭你的知识回答。\n"
            "回答用中文，简洁准确，不超过 200 字。\n"
            "只输出一个 JSON：{\"tool\":\"final\",\"answer\":\"你的回答\"}。"
            "answer 里就是最终答案本身，不要提工具、不要提 JSON。");
    } else
    snprintf(sys_raw, sizeof(sys_raw),
        "你只能输出一个 JSON 对象，不要解释、不要代码块。"
        "**每个 JSON 都必须有 tool 字段**（比如 {\"tool\":\"doc\",\"op\":\"xlsx\",...}），"
        "漏了 tool 就等于没调用。\n%s"
        /* 别写 \"插件名\" 这种占位符 —— 3B 会原样照抄，输出 {"tool":"插件名",参数} */
        "调用格式：{\"tool\":\"sys\",\"op\":\"list\",\"path\":\"/tmp\"}"
        "（tool 用上面列表里的名字）；答完：{\"tool\":\"final\",\"answer\":\"...\"}\n"
        "【op 归属表】op 放在哪个插件是固定的，别放错：\n"
        "  sys    : shell list du grep read write sysinfo http_get download\n"
        "  owrt   : uci_get uci_set uci_show uci_commit uci_revert iwinfo iw "
        "service logread passwd reboot opkg_list\n"
        "  net    : ping port resolve route arp stat link wifi trace hostname\n"
        "  math   : calc solve system stat num（应用题给 task 让插件自己判题型）\n"
        "  doc    : docx xlsx pptx pdf text（Office/PDF 读写）\n"
        "  jq     : probe get query csv table keys（JSON）\n"
        "  table  : sort（文本表格排序）\n"
        "  browser: probe autologin hunt drag captcha cookie shot\n"
        "  music  : midi render import\n"
        "  curl   : parse（吃浏览器 Copy as cURL 的整段原文，不是网址）\n"
        "注意：日志是 owrt 的 logread，不是 sys；OpenWrt 上没有 /var/log/syslog 这个文件。\n"
        "【示例】\n"
        "用户：写个 shell 脚本统计日志里 404 的数量\n"
        "→ {\"tool\":\"sys\",\"op\":\"write\",\"path\":\"/tmp/count404.sh\","
        "\"content\":\"#!/bin/sh\\nlogread | grep -c -- 404\\n\"}\n"
        "  然后 → {\"tool\":\"sys\",\"op\":\"shell\",\"cmd\":\"sh /tmp/count404.sh\"}\n"
        "用户：看看最近 20 行日志\n"
        "→ {\"tool\":\"owrt\",\"op\":\"logread\",\"lines\":20}\n"
        "用户：日志里有没有 error\n"
        "→ {\"tool\":\"owrt\",\"op\":\"logread\",\"lines\":60,\"key\":\"error\"}\n"
        "用户：抓 http://x/y 看看是不是 404\n"
        "→ {\"tool\":\"sys\",\"op\":\"http_get\",\"url\":\"http://x/y\"}\n"
        "凡是算式、方程、方程组、统计、数论题，一律调用 math 插件，不要心算。\n"
        "应用题（工程量、行程、相遇追及）把原题文字直接传给 math（只给 task，不要自己指定 op，"
        "也不要自己列式子）——插件会自己判断题型；列错式子算得再准也是错答案。\n",
        tools);
    size_t syscap = strlen(sys_raw) * 6 + 16;
    char *sys_esc = malloc(syscap);
    if (!sys_esc) { free(tools); return 1; }
    json_escape(sys_raw, sys_esc, syscap);

    /* 对话记录：任务 + 每步结果 */
    char *log = malloc(BIG);
    if (!log) { free(sys_esc); free(tools); free(task_esc); free(pre_result); return 1; }
    char *last_result_init = NULL;      /* 预执行（curl）的结果，交给兜底收尾用 */

    if (pre_result) {
        char *cut = strdup(pre_result);
        if (cut) {
            utf8_cut(cut, 1200);       /* 3B 扛不住长上下文：只给摘要 */
            const char *cs = strstr(task, "curl ");
            size_t pre = cs ? (size_t)(cs - task) : 0;
            while (pre > 0 && (task[pre - 1] == ' ' || task[pre - 1] == '\n'
                               || task[pre - 1] == '\\'))
                pre--;
            if (pre > 200) pre = 200;
            if (pre > 3) {
                /* 用户除了 curl 还写了要求（"帮我找出前 5 条标题"）→ 保留他的话，走模型 */
                snprintf(log, BIG,
                    "任务：%.*s\n\n"
                    "【数据已经抓到了，直接用下面这份，不要再调工具去取】\n\n%s\n\n"
                    "要更多字段用 curl 的 op=jq 分批取；不要再发一次请求，"
                    "也不要把 URL 当文件路径去读。\n",
                    (int)pre, task, cut);
            } else {
                /* 只粘了一条 curl，没有别的指令 —— 用户要的就是
                 * "把数据取回来、整理好、能下载"。这活框架自己就干完了，
                 * 不需要模型（3B 只会把数据再"整理"一遍，白烧几分钟）。 */
                /* 必须带 [curl] 前缀：前端靠这个标记把结果渲染成表格和
                 * 代码块；直接打 JSON 原文，用户看到的就是一串转义字符。 */
                /* 前端拿完整的（要能 JSON.parse 才能渲染表格），
                 * 模型拿截断的（context 宝贵） */
                printf("  [curl] %s\n\n", pre_result);
                /* 抓到的是 JSON → 顺手让 jq 报一下结构（多少条、哪些字段） */
                {
                    char *nxo = pipe_followup("curl", pre_result, "prefetch");
                    if (nxo) {
                        char cut2[BIG];
                        snprintf(cut2, sizeof(cut2), "%s", nxo);
                        utf8_cut(cut2, 3000);
                        printf("  [jq] %s\n\n", cut2);
                        free(nxo);
                    }
                }
                free(cut);
                free(pre_result);
                free(log);
                free(sys_esc);
                free(tools);
                free(task_esc);
                return 0;
            }
            printf("  [curl] %s\n\n", pre_result);
            {
                char *nxo = pipe_followup("curl", pre_result, "prefetch");
                if (nxo) {
                    char cut2[BIG];
                    snprintf(cut2, sizeof(cut2), "%s", nxo);
                    utf8_cut(cut2, 3000);
                    printf("  [jq] %s\n\n", cut2);
                    free(nxo);
                }
            }
            free(cut);
            last_result_init = pre_result;    /* 所有权转走 */
            pre_result = NULL;
        }
    } else {
        snprintf(log, BIG, "任务：%s\n", task_esc);
    }
    /* 只在任务明确指代"上面那个文件"时才注入最近文件列表。
     * 无条件注入的话，模型看到一串 /tmp/xxx.log 会自作主张去列目录
     * （实测问"NPU 是什么"它先调了 sys list /tmp）。 */
    if (task && (strstr(task, "上面") || strstr(task, "上文") || strstr(task, "刚才")
                 || strstr(task, "那个文件") || strstr(task, "这个文件")
                 || strstr(task, "上面的") || strstr(task, "之前")))
        inject_recent_files(log, BIG);
    /* task_esc 不在这里释放：下面调 math 时要用它补 task 字段 */

    printf("任务: %s\n插件: %d 个\n", task, g_n);

    int rc = 2;
    /* 插件返回的数值：模型 final 时如果没体现出来，由框架补一行。
     * 3B 经常把表达式原样当答案（问"根号3加2"就答 "sqrt(3)+2"），
     * 插件明明算出了 3.732 却不展示，用户会以为算错了。 */
    char last_val[64] = {0};
    char *last_result = last_result_init;   /* 最后一次插件返回的 JSON，兜底收尾用 */
    /* 任务里出现网址 + "抓/看/404/状态码" 时，直接把该调的 JSON 贴给模型抄。
     * 0.5B/3B 自己选 op 很不可靠：实测会去调 curl（要整段 cURL，不是网址）
     * 或者 sys 的 list（列出 /tmp 目录），最后胡答一通。给它一条现成的调用
     * 最省事，也最不容易跑偏。 */
    if (task && (strstr(task, "http://") || strstr(task, "https://"))
        && (strstr(task, "404") || strstr(task, "状态码") || strstr(task, "抓")
            || strstr(task, "访问") || strstr(task, "打开") || strstr(task, "页面"))) {
        const char *u = strstr(task, "http");
        char url[600];
        size_t k = 0;
        while (u && *u && k + 1 < sizeof(url)
               && *u != ' ' && *u != '\n' && *u != '\t' && *u != '"' && *u != '\''
               && *u != '，' && *u != '。')
            url[k++] = *u++;
        url[k] = 0;
        if (k > 8) {
            char hint[800];
            snprintf(hint, sizeof(hint),
                     "（框架提示：任务里有网址 %s。要取状态码和页面内容请直接调用 "
                     "{\"op\":\"http_get\",\"url\":\"%s\"}，"
                     "不要用 curl（它要整段 cURL 命令），也不要用 list/du 看目录。）\n",
                     url, url);
            log_append(log, BIG, "%s", hint, "");
        }
    }

    /* 步数上限不是硬墙：超过 max_steps 后，只要还在推进（没触发熔断）、
     * 也没超出时间预算，就自动续跑。用户的原话是"4 步不够就把结果存下来
     * 继续跑"，而不是"4 步一到就报未完成"。 */
    time_t t_start = time(NULL);
    long budget = 900;                     /* 默认 15 分钟 */
    {
        const char *eb = getenv("AGENT_BUDGET_SEC");
        if (eb && atol(eb) > 0) budget = atol(eb);
    }
    int hard_cap = max_steps * 2;
    if (hard_cap < 8) hard_cap = 8;
    if (hard_cap > 16) hard_cap = 16;

    int last_step = 0;
    for (int step = 1; step <= hard_cap; step++) {
        last_step = step;
        if (step > max_steps) {
            long used = (long)(time(NULL) - t_start);
            if (used >= budget) {
                printf("\n⚠ 已跑 %ld 秒，超过时间预算（%ld 秒），停止续跑\n", used, budget);
                break;
            }
            /* 只提示一次，别每步都刷一长串 */
            if (step == max_steps + 1)
                printf("（已到设定 %d 步，自动续跑：最多 %d 步 / %ld 秒）\n",
                       max_steps, hard_cap, budget);
            printf("── 第 %d 步 ──\n", step);
        } else {
            printf("── 第 %d 步 ──\n", step);
        }
        MEMCHK("step-begin");

        size_t lcap = strlen(log) * 6 + 16;
        char *log_esc = malloc(lcap);
        if (!log_esc) break;
        json_escape(log, log_esc, lcap);

        char *body = malloc(BIG);
        if (!body) { free(log_esc); break; }
        snprintf(body, BIG,
            "{\"model\":\"%s\",\"messages\":["
            "{\"role\":\"system\",\"content\":\"%s\"},"
            "{\"role\":\"user\",\"content\":\"%s\"}],"
            "\"max_tokens\":%d,\"temperature\":0.3,\"stream\":false}",
            model, sys_esc, log_esc, is_pure_chat(task) ? 180 : 240);
        free(log_esc);

        time_t t0 = time(NULL);
        /* 流式：模型每吐一段就写进日志，前端轮询能看到进度。
         * 以前是一次性等完整响应，三分钟空白让人以为卡死。 */
        /* 一次性请求（非流式）。流式实测会让 llama-server 卡在写阻塞：
         * 唯一 slot 被占死、4 核满载不出结果，后面所有请求排队。
         * 前端有"运行中 XX 秒"的计时，能看出还在跑。 */
        if (getenv("AGENT_DEBUG")) {
            fprintf(stderr, "  [body] %.500s\n", body);
            fflush(stderr);
        }
        char *resp = http_post(host, port, "/v1/chat/completions", body, 300);
        if (getenv("AGENT_DEBUG")) {
            fprintf(stderr, "  [resp] %.400s\n", resp ? resp : "(NULL)");
            fflush(stderr);
        }
        char *content = resp ? extract_content(resp) : NULL;
        if (resp) free(resp);
        free(body);
        if (!content) {
            fprintf(stderr, "llama-server 无有效响应（%s:%d）\n", host, port);
            printf("\n⚠ 模型服务没响应（%s:%d）——不是任务本身失败。"
                   "多半是 llama-server 正在加载模型（装包/重启后要 10~30 秒），"
                   "等它 health 返回 ok 再重试。\n", host, port);
            break;
        }
        if (!content[0]) {
            fprintf(stderr, "响应无 content（%.0f 秒）\n", difftime(time(NULL), t0));
            free(content);
            break;
        }
        /* 流式已经逐字打过了，这里只补一行耗时标记，不要再重复整段 */
        printf("\n模型(%.0fs) 生成完毕，共 %zu 字节\n", difftime(time(NULL), t0), strlen(content));
        if (getenv("AGENT_DEBUG")) {
            fprintf(stderr, "  [content] %.400s\n", content);
            fflush(stderr);
        }

        char err2[128];
        jval *reply = json_parse(content, err2, sizeof(err2));
        /* 注意空字符串：小模型会写出 tool:"" 这种，json_str 返回非 NULL 的 "" */
        const char *ck = reply ? json_str(json_get(reply, "tool")) : NULL;
        if (!ck || !ck[0]) {
            /* 小模型把 JSON 当字符串写了一次，会多出反斜杠 / 多余引号：
             *   {"limit\":3\"}  ／  {"limit":3"}
             * 两步链式修：先去掉不该有的反斜杠，再去掉数字后面的多余引号。 */
            char *s1 = malloc(strlen(content) + 8);
            char *s2 = malloc(strlen(content) + 8);
            if (s1 && s2) {
                size_t fi = 0;
                for (const char *q = content; *q; q++) {
                    if (q[0] == '\\' && q[1] == '"') {
                        char n = q[2];
                        if (n == ':' || n == ',' || n == '}' || n == ']' || n == 0
                            || n == '\n' || n == '\r' || n == ' ') {
                            s1[fi++] = '"';
                            q++;
                            continue;
                        }
                    }
                    s1[fi++] = *q;
                }
                s1[fi] = 0;

                fi = 0;
                for (const char *q = s1; *q; q++) {
                    if (*q == '"' && q > s1) {
                        char prev = q[-1];
                        char next = q[1];
                        /* 只看数字结尾：之前加了 prev=='e' 是为了科学计数法，
                         * 结果把 ".article_title" 的收尾引号也删了（e 结尾） */
                        if (isdigit((unsigned char)prev)
                            && (next == ',' || next == '}' || next == ']'
                                || next == '\n' || next == 0)) {
                            continue;
                        }
                    }
                    s2[fi++] = *q;
                }
                s2[fi] = 0;

                jval *r1 = json_parse(s1, err2, sizeof(err2));
                if (r1 && json_str(json_get(r1, "tool")) && json_str(json_get(r1, "tool"))[0]) {
                    fprintf(stderr, "  （JSON 多余反斜杠已修）\n");
                    reply = r1;
                } else {
                    jval *r2 = json_parse(s2, err2, sizeof(err2));
                    if (r2 && json_str(json_get(r2, "tool")) && json_str(json_get(r2, "tool"))[0]) {
                        fprintf(stderr, "  （JSON 多余反斜杠+引号已修）\n");
                        reply = r2;
                    }
                }
            }
            free(s1);
            free(s2);
        }
        const char *toolp = json_str(json_get(reply, "tool"));
        char tool[64] = {0};
        if (toolp) snprintf(tool, sizeof(tool), "%s", toolp);
        if (!toolp) {
            /* 模型偶尔漏掉 tool 字段，只写 {"op":"xlsx",...}。
             * 直接把整段 JSON 当最终回答等于白干 —— 按 op 猜一次插件。 */
            const char *opv = json_str(json_get(reply, "op"));
            if (opv && *opv) {
                static const char *docops[] = {
                    "text", "xlsx", "xlsx_edit", "xlsx_read",
                    "docx", "docx_append", "pptx", "pdf", NULL
                };
                for (int di = 0; docops[di]; di++) {
                    if (!strcmp(opv, docops[di])) {
                        snprintf(tool, sizeof(tool), "doc");
                        fprintf(stderr, "  （没给 tool，按 op=%s 路由到 doc）\n", opv);
                        break;
                    }
                }
            }
            if (!tool[0]) {
                json_free(reply);
                printf("\n最终回答: %s\n", content);
                free(content);
                rc = 0;
                break;
            }
        }

        if (!strcmp(tool, "final")) {
            const char *ans = json_str(json_get(reply, "answer"));
            printf("\n最终回答: %s\n", ans ? ans : content);
            /* 模型答案里没出现插件算出的数 → 补一行，别让用户以为没算 */
            if (last_val[0] && (!ans || !strstr(ans, last_val)))
                printf("（插件算得 %s）\n", last_val);
            json_free(reply);
            free(content);
            rc = 0;
            break;
        }

        plugin_desc_t *p = find_plugin(tool);

        /* 模型选错插件：op 其实属于别的插件。
         * 典型：让 sys 执行 logread / iwinfo / uci_get —— 这些是 owrt 的 op。
         * 框架本来就有 op→插件 的映射表（直连时用），这里复用它纠正，
         * 别白费一步去报"不支持的 op"。 */
        if (p) {
            char op2[48] = {0};
            json_str_field(content, "op", op2, sizeof(op2));
            if (op2[0]) {
                const char *right = op_to_plugin(content, op2);
                if (right && strcmp(right, tool) != 0) {
                    plugin_desc_t *p2 = find_plugin(right);
                    if (p2) {
                        printf("  （%s 不是 %s 的 op，自动改投 %s 插件）\n", op2, tool, right);
                        p = p2;
                    }
                }
            }
        }

        if (!p) {
            printf("  ! 未知工具 %s\n", tool);
            log_append(log, BIG, "%s%s", "（上一轮：没有该工具，可用工具见上）\n", "");
            free(content);
            continue;
        }

        printf("  调用插件 %s…\n", tool);
        /* 注入 llama 地址：插件（比如 batch）自己要调模型时用 */
        char enr[BIG];
        size_t cl = strlen(content);
        while (cl > 0 && (content[cl - 1] == ' ' || content[cl - 1] == '\n')) cl--;

        /* math 的机械题型要靠原题文字才能识别。模型经常只给
         * {"op":"solve","equation":"..."} 不带 task，结果应用题被当成
         * 纯方程解（还可能列错式子）。这里把用户原任务补进去。
         * 纯方程题不受影响：模板匹配不上会回落到 solve。 */
        char extra[2100] = {0};
        if ((!strcmp(p->name, "math") || !strcmp(tool, "math"))
            && !strstr(content, "\"task\"") && task && task[0]) {
            snprintf(extra, sizeof(extra), ",\"task\":\"%s\"", task_esc);
        }

        if (cl > 0 && content[cl - 1] == '}') {
            snprintf(enr, sizeof(enr), "%.*s%s,\"_host\":\"%s\",\"_port\":%d,"
                     "\"_model\":\"%s\"}", (int)(cl - 1), content, extra, host, port, model);
        } else {
            snprintf(enr, sizeof(enr), "%s", content);
        }
        char *out = p->run(enr);
        free(content);
        if (!out) {
            printf("  ! 插件返回空\n");
            continue;
        }

        /* 同一个调用连续失败两次就别再试了。
         * 小模型遇到"缺少参数"这类错误会原样重发，把 step 全烧在同一个
         * 错误上（实测：grep 缺 pattern 连发 3 次，4 步一步没成）。
         * 与其耗到步数上限再报"未完成"，不如立刻停并说清缺什么。 */
        {
            static char last_sig[700];
            static char fail_tool[64];
            static int fail_cnt;
            if (strstr(out, "\"error\"")) {
                char sig[700];
                snprintf(sig, sizeof(sig), "%s|%.500s", tool, enr);
                /* 同一插件连续失败 2 次就停。小模型不只原样重发，还会换着
                 * 花样发错 op（logread 给 sys、grep 找不到 /var/log/syslog），
                 * 光比对"参数完全相同"会被它绕过去，一路烧到步数上限。 */
                if (!strcmp(fail_tool, tool)) fail_cnt++;
                else { snprintf(fail_tool, sizeof(fail_tool), "%s", tool); fail_cnt = 1; }
                if (!strcmp(sig, last_sig) || fail_cnt >= 2) {
                    char msg[400];
                    snprintf(msg, sizeof(msg), "%s", out);
                    utf8_cut(msg, 240);
                    printf("  ! 插件 %s 连续失败两次，停止重试\n", tool);
                    printf("\n最终回答: 没能完成。%s 返回的错误是：%s\n"
                           "（重复同一个调用只会得到同样的错误，需要补参数或换插件）\n",
                           tool, msg);
                    free(out);
                    rc = 0;
                    break;
                }
                snprintf(last_sig, sizeof(last_sig), "%s", sig);
            } else {
                last_sig[0] = 0;
                fail_cnt = 0;
                fail_tool[0] = 0;
            }
        }
        /* 记下完整结果：模型没给 final 时拿它收尾，别白算一场 */
        free(last_result);
        last_result = strdup(out);
        /* 记下这一轮插件算出的数值：模型 final 时若没体现，由框架补一行。
         * 3B 常把表达式原样当答案（问"根号3加2"就答 "sqrt(3)+2"），
         * 插件明明算了 3.732 却不展示，用户会以为算错了。 */
        {
            char *pv = strstr(out, "\"value\":");
            if (pv) {
                pv += 8;
                while (*pv == ' ') pv++;
                size_t k = 0;
                if (*pv != '"' && *pv != '{' && *pv != '[') {
                    while (*pv && *pv != ',' && *pv != '}' && k + 1 < sizeof(last_val))
                        last_val[k++] = *pv++;
                    last_val[k] = 0;
                }
            }
        }
        {
            char cut[BIG];
            snprintf(cut, sizeof(cut), "%s", out);
            /* 不能截太短：math 的解题过程是 markdown 表格，1000+ 字节很正常。
             * 截断会把 JSON 砍坏，前端 JSON.parse 失败就只能显示原始文本，
             * 用户看到的就是"只有结果没有过程"。 */
            utf8_cut(cut, 4000);
            printf("  [%s] %s\n", p->name, cut);
            MEMCHK("after-plugin");
        }

        /* ===== 插件联动：curl 抓到 JSON → jq 探测结构 ===== */
        {
            char *nxo = pipe_followup(p->name, out, "model");
            if (nxo) {
                char cut2[BIG];
                snprintf(cut2, sizeof(cut2), "%s", nxo);
                utf8_cut(cut2, 3000);
                printf("  [%s] %s\n", PIPES[0].to, cut2);
                free(last_result);
                last_result = strdup(nxo);
                free(nxo);
            }
        }
        log_append(log, BIG, "工具 %s 返回：\n", tool, "");
        log_append(log, BIG, "%s\n", out, "");
        free(out);
    }

    if (rc != 0) {
        /* 模型没给出 final（3B 常见：拿到插件结果不知道怎么说），
         * 但插件其实已经算出来了 → 直接用它收尾，别丢结果 */
        char *ans = NULL;
        if (last_result) {
            char err3[64];
            jval *lv = json_parse(last_result, err3, sizeof(err3));
            if (lv) {
                const char *a = json_str(json_get(lv, "answer"));
                if (!a) a = json_str(json_get(lv, "value"));
                if (a) ans = strdup(a);
                json_free(lv);
            }
        }
        if (ans) {
            printf("\n最终回答: %s\n", ans);
            free(ans);
            rc = 0;
        } else {
            printf("\n⚠ 跑到第 %d 步仍未给出最终答案。\n"
                   "（已产出的文件都保存在 /tmp 或 /tmp/llama_agent/ 下，"
                   "上面每一步的输出里能看到路径；可以让我接着做，"
                   "或者把任务拆小一点重问）\n", last_step);
        }
    }
    free(last_result);
    printf("[统计] %d 个插件 / C 框架\n", g_n);
    free(task_esc);

    free(log);
    free(tools);
    free(sys_esc);
    return rc;
}
