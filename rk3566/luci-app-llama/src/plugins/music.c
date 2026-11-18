/* music.c — 音乐：MIDI 生成 / 读取 / FC 音色合成（纯 C）
 *
 * 音色参数全部照搬 Python 版 fc_synth.py 的 A/B 试听结论：
 *   SR=32000      22050 会把方波高次谐波砍掉，听着发闷
 *   pulse duty=0.5 主旋律（必须减直流，否则窄占空会累积巨大 DC 偏移）
 *   triangle       低音通道
 *   noise+exp decay 鼓
 *   ADSR a=1ms d=75ms s=42% r=30ms  瞬时起音才有"拨弦/打击"颗粒感
 *   vibrato 0.006 @5.5Hz            FC 那种轻微抖
 *
 * op：
 *   midi    {"path","notes":[{pitch,dur}],"tempo","tracks"}  生成 .mid
 *   render  {"path":"a.mid"} 或 {"out":"a.mp3","notes":[...]} 合成 mp3
 *   import  {"path":"a.mid"}                                  读现有 MIDI 再合成
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <math.h>
#include <unistd.h>
#include <sys/stat.h>
#include "plugin.h"

#define SR        32000
#define MAX_NOTE  4096
#define WORK_DIR  "/tmp/llama_agent"

typedef struct {
    int pitch;        /* MIDI 音高 0-127 */
    double start;     /* 秒 */
    double dur;       /* 秒 */
    int ch;           /* 0=旋律 1=低音 9=鼓 */
} note_t;

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

static double jget_num(const char *js, const char *key, double dflt) {
    char pat[64];
    snprintf(pat, sizeof(pat), "\"%s\":", key);
    const char *p = strstr(js, pat);
    return p ? atof(p + strlen(pat)) : dflt;
}

static char *jerr(const char *msg) {
    size_t cap = strlen(msg) * 2 + 64;
    char *o = malloc(cap);
    if (!o) return NULL;
    size_t i = (size_t)snprintf(o, cap, "{\"error\":\"");
    for (const char *p = msg; *p && i + 8 < cap; p++) {
        if (*p == '"' || *p == '\\') { o[i++] = '\\'; o[i++] = *p; }
        else if (*p == '\n') { o[i++] = '\\'; o[i++] = 'n'; }
        else o[i++] = *p;
    }
    snprintf(o + i, cap - i, "\"}");
    return o;
}

static char *jpair(const char *text, const char *answer) {
    size_t cap = strlen(text) * 2 + strlen(answer) * 2 + 256;
    char *o = malloc(cap);
    if (!o) return NULL;
    size_t i = 0;
    i += (size_t)snprintf(o + i, cap - i, "{\"value\":\"");
    for (const char *p = text; *p && i + 12 < cap; p++) {
        if (*p == '"' || *p == '\\') { o[i++] = '\\'; o[i++] = *p; }
        else if (*p == '\n') { o[i++] = '\\'; o[i++] = 'n'; }
        else if (*p == '\r') { }
        else if ((unsigned char)*p < 0x20) { o[i++] = ' '; }
        else o[i++] = *p;
    }
    i += (size_t)snprintf(o + i, cap - i, "\",\"answer\":\"");
    for (const char *p = answer; *p && i + 12 < cap; p++) {
        if (*p == '"' || *p == '\\') { o[i++] = '\\'; o[i++] = *p; }
        else if ((unsigned char)*p < 0x20) { o[i++] = ' '; }
        else o[i++] = *p;
    }
    snprintf(o + i, cap - i, "\"}");
    return o;
}

static void resolve_path(const char *in, char *out, size_t cap) {
    if (!in || !in[0]) { snprintf(out, cap, "%s", WORK_DIR); return; }
    if (in[0] == '/') snprintf(out, cap, "%s", in);
    else snprintf(out, cap, "%s/%s", WORK_DIR, in);
}

static void mkdirs(const char *path) {
    char tmp[1024];
    snprintf(tmp, sizeof(tmp), "%s", path);
    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/') { *p = 0; mkdir(tmp, 0755); *p = '/'; }
    }
}

/* ---------------- 解析 notes 数组 ----------------
 * 接受 [{"pitch":69,"dur":1}, ...]，dur 以"拍"为单位（1 = 四分音符）。
 */
