# -*- coding: utf-8 -*-
"""DZOD Executor, этап E1 (проба внутри DZOD Vocalist): MIDI-ноты с текстом → английское пение (SoulX-Singer)
→ голос вокалиста (RVC, тот же код, что перепевка Vocalist/Revoicer) → WAV 48 кГц от начала MIDI.

Результаты: <корень>/Executor/<имя MIDI>/<имя MIDI> - DZOD <голос> Executor NN.wav (+ сырое пение SoulX
«… - SoulX.wav», разметка «… - разметка.json», замер «… - замер.json»)."""
import hashlib
import json
import shutil
import time
from pathlib import Path

import numpy as np

import audio as A
import exec_post as P
import engine as E
import songs as S
import voices as V

OUT_SR = 48000
DEFAULT_PRESET = "E1 Default"
PRESET_FILE = "preset.json"


# ------------------------------------------------------------------ голоса Executor (образец + вокалист + параметры)
# Решение автора 07.11 13:07: голос, который звучит в E1, сохранить навсегда. Голос Executor — папка
# <корень>/Executor/Голоса/<имя>/ с копией образца (prompt.wav + prompt.json), вокалистом и параметрами RVC;
# версия модели пения закреплена отпечатком (sha256 model.pt) и коммитом кода SoulX-Singer.
def presets_dir():
    d = E.root() / "Executor" / "Голоса"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sha256(path, cache={}):  # noqa: B006 — кэш на время работы программы
    import hashlib
    p = Path(path)
    key = (str(p), p.stat().st_size, int(p.stat().st_mtime))
    if key not in cache:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 22), b""):
                h.update(chunk)
        cache[key] = h.hexdigest()
    return cache[key]


