# -*- coding: utf-8 -*-
"""DZOD Vocalist 0.8.0 — МЕРИЛО качества перепевки (письмо sound 15.11 23:00). Окружение Executor:

    python quality_worker.py job.json

Задание: {"source": исходник.wav, "take": дубль.wav, "lyrics": текст|"" , "language": "auto"|"en"|"ru",
          "out_json": путь, "whisper": папка модели Whisper (transformers), "device": "cuda"|"cpu",
          "align_model_dir": папка модели выравнивания (как у «MIDI из вокала»)}
Что меряет (исходник против дубля, одной длины — перепевка RVC длину не меняет):
1. Разборчивость — распознавание речи (Whisper) исходника и дубля; WER против текста автора (если есть)
   и против распознанного исходника; слова «съедено / искажено» с временем.
2. Яркость по словам — доля энергии >2 и >4 кГц в каждом слове, дубль / исходник.
3. Динамика — отклонение огибающей окнами 20 мс после выравнивания общего уровня (медиана, p90).
4. Артефакты — всплески ВЧ (кадры 20 мс), щелчки (1 мс), «металл» (спектральная плоскость гласных).
Цели приёмки: WER ≤ исходник + 2 п.п.; яркость ≥ 80 %; динамика 20 мс ≤ 1,5 дБ; 0 щелчков.
Строки: @@P <доля> <текст> / @@R <json> / @@E <текст>."""
import json
import os
import re
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def say(kind, payload):
    sys.stdout.write(f"@@{kind} {payload}\n")
    sys.stdout.flush()


TARGET = {"wer_pp": 2.0, "bright": 0.80, "bright_max": 1.50, "env_db": 1.5, "clicks": 0, "wer_sane": 50.0}


# ------------------------------------------------------------------ текст
def norm_words(text):
    t = (text or "").lower().replace("ё", "е").replace("’", "'")
    t = re.sub(r"\[[^\]]*\]|\{[^}]*\}", " ", t)
    return [w.strip("'") for w in re.findall(r"[a-zа-я0-9']+", t) if w.strip("'")]


def edit_ops(ref, hyp):
    """Выравнивание слов (Левенштейн) → [(op, i_ref|None, j_hyp|None)], op: ok / sub / del / ins."""
    n, m = len(ref), len(hyp)
    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            c = 0 if ref[i - 1] == hyp[j - 1] else 1
            d[i][j] = min(d[i - 1][j - 1] + c, d[i - 1][j] + 1, d[i][j - 1] + 1)
    i, j, ops = n, m, []
    while i > 0 or j > 0:
        if i > 0 and j > 0 and d[i][j] == d[i - 1][j - 1] + (0 if ref[i - 1] == hyp[j - 1] else 1):
            ops.append(("ok" if ref[i - 1] == hyp[j - 1] else "sub", i - 1, j - 1))
            i, j = i - 1, j - 1
        elif i > 0 and d[i][j] == d[i - 1][j] + 1:
            ops.append(("del", i - 1, None))
            i -= 1
        else:
            ops.append(("ins", None, j - 1))
            j -= 1
    return ops[::-1]


def wer(ref, hyp):
    if not ref:
        return None
    ops = edit_ops(ref, hyp)
    return round(100.0 * sum(1 for o in ops if o[0] != "ok") / len(ref), 1)


# ------------------------------------------------------------------ распознавание
def asr(model_dir, x16, lang, device):
    """Whisper (transformers): слова с временем. Длинная запись — кусками по 30 с."""
    import torch
    from transformers import pipeline
    dt = torch.float16 if device.startswith("cuda") else torch.float32
    pipe = pipeline("automatic-speech-recognition", model=model_dir, torch_dtype=dt, device=device)
    kw = {"task": "transcribe"}
    if lang in ("en", "ru"):
        kw["language"] = "english" if lang == "en" else "russian"
    try:
        r = pipe(x16.astype("float32"), chunk_length_s=30, batch_size=4, return_timestamps="word", generate_kwargs=kw)
        chunks = r.get("chunks") or []
    except Exception as ex:  # noqa: BLE001 — модель без голов выравнивания: время по фразам
        say("P", f"0.0 (время слов — по фразам: {str(ex)[:80]})")
        r = pipe(x16.astype("float32"), chunk_length_s=30, batch_size=4, return_timestamps=True, generate_kwargs=kw)
        chunks = []
        for c in r.get("chunks") or []:
            ws = norm_words(c["text"])
            a, b = c["timestamp"]
            b = b if b is not None else a + 1.0
            for k, w in enumerate(ws):
                chunks.append({"text": w, "timestamp": (a + (b - a) * k / len(ws), a + (b - a) * (k + 1) / len(ws))})
    out = []
    for c in chunks:
        for w in norm_words(c.get("text", "")):
            a, b = c.get("timestamp") or (None, None)
            out.append({"w": w, "t0": None if a is None else round(float(a), 3),
                        "t1": None if b is None else round(float(b), 3)})
    del pipe
    if device.startswith("cuda"):
        torch.cuda.empty_cache()
    return {"text": r.get("text", "").strip(), "words": out}


