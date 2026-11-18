#!/usr/bin/env python3
"""FC (NES) 音源风格合成器 —— 直接输出 WAV，不依赖 MIDI 播放器的波表。

为什么不用 MIDI：MIDI 只记录"哪个音、多长、什么乐器号"，具体音色由播放器的
GM 波表决定。GM 的 "Square Lead" 是电子琴音色，跟 FC 的脉冲波完全不是一回事，
所以再怎么编曲也出不来赤影战士那种锋利感。

FC 的硬件只有四路：
    Pulse 1 / Pulse 2   方波，占空比 12.5% / 25% / 50%   → 旋律与和声
    Triangle            三角波                          → 低音
    Noise               噪声                            → 鼓
这里用 numpy 直接生成这四种波形，再叠 ADSR 包络，就是那个味道。

用法：
    python3 fc_synth.py out.wav            # 默认 32 小节 @160BPM
    python3 fc_synth.py out.wav 64 168     # 64 小节 @168BPM
"""
import sys
import wave

import numpy as np

SR = 32000          # 实测 22050 会砍掉方波高次谐波、听着发闷；32k 才够亮


# --------------------------------------------------------------- 波形
def note_freq(n):
    """MIDI 音高号 → 频率（A4=69=440Hz）"""
    return 440.0 * (2.0 ** ((n - 69) / 12.0))


def pulse_wave(t, freq, duty=0.50):
    """方波。duty=0.5 是厚实的标准方波 —— A/B 试听后选定的主旋律音色
    （12.5% 太薄，那是 FC 里给和声用的；25% 在方波上听感差别不明显）。

    必须去掉直流分量：窄占空时波形大部分时间是 -1，均值不为零，
    几十个音符叠起来会累积巨大 DC 偏移（实测 63% 能量堆在 0-10Hz）。
    """
    phase = np.mod(t * freq, 1.0)
    w = np.where(phase < duty, 1.0, -1.0)
    return w - (2.0 * duty - 1.0)


def triangle_wave(t, freq):
    """三角波。FC 的低音通道就是它，音色圆但穿透力强。"""
    phase = np.mod(t * freq, 1.0)
    return 2.0 * np.abs(2.0 * phase - 1.0) - 1.0


def adsr(n, sr, a=0.001, d=0.075, s=0.42, r=0.030):
    """打击型包络：瞬时起音 + 明显衰减 + 低保持。

    A/B 试听确认的参数：原来的 a=3ms/d=45ms/s=70% 拖太久，听着像持续的
    电子音；这个版本才有 FC 那种"拨弦/打击"的颗粒感。
    """
    a_n = max(1, int(a * sr))
    d_n = max(1, int(d * sr))
    r_n = max(1, int(r * sr))
    sus_n = max(0, n - a_n - d_n - r_n)
    e = np.concatenate([
        np.linspace(0.0, 1.0, a_n, endpoint=False),
        np.linspace(1.0, s, d_n, endpoint=False),
        np.full(sus_n, s),
        np.linspace(s, 0.0, r_n),
    ])
    if len(e) < n:
        e = np.concatenate([e, np.zeros(n - len(e))])
    return e[:n]


def add_tone(buf, sr, start, freq, dur, vol=0.5, wave="pulse", duty=0.125,
             vib=0.0):
    """往缓冲区叠一个音。vib 是颤音深度（0.006 ≈ FC 那种轻微抖）。

    注意 start 是**秒**，必须先乘 sr 换成采样下标 —— 早期版本忘了这步，
    结果所有音符都堆在开头几个采样里，听起来整首都是静音。
    """
    if freq <= 0 or dur <= 0:
        return
    s0 = int(round(start * sr))
    if s0 < 0 or s0 >= len(buf):
        return
    n = min(int(dur * sr), len(buf) - s0)
    if n <= 0:
        return
    t = np.arange(n) / sr
    f = freq
    if vib:
        f = freq * (1.0 + vib * np.sin(2 * np.pi * 5.5 * t))
    if wave == "pulse":
        w = pulse_wave(t, f, duty)
    elif wave == "triangle":
        w = triangle_wave(t, f)
    else:
        return
    buf[s0:s0 + n] += w * adsr(n, sr) * vol


