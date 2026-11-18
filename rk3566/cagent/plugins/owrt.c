/* owrt.c — OpenWrt 配置插件
 *
 * 覆盖 OpenWrt 日常配置与无线查询：uci / iwinfo / iw / init.d / logread。
 *
 * 安全三条原则：
 *   1. 全部用 fork+execv，参数作为 argv 直接传给命令，不经过 shell —— 没有注入面。
 *   2. uci 的"改"和"生效"分开：uci_set/add_list/delete 只写 /etc/config 里的文件，
 *      不 commit 就不生效；要生效必须再调 op=uci_commit。中间随时能 op=uci_revert。
 *   3. 碰到会断网/锁死自己的键（network.lan.*、dropbear、firewall、network.wan）
 *      照样执行，但返回里带警告，让调用方知道风险。
 *
 * 支持的 op：
 *   uci_show     [package]           整个包（表格）
 *   uci_get      key                 单个值
 *   uci_changes                      还没提交的改动
 *   uci_set      key value           改一个值（不生效）
 *   uci_add_list key value           往列表加一项
 *   uci_del_list key value           从列表删一项
 *   uci_delete   key                 删节点
 *   uci_commit   package [reload]    提交（reload=network/firewall/dropbear… 可选）
 *   uci_revert   [package]           撤销未提交改动
 *   iwinfo       [iface] [sub]       无线信息（sub=info/assoclist/scan）
 *   iw           sub [iface]         iw dev / iw list / station dump
 *   service      name action         status / reload / restart
 *   logread      [lines]             最近系统日志
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <unistd.h>
#include <stdarg.h>
#include <sys/wait.h>
#include <fcntl.h>
#include "plugin.h"

#define OUTMAX 24576

/* ---------------- JSON 取值 ---------------- */
static int jget(const char *js, const char *key, char *out, size_t cap) {
    char pat[64];
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

static int jget_int(const char *js, const char *key, int dflt) {
    char pat[64];
    snprintf(pat, sizeof(pat), "\"%s\":", key);
    const char *p = strstr(js, pat);
    return p ? atoi(p + strlen(pat)) : dflt;
}

/* ---------------- 参数白名单 ---------------- */
/* uci 的键/节/包名、以及值：只允许可打印且不含 shell 元字符。
 * 反正走 execv 不过 shell，这里再拦一道是防"参数被当成选项"（比如值以 - 开头）。 */
static int safe_token(const char *s, size_t maxlen, int allow_space) {
    if (!s || !*s || strlen(s) > maxlen) return 0;
    if (s[0] == '-') return 0;                       /* 别让它变成选项 */
    for (const char *p = s; *p; p++) {
        unsigned char c = (unsigned char)*p;
        if (c < 0x20 || c == 0x7f) return 0;         /* 控制字符 */
        if (c >= 0x80) continue;                     /* 中文等 UTF-8 放行 */
        if (isalnum(c) || strchr("._-:/@,+=%()[]", c)) continue;
        if (allow_space && c == ' ') continue;
        return 0;
    }
    return 1;
}

/* 接口名：wlan0 / phy0-ap0 / br-lan */
static int safe_iface(const char *s) {
    if (!s || !*s || strlen(s) > 24) return 0;
    for (const char *p = s; *p; p++)
        if (!isalnum((unsigned char)*p) && !strchr("._-@", *p)) return 0;
    return 1;
}

/* ---------------- fork+execv 执行，输出 + 退出码 ---------------- */
static int run_argv(char *const argv[], char *out, size_t cap) {
    int fd[2];
    if (pipe(fd) != 0) { snprintf(out, cap, "pipe 失败"); return -1; }

    pid_t pid = fork();
    if (pid < 0) { close(fd[0]); close(fd[1]); snprintf(out, cap, "fork 失败"); return -1; }

    if (pid == 0) {
        close(fd[0]);
        dup2(fd[1], STDOUT_FILENO);
        dup2(fd[1], STDERR_FILENO);
        close(fd[1]);
        int nul = open("/dev/null", 0);
        if (nul >= 0) { dup2(nul, STDIN_FILENO); if (nul > 2) close(nul); }
        execv(argv[0], argv);
        _exit(127);
    }

    close(fd[1]);
    size_t n = 0;
    ssize_t r;
    while ((r = read(fd[0], out + n, cap - 1 - n)) > 0) {
        n += (size_t)r;
        if (n + 1 >= cap) break;
    }
    out[n] = 0;
    close(fd[0]);

    int status = 0;
    waitpid(pid, &status, 0);
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    return -1;
}

/* ---------------- 输出封装 ---------------- */
static char *json_pair(const char *text, const char *answer, const char *warn) {
    size_t cap = strlen(text) * 2 + strlen(answer) * 2 + (warn ? strlen(warn) * 2 : 0) + 512;
    char *o = malloc(cap);
    if (!o) return NULL;
    size_t i = 0;
    i += (size_t)snprintf(o + i, cap - i, "{\"value\":\"");
    for (const char *p = text; *p && i + 12 < cap; p++) {
        if (*p == '"' || *p == '\\') { o[i++] = '\\'; o[i++] = *p; }
        else if (*p == '\n') { o[i++] = '\\'; o[i++] = 'n'; }
        else if (*p == '\r') { }
        else if (*p == '\t') { o[i++] = ' '; }
        else if ((unsigned char)*p < 0x20) { o[i++] = ' '; }
        else o[i++] = *p;
    }
    i += (size_t)snprintf(o + i, cap - i, "\",\"answer\":\"");
    for (const char *p = answer; *p && i + 12 < cap; p++) {
        if (*p == '"' || *p == '\\') { o[i++] = '\\'; o[i++] = *p; }
        else if (*p == '\n') { o[i++] = '\\'; o[i++] = 'n'; }
        else if ((unsigned char)*p < 0x20) { o[i++] = ' '; }
        else o[i++] = *p;
    }
    i += (size_t)snprintf(o + i, cap - i, "\"");
    if (warn && warn[0]) {
        i += (size_t)snprintf(o + i, cap - i, ",\"warning\":\"");
        for (const char *p = warn; *p && i + 12 < cap; p++) {
            if (*p == '"' || *p == '\\') { o[i++] = '\\'; o[i++] = *p; }
            else if (*p == '\n') { o[i++] = '\\'; o[i++] = 'n'; }
            else if ((unsigned char)*p < 0x20) { o[i++] = ' '; }
            else o[i++] = *p;
        }
        i += (size_t)snprintf(o + i, cap - i, "\"");
    }
    snprintf(o + i, cap - i, "}");
    return o;
}

static char *err_json(const char *fmt, ...) {
    char msg[400];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);
    char *o = malloc(strlen(msg) * 2 + 64);
    if (!o) return NULL;
    size_t i = (size_t)snprintf(o, strlen(msg) * 2 + 64, "{\"error\":\"");
    for (const char *p = msg; *p; p++) {
        if (*p == '"' || *p == '\\') { o[i++] = '\\'; o[i++] = *p; }
        else if (*p == '\n') { o[i++] = '\\'; o[i++] = 'n'; }
        else o[i++] = *p;
    }
    snprintf(o + i, strlen(msg) * 2 + 64 - i, "\"}");
    return o;
}

