# -*- coding: utf-8 -*-
"""DZOD Executor, E2 — обработка после голоса вокалиста (48 кГц, моно): параметры Omnivocal, которых нет у моделей.

  Power (0–100, 65)     — сила: громкость ±6 дБ и яркость (полка 3 кГц ±2 дБ);
  Velocity → Power      — громкость каждой ноты от velocity (100 → 0 дБ, ±4,5 дБ на 27 единиц);
  Attack (0–100, 50)    — начало нот: меньше 50 — мягкий вход до 80 мс, больше — щелчок атаки до +6 дБ (40 мс);
  «слог!»               — твёрдая атака этой ноты (+6 дБ, 30 мс);
  «слог0»               — шёпот: голос ноты заменяется шумом с той же огибающей спектра (LPC);
  Air (0–100, 50)       — воздух: полка 7 кГц ±6 дБ, выше 50 — подмешивается шёпот-копия (дыхание в голосе);
  Breath (вкл, 40)      — вдохи перед фразами после пауз ≥ 0,35 с и на нотах «br»;
  Presence (0–100, 10)  — вокал «впереди»: компрессия (до 4:1) и подъём 3 кГц до +4 дБ;
  Output (±12 дБ, 0)    — выход; пики выше 0 дБFS мягко ограничиваются.
Кривые (E2b, время в с от начала MIDI): power, air — 0–100 % во времени (заменяют общие значения).
Манеры (отрезки): soft — Power −15, Air +25, мягкий вход нот; belt — Power +20, атака +15; shout — belt + насыщение
(крик/надрыв); falsetto — Air +15; whisper — шёпот (LPC) на отрезке; spoken — высота (делается до голоса), normal — ничего.
Переходы между манерами — кроссфейд fade (по умолчанию 50 мс).
Все шаги — numpy/scipy, без видеокарты."""
import numpy as np
from scipy import signal

DEFAULTS = {"power": 65.0, "velocity_power": True, "attack": 50.0, "air": 50.0, "breath": True, "breath_level": 40.0,
            "presence": 10.0, "output": 0.0}


def _db(x):
    return 10 ** (x / 20.0)


def _shelf(x, sr, f0, gain_db, high=True):
    """Полка второго порядка (RBJ)."""
    if abs(gain_db) < 0.05:
        return x
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * f0 / sr
    alpha = np.sin(w0) / 2 * np.sqrt(2)
    c = np.cos(w0)
    sA = 2 * np.sqrt(A) * alpha
    if high:
        b = [A * ((A + 1) + (A - 1) * c + sA), -2 * A * ((A - 1) + (A + 1) * c), A * ((A + 1) + (A - 1) * c - sA)]
        a = [(A + 1) - (A - 1) * c + sA, 2 * ((A - 1) - (A + 1) * c), (A + 1) - (A - 1) * c - sA]
    else:
        b = [A * ((A + 1) - (A - 1) * c + sA), 2 * A * ((A - 1) - (A + 1) * c), A * ((A + 1) - (A - 1) * c - sA)]
        a = [(A + 1) + (A - 1) * c + sA, -2 * ((A - 1) + (A + 1) * c), (A + 1) + (A - 1) * c - sA]
    return signal.lfilter(np.array(b) / a[0], np.array(a) / a[0], x)


def _peak(x, sr, f0, gain_db, q=0.9):
    if abs(gain_db) < 0.05:
        return x
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * f0 / sr
    alpha = np.sin(w0) / (2 * q)
    c = np.cos(w0)
    b = [1 + alpha * A, -2 * c, 1 - alpha * A]
    a = [1 + alpha / A, -2 * c, 1 - alpha / A]
    return signal.lfilter(np.array(b) / a[0], np.array(a) / a[0], x)


def whisperize(x, sr, order=24, frame=0.025, rng=None):
    """Шёпот из голоса: огибающая спектра каждого кадра (LPC) возбуждается белым шумом, громкость кадра та же."""
    rng = rng or np.random.default_rng(7)
    n = int(frame * sr)
    hop = n // 2
    win = np.hanning(n)
    out = np.zeros(len(x) + n)
    norm = np.zeros(len(x) + n)
    for s in range(0, max(1, len(x) - n + 1), hop):
        seg = x[s:s + n]
        if len(seg) < n:
            seg = np.pad(seg, (0, n - len(seg)))
        e = np.sqrt(np.mean(seg ** 2))
        if e < 1e-6:
            continue
        r = np.correlate(seg * win, seg * win, "full")[n - 1:n + order]
        r[0] *= 1.0001
        try:
            a = _levinson(r, order)
        except FloatingPointError:
            continue
        noise = rng.standard_normal(n)
        y = signal.lfilter([1.0], a, noise)
        ye = np.sqrt(np.mean(y ** 2)) or 1.0
        out[s:s + n] += y / ye * e * 0.7 * win
        norm[s:s + n] += win
    return (out / np.maximum(norm, 1e-3))[:len(x)]


