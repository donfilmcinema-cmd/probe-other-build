# -*- coding: utf-8 -*-
"""DZOD Vocalist 0.9.2 — «Подобрать настройки по тексту» (план разборчивости RVC, письмо sound 17.11).

Отрывок 30–40 с с самым плотным текстом песни перепевается сеткой настроек RVC и каждый вариант меряется:
- разборчивость — распознавание (Whisper) против слов текста этого отрывка (WER);
- яркость — доля энергии > 2 кГц, вариант / исходник;
- динамика — отклонение огибающей 20 мс от исходника (медиана, дБ).
Сетка: защита согласных 0,08 / 0,12 / 0,2 × тембр 0,45 / 0,6 × эпохи (до трёх последних сохранённых: 200 / 250 / 300).
Заодно диагностика чистки: WER сырого исходника песни и очищенного (если песню чистили) на том же отрезке.
Порядок: в норме (яркость 80–150 %, динамика ≤ 1,5 дБ), внутри — по WER; четыре верхних — автору на слух (WER не единственный судья). Отрывки вариантов — в папке песни «подбор/».
"""
import json
import shutil
import time
from pathlib import Path

import numpy as np

import audio as A
import engine as E

WIN = 35.0          # с: длина отрывка
PAD = 0.5
PROTECT = (0.08, 0.12, 0.2)     # вокруг 0,12 — лучшие на слух дубли Ravenous Mars (sound 17.11)
INDEX = (0.45, 0.6)
EPOCHS = (200, 250, 300)


def _job(tmp, name, d):
    f = tmp / f"{name}.json"
    f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return f


def densest_window(words, total, win=WIN):
    """Отрезок длиной win с наибольшим числом слов текста (по времени слов в исходнике)."""
    ts = sorted(w["t0"] for w in words if w.get("t0") is not None)
    if not ts:
        return 0.0, min(total, win)
    best, a_best = -1, ts[0]
    j = 0
    for i, a in enumerate(ts):
        while j < len(ts) and ts[j] < a + win:
            j += 1
        if j - i > best:
            best, a_best = j - i, a
    a = max(0.0, min(a_best - PAD, total - win))
    return round(a, 2), round(min(total, a + win), 2)


def _env_dev(x, y, sr):
    w, h = int(0.02 * sr), int(0.01 * sr)
    ex = 10 * np.log10(A._movmean(x * x, w)[::h] + 1e-12)
    ey = 10 * np.log10(A._movmean(y * y, w)[::h] + 1e-12)
    n = min(len(ex), len(ey))
    ex, ey = ex[:n], ey[:n]
    loud = (ex > ex.max() - 35) & (ey > ey.max() - 45)
    if loud.sum() < 10:
        return None
    d = ey[loud] - ex[loud]
    return round(float(np.median(np.abs(d - np.median(d)))), 2)


def _hf(v, sr, f=2000):
    S = np.abs(np.fft.rfft(v * np.hanning(len(v)))) ** 2
    fr = np.fft.rfftfreq(len(v), 1 / sr)
    tot = S[fr > 60].sum()
    return float(S[fr > f].sum() / tot) if tot > 0 else 0.0


def _epoch_models(voice, model_now):
    """До трёх эпох около 200 / 250 / 300 из сохранённых + текущая модель голоса."""
    import voices as V
    cks = V.checkpoints(voice)
    out = []
    for e in EPOCHS:
        near = [c for c in cks if abs(c["epoch"] - e) <= 10]
        if near:
            c = min(near, key=lambda c: abs(c["epoch"] - e))
            if c["path"] not in [o["path"] for o in out]:
                out.append({"label": f"эпоха {c['epoch']}", "epoch": c["epoch"], "file": c["file"], "path": c["path"]})
    if not out:
        out.append({"label": "текущая модель", "epoch": None, "file": None, "path": str(model_now)})
    return out


