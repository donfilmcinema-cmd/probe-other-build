# -*- coding: utf-8 -*-
"""Работа со звуком внутри программы: чтение, запись, частота, длина, громкость,
волна для экрана, нарезка на фразы и разбор фраз для датасета."""
import math
from fractions import Fraction
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

EPS = 1e-12


class AudioError(Exception):
    pass


def info(path):
    try:
        i = sf.info(str(path))
    except Exception as ex:  # noqa: BLE001
        raise AudioError(f"Не удалось прочитать файл «{Path(path).name}»: {ex}")
    return {"sr": int(i.samplerate), "frames": int(i.frames), "channels": int(i.channels),
            "seconds": float(i.frames) / max(1, i.samplerate)}


def read(path, mono=False):
    """-> (массив [кадры, каналы] float32, частота)."""
    try:
        x, sr = sf.read(str(path), dtype="float32", always_2d=True)
    except Exception as ex:  # noqa: BLE001
        raise AudioError(f"Не удалось прочитать файл «{Path(path).name}»: {ex}")
    if mono:
        x = x.mean(axis=1, keepdims=True)
    return x, int(sr)


def write(path, x, sr, subtype="PCM_24"):
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 2 and x.shape[1] == 1:
        x = x[:, 0]
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak > 0.9995:                       # не даём файлу клипнуть при записи в целые числа
        x = x * (0.9995 / peak)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".part.wav")
    sf.write(str(tmp), x, int(sr), subtype=subtype)
    tmp.replace(path)


def resample(x, sr_from, sr_to):
    if sr_from == sr_to:
        return x
    fr = Fraction(int(sr_to), int(sr_from)).limit_denominator(1000)
    return resample_poly(x, fr.numerator, fr.denominator, axis=0).astype(np.float32)


def fit_length(x, n):
    """Ровно n кадров: короче — дополнить тишиной, длиннее — обрезать."""
    if len(x) == n:
        return x
    if len(x) > n:
        return x[:n]
    pad = np.zeros((n - len(x),) + x.shape[1:], dtype=x.dtype)
    return np.concatenate([x, pad], axis=0)