def _levinson(r, order):
    a = np.zeros(order + 1)
    a[0] = 1.0
    err = r[0]
    for i in range(1, order + 1):
        acc = r[i] + np.dot(a[1:i], r[i - 1:0:-1])
        k = -acc / err
        a[1:i + 1] = a[1:i + 1] + k * a[i - 1::-1][:i]
        err *= (1 - k * k)
        if err <= 0:
            raise FloatingPointError
    return a


def _breath(n, sr, rng):
    """Синтетический вдох: шум в полосе 600–5000 Гц, нарастание 70 %, быстрый спад."""
    w = rng.standard_normal(n)
    sos = signal.butter(2, [600, 5000], btype="band", fs=sr, output="sos")
    w = signal.sosfilt(sos, w)
    k = np.linspace(0, 1, n)
    env = np.where(k < 0.7, (k / 0.7) ** 1.5, (1 - (k - 0.7) / 0.3) ** 2)
    w = w * env
    return w / (np.sqrt(np.mean(w ** 2)) or 1.0)


# тембр манеры (после голоса): полки/колокола (Гц, дБ), примесь шёпота, громкость, хрип
STYLE_TONE = {
    "soft":     {"lo": (220, -2.0), "hi": (2500, -6.0), "peak": None,           "air": 0.30, "gain": -2.0, "rasp": 0.0},
    "belt":     {"lo": (250, -2.0), "hi": (5000, 2.0),  "peak": (2600, 5.0, 0.9), "air": 0.0, "gain": 1.5, "rasp": 0.0},
    "shout":    {"lo": (250, -3.0), "hi": (6000, 1.0),  "peak": (2800, 6.0, 0.8), "air": 0.10, "gain": 0.0, "rasp": 0.35},
    "falsetto": {"lo": (450, -8.0), "hi": (6000, 3.0),  "peak": None,           "air": 0.25, "gain": -1.5, "rasp": 0.0},
}


def _style_tone(x, sr, name, rng):
    t = STYLE_TONE[name]
    y = _shelf(x, sr, t["lo"][0], t["lo"][1], high=False)
    y = _shelf(y, sr, t["hi"][0], t["hi"][1])
    if t["peak"]:
        y = _peak(y, sr, *t["peak"])
    if t["air"] > 0:
        y = y + whisperize(x, sr, rng=rng) * t["air"]
    if t["rasp"] > 0:
        # хрип: громкость дрожит шумом 40–110 Гц (неровные смыкания связок)
        sos = signal.butter(2, [40, 110], btype="band", fs=sr, output="sos")
        m = signal.sosfilt(sos, rng.standard_normal(len(x)))
        m = m / (np.std(m) or 1.0)
        y = y * (1 + t["rasp"] * np.clip(m, -2.5, 2.5) / 2.5)
    return y * _db(t["gain"])


STYLE_FX = {  # манера → (Power +, Air +, Attack +)
    "soft": (-15.0, 25.0, -25.0), "belt": (20.0, 0.0, 15.0), "shout": (20.0, 5.0, 25.0), "falsetto": (0.0, 15.0, -10.0),
}


