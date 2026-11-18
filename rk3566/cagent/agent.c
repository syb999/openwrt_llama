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
#define BIG 262144
#define SMALL 8192

static plugin_desc_t *g_plug[MAX_PLUGINS];
static int g_n = 0;

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
    const char *p = strstr(js, pat);
    if (!p) return -1;
    p = strchr(p + strlen(pat), ':');
    if (!p) return -1;
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
        close(fd);
        return NULL;
    }
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
    if (!ok) { close(fd); return NULL; }

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

    /* 分两组：runtimes = 底层运行时（谁都得有），libs = 专用库（特定插件才用） */
    size_t off = (size_t)snprintf(out, cap,
        "{\"runtimes\":{\"python3\":%d,\"node\":%d,\"ffmpeg\":%d},"
        "\"libs\":{\"sympy\":%d,\"midiutil\":%d,\"pypdf\":%d,\"docx\":%d,"
        "\"openpyxl\":%d,\"pptx\":%d},\"plugins\":[",
        py, bits[1] == '1', bits[2] == '1', f_sympy, f_midi,
        f_pypdf, f_docx, f_xlsx, f_pptx);
    for (int i = 0; i < g_n && off < cap - 320; i++) {
        off += (size_t)snprintf(out + off, cap - off,
            "%s{\"name\":\"%s\",\"desc\":\"%s\",\"kind\":\"tool\"}",
            i ? "," : "", g_plug[i]->name, g_plug[i]->description);
    }
    for (int i = 0; i < g_nauto && off < cap - 400; i++) {
        pid_t apid = 0;
        int running = auto_state(g_auto[i]->name, &apid);
        off += (size_t)snprintf(out + off, cap - off,
            "%s{\"name\":\"%s\",\"desc\":\"%s\",\"kind\":\"auto\",\"triggers\":\"%s\","
            "\"running\":%d,\"pid\":%d}",
            (g_n + i) ? "," : "", g_auto[i]->name, g_auto[i]->description,
            g_auto[i]->triggers ? g_auto[i]->triggers : "manual",
            running ? 1 : 0, running ? (int)apid : 0);
    }
    snprintf(out + off, cap - off, "]}");
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

static char *tools_prompt(void) {
    char *s = malloc(SMALL);
    if (!s) return NULL;
    size_t off = 0;
    off += (size_t)snprintf(s + off, SMALL - off, "可用工具：\n");
    for (int i = 0; i < g_n && off < SMALL - 300; i++) {
        off += (size_t)snprintf(s + off, SMALL - off, "- %s：%s  参数 %s\n",
                                g_plug[i]->name, g_plug[i]->description,
                                g_plug[i]->params ? g_plug[i]->params : "{}");
    }
    off += (size_t)snprintf(s + off, SMALL - off,
        "- final：最终答案  参数 {\"answer\":\"...\"}\n");
    return s;
}

/* 往对话记录里追加一行（结果回灌） */
static void log_append(char *log, size_t cap, const char *fmt, const char *a, const char *b) {
    size_t off = strlen(log);
    if (off + 2048 >= cap) return;
    snprintf(log + off, cap - off, fmt, a, b);
}

/* ============ main ============ */
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

    reload_plugins(plugdir);   /* 每次启动重扫：新增/删除 .so 都生效，不用改框架 */
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
        char envbuf[4096];
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

    char *tools = tools_prompt();
    if (!tools) return 1;

    /* system prompt */
    char sys_raw[BIG];
    snprintf(sys_raw, sizeof(sys_raw),
        "你只能输出一个 JSON 对象，不要解释、不要代码块。\n%s"
        "选插件：{\"tool\":\"插件名\",...参数}；答完：{\"tool\":\"final\",\"answer\":\"...\"}\n"
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
    if (!log) { free(sys_esc); free(tools); return 1; }
    size_t tcap = strlen(task) * 6 + 16;
    char *task_esc = malloc(tcap);
    if (!task_esc) { free(log); free(sys_esc); free(tools); return 1; }
    json_escape(task, task_esc, tcap);
    snprintf(log, BIG, "任务：%s\n", task_esc);
    /* task_esc 不在这里释放：下面调 math 时要用它补 task 字段 */

    printf("任务: %s\n插件: %d 个\n", task, g_n);

    int rc = 2;
    /* 插件返回的数值：模型 final 时如果没体现出来，由框架补一行。
     * 3B 经常把表达式原样当答案（问"根号3加2"就答 "sqrt(3)+2"），
     * 插件明明算出了 3.732 却不展示，用户会以为算错了。 */
    char last_val[64] = {0};
    char *last_result = NULL;      /* 最后一次插件返回的 JSON，兜底收尾用 */
    for (int step = 1; step <= max_steps; step++) {
        printf("── 第 %d 步 ──\n", step);
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
            "\"max_tokens\":512,\"temperature\":0.3,\"stream\":false}",
            model, sys_esc, log_esc);
        free(log_esc);

        time_t t0 = time(NULL);
        char *resp = http_post(host, port, "/v1/chat/completions", body, 900);
        free(body);
        if (!resp) {
            fprintf(stderr, "连不上 llama-server（%s:%d）\n", host, port);
            break;
        }
        char *content = extract_content(resp);
        if (!content) {
            fprintf(stderr, "响应无 content：%.300s\n", resp);
            free(content);
            free(resp);
            break;
        }
        free(resp);
        {
            char cut[BIG];
            snprintf(cut, sizeof(cut), "%s", content);
            utf8_cut(cut, 4000);
            printf("模型(%.0fs): %s\n", difftime(time(NULL), t0), cut);
        }

        char err2[128];
        jval *reply = json_parse(content, err2, sizeof(err2));
        const char *toolp = json_str(json_get(reply, "tool"));
        char tool[64] = {0};
        if (toolp) snprintf(tool, sizeof(tool), "%s", toolp);
        if (!toolp) {
            json_free(reply);
            printf("\n最终回答: %s\n", content);
            free(content);
            rc = 0;
            break;
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
            printf("\n⚠ 达到 %d 步上限，未完成\n", max_steps);
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