def frame_rms(mono, sr, win=0.05, hop=0.025):
    w = max(1, int(sr * win))
    h = max(1, int(sr * hop))
    n = 1 + max(0, (len(mono) - w) // h)
    if len(mono) < w:
        return np.array([math.sqrt(float(np.mean(mono ** 2)) + EPS)])
    idx = np.arange(n)[:, None] * h + np.arange(w)[None, :]
    return np.sqrt(np.mean(mono[idx] ** 2, axis=1) + EPS)


def active_rms(mono, sr):
    """Громкость по звучащим местам (тишина не считается)."""
    r = frame_rms(mono, sr)
    if r.size == 0:
        return 0.0
    thr = max(np.max(r) * 10 ** (-35 / 20), 1e-5)
    act = r[r > thr]
    return float(np.sqrt(np.mean(act ** 2))) if act.size else 0.0


def conform(src_path, out_path, ref_path, match_level=True, subtype="PCM_24"):
    """Приводит результат движка к исходнику: та же частота, ровно та же длина (кладётся в проект
    Samplitude с начала и совпадает по времени), по желанию — та же громкость звучащих мест."""
    ref = info(ref_path)
    x, sr = read(src_path)
    x = resample(x, sr, ref["sr"])
    x = fit_length(x, ref["frames"])
    if match_level:
        r, rsr = read(ref_path, mono=True)
        a = active_rms(r[:, 0], rsr)
        b = active_rms(x.mean(axis=1), ref["sr"])
        if a > 0 and b > 0:
            g = min(a / b, 10 ** (18 / 20))
            x = x * g
    write(out_path, x, ref["sr"], subtype)
    return out_path


def to_wav(src, dst, mono=False, sr=None):
    """Копия в WAV (float) — движок читает её без сюрпризов с MP3 и длинными путями."""
    x, s = read(src, mono=mono)
    if sr and sr != s:
        x = resample(x, s, sr)
        s = sr
    write(dst, x, s, "FLOAT")
    return dst


def peaks(path, n=1200, t0=None, t1=None):
    """Волна для экрана: n пар (минимум, максимум) по всему файлу или по отрезку [t0, t1] с."""
    x, sr = read(path, mono=True)
    m = x[:, 0]
    if t0 is not None and t1 is not None and t1 > t0:
        m = m[max(0, int(t0 * sr)):max(0, int(t1 * sr))]
    n = int(max(50, min(4000, n)))
    if len(m) == 0:
        return {"min": [0] * n, "max": [0] * n, "seconds": 0}
    edges = np.linspace(0, len(m), n + 1).astype(int)
    mn = np.zeros(n, dtype=np.float32)
    mx = np.zeros(n, dtype=np.float32)
    for i in range(n):
        a, b = edges[i], max(edges[i] + 1, edges[i + 1])
        seg = m[a:b]
        if seg.size:
            mn[i], mx[i] = seg.min(), seg.max()
    return {"min": [round(float(v), 3) for v in mn], "max": [round(float(v), 3) for v in mx],
            "seconds": len(m) / sr}


# ---------------------------------------------------------------- фразы для датасета

def split_phrases(mono, sr, min_len=1.2, max_len=12.0, min_gap=0.30, merge_gap=0.9, floor_db=-45.0):
    """Делит дорожку вокала на фразы по паузам. -> список (начало, конец) в кадрах."""
    hop = int(sr * 0.01)
    win = int(sr * 0.03)
    if len(mono) < win * 2:
        return []
    r = frame_rms(mono, sr, 0.03, 0.01)
    db = 20 * np.log10(r + EPS)
    loud = np.percentile(db, 95)
    quiet = np.percentile(db, 10)
    thr = max(floor_db, loud - 38, min(quiet + 8, loud - 20))   # без пауз в записи порог не выше «громко − 20 дБ»
    voiced = db > thr
    # заполняем короткие провалы (согласные, вдохи между словами)
    gap = int(min_gap / 0.01)
    v = voiced.copy()
    i = 0
    n = len(v)
    while i < n:
        if not v[i]:
            j = i
            while j < n and not v[j]:
                j += 1
            if 0 < i and j < n and (j - i) < gap:
                v[i:j] = True
            i = j
        else:
            i += 1
    # отрезки звучания
    segs = []
    i = 0
    while i < n:
        if v[i]:
            j = i
            while j < n and v[j]:
                j += 1
            segs.append([i, j])
            i = j
        else:
            i += 1
    # соседние куски с паузой короче merge_gap склеиваем в одну фразу (пока она не длиннее max_len)
    maxf = int(max_len / 0.01)
    mg = int(merge_gap / 0.01)
    merged = []
    for a, b in segs:
        if merged and a - merged[-1][1] < mg and b - merged[-1][0] <= maxf:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    segs = merged
    # длинные — режем по самому тихому месту, пока не уложатся
    out = []

    def cut(a, b):
        if b - a <= maxf:
            out.append((a, b))
            return
        lo = a + int(maxf * 0.35)
        hi = a + maxf
        k = lo + int(np.argmin(db[lo:hi]))
        cut(a, k)
        cut(k, b)

    for a, b in segs:
        cut(a, b)
    res = []
    pad = int(0.08 * sr)
    for a, b in out:
        s = max(0, a * hop - pad)
        e = min(len(mono), b * hop + win + pad)
        if (e - s) / sr >= min_len:
            res.append((s, e))
    return res


def f0_median(mono, sr):
    """Примерная средняя высота голоса во фразе (Гц) — для сортировки и охвата диапазона."""
    x = mono
    if sr != 16000:
        x = resample(mono[:, None], sr, 16000)[:, 0]
    s = 16000
    w = 1024
    h = 320
    if len(x) < w:
        return 0.0
    lo, hi = s // 1000, s // 70                       # 70…1000 Гц
    vals = []
    r = frame_rms(x, s, w / s, h / s)
    thr = np.max(r) * 0.25
    for k, i in enumerate(range(0, len(x) - w, h)):
        if k < len(r) and r[k] < thr:
            continue
        fr = x[i:i + w] * np.hanning(w)
        ac = np.fft.irfft(np.abs(np.fft.rfft(fr, 2 * w)) ** 2)[:w]
        if ac[0] <= 0:
            continue
        ac = ac / ac[0]
        seg = ac[lo:hi]
        j = int(np.argmax(seg))
        if seg[j] > 0.45:
            vals.append(s / (lo + j))
    return float(np.median(vals)) if len(vals) >= 3 else 0.0


def analyze(seg, sr):
    mono = seg if seg.ndim == 1 else seg.mean(axis=1)
    peak = float(np.max(np.abs(mono))) if mono.size else 0.0
    spec = np.abs(np.fft.rfft(mono[: min(len(mono), sr * 8)] * 1.0))
    freqs = np.fft.rfftfreq(min(len(mono), sr * 8), 1 / sr)
    centroid = float(np.sum(freqs * spec) / (np.sum(spec) + EPS)) if spec.size else 0.0
    hf = float(np.sum(spec[freqs > 8000] ** 2) / (np.sum(spec ** 2) + EPS)) if spec.size else 0.0
    clip = int(np.sum(np.abs(seg) >= 0.999))
    return {
        "rms_db": round(20 * math.log10(active_rms(mono, sr) + EPS), 1),
        "peak_db": round(20 * math.log10(peak + EPS), 1),
        "f0": round(f0_median(mono, sr), 1),
        "bright": round(centroid),
        "hf": round(hf * 100, 2),            # доля энергии выше 8 кГц, % — «стекло» и шипение
        "clip": clip > 3,
    }


def note_name(hz):
    if not hz or hz <= 0:
        return ""
    n = int(round(12 * math.log2(hz / 440.0))) + 57
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    return f"{names[n % 12]}{n // 12}"
