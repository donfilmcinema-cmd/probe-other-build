# -*- coding: utf-8 -*-
"""DZOD Vocalist — ядро без окна: обработка файла по JSON-запросу.

Для встраивания в DZOD Studio (и для любых других программ): модуль Studio отдаёт аудио клипа
файлом, запускает программу с запросом и получает обработанные файлы той же длины и частоты.

    "DZOD Vocalist.exe" --request C:\\path\\request.json

Запрос (UTF-8 JSON):
    {
      "version": 1,
      "action": "convert" | "clean" | "clean_convert" | "voices" | "status" | "sing",
      "midi": "C:\\...\\vocal.mid",           # для sing (DZOD Executor, проба): MIDI с текстом у нот
      "input": "C:\\...\\clip.wav",           # для clean / convert / clean_convert
      "output_dir": "C:\\...\\out",            # куда положить результат (создаётся)
      "name": "Vocal 01",                       # основа имён файлов (по умолчанию — имя входа)
      "naming": "resinger",                     # необязательно: имена как у DZOD Revoicer — <name>_ResingerNN.wav
      "voice": "dzod_vokalistka",               # код голоса (из action=voices); для convert
      "parts": ["lead"],                        # что перепевать при clean_convert: lead / backing
      "clean": {"mix": false, "backing": true, "dereverb": "uvr", "dereverb_aggr": 50, "denoise": false, "quality": "fast"},
      "convert": {"pitch": 0, "index_rate": 0.6, "protect": 0.33, ...},   # как в окне программы
      "result": "C:\\...\\result.json",         # куда записать ответ (по умолчанию <output_dir>\\result.json)
      "progress": "C:\\...\\progress.txt"       # необязательно: ход работы одной строкой «0.42 текст»
    }

Ответ (result.json):
    {"ok": true, "action": "...", "files": [{"role": "lead" | "backing" | "dzod_lead" | ..., "path": "..."}],
     "voices": [...], "engine": {...}, "seconds": 12.3}
    при ошибке: {"ok": false, "error": "текст для человека"}

Код выхода программы: 0 — успех, 1 — ошибка (подробности в result.json и в журнале).
Все звуковые результаты — WAV 24 бит той же частоты и ровно той же длины, что вход."""
import json
import os
import shutil
import time
import traceback
from pathlib import Path

import audio as A
import engine as E
import songs as S
import voices as V

API_VERSION = 2                       # 1 и 2 (sing v2: params/styles/curves/range); старше — «обновите»
ACTIONS = ("convert", "clean", "clean_convert", "voices", "status", "sing", "transcribe", "text_analyze", "revoice_text")


