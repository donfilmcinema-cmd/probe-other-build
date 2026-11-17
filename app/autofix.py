# -*- coding: utf-8 -*-
"""DZOD Vocalist 0.8.2 — ТЕКСТ для всей перепевки: дубль сверяется с текстом песни, слова, которые разошлись,
переделываются сами (письма sound 15.11 23:00 / 23:10).

1. Мерило (0.8.0) против текста автора → слова дубля «съедено / искажено» с временем (ошибки, которые есть и в
   исходнике, не трогаем).
2. Для каждого такого слова — до трёх вариантов места (окно слова ± 0,6 с перепевается заново, вклеивается только
   само слово ± 50 мс, стык 60 мс, уровень — как у дубля рядом):
   - «Согласные исходника»: в глухих кадрах слова (s, t, k, f, ш…) — звук исходника, в звонких — дубль;
   - «Меньше тембра»: та же модель RVC с индексом 0,15 и защитой согласных 0,15 — модель меньше «подгоняет» звуки;
   - «Seed-VC»: место перепевается движком со словами по Whisper (если он установлен);
     для дубля Seed-VC — наоборот, место перепевает RVC.
3. Каждый вариант и исходный дубль распознаются в отрезке слово ± 1 с; выбирается тот, где нужное слово
   распознано и меньше ошибок против текста. Ничья — остаётся дубль как был.
4. Новый дубль «… - по тексту NN» (прежний не трогается) + отчёт: какие слова исправлены и чем, какие остались.
"""
import json
import shutil
import time
from pathlib import Path

import numpy as np

import audio as A
import engine as E

CTX = 0.6          # с: контекст перепевки вокруг слова
PAD = 0.05         # с: вклеивается слово ± это
CLIP = 1.0         # с: отрезок распознавания вокруг слова
MAX_WORDS = 200    # 0.8.9: было 40 — на Suno-песне ошибок ~150
METHODS = {"consonants": "согласные исходника", "lowidx": "меньше тембра", "seedvc": "Seed-VC", "rvc": "RVC",
           "executor": "Executor → RVC"}
EXEC_XF = 0.02     # 0.9.4: стык места «Executor → RVC» — 20 мс (письмо sound 17.11)


def _job(tmp, name, d):
    f = tmp / f"{name}.json"
    f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return f


def _regions(problems, total):
    """Окна перепевки (слово ± 0,6 с), пересекающиеся — слиты; в каждом — свои отрезки вклейки."""
    regs = []
    for k, p in sorted(enumerate(problems), key=lambda kp: kp[1]["t0"]):
        a, b = max(0.0, p["t0"] - CTX), min(total, p["t1"] + CTX)
        span = [max(0.0, p["t0"] - PAD), min(total, p["t1"] + PAD)]
        if regs and a <= regs[-1]["b"]:
            regs[-1]["b"] = max(regs[-1]["b"], b)
            regs[-1]["spans"].append(span)
            regs[-1]["words"].append(k)
        else:
            regs.append({"a": round(a, 3), "b": round(b, 3), "spans": [span], "words": [k]})
    for r in regs:                          # слить пересекающиеся отрезки вклейки
        sp = sorted(r["spans"])
        m = [sp[0]]
        for s0, s1 in sp[1:]:
            if s0 <= m[-1][1]:
                m[-1][1] = max(m[-1][1], s1)
            else:
                m.append([s0, s1])
        r["spans"] = [[round(s0, 3), round(s1, 3)] for s0, s1 in m]
    return regs


