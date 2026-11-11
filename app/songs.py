# -*- coding: utf-8 -*-
"""Песни: исходник → чистка → перепевка голосом DZOD. Всё лежит в <корень>\\Songs\\<песня>.
Каждый результат — WAV той же частоты и ровно той же длины, что исходник:
его ставят в проект Samplitude с начала, и он совпадает по времени."""
import json
import os
import re
import shutil
import time
from pathlib import Path

import audio as A
import engine as E

CLEAN_DEFAULTS = {"mix": False, "backing": True, "quality": "max", "dereverb": "uvr", "dereverb_aggr": 50, "denoise": False,
                  "backing_same": False}

CONVERT_DEFAULTS = {"pitch": 0, "index_rate": 0.6, "protect": 0.33, "volume_envelope": 1.0,
                    "f0_method": "rmvpe", "f0_autotune": False, "f0_autotune_strength": 0.4,
                    "clean_audio": False, "clean_strength": 0.4, "split_audio": False,
                    "proposed_pitch": False, "match_level": True}

BUILTIN_PRESETS = {
    "Стандарт": {},
    "Надрыв и крик": {"index_rate": 0.5, "protect": 0.2, "volume_envelope": 0.6},
    "Тихая партия": {"index_rate": 0.7, "protect": 0.4, "volume_envelope": 0.85},
    "Максимум тембра": {"index_rate": 0.85, "protect": 0.33},
}
PRESET_TEXT = {
    "Стандарт": "Средние настройки. Подходит большинству партий — начинайте с него.",
    "Надрыв и крик": "Сохраняет рваную подачу и громкостные удары исходника. Для гранжа, крика, хрипа.",
    "Тихая партия": "Бережёт дыхание и согласные, тембр сильнее. Для мягких куплетов и шёпота.",
    "Максимум тембра": "Голос максимально похож на модель. На сложных местах возможны призвуки.",
}
OLD_PRESET_NAMES = {"Ровно": "Стандарт", "Гранж — надрыв": "Надрыв и крик", "Тихий куплет": "Тихая партия"}

ROLES = {
    "source": "исходник",
    "lead": "вокал чистый",
    "backing": "бэки",
    "reverb": "убранная реверберация",
    "noise": "убранный шум",
    "music": "музыка без вокала",
}


def safe_name(s, limit=60):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(s)).strip().strip(".")
    s = re.sub(r"\s+", " ", s)
    return (s[:limit].rstrip() or "Песня")


class Song:
    def __init__(self, folder):
        self.dir = Path(folder)
        self.meta_path = self.dir / "song.json"
        try:
            self.m = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception:
            self.m = {}

    @property
    def id(self):
        return self.dir.name

    @property
    def name(self):
        return self.m.get("name") or self.dir.name

    def save(self):
        self.m["updated"] = time.time()
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.m, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.meta_path)

    def file(self, role):
        f = self.m.get("files", {}).get(role)
        return self.dir / f if f else None

    def set_file(self, role, fname):
        self.m.setdefault("files", {})[role] = fname

    def view(self):
        files = []
        for role, f in (self.m.get("files") or {}).items():
            p = self.dir / f
            if p.exists():
                files.append({"role": role, "label": ROLES.get(role, role), "path": str(p), "name": f})
        takes = [t for t in self.m.get("takes", []) if (self.dir / t["file"]).exists()]
        for t in takes:
            t["path"] = str(self.dir / t["file"])
        size = 0
        for f in self.dir.glob("*"):
            try:
                size += f.stat().st_size
            except OSError:
                pass
        return {"id": self.id, "name": self.name, "dir": str(self.dir), "info": self.m.get("info"),
                "kind": self.m.get("kind", "stem"), "files": files, "takes": takes,
                "clean_opts": self.m.get("clean_opts"), "updated": self.m.get("updated", 0),
                "origin": self.m.get("origin"), "origin_exists": bool(self.m.get("origin")) and Path(self.m["origin"]).exists(),
                "created": self.m.get("created"), "cleaned": self.m.get("cleaned"), "size_mb": round(size / 1e6, 1),
                "lyrics": self.m.get("lyrics", ""), "best": self.m.get("best")}