/* 危险键：不阻止，但返回警告 */
static const char *risky_key(const char *key) {
    static const char *risky[] = {
        "network.lan.", "network.wan.", "network.globals", "dropbear.",
        "firewall.", "dhcp.lan.dhcpv4", "system.", NULL
    };
    for (int i = 0; risky[i]; i++)
        if (!strncmp(key, risky[i], strlen(risky[i]))) return risky[i];
    return NULL;
}

/* ---------------- op: uci show ---------------- */
static char *op_uci_show(const char *pkg) {
    char out[OUTMAX];
    char *argv[4];
    argv[0] = (char *)"/sbin/uci";
    argv[1] = (char *)"show";
    argv[2] = (pkg && pkg[0]) ? (char *)pkg : NULL;
    argv[3] = NULL;
    if (pkg && pkg[0] && !safe_token(pkg, 64, 0))
        return err_json("包名不合法：%s", pkg);

    int rc = run_argv(argv, out, sizeof(out));
    if (rc != 0 || !out[0])
        return err_json("uci show %s 没有输出（包不存在？）", pkg ? pkg : "");

    /* 输出是 key='value' 一行一条，做成表格更好读 */
    char *buf = malloc(OUTMAX);
    if (!buf) return NULL;
    size_t i = (size_t)snprintf(buf, OUTMAX, "| 键 | 值 |\n|---|---|\n");
    int cnt = 0;
    char *save = NULL;
    for (char *line = strtok_r(out, "\n", &save); line && i < OUTMAX - 200;
         line = strtok_r(NULL, "\n", &save)) {
        char *eq = strchr(line, '=');
        if (!eq) continue;
        *eq = 0;
        char *val = eq + 1;
        size_t vl = strlen(val);
        if (vl >= 2 && (*val == '\'' || *val == '"') && val[vl - 1] == *val) {
            val[vl - 1] = 0;
            val++;
        }
        i += (size_t)snprintf(buf + i, OUTMAX - i, "| `%s` | %s |\n", line, val);
        cnt++;
    }
    char ans[128];
    snprintf(ans, sizeof(ans), "%s 共 %d 项配置",
             (pkg && pkg[0]) ? pkg : "全部", cnt);
    char *r = json_pair(buf, ans, NULL);
    free(buf);
    return r;
}