def consonant_mask(src, sr, thr=0.4, floor_db=-40):
    """1 — глухой кадр исходника (шум без высоты: s, t, k, f, ш…), 0 — голос или тишина. Кадры 20 мс, шаг 5 мс."""
    n, h = int(0.02 * sr), int(0.005 * sr)
    lo, hi = max(2, int(sr / 1000)), int(sr / 70)
    k = max(0, (len(src) - n) // h)
    m = np.zeros(len(src))
    if k < 2:
        return m
    rm = np.array([np.sqrt(np.mean(src[i * h:i * h + n] ** 2)) for i in range(k)])
    top = rm.max() or 1.0
    fr = np.zeros(k)
    for i in range(k):
        f = src[i * h:i * h + n]
        if rm[i] < top * 10 ** (floor_db / 20):
            continue
        f = f - f.mean()
        ac = np.correlate(f, f, "full")[n - 1:]
        if ac[0] <= 0:
            continue
        fr[i] = 1.0 if ac[lo:hi].max() / ac[0] < thr else 0.0
    # сгладить 10 мс, растянуть на сэмплы
    fr = np.convolve(fr, np.ones(3) / 3, "same")
    m = np.interp(np.arange(len(src)), np.arange(k) * h + n // 2, fr)
    return np.clip(m, 0, 1)


def _consonant_region(take, src, sr, a, b, spans, out):
    """Вариант «согласные исходника» для окна [a, b]: в глухих кадрах слов — исходник (в громкости дубля)."""
    ia, ib = int(a * sr), int(b * sr)
    y = take[ia:ib].copy()
    x = src[ia:ib]
    x = np.pad(x, (0, max(0, len(y) - len(x))))[:len(y)]
    m = consonant_mask(x, sr)
    keep = np.zeros(len(y))
    for s0, s1 in spans:
        keep[max(0, int((s0 - a) * sr)):max(0, int((s1 - a) * sr))] = 1.0
    m = m * keep
    # громкость исходника в глухих кадрах — как у дубля в тех же кадрах (дубль тише/громче в целом)
    sel = m > 0.5
    if sel.sum() > int(0.01 * sr):
        ry = float(np.sqrt(np.mean(y[sel] ** 2)) + 1e-9)
        rx = float(np.sqrt(np.mean(x[sel] ** 2)) + 1e-9)
        g = float(np.clip(ry / rx, 0.25, 4.0)) if ry > 1e-4 else 1.0
        # если в дубле на месте согласной почти тишина (съедено) — берём громкость исходника относительно гласных
        if ry < rx * 0.1:
            g = 1.0
    else:
        g = 1.0
    z = y * (1 - m) + x * g * m
    A.write(out, z, sr, "FLOAT")


VAR_NAME = {"executor": "Executor-RVC", "consonants": "согласные исходника", "lowidx": "меньше тембра",
            "seedvc": "Seed-VC", "rvc": "RVC"}


def fix(ctx, source, take, item, lyrics, out, voice=None, report=None, language="auto", engine_take="rvc",
        use_seedvc=False, variants_dir=None):
    """source — исходник дубля, take — дубль, item — настройки той перепевки RVC (model, index, …), voice — код голоса
    (для образца Seed-VC). Пишет out; возвращает отчёт по словам."""
    import quality as Q
    if not (lyrics or "").strip():
        raise E.EngineError("Нужен текст песни: откройте «Текст» и вставьте слова — программа сверит дубль с ним.")
    p = E.paths()
    workers = E.ensure_workers()
    source, take, out = Path(source), Path(take), Path(out)
    t_all = time.time()
    old = report is not None and any("ca" not in pr for pr in report.get("problems", []))    # отчёт до 0.9.4
    if report is None or old or not report.get("lyrics_used") or "ref_words" not in report:
        ctx.span(0.0, 0.35, "Сверка дубля с текстом…")
        report = Q.measure(ctx, source, take, lyrics, language, out=E.new_tmp("afq") / "q.json")
    problems = [pr for pr in report.get("problems", []) if pr.get("t0") is not None and pr.get("t1") is not None]
    problems = problems[:MAX_WORDS]
    base = {"before": report["checks"]["wer"], "words": [], "file": None, "seconds": 0}
    if not problems:
        base["note"] = "Все слова дубля совпадают с текстом — исправлять нечего."
        return base
    ref = report.get("ref_words") or []
    lang = report.get("language") or language
    tmp = E.new_tmp("af")
    try:
        y, sr = A.read(take, mono=True)
        y = y[:, 0].astype(np.float64)
        x, xsr = A.read(source, mono=True)
        x = (A.resample(x, xsr, sr) if xsr != sr else x)[:, 0].astype(np.float64)
        x = np.pad(x, (0, max(0, len(y) - len(x))))[:len(y)]
        total = len(y) / sr
        regs = _regions(problems, total)
        rd = tmp / "reg"
        rd.mkdir()
        for k, r in enumerate(regs):
            r["src"] = str(rd / f"r{k:02d}_src.wav")
            A.write(r["src"], x[int(r["a"] * sr):int(r["b"] * sr)], sr, "FLOAT")

        # ---- варианты
        methods = ["consonants"]
        if engine_take == "seedvc":
            if item.get("model"):
                methods.append("rvc")
        else:
            methods.append("lowidx")
        # 17.11 (вердикт автора на слух: Seed-VC «ненастоящий, повреждённый») — только по явной галочке
        svc_ok = use_seedvc and engine_take != "seedvc" and E.seedvc_ready() and voice is not None
        if svc_ok:
            methods.append("seedvc")
        # 0.9.4: «Executor → RVC» — слово с соседями поёт Executor, тембр даёт та же модель RVC; первым способом
        ex_ok = engine_take != "seedvc" and item.get("model") and E.executor_status()["ready"]
        ex_regs = []
        if ex_ok:
            import textfix as TF
            marks = [{"from": pr["ca"], "to": pr["cb"]} for pr in problems
                     if pr.get("ca") is not None and pr.get("cb") is not None]
            if marks:
                ctx.span(0.35, 0.55, "Executor поёт слова, RVC — тембр…")
                try:
                    (tmp / "ex").mkdir(exist_ok=True)
                    _plan, ex_regs, _q = TF.executor_regions(ctx, tmp / "ex", source, item, lyrics, marks, lang)
                except E.EngineError as ex:
                    E.log(f"ПО ТЕКСТУ: Executor → RVC пропущен: {ex}")
                    ex_regs = []
            if ex_regs:
                methods.insert(0, "executor")
        ctx.span(0.55, 0.65, "Варианты мест…")
        outs = {m: [str(rd / f"r{k:02d}_{m}.wav") for k in range(len(regs))] for m in methods if m != "executor"}
        for k, r in enumerate(regs):
            _consonant_region(y, x, sr, r["a"], r["b"], r["spans"], outs["consonants"][k])
        rvc_m = "lowidx" if "lowidx" in methods else ("rvc" if "rvc" in methods else None)
        if rvc_m:
            its = []
            for k, r in enumerate(regs):
                it = dict(item)
                if rvc_m == "lowidx":
                    it["index_rate"] = min(float(it.get("index_rate", 0.6)), 0.15)
                    it["protect"] = min(float(it.get("protect", 0.33)), 0.15)
                it.pop("engine", None)
                it.update(input=r["src"], output=outs[rvc_m][k], label=f"место {k + 1}", split_audio=False,
                          no_fine_envelope=False)
                its.append(it)
            ctx.run([p.applio_py, workers / "applio_worker.py", _job(tmp, "rvc", {"items": its})], cwd=p.applio,
                    what="Варианты мест (RVC)")
        if svc_ok:
            try:
                import voices as V
                refw = V.reference(voice)
                sj = {"src_dir": str(E.seedvc_dir()), "fp16": True,
                      "items": [{"input": r["src"], "output": outs["seedvc"][k], "reference": str(refw),
                                 "pitch": item.get("pitch", 0), "steps": 30, "cfg_rate": 0.7, "label": f"место {k + 1}"}
                                for k, r in enumerate(regs)]}
                ctx.run([p.exec_py, workers / "seedvc_worker.py", _job(tmp, "svc", sj)], env=E.exec_env(),
                        cwd=E.seedvc_dir(), what="Варианты мест (Seed-VC)")
            except E.EngineError as ex:
                E.log(f"ПО ТЕКСТУ: Seed-VC пропущен: {ex}")
                methods.remove("seedvc")

        # ---- полный дубль с каждым вариантом (только места), для оценки
        ctx.span(0.65, 0.75, "Вклейка вариантов…")
        cand = {"take": str(take)}
        for m in methods:
            if m == "executor":
                rj = [{"a": r["a"], "b": r["b"], "out": r["out"], "spans": r["spans"], "xf": EXEC_XF, "match_bright": True}
                      for r in ex_regs]
            else:
                rj = [{"a": r["a"], "b": r["b"], "out": outs[m][k], "spans": r["spans"]} for k, r in enumerate(regs)
                      if Path(outs[m][k]).exists()]
            (tmp / f"regs_{m}.json").write_text(json.dumps(rj), encoding="utf-8")
            f = tmp / f"cand_{m}.wav"
            ctx.run([p.exec_py, workers / "textfix_worker.py",
                     _job(tmp, f"splice_{m}", {"step": "splice", "take": str(take), "regions": str(tmp / f"regs_{m}.json"),
                                                "out": str(f)})], env=E.exec_env(), what="Вклейка вариантов")
            if f.exists():
                cand[m] = str(f)

        # 0.9.5: каждый способ целиком (все места этим способом) — автору на слух, даже если Whisper его не выбрал
        variants = []
        if variants_dir is not None:
            vd = Path(variants_dir)
            if vd.exists():
                shutil.rmtree(vd, ignore_errors=True)
            vd.mkdir(parents=True, exist_ok=True)
            for m in methods:
                if m in cand:
                    f = vd / f"{VAR_NAME.get(m, m)}.wav"
                    shutil.copy2(cand[m], f)
                    variants.append({"method": m, "label": METHODS.get(m, m), "file": str(f)})

        # ---- оценка распознаванием: слово ± 1 с, против слов текста в этом отрезке
        ctx.span(0.75, 0.93, "Оценка вариантов по тексту…")
        items, index = [], []
        for pi, pr in enumerate(problems):
            c0, c1 = max(0.0, pr["t0"] - CLIP), min(total, pr["t1"] + CLIP)
            exp = [w["w"] for w in ref if w.get("t0") is not None and w["t0"] >= c0 - 0.1 and w["t1"] <= c1 + 0.1]
            for name, f in cand.items():
                items.append({"wav": f, "t0": c0, "t1": c1, "expect": exp, "target": pr["word"]})
                index.append((pi, name))
        sjob = {"mode": "score", "whisper": str(Q.whisper_dir()), "language": lang, "items": items}
        res = ctx.run([p.exec_py, workers / "quality_worker.py", _job(tmp, "score", sjob)], env=E.exec_env(),
                      what="Оценка вариантов") or {}
        scores = res.get("items") or []
        best = {}
        per = {}
        for (pi, name), sc in zip(index, scores):
            per.setdefault(pi, {})[name] = sc
        words = []
        for pi, pr in enumerate(problems):
            opts = per.get(pi, {})
            key = lambda n: (0 if opts[n]["has_target"] else 1, opts[n]["wer"] if opts[n]["wer"] is not None else 999)  # noqa: E731
            if not opts or "take" not in opts:
                continue
            win = min(opts, key=lambda n: (key(n), 0 if n == "take" else 1))
            if win != "take" and key(win) >= key("take"):
                win = "take"
            best[pi] = win
            words.append({"word": pr["word"], "t0": pr["t0"], "t1": pr["t1"], "kind": pr["kind"], "heard": pr.get("heard"),
                          "chosen": win, "method": METHODS.get(win, "без изменений"),
                          "fixed": win != "take" and opts[win]["has_target"],
                          "before": opts["take"]["text"], "after": opts[win]["text"],
                          "tried": {n: {"text": o["text"], "wer": o["wer"], "ok": o["has_target"]} for n, o in opts.items()}})

        # ---- итог: в каждом окне — отрезки слов с выбранным вариантом
        ctx.span(0.93, 1.0, "Новый дубль…")
        final = []
        for k, r in enumerate(regs):
            for pi in r["words"]:
                m = best.get(pi, "take")
                if m == "take":
                    continue
                pr = problems[pi]
                if m == "executor":
                    for er in ex_regs:
                        sp = [s for s in er["spans"] if s[0] < pr["t1"] + PAD and s[1] > pr["t0"] - PAD]
                        if sp:
                            final.append({"a": er["a"], "b": er["b"], "out": er["out"], "spans": sp, "xf": EXEC_XF,
                                          "match_bright": True})
                    continue
                final.append({"a": r["a"], "b": r["b"], "out": outs[m][k],
                              "spans": [[round(max(0.0, pr["t0"] - PAD), 3), round(min(total, pr["t1"] + PAD), 3)]]})
        changed = []
        if final:
            (tmp / "final_regs.json").write_text(json.dumps(final), encoding="utf-8")
            out.parent.mkdir(parents=True, exist_ok=True)
            rr = ctx.run([p.exec_py, workers / "textfix_worker.py",
                          _job(tmp, "final", {"step": "splice", "take": str(take), "regions": str(tmp / "final_regs.json"),
                                              "out": str(out)})], env=E.exec_env(), what="Новый дубль") or {}
            changed = rr.get("changed") or []
        n_fixed = sum(1 for w in words if w["fixed"])
        E.log(f"ПО ТЕКСТУ {take.name}: слов с ошибкой {len(problems)}, исправлено {n_fixed}, "
              f"способы {[w['method'] for w in words if w['chosen'] != 'take']}, {time.time() - t_all:.0f} с")
        return {"before": report["checks"]["wer"], "words": words, "changed": changed,
                "file": str(out) if final and out.exists() else None, "fixed": n_fixed, "left": len(problems) - n_fixed,
                "methods": methods, "variants": variants, "seconds": round(time.time() - t_all, 1)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def fix_passes(ctx, source, take, item, lyrics, out, voice=None, report=None, language="auto", engine_take="rvc",
               passes=3, use_seedvc=False, variants_dir=None):
    """0.8.9: «проходить, пока исправляется» — до 3 проходов. Каждый следующий заново сверяет уже исправленный
    дубль с текстом и берётся за оставшиеся слова; останов, когда за проход ничего не исправилось."""
    out = Path(out)
    cur, rep, allw, total_fixed, first, res, chg = Path(take), report, [], 0, None, None, []
    tmp = E.new_tmp("afp")
    try:
        for k in range(passes):
            ctx.span(k / passes, (k + 1) / passes, f"Проход {k + 1} из {passes}…")
            dst = tmp / f"pass{k + 1}.wav"
            r = fix(ctx, source, cur, item, lyrics, dst, voice=voice, report=rep, language=language,
                    engine_take=engine_take, use_seedvc=use_seedvc, variants_dir=variants_dir if k == 0 else None)
            if first is None:
                first = r
            for w in r.get("words", []):
                w["pass"] = k + 1
            allw += r.get("words", [])
            chg += [list(c) for c in (r.get("changed") or [])]
            total_fixed += r.get("fixed", 0) or 0
            res = r
            if not r.get("file") or not r.get("fixed"):
                break
            cur, rep = Path(r["file"]), None
        if cur != Path(take):
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cur, out)
        # последнее слово о каждом месте — из последнего прохода, где оно было
        last = {}
        for w in allw:
            last[(w["word"], round(w["t0"], 1))] = w
        words = sorted(last.values(), key=lambda w: w["t0"])
        changed = sorted({tuple(c) for c in chg})
        return {"before": first["before"] if first else None, "words": words, "changed": [list(c) for c in changed],
                "file": str(out) if cur != Path(take) else None, "fixed": sum(1 for w in words if w.get("fixed")),
                "left": sum(1 for w in words if not w.get("fixed")), "passes": k + 1,
                "methods": (first or {}).get("methods"), "variants": (first or {}).get("variants") or [],
                "note": (first or {}).get("note")}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