def list_songs():
    out = []
    for d in E.songs_dir().iterdir():
        if (d / "song.json").exists():
            s = Song(d)
            lead = s.file("lead")
            has_lead = bool(lead and lead.exists())
            out.append({"id": s.id, "name": s.name, "updated": s.m.get("updated", 0),
                        "has_lead": has_lead, "lead": str(lead) if has_lead else None,
                        "takes": len(s.m.get("takes", []))})
    out.sort(key=lambda x: -x["updated"])
    return out


def get(song_id):
    d = E.songs_dir() / song_id
    if not (d / "song.json").exists():
        raise E.EngineError("Песня не найдена — возможно, её папку удалили или переименовали.")
    return Song(d)


def create(src_path, kind="stem", name=None):
    src = Path(src_path)
    if not src.exists():
        raise E.EngineError("Файл не найден.")
    inf = A.info(src)
    # та же песня уже открывалась — возвращаем её
    for d in E.songs_dir().iterdir():
        mp = d / "song.json"
        if mp.exists():
            try:
                m = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:
                continue
            if m.get("origin") == str(src) and m.get("origin_size") == src.stat().st_size and \
                    abs((m.get("origin_mtime") or src.stat().st_mtime) - src.stat().st_mtime) < 2:
                return Song(d)
    base = safe_name(name or src.stem)
    folder = E.songs_dir() / base
    k = 2
    while folder.exists():
        folder = E.songs_dir() / f"{base} ({k})"
        k += 1
    folder.mkdir(parents=True)
    s = Song(folder)
    s.m = {"name": base, "origin": str(src), "origin_size": src.stat().st_size, "origin_mtime": src.stat().st_mtime,
           "created": time.time(),
           "kind": kind, "info": inf, "files": {}, "takes": []}
    fname = f"{base} - исходник{src.suffix.lower()}"
    shutil.copyfile(src, folder / fname)
    s.set_file("source", fname)
    s.save()
    return s


def set_kind(song_id, kind):
    s = get(song_id)
    s.m["kind"] = "mix" if kind == "mix" else "stem"
    s.save()
    return s.view()


def _drop_parts(s, t):
    """Папка промежуточных файлов «Перепевки с текстом» рядом с дублем — уходит вместе с дублем."""
    d = s.dir / (Path(t["file"]).stem + " - части")
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)


def delete_take(song_id, take_id):
    s = get(song_id)
    keep = []
    for t in s.m.get("takes", []):
        if t["id"] == take_id:
            (s.dir / t["file"]).unlink(missing_ok=True)
            _drop_parts(s, t)
        else:
            keep.append(t)
    s.m["takes"] = keep
    s.save()
    return s.view()


def clear(song_id, what):
    """Удаляет результаты: what = "clean" (файлы чистки) или "takes" (все дубли перепевки)."""
    s = get(song_id)
    if what == "clean":
        for role in ("lead", "backing", "reverb", "noise", "music"):
            f = s.file(role)
            if f:
                f.unlink(missing_ok=True)
            s.m.get("files", {}).pop(role, None)
        s.m.pop("cleaned", None)
    elif what == "takes":
        for t in s.m.get("takes", []):
            (s.dir / t["file"]).unlink(missing_ok=True)
            _drop_parts(s, t)
        s.m["takes"] = []
    else:
        raise E.EngineError("Неизвестно, что удалять.")
    s.save()
    return s.view()


# ---------------------------------------------------------------- чистка