/* ---------------- op: uci get ---------------- */
static char *op_uci_get(const char *key) {
    if (!safe_token(key, 128, 0)) return err_json("键名不合法：%s", key);
    char out[8192];
    char *argv[] = { (char *)"/sbin/uci", (char *)"get", (char *)key, NULL };
    int rc = run_argv(argv, out, sizeof(out));
    if (rc != 0) return err_json("%s 不存在", key);

    size_t l = strlen(out);
    while (l > 0 && (out[l - 1] == '\n' || out[l - 1] == '\r')) out[--l] = 0;
    char text[9000];
    snprintf(text, sizeof(text), "`%s` = **%s**", key, out);
    char ans[256];
    snprintf(ans, sizeof(ans), "%s = %s", key, out);
    return json_pair(text, ans, NULL);
}

/* ---------------- op: uci changes ---------------- */
static char *op_uci_changes(void) {
    char out[OUTMAX];
    char *argv[] = { (char *)"/sbin/uci", (char *)"changes", NULL };
    int rc = run_argv(argv, out, sizeof(out));
    (void)rc;
    if (!out[0]) {
        return json_pair("没有未提交的改动。", "没有未提交的改动", NULL);
    }
    char text[OUTMAX];
    snprintf(text, sizeof(text), "未提交的改动：\n\n```\n%s\n```\n\n"
             "要生效：`op=uci_commit`；要撤销：`op=uci_revert`", out);
    char ans[128];
    snprintf(ans, sizeof(ans), "有未提交的 uci 改动");
    return json_pair(text, ans, NULL);
}

/* ---------------- op: uci set / add_list / del_list / delete ---------------- */
static char *op_uci_set(const char *key, const char *value, const char *mode) {
    if (!safe_token(key, 128, 0)) return err_json("键名不合法：%s", key);
    if (value && !safe_token(value, 512, 1)) return err_json("值含不允许的字符：%s", value);

    char pair[700];
    char out[4096];
    char *argv[6];
    int rc;
    const char *verb_cn;

    if (!strcmp(mode, "set")) {
        if (!value || !*value) return err_json("set 需要 value");
        snprintf(pair, sizeof(pair), "%s=%s", key, value);
        argv[0] = (char *)"/sbin/uci"; argv[1] = (char *)"set"; argv[2] = pair; argv[3] = NULL;
        verb_cn = "设置";
    } else if (!strcmp(mode, "add_list")) {
        if (!value || !*value) return err_json("add_list 需要 value");
        snprintf(pair, sizeof(pair), "%s=%s", key, value);
        argv[0] = (char *)"/sbin/uci"; argv[1] = (char *)"add_list"; argv[2] = pair; argv[3] = NULL;
        verb_cn = "追加到列表";
    } else if (!strcmp(mode, "del_list")) {
        if (!value || !*value) return err_json("del_list 需要 value");
        snprintf(pair, sizeof(pair), "%s=%s", key, value);
        argv[0] = (char *)"/sbin/uci"; argv[1] = (char *)"del_list"; argv[2] = pair; argv[3] = NULL;
        verb_cn = "从列表移除";
    } else {
        argv[0] = (char *)"/sbin/uci"; argv[1] = (char *)"delete"; argv[2] = (char *)key; argv[3] = NULL;
        verb_cn = "删除";
    }

    rc = run_argv(argv, out, sizeof(out));
    if (rc != 0)
        return err_json("%s 失败：%s", verb_cn, out[0] ? out : "uci 返回非 0");

    /* 读回当前值，确认真的写进去了 */
    char back[1024] = {0};
    {
        char *gargv[] = { (char *)"/sbin/uci", (char *)"get", (char *)key, NULL };
        run_argv(gargv, back, sizeof(back));
        size_t bl = strlen(back);
        while (bl > 0 && (back[bl - 1] == '\n' || back[bl - 1] == '\r')) back[--bl] = 0;
    }

    /* 包名 = 键的第一个点之前那段：network.lan.ipaddr → network */
    char pkgname[80] = {0};
    {
        const char *dot = strchr(key, '.');
        size_t L = dot ? (size_t)(dot - key) : strlen(key);
        if (L >= sizeof(pkgname)) L = sizeof(pkgname) - 1;
        memcpy(pkgname, key, L);
        pkgname[L] = 0;
    }

    char text[2048];
    snprintf(text, sizeof(text),
        "%s 完成：`%s`\n\n当前值：`%s`\n\n"
        "**还没生效** —— 改动只写进了 /etc/config。\n"
        "要生效：`{\"op\":\"uci_commit\",\"package\":\"%s\"}`\n"
        "要撤销：`{\"op\":\"uci_revert\",\"package\":\"%s\"}`",
        verb_cn, key, back[0] ? back : "(空)", pkgname, pkgname);

    char ans[256];
    snprintf(ans, sizeof(ans), "%s %s 成功（未提交）", verb_cn, key);

    const char *rk = risky_key(key);
    if (rk)
        return json_pair(text, ans,
            "这个键会影响网络/远程登录，commit 后可能立刻断开现有连接。"
            "建议先确认有串口或本地终端可用。");
    return json_pair(text, ans, NULL);
}

