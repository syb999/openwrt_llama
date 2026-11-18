#define _GNU_SOURCE
/* cgi.c — agent 的 HTTP 接口层（纯 C，去掉 python 版 CGI）
 *
 * 路由（和 python 版行为一致，页面不用改）：
 *   POST ?task       {"task":"..."}            → 起 agent-c 子进程，返回 job id
 *   GET  ?job=xxx                               → 读日志（done 靠 "[统计]" 判断）
 *   GET  ?env=1                                 → 环境能力 + 插件清单
 *   GET  ?file=xxx                              → 下载生成物
 *   POST ?upload=1   {"name":"x.pdf","data":"base64"} → 存文件 + 转文本
 *
 * 任务按日期分目录：/tmp/agent-jobs/YYYYMMDD/<jid>.log
 * 轮询时跨日查找（今天→昨天→保留期），23:59 起、00:01 查也能找到。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
#include <dirent.h>
#include <errno.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/types.h>
#include <strings.h>
#include <ctype.h>

#define JOBS        "/tmp/agent-jobs"
#define WORK_DIR    "/tmp/llama_agent"
#define AGENT_C     "/opt/llama/agent/agent-c"
#define PLUGIN_DIR  "/opt/llama/agent/plugins"
#define KEEP_DAYS   3
#define MAX_BODY    (24 * 1024 * 1024)
#define MAX_OUTPUT  6000

/* ---------------- 输出 ---------------- */
static void out_json(const char *fmt, ...);

/* 直接吐一段已经成形的 JSON（比如从 agent-c 代理过来的结果） */
static void out_raw(const char *json) {
    if (!json) json = "{}";
    printf("Content-Type: application/json; charset=utf-8\r\n");
    printf("Cache-Control: no-store\r\n");
    printf("Content-Length: %zu\r\n\r\n%s", strlen(json), json);
    fflush(stdout);
}

#include <stdarg.h>
static void out_json(const char *fmt, ...) {
    char *body = malloc(MAX_OUTPUT + 4096);
    if (!body) return;
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(body, MAX_OUTPUT + 4096, fmt, ap);
    va_end(ap);
    if (n < 0) n = 0;
    printf("Content-Type: application/json; charset=utf-8\r\n");
    printf("Cache-Control: no-store\r\n");
    printf("Access-Control-Allow-Origin: *\r\n");
    printf("Content-Length: %d\r\n\r\n", n);
    fwrite(body, 1, (size_t)n, stdout);
    fflush(stdout);
    free(body);
}

/* 转义成 JSON 字符串内容（写进 buf） */
static size_t jesc(char *dst, size_t cap, const char *src) {
    size_t i = 0;
    for (const unsigned char *p = (const unsigned char *)src; *p; p++) {
        if (i + 8 >= cap) break;
        switch (*p) {
        case '"':  dst[i++] = '\\'; dst[i++] = '"';  break;
        case '\\': dst[i++] = '\\'; dst[i++] = '\\'; break;
        case '\n': dst[i++] = '\\'; dst[i++] = 'n';  break;
        case '\r': dst[i++] = '\\'; dst[i++] = 'r';  break;
        case '\t': dst[i++] = '\\'; dst[i++] = 't';  break;
        default:
            if (*p < 0x20) i += (size_t)snprintf(dst + i, cap - i, "\\u%04x", *p);
            else dst[i++] = (char)*p;
        }
    }
    dst[i] = 0;
    return i;
}

/* ---------------- 读请求体 ---------------- */
static char *read_body(size_t *len) {
    const char *cl = getenv("CONTENT_LENGTH");
    long n = cl ? atol(cl) : 0;
    if (n <= 0 || n > MAX_BODY) { *len = 0; return NULL; }
    char *buf = malloc((size_t)n + 1);
    if (!buf) { *len = 0; return NULL; }
    size_t got = fread(buf, 1, (size_t)n, stdin);
    buf[got] = 0;
    *len = got;
    return buf;
}

/* 从扁平 JSON 取字符串 */
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
        if (*p == '\\' && p[1]) {
            p++;
            switch (*p) {
            case 'n': out[i++] = '\n'; break;
            case 'r': out[i++] = '\r'; break;
            case 't': out[i++] = '\t'; break;
            case '"': out[i++] = '"'; break;
            case '\\': out[i++] = '\\'; break;
            case '/': out[i++] = '/'; break;
            default: out[i++] = *p; break;
            }
            p++;
        } else out[i++] = *p++;
    }
    out[i] = 0;
    return 0;
}

/* ---------------- 小工具 ---------------- */
static void today_str(char *buf, size_t cap, int delta_days) {
    time_t t = time(NULL) - (time_t)delta_days * 86400;
    struct tm tm;
    localtime_r(&t, &tm);
    strftime(buf, cap, "%Y%m%d", &tm);
}