static int parse_notes(const char *js, note_t *out, int maxn, double beat, int ch) {
    const char *p = js;
    int n = 0;
    double t = 0;
    while ((p = strstr(p, "\"pitch\"")) != NULL && n < maxn) {
        const char *c = strchr(p, ':');
        if (!c) break;
        int pitch = atoi(c + 1);
        double dur_beats = 1.0;
        const char *d = strstr(p, "\"dur\"");
        const char *brace = strchr(p, '}');
        if (d && (!brace || d < brace)) {
            const char *c2 = strchr(d, ':');
            if (c2) dur_beats = atof(c2 + 1);
        }
        if (pitch > 0 && pitch < 128) {
            out[n].pitch = pitch;
            out[n].start = t;
            out[n].dur = dur_beats * beat;
            out[n].ch = ch;
            t += dur_beats * beat;
            n++;
        } else if (pitch == 0) {
            t += dur_beats * beat;      /* 休止符 */
        }
        p = brace ? brace : p + 7;
    }
    return n;
}

/* ---------------- MIDI 写 ---------------- */
static void wr32(FILE *f, unsigned v) {
    fputc((int)((v >> 24) & 0xFF), f);
    fputc((int)((v >> 16) & 0xFF), f);
    fputc((int)((v >> 8) & 0xFF), f);
    fputc((int)(v & 0xFF), f);
}

static void wr_var(FILE *f, unsigned v) {
    unsigned char buf[5];
    int n = 0;
    buf[n++] = (unsigned char)(v & 0x7F);
    v >>= 7;
    while (v) {
        buf[n++] = (unsigned char)((v & 0x7F) | 0x80);
        v >>= 7;
    }
    while (n--) fputc(buf[n], f);
}

/* 把一个通道的音符写成一条 MTrk。
 * 做法：把 note-on / note-off 事件按时间排序（同一时刻 off 在前，
 * 避免同音高连打时被误当成延音）。 */
typedef struct { long tick; int on; int pitch; } ev_t;

static int cmp_ev(const void *a, const void *b) {
    const ev_t *x = a, *y = b;
    if (x->tick != y->tick) return (x->tick < y->tick) ? -1 : 1;
    return x->on - y->on;      /* 0(off) 先于 1(on) */
}

static void write_track(FILE *f, note_t *notes, int n, int ch, int div, double beat,
                        int with_tempo, unsigned us_per_beat) {
    ev_t ev[MAX_NOTE * 2];
    int ne = 0;
    for (int i = 0; i < n && ne < MAX_NOTE * 2 - 2; i++) {
        long t0 = (long)(notes[i].start / beat * div + 0.5);
        long t1 = t0 + (long)(notes[i].dur / beat * div + 0.5);
        if (t1 <= t0) t1 = t0 + div / 4;
        ev[ne].tick = t0; ev[ne].on = 1; ev[ne].pitch = notes[i].pitch; ne++;
        ev[ne].tick = t1; ev[ne].on = 0; ev[ne].pitch = notes[i].pitch; ne++;
    }
    qsort(ev, (size_t)ne, sizeof(ev[0]), cmp_ev);

    /* 先写进内存，才知道长度 */
    unsigned char *buf = malloc((size_t)ne * 8 + 64);
    if (!buf) return;
    size_t len = 0;
    long last = 0;

    /* 速度事件必须写，否则任何软件读回来都按默认 120 BPM */
    if (with_tempo) {
        buf[len++] = 0x00;                       /* delta 0 */
        buf[len++] = 0xFF; buf[len++] = 0x51; buf[len++] = 0x03;
        buf[len++] = (unsigned char)((us_per_beat >> 16) & 0xFF);
        buf[len++] = (unsigned char)((us_per_beat >> 8) & 0xFF);
        buf[len++] = (unsigned char)(us_per_beat & 0xFF);
    }
    for (int i = 0; i < ne; i++) {
        unsigned delta = (unsigned)(ev[i].tick - last);
        last = ev[i].tick;
        /* 变长编码 */
        unsigned char vb[5];
        int vn = 0;
        vb[vn++] = (unsigned char)(delta & 0x7F);
        delta >>= 7;
        while (delta) { vb[vn++] = (unsigned char)((delta & 0x7F) | 0x80); delta >>= 7; }
        while (vn--) buf[len++] = vb[vn];

        int is_drum = (ch == 9);
        buf[len++] = (unsigned char)((ev[i].on ? 0x90 : 0x80) | (is_drum ? 9 : ch));
        buf[len++] = (unsigned char)(ev[i].pitch & 0x7F);
        buf[len++] = (unsigned char)(ev[i].on ? 100 : 0);
    }
    /* end of track */
    buf[len++] = 0x00; buf[len++] = 0xFF; buf[len++] = 0x2F; buf[len++] = 0x00;

    fwrite("MTrk", 1, 4, f);
    wr32(f, (unsigned)len);
    fwrite(buf, 1, len, f);
    free(buf);
}