/* ---------------- op: uci commit ---------------- */
static char *op_uci_commit(const char *pkg, const char *reload) {
    if (pkg && pkg[0] && !safe_token(pkg, 64, 0)) return err_json("包名不合法：%s", pkg);

    char out[4096];
    char *argv[4];
    argv[0] = (char *)"/sbin/uci";
    argv[1] = (char *)"commit";
    argv[2] = (pkg && pkg[0]) ? (char *)pkg : NULL;
    argv[3] = NULL;
    int rc = run_argv(argv, out, sizeof(out));
    if (rc != 0) return err_json("uci commit 失败：%s", out[0] ? out : "非 0");

    char text[4096];
    size_t i = (size_t)snprintf(text, sizeof(text),
        "已提交到 `/etc/config/%s`。", (pkg && pkg[0]) ? pkg : "(全部)");

    char reloaded[128] = {0};
    if (reload && reload[0]) {
        if (!safe_token(reload, 48, 0)) return err_json("reload 名不合法：%s", reload);
        char script[128];
        snprintf(script, sizeof(script), "/etc/init.d/%s", reload);
        char *rargv[] = { script, (char *)"reload", NULL };
        int rrc = run_argv(rargv, out, sizeof(out));
        if (rrc != 0) {
            char *rargv2[] = { script, (char *)"restart", NULL };
            rrc = run_argv(rargv2, out, sizeof(out));
        }
        snprintf(reloaded, sizeof(reloaded), "%s(%s)", reload, rrc == 0 ? "ok" : "失败");
        i += (size_t)snprintf(text + i, sizeof(text) - i,
            "\n已重载服务：%s%s", reload, rrc == 0 ? "" : "（reload/restart 返回非 0，看看下面的输出）");
        if (rrc != 0 && out[0] && i < sizeof(text) - 400)
            i += (size_t)snprintf(text + i, sizeof(text) - i, "\n\n```\n%.300s\n```", out);
    } else {
        i += (size_t)snprintf(text + i, sizeof(text) - i,
            "\n**注意**：commit 只是写盘，很多改动要 reload 服务才生效。"
            "\n可以用 `{\"op\":\"uci_commit\",\"package\":\"%s\",\"reload\":\"%s\"}`",
            (pkg && pkg[0]) ? pkg : "network",
            (pkg && !strcmp(pkg, "wireless")) ? "network" : ((pkg && pkg[0]) ? pkg : "network"));
    }

    char ans[256];
    snprintf(ans, sizeof(ans), "已提交%s%s", (pkg && pkg[0]) ? pkg : "全部配置",
             reloaded[0] ? "，服务已重载" : "");
    const char *rk = pkg ? risky_key(pkg) : NULL;
    return json_pair(text, ans,
        (pkg && (!strcmp(pkg, "network") || !strcmp(pkg, "firewall") || !strcmp(pkg, "dhcp")))
            ? "网络类配置提交后可能断开当前连接，如果通过 SSH 操作请准备好重连或串口。"
            : (rk ? "这个包会影响网络/登录，注意连接可能中断。" : NULL));
}