static int mkdir_p(const char *path) {
    char tmp[512];
    snprintf(tmp, sizeof(tmp), "%s", path);
    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/') { *p = 0; mkdir(tmp, 0755); *p = '/'; }
    }
    return mkdir(tmp, 0755) == 0 || errno == EEXIST ? 0 : -1;
}

/* URL 解码：中文文件名在 query 里是 %E4%B8%AD 这种，不解码就找不到文件 */
static void url_decode(char *s) {
    char *o = s;
    for (char *p = s; *p; p++) {
        if (*p == '%' && p[1] && p[2]
            && isxdigit((unsigned char)p[1]) && isxdigit((unsigned char)p[2])) {
            char hex[3] = { p[1], p[2], 0 };
            *o++ = (char)strtol(hex, NULL, 16);
            p += 2;
        } else if (*p == '+') {
            *o++ = ' ';
        } else {
            *o++ = *p;
        }
    }
    *o = 0;
}

/* 按 job id 找日志（跨日） */
static int find_job(const char *jid, const char *ext, char *out, size_t cap) {
    for (int d = 0; d <= KEEP_DAYS; d++) {
        char day[16];
        today_str(day, sizeof(day), d);
        snprintf(out, cap, "%s/%s/%s%s", JOBS, day, jid, ext);
        if (access(out, F_OK) == 0) return 0;
    }
    return -1;
}

/* 清理：删超过保留期的日期目录 + 旧格式平铺文件 */
static void prune_jobs(void) {
    DIR *dp = opendir(JOBS);
    if (!dp) return;
    struct dirent *e;
    char today[16];
    today_str(today, sizeof(today), 0);
    while ((e = readdir(dp)) != NULL) {
        if (e->d_name[0] == '.') continue;
        char path[512];
        snprintf(path, sizeof(path), "%s/%s", JOBS, e->d_name);
        struct stat st;
        if (stat(path, &st) != 0) continue;

        if (S_ISREG(st.st_mode)) {          /* 旧格式平铺 → 删 */
            size_t L = strlen(e->d_name);
            if ((L > 4 && !strcmp(e->d_name + L - 4, ".log"))
                || (L > 5 && !strcmp(e->d_name + L - 5, ".task"))) {
                unlink(path);
            }
            continue;
        }
        if (!S_ISDIR(st.st_mode) || strlen(e->d_name) != 8) continue;
        if (strcmp(e->d_name, today) >= 0) continue;   /* 今天及以后不动 */
        /* 目录名是 yyyymmdd：算天数差 */
        struct tm tm;
        memset(&tm, 0, sizeof(tm));
        if (strptime(e->d_name, "%Y%m%d", &tm) == NULL) continue;
        time_t dt = mktime(&tm);
        if (dt <= 0) continue;
        if ((time(NULL) - dt) / 86400 > KEEP_DAYS) {
            char cmd[600];
            snprintf(cmd, sizeof(cmd), "rm -rf '%s'", path);
            if (system(cmd) != 0) { /* 忽略 */ }
        }
    }
    closedir(dp);
}

/* ---------------- 起任务 ---------------- */
static int job_running(pid_t *pid_out) {
    FILE *f = fopen(JOBS "/.running", "r");
    if (!f) return 0;
    int pid = 0;
    if (fscanf(f, "%d", &pid) != 1) { fclose(f); return 0; }
    fclose(f);
    if (pid <= 0) return 0;
    if (kill((pid_t)pid, 0) == 0) { if (pid_out) *pid_out = (pid_t)pid; return 1; }
    return 0;
}