static char *op_midi(const char *json) {
    char p[512] = {0};
    jget(json, "path", p, sizeof(p));
    if (!p[0]) snprintf(p, sizeof(p), "music.mid");
    char path[768];
    resolve_path(p, path, sizeof(path));
    if (!strstr(path, ".mid")) strncat(path, ".mid", sizeof(path) - strlen(path) - 1);
    mkdirs(path);

    double tempo = jget_num(json, "tempo", 160);
    if (tempo < 40 || tempo > 260) tempo = 160;
    double beat = 60.0 / tempo;        /* 一拍几秒 */
    int div = 96;                      /* 每拍 96 tick */

    /* notes 可以是单组，也可以是 {"melody":[...],"bass":[...]} */
    note_t *notes = malloc(sizeof(note_t) * MAX_NOTE);
    if (!notes) return NULL;
    int total = 0;

    /* 先看有没有分轨写法 */
    const char *mp = strstr(json, "\"melody\"");
    const char *bp = strstr(json, "\"bass\"");
    const char *dp = strstr(json, "\"drums\"");
    int ntrk = 0;

    if (mp || bp || dp) {
        char sub[65536];
        if (mp) {
            /* 取 melody 数组内容（到下一个 "xxx": 或结尾） */
            snprintf(sub, sizeof(sub), "%s", mp);
            total += parse_notes(sub, notes + total, MAX_NOTE - total, beat, 0);
            ntrk++;
        }
        if (bp) {
            snprintf(sub, sizeof(sub), "%s", bp);
            total += parse_notes(sub, notes + total, MAX_NOTE - total, beat, 1);
            ntrk++;
        }
        if (dp) {
            snprintf(sub, sizeof(sub), "%s", dp);
            total += parse_notes(sub, notes + total, MAX_NOTE - total, beat, 9);
            ntrk++;
        }
    } else {
        total = parse_notes(json, notes, MAX_NOTE, beat, 0);
        ntrk = 1;
    }

    if (total == 0) { free(notes); return jerr("没有音符：需要 notes:[{\"pitch\":69,\"dur\":1}]"); }

    FILE *f = fopen(path, "wb");
    if (!f) { free(notes); return jerr("写不了 MIDI 文件"); }

    /* 头：format 1（多轨同步） */
    fwrite("MThd", 1, 4, f);
    wr32(f, 6);
    fputc(0, f); fputc(1, f);                 /* format=1 */
    fputc(0, f); fputc((int)(ntrk > 1 ? ntrk : 1), f);
    fputc((int)((div >> 8) & 0xFF), f); fputc((int)(div & 0xFF), f);

    /* 每个通道一条轨 */
    int first_trk = 0;
    for (int c = 0; c < 3; c++) {
        int want = (c == 0) ? 0 : (c == 1) ? 1 : 9;
        note_t tmp[MAX_NOTE];
        int nn = 0;
        for (int i = 0; i < total; i++)
            if (notes[i].ch == want && nn < MAX_NOTE) tmp[nn++] = notes[i];
        if (nn == 0) continue;
        unsigned us = (unsigned)(60000000.0 / tempo);
        write_track(f, tmp, nn, want, div, beat, (first_trk++ == 0), us);
    }
    fclose(f);
    free(notes);

    struct stat st;
    stat(path, &st);
    char text[600];
    snprintf(text, sizeof(text),
        "已生成 MIDI：`%s`\n\n| 项 | 值 |\n|---|---|\n"
        "| 音符数 | %d |\n| 轨数 | %d |\n| 速度 | %.0f BPM |\n| 大小 | %lld 字节 |\n",
        path, total, ntrk > 1 ? ntrk : 1, tempo, (long long)st.st_size);
    char ans[220];
    snprintf(ans, sizeof(ans), "已生成 MIDI（%d 个音符，%.0f BPM）：%s", total, tempo, path);
    return jpair(text, ans);
}