/* ---------------- op: uci revert ---------------- */
static char *op_uci_revert(const char *pkg) {
    if (pkg && pkg[0] && !safe_token(pkg, 64, 0)) return err_json("包名不合法：%s", pkg);
    char out[4096];
    char *argv[4];
    argv[0] = (char *)"/sbin/uci";
    argv[1] = (char *)"revert";
    argv[2] = (pkg && pkg[0]) ? (char *)pkg : NULL;
    argv[3] = NULL;
    int rc = run_argv(argv, out, sizeof(out));
    if (rc != 0) return err_json("uci revert 失败：%s", out[0] ? out : "非 0");
    char text[512];
    snprintf(text, sizeof(text), "已撤销 %s 的未提交改动，回到 /etc/config 里的原值。",
             (pkg && pkg[0]) ? pkg : "全部");
    char ans[128];
    snprintf(ans, sizeof(ans), "已撤销未提交改动");
    return json_pair(text, ans, NULL);
}

/* ---------------- op: iwinfo ---------------- */
static char *op_iwinfo(const char *iface, const char *sub) {
    char out[OUTMAX];
    char *argv[5];
    argv[0] = (char *)"/usr/bin/iwinfo";

    if (!iface || !iface[0]) {
        /* 不带参数：列出所有无线设备 */
        argv[1] = NULL;
        run_argv(argv, out, sizeof(out));
        if (!out[0]) return json_pair("这台设备没有无线接口（iwinfo 无输出）。", "没有无线接口", NULL);
        char text[OUTMAX];
        snprintf(text, sizeof(text), "```\n%s```", out);
        return json_pair(text, "无线设备列表", NULL);
    }

    if (!safe_iface(iface)) return err_json("接口名不合法：%s", iface);
    const char *s = (sub && sub[0]) ? sub : "info";
    if (strcmp(s, "info") && strcmp(s, "assoclist") && strcmp(s, "scan")
        && strcmp(s, "txpower") && strcmp(s, "country") && strcmp(s, "freqlist"))
        return err_json("不支持的 iwinfo 子命令：%s（可用 info/assoclist/scan/txpower/country）", s);

    argv[1] = (char *)iface;
    argv[2] = (char *)s;
    argv[3] = NULL;
    int rc = run_argv(argv, out, sizeof(out));
    if (rc != 0 || !out[0]) return err_json("iwinfo %s %s 失败", iface, s);

    char text[OUTMAX];
    if (!strcmp(s, "scan")) {
        snprintf(text, sizeof(text), "扫描结果（`%s`）：\n\n```\n%.12000s```", iface, out);
    } else {
        snprintf(text, sizeof(text), "```\n%.12000s```", out);
    }
    char ans[128];
    snprintf(ans, sizeof(ans), "iwinfo %s %s", iface, s);
    return json_pair(text, ans, NULL);
}