def tune(ctx, source, voice, base_opts, lyrics, out_dir, raw_source=None, report=None, language="auto"):
    """source — что перепевается (очищенный вокал), raw_source — сырой исходник песни (для диагностики чистки)."""
    import quality as Q
    import voices as V
    if not (lyrics or "").strip():
        raise E.EngineError("Нужен текст песни: «Текст» у дубля → «Правка текста» → вставьте слова.")
    p = E.paths()
    workers = E.ensure_workers()
    t_all = time.time()
    if not Q.whisper_ready():
        ctx.span(0.0, 0.1, "Модель распознавания речи — первая установка (~1,6 ГБ)…")
        Q.install_whisper(ctx)
    model, index = V.model_files(voice)
    x, sr = A.read(source, mono=True)
    x = x[:, 0].astype(np.float64)
    total = len(x) / sr
    # слова текста по времени — из отчёта «Качества» (если есть) или новой сверкой исходника с самим собой
    if report is None or not report.get("ref_words") or not report.get("lyrics_used"):
        ctx.span(0.0, 0.2, "Слова текста по времени…")
        report = Q.measure(ctx, source, source, lyrics, language, out=E.new_tmp("tq") / "q.json")
    ref = [w for w in report["ref_words"] if w.get("t0") is not None]
    a, b = densest_window(ref, total)
    exp = [w["w"] for w in ref if w["t0"] >= a - 0.1 and w["t1"] <= b + 0.1]
    lang = report.get("language") or language
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = E.new_tmp("tune")
    try:
        seg = x[int(a * sr):int(b * sr)]
        src_seg = out_dir / "00 исходник.wav"
        A.write(src_seg, seg, sr, "PCM_24")
        raw_seg = None
        if raw_source and Path(raw_source).exists() and Path(raw_source).resolve() != Path(source).resolve():
            r_, rsr = A.read(raw_source, mono=True)
            r_ = r_[:, 0]
            raw_seg = out_dir / "00 сырой исходник.wav"
            A.write(raw_seg, r_[int(a * rsr):int(b * rsr)], rsr, "PCM_24")

        # ---- сетка
        eps = _epoch_models(voice, model)
        grid = []
        for ep in eps:
            for ix in INDEX:
                for pr in PROTECT:
                    grid.append({"epoch": ep, "index_rate": ix, "protect": pr})
        items = []
        for k, g in enumerate(grid):
            o = dict(base_opts)
            for key in ("match_level", "engine", "svc_steps", "svc_cfg"):
                o.pop(key, None)
            o.update(index_rate=g["index_rate"], protect=g["protect"])
            name = (f"{k + 1:02d} {g['epoch']['label']} · тембр {int(g['index_rate'] * 100)} · "
                    f"защита {g['protect']:.2f}.wav")
            g["file"] = str(out_dir / name)
            items.append(dict(o, model=g["epoch"]["path"], index=str(index or ""), input=str(src_seg),
                              output=g["file"], label=f"вариант {k + 1} из {len(grid)}", split_audio=False))
        ctx.span(0.2, 0.75, f"Перепевка {len(grid)} вариантов отрывка…")
        ctx.run([p.applio_py, workers / "applio_worker.py", _job(tmp, "grid", {"items": items})], cwd=p.applio,
                what="Подбор настроек")

        # ---- оценка распознаванием
        ctx.span(0.75, 0.95, "Оценка вариантов по тексту…")
        sitems = [{"wav": str(src_seg), "t0": 0, "t1": b - a, "expect": exp, "target": ""}]
        if raw_seg:
            sitems.append({"wav": str(raw_seg), "t0": 0, "t1": b - a, "expect": exp, "target": ""})
        sitems += [{"wav": g["file"], "t0": 0, "t1": b - a, "expect": exp, "target": ""} for g in grid
                   if Path(g["file"]).exists()]
        sj = {"mode": "score", "whisper": str(Q.whisper_dir()), "language": lang, "items": sitems}
        sc = (ctx.run([p.exec_py, workers / "quality_worker.py", _job(tmp, "score", sj)], env=E.exec_env(),
                      what="Оценка вариантов") or {}).get("items") or []
        wer_src = sc[0]["wer"] if sc else None
        wer_raw = sc[1]["wer"] if raw_seg and len(sc) > 1 else None
        rest = sc[2:] if raw_seg else sc[1:]
        hx = _hf(seg, sr)
        rows = []
        gi = [g for g in grid if Path(g["file"]).exists()]
        for g, s_ in zip(gi, rest):
            y, ysr = A.read(g["file"], mono=True)
            y = (A.resample(y, ysr, sr) if ysr != sr else y)[:, 0].astype(np.float64)
            n = min(len(y), len(seg))
            br = round(_hf(y[:n], sr) / hx, 2) if hx > 0.02 else None
            env = _env_dev(seg[:n], y[:n], sr)
            ok = (br is None or 0.8 <= br <= 1.5) and (env is None or env <= 1.5)
            rows.append({"epoch": g["epoch"]["epoch"], "epoch_file": g["epoch"]["file"], "epoch_label": g["epoch"]["label"],
                         "index_rate": g["index_rate"], "protect": g["protect"], "wer": s_["wer"], "heard": s_["text"],
                         "bright": br, "env_db": env, "in_norm": ok, "file": g["file"]})
        # в норме — вперёд; внутри — по WER; при равном WER — меньше тембра не лучше: выше тембр (ближе к голосу)
        rows.sort(key=lambda r: (0 if r["in_norm"] else 1, r["wer"] if r["wer"] is not None else 999,
                                 -r["index_rate"]))
        best = rows[0] if rows else None
        res = {"voice": voice, "window": [a, b], "words": len(exp), "wer_source": wer_src, "wer_raw": wer_raw,
               "rows": rows, "best": best, "folder": str(out_dir), "seconds": round(time.time() - t_all, 1),
               "source_file": str(src_seg), "raw_file": str(raw_seg) if raw_seg else None}
        (out_dir / "подбор.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        E.log(f"ПОДБОР НАСТРОЕК: отрывок {a:.1f}–{b:.1f} с, {len(exp)} сл., WER исходника {wer_src}"
              + (f", сырого {wer_raw}" if raw_seg else "") + f"; лучший: {best and best['epoch_label']} · тембр "
              f"{best and best['index_rate']} · защита {best and best['protect']} — WER {best and best['wer']}; "
              f"{res['seconds']} с")
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