/* ---------------- MIDI 读 ---------------- */
static unsigned rd32(const unsigned char *p) {
    return ((unsigned)p[0] << 24) | ((unsigned)p[1] << 16) | ((unsigned)p[2] << 8) | p[3];
}

static unsigned rd_var(const unsigned char *p, int *used) {
    unsigned v = 0;
    int i = 0;
    while (i < 4) {
        unsigned char c = p[i++];
        v = (v << 7) | (c & 0x7F);
        if (!(c & 0x80)) break;
    }
    *used = i;
    return v;
}

/* 解析 MIDI → notes。返回音符数，失败返回 -1 */
static int read_midi(const char *path, note_t *out, int maxn, double *beat_out) {
    FILE *f = fopen(path, "rb");
    if (!f) return -1;
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (sz <= 14 || sz > 8 * 1024 * 1024) { fclose(f); return -1; }
    unsigned char *buf = malloc((size_t)sz);
    if (!buf) { fclose(f); return -1; }
    if (fread(buf, 1, (size_t)sz, f) != (size_t)sz) { free(buf); fclose(f); return -1; }
    fclose(f);

    if (memcmp(buf, "MThd", 4) != 0) { free(buf); return -1; }
    int div = (buf[12] << 8) | buf[13];
    if (div <= 0) div = 96;
    /* tempo 默认 120 → 一拍 0.5s；有 FF 51 会覆盖 */
    double beat = 0.5;

    int n = 0;
    long pos = 8 + (long)rd32(buf + 4);
    /* 正在响的音：按 (ch,pitch) 记起始 tick */
    long on_tick[16][128];
    for (int c = 0; c < 16; c++)
        for (int k = 0; k < 128; k++) on_tick[c][k] = -1;

    while (pos + 8 <= sz) {
        if (memcmp(buf + pos, "MTrk", 4) != 0) break;
        unsigned tlen = rd32(buf + pos + 4);
        long p = pos + 8;
        long end = p + (long)tlen;
        if (end > sz) end = sz;

        unsigned running = 0;
        long tick = 0;
        while (p < end) {
            int used = 0;
            unsigned delta = rd_var(buf + p, &used);
            p += used;
            tick += delta;
            if (p >= end) break;
            unsigned char st = buf[p];
            if (st & 0x80) {
                p++;
                if (st < 0xF0) running = st;
            } else {
                st = running;            /* running status */
            }
            if (st == 0xFF) {            /* meta */
                if (p >= end) break;
                unsigned char type = buf[p++];
                int u2 = 0;
                unsigned len = rd_var(buf + p, &u2);
                p += u2;
                if (type == 0x51 && len == 3 && p + 2 < end) {
                    unsigned us = ((unsigned)buf[p] << 16) | ((unsigned)buf[p + 1] << 8) | buf[p + 2];
                    if (us) beat = us / 1000000.0;
                }
                p += (long)len;
                continue;
            }
            if (st == 0xF0 || st == 0xF7) {
                int u2 = 0;
                unsigned len = rd_var(buf + p, &u2);
                p += u2 + (long)len;
                continue;
            }

            int ch = (int)(st & 0x0F);
            int hi = (int)(st & 0xF0);
            if (hi == 0x90 || hi == 0x80) {
                if (p + 1 >= end) break;
                int pitch = buf[p] & 0x7F;
                int vel = buf[p + 1] & 0x7F;
                p += 2;
                if (hi == 0x90 && vel > 0) {
                    on_tick[ch][pitch] = tick;
                } else if (on_tick[ch][pitch] >= 0) {
                    long t0 = on_tick[ch][pitch];
                    on_tick[ch][pitch] = -1;
                    if (n < maxn) {
                        out[n].pitch = pitch;
                        out[n].start = (double)t0 / div * beat;
                        out[n].dur = (double)(tick - t0) / div * beat;
                        out[n].ch = (ch == 9) ? 9 : (ch == 1 ? 1 : 0);
                        n++;
                    }
                }
            } else if (hi == 0xC0 || hi == 0xD0) {
                p += 1;
            } else {
                p += 2;
            }
        }
        pos = pos + 8 + (long)tlen;
    }
    free(buf);
    if (beat_out) *beat_out = beat;
    return n;
}