/* ---------------- op: iw ---------------- */
static char *op_iw(const char *sub, const char *iface) {
    char out[OUTMAX];
    char *argv[6];
    argv[0] = (char *)"/usr/sbin/iw";

    if (!sub || !*sub || !strcmp(sub, "dev")) {
        argv[1] = (char *)"dev";
        argv[2] = NULL;
    } else if (!strcmp(sub, "list")) {
        argv[1] = (char *)"list";
        argv[2] = NULL;
    } else if (!strcmp(sub, "station")) {
        if (!safe_iface(iface)) return err_json("station 需要合法 iface");
        argv[1] = (char *)"dev"; argv[2] = (char *)iface;
        argv[3] = (char *)"station"; argv[4] = (char *)"dump"; argv[5] = NULL;
    } else if (!strcmp(sub, "link")) {
        if (!safe_iface(iface)) return err_json("link 需要合法 iface");
        argv[1] = (char *)"dev"; argv[2] = (char *)iface;
        argv[3] = (char *)"link"; argv[4] = NULL;
    } else {
        return err_json("不支持的 iw 子命令：%s（可用 dev/list/station/link）", sub);
    }

    int rc = run_argv(argv, out, sizeof(out));
    /* station dump 在"没有客户端关联"时输出为空、退出码 0 —— 那是正常状态，
     * 不是失败。这里分开判断，别把"没人连着"报成错误。 */
    if (!out[0] && rc == 0) {
        if (!strcmp(sub, "station"))
            return json_pair("`station dump` 为空：当前没有客户端关联到该接口。",
                             "没有客户端连接", NULL);
        return json_pair("命令没有输出（接口或无线不存在？）", "无输出", NULL);
    }
    if (rc != 0) return err_json("iw %s 失败（设备可能没有无线）", sub ? sub : "dev");
    char text[OUTMAX];
    snprintf(text, sizeof(text), "```\n%.12000s```", out);
    char ans[128];
    snprintf(ans, sizeof(ans), "iw %s", sub && *sub ? sub : "dev");
    return json_pair(text, ans, NULL);
}

/* ---------------- op: service ---------------- */
static char *op_service(const char *name, const char *action) {
    if (!safe_token(name, 48, 0)) return err_json("服务名不合法：%s", name);
    const char *a = (action && action[0]) ? action : "status";
    if (strcmp(a, "status") && strcmp(a, "reload") && strcmp(a, "restart")
        && strcmp(a, "start") && strcmp(a, "stop") && strcmp(a, "enable") && strcmp(a, "disable"))
        return err_json("不支持的动作：%s", a);

    char script[160];
    snprintf(script, sizeof(script), "/etc/init.d/%s", name);
    if (access(script, X_OK) != 0) return err_json("没有这个服务：%s", name);

    char out[8192];
    char *argv[] = { script, (char *)a, NULL };
    int rc = run_argv(argv, out, sizeof(out));

    char text[4096];
    snprintf(text, sizeof(text), "`%s %s` → 退出码 %d\n\n```\n%.2000s```", name, a, rc, out);
    char ans[160];
    snprintf(ans, sizeof(ans), "%s %s %s", name, a, rc == 0 ? "成功" : "返回非 0");
    return json_pair(text, ans,
        (!strcmp(a, "stop") || !strcmp(a, "restart"))
            ? "停止/重启网络类服务可能导致当前连接中断。" : NULL);
}

/* ---------------- op: logread ---------------- */
static char *op_logread(int lines) {
    if (lines <= 0) lines = 30;
    if (lines > 300) lines = 300;
    char out[OUTMAX];
    char *argv[] = { (char *)"/sbin/logread", NULL };
    int rc = run_argv(argv, out, sizeof(out));
    if (rc != 0 || !out[0]) return err_json("logread 失败");

    /* 只留最后 N 行 */
    char *save = NULL;
    char *keep[300];
    int n = 0;
    for (char *line = strtok_r(out, "\n", &save); line; line = strtok_r(NULL, "\n", &save)) {
        keep[n % 300] = line;
        n++;
    }
    int show = n < lines ? n : lines;
    char text[OUTMAX];
    size_t i = (size_t)snprintf(text, sizeof(text), "最近 %d 行日志：\n\n```\n", show);
    for (int k = n - show; k < n && i < OUTMAX - 300; k++)
        i += (size_t)snprintf(text + i, OUTMAX - i, "%s\n", keep[k % 300]);
    snprintf(text + i, OUTMAX - i, "```");
    char ans[96];
    snprintf(ans, sizeof(ans), "最近 %d 行系统日志", show);
    return json_pair(text, ans, NULL);
}