# ------------------------------------------------------------------ звук
def hf_share(S, fr, f):
    tot = S[fr > 60].sum()
    return float(S[fr > f].sum() / tot) if tot > 0 else 0.0


def spec(v, sr, a, b):
    import numpy as np
    seg = v[max(0, int(a * sr)):int(b * sr)]
    if len(seg) < 256:
        return None, None
    S = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
    return S, np.fft.rfftfreq(len(seg), 1 / sr)


def envelope_dev(x, y, sr):
    import numpy as np
    from scipy.ndimage import uniform_filter1d
    w, h = int(0.02 * sr), int(0.01 * sr)
    ex = 10 * np.log10(uniform_filter1d(x * x, w)[::h] + 1e-12)
    ey = 10 * np.log10(uniform_filter1d(y * y, w)[::h] + 1e-12)
    loud = (ex > ex.max() - 35) & (ey > ey.max() - 45)
    if loud.sum() < 10:
        return None
    d = ey[loud] - ex[loud]
    d = np.abs(d - np.median(d))
    return {"median_db": round(float(np.median(d)), 2), "p90_db": round(float(np.percentile(d, 90)), 2),
            "frames": int(loud.sum())}


def artifacts(x, y, sr):
    """Всплески ВЧ: кадр 20 мс, доля >4 кГц втрое выше соседей (±0,25 с) и вдвое с лишним выше исходника.
    Щелчки: энергия >6 кГц в окне 1 мс на 20 дБ выше соседних 30 мс и без такого же скачка в исходнике.
    «Металл»: спектральная плоскость в громких кадрах, дубль / исходник (больше 1 — шумнее/звонче исходника)."""
    import numpy as np
    from scipy.ndimage import median_filter, uniform_filter1d
    from scipy.signal import butter, sosfilt
    n, h = int(0.02 * sr), int(0.01 * sr)
    win = np.hanning(n)

    def frames(v):
        k = (len(v) - n) // h
        if k < 5:
            return None, None, None
        idx = np.arange(n)[None, :] + h * np.arange(k)[:, None]
        S = np.abs(np.fft.rfft(v[idx] * win, axis=1)) ** 2
        fr = np.fft.rfftfreq(n, 1 / sr)
        tot = S[:, fr > 60].sum(1) + 1e-12
        hf4 = S[:, fr > 4000].sum(1) / tot
        band = S[:, (fr > 300) & (fr < 8000)] + 1e-12
        flat = np.exp(np.mean(np.log(band), 1)) / np.mean(band, 1)
        db = 10 * np.log10(tot)
        return hf4, flat, db

    hx, fx, dx = frames(x)
    hy, fy, dy = frames(y)
    res = {"bursts": [], "harsh": [], "clicks": [], "metal_ratio": None}
    if hy is None:
        return res
    m = min(len(hx), len(hy))
    hx, fx, dx, hy, fy, dy = hx[:m], fx[:m], dx[:m], hy[:m], fy[:m], dy[:m]
    loc = median_filter(hy, size=51, mode="nearest")
    bad = (hy > np.maximum(3 * loc, 0.25)) & (hy > 2.5 * hx + 0.05) & (dy > dy.max() - 40)
    k = 0
    while k < m:
        if bad[k]:
            e = k
            while e < m and bad[e]:
                e += 1
            ev = {"t": round(float(k * h / sr), 3), "dur": round((e - k) * h / sr, 3),
                  "hf4k": round(float(hy[k:e].max()), 2), "src_hf4k": round(float(hx[k:e].max()), 2)}
            # 0.8.3: в исходнике здесь тоже шипящая (доля >4 кГц ≥ 15 %) — это не призвук, а согласная дубля
            # резче исходника (дело деэссера); иначе — всплеск ВЧ на ровном месте
            (res["harsh"] if ev["src_hf4k"] >= 0.15 else res["bursts"]).append(ev)
            k = e
        else:
            k += 1
    loud = (dx > dx.max() - 30) & (dy > dy.max() - 30)
    if loud.sum() > 10:
        res["metal_ratio"] = round(float(np.median(fy[loud]) / max(1e-9, np.median(fx[loud]))), 2)
    # щелчки
    if sr > 13000:
        sos = butter(4, 6000, btype="high", fs=sr, output="sos")
        w1, w30 = max(4, int(0.001 * sr)), int(0.03 * sr)

        def spikes(v):
            e = uniform_filter1d(sosfilt(sos, v) ** 2, w1)[::w1]          # энергия >6 кГц, шаг 1 мс
            base = median_filter(e, size=31, mode="nearest") + 1e-12    # фон соседних 30 мс (медиана — без самого щелчка)
            return 10 * np.log10(e / base + 1e-12), e

        sy, ey = spikes(y)
        sx, _ = spikes(x[:len(y)] if len(x) >= len(y) else np.pad(x, (0, len(y) - len(x))))
        m2 = min(len(sy), len(sx))
        sy, sx, ey = sy[:m2], sx[:m2], ey[:m2]
        lvl = ey.max() * 10 ** (-50 / 10)
        cand = np.flatnonzero((sy > 15) & (sx < 8) & (ey > lvl))
        last = -10 ** 9
        for c in cand:
            if c - last > 50:
                res["clicks"].append({"t": round(float(c * w1 / sr), 3), "db": round(float(sy[c]), 1)})
            last = c
        res["clicks"] = res["clicks"][:200]
    res["bursts"] = res["bursts"][:200]
    res["harsh"] = res["harsh"][:200]
    return res