/* ---------------- 合成 ---------------- */
/* FC 音色参数（照搬 fc_synth.py 的试听结论） */
static double adsr_env(long i, long n) {
    const double a = 0.001, d = 0.075, s = 0.42, r = 0.030;
    long an = (long)(a * SR); if (an < 1) an = 1;
    long dn = (long)(d * SR); if (dn < 1) dn = 1;
    long rn = (long)(r * SR); if (rn < 1) rn = 1;
    long sus = n - an - dn - rn;
    if (sus < 0) sus = 0;
    if (i < an) return (double)i / an;
    if (i < an + dn) {
        double k = (double)(i - an) / dn;
        return 1.0 + (s - 1.0) * k;
    }
    if (i < an + dn + sus) return s;
    {
        long ri = i - an - dn - sus;
        double k = (double)ri / rn;
        if (k > 1) k = 1;
        return s * (1.0 - k);
    }
}

static double note_freq(int n) {
    return 440.0 * pow(2.0, (n - 69) / 12.0);
}

static unsigned rng_state = 12345;
static double frand(void) {
    rng_state = rng_state * 1103515245u + 12345u;
    return ((rng_state >> 16) & 0x7FFF) / 16384.0 - 1.0;
}

static void add_tone(float *buf, long total, double start, int pitch, double dur,
                     double vol, int wave, double duty, double vib) {
    long s0 = (long)(start * SR);
    if (s0 < 0 || s0 >= total) return;
    long n = (long)(dur * SR);
    if (n <= 0) return;
    if (s0 + n > total) n = total - s0;

    double f0 = note_freq(pitch);
    double phase = 0;
    for (long i = 0; i < n; i++) {
        double t = (double)i / SR;
        double f = f0;
        if (vib > 0) f = f0 * (1.0 + vib * sin(2 * M_PI * 5.5 * t));
        phase += f / SR;
        if (phase >= 1.0) phase -= (long)phase;

        double w;
        if (wave == 0) {              /* pulse */
            w = (phase < duty) ? 1.0 : -1.0;
            w -= (2.0 * duty - 1.0);  /* 去直流 */
        } else {                      /* triangle */
            w = 2.0 * fabs(2.0 * phase - 1.0) - 1.0;
        }
        buf[s0 + i] += (float)(w * adsr_env(i, n) * vol);
    }
}

static void add_noise(float *buf, long total, double start, double dur, double vol) {
    long s0 = (long)(start * SR);
    if (s0 < 0 || s0 >= total) return;
    long n = (long)(dur * SR);
    if (n <= 0) return;
    if (s0 + n > total) n = total - s0;
    const double decay = 0.0018;
    for (long i = 0; i < n; i++) {
        double e = exp(-(double)i / (decay * SR));
        buf[s0 + i] += (float)(frand() * e * vol);
    }
}

static int write_wav(const char *path, float *buf, long total) {
    FILE *f = fopen(path, "wb");
    if (!f) return -1;
    unsigned data = (unsigned)(total * 2);
    fwrite("RIFF", 1, 4, f);
    unsigned riff = 36 + data;
    fputc((int)(riff & 0xFF), f); fputc((int)((riff >> 8) & 0xFF), f);
    fputc((int)((riff >> 16) & 0xFF), f); fputc((int)((riff >> 24) & 0xFF), f);
    fwrite("WAVEfmt ", 1, 8, f);
    unsigned fmtlen = 16;
    fputc(16, f); fputc(0, f); fputc(0, f); fputc(0, f);
    fputc(1, f); fputc(0, f);                     /* PCM */
    fputc(1, f); fputc(0, f);                     /* mono */
    unsigned sr = SR;
    fputc((int)(sr & 0xFF), f); fputc((int)((sr >> 8) & 0xFF), f);
    fputc((int)((sr >> 16) & 0xFF), f); fputc((int)((sr >> 24) & 0xFF), f);
    unsigned br = SR * 2;
    fputc((int)(br & 0xFF), f); fputc((int)((br >> 8) & 0xFF), f);
    fputc((int)((br >> 16) & 0xFF), f); fputc((int)((br >> 24) & 0xFF), f);
    fputc(2, f); fputc(0, f);                     /* block align */
    fputc(16, f); fputc(0, f);                    /* bits */
    fwrite("data", 1, 4, f);
    fputc((int)(data & 0xFF), f); fputc((int)((data >> 8) & 0xFF), f);
    fputc((int)((data >> 16) & 0xFF), f); fputc((int)((data >> 24) & 0xFF), f);
    (void)fmtlen;

    /* 归一化 + 转 16bit：不归一化会削波，整首都是破音 */
    float peak = 0.0001f;
    for (long i = 0; i < total; i++) {
        float a = fabsf(buf[i]);
        if (a > peak) peak = a;
    }
    float gain = (peak > 0.95f) ? (0.95f / peak) : 1.0f;
    for (long i = 0; i < total; i++) {
        int s = (int)(buf[i] * gain * 32000.0f);
        if (s > 32767) s = 32767;
        if (s < -32768) s = -32768;
        fputc(s & 0xFF, f);
        fputc((s >> 8) & 0xFF, f);
    }
    fclose(f);
    return 0;
}

