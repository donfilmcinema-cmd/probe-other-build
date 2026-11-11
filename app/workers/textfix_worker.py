# -*- coding: utf-8 -*-
"""DZOD Vocalist — «Перепевка с текстом» (исправление съеденных слов и замена слов). Среда Executor.

    python textfix_worker.py job.json

Шаги (job["step"]):
  prepare — выравнивание текста по исходнику, высота (RMVPE), план пометок, MIDI подсказки (для executor_worker);
            ответ: plan.json
  content — «звук для содержания»: исходник, где только помеченные места заменены подсказкой (спетой SoulX по MIDI
            подсказки); куски-«окна» вокруг пометок для перепевки (исходник + содержание)
  piper   — подсказка речью Piper (русский; или английский без SoulX): вместо пения SoulX по MIDI подсказки
  splice  — готовая перепевка (дубль) + перепетые окна → дубль, где изменены только помеченные места

Строки для программы:  @@P <доля> <текст>  /  @@R <json>  /  @@E <текст>
"""
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PAD = 1.0          # с: окно перепевки вокруг пометки (контекст для RVC)
XF = 0.03          # с: кроссфейд стыка
CTX_GAP = 0.8      # с: соседнее слово дальше этого — не поётся в подсказке


def say(kind, payload):
    sys.stdout.write(f"@@{kind} {payload}\n")
    sys.stdout.flush()