def add_noise(buf, sr, start, dur, vol=0.35, decay=0.0018):
    """噪声通道：FC 的鼓。decay 越小越"啪"。"""
    s0 = int(round(start * sr))
    if s0 < 0 or s0 >= len(buf):
        return
    n = min(int(dur * sr), len(buf) - s0)
    if n <= 0:
        return
    rng = np.random.default_rng(s0 & 0xFFFF)
    w = rng.uniform(-1.0, 1.0, n)
    e = np.exp(-np.arange(n) / (decay * sr))
    buf[s0:s0 + n] += w * e * vol


def write_wav(path, buf, sr=SR):
    pcm = (np.clip(buf, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(path, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sr)
        fh.writeframes(pcm.tobytes())


# ---- 旋律素材见下方 SPARSE_*（用户实测：稀疏才听得清，密集版已删除）----

def render_notes(path, bars, tempo=160, sr=SR):
    """渲染**模型手写**的音符。

    bars = [{"notes": [{"pitch": 69, "dur": 0.5}, ...], "chord": "Am"}, ...]
    pitch 是 MIDI 音高号，dur 以拍为单位（0.5=八分音符）。

    伴奏（低音 + 鼓）由这里补上，因为模型管不了那么多轨；
    但**旋律完全是模型给定的**，工具不做任何"创作"。
    """
    beat = 60.0 / tempo
    bar_sec = beat * 4
    total = max(1, len(bars)) * bar_sec + 1.2
    buf = np.zeros(int(total * sr))

    root_map = {"Am": 57, "A": 57, "F": 53, "G": 55, "E": 52, "Em": 52,
                "Dm": 50, "C": 48, "D": 50}

    for bi, bar in enumerate(bars):
        t0 = bi * bar_sec
        chord = str(bar.get("chord") or "Am")
        root = root_map.get(chord, 57)

        # ---- 伴奏：低音（三角波）
        for i in range(8):
            n = root - 12 if i % 4 else root - 24
            add_tone(buf, sr, t0 + i * (bar_sec / 8.0), note_freq(n),
                     (bar_sec / 8.0) * 0.92, vol=0.24, wave="triangle")
        # ---- 伴奏：鼓
        for b in range(4):
            tb = t0 + b * beat
            add_noise(buf, sr, tb, 0.09, vol=0.28, decay=0.0009)
            if b % 2 == 1:
                add_noise(buf, sr, tb, 0.12, vol=0.24, decay=0.0016)
            add_noise(buf, sr, tb + beat / 2, 0.04, vol=0.11, decay=0.0004)

        # ---- 旋律：完全按模型写的来
        t = 0.0
        for n in (bar.get("notes") or []):
            try:
                pitch = int(n.get("pitch", 69))
                dur = float(n.get("dur", 0.5))
            except (TypeError, ValueError, AttributeError):
                continue
            if dur <= 0:
                continue
            if t >= 4.0:
                break
            dur = min(dur, 4.0 - t)
            add_tone(buf, sr, t0 + t * beat, note_freq(pitch),
                     dur * beat * 0.94, vol=0.52, wave="pulse",
                     duty=0.50, vib=0.0)
            t += dur

    peak = float(np.max(np.abs(buf))) or 1.0
    buf = buf / peak * 0.9
    write_wav(path, buf, sr)
    return {"path": path, "seconds": round(len(buf) / sr, 1),
            "bars": len(bars), "tempo": tempo}


# ---- 稀疏风格素材（用户实测确认的方向）----
# 每小节最多 3 个音、长音为主。之前每小节塞 8 个十六分音符 = 糊成噪音。
SPARSE_THEME = [
    ("Am", [(69, 2), (72, 2)]),
    ("F",  [(65, 1), (69, 1), (72, 2)]),
    ("G",  [(67, 2), (71, 2)]),
    ("E",  [(64, 1), (68, 1), (71, 2)]),
]
# B 段：整体移高一个八度、节奏打散，做对比
SPARSE_B = [
    ("Am", [(72, 1), (76, 1), (81, 2)]),
    ("F",  [(77, 2), (72, 2)]),
    ("G",  [(74, 1), (79, 1), (83, 2)]),
    ("E",  [(76, 2), (71, 2)]),
]
# 尾奏：只留骨架长音
SPARSE_OUT = [
    ("Am", [(69, 4)]),
    ("F",  [(65, 2), (69, 2)]),
    ("G",  [(67, 4)]),
    ("E",  [(64, 4)]),
]


def render_sparse_song(path, tempo=150, sr=SR):
    """稀疏风格 + 段落结构：intro - A - B - A' - outro。

    纪律（用户耳朵验证过的）：
      · 每小节不超过 3 个旋律音，二分/四分音符为主
      · 鼓只用 kick + snare，不加 hihat 十六分
      · 低音用二分音符（不用八分推进，那是"吵"的来源）
      · 和声只在 A' 段轻轻加一层
    """
    beat = 60.0 / tempo
    bar_sec = beat * 4

    layout = [("intro", 4), ("A", 8), ("B", 8), ("A2", 8), ("outro", 4)]
    seq = []
    for name, n in layout:
        seq.extend([name] * n)

    total = len(seq) * bar_sec + 1.2
    buf = np.zeros(int(total * sr))
    root_map = {"Am": 57, "F": 53, "G": 55, "E": 52}

    def put_phrase(t0, phrase, harmony=False):
        for pitch, dur in phrase:
            add_tone(buf, sr, t0, note_freq(pitch), dur * beat * 0.95,
                     vol=0.52, wave="pulse", duty=0.50)
            if harmony:                      # 低三度，音量压很低，只做厚度
                add_tone(buf, sr, t0, note_freq(pitch - 4), dur * beat * 0.95,
                         vol=0.14, wave="pulse", duty=0.25)
            t0 += dur * beat

    for bi, sec in enumerate(seq):
        t0 = bi * bar_sec
        idx = bi % 4

        if sec == "intro":
            # 前奏：只有低音长音 + 每两拍一下鼓
            root = root_map[SPARSE_THEME[idx][0]]
            add_tone(buf, sr, t0, note_freq(root - 12), bar_sec * 0.95,
                     vol=0.26, wave="triangle")
            add_noise(buf, sr, t0, 0.09, vol=0.26, decay=0.0009)
            add_noise(buf, sr, t0 + beat * 2, 0.09, vol=0.26, decay=0.0009)
            continue

        if sec == "outro":
            chord, phrase = SPARSE_OUT[idx]
            root = root_map[chord]
            add_tone(buf, sr, t0, note_freq(root - 12), bar_sec * 0.95,
                     vol=0.24, wave="triangle")
            put_phrase(t0, phrase)
            add_noise(buf, sr, t0, 0.09, vol=0.22, decay=0.0009)
            continue

        if sec == "B":
            chord, phrase = SPARSE_B[idx]
        else:
            chord, phrase = SPARSE_THEME[idx]
        root = root_map[chord]

        # 低音：二分音符，干净
        add_tone(buf, sr, t0, note_freq(root - 12), beat * 1.9,
                 vol=0.26, wave="triangle")
        add_tone(buf, sr, t0 + beat * 2, note_freq(root - 12), beat * 1.9,
                 vol=0.24, wave="triangle")
        # 鼓：kick 1/3 拍 + snare 2/4 拍，不要 hihat
        for b in (0, 1, 2, 3):
            if b % 2 == 0:
                add_noise(buf, sr, t0 + b * beat, 0.09, vol=0.28, decay=0.0009)
            else:
                add_noise(buf, sr, t0 + b * beat, 0.11, vol=0.24, decay=0.0016)
        # 旋律
        put_phrase(t0, phrase, harmony=(sec == "A2"))

    peak = float(np.max(np.abs(buf))) or 1.0
    buf = buf / peak * 0.9
    write_wav(path, buf, sr)
    return {"path": path, "seconds": round(len(buf) / sr, 1),
            "bars": len(seq), "tempo": tempo}


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "fc.wav"
    b = int(sys.argv[2]) if len(sys.argv) > 2 else 32
    t = int(sys.argv[3]) if len(sys.argv) > 3 else 160
    info = render_sparse_song(out, t)
    print("wrote %s  %s 秒  %d 小节  %d BPM" % (out, info["seconds"], b, t))