static char *do_render(note_t *notes, int nn, const char *midi_path,
                       const char *out_name, double tempo) {
    if (nn <= 0) return jerr("没有音符可渲染");

    double end_t = 0;
    for (int i = 0; i < nn; i++) {
        double e = notes[i].start + notes[i].dur;
        if (e > end_t) end_t = e;
    }
    end_t += 1.0;                                /* 尾巴留 1 秒余音 */
    long total = (long)(end_t * SR);
    if (total <= 0 || total > (long)SR * 600) return jerr("曲子太长（上限 10 分钟）");

    float *buf = calloc((size_t)total, sizeof(float));
    if (!buf) return jerr("内存不足（曲子太长？）");

    for (int i = 0; i < nn; i++) {
        note_t *x = &notes[i];
        if (x->ch == 9) {
            add_noise(buf, total, x->start, x->dur, 0.35);
        } else if (x->ch == 1) {
            add_tone(buf, total, x->start, x->pitch, x->dur, 0.34, 1, 0.5, 0.0);
        } else {
            add_tone(buf, total, x->start, x->pitch, x->dur, 0.42, 0, 0.5, 0.006);
        }
    }

    char wav[900];
    if (midi_path && midi_path[0]) {
        snprintf(wav, sizeof(wav), "%s", midi_path);
    } else {
        resolve_path((out_name && out_name[0]) ? out_name : "music", wav, sizeof(wav));
    }
    char *dot = strrchr(wav, '.');
    if (dot && (!strcmp(dot, ".mid") || !strcmp(dot, ".midi"))) *dot = 0;
    strncat(wav, ".wav", sizeof(wav) - strlen(wav) - 1);
    mkdirs(wav);

    if (write_wav(wav, buf, total) != 0) {
        free(buf);
        return jerr("写 WAV 失败");
    }
    free(buf);

    /* WAV → MP3：板上 ffmpeg 有 mp3 编码器，比自己实现编码器实在 */
    char mp3[900];
    snprintf(mp3, sizeof(mp3), "%s", wav);
    dot = strrchr(mp3, '.');
    if (dot) { *dot = 0; }
    strncat(mp3, ".mp3", sizeof(mp3) - strlen(mp3) - 1);

    char cmd[2000];
    snprintf(cmd, sizeof(cmd),
        "/usr/bin/ffmpeg -y -loglevel error -i '%s' -codec:a libmp3lame -b:a 128k '%s' 2>&1",
        wav, mp3);
    int rc = system(cmd);

    struct stat st;
    long long mp3sz = 0;
    if (stat(mp3, &st) == 0) mp3sz = (long long)st.st_size;

    char text[1200];
    snprintf(text, sizeof(text),
        "**渲染完成**\n\n| 项 | 值 |\n|---|---|\n"
        "| 音符 | %d |\n| 时长 | %.1f 秒 |\n| 速度 | %.0f BPM |\n"
        "| WAV | `%s` |\n| MP3 | %s |\n",
        nn, end_t - 1.0, tempo, wav,
        mp3sz > 0 ? mp3 : "（ffmpeg 没生成，只有 WAV）");
    char ans[300];
    if (mp3sz > 0)
        snprintf(ans, sizeof(ans), "已合成 MP3（%d 个音符，%.1f 秒）：%s", nn, end_t - 1.0, mp3);
    else
        snprintf(ans, sizeof(ans), "已合成 WAV（%d 个音符，%.1f 秒）：%s", nn, end_t - 1.0, wav);
    (void)rc;
    return jpair(text, ans);
}