def clean(ctx, song_id, opts):
    s = get(song_id)
    o = dict(CLEAN_DEFAULTS)
    o.update({k: v for k, v in (opts or {}).items() if k in CLEAN_DEFAULTS})
    o["mix"] = s.m.get("kind") == "mix"
    if not (o["mix"] or o["backing"] or o["dereverb"] != "off" or o["denoise"]):
        raise E.EngineError("Выберите хотя бы один шаг чистки.")
    p = E.paths()
    if not p.sep_py.exists():
        raise E.EngineError("Движок не установлен — откройте вкладку «Движок».")
    workers = E.ensure_workers()
    tmp = E.new_tmp("clean")
    ctx.span(0.0, 0.06, "Готовлю файл…")
    src = s.file("source")
    A.to_wav(src, tmp / "in.wav")
    job = dict(o, input=str(tmp / "in.wav"), work_dir=str(tmp / "w"), models_dir=str(p.sep_models))
    (tmp / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    ctx.span(0.06, 0.92, "Чистка…")
    res = ctx.run([p.sep_py, workers / "sep_worker.py", tmp / "job.json"], cwd=tmp, what="Чистка") or {}
    ctx.span(0.92, 1.0, "Сохраняю результат…")
    base = s.name
    names = {"lead": "вокал чистый", "backing": "бэки", "reverb": "реверберация (убрано)",
             "noise": "шум (убрано)", "music": "музыка без вокала"}
    for role in ("music", "backing", "reverb", "noise"):
        s.m.get("files", {}).pop(role, None)
    for role, label in names.items():
        f = res.get(role)
        if f and os.path.exists(f):
            fname = f"{base} - {label}.wav"
            A.conform(f, s.dir / fname, src, match_level=False)
            s.set_file(role, fname)
    s.m["clean_opts"] = o
    s.m["cleaned"] = time.time()
    s.save()
    shutil.rmtree(tmp, ignore_errors=True)
    v = s.view()
    v["seconds"] = res.get("seconds")
    return v


# ---------------------------------------------------------------- перепевка

def convert(ctx, song_id, voice, opts, what=("lead",)):
    import voices as V
    s = get(song_id)
    model, index = V.model_files(voice)
    o = dict(CONVERT_DEFAULTS)
    o.update({k: v for k, v in (opts or {}).items() if k in CONVERT_DEFAULTS})
    p = E.paths()
    if not p.applio_py.exists():
        raise E.EngineError("Движок не установлен — откройте вкладку «Движок».")
    workers = E.ensure_workers()
    tmp = E.new_tmp("conv")
    items = []
    ctx.span(0.0, 0.05, "Готовлю файлы…")
    sources = {}
    for role in what:
        f = s.file(role)
        if role == "lead" and (not f or not f.exists()):
            if s.m.get("kind") == "mix":
                raise E.EngineError("Это полный трек — сначала выделите вокал на шаге «Чистка».")
            f = s.file("source")              # без чистки — перепеваем исходник как есть
            role = "source"
        if not f or not f.exists():
            continue
        inp = tmp / f"in_{role}.wav"
        A.to_wav(f, inp, mono=True)
        sources[role] = f
        it = {k: o[k] for k in o if k != "match_level"}
        it.update(input=str(inp), output=str(tmp / f"out_{role}.wav"), model=str(model),
                  index=str(index or ""), label=E_ROLE.get(role, role))
        items.append((role, it))
    if not items:
        raise E.EngineError("Нечего перепевать: нет нужной дорожки.")
    job = {"items": [it for _, it in items]}
    (tmp / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    ctx.span(0.05, 0.93, "Перепевка…")
    ctx.run([p.applio_py, workers / "applio_worker.py", tmp / "job.json"], cwd=p.applio, what="Перепевка")
    ctx.span(0.93, 1.0, "Сохраняю результат…")
    vname = V.get(voice).name
    takes = s.m.setdefault("takes", [])
    num = 1 + max([t.get("num", 0) for t in takes] + [0])
    made = []
    for role, it in items:
        out = Path(it["output"])
        if not out.exists():
            continue
        part = "вокал" if role in ("lead", "source") else "бэки"
        fname = f"{s.name} - DZOD {safe_name(vname, 30)} {part} {num:02d}.wav"
        A.conform(out, s.dir / fname, sources[role], match_level=o["match_level"])
        t = {"id": f"t{int(time.time() * 1000)}{role}", "num": num, "role": role, "part": part,
             "file": fname, "voice": voice, "voice_name": vname, "opts": o, "made": time.time(),
             "from": sources[role].name}
        takes.append(t)
        made.append(t)
    s.save()
    shutil.rmtree(tmp, ignore_errors=True)
    v = s.view()
    v["made"] = [m["id"] for m in made]
    return v


E_ROLE = {"lead": "ведущий вокал", "backing": "бэк-вокал", "source": "исходный вокал"}


# ---------------------------------------------------------------- пресеты

def presets():
    user = E.SETTINGS.get("presets") or {}
    out = [{"name": k, "opts": dict(CONVERT_DEFAULTS, **v), "builtin": True, "text": PRESET_TEXT.get(k, "")}
           for k, v in BUILTIN_PRESETS.items()]
    out += [{"name": k, "opts": dict(CONVERT_DEFAULTS, **v), "builtin": False, "text": "Ваш пресет."}
            for k, v in user.items()]
    return out


def save_preset(name, opts):
    name = safe_name(name, 40)
    if name in BUILTIN_PRESETS:
        raise E.EngineError("Такое имя у встроенного пресета — выберите другое.")
    user = dict(E.SETTINGS.get("presets") or {})
    user[name] = {k: v for k, v in (opts or {}).items() if k in CONVERT_DEFAULTS}
    E.SETTINGS.set(presets=user)
    return presets()


def delete_preset(name):
    user = dict(E.SETTINGS.get("presets") or {})
    user.pop(name, None)
    E.SETTINGS.set(presets=user)
    return presets()


# ---------------------------------------------------------------- перепевка с текстом (исправление дубля)
def set_best(song_id, take_id):
    """★ лучший дубль (повторно — снять)."""
    s = get(song_id)
    s.m["best"] = None if s.m.get("best") == take_id else take_id
    s.save()
    return s.view()


def save_lyrics(song_id, lyrics):
    s = get(song_id)
    s.m["lyrics"] = lyrics or ""
    s.save()
    return {"ok": True}


def _take(s, take_id):
    t = next((t for t in s.m.get("takes", []) if t["id"] == take_id), None)
    if not t or not (s.dir / t["file"]).exists():
        raise E.EngineError("Дубль не найден.")
    src = s.dir / t.get("from", "")
    if not t.get("from") or not src.exists():
        raise E.EngineError("Нет исходника этого дубля (файл чистки удалён?) — исправлять не с чем.")
    return t, src


def text_analyze(ctx, song_id, take_id, lyrics, marks=None, language="auto"):
    """Где каждое слово текста в исходнике дубля, подозрительные слова, где найдены пометки."""
    import textfix as TF
    s = get(song_id)
    if lyrics is not None:
        s.m["lyrics"] = lyrics
        s.save()
    _t, src = _take(s, take_id)
    tmp = E.new_tmp("tfs")
    try:
        inp = tmp / "src.wav"
        A.to_wav(src, inp, mono=True)
        return TF.analyze(ctx, inp, s.m.get("lyrics", ""), marks, language)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def text_fix(ctx, song_id, take_id, lyrics, marks, language="auto"):
    """Исправить дубль по тексту: новый дубль, где изменены только помеченные места."""
    import textfix as TF
    import voices as V
    s = get(song_id)
    s.m["lyrics"] = lyrics or s.m.get("lyrics", "")
    s.save()
    t, src = _take(s, take_id)
    model, index = V.model_files(t["voice"])
    o = dict(CONVERT_DEFAULTS)
    o.update({k: v for k, v in (t.get("opts") or {}).items() if k in CONVERT_DEFAULTS})
    item = {k: o[k] for k in o if k != "match_level"}
    item.update(model=str(model), index=str(index or ""))
    takes = s.m.setdefault("takes", [])
    num = 1 + max([x.get("num", 0) for x in takes] + [0])
    fname = f"{Path(t['file']).stem} - текст {num:02d}.wav"
    tmp = E.new_tmp("tfx")
    try:
        inp = tmp / "src.wav"
        A.to_wav(src, inp, mono=True)
        r = TF.revoice_text(ctx, inp, s.dir / t["file"], item, s.m["lyrics"], marks, s.dir / fname, language)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    nt = dict(t, id=f"t{int(time.time() * 1000)}fix", num=num, file=fname, made=time.time(),
              fixed_from=t["id"], fixed_from_num=t.get("num"), text_marks=marks, changed=r.get("changed", []),
              text_checks=r.get("checks", []), text_skipped=[q for q in r.get("quality", []) if q.get("skip")])
    takes.append(nt)
    s.save()
    v = s.view()
    v["made"] = [nt["id"]]
    v["text_plan"] = r.get("plan", [])
    v["changed"] = r.get("changed", [])
    v["checks"] = r.get("checks", [])
    v["skipped"] = [q for q in r.get("quality", []) if q.get("skip")]
    v["parts"] = r.get("parts")
    v["fixed_from"] = t["id"]
    return v