static void do_task(const char *body) {
    char task[8192] = {0};
    if (jget(body, "task", task, sizeof(task)) != 0 || !task[0]) {
        out_json("{\"ok\":0,\"error\":\"任务为空\"}");
        return;
    }
    if (access(AGENT_C, X_OK) != 0) {
        out_json("{\"ok\":0,\"error\":\"agent-c 不在 %s\"}", AGENT_C);
        return;
    }
    pid_t busy = 0;
    if (job_running(&busy)) {
        out_json("{\"ok\":0,\"error\":\"已有 agent 任务在跑（pid %d），llama-server 单 slot 并发只会互相排队\"}",
                 (int)busy);
        return;
    }

    mkdir_p(JOBS);
    char day[16], daydir[512];
    today_str(day, sizeof(day), 0);
    snprintf(daydir, sizeof(daydir), "%s/%s", JOBS, day);
    mkdir_p(daydir);

    unsigned int r = 0;
    int fd = open("/dev/urandom", O_RDONLY);
    if (fd >= 0) { if (read(fd, &r, sizeof(r)) != sizeof(r)) r = (unsigned)time(NULL); close(fd); }
    else r = (unsigned)time(NULL);
    char jid[16];
    snprintf(jid, sizeof(jid), "%08x", r);

    char taskf[600], logf[600];
    snprintf(taskf, sizeof(taskf), "%s/%s.task", daydir, jid);
    snprintf(logf, sizeof(logf), "%s/%s.log", daydir, jid);

    FILE *tf = fopen(taskf, "w");
    if (!tf) { out_json("{\"ok\":0,\"error\":\"写任务文件失败\"}"); return; }
    fputs(task, tf);
    fclose(tf);

    pid_t pid = fork();
    if (pid < 0) { out_json("{\"ok\":0,\"error\":\"fork 失败\"}"); return; }
    if (pid == 0) {
        /* 子进程：stdout/stderr 都进日志 */
        int lf = open(logf, O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (lf >= 0) { dup2(lf, 1); dup2(lf, 2); if (lf > 2) close(lf); }
        int nf = open("/dev/null", O_RDONLY);
        if (nf >= 0) { dup2(nf, 0); if (nf > 2) close(nf); }
        char steps[16];
        snprintf(steps, sizeof(steps), "%d", 4);
        /* 记账开关：默认开，AGENT_MEMCHECK=0 关。
         * 每次任务在日志里留几行 [mem]，攒起来能看出同一类任务的分配是否漂移。 */
        const char *mc = getenv("AGENT_MEMCHECK");
        int memcheck = !(mc && !strcmp(mc, "0"));
        if (memcheck) {
            execl(AGENT_C, AGENT_C, "--task", task, "--plugins", PLUGIN_DIR,
                  "--max-steps", steps, "--memcheck", (char *)NULL);
        } else {
            execl(AGENT_C, AGENT_C, "--task", task, "--plugins", PLUGIN_DIR,
                  "--max-steps", steps, (char *)NULL);
        }
        _exit(127);
    }
    /* 父进程记录 pid（供互斥判断） */
    FILE *rf = fopen(JOBS "/.running", "w");
    if (rf) { fprintf(rf, "%d\n", (int)pid); fclose(rf); }

    out_json("{\"ok\":1,\"job\":\"%s\",\"task\":\"%s\",\"pid\":%d}", jid, "", (int)pid);
}

/* ---------------- 轮询 ---------------- */
static void do_poll(const char *jid) {
    char logf[600];
    if (find_job(jid, ".log", logf, sizeof(logf)) != 0) {
        out_json("{\"ok\":0,\"error\":\"任务不存在或已过期（保留 %d 天）\"}", KEEP_DAYS);
        return;
    }
    FILE *f = fopen(logf, "r");
    if (!f) { out_json("{\"ok\":0,\"error\":\"读日志失败\"}"); return; }
    char *txt = malloc(1 << 20);
    if (!txt) { fclose(f); out_json("{\"ok\":0,\"error\":\"内存不足\"}"); return; }
    size_t n = fread(txt, 1, (1 << 20) - 1, f);
    txt[n] = 0;
    fclose(f);

    /* 完成判定：agent 任务会打 [统计]；自动化插件不一定打，
     * 所以再加一条——进程已经退出就算完成。
     * 否则自动化流程跑完了页面还在转圈。 */
    int done = strstr(txt, "[统计]") != NULL;
    if (!done) {
        pid_t alive = 0;
        if (!job_running(&alive)) done = 1;
    }
    const char *tail = txt;
    if (n > MAX_OUTPUT) {
        tail = txt + n - MAX_OUTPUT;
        /* 退到 UTF-8 字符边界，否则开头会露出半个汉字（显示成 "�"） */
        while (*tail && ((unsigned char)*tail & 0xC0) == 0x80) tail++;
    }
    char *esc = malloc(strlen(tail) * 6 + 64);
    if (!esc) { free(txt); out_json("{\"ok\":0,\"error\":\"内存不足\"}"); return; }
    jesc(esc, strlen(tail) * 6 + 64, tail);
    out_json("{\"ok\":1,\"done\":%s,\"output\":\"%s\"}", done ? "true" : "false", esc);
    free(esc);
    free(txt);
}

/* ---------------- 环境 ---------------- */
static void do_env(void) {
    char buf[8192] = {0};
    FILE *p = popen(AGENT_C " --env --plugins " PLUGIN_DIR " 2>/dev/null", "r");
    if (!p) { out_json("{\"ok\":0,\"error\":\"agent-c 执行失败\"}"); return; }
    size_t n = fread(buf, 1, sizeof(buf) - 1, p);
    buf[n] = 0;
    pclose(p);
    /* agent-c 会先打插件加载信息（stderr），stdout 是 JSON 单行 */
    char *line = NULL, *save = NULL;
    for (char *t = strtok_r(buf, "\n", &save); t; t = strtok_r(NULL, "\n", &save)) {
        if (t[0] == '{') line = t;
    }
    if (!line) { out_json("{\"ok\":0,\"error\":\"环境信息解析失败\"}"); return; }
    out_json("{\"ok\":1,\"env\":%s}", line);
}

/* ---------------- 启动自动化插件 ---------------- */
/* 和提交任务同样的 job 机制：fork 一个 agent-c --auto，日志进 job 文件，
 * 前端用同一个 ?job= 轮询进度。自动化插件自己在 stdout 打印进度。 */
static void do_auto(const char *name, const char *args_json, int daemon_mode) {
    if (!name || !name[0]) { out_json("{\"ok\":0,\"error\":\"缺少 auto 名称\"}"); return; }
    for (const char *p = name; *p; p++) {
        if (!isalnum((unsigned char)*p) && *p != '-' && *p != '_') {
            out_json("{\"ok\":0,\"error\":\"auto 名称不合法\"}");
            return;
        }
    }
    if (!args_json || !args_json[0]) args_json = "{}";

    /* 已经在跑就直接回"已在运行"：必须在 fork 之前判断，
     * 否则会起出第二个进程，把 pid 文件覆盖掉，老的变孤儿还在后台转。 */
    {
        char pf[256];
        snprintf(pf, sizeof(pf), "/tmp/agent-auto/%s.pid", name);
        FILE *f = fopen(pf, "r");
        if (f) {
            int old = 0;
            int ok = (fscanf(f, "%d", &old) == 1);
            fclose(f);
            if (ok && old > 0 && kill((pid_t)old, 0) == 0) {
                out_json("{\"ok\":1,\"already\":1,\"running\":1,\"pid\":%d,"
                         "\"note\":\"%s 已经在运行（pid %d）\"}", old, name, old);
                return;
            }
        }
    }

    pid_t busy = 0;
    if (!daemon_mode && job_running(&busy)) {
        out_json("{\"ok\":0,\"error\":\"已有任务在跑（pid %d）\"}", (int)busy);
        return;
    }
    if (access(AGENT_C, X_OK) != 0) {
        out_json("{\"ok\":0,\"error\":\"agent-c 不可执行\"}");
        return;
    }

    mkdir_p(JOBS);
    char day[16], daydir[512];
    today_str(day, sizeof(day), 0);
    snprintf(daydir, sizeof(daydir), "%s/%s", JOBS, day);
    mkdir_p(daydir);

    unsigned int r = 0;
    int fd = open("/dev/urandom", O_RDONLY);
    if (fd >= 0) { if (read(fd, &r, sizeof(r)) != sizeof(r)) r = (unsigned)time(NULL); close(fd); }
    else r = (unsigned)time(NULL);
    char jid[16];
    snprintf(jid, sizeof(jid), "%08x", r);

    char logf[600];
    snprintf(logf, sizeof(logf), "%s/%s.log", daydir, jid);

    pid_t pid = fork();
    if (pid < 0) { out_json("{\"ok\":0,\"error\":\"fork 失败\"}"); return; }
    if (pid == 0) {
        /* 守护模式脱离 CGI 会话：CGI 进程退出后它继续跑，
         * 不受 uhttpd 的请求生命周期影响 */
        if (daemon_mode) setsid();
        int lf = open(logf, O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (lf >= 0) { dup2(lf, 1); dup2(lf, 2); if (lf > 2) close(lf); }
        int nf = open("/dev/null", O_RDONLY);
        if (nf >= 0) { dup2(nf, 0); if (nf > 2) close(nf); }
        if (daemon_mode)
            execl(AGENT_C, AGENT_C, "--auto", name, "--args", args_json, "--daemon",
                  "--plugins", PLUGIN_DIR, (char *)NULL);
        else
            execl(AGENT_C, AGENT_C, "--auto", name, "--args", args_json,
                  "--plugins", PLUGIN_DIR, (char *)NULL);
        _exit(127);
    }
    /* 守护模式不占任务互斥锁：它长期在跑，不该把页面锁成"忙碌"、
     * 也不该让用户提交不了普通任务。它自己的 pid 在 /tmp/agent-auto/ */
    if (!daemon_mode) {
        FILE *rf = fopen(JOBS "/.running", "w");
        if (rf) { fprintf(rf, "%d\n", (int)pid); fclose(rf); }
    }
    out_json("{\"ok\":1,\"job\":\"%s\",\"auto\":\"%s\",\"pid\":%d,\"daemon\":%d}",
             jid, name, (int)pid, daemon_mode ? 1 : 0);
}

/* ---------------- 停止自动化 ---------------- */
static void do_auto_stop(const char *name) {
    if (!name || !name[0]) { out_json("{\"ok\":0,\"error\":\"缺少 auto 名称\"}"); return; }
    for (const char *p = name; *p; p++) {
        if (!isalnum((unsigned char)*p) && *p != '-' && *p != '_') {
            out_json("{\"ok\":0,\"error\":\"auto 名称不合法\"}");
            return;
        }
    }
    char cmd[600];
    snprintf(cmd, sizeof(cmd), AGENT_C " --auto-stop '%s' --plugins " PLUGIN_DIR " 2>/dev/null", name);
    char buf[1024] = {0};
    FILE *p = popen(cmd, "r");
    if (p) { size_t n = fread(buf, 1, sizeof(buf) - 1, p); buf[n] = 0; pclose(p); }
    char *line = NULL, *save = NULL, *last = NULL;
    for (char *t = strtok_r(buf, "\n", &save); t; t = strtok_r(NULL, "\n", &save))
        if (t[0] == '{') last = t;
    if (last) out_raw(last);
    else out_json("{\"ok\":0,\"error\":\"停止失败\"}");
}

/* ---------------- 忙闲状态 ---------------- */
/* 扫 /proc 找真正在跑的 agent-c。
 * 为什么不能只看 .running：只有 CGI 提交任务才写那个文件，
 * 从命令行直接跑 agent-c --task 时不写 → 明明在跑，页面却显示空闲。 */
static int find_agent_proc(pid_t *out) {
    DIR *d = opendir("/proc");
    if (!d) return 0;
    struct dirent *e;
    pid_t self = getpid();
    int found = 0;
    while ((e = readdir(d)) != NULL) {
        if (!isdigit((unsigned char)e->d_name[0])) continue;
        pid_t p = (pid_t)atoi(e->d_name);
        if (p == self) continue;
        char path[64], buf[1024];
        snprintf(path, sizeof(path), "/proc/%d/cmdline", (int)p);
        int fd = open(path, O_RDONLY);
        if (fd < 0) continue;
        ssize_t n = read(fd, buf, sizeof(buf) - 1);
        close(fd);
        if (n <= 0) continue;
        buf[n] = 0;
        /* 先按 argv[0] 的 basename 判断：命令行直接跑的叫 agent，
         * CGI 起的是 agent-c，两个都要认。用 basename 比 strstr 准，
         * 否则 "grep agent" 之类的进程也会被误判成忙。 */
        {
            const char *b = strrchr(buf, '/');
            b = b ? b + 1 : buf;
            if (strcmp(b, "agent") != 0 && strcmp(b, "agent-c") != 0) continue;
        }
        for (ssize_t i = 0; i < n; i++) if (buf[i] == '\0') buf[i] = ' ';
        if (strstr(buf, "--env") || strstr(buf, "--auto-list")) continue;  /* 查询类，不算忙 */
        if (strstr(buf, "--daemon")) continue;   /* 后台守护，不是用户等的任务 */
        if (out) *out = p;
        found = 1;
        break;
    }
    closedir(d);
    return found;
}

/* 页面左上角那个（忙碌）/（空闲）就是问这个 */
static void do_status(void) {
    pid_t pid = 0;
    int busy = job_running(&pid);            /* 先看 .running（CGI 提交的任务） */
    if (!busy) busy = find_agent_proc(&pid); /* 再看真实进程（命令行直接跑的） */
    out_json("{\"ok\":1,\"busy\":%d,\"pid\":%d}", busy ? 1 : 0, busy ? (int)pid : 0);
}

/* ---------------- base64 解码 ---------------- */
static int b64d(const char *in, unsigned char *out, size_t cap) {
    static signed char tab[256];
    static int init = 0;
    if (!init) {
        for (int i = 0; i < 256; i++) tab[i] = -1;
        const char *a = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        for (int i = 0; i < 64; i++) tab[(unsigned char)a[i]] = (signed char)i;
        init = 1;
    }
    size_t n = 0;
    int acc = 0, bits = 0;
    for (const unsigned char *p = (const unsigned char *)in; *p && *p != '"'; p++) {
        int v = tab[*p];
        if (v < 0) continue;
        acc = (acc << 6) | v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            if (n < cap) out[n++] = (unsigned char)((acc >> bits) & 0xFF);
        }
    }
    return (int)n;
}

/* ---------------- 上传：存原文件 + 转文本 ---------------- */
/* ---------- 上传能力分层 ----------
 * 内置只认纯文本，完全不依赖任何外部库。
 * PDF/Word/Excel/PPT 属于"额外能力"：要 doc 插件装好，并且它依赖的
 * python 库也在。缺哪一层就说清缺哪一层，不要笼统报"上传失败"。 */
static int is_plain_text_ext(const char *ext) {
    static const char *e[] = {
        ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log",
        ".yaml", ".yml", ".xml", ".ini", ".conf", ".cfg", ".env",
        ".sh", ".py", ".c", ".h", ".js", ".css", ".html", ".htm",
        ".sql", ".m3u", ".m3u8", ".list", ".toml", ".properties", NULL
    };
    if (!ext) return 1;                        /* 没扩展名，当文本试 */
    for (int i = 0; e[i]; i++)
        if (!strcasecmp(ext, e[i])) return 1;
    return 0;
}

/* 扩展名 → 需要的 python 库；NULL 表示这个格式根本不支持 */
static const char *lib_for_ext(const char *ext) {
    if (!ext) return NULL;
    if (!strcasecmp(ext, ".pdf"))  return "pypdf";
    if (!strcasecmp(ext, ".docx")) return "docx";
    if (!strcasecmp(ext, ".xlsx") || !strcasecmp(ext, ".xlsm")) return "openpyxl";
    if (!strcasecmp(ext, ".pptx")) return "pptx";
    return NULL;
}

/* 库在不在：查 site-packages 目录，不起 python 进程 */
static int cgi_have_pymod(const char *mod) {
    static const char *dirs[] = {
        "/usr/lib/python3.13/site-packages", "/usr/lib/python3.12/site-packages",
        "/usr/lib/python3.11/site-packages", "/usr/lib/python3.10/site-packages",
        "/usr/lib/python3.9/site-packages",  "/usr/lib/python3.8/site-packages",
        "/usr/lib/python3/dist-packages",    "/usr/lib/python3/site-packages",
        "/usr/lib/python3.9/dist-packages",  "/usr/lib/python3.10/dist-packages",
        "/usr/lib/python3.11/dist-packages",
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
        snprintf(p, sizeof(p), "%s/%s.py", dirs[i], mod);
        if (access(p, F_OK) == 0) return 1;
    }
    return 0;
}

static void do_upload(const char *body) {
    char name[256] = {0};
    if (jget(body, "name", name, sizeof(name)) != 0 || !name[0]) {
        out_json("{\"ok\":0,\"error\":\"缺少文件名\"}");
        return;
    }
    const char *dp = strstr(body, "\"data\"");
    if (!dp) { out_json("{\"ok\":0,\"error\":\"缺少 data\"}"); return; }
    dp = strchr(dp, ':');
    if (!dp) { out_json("{\"ok\":0,\"error\":\"data 格式错\"}"); return; }
    dp = strchr(dp, '"');
    if (!dp) { out_json("{\"ok\":0,\"error\":\"data 格式错\"}"); return; }
    dp++;

    size_t cap = strlen(dp) / 4 * 3 + 16;
    unsigned char *raw = malloc(cap);
    if (!raw) { out_json("{\"ok\":0,\"error\":\"内存不足\"}"); return; }
    int rn = b64d(dp, raw, cap);
    if (rn <= 0) { free(raw); out_json("{\"ok\":0,\"error\":\"base64 解码失败\"}"); return; }

    char updir[512], path[768];
    snprintf(updir, sizeof(updir), "%s/uploads", WORK_DIR);
    mkdir_p(updir);
    snprintf(path, sizeof(path), "%s/%s", updir, name);

    FILE *f = fopen(path, "wb");
    if (!f) { free(raw); out_json("{\"ok\":0,\"error\":\"写文件失败\"}"); return; }
    fwrite(raw, 1, (size_t)rn, f);
    fclose(f);
    free(raw);

    /* 转文本：交给 doc 插件（它自己会报缺库），没有 doc 插件就直说 */
    char txtpath[800], esc_txt[4096];
    snprintf(txtpath, sizeof(txtpath), "%s.txt", path);
    snprintf(esc_txt, sizeof(esc_txt), "（未转换）");

    const char *dot = strrchr(name, '.');

    /* 第一层：内置纯文本。不需要任何插件、任何库，直接当文本用。 */
    if (is_plain_text_ext(dot)) {
        char esc_path0[2048], esc_name0[1024];
        jesc(esc_path0, sizeof(esc_path0), path);
        jesc(esc_name0, sizeof(esc_name0), name);
        /* 字段名要跟页面一致：页面读 j.name / j.file / j.chars */
        out_json("{\"ok\":1,\"name\":\"%s\",\"file\":\"%s\",\"path\":\"%s\","
                 "\"chars\":%d,\"bytes\":%d,\"kind\":\"text\",\"builtin\":1}",
                 esc_name0, esc_path0, esc_path0, rn, rn);
        return;
    }

    /* 第二层：二进制文档。必须有 doc 插件，且它依赖的库也在。 */
    const char *need = lib_for_ext(dot);
    if (!need) {
        unlink(path);
        char esc_ext[64] = {0};
        jesc(esc_ext, sizeof(esc_ext), dot ? dot : "");
        out_json("{\"ok\":0,\"error\":\"不支持 %s 这种格式。内置只支持纯文本"
                 "（txt/md/csv/json/log/yaml/xml/代码等）；"
                 "PDF/Word/Excel/PPT 需要 doc 插件\"}", esc_ext);
        return;
    }
    if (access("/opt/llama/agent/plugins/doc.so", R_OK) != 0) {
        unlink(path);
        out_json("{\"ok\":0,\"error\":\"不支持 %s：没装 doc 插件。"
                 "纯文本可以直接传；要解析这类文档请先装上 doc 插件\"}",
                 dot);
        return;
    }
    if (!cgi_have_pymod(need)) {
        unlink(path);
        out_json("{\"ok\":0,\"error\":\"不支持 %s：doc 插件已装，但缺 Python 库 %s。"
                 "板上装：pip install %s\"}", dot, need, need);
        return;
    }

    char *txout = malloc(1 << 20);
    if (txout) {
        char cmd[1200];
        snprintf(cmd, sizeof(cmd),
            "python3 /opt/llama/agent/extract.py '%s' 2>/dev/null", path);
        FILE *tp = popen(cmd, "r");
        if (tp) {
            size_t tn = fread(txout, 1, (1 << 20) - 1, tp);
            txout[tn] = 0;
            pclose(tp);
            if (tn > 0) {
                FILE *of = fopen(txtpath, "w");
                if (of) { fwrite(txout, 1, tn, of); fclose(of); }
                size_t ep = snprintf(esc_txt, sizeof(esc_txt), "%s", txtpath);
                (void)ep;
            }
        }
        free(txout);
    }

    /* 输出：字段名跟页面一致（name/file/chars），并标明这次是插件解出来的 */
    char esc_path[2048], esc_txtp[2048], esc_name[1024], esc_ext[64] = {0};
    jesc(esc_path, sizeof(esc_path), path);
    jesc(esc_txtp, sizeof(esc_txtp), txtpath);
    jesc(esc_name, sizeof(esc_name), name);
    if (dot) jesc(esc_ext, sizeof(esc_ext), dot + 1);
    int chars = (int)strlen(esc_txt);      /* 转出来的文本长度（字节） */
    out_json("{\"ok\":1,\"name\":\"%s\",\"file\":\"%s\",\"path\":\"%s\","
             "\"chars\":%d,\"bytes\":%d,\"kind\":\"%s\",\"plugin\":\"doc\"}",
             esc_name, esc_txtp, esc_path, chars, rn, esc_ext);
}

/* ---------------- 下载 ---------------- */
static void do_file(const char *fname) {
    /* 只取文件名，防目录穿越 */
    const char *base = strrchr(fname, '/');
    base = base ? base + 1 : fname;

    char cand[8][700];
    int nc = 0;
    for (int d = 0; d <= KEEP_DAYS; d++) {
        char day[16];
        today_str(day, sizeof(day), d);
        snprintf(cand[nc++], 700, "%s/%s/%s", WORK_DIR, day, base);
    }
    snprintf(cand[nc++], 700, "%s/%s", WORK_DIR, base);
    snprintf(cand[nc++], 700, "%s/uploads/%s", WORK_DIR, base);

    const char *hit = NULL;
    for (int i = 0; i < nc; i++) {
        if (access(cand[i], R_OK) == 0) { hit = cand[i]; break; }
    }
    if (!hit) {
        out_json("{\"ok\":0,\"error\":\"文件不存在: %s\"}", base);
        return;
    }

    const char *ext = strrchr(base, '.');
    const char *ct = "application/octet-stream";
    if (ext) {
        if (!strcasecmp(ext, ".mid") || !strcasecmp(ext, ".midi")) ct = "audio/midi";
        else if (!strcasecmp(ext, ".mp3")) ct = "audio/mpeg";
        else if (!strcasecmp(ext, ".wav")) ct = "audio/wav";
        else if (!strcasecmp(ext, ".pptx")) ct = "application/vnd.openxmlformats-officedocument.presentationml.presentation";
        else if (!strcasecmp(ext, ".txt") || !strcasecmp(ext, ".md") || !strcasecmp(ext, ".py"))
            ct = "text/plain; charset=utf-8";
    }
    struct stat st;
    if (stat(hit, &st) != 0) { out_json("{\"ok\":0,\"error\":\"stat 失败\"}"); return; }
    printf("Content-Type: %s\r\n", ct);
    printf("Content-Disposition: attachment; filename=\"%s\"\r\n", base);
    printf("Content-Length: %lld\r\n", (long long)st.st_size);
    printf("Cache-Control: no-store\r\n\r\n");
    fflush(stdout);
    FILE *f = fopen(hit, "rb");
    if (!f) return;
    char buf[8192];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0) fwrite(buf, 1, n, stdout);
    fclose(f);
    fflush(stdout);
}