# ------------------------------------------------------------------ prepare
def plan_marks(words, marks, f0_t, f0_midi, total, db=None, fps=100.0):
    """words — выравнивание (align_out["words"]), marks — [{from, to, replace?}] по символам текста.
    db — громкость по кадрам (дБ, 10 мс) — для поиска «дыры» на месте съеденного слова.
    Возвращает список пометок: слова, цель [t0, t1], текст для пения, соседи для подсказки."""
    import numpy as np
    sc = sorted(w["score"] for w in words if w.get("score") is not None)
    med = sc[len(sc) // 2] if sc else 0.0

    def reliable(k):
        w = words[k]
        if w["t0"] is None:
            return False
        if w["t1"] - w["t0"] < 0.03 * len(w["text"]) + 0.06:      # слово сжато в пару кадров — выравнивание не нашло
            return False
        return not (w.get("score") is not None and med > 0 and w["score"] < 0.3 * med)

    found = [k for k in range(len(words)) if reliable(k)]

    def holes(lo, hi):
        """Провалы громкости между lo и hi (съеденное место): ниже громкой части записи на 20 дБ, от 0,12 с."""
        if db is None:
            return []
        ref = float(np.percentile(db[np.isfinite(db)], 85))
        a_, b_ = int(lo * fps), min(len(db), int(hi * fps))
        res, k = [], a_
        while k < b_:
            if db[k] < ref - 20:
                m = k
                while m < b_ and db[m] < ref - 20:
                    m += 1
                if (m - k) / fps >= 0.12 and k > a_ and m < b_:   # дыра внутри, а не пауза до/после соседей
                    res.append((k / fps, m / fps))
                k = m
            else:
                k += 1
        return res

    out = []
    for mk in marks:
        a, b = int(mk["from"]), int(mk["to"])
        wi = [k for k, w in enumerate(words) if w["from"] < b and w["to"] > a]
        if not wi:
            out.append({"mark": mk, "found": False, "why": "пометка не попала ни на одно слово текста"})
            continue
        i0, i1 = wi[0], wi[-1]
        prev = [k for k in found if k < i0]
        nxt = [k for k in found if k > i1]
        lo = words[prev[-1]]["t1"] if prev else 0.0
        hi = words[nxt[0]]["t0"] if nxt else total
        plausible = all(words[k]["t0"] is not None and words[k]["t1"] - words[k]["t0"] >= 0.03 * len(words[k]["text"]) + 0.06
                        for k in wi)
        if all(reliable(k) for k in wi) or (mk.get("replace") and plausible):
            # замена: слово в записи целое — его место верное, даже если уверенность выравнивания низкая
            t0, t1 = words[i0]["t0"], words[i1]["t1"]
            where = "found"
            # помечено — значит, в этом месте звук испорчен: выравнивание находит только остаток слова.
            # Слово тянется до следующего слова (если оно ближе 1 с — дальше уже пауза). Назад — нет: конец
            # предыдущего слова выравнивание ставит рано (долгая гласная поётся дольше) — можно задеть его гласную
            # до следующего слова текста (даже ненадёжного — через слово не перескакиваем)
            nn = i1 + 1
            nt = words[nn]["t0"] if nn < len(words) and words[nn]["t0"] is not None and words[nn]["t0"] > t1 else (
                words[nxt[0]]["t0"] if nxt else None)
            if nt is not None and 0 < nt - t1 < 1.0:
                t1 = nt - 0.02
        else:
            hs = holes(lo, hi)
            if hs:
                # съеденное слово — в провал громкости между надёжными соседями (самый длинный)
                h0, h1 = max(hs, key=lambda h: h[1] - h[0])
                t0, t1 = h0 - 0.03, h1 + 0.03
                where = "hole"
            else:
                # провала нет: промежуток между надёжными соседями
                need = sum(0.09 * len(words[k]["text"]) for k in wi) + 0.1
                if hi - lo > need * 1.6:          # промежуток длинный: слово — ближе к предыдущему (паузы чаще после)
                    hi = lo + need
                t0, t1 = lo, hi
                where = "gap"
        if t1 - t0 < 0.08:
            t0, t1 = t0 - 0.04, t1 + 0.04
        t0, t1 = max(0.0, t0), min(total, t1)
        text = mk.get("replace") or " ".join(words[i]["text"] for i in wi)
        ctx_prev = prev[-1] if prev and t0 - words[prev[-1]]["t1"] < CTX_GAP else None
        ctx_next = nxt[0] if nxt and words[nxt[0]]["t0"] - t1 < CTX_GAP else None
        # высота — по исходнику: медиана голоса внутри; нет голоса (съедено) — ближайшие соседи
        sel = (f0_t >= t0) & (f0_t < t1) & ~np.isnan(f0_midi)
        if sel.any():
            pitch = float(np.median(f0_midi[sel]))
        else:
            near = np.flatnonzero(~np.isnan(f0_midi))
            pitch = float(f0_midi[near[np.argmin(np.abs(f0_t[near] - (t0 + t1) / 2))]]) if len(near) else 60.0
        # где менять содержание: съедено (провал громкости внутри слова) — только провал ±40 мс, целые края слова
        # остаются родными; провала нет (слово «смазано») и замена слова — всё помеченное место
        win = [[t0, t1]]
        if not mk.get("replace"):
            hs = holes(t0 - 0.05, t1 + 0.05)
            if hs:
                win = [[max(t0, h0 - 0.04), min(t1, h1 + 0.04)] for h0, h1 in hs]
        out.append({"mark": mk, "found": True, "where": where, "words": wi, "t0": round(t0, 3), "t1": round(t1, 3),
                    "windows": [[round(a_, 3), round(b_, 3)] for a_, b_ in win],
                    "text": text, "replace": bool(mk.get("replace")), "pitch": round(pitch, 2),
                    "ctx_prev": ctx_prev, "ctx_next": ctx_next})
    return out


def hint_notes(plan, words, f0_t, f0_midi, lang, syllables):
    """Ноты подсказки (как читает Executor): помеченное + соседние слова, по слогу на ноту, в их времени и высоте."""
    import numpy as np

    def pitch_at(a, b, fallback):
        sel = (f0_t >= a) & (f0_t < b) & ~np.isnan(f0_midi)
        return int(round(float(np.median(f0_midi[sel])))) if sel.any() else int(round(fallback))

    def word_notes(text, a, b, fallback):
        res = []
        toks = text.split()
        span = (b - a) / max(1, len(toks))
        for k, tok in enumerate(toks):
            wa, wb = a + k * span, a + (k + 1) * span
            sy = syllables(tok, lang) or [(0, len(tok))]
            d = (wb - wa) / len(sy)
            for si, (x, y) in enumerate(sy):
                lab = tok[x:y] + ("-" if si < len(sy) - 1 else "")
                if si == 0 and x > 0:
                    lab = tok[:y] + ("-" if si < len(sy) - 1 else "")
                if si == len(sy) - 1 and y < len(tok):
                    lab = tok[x:]
                sa, sb = wa + si * d, wa + (si + 1) * d
                res.append({"t": sa, "dur": max(0.06, sb - sa - 0.005), "pitch": pitch_at(sa, sb, fallback),
                            "vel": 96, "syl": lab})
        return res

    notes = []
    used = set()
    for p in plan:
        if not p.get("found"):
            continue
        for ci in (p["ctx_prev"],):
            if ci is not None and ci not in used and ci not in p["words"]:
                w = words[ci]
                notes += word_notes(w["text"], w["t0"], w["t1"], p["pitch"])
                used.add(ci)
        notes += word_notes(p["text"], p["t0"], p["t1"], p["pitch"])
        used.update(p["words"])
        ci = p["ctx_next"]
        if ci is not None and ci not in used:
            w = words[ci]
            notes += word_notes(w["text"], w["t0"], w["t1"], p["pitch"])
            used.add(ci)
    notes.sort(key=lambda n: n["t"])
    # без перекрытий
    for k in range(len(notes) - 1):
        notes[k]["dur"] = max(0.04, min(notes[k]["dur"], notes[k + 1]["t"] - notes[k]["t"] - 0.005))
    return notes


def step_prepare(job):
    import numpy as np
    import transcribe_worker as T
    say("P", "0.05 Текст по записи: высота…")
    x16 = T.load_mono16(job["input"])
    total = len(x16) / 16000.0
    tj = dict(job)
    if job.get("f0_in"):                         # отладка: готовая высота
        f0 = json.load(open(job["f0_in"]))
    else:
        f0, _kind = T.f0_track(x16, tj)
    f0 = np.asarray(f0, dtype=float)
    fps = 100.0
    f0_t = np.arange(len(f0)) / fps
    f0_midi = T.to_midi(np.nan_to_num(f0, nan=0.0))
    lyrics = job["lyrics"]
    words_t = T.words_of(lyrics)
    if not words_t:
        raise RuntimeError("Нет текста: вставьте слова песни.")
    lang = job.get("language") or "auto"
    if lang == "auto":
        lang = T.detect_lang(lyrics)
    say("P", "0.30 Текст по записи: выравнивание слов…")
    letters = T.align(x16, words_t, lang, tj)
    ws = tj.get("_wscore") or []
    words = []
    for wi, ((w, li, ca, cb), lt) in enumerate(zip(T.words_pos(lyrics), letters)):
        known = [x for x in lt if x]
        words.append({"text": w, "line": li, "from": ca, "to": cb,
                      "t0": round(known[0][0], 3) if known else None, "t1": round(known[-1][1], 3) if known else None,
                      "score": round(ws[wi], 4) if wi < len(ws) else None})
    # подсказка мест: слово без кадров или с уверенностью намного ниже медианы — «возможно, съедено»
    sc = sorted(w["score"] for w in words if w["score"] is not None)
    med = sc[len(sc) // 2] if sc else 0.0
    for w in words:
        w["suspect"] = w["t0"] is None or (w["score"] is not None and med > 0 and w["score"] < 0.35 * med)
    hop = 160
    rms = np.sqrt(np.convolve(x16.astype(np.float64) ** 2, np.ones(hop * 3) / (hop * 3), "same")[::hop] + 1e-12)
    db = 20 * np.log10(rms)
    plan = plan_marks(words, job.get("marks") or [], f0_t, f0_midi, total, db, fps)
    notes = hint_notes(plan, words, f0_t, f0_midi, lang, T.syllables)
    if notes:
        T.write_midi(job["hint_midi"], notes, 120.0)
    res = {"language": lang, "seconds": round(total, 3), "words": words, "plan": plan, "hint_notes": len(notes)}
    json.dump(res, open(job["plan_out"], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    say("P", "1.00 План готов.")
    return {"plan_out": job["plan_out"], "marks": len(plan), "found": sum(1 for p in plan if p.get("found")),
            "suspect": sum(1 for w in words if w["suspect"]), "hint_notes": len(notes)}


# ------------------------------------------------------------------ content
def _fade(n):
    import numpy as np
    return 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, max(1, n)))


def regions_of(plan, total):
    """Окна перепевки: пометка ± PAD; пересекающиеся — сливаются."""
    rs = []
    wins = sorted(w for p in plan if p.get("found") for w in (p.get("windows") or [[p["t0"], p["t1"]]]))
    for w0, w1 in wins:
        a, b = max(0.0, w0 - PAD), min(total, w1 + PAD)
        if rs and a <= rs[-1]["b"]:
            rs[-1]["b"] = max(rs[-1]["b"], b)
            rs[-1]["spans"].append([w0, w1])
        else:
            rs.append({"a": a, "b": b, "spans": [[w0, w1]]})
    return rs


def step_content(job):
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly
    from math import gcd
    plan = json.load(open(job["plan"], encoding="utf-8"))
    x, sr = sf.read(job["input"], dtype="float64", always_2d=True)
    x = x.mean(1)
    h, hsr = sf.read(job["hint_wav"], dtype="float64", always_2d=True)
    h = h.mean(1)
    if hsr != sr:
        g = gcd(int(sr), int(hsr))
        h = resample_poly(h, int(sr) // g, int(hsr) // g)
    h = np.pad(h, (0, max(0, len(x) - len(h))))[:len(x)]
    total = len(x) / sr
    c = x.copy()
    xf = int(XF * sr)

    def rms(v):
        return float(np.sqrt(np.mean(v ** 2))) if len(v) else 0.0

    for p, (w0, w1) in [(p, w) for p in plan["plan"] if p.get("found") for w in (p.get("windows") or [[p["t0"], p["t1"]]])]:
        a, b = int(w0 * sr), int(w1 * sr)
        a0, b1 = max(0, a - xf), min(len(x), b + xf)
        seg = h[a0:b1].copy()
        # уровень подсказки — как у исходника рядом (в провале исходник тих: берём соседей)
        near = np.concatenate([x[max(0, a0 - int(0.4 * sr)):a0], x[b1:b1 + int(0.4 * sr)]])
        ref = max(rms(near), rms(x[a0:b1]))
        if rms(seg) > 1e-6 and ref > 1e-6:
            seg *= min(8.0, ref / rms(seg))
        w = np.ones(b1 - a0)
        f = _fade(min(xf, len(w) // 2))
        w[:len(f)] = f
        w[len(w) - len(f):] = f[::-1]
        c[a0:b1] = x[a0:b1] * (1 - w) + seg * w
    sf.write(job["content_out"], c.astype(np.float32), sr, subtype="FLOAT")
    regs = regions_of(plan["plan"], total)
    os.makedirs(job["region_dir"], exist_ok=True)
    for k, r in enumerate(regs):
        ia, ib = int(r["a"] * sr), int(r["b"] * sr)
        r["src"] = os.path.join(job["region_dir"], f"r{k:02d}_src.wav")
        r["content"] = os.path.join(job["region_dir"], f"r{k:02d}_content.wav")
        r["out"] = os.path.join(job["region_dir"], f"r{k:02d}_out.wav")
        sf.write(r["src"], x[ia:ib].astype(np.float32), sr, subtype="FLOAT")
        sf.write(r["content"], c[ia:ib].astype(np.float32), sr, subtype="FLOAT")
        r["f0_spans"] = [[round(s0 - r["a"] - 0.02, 3), round(s1 - r["a"] + 0.02, 3)] for s0, s1 in r["spans"]]
    json.dump(regs, open(job["regions_out"], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return {"regions_out": job["regions_out"], "regions": len(regs)}


# ------------------------------------------------------------------ splice
def step_splice(job):
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly
    from math import gcd
    regs = json.load(open(job["regions"], encoding="utf-8"))
    y, sr = sf.read(job["take"], dtype="float64", always_2d=True)
    out = y.copy()
    xf = int(XF * sr)
    changed = []
    for r in regs:
        z, zsr = sf.read(r["out"], dtype="float64", always_2d=True)
        z = z.mean(1)
        if zsr != sr:
            g = gcd(int(sr), int(zsr))
            z = resample_poly(z, int(sr) // g, int(zsr) // g)
        ia = int(round(r["a"] * sr))
        for s0, s1 in r["spans"]:
            a, b = int(s0 * sr) - xf, int(s1 * sr) + xf
            a, b = max(ia, a), min(ia + len(z), b, len(y))
            if b - a < 2 * xf + 8:
                continue
            seg = z[a - ia:b - ia]
            # уровень окна — как у дубля по краям (дубль мог пройти обработку после перепевки)
            ma, mb = max(ia, a - int(0.25 * sr)), min(ia + len(z), b + int(0.25 * sr), len(y))
            edge_y = np.concatenate([y[ma:a].mean(1), y[b:mb].mean(1)])
            edge_z = np.concatenate([z[ma - ia:a - ia], z[b - ia:mb - ia]])
            ry, rz = float(np.sqrt(np.mean(edge_y ** 2) + 1e-12)), float(np.sqrt(np.mean(edge_z ** 2) + 1e-12))
            if ry > 1e-5 and rz > 1e-5:
                seg = seg * min(4.0, max(0.25, ry / rz))
            # уровень внутри — как у дубля рядом (до и после окна): модель поёт восстановленный слог своей громкостью
            inner = seg[xf:-xf]
            if len(inner) > 16:
                ri = float(np.sqrt(np.mean(inner ** 2) + 1e-12))
                if ri > 1e-5 and ry > 1e-5:
                    gl = float(np.clip(ry / ri, 0.5, 2.0))
                    if abs(20 * np.log10(gl)) > 1.0:
                        env = np.ones(len(seg))
                        env[xf:-xf] = gl
                        ramp = _fade(xf)
                        env[:xf] = 1 + (gl - 1) * ramp
                        env[-xf:] = 1 + (gl - 1) * ramp[::-1]
                        seg = seg * env
            w = np.ones(b - a)
            f = _fade(xf)
            w[:xf] = f
            w[-xf:] = f[::-1]
            for ch in range(out.shape[1]):
                out[a:b, ch] = y[a:b, ch] * (1 - w) + seg * w
            changed.append([round(a / sr, 3), round(b / sr, 3)])
    info = sf.info(job["take"])
    sf.write(job["out"], out, sr, subtype=info.subtype if info.subtype in ("PCM_16", "PCM_24", "FLOAT") else "PCM_24")
    return {"out": job["out"], "changed": changed}


# ------------------------------------------------------------------ piper (русский, или запасной путь)
def _stretch(seg, n):
    """Растяжка/сжатие куска до n отсчётов без смены высоты (фазовый вокодер librosa)."""
    import numpy as np
    import librosa
    if len(seg) < 64 or n < 64:
        return np.zeros(n)
    rate = float(np.clip(len(seg) / n, 0.33, 3.0))
    y = librosa.effects.time_stretch(seg.astype(np.float32), rate=rate)
    if len(y) >= n:
        return y[:n].astype(np.float64)
    return np.pad(y, (0, n - len(y))).astype(np.float64)


def step_piper(job):
    """Подсказка речью Piper: фраза (соседи + помеченное) → помеченное слово вырезается по выравниванию →
    растягивается на время слова в записи → кладётся в ту же точку времени."""
    import numpy as np
    import soundfile as sf
    import librosa
    import transcribe_worker as T
    from piper import PiperVoice
    plan = json.load(open(job["plan"], encoding="utf-8"))
    words, lang = plan["words"], plan["language"]
    voice = PiperVoice.load(job["voice"])
    sr = int(voice.config.sample_rate)
    hint = np.zeros(int(plan["seconds"] * sr) + sr)
    found = [p for p in plan["plan"] if p.get("found")]
    for k, p in enumerate(found):
        say("P", f"{0.05 + 0.9 * k / max(1, len(found)):.2f} Подсказка речью: «{p['text']}»")
        before = [words[p["ctx_prev"]]["text"]] if p.get("ctx_prev") is not None else []
        after = [words[p["ctx_next"]]["text"]] if p.get("ctx_next") is not None else []
        mine = p["text"].split()
        phrase = " ".join(before + mine + after)
        x = np.concatenate([c.audio_float_array for c in voice.synthesize(phrase)]).astype(np.float64)
        x16 = librosa.resample(x.astype(np.float32), orig_sr=sr, target_sr=16000)
        toks = [(w, 0) for w in before + mine + after]
        try:
            lt = T.align(np.ascontiguousarray(x16, dtype=np.float32), toks, lang, dict(job))
            spans = [[y for y in w_ if y] for w_ in lt]
            got = spans[len(before):len(before) + len(mine)]
            my0 = min(g[0][0] for g in got if g)
            my1 = max(g[-1][1] for g in got if g)
            # границы слова — посередине между соседями: выравнивание ставит буквы по пикам, края слова (согласная
            # в начале, гласная в конце) лежат за пиками
            pv = spans[len(before) - 1] if before and spans[len(before) - 1] else None
            nx = spans[len(before) + len(mine)] if after and spans[len(before) + len(mine)] else None
            a = (pv[-1][1] + my0) / 2 if pv else my0 - 0.06
            b = (my1 + nx[0][0]) / 2 if nx else my1 + 0.10
        except Exception:  # noqa: BLE001 — выравнивания нет: доля по буквам
            n_all = sum(len(w) for w, _ in toks) or 1
            a = len(x) / sr * sum(len(w) for w in before) / n_all
            b = a + len(x) / sr * sum(len(w) for w in mine) / n_all
        a, b = max(0.0, a), min(len(x) / sr, b)
        seg = x[int(a * sr):int(b * sr)]
        n = int((p["t1"] - p["t0"]) * sr)
        y = _stretch(seg, n)
        i0 = int(p["t0"] * sr)
        hint[i0:i0 + n] = y[:max(0, min(n, len(hint) - i0))]
    sf.write(job["out_wav"], hint.astype(np.float32), sr, subtype="FLOAT")
    return {"out_wav": job["out_wav"], "hints": len(found)}


def step_score(job):
    """Проверка: в окне [a, b] записи — насколько выравнивание «слышит» каждое слово-кандидата (средняя уверенность букв)."""
    import numpy as np
    import transcribe_worker as T
    x16 = T.load_mono16(job["input"])
    a, b = int(job["a"] * 16000), int(job["b"] * 16000)
    seg = np.ascontiguousarray(x16[max(0, a):b], dtype=np.float32)
    res = {}
    for w in job["words"]:
        tj = dict(job)
        T.align(seg, [(w, 0)], job.get("language") or "en", tj)
        res[w] = round(float((tj.get("_wscore") or [0.0])[0]), 4)
    return res


def main():
    job = json.load(open(sys.argv[1], encoding="utf-8"))
    step = job.get("step")
    fn = {"prepare": step_prepare, "content": step_content, "splice": step_splice, "piper": step_piper, "score": step_score}.get(step)
    if not fn:
        raise RuntimeError(f"Неизвестный шаг: {step}")
    say("R", json.dumps(fn(job), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:  # noqa: BLE001
        traceback.print_exc()
        say("E", str(ex).replace("\n", " ")[:600] or ex.__class__.__name__)
        sys.exit(1)
