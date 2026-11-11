# -*- coding: utf-8 -*-
"""DZOD Vocalist — «Перепевка с текстом»: исправление съеденных слов и замена слов в готовой перепевке.

Движок перепевки не меняется. В помеченных местах у той же перепевки два входа:
высота и громкость — из исходника, содержание (ContentVec) — из подсказки нужного звучания
(SoulX-Singer поёт помеченное слово в его времени и высоте). Всё вне пометок — бит в бит прежний дубль.
План и шаги — shared/docs/vocalist/text-guided-revoice_RU.md (хаб).
"""
import json
import os
import shutil
import time
from pathlib import Path

import engine as E

APPLIO_KEYS = ("model", "index", "pitch", "f0_method", "index_rate", "volume_envelope", "protect",
               "f0_autotune", "f0_autotune_strength", "proposed_pitch", "proposed_pitch_threshold",
               "clean_audio", "clean_strength", "embedder_model", "formant")


def _need_executor():
    if not E.executor_status()["ready"]:
        raise E.EngineError("Нужен установленный Executor (вкладка «4 Executor» → «Установить»): "
                            "в нём выравнивание текста и подсказка звучания.")


def _job(tmp, name, d):
    f = tmp / f"{name}.json"
    f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return f


def _prepare(ctx, tmp, source, lyrics, marks, language):
    p = E.paths()
    workers = E.ensure_workers()
    (p.exec / "align").mkdir(parents=True, exist_ok=True)
    job = {"step": "prepare", "input": str(source), "lyrics": lyrics, "marks": marks or [], "language": language or "auto",
           "hint_midi": str(tmp / "hint.mid"), "plan_out": str(tmp / "plan.json"),
           "rmvpe": str(p.applio / "rvc" / "models" / "predictors" / "rmvpe.pt"),
           "rmvpe_py": str(p.applio / "rvc" / "lib" / "predictors" / "RMVPE.py"),
           "align_model_dir": str(p.exec / "align")}
    ctx.run([p.exec_py, workers / "textfix_worker.py", _job(tmp, "prep", job)], env=E.exec_env(), what="Текст по записи")
    return json.loads((tmp / "plan.json").read_text(encoding="utf-8"))


def _soulx_hint(ctx, tmp, p, workers):
    ctx.span(0.25, 0.55, "Подсказка звучания (SoulX-Singer)…")
    (tmp / "segs").mkdir(exist_ok=True)
    hj = {"midi": str(tmp / "hint.mid"), "src_dir": str(p.exec_src), "model": str(p.exec_model),
          "prompt_wav": str(p.exec_prompt_wav), "prompt_meta": str(p.exec_prompt_meta),
          "out_wav": str(tmp / "hint.wav"), "meta_out": str(tmp / "hint_meta.json"),
          "notes_out": str(tmp / "hint_notes.json"), "seg_dir": str(tmp / "segs"),
          "cache_dir": str(p.exec / "cache" / "soulx"), "fp16": True, "style": "straight", "vibrato": 0}
    ctx.run([p.exec_py, workers / "executor_worker.py", _job(tmp, "hint", hj)], cwd=p.exec_src,
            env=E.exec_env(), what="Подсказка звучания")


def _parts_dir(out):
    out = Path(out)
    return out.parent / (out.stem + " - части")