static char *op_render(const char *json) {
    char p[512] = {0}, out[512] = {0};
    jget(json, "path", p, sizeof(p));
    jget(json, "out", out, sizeof(out));
    double tempo = jget_num(json, "tempo", 160);

    /* 给了 path 又有 notes：直接按 notes 渲染；只给 path：读 MIDI */
    note_t *notes = malloc(sizeof(note_t) * MAX_NOTE);
    if (!notes) return NULL;

    int nn = 0;
    double beat = 60.0 / tempo;
    if (strstr(json, "\"notes\"") || strstr(json, "\"melody\"")) {
        nn = parse_notes(json, notes, MAX_NOTE, beat, 0);
    }
    char midi_path[768] = {0};
    if (p[0]) {
        resolve_path(p, midi_path, sizeof(midi_path));
        if (!nn && strstr(midi_path, ".mid")) {
            double b = beat;
            nn = read_midi(midi_path, notes, MAX_NOTE, &b);
            if (b > 0) beat = b;
        }
    }
    char *r = do_render(notes, nn, midi_path, out, 60.0 / beat);
    free(notes);
    return r;
}

static char *op_import(const char *json) {
    char p[512] = {0};
    jget(json, "path", p, sizeof(p));
    if (!p[0]) return jerr("缺少 path（要导入的 .mid）");
    char path[768];
    resolve_path(p, path, sizeof(path));

    note_t *notes = malloc(sizeof(note_t) * MAX_NOTE);
    if (!notes) return NULL;
    double beat = 0.5;
    int nn = read_midi(path, notes, MAX_NOTE, &beat);
    if (nn < 0) { free(notes); return jerr("读不了这个 MIDI（格式不对或文件坏）"); }
    if (nn == 0) { free(notes); return jerr("MIDI 里没有音符"); }

    char *r = do_render(notes, nn, path, NULL, 60.0 / beat);
    free(notes);
    return r;
}

/* ---------------- 入口 ---------------- */
static char *run(const char *json_in) {
    char op[32] = {0};
    jget(json_in, "op", op, sizeof(op));
    if (!op[0]) {
        if (strstr(json_in, "\"notes\"") || strstr(json_in, "\"melody\""))
            snprintf(op, sizeof(op), "midi");
        else if (strstr(json_in, "\"path\"") && strstr(json_in, ".mid"))
            snprintf(op, sizeof(op), "import");
        else
            snprintf(op, sizeof(op), "render");
    }

    if (!strcmp(op, "midi") || !strcmp(op, "make"))   return op_midi(json_in);
    if (!strcmp(op, "render") || !strcmp(op, "play")) return op_render(json_in);
    if (!strcmp(op, "import") || !strcmp(op, "wav"))  return op_import(json_in);

    return jerr("不支持的 op：midi / render / import");
}

static plugin_desc_t desc = {
    PLUGIN_API_VERSION,
    "music",
    "音乐（纯 C）：生成 MIDI(midi) / 合成 FC 音色 MP3(render) / 导入现成 MIDI 转 MP3(import)。"
    "notes 格式 [{\"pitch\":69,\"dur\":1}]，dur 以拍为单位；pitch 0 表示休止",
    "{\"op\":\"midi\",\"path\":\"/tmp/llama_agent/a.mid\",\"tempo\":160,"
    "\"notes\":[{\"pitch\":72,\"dur\":1},{\"pitch\":74,\"dur\":1}]}"
    "  或分轨 {\"op\":\"midi\",\"melody\":[...],\"bass\":[...],\"drums\":[...]}"
    "  或 {\"op\":\"render\",\"path\":\"/tmp/llama_agent/a.mid\"}"
    "  或 {\"op\":\"import\",\"path\":\"/tmp/llama_agent/x.mid\"}",
    run,
    "midi,music,render,import,音乐,作曲,合成"
};

plugin_desc_t *plugin_entry(void) { return &desc; }
