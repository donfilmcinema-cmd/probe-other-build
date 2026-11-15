# -*- coding: utf-8 -*-
"""DZOD Vocalist 0.8.0 — «Качество»: мерило перепевки (письмо sound 15.11 23:00).

Отчёт по дублю: разборчивость (Whisper: WER против текста автора / исходника, съеденные и искажённые слова с временем),
яркость по словам (>2/>4 кГц, дубль / исходник), динамика (огибающая 20 мс), артефакты (всплески ВЧ, щелчки, «металл»).
Файл рядом с дублем: «<дубль> - качество.json» (без звука — можно пересылать).
Нужен Executor (выравнивание текста, torch). Модель распознавания — Whisper large-v3-turbo (~1,6 ГБ), ставится
сама при первой проверке (нужен интернет один раз)."""
import json
import os
import shutil
import time
from pathlib import Path

import engine as E

WHISPER_REPO = os.environ.get("DZOD_WHISPER_REPO", "openai/whisper-large-v3-turbo")
WHISPER_FILES = ["config.json", "generation_config.json", "model.safetensors", "preprocessor_config.json",
                 "tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt", "normalizer.json",
                 "added_tokens.json", "special_tokens_map.json"]


def whisper_dir():
    return E.paths().exec / "models" / ("whisper-" + WHISPER_REPO.split("/")[-1])


def whisper_ready():
    d = whisper_dir()
    return (d / ".done").exists() and (d / "model.safetensors").exists()


def install_whisper(ctx):
    """Whisper для мерила: файлы модели с huggingface.co (один раз)."""
    if whisper_ready():
        return
    d = whisper_dir()
    d.mkdir(parents=True, exist_ok=True)
    n = len(WHISPER_FILES)
    for k, f in enumerate(WHISPER_FILES):
        ctx.span(k / n, (k + 1) / n, f"Модель распознавания речи (Whisper): {f}…")
        if not (d / f).exists():
            E.download(f"https://huggingface.co/{WHISPER_REPO}/resolve/main/{f}", d / f, ctx, "Whisper")
    (d / ".done").write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")


def report_path(take):
    take = Path(take)
    return take.parent / (take.stem + " - качество.json")


def measure(ctx, source, take, lyrics="", language="auto", out=None):
    """Мерило дубля. Возвращает отчёт (dict) и пишет его рядом с дублем."""
    if not E.executor_status()["ready"]:
        raise E.EngineError("Для проверки качества нужен установленный Executor (вкладка «4 Executor» → «Установить»): "
                            "в нём распознавание речи и выравнивание текста.")
    source, take = Path(source), Path(take)
    for f in (source, take):
        if not f.exists():
            raise E.EngineError(f"Нет файла: {f}")
    p = E.paths()
    workers = E.ensure_workers()
    fresh = not whisper_ready()
    if fresh:
        ctx.span(0.0, 0.3, "Модель распознавания речи — первая установка (~1,6 ГБ)…")
        install_whisper(ctx)
    out = Path(out) if out else report_path(take)
    tmp = E.new_tmp("q")
    try:
        (p.exec / "align").mkdir(parents=True, exist_ok=True)
        job = {"source": str(source), "take": str(take), "lyrics": lyrics or "", "language": language or "auto",
               "out_json": str(out), "whisper": str(whisper_dir()), "align_model_dir": str(p.exec / "align"),
               "version": E.VERSION}
        if os.environ.get("DZOD_QUALITY_DEVICE"):
            job["device"] = os.environ["DZOD_QUALITY_DEVICE"]
        f = tmp / "quality.json"
        f.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        ctx.span(0.3 if fresh else 0.0, 1.0, "Проверка качества…")
        ctx.run([p.exec_py, workers / "quality_worker.py", f], env=E.exec_env(), what="Проверка качества")
        rep = json.loads(out.read_text(encoding="utf-8"))
        c = rep["checks"]
        E.log(f"КАЧЕСТВО {take.name}: WER {c['wer']['take']} (исходник {c['wer']['source']}), "
              f"яркость {c['brightness']['median_ratio']}, огибающая {c['dynamics'].get('median_db')} дБ, "
              f"щелчков {c['clicks']['count']}, проблемных слов {len(rep['problems'])}, {rep['time_s']} с")
        return rep
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def summary(rep):
    """Коротко для списка дублей."""
    c = rep["checks"]
    return {"passed": rep["passed"], "wer": c["wer"]["take"], "wer_src": c["wer"]["source"],
            "bright": c["brightness"]["median_ratio"], "env_db": c["dynamics"].get("median_db"),
            "clicks": c["clicks"]["count"], "problems": len(rep["problems"]),
            "ok": {k: v["ok"] for k, v in c.items()}, "at": time.strftime("%Y-%m-%d %H:%M")}