def _style_weight(n, sr, styles, name):
    """0…1 по отсчётам: где действует манера name (с кроссфейдом fade)."""
    w = np.zeros(n)
    for x in styles or ():
        if x.get("style") != name:
            continue
        a, b = int(float(x["start"]) * sr), min(n, int(float(x["end"]) * sr))
        if b <= a:
            continue
        f = max(1, int(float(x.get("fade", 0.05)) * sr))
        seg = np.ones(b - a)
        k = min(f, (b - a) // 2)
        if k > 0:
            seg[:k] = np.linspace(0, 1, k)
            seg[-k:] = np.linspace(1, 0, k)
        w[a:b] = np.maximum(w[a:b], seg)
    return w


def _ctrl(n, sr, points, default):
    """Кривая [[t, v], …] → значения на каждый отсчёт (через сетку 200 Гц)."""
    if not points:
        return np.full(n, float(default))
    grid = np.arange(0, n / sr + 0.005, 0.005)
    pts = sorted((float(a), float(b)) for a, b in points)
    v = np.interp(grid, [a for a, _ in pts], [b for _, b in pts])
    return np.interp(np.arange(n) / sr, grid, v)


def _gain_curve(n, sr, notes, p, power_t=None):
    """Громкость по нотам: Power (или кривая Power) + Velocity → Power, переходы 20 мс."""
    pw = power_t if power_t is not None else np.full(n, float(p["power"]))
    g = _db((pw - 65.0) / 35.0 * 6.0)
    if p.get("velocity_power", True):
        for x in notes:
            if x["pitch"] <= 0:
                continue
            a, b = int(x["start"] * sr), min(n, int(x["end"] * sr))
            if b > a:
                g[a:b] *= _db(float(np.clip((x["vel"] - 100) / 27.0 * 4.5, -9, 4)))
    k = max(1, int(0.02 * sr))
    return np.convolve(g, np.ones(k) / k, mode="same")


def _onsets(notes):
    """Начала нот, которые поются «с атаки»: после паузы или с новым словом (не продолжение-мелизм)."""
    out, prev = [], None
    for x in notes:
        if x["pitch"] > 0 and (prev is None or prev["pitch"] <= 0 or abs(prev["end"] - x["start"]) > 0.02
                               or x["word"] != prev["word"] or x["flag"] == "attack"):
            out.append(x)
        prev = x
    return out


def process(x, sr, notes, opts=None, curves=None, styles=None):
    """x — моно float, sr — частота; notes — список из notes_timeline (start, end, pitch, vel, flag, word)."""
    p = dict(DEFAULTS)
    p.update({k: v for k, v in (opts or {}).items() if k in DEFAULTS and v is not None})
    x = np.asarray(x, dtype=np.float64).copy()
    n = len(x)
    rng = np.random.default_rng(11)
    curves = curves or {}
    power_t = _ctrl(n, sr, curves.get("power"), p["power"])
    air_t = _ctrl(n, sr, curves.get("air"), p["air"])
    att_t = np.full(n, float(p["attack"]))
    for name, (dp_, da, dat) in STYLE_FX.items():
        w = _style_weight(n, sr, styles, name)
        if w.any():
            power_t += w * dp_
            air_t += w * da
            att_t += w * dat
    power_t = np.clip(power_t, 0, 100)
    air_t = np.clip(air_t, 0, 100)
    att_t = np.clip(att_t, 0, 100)
    w_wh = _style_weight(n, sr, styles, "whisper")
    w_sh = _style_weight(n, sr, styles, "shout")
    # шёпот «слог0»
    for nt in notes:
        if nt["flag"] == "whisper" and nt["pitch"] > 0:
            a, b = int(nt["start"] * sr), min(n, int(nt["end"] * sr) + int(0.03 * sr))
            if b - a > int(0.05 * sr):
                w = whisperize(x[a:b], sr, rng=rng)
                f = min(int(0.015 * sr), (b - a) // 4)
                fade = np.ones(b - a)
                fade[:f] = np.linspace(0, 1, f)
                fade[-f:] = np.linspace(1, 0, f)
                x[a:b] = x[a:b] * (1 - fade) + w * fade
    # кисть Whisper: отрезок целиком — шёпотом
    if w_wh.any():
        idx = np.flatnonzero(w_wh)
        a, b = idx[0], idx[-1] + 1
        x[a:b] = x[a:b] * (1 - w_wh[a:b]) + whisperize(x[a:b], sr, rng=rng) * w_wh[a:b]
    # Power (кривая), Velocity → Power; яркость — полкой 3 кГц по средней силе
    x *= _gain_curve(n, sr, notes, p, power_t)
    x = _shelf(x, sr, 3000, (float(np.mean(power_t)) - 65.0) / 35.0 * 2.0)
    # Attack и «!»
    env = np.ones(n)
    for nt in _onsets(notes):
        a = int(nt["start"] * sr)
        att = float(att_t[min(a, n - 1)]) if n else 50.0
        if nt["flag"] == "attack":
            L = int(0.03 * sr)
            k = np.arange(min(L, n - a))
            env[a:a + len(k)] *= 1 + (_db(6) - 1) * (1 - k / L) ** 2
        elif att < 49.5:
            L = int((50 - att) / 50 * 0.08 * sr)
            k = np.arange(min(L, n - a))
            env[a:a + len(k)] *= 0.5 - 0.5 * np.cos(np.pi * k / max(L, 1))
        elif att > 50.5:
            L = int(0.04 * sr)
            k = np.arange(min(L, n - a))
            env[a:a + len(k)] *= 1 + (_db((att - 50) / 50 * 6) - 1) * (1 - k / L) ** 2
    x *= env
    # тембр манер (soft, belt, shout, falsetto) — на своих отрезках, с кроссфейдом
    for name in STYLE_TONE:
        w = _style_weight(n, sr, styles, name)
        if w.any():
            idx = np.flatnonzero(w)
            a, b = max(0, idx[0] - int(0.05 * sr)), min(n, idx[-1] + 1 + int(0.05 * sr))
            y = _style_tone(x[a:b], sr, name, rng)
            x[a:b] = x[a:b] * (1 - w[a:b]) + y * w[a:b]
    # Air (кривая): примесь шёпота выше 50 % и верх 7 кГц ±6 дБ во времени
    if np.any(air_t > 50.5):
        x = x + whisperize(x, sr, rng=rng) * np.clip((air_t - 50) / 50, 0, 1) * 0.18
    if np.any(np.abs(air_t - 50) > 0.5):
        sos = signal.butter(2, 7000, btype="high", fs=sr, output="sos")
        hi = signal.sosfiltfilt(sos, x)
        x = x + hi * (_db((air_t - 50) / 50 * 6.0) - 1.0)
    # кисть Shout: насыщение (надрыв) — мягкое ограничение с подъёмом 1–4 кГц, крик держит громкость
    if w_sh.any():
        sos = signal.butter(2, [1000, 4000], btype="band", fs=sr, output="sos")
        rms = np.sqrt(np.mean(x[np.abs(x) > 1e-4] ** 2)) if np.any(np.abs(x) > 1e-4) else 0.05
        drive = 6.0 / max(rms * 4, 1e-3)
        sat = np.tanh((x + 0.6 * signal.sosfilt(sos, x)) * drive) / np.tanh(drive * rms * 4) * rms * 2.2
        x = x * (1 - w_sh) + sat * w_sh
    # Breath: перед фразами после пауз и на «br»
    if p.get("breath", True) and float(p["breath_level"]) > 0:
        sung = [nt for nt in notes if nt["pitch"] > 0]
        rms = np.sqrt(np.mean(x[np.abs(x) > 1e-4] ** 2)) if np.any(np.abs(x) > 1e-4) else 0.05
        lvl = rms * _db(-30 + float(p["breath_level"]) / 100 * 18)          # 0 → −30 дБ, 100 → −12 дБ от голоса
        spots = []
        for i, nt in enumerate(sung):
            gap = nt["start"] - (sung[i - 1]["end"] if i else 0.0)
            if gap >= 0.35:
                d = min(0.42, gap - 0.06)
                spots.append((nt["start"] - 0.04 - d, d))
        for nt in notes:
            if nt["flag"] == "breath":
                spots.append((nt["start"], max(0.15, min(0.6, nt["end"] - nt["start"]))))
        for t0, d in spots:
            a = int(max(0, t0) * sr)
            m = min(int(d * sr), n - a)
            if m > int(0.08 * sr):
                x[a:a + m] += _breath(m, sr, rng) * lvl
    # Presence: компрессия + 3 кГц
    pr = float(p["presence"]) / 100.0
    if pr > 0.005:
        ratio = 1 + 3 * pr
        k = int(0.01 * sr)
        lev = np.sqrt(np.convolve(x ** 2, np.ones(k) / k, mode="same")) + 1e-9
        thr = np.percentile(lev[lev > 1e-4], 60) if np.any(lev > 1e-4) else 0.05
        over = np.maximum(lev / thr, 1.0)
        gr = over ** (1 / ratio - 1)
        # сглаживание: атака ~5 мс, спад ~80 мс
        b, a = signal.butter(1, 12, fs=sr)
        gr = signal.filtfilt(b, a, gr)
        x = x * gr
        x *= np.sqrt(ratio) ** 0.5          # примерная компенсация громкости
        x = _peak(x, sr, 3000, 4.0 * pr)
    # Output и мягкое ограничение пиков
    x *= _db(float(p["output"]))
    pk = np.max(np.abs(x)) if n else 0
    if pk > 0.98:
        x = np.tanh(x / 0.98) * 0.98
    return x.astype(np.float32)