/* ---------------- 入口 ---------------- */
static char *run(const char *json_in) {
    char op[32] = {0}, key[160] = {0}, value[600] = {0};
    char pkg[80] = {0}, iface[40] = {0}, sub[32] = {0};
    char reload[64] = {0}, name[64] = {0}, action[32] = {0};

    jget(json_in, "op", op, sizeof(op));
    jget(json_in, "key", key, sizeof(key));
    jget(json_in, "value", value, sizeof(value));
    jget(json_in, "package", pkg, sizeof(pkg));
    jget(json_in, "iface", iface, sizeof(iface));
    jget(json_in, "sub", sub, sizeof(sub));
    jget(json_in, "reload", reload, sizeof(reload));
    jget(json_in, "name", name, sizeof(name));
    jget(json_in, "action", action, sizeof(action));

    if (!op[0]) {
        /* 模型常把子命令当工具名写（{"tool":"uci_show"}），
         * 框架按别名前缀路由到这里，这里把 tool 名拿回来当 op。 */
        char toolname[40] = {0};
        jget(json_in, "tool", toolname, sizeof(toolname));
        if (toolname[0] && strcmp(toolname, "owrt") != 0
            && strcmp(toolname, "openwrt") != 0 && strcmp(toolname, "config") != 0)
            snprintf(op, sizeof(op), "%s", toolname);
    }
    if (!op[0]) {
        /* 猜：有 key+value 是改，有 key 是查，有 iface 是无线 */
        if (key[0] && value[0])      snprintf(op, sizeof(op), "uci_set");
        else if (key[0])             snprintf(op, sizeof(op), "uci_get");
        else if (iface[0])           snprintf(op, sizeof(op), "iwinfo");
        else                         snprintf(op, sizeof(op), "uci_show");
    }

    if (!strcmp(op, "uci_show") || !strcmp(op, "uci") || !strcmp(op, "show"))
        return op_uci_show(pkg);
    if (!strcmp(op, "uci_get") || !strcmp(op, "get"))
        return op_uci_get(key);
    if (!strcmp(op, "uci_changes") || !strcmp(op, "changes"))
        return op_uci_changes();
    if (!strcmp(op, "uci_set") || !strcmp(op, "set"))
        return op_uci_set(key, value, "set");
    if (!strcmp(op, "uci_add_list") || !strcmp(op, "add_list"))
        return op_uci_set(key, value, "add_list");
    if (!strcmp(op, "uci_del_list") || !strcmp(op, "del_list"))
        return op_uci_set(key, value, "del_list");
    if (!strcmp(op, "uci_delete") || !strcmp(op, "delete"))
        return op_uci_set(key, NULL, "delete");
    if (!strcmp(op, "uci_commit") || !strcmp(op, "commit"))
        return op_uci_commit(pkg, reload);
    if (!strcmp(op, "uci_revert") || !strcmp(op, "revert"))
        return op_uci_revert(pkg);
    if (!strcmp(op, "iwinfo"))
        return op_iwinfo(iface, sub);
    if (!strcmp(op, "iw"))
        return op_iw(sub[0] ? sub : "dev", iface);
    if (!strcmp(op, "service") || !strcmp(op, "initd"))
        return op_service(name[0] ? name : (pkg[0] ? pkg : ""), action);
    if (!strcmp(op, "logread") || !strcmp(op, "log"))
        return op_logread(jget_int(json_in, "lines", 30));

    return err_json("不支持的 op：%s（uci_show/get/set/add_list/del_list/delete/commit/revert, "
                    "iwinfo, iw, service, logread）", op);
}

static plugin_desc_t desc = {
    PLUGIN_API_VERSION,
    "owrt",
    "OpenWrt 配置：uci 查询/修改/提交(uci_*) 无线信息(iwinfo) 无线诊断(iw) "
    "服务控制(service) 系统日志(logread)。改配置分两步，commit 才生效",
    "{\"op\":\"uci_show\",\"package\":\"network\"}"
    "  或 {\"op\":\"uci_get\",\"key\":\"network.lan.ipaddr\"}"
    "  或 {\"op\":\"uci_set\",\"key\":\"network.lan.ipaddr\",\"value\":\"192.168.1.1\"}"
    "  或 {\"op\":\"uci_commit\",\"package\":\"network\",\"reload\":\"network\"}"
    "  或 {\"op\":\"uci_changes\"}  查未提交改动"
    "  或 {\"op\":\"iwinfo\",\"iface\":\"wlan0\",\"sub\":\"assoclist\"}"
    "  或 {\"op\":\"iw\",\"sub\":\"station\",\"iface\":\"wlan0\"}"
    "  或 {\"op\":\"service\",\"name\":\"dnsmasq\",\"action\":\"restart\"}"
    "  或 {\"op\":\"logread\",\"lines\":40}",
    run,
    "uci,iwinfo,iw,openwrt,owrt,config,service,logread"
};

plugin_desc_t *plugin_entry(void) { return &desc; }