def _progress_writer(path):
    if not path:
        return lambda f, m="": None
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    last = {"t": 0.0, "line": ""}

    def write(frac, msg=""):
        line = f"{max(0.0, min(1.0, frac)):.3f} {msg}".strip()
        now = time.time()
        if line == last["line"] or (now - last["t"] < 0.25 and frac < 1):
            return
        last.update(t=now, line=line)
        try:
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(line, encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            pass
    return write


def _sep_job(ctx, src_wav, work, opts):
    p = E.paths()
    if not p.sep_py.exists():
        raise E.EngineError("Движок DZOD Vocalist не установлен: откройте программу DZOD Vocalist, вкладка «Движок».")
    workers = E.ensure_workers()
    o = dict(S.CLEAN_DEFAULTS)
    o.update({k: v for k, v in (opts or {}).items() if k in S.CLEAN_DEFAULTS})
    if not (o["mix"] or o["backing"] or o["dereverb"] != "off" or o["denoise"]):
        raise E.EngineError("В запросе не выбрано ни одного шага чистки.")
    job = dict(o, input=str(src_wav), work_dir=str(work / "sep"), models_dir=str(p.sep_models))
    (work / "sep.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    return ctx.run([p.sep_py, workers / "sep_worker.py", work / "sep.json"], cwd=work, what="Чистка") or {}


def _convert_job(ctx, items, work):
    p = E.paths()
    if not p.applio_py.exists():
        raise E.EngineError("Движок DZOD Vocalist не установлен: откройте программу DZOD Vocalist, вкладка «Движок».")
    workers = E.ensure_workers()
    (work / "conv.json").write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
    ctx.run([p.applio_py, workers / "applio_worker.py", work / "conv.json"], cwd=p.applio, what="Перепевка")


def process(ctx, req):
    """Выполняет запрос; возвращает словарь ответа (без поля ok)."""
    action = req.get("action")
    if action not in ACTIONS:
        raise E.EngineError(f"Неизвестное действие «{action}». Возможные: {', '.join(ACTIONS)}.")
    if action == "status":
        return {"engine": E.engine_status(), "version": E.VERSION, "api": API_VERSION}
    if action == "voices":
        out = {"voices": [v for v in V.list_voices()]}
        if E.executor_status()["ready"]:
            import executor as X
            out["executor_voices"] = [{"name": m["name"], "default": m["default"], "vocalist": m.get("vocalist"),
                                       "note": m.get("note", "")} for m in X.list_presets()]
        return out
    if action == "transcribe":
        # MIDI из вокала: {"input": WAV, "lyrics": "...", "language": "auto|en|ru", "detail": "notes|detailed", "midi_out": "..."}
        import executor as X
        r = X.transcribe(ctx, req.get("input") or "", req.get("lyrics") or "", req.get("language") or "auto",
                         req.get("detail") or "notes", req.get("midi_out"))
        r["files"] = [{"role": "midi", "path": r.pop("midi")}]
        return r
    if action == "text_analyze":
        # Перепевка с текстом — разбор: {"input": исходник, "lyrics": "...", "marks": [{from, to, replace?}], "language"}
        # → где каждое слово текста в записи, подозрительные слова (suspect), где найдены пометки (plan)
        import textfix as TF
        r = TF.analyze(ctx, req.get("input") or "", req.get("lyrics") or "", req.get("marks"), req.get("language") or "auto")
        return {"words": r["words"], "plan": r["plan"], "language": r["language"], "seconds": r["seconds"]}
    if action == "revoice_text":
        # Перепевка с текстом — исправление: {"input": исходник, "take": готовый дубль (результат convert),
        #  "voice": "<код голоса>", "convert": {...как в convert...}, "lyrics": "...", "marks": [...], "output": WAV}
        import textfix as TF
        voice = req.get("voice")
        if not voice:
            raise E.EngineError("В запросе не указан голос (voice).")
        src, take = Path(req.get("input") or ""), Path(req.get("take") or "")
        for f, what in ((src, "исходник (input)"), (take, "дубль (take)")):
            if not f.is_file():
                raise E.EngineError(f"Не найден {what}: {f}")
        model, index = V.model_files(voice)
        o = dict(S.CONVERT_DEFAULTS)
        o.update({k: v for k, v in (req.get("convert") or {}).items() if k in S.CONVERT_DEFAULTS})
        item = {k: o[k] for k in o if k != "match_level"}
        item.update(model=str(model), index=str(index or ""))
        out = Path(req.get("output") or "")
        if not req.get("output"):
            import revoicer as R
            out = R.next_name(take)
        tmp = E.new_tmp("rt")
        try:
            src16 = tmp / "src.wav"
            A.to_wav(src, src16, mono=True)
            r = TF.revoice_text(ctx, src16, take, item, req.get("lyrics") or "", req.get("marks") or [], out,
                                req.get("language") or "auto")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return {"files": [{"role": "dzod_lead", "path": r["file"], "voice": voice}], "plan": r["plan"],
                "changed": r["changed"], "checks": r.get("checks", []),
                "skipped": [q for q in r.get("quality", []) if q.get("skip")], "parts": r.get("parts"),
                "seconds": r["seconds"]}
    if action == "sing":
        # DZOD Executor (E1): {"midi": "...mid", "voice": "<код голоса>", "convert": {...}, "output_dir": "..."}
        import executor as X
        r = X.sing(ctx, req.get("midi") or "", req.get("voice") or None, req.get("convert"), req.get("voice_preset"),
                   req.get("params"), req.get("styles"), req.get("curves"), req.get("range"))
        files = [{"role": "dzod_sing", "path": r["file"]}, {"role": "soulx_raw", "path": r["raw"]}]
        if req.get("output_dir"):
            od = Path(req["output_dir"])
            od.mkdir(parents=True, exist_ok=True)
            for f in files:
                d = od / Path(f["path"]).name
                shutil.copyfile(f["path"], d)
                f["path"] = str(d)
        return {"files": files, "measure": r["measure"], "phrases": r["phrases"], "changed": r["changed"]}

    src = Path(req.get("input") or "")
    if not src.is_file():
        raise E.EngineError(f"Входной файл не найден: {src}")
    out_dir = Path(req.get("output_dir") or src.parent)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = S.safe_name(req.get("name") or src.stem)
    work = E.new_tmp("req")
    files = []
    try:
        ctx.span(0.0, 0.04, "Готовлю файл…")
        A.to_wav(src, work / "in.wav")
        sources = {"lead": src}

        if action in ("clean", "clean_convert"):
            ctx.span(0.04, 0.55 if action == "clean_convert" else 0.95, "Чистка…")
            res = _sep_job(ctx, work / "in.wav", work, req.get("clean"))
            names = {"lead": "вокал чистый", "backing": "бэки", "reverb": "реверберация (убрано)",
                     "noise": "шум (убрано)", "music": "музыка без вокала"}
            for role, label in names.items():
                f = res.get(role)
                if f and os.path.exists(f):
                    dst = out_dir / f"{base} - {label}.wav"
                    A.conform(f, dst, src, match_level=False)
                    files.append({"role": role, "path": str(dst)})
                    sources[role] = dst

        if action in ("convert", "clean_convert"):
            voice = req.get("voice")
            if not voice:
                raise E.EngineError("В запросе не указан голос (voice).")
            model, index = V.model_files(voice)
            vname = V.get(voice).name
            o = dict(S.CONVERT_DEFAULTS)
            o.update({k: v for k, v in (req.get("convert") or {}).items() if k in S.CONVERT_DEFAULTS})
            parts = req.get("parts") or ["lead"]
            if action == "convert":
                parts = ["lead"]
            items, roles = [], []
            for role in parts:
                if role not in sources:
                    continue
                inp = work / f"c_{role}.wav"
                A.to_wav(sources[role], inp, mono=True)
                it = {k: o[k] for k in o if k != "match_level"}
                it.update(input=str(inp), output=str(work / f"o_{role}.wav"), model=str(model),
                          index=str(index or ""), label=S.E_ROLE.get(role, role))
                items.append(it)
                roles.append(role)
            if not items:
                raise E.EngineError("Нечего перепевать: в результате чистки нет нужной дорожки.")
            ctx.span(0.55 if action == "clean_convert" else 0.04, 0.96, "Перепевка…")
            _convert_job(ctx, items, work)
            for role, it in zip(roles, items):
                out = Path(it["output"])
                if not out.exists():
                    continue
                part = "вокал" if role == "lead" else "бэки"
                if req.get("naming") == "resinger":
                    # как в DZOD Revoicer: <имя>_ResingerNN.wav (бэки — <имя> backing_ResingerNN.wav)
                    import revoicer as R
                    dst = R.next_name(out_dir / (f"{base}.wav" if role == "lead" else f"{base} backing.wav"))
                else:
                    dst = out_dir / f"{base} - DZOD {S.safe_name(vname, 30)} {part}.wav"
                    k = 2
                    while dst.exists():
                        dst = out_dir / f"{base} - DZOD {S.safe_name(vname, 30)} {part} {k:02d}.wav"
                        k += 1
                A.conform(out, dst, sources[role], match_level=o["match_level"])
                files.append({"role": "dzod_" + role, "path": str(dst), "voice": voice})
        ctx.span(1.0, 1.0, "Готово.")
        return {"files": files}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_request(path):
    """Точка входа для «--request»: читает запрос, пишет ответ; возвращает код выхода."""
    t0 = time.time()
    req_path = Path(path)
    result_path = None
    try:
        req = json.loads(req_path.read_text(encoding="utf-8-sig"))
        result_path = Path(req.get("result") or (Path(req.get("output_dir") or req_path.parent) / "result.json"))
        if int(req.get("version", API_VERSION)) > API_VERSION:
            raise E.EngineError(f"Запрос версии {req.get('version')}, а программа понимает версию {API_VERSION}. Обновите DZOD Vocalist.")
        E.log(f"ЗАПРОС {req_path}: {req.get('action')}")
        ctx = E.Ctx(_progress_writer(req.get("progress")))
        out = process(ctx, req)
        out.update(ok=True, action=req.get("action"), seconds=round(time.time() - t0, 1))
        code = 0
    except Exception as ex:  # noqa: BLE001
        if not isinstance(ex, (E.EngineError, A.AudioError)):
            E.log("ОШИБКА ЗАПРОСА:\n" + traceback.format_exc())
        out = {"ok": False, "error": str(ex) or ex.__class__.__name__, "seconds": round(time.time() - t0, 1)}
        code = 1
    if result_path is None:
        result_path = req_path.with_name("result.json")
    try:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = result_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, result_path)
    except OSError as ex:
        E.log(f"не удалось записать ответ {result_path}: {ex}")
        code = 1
    return code