def _keep_parts(tmp, out, quality, checks, plan):
    """Промежуточные файлы рядом с новым дублем — автор может послушать и прислать:
    rNN_src (исходник), rNN_hint (подсказка), rNN_content (звук для содержания), rNN_out (перепетое окно),
    план, оценка. Только окна вокруг пометок (секунды), не вся песня."""
    try:
        d = _parts_dir(out)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
        reg = Path(tmp) / "reg"
        if reg.exists():
            for f in sorted(reg.glob("r*_*.wav")):
                shutil.copy2(f, d / f.name)
        regs = []
        if (Path(tmp) / "regions.json").exists():
            regs = json.loads((Path(tmp) / "regions.json").read_text(encoding="utf-8"))
        rep = {"plan": plan.get("plan"), "regions": [{"file": f"r{k:02d}", "from_s": r["a"], "to_s": r["b"],
                                                      "spans": r["spans"]} for k, r in enumerate(regs)],
               "quality": quality, "checks": checks}
        (d / "отчёт.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        lines = ["Перепевка с текстом — промежуточные файлы (для проверки; можно удалить).",
                 "rNN_src — исходник окна, rNN_hint — подсказка звучания, rNN_content — звук для содержания,",
                 "rNN_out — перепетое окно (до вклейки). Время окна в песне — в отчёт.json (from_s / to_s).", ""]
        for q in quality:
            lines.append(f"«{q.get('text')}» {q['t0']:.2f}–{q['t1']:.2f} с: подсказка голос {q['hint_voiced']:.0%}, "
                         f"{q['hint_db']} дБ" + (f" — ПРОПУЩЕНО: {q['skip']}" if q.get("skip") else ""))
        for c in checks:
            lines.append(f"место {c['t0']:.2f}–{c['t1']:.2f} с: {c['verdict']} (голос: было {c['take_voiced']:.0%}, "
                         f"стало {c['new_voiced']:.0%})" + (f" — {c['kept']}" if c.get("kept") else ""))
        (d / "ЧИТАЙ.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return d
    except Exception as ex:  # noqa: BLE001
        E.log(f"Перепевка с текстом: части не сохранены: {ex}")
        return None


def analyze(ctx, source, lyrics, marks=None, language="auto"):
    """Только разбор: где в записи каждое слово текста, подозрительные (возможно, съедены), где найдены пометки.
    Для окна «Текст» — подсветка и «найденное место» до перепевки."""
    _need_executor()
    tmp = E.new_tmp("tfa")
    try:
        ctx.span(0.0, 1.0, "Текст по записи…")
        return _prepare(ctx, tmp, Path(source), lyrics, marks, language)
    finally:
        if not os.environ.get("DZOD_TEXTFIX_KEEP"):             # отладка: оставить промежуточные файлы
            shutil.rmtree(tmp, ignore_errors=True)


def revoice_text(ctx, source, take, item, lyrics, marks, out, language="auto", hint="auto"):
    """source — исходник (тот, что перепевали), take — готовый дубль, item — настройки той перепевки
    (model, index, pitch, index_rate, protect, …), marks — [{from, to, replace?}] по символам lyrics.
    hint: auto (английский — SoulX, русский — Piper) | piper. Пишет out; возвращает план, изменённые отрезки, время."""
    _need_executor()
    p = E.paths()
    workers = E.ensure_workers()
    source, take, out = Path(source), Path(take), Path(out)
    if not marks:
        raise E.EngineError("Нет пометок: выделите в тексте слова или слоги, которые нужно исправить.")
    tmp = E.new_tmp("tf")
    t_all = time.time()
    try:
        # 1. текст по записи, план пометок, ноты подсказки
        ctx.span(0.0, 0.25, "Текст по записи…")
        plan = _prepare(ctx, tmp, source, lyrics, marks, language)
        found = [m for m in plan["plan"] if m.get("found")]
        if not found:
            raise E.EngineError("Ни одна пометка не найдена в записи: проверьте, что текст совпадает с тем, что поётся.")
        # 2. подсказка нужного звучания: английский — SoulX-Singer поёт помеченные слова (с соседями) в их времени
        #    и высоте; русский (SoulX его не знает) — Piper произносит, слово растягивается на своё время
        if plan["language"] == "ru" or hint == "piper":
            if not E.piper_ready():
                ctx.span(0.25, 0.4, "Piper — первая установка…")
                E.install_piper(ctx)
            ctx.span(0.4, 0.55, "Подсказка звучания (Piper)…")
            pj = {"step": "piper", "plan": str(tmp / "plan.json"), "voice": str(p.exec_piper_voice),
                  "out_wav": str(tmp / "hint.wav"), "align_model_dir": str(p.exec / "align")}
            ctx.run([p.exec_py, workers / "textfix_worker.py", _job(tmp, "piper", pj)], env=E.exec_env(),
                    what="Подсказка звучания")
        else:
            _soulx_hint(ctx, tmp, p, workers)
        # 3. звук для содержания + окна перепевки
        ctx.span(0.55, 0.62, "Содержание для перепевки…")
        cj = {"step": "content", "input": str(source), "hint_wav": str(tmp / "hint.wav"), "plan": str(tmp / "plan.json"),
              "content_out": str(tmp / "content.wav"), "region_dir": str(tmp / "reg"), "regions_out": str(tmp / "regions.json"),
              "quality_out": str(tmp / "quality.json")}
        rc = ctx.run([p.exec_py, workers / "textfix_worker.py", _job(tmp, "content", cj)], env=E.exec_env(),
                     what="Содержание") or {}
        quality = rc.get("quality") or []
        regs = json.loads((tmp / "regions.json").read_text(encoding="utf-8"))
        if not regs:
            _keep_parts(tmp, out, quality, [], plan)
            raise E.EngineError("Подсказка звучания не получилась ни для одной пометки (пустая или не голосом) — "
                                "дубль не изменён. Промежуточные файлы — в папке «" + _parts_dir(out).name + "».")
        # 4. та же перепевка окон: высота и громкость — исходник, содержание — с подсказкой
        ctx.span(0.62, 0.95, "Перепевка помеченных мест…")
        base = {k: item[k] for k in APPLIO_KEYS if k in item}
        items = [dict(base, input=r["src"], content_input=r["content"], f0_spans=r["f0_spans"], output=r["out"],
                      label=f"место {k + 1}") for k, r in enumerate(regs)]
        ctx.run([p.applio_py, workers / "applio_worker.py", _job(tmp, "conv", {"items": items})], cwd=p.applio,
                what="Перепевка помеченных мест")
        # 5. вклейка в дубль — только помеченные места
        ctx.span(0.95, 1.0, "Вклейка…")
        out.parent.mkdir(parents=True, exist_ok=True)
        sj = {"step": "splice", "take": str(take), "regions": str(tmp / "regions.json"), "out": str(out)}
        r = ctx.run([p.exec_py, workers / "textfix_worker.py", _job(tmp, "splice", sj)], env=E.exec_env(), what="Вклейка") or {}
        checks = r.get("checks") or []
        parts = _keep_parts(tmp, out, quality, checks, plan)
        E.log(f"ПЕРЕПЕВКА С ТЕКСТОМ: {take.name} → {out.name}: пометок {len(marks)}, найдено {len(found)}, "
              f"мест {len(regs)}, вклеено {len(r.get('changed', []))}, {time.time() - t_all:.1f} с; "
              f"оценка: {[c.get('verdict') for c in checks]}; пропуски: {[q.get('skip') for q in quality if q.get('skip')]}")
        return {"file": str(out), "plan": plan["plan"], "words": plan["words"], "changed": r.get("changed", []),
                "checks": checks, "quality": quality, "parts": str(parts) if parts else None,
                "seconds": round(time.time() - t_all, 1)}
    finally:
        if not os.environ.get("DZOD_TEXTFIX_KEEP"):             # отладка: оставить промежуточные файлы
            shutil.rmtree(tmp, ignore_errors=True)