def _last_sing():
    """Последнее пение (по файлам замера): вокалист и параметры — чем пел автор, когда понравилось."""
    best = None
    for f in (E.root() / "Executor").glob("*/*замер*.json"):
        if best is None or f.stat().st_mtime > best.stat().st_mtime:
            best = f
    if not best:
        return None
    try:
        return json.loads(best.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _voice_by_name(name):
    for v in V.list_voices():
        if v["name"] == name and v["has_model"]:
            return v
    return None


def list_presets():
    ensure_default_preset()
    out = []
    for d in sorted(presets_dir().iterdir()):
        f = d / PRESET_FILE
        if f.exists():
            try:
                m = json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                continue
            m["dir"] = str(d)
            m["default"] = m.get("name") == DEFAULT_PRESET
            out.append(m)
    out.sort(key=lambda m: (not m["default"], m.get("name", "")))
    return out


def get_preset(name):
    for m in list_presets():
        if m.get("name") == name:
            return m
    raise E.EngineError(f"Голос Executor «{name}» не найден.")


def save_preset(name, voice=None, opts=None, prompt_from=None, note="", params=None):
    """Сохраняет голос Executor: копия образца, вокалист, параметры RVC, отпечаток модели пения."""
    p = E.paths()
    name = (name or "").strip()
    if not name:
        raise E.EngineError("Нужно имя голоса Executor.")
    d = presets_dir() / S.safe_name(name, 60)
    d.mkdir(parents=True, exist_ok=True)
    src = Path(prompt_from) if prompt_from else p.exec_prompt_wav.parent
    if not (d / "prompt.wav").exists():
        shutil.copyfile(src / "en_prompt.wav" if (src / "en_prompt.wav").exists() else src / "prompt.wav", d / "prompt.wav")
        shutil.copyfile(src / "en_prompt.json" if (src / "en_prompt.json").exists() else src / "prompt.json", d / "prompt.json")
    o = dict(S.CONVERT_DEFAULTS)
    o.update({k: v for k, v in (opts or {}).items() if k in S.CONVERT_DEFAULTS})
    vv = None
    if voice:
        try:
            vv = V.get(voice)
        except Exception:  # noqa: BLE001
            vv = None
    m = {"kind": "dzod-executor-voice", "version": 1, "name": name, "note": note,
         "vocalist": {"slug": vv.slug, "name": vv.name} if vv else None,
         "convert": o,
         "exec": exec_params(params),
         "prompt": {"wav": "prompt.wav", "meta": "prompt.json", "sha256": _sha256(d / "prompt.wav"),
                    "source": f"SoulX-Singer {E.EXEC_COMMIT[:7]} example/audio/en_prompt.mp3 (Apache 2.0)"},
         "soulx": {"commit": E.EXEC_COMMIT, "hf_repo": E.EXEC_HF_REPO,
                   "model_sha256": _sha256(p.exec_model) if p.exec_model.exists() else None},
         "saved": time.strftime("%Y-%m-%d %H:%M:%S"), "from": f"{E.APP} {E.VERSION}"}
    (d / PRESET_FILE).write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    E.log(f"ГОЛОС EXECUTOR сохранён: {name} ({m['vocalist']})")
    return m


def ensure_default_preset():
    """«E1 Default» — голос, который звучал в E1: встроенный образец + вокалист и параметры последнего пения."""
    d = presets_dir() / DEFAULT_PRESET
    if (d / PRESET_FILE).exists() or not E.paths().exec_prompt_wav.exists():
        return
    last = _last_sing() or {}
    v = _voice_by_name(last.get("voice")) if last.get("voice") else None
    opts = {k: v2 for k, v2 in (last.get("opts") or {}).items() if k in S.CONVERT_DEFAULTS}
    save_preset(DEFAULT_PRESET, v["slug"] if v else None, opts,
                note="Голос, который звучал в Executor E1 (07.11.2026): встроенный английский образец SoulX-Singer → RVC.")


def out_dir(midi):
    d = E.root() / "Executor" / S.safe_name(Path(midi).stem, 60)
    d.mkdir(parents=True, exist_ok=True)
    return d


def check_midi(ctx, midi):
    """Только разбор нот и текста (без видеокарты): число фраз, предупреждения, разметка."""
    p = E.paths()
    workers = E.ensure_workers()
    tmp = E.new_tmp("exec")
    job = {"midi": str(midi), "meta_out": str(tmp / "meta.json"), "only_meta": True}
    (tmp / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    r = ctx.run([p.exec_py, workers / "executor_worker.py", tmp / "job.json"], env=E.exec_env(), what="Разбор MIDI") or {}
    r["meta"] = json.loads((tmp / "meta.json").read_text(encoding="utf-8"))
    shutil.rmtree(tmp, ignore_errors=True)
    return r


# Параметры Executor (ТЗ §1.1/§2.3; имена, диапазоны и значения по умолчанию — как у Omnivocal)
EXEC_DEFAULTS = {
    "style": "dynamic",       # Dynamic — живая высота модели; Straight — ровно по нотам (+ вибрато ниже)
    "formant": 50.0,          # 0–100: цвет голоса (сдвиг формант 0,8…1,25)
    "attack": 50.0, "air": 50.0,
    "transpose": 0, "fine_tune": 0.0, "bend_range": 0,
    "power": 65.0, "velocity_power": True,
    "vibrato": 50.0, "vib_rate": 5.5, "vib_delay": 250,   # в Straight: глубина, Гц, мс
    "breath": True, "breath_level": 40.0,
    "presence": 10.0, "output": 0.0,
}


def exec_params(*layers):
    o = dict(EXEC_DEFAULTS)
    for lay in layers:
        o.update({k: v for k, v in (lay or {}).items() if k in EXEC_DEFAULTS and v is not None})
    return o


def _formant_factor(f):
    return float(2 ** ((float(f) - 50.0) / 50.0 * np.log2(1.25)))


STYLES = ("normal", "soft", "whisper", "belt", "shout", "falsetto", "spoken")


def sing(ctx, midi, voice=None, opts=None, preset=None, params=None, styles=None, curves=None, rng=None):
    """MIDI с текстом → пение. preset — голос Executor (образец + вокалист + параметры); voice, opts (RVC) и params
    (параметры Executor) — поверх него. Без preset поёт «E1 Default» (если есть).
    Кэш: каждая фраза SoulX и её голос RVC запоминаются; при повторе меняются только изменённые фразы.
    styles — отрезки манер [{start, end, style, fade}], curves — {power|air|vibrato|pitch: [[t, v], …]},
    rng — [t0, t1]: SING SELECTION (поются только задетые фразы). Время — секунды от тика 0 MIDI."""
    o = dict(S.CONVERT_DEFAULTS)
    pr = None
    if preset or not voice:
        ensure_default_preset()
        try:
            pr = get_preset(preset or DEFAULT_PRESET)
        except E.EngineError:
            if preset:
                raise
    if pr:
        o.update(pr.get("convert") or {})
        if not voice and pr.get("vocalist"):
            voice = pr["vocalist"]["slug"]
    if not voice:
        raise E.EngineError("Не выбран вокалист (и в голосе Executor он не записан).")
    o.update({k: v for k, v in (opts or {}).items() if k in S.CONVERT_DEFAULTS})
    ep = exec_params(pr.get("exec") if pr else None, params)
    midi = Path(midi)
    if not midi.is_file():
        raise E.EngineError(f"MIDI не найден: {midi}")
    st = E.executor_status()
    if not st["ready"]:
        raise E.EngineError("Executor не установлен: вкладка «Executor (проба)» → «Установить».")
    model, index = V.model_files(voice)
    vname = V.get(voice).name
    p = E.paths()
    workers = E.ensure_workers()
    dst = out_dir(midi)
    tmp = E.new_tmp("exec")
    cache = p.exec / "cache"
    try:
        # 1–2. ноты и текст → пение SoulX-Singer (по фразам, с кэшем)
        ctx.span(0.0, 0.6, "Пение по нотам (SoulX-Singer)…")
        raw = tmp / "soulx.wav"
        (tmp / "segs").mkdir()
        job = {"midi": str(midi), "src_dir": str(p.exec_src), "model": str(p.exec_model),
               "prompt_wav": str(Path(pr["dir"]) / pr["prompt"]["wav"]) if pr else str(p.exec_prompt_wav),
               "prompt_meta": str(Path(pr["dir"]) / pr["prompt"]["meta"]) if pr else str(p.exec_prompt_meta),
               "out_wav": str(raw), "meta_out": str(tmp / "meta.json"), "notes_out": str(tmp / "notes.json"),
               "seg_dir": str(tmp / "segs"), "cache_dir": str(cache / "soulx"), "fp16": True,
               "pitch_shift": int(ep["transpose"]), "style": ep["style"], "vibrato": float(ep["vibrato"]),
               "vib_rate": float(ep["vib_rate"]), "vib_delay": float(ep["vib_delay"]) / 1000.0,
               "bend_range": float(ep["bend_range"]),
               "styles": [x for x in (styles or []) if x.get("style") in STYLES],
               "curves": {k: v for k, v in (curves or {}).items() if k in ("power", "air", "vibrato", "pitch") and v},
               "range": list(rng) if rng else None}
        (tmp / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        t0 = time.time()
        r1 = ctx.run([p.exec_py, workers / "executor_worker.py", tmp / "job.json"], cwd=p.exec_src,
                     env=E.exec_env(), what="Пение по нотам") or {}
        t_sing = time.time() - t0
        notes = json.loads((tmp / "notes.json").read_text(encoding="utf-8"))
        # 3. голос вокалиста (RVC) — по фразам, с кэшем
        ctx.span(0.6, 0.93, f"Голос «{vname}» (RVC)…")
        it0 = {k: o[k] for k in o if k != "match_level"}
        it0.update(model=str(model), index=str(index or ""), pitch=float(ep["fine_tune"]) / 100.0,
                   formant=_formant_factor(ep["formant"]))
        mst = Path(model).stat()
        sig = json.dumps({"v": 1, "m": [str(model), mst.st_size, int(mst.st_mtime)], "it": it0}, sort_keys=True, default=str)
        rvc_cache = cache / "rvc"
        rvc_cache.mkdir(parents=True, exist_ok=True)
        segs = r1.get("seg_files") or []
        items, outs = [], []
        for sgi, sg in enumerate(segs):
            if sg.get("silent"):
                continue
            key = hashlib.sha1((sig + sg["key"]).encode()).hexdigest()[:24]
            cf = rvc_cache / f"{key}.wav"
            outs.append((sg, cf))
            if not cf.exists():
                inp = tmp / f"rvc_in_{sgi:03d}.wav"
                A.to_wav(sg["path"], inp, mono=True)
                it = dict(it0, input=str(inp), output=str(cf), label=f"фраза {sgi + 1}")
                items.append(it)
        t1 = time.time()
        if items:
            (tmp / "conv.json").write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
            ctx.run([p.applio_py, workers / "applio_worker.py", tmp / "conv.json"], cwd=p.applio, what="Голос вокалиста")
        t_rvc = time.time() - t1
        # 4. сборка 48 кГц от начала MIDI + обработка Executor (Power, Attack, Air, вдохи, шёпот, Presence, Output)
        ctx.span(0.93, 0.99, "Обработка: Power, Attack, Air, вдохи, Presence…")
        n = int(round(A.info(raw)["frames"] / A.info(raw)["sr"] * OUT_SR))
        x = np.zeros(n, dtype=np.float64)
        for sg, cf in outs:
            if not cf.exists():
                raise E.EngineError("Голос вокалиста не получился (нет файла RVC).")
            y, sr = A.read(cf, mono=True)
            y = A.resample(y, sr, OUT_SR)[:, 0]
            if o.get("match_level", True):
                src_seg, ssr = A.read(sg["path"], mono=True)
                a_src, a_y = A.active_rms(src_seg[:, 0], ssr), A.active_rms(y, OUT_SR)
                if a_src > 1e-5 and a_y > 1e-5:
                    y = y * min(_db_lin(18), a_src / a_y)
            a = int(round(sg["start"] * OUT_SR))
            m = min(len(y), n - a)
            x[a:a + m] += y[:m]
        x = P.process(x, OUT_SR, notes, ep, curves=curves, styles=styles)
        # фразы, которые не пелись (SING SELECTION мимо и нет в кэше), остаются тишиной — без вдохов и обработки
        for sg in segs:
            if sg.get("silent"):
                a0, b0 = int(round(sg["start"] * OUT_SR)), min(n, int(round(sg["end"] * OUT_SR)))
                x[a0:b0] = 0.0
        base = S.safe_name(midi.stem, 60)
        k = 1
        while (dst / f"{base} - DZOD {S.safe_name(vname, 30)} Executor {k:02d}.wav").exists():
            k += 1
        final = dst / f"{base} - DZOD {S.safe_name(vname, 30)} Executor {k:02d}.wav"
        A.write(final, x[:, None], OUT_SR)
        shutil.copyfile(raw, dst / f"{base} - SoulX {k:02d}.wav")
        shutil.copyfile(tmp / "meta.json", dst / f"{base} - разметка.json")
        secs = n / OUT_SR
        measure = {"version": E.VERSION, "midi": str(midi), "voice": vname, "voice_slug": voice,
                   "preset": pr.get("name") if pr else None, "seconds_audio": round(secs, 2),
                   "soulx": {k2: r1.get(k2) for k2 in ("t_load", "t_synth", "rtf", "vram_gb", "device", "fp16", "segments",
                                                       "sung", "control")},
                   "rvc_phrases": len(items), "phrases": len(segs),
                   "t_sing_total": round(t_sing, 1), "t_rvc": round(t_rvc, 1),
                   "rtf_total": round((t_sing + t_rvc) / max(secs, 1e-6), 3), "warnings": r1.get("warnings", []),
                   "opts": {k2: v for k2, v in it0.items() if k2 not in ("model", "index")}, "exec": ep}
        (dst / f"{base} - замер {k:02d}.json").write_text(json.dumps(measure, ensure_ascii=False, indent=1), encoding="utf-8")
        E.log(f"EXECUTOR: {final.name}; звук {secs:.1f} с, фраз {len(segs)} (спето заново {r1.get('sung')}, "
              f"голос заново {len(items)}), пение {t_sing:.1f} с, RVC {t_rvc:.1f} с")
        sty = [x for x in (styles or []) if x.get("style") in STYLES and x.get("style") != "normal"]
        summ = ", ".join(f'{x["style"]} {float(x["start"]):.1f}–{float(x["end"]):.1f} с' for x in sty[:8])
        E.log(f"EXECUTOR: манеры — {summ + (' …' if len(sty) > 8 else '') if sty else 'нет (normal)'}; "
              f"кривые — {', '.join(k2 for k2, v in (curves or {}).items() if v) or 'нет'}")
        ctx.span(1.0, 1.0, "Готово." if items or r1.get("sung") else "Готово: голос — из кэша, манеры и кривые применены заново.")
        phrases = r1.get("phrases") or []
        # изменённые фразы: перепетые + задетые манерами/кривыми (обработка после голоса делается всегда)
        marks = [(float(x["start"]), float(x["end"])) for x in sty]
        for v in (curves or {}).values():
            if v:
                ts = [float(a) for a, _b in v]
                marks.append((min(ts), max(ts)))
        changed = [[ph["start"], ph["end"]] for ph in phrases
                   if ph.get("sung") or (not ph.get("silent") and any(a < ph["end"] and b > ph["start"] for a, b in marks))]
        return {"file": str(final), "raw": str(dst / f"{base} - SoulX {k:02d}.wav"), "dir": str(dst),
                "measure": measure, "warnings": r1.get("warnings", []), "phrases": phrases,
                "styles_applied": [x["style"] for x in sty], "changed": changed}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _db_lin(db):
    return 10 ** (db / 20.0)


def guide(ctx, midi):
    """Guide Tone: простой тон по нотам (без видеокарты) — проверить высоту и ритм, пока ноты набираются."""
    p = E.paths()
    workers = E.ensure_workers()
    tmp = E.new_tmp("exec")
    try:
        job = {"midi": str(midi), "meta_out": str(tmp / "meta.json"), "notes_out": str(tmp / "notes.json"), "only_meta": True}
        (tmp / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        ctx.run([p.exec_py, workers / "executor_worker.py", tmp / "job.json"], env=E.exec_env(), what="Разбор MIDI")
        notes = json.loads((tmp / "notes.json").read_text(encoding="utf-8"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    end = max((x["end"] for x in notes), default=0) + 0.5
    n = int(end * OUT_SR)
    y = np.zeros(n)
    for x in notes:
        if x["pitch"] <= 0:
            continue
        a, b = int(x["start"] * OUT_SR), min(n, int(x["end"] * OUT_SR))
        t = np.arange(b - a) / OUT_SR
        f = 440.0 * 2 ** ((x["pitch"] - 69) / 12.0)
        tone = 0.6 * np.sin(2 * np.pi * f * t) + 0.25 * np.sin(4 * np.pi * f * t) + 0.1 * np.sin(6 * np.pi * f * t)
        env = np.minimum(1, np.minimum(t / 0.01, (t[-1] - t) / 0.03 + 1e-9)) if len(t) else t
        y[a:b] += tone * np.clip(env, 0, 1) * 0.25 * (x["vel"] / 127.0)
    dst = out_dir(midi)
    f = dst / f"{S.safe_name(Path(midi).stem, 60)} - Guide Tone.wav"
    A.write(f, y[:, None].astype(np.float32), OUT_SR)
    return {"file": str(f), "notes": len([x for x in notes if x["pitch"] > 0])}


def results(midi):
    d = out_dir(midi)
    return sorted((str(f) for f in d.glob("*Executor*.wav")), reverse=True) + [str(f) for f in d.glob("* - Guide Tone.wav")]


# ------------------------------------------------------------------ MIDI из вокала (ТЗ vocal-to-midi, 08.11.2026)
def transcribe(ctx, wav, lyrics, language="auto", detail="notes", midi_out=None):
    """WAV вокала + текст песни → ноты со слогами (JSON §2 ТЗ) и, по желанию, MIDI с текстом нот.
    Работает в среде Executor; высота — RMVPE из Applio (как в RVC), выравнивание текста — wav2vec2 (MMS_FA)."""
    wav = Path(wav)
    if not wav.is_file():
        raise E.EngineError(f"Файл вокала не найден: {wav}")
    # без текста — только ноты (слоги пустые); с текстом — ноты со слогами
    if not E.executor_status()["ready"]:
        raise E.EngineError("Нужен установленный Executor (вкладка «4 Executor» → «Установить»): в нём модели выравнивания текста.")
    p = E.paths()
    workers = E.ensure_workers()
    tmp = E.new_tmp("tr")
    try:
        inp = tmp / "in.wav"
        A.to_wav(wav, inp, mono=True)
        (p.exec / "align").mkdir(parents=True, exist_ok=True)
        job = {"input": str(inp), "lyrics": lyrics, "language": language or "auto", "detail": detail or "notes",
               "out_json": str(tmp / "out.json"), "midi_out": str(tmp / "out.mid"),
               "rmvpe": str(p.applio / "rvc" / "models" / "predictors" / "rmvpe.pt"),
               "rmvpe_py": str(p.applio / "rvc" / "lib" / "predictors" / "RMVPE.py"),
               "align_model_dir": str(p.exec / "align")}
        (tmp / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        ctx.span(0.0, 1.0, "MIDI из вокала…")
        ctx.run([p.exec_py, workers / "transcribe_worker.py", tmp / "job.json"], env=E.exec_env(), what="MIDI из вокала")
        res = json.loads((tmp / "out.json").read_text(encoding="utf-8"))
        dst = E.root() / "Executor" / "MIDI из вокала"
        dst.mkdir(parents=True, exist_ok=True)
        base = S.safe_name(wav.stem, 60)
        mid = Path(midi_out) if midi_out else dst / f"{base} - MIDI из вокала.mid"
        mid.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tmp / "out.mid", mid)
        (dst / f"{base} - MIDI из вокала.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        res["midi"] = str(mid)
        E.log(f"MIDI ИЗ ВОКАЛА: {wav.name} → {len(res['notes'])} нот, {len(res['words'])} слов, {res.get('t_total')} с")
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