# ------------------------------------------------------------------ главное
def run(job):
    import numpy as np
    import soundfile as sf
    import transcribe_worker as T
    from math import gcd
    from scipy.signal import resample_poly
    t_all = time.time()
    y, sr = sf.read(job["take"], dtype="float64", always_2d=True)
    y = y.mean(1)
    x, xsr = sf.read(job["source"], dtype="float64", always_2d=True)
    x = x.mean(1)
    if xsr != sr:
        g = gcd(int(sr), int(xsr))
        x = resample_poly(x, int(sr) // g, int(xsr) // g)
    n = min(len(x), len(y))
    x, y = x[:n], y[:n]
    g16 = gcd(int(sr), 16000)
    x16 = resample_poly(x, 16000 // g16, int(sr) // g16).astype(np.float32)
    y16 = resample_poly(y, 16000 // g16, int(sr) // g16).astype(np.float32)
    lyrics = (job.get("lyrics") or "").strip()
    lang = job.get("language") or "auto"
    if lang == "auto" and lyrics:
        lang = T.detect_lang(lyrics)
    dev = job.get("device")
    if not dev:
        import torch
        dev = "cuda:0" if torch.cuda.is_available() else "cpu"

    say("P", "0.05 Распознавание исходника…")
    a_src = asr(job["whisper"], x16, lang, dev)
    say("P", "0.40 Распознавание дубля…")
    a_take = asr(job["whisper"], y16, lang, dev)
    hyp_s = [w["w"] for w in a_src["words"]]
    hyp_t = [w["w"] for w in a_take["words"]]

    # опорные слова с временем: текст автора, выровненный по исходнику; без текста — распознанный исходник
    say("P", "0.75 Слова по времени…")
    ref = []
    if lyrics:
        wt = T.words_of(lyrics)
        tj = dict(job)
        tj.setdefault("device", dev)
        letters = T.align(x16, wt, lang if lang in ("en", "ru") else T.detect_lang(lyrics), tj)
        for (w, li, ca, cb), lt in zip(T.words_pos(lyrics), letters):
            known = [q for q in lt if q]
            for part in (norm_words(w.replace("-", "")) or [w.lower()]):
                ref.append({"w": part, "line": li, "t0": known[0][0] if known else None,
                            "t1": known[-1][1] if known else None})
    else:
        ref = [dict(w) for w in a_src["words"]]
    rw = [r["w"] for r in ref]

    # 0.8.3: текст автора может покрывать только часть песни (T022: 4 строки из 8) — сверяем только участок,
    # покрытый текстом по выравниванию (±1 с); распознанное вне участка — «текста нет», в ошибки не идёт
    # Участок — по распознанному исходнику: где в нём нашлись слова текста (выравнивание текста по звуку
    # растягивает короткий текст на всю песню). Время слов текста, найденных в исходнике, — тоже оттуда.
    cover = None
    if lyrics:
        sw = a_src["words"]
        hit = [(i, j) for op, i, j in edit_ops(rw, [w["w"] for w in sw]) if op == "ok" and sw[j]["t0"] is not None]
        if len(hit) >= max(2, len(rw) // 4):
            for i, j in hit:
                if sw[j]["t1"] is not None:
                    ref[i]["t0"], ref[i]["t1"] = sw[j]["t0"], sw[j]["t1"]
            t_a = sw[hit[0][1]]["t0"]
            t_b = sw[hit[-1][1]]["t1"] or sw[hit[-1][1]]["t0"]
        else:
            tt = [r["t0"] for r in ref if r["t0"] is not None] + [r["t1"] for r in ref if r["t1"] is not None]
            t_a, t_b = (min(tt), max(tt)) if tt else (None, None)
        if t_a is not None:
            cover = [max(0.0, t_a - 1.0), min(n / sr, t_b + 1.0)]

            def inside(ws):
                return [w["w"] for w in ws if w["t0"] is None or (cover[0] <= w["t0"] <= cover[1])]
            hyp_s, hyp_t = inside(a_src["words"]), inside(a_take["words"])
    wer_take = wer(rw, hyp_t)
    wer_src = wer(rw, hyp_s) if lyrics else 0.0
    # ошибки дубля, которых нет в исходнике
    ops_t, ops_s = edit_ops(rw, hyp_t), edit_ops(rw, hyp_s)
    src_bad = {i for op, i, j in ops_s if op in ("sub", "del") and i is not None} if lyrics else set()
    problems = []
    for op, i, j in ops_t:
        if op in ("sub", "del") and i is not None:
            r = ref[i]
            problems.append({"word": r["w"], "t0": None if r["t0"] is None else round(r["t0"], 2),
                             "t1": None if r["t1"] is None else round(r["t1"], 2),
                             "kind": "съедено" if op == "del" else "искажено",
                             "heard": None if j is None else hyp_t[j],
                             "also_in_source": i in src_bad})

    # яркость по словам
    say("P", "0.85 Яркость, динамика, артефакты…")
    bright = []
    for r in ref:
        if r["t0"] is None or r["t1"] is None or r["t1"] - r["t0"] < 0.06:
            continue
        Sx, fr = spec(x, sr, r["t0"], r["t1"])
        Sy, _ = spec(y, sr, r["t0"], r["t1"])
        if Sx is None:
            continue
        s2, t2 = hf_share(Sx, fr, 2000), hf_share(Sy, fr, 2000)
        s4, t4 = hf_share(Sx, fr, 4000), hf_share(Sy, fr, 4000)
        bright.append({"word": r["w"], "t0": round(r["t0"], 2), "t1": round(r["t1"], 2),
                       "src_hf2k": round(s2, 3), "take_hf2k": round(t2, 3), "src_hf4k": round(s4, 3),
                       "take_hf4k": round(t4, 3), "ratio": round(t2 / s2, 2) if s2 > 0.05 else None})
    ratios = sorted(b["ratio"] for b in bright if b["ratio"] is not None)
    b_med = round(float(np.median(ratios)), 2) if ratios else None
    worst = sorted([b for b in bright if b["ratio"] is not None], key=lambda b: b["ratio"])[:10]
    brightest = sorted([b for b in bright if b["ratio"] is not None], key=lambda b: -b["ratio"])[:10]
    env = envelope_dev(x, y, sr)
    art = artifacts(x, y, sr)

    own = [p for p in problems if not p["also_in_source"]]
    checks = {
        "wer": {"take": wer_take, "source": wer_src, "against": "текст автора" if lyrics else "распознанный исходник",
                "covered": cover, "words": len(rw),
                # WER исходника > 50 % против текста — ошибка самой сверки (не тот текст, не тот язык); высокий WER
                # одного дубля — настоящий результат (дубль неразборчив), не «сверка не удалась»
                "suspect": bool(lyrics) and (wer_src or 0) > TARGET["wer_sane"],
                "ok": wer_take is not None and wer_take <= (wer_src or 0.0) + TARGET["wer_pp"] + (0 if lyrics else 3.0)
                and (wer_src or 0) <= TARGET["wer_sane"]},
        "brightness": {"median_ratio": b_med, "too_bright": b_med is not None and b_med > TARGET["bright_max"],
                       "ok": b_med is not None and TARGET["bright"] <= b_med <= TARGET["bright_max"]},
        "dynamics": {**(env or {}), "ok": env is not None and env["median_db"] <= TARGET["env_db"]},
        "clicks": {"count": len(art["clicks"]), "ok": len(art["clicks"]) <= TARGET["clicks"]},
    }
    res = {
        "version": job.get("version"), "source": job["source"], "take": job["take"], "language": lang,
        "seconds": round(n / sr, 2), "time_s": round(time.time() - t_all, 1), "target": TARGET,
        "checks": checks, "passed": all(c["ok"] for c in checks.values()),
        "problems": own, "problems_also_in_source": [p for p in problems if p["also_in_source"]],
        "brightness_worst": worst, "brightness_brightest": brightest, "brightness_words": bright,
        "bursts": art["bursts"], "harsh": art["harsh"], "clicks": art["clicks"], "metal_ratio": art["metal_ratio"],
        "asr_source": a_src["text"], "asr_take": a_take["text"],
        "ref_words": [{"w": r["w"], "t0": None if r["t0"] is None else round(r["t0"], 3),
                       "t1": None if r["t1"] is None else round(r["t1"], 3)} for r in ref],
        "lyrics_used": bool(lyrics),
    }
    json.dump(res, open(job["out_json"], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    say("P", "1.00 Готово.")
    return {"out_json": job["out_json"], "passed": res["passed"], "checks": checks, "problems": len(own)}


def run_score(job):
    """0.8.2: оценка вариантов места по распознаванию. items: [{"wav", "t0", "t1", "expect": [слова], "target": слово}]
    → для каждого: что распознано в отрезке, WER против ожидаемых слов, найдено ли нужное слово."""
    import numpy as np
    import soundfile as sf
    import torch
    from math import gcd
    from scipy.signal import resample_poly
    from transformers import pipeline
    dev = job.get("device") or ("cuda:0" if torch.cuda.is_available() else "cpu")
    dt = torch.float16 if dev.startswith("cuda") else torch.float32
    pipe = pipeline("automatic-speech-recognition", model=job["whisper"], torch_dtype=dt, device=dev)
    lang = job.get("language")
    kw = {"task": "transcribe"}
    if lang in ("en", "ru"):
        kw["language"] = "english" if lang == "en" else "russian"
    cache, out = {}, []
    items = job.get("items") or []
    for k, it in enumerate(items):
        if k % 5 == 0:
            say("P", f"{0.05 + 0.9 * k / max(1, len(items)):.3f} Оценка вариантов: {k + 1} из {len(items)}…")
        if it["wav"] not in cache:
            y, sr = sf.read(it["wav"], dtype="float64", always_2d=True)
            cache.clear()
            cache[it["wav"]] = (y.mean(1), sr)
        y, sr = cache[it["wav"]]
        seg = y[max(0, int(it["t0"] * sr)):int(it["t1"] * sr)]
        g = gcd(int(sr), 16000)
        x16 = resample_poly(seg, 16000 // g, int(sr) // g).astype(np.float32)
        txt = pipe(x16, generate_kwargs=kw).get("text", "") if len(x16) > 1600 else ""
        hyp = norm_words(txt)
        exp = [w for w in it.get("expect") or []]
        out.append({"text": txt.strip(), "wer": wer(exp, hyp) if exp else None,
                    "has_target": (it.get("target") or "") in hyp})
    return {"items": out}


def main():
    try:
        job = json.load(open(sys.argv[1], encoding="utf-8"))
        if job.get("mode") == "score":
            say("R", json.dumps(run_score(job), ensure_ascii=False))
            return
        say("R", json.dumps(run(job), ensure_ascii=False))
    except Exception as ex:  # noqa: BLE001
        traceback.print_exc()
        say("E", f"Проверка качества: {ex}")
        sys.exit(1)


if __name__ == "__main__":
    main()