/* ---------------- main ---------------- */
int main(void) {
    const char *method = getenv("REQUEST_METHOD");
    const char *qs = getenv("QUERY_STRING");
    if (!method) method = "GET";
    if (!qs) qs = "";

    mkdir_p(JOBS);
    prune_jobs();

    /* 解析 query（只用一层 ?a=b&c=d） */
    char q[1024];
    snprintf(q, sizeof(q), "%s", qs);
    char *pjob = NULL, *penv = NULL, *pfile = NULL, *pupload = NULL, *pstat = NULL;
    char *pauto = NULL, *pargs = NULL, *pautolist = NULL, *pdaemon = NULL, *pautostop = NULL;
    char *pautostate = NULL;
    char *save = NULL;
    for (char *t = strtok_r(q, "&", &save); t; t = strtok_r(NULL, "&", &save)) {
        char *eq = strchr(t, '=');
        if (!eq) continue;
        *eq = 0;
        char *v = eq + 1;
        url_decode(v);
        if (!strcmp(t, "job")) pjob = v;
        else if (!strcmp(t, "env")) penv = v;
        else if (!strcmp(t, "file")) pfile = v;
        else if (!strcmp(t, "upload")) pupload = v;
        else if (!strcmp(t, "status")) pstat = v;
        else if (!strcmp(t, "auto")) pauto = v;
        else if (!strcmp(t, "args")) pargs = v;
        else if (!strcmp(t, "auto_list")) pautolist = v;
        else if (!strcmp(t, "daemon")) pdaemon = v;
        else if (!strcmp(t, "auto_stop")) pautostop = v;
        else if (!strcmp(t, "auto_state")) pautostate = v;
    }

    if (pautolist && !strcmp(pautolist, "1")) {
        /* 代理给 agent-c --auto-list */
        char buf[8192] = {0};
        FILE *p = popen(AGENT_C " --auto-list --plugins " PLUGIN_DIR " 2>/dev/null", "r");
        if (p) {
            size_t n = fread(buf, 1, sizeof(buf) - 1, p);
            buf[n] = 0;
            pclose(p);
        }
        char *line = NULL, *save2 = NULL, *last = NULL;
        for (char *t2 = strtok_r(buf, "\n", &save2); t2; t2 = strtok_r(NULL, "\n", &save2))
            if (t2[0] == '{') last = t2;
        if (last) {
            printf("Content-Type: application/json; charset=utf-8\r\n"
                   "Cache-Control: no-store\r\nContent-Length: %zu\r\n\r\n%s",
                   strlen(last), last);
            fflush(stdout);
        } else {
            out_json("{\"ok\":0,\"error\":\"取自动化清单失败\"}");
        }
        return 0;
    }
    if (pautostate && !strcmp(pautostate, "1")) {
        char buf[2048] = {0};
        FILE *p = popen(AGENT_C " --auto-state --plugins " PLUGIN_DIR " 2>/dev/null", "r");
        if (p) { size_t n = fread(buf, 1, sizeof(buf) - 1, p); buf[n] = 0; pclose(p); }
        char *line = NULL, *save2 = NULL, *last = NULL;
        for (char *t2 = strtok_r(buf, "\n", &save2); t2; t2 = strtok_r(NULL, "\n", &save2))
            if (t2[0] == '{') last = t2;
        if (last) out_raw(last);
        else out_json("{\"ok\":0,\"error\":\"取状态失败\"}");
        return 0;
    }
    if (pautostop && pautostop[0]) { do_auto_stop(pautostop); return 0; }
    if (pauto && pauto[0]) {
        do_auto(pauto, pargs, (pdaemon && !strcmp(pdaemon, "1")) ? 1 : 0);
        return 0;
    }
    if (pstat && !strcmp(pstat, "1")) { do_status(); return 0; }
    if (penv && !strcmp(penv, "1")) { do_env(); return 0; }
    if (pfile && pfile[0])          { do_file(pfile); return 0; }
    if (pjob && pjob[0])            { do_poll(pjob); return 0; }

    if (!strcasecmp(method, "POST")) {
        size_t blen = 0;
        char *body = read_body(&blen);
        if (!body) { out_json("{\"ok\":0,\"error\":\"请求体为空或过大\"}"); return 0; }
        if (pupload && !strcmp(pupload, "1")) do_upload(body);
        else do_task(body);
        free(body);
        return 0;
    }

    out_json("{\"ok\":0,\"error\":\"不支持的方法\"}");
    return 0;
}
