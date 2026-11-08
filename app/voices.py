# -*- coding: utf-8 -*-
"""Голоса DZOD: датасет (источники → фразы → отбор), обучение, контрольные эпохи, основная модель.
Голос лежит в <корень>\\Voices\\<код>, обучение идёт в папке Applio\\logs\\<код>."""
import json
import os
import re
import shutil
import time
from pathlib import Path

import numpy as np

import audio as A
import engine as E

TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                     "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))

TRAIN_DEFAULTS = {"sample_rate": 48000, "epochs": 300, "save_every": 10, "batch": 8, "fresh": False}
SR_CHOICES = (32000, 40000, 48000)


def slugify(name):
    s = "".join(TRANSLIT.get(c, c) for c in str(name).lower())
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    if not s or s[0].isdigit():
        s = "v_" + s
    return s[:40].strip("_") or "voice"


class Voice:
    def __init__(self, slug):
        self.slug = slug
        self.dir = E.voices_dir() / slug
        self.meta_path = self.dir / "voice.json"
        self.ph_path = self.dir / "phrases.json"
        try:
            self.m = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception:
            self.m = {}
        self._ph = None

    @property
    def name(self):
        return self.m.get("name") or self.slug

    def save(self):
        self.m["updated"] = time.time()
        _write_json(self.meta_path, self.m)

    # фразы
    def phrases(self):
        if self._ph is None:
            try:
                self._ph = json.loads(self.ph_path.read_text(encoding="utf-8"))
            except Exception:
                self._ph = []
        return self._ph

    def save_phrases(self):
        _write_json(self.ph_path, self.phrases())

    def model(self):
        mm = self.m.get("model") or {}
        pth = self.dir / "model" / mm["file"] if mm.get("file") else None
        idx = self.dir / "model" / mm["index"] if mm.get("index") else None
        if pth and pth.exists():
            return pth, (idx if idx and idx.exists() else None)
        return None, None

    def logs(self):
        return E.paths().logs() / self.slug

    def stats(self):
        ph = self.phrases()
        keep = [p for p in ph if p.get("status") == "keep"]
        new = [p for p in ph if p.get("status", "new") == "new"]
        return {"phrases": len(ph), "keep": len(keep), "new": len(new),
                "keep_min": round(sum(p["dur"] for p in keep) / 60, 1),
                "total_min": round(sum(p["dur"] for p in ph) / 60, 1)}

    def view(self, full=False):
        pth, idx = self.model()
        v = {"slug": self.slug, "name": self.name, "dir": str(self.dir), "has_model": bool(pth),
             "model": self.m.get("model"), "train": self.m.get("train"), "stats": self.stats(),
             "sources": self.m.get("sources", []), "updated": self.m.get("updated", 0),
             "dataset": self.m.get("dataset"), "dataset_dirty": dataset_changed(self),
             "compare": self.m.get("compare")}
        if full:
            v["checkpoints"] = checkpoints(self.slug)
        return v


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def get(slug):
    v = Voice(slug)
    if not v.meta_path.exists():
        raise E.EngineError("Голос не найден — возможно, его папку удалили.")
    return v


def list_voices():
    out = []
    for d in sorted(E.voices_dir().iterdir()):
        if (d / "voice.json").exists():
            v = Voice(d.name)
            pth, _ = v.model()
            imp = (v.m.get("imported") or {}).get("meta") or {}
            out.append({"slug": v.slug, "name": v.name, "has_model": bool(pth), "stats": v.stats(),
                        "epoch": (v.m.get("model") or {}).get("epoch"),
                        "dataset_min": (v.m.get("dataset") or {}).get("minutes") or imp.get("dataset_min"),
                        "has_index": bool(v.model()[1]), "from_folder": (v.m.get("imported") or {}).get("folder")})
    return out


def create(name):
    name = str(name or "").strip()
    if not name:
        raise E.EngineError("Введите имя голоса.")
    for x in list_voices():
        if x["name"].lower() == name.lower():
            raise E.EngineError("Голос с таким именем уже есть.")
    base = slugify(name)
    slug = base
    k = 2
    while (E.voices_dir() / slug).exists() or (E.paths().logs() / slug).exists():
        slug = f"{base}_{k}"
        k += 1
    v = Voice(slug)
    v.dir.mkdir(parents=True, exist_ok=True)
    v.m = {"name": name, "slug": slug, "created": time.time(), "sources": [], "model": None, "train": None}
    v.save()
    v.save_phrases()
    return v.view()


def rename(slug, name):
    v = get(slug)
    name = str(name or "").strip()
    if not name:
        raise E.EngineError("Введите имя голоса.")
    v.m["name"] = name
    v.save()
    return v.view()


def model_files(slug):
    v = get(slug)
    pth, idx = v.model()
    if not pth:
        raise E.EngineError(f"У голоса «{v.name}» ещё нет модели: обучите её или выберите эпоху.")
    return pth, idx


def import_model(name, pth, index=None):
    """Готовая модель RVC (.pth и, если есть, .index) — сразу становится голосом."""
    pth = Path(pth)
    if not pth.exists() or pth.suffix.lower() != ".pth":
        raise E.EngineError("Нужен файл модели .pth.")
    v = Voice(create(name)["slug"])
    md = v.dir / "model"
    md.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pth, md / f"{v.slug}.pth")
    mm = {"file": f"{v.slug}.pth", "from": "импорт", "epoch": None, "set": time.time()}
    if index and Path(index).exists():
        shutil.copyfile(index, md / f"{v.slug}.index")
        mm["index"] = f"{v.slug}.index"
    v.m["model"] = mm
    v.save()
    return v.view()


# ---------------------------------------------------------------- вокалист папкой (Vocalist → Revoicer)

VOICE_FILE = "voice.json"


def export_folder(slug, dest_dir):
    """Сохраняет вокалиста в папку: <dest>/<Имя>/ — модель .pth, индекс .index и voice.json.
    Эту папку загружает DZOD Revoicer («Загрузить вокалиста…»)."""
    import songs as S
    v = get(slug)
    pth, idx = v.model()
    if not pth:
        raise E.EngineError(f"У голоса «{v.name}» ещё нет модели: выберите эпоху на шаге «Эпохи».")
    dest = Path(dest_dir) / S.safe_name(v.name, 60)
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pth, dest / f"{v.slug}.pth")
    if idx:
        shutil.copyfile(idx, dest / f"{v.slug}.index")
    mm = v.m.get("model") or {}
    tr = v.m.get("train") or {}
    meta = {"kind": "dzod-voice", "version": 1, "name": v.name, "slug": v.slug, "epoch": mm.get("epoch"),
            "sample_rate": tr.get("sample_rate"), "dataset_min": (v.m.get("dataset") or {}).get("minutes"),
            "exported": time.strftime("%Y-%m-%d %H:%M"), "from": f"{E.APP} {E.VERSION}",
            "model": f"{v.slug}.pth", "index": f"{v.slug}.index" if idx else None}
    _write_json(dest / VOICE_FILE, meta)
    E.log(f"ВОКАЛИСТ СОХРАНЁН: {v.name} → {dest}")
    return {"path": str(dest), "name": v.name}


def import_folder(folder):
    """Загружает вокалиста из папки (сохранённой Vocalist или любой папки с моделью RVC .pth/.index).
    Если вокалист с таким именем уже есть — его модель заменяется."""
    folder = Path(folder)
    if not folder.is_dir():
        raise E.EngineError("Папка не найдена.")
    meta = {}
    try:
        meta = json.loads((folder / VOICE_FILE).read_text(encoding="utf-8"))
    except Exception:
        pass
    pths = [folder / meta["model"]] if meta.get("model") and (folder / meta["model"]).exists() else []
    if not pths:
        pths = sorted(folder.rglob("*.pth"), key=lambda f: f.stat().st_size, reverse=True)
        pths = [f for f in pths if not f.name.startswith(("G_", "D_"))] or pths
    if not pths:
        raise E.EngineError("В папке нет модели голоса (файла .pth). Сохраните вокалиста в DZOD Vocalist: "
                            "«Голос» → «Сохранить вокалиста в папку…».")
    idx = folder / meta["index"] if meta.get("index") and (folder / meta["index"]).exists() else None
    if not idx:
        idxs = sorted(folder.rglob("*.index"), key=lambda f: ("added" not in f.name, -f.stat().st_size))
        idx = idxs[0] if idxs else None
    name = str(meta.get("name") or folder.name).strip() or "Вокалист"
    existing = next((x for x in list_voices() if x["name"].lower() == name.lower()), None)
    if existing:
        v = get(existing["slug"])
    else:
        v = Voice(create(name)["slug"])
    md = v.dir / "model"
    md.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(pths[0], md / f"{v.slug}.pth")
    mm = {"file": f"{v.slug}.pth", "from": str(folder), "epoch": meta.get("epoch"), "set": time.time()}
    (md / f"{v.slug}.index").unlink(missing_ok=True)
    if idx:
        shutil.copyfile(idx, md / f"{v.slug}.index")
        mm["index"] = f"{v.slug}.index"
    v.m["model"] = mm
    v.m["imported"] = {"folder": str(folder), "at": time.time(), "meta": meta}
    v.save()
    E.log(f"ВОКАЛИСТ ЗАГРУЖЕН: {name} ← {folder}")
    out = v.view()
    out["replaced"] = bool(existing)
    return out


# ---------------------------------------------------------------- датасет

def add_sources(ctx, slug, files, clean_opts=None):
    """Добавляет записи голоса: (по желанию) чистит, режет на фразы, разбирает каждую фразу."""
    v = get(slug)
    files = [Path(f) for f in files if f and Path(f).exists()]
    if not files:
        raise E.EngineError("Не выбрано ни одного файла.")
    src_dir = v.dir / "sources"
    ph_dir = v.dir / "phrases"
    src_dir.mkdir(parents=True, exist_ok=True)
    ph_dir.mkdir(parents=True, exist_ok=True)
    cleaned = {}
    if clean_opts:
        p = E.paths()
        if not p.sep_py.exists():
            raise E.EngineError("Движок не установлен — откройте вкладку «Движок».")
        workers = E.ensure_workers()
        tmp = E.new_tmp("dsclean")
        ctx.span(0.0, 0.08, "Готовлю файлы…")
        inputs = []
        for i, f in enumerate(files):
            ctx.check()
            dst = tmp / f"in{i}.wav"
            A.to_wav(f, dst)
            inputs.append(str(dst))
            ctx.progress((i + 1) / len(files))
        o = {"backing": bool(clean_opts.get("backing", True)), "dereverb": clean_opts.get("dereverb", "uvr"),
             "dereverb_aggr": int(clean_opts.get("dereverb_aggr", 50)),
             "denoise": bool(clean_opts.get("denoise", False)), "quality": clean_opts.get("quality", "fast"),
             "mix": bool(clean_opts.get("mix", False)), "backing_same": False}
        job = dict(o, inputs=inputs, work_dir=str(tmp / "w"), models_dir=str(p.sep_models))
        (tmp / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        ctx.span(0.08, 0.80, "Чистка записей…")
        res = ctx.run([p.sep_py, workers / "sep_worker.py", tmp / "job.json"], cwd=tmp, what="Чистка") or {}
        items = res.get("items") or [res]
        for f, r in zip(files, items):
            if r.get("lead"):
                cleaned[str(f)] = r["lead"]
        lo = 0.80
    else:
        lo = 0.0
    ctx.span(lo, 1.0, "Режу на фразы…")
    ph = v.phrases()
    added = 0
    for i, f in enumerate(files):
        ctx.check()
        ctx.progress(i / len(files), f"Режу на фразы: {f.name}")
        sid = f"s{int(time.time() * 1000) % 10**10}{i}"
        use = Path(cleaned.get(str(f), f))
        x, sr = A.read(use)
        A.write(src_dir / f"{sid}.wav", x, sr, "PCM_24")
        mono = x.mean(axis=1)
        segs = A.split_phrases(mono, sr)
        n = 0
        for k, (a, b) in enumerate(segs):
            seg = x[a:b]
            fade = min(int(0.01 * sr), len(seg) // 4)
            if fade > 0:
                ramp = np.linspace(0, 1, fade, dtype=np.float32)[:, None]
                seg = seg.copy()
                seg[:fade] *= ramp
                seg[-fade:] *= ramp[::-1]
            pid = f"{sid}_{k:03d}"
            A.write(ph_dir / f"{pid}.wav", seg, sr, "PCM_24")
            info = A.analyze(seg, sr)
            ph.append(dict(info, id=pid, src=sid, start=round(a / sr, 2), dur=round((b - a) / sr, 2),
                           status="new", note=A.note_name(info["f0"])))
            n += 1
        v.m.setdefault("sources", []).append({"id": sid, "name": f.name, "path": str(f), "added": time.time(),
                                              "cleaned": str(f) in cleaned, "phrases": n,
                                              "seconds": round(len(x) / sr, 1)})
        added += n
    v.save_phrases()
    v.save()
    if clean_opts:
        shutil.rmtree(tmp, ignore_errors=True)
    out = v.view()
    out["added"] = added
    return out


PHRASE_EXT = {".wav", ".flac", ".mp3", ".ogg", ".aif", ".aiff"}


def _read_table(folder):
    """table.csv рядом с фразами (отсев КЛАВДИИ ГЛАВНОЙ): file, …, best (1/0), reject_reason.
    Разделитель — запятая или точка с запятой, кодировка UTF-8."""
    import csv
    t = Path(folder) / "table.csv"
    if not t.exists():
        return {}
    text = t.read_text(encoding="utf-8-sig", errors="replace")
    delim = ";" if text.splitlines()[0].count(";") > text.splitlines()[0].count(",") else ","
    rows = {}
    for r in csv.DictReader(text.splitlines(), delimiter=delim):
        r = {str(k).strip().lower(): (v or "").strip() for k, v in r.items() if k}
        name = r.get("file") or r.get("файл")
        if name:
            rows[Path(name).name.lower()] = r
    return rows


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "yes", "true", "да", "+", "x", "лучшее", "★", "*")


def _reason(row):
    r = row.get("reject_reason") or row.get("reasons") or row.get("reason") or row.get("причина") or ""
    if not r and str(row.get("ok", "")).strip().lower() in ("нет", "no", "0", "false"):
        r = "отбраковано"
    return r


def _unpack_zips(folder, dst):
    """Ветка vocal-train: best_*.zip и spare_*.zip распаковываются (без вложенных папок)."""
    import zipfile
    zips = sorted(Path(folder).glob("*.zip"))
    zips = [z for z in zips if z.stem.lower().startswith(("best", "spare"))] or zips
    n = 0
    for z in zips:
        with zipfile.ZipFile(z) as zf:
            for name in zf.namelist():
                base = Path(name).name
                if Path(base).suffix.lower() in PHRASE_EXT and not base.startswith("."):
                    with zf.open(name) as src, open(dst / base, "wb") as out:
                        shutil.copyfileobj(src, out)
                    n += 1
    return n


def add_phrase_folder(ctx, slug, folder):
    """Папка уже нарезанных фраз: каждый файл — одна фраза, без повторной нарезки.
    Если рядом лежит table.csv с колонкой best — лучшие сразу отмечаются «Оставить»,
    фразы с reject_reason — «Выбросить» (их можно вернуть)."""
    v = get(slug)
    folder = Path(folder)
    if not folder.is_dir():
        raise E.EngineError("Папка не найдена.")
    table = _read_table(folder) or _read_table(folder.parent)
    files = sorted(f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in PHRASE_EXT)
    unz = None
    if not files and list(folder.glob("*.zip")):
        ctx.progress(0.0, "Распаковываю архивы фраз…")
        unz = E.new_tmp("phz")
        _unpack_zips(folder, unz)
        files = sorted(f for f in unz.iterdir() if f.is_file())
    if not files:
        raise E.EngineError("В папке нет звуковых файлов (WAV, FLAC, MP3) и архивов с ними.")
    ph_dir = v.dir / "phrases"
    ph_dir.mkdir(parents=True, exist_ok=True)
    sid = f"p{int(time.time() * 1000) % 10**10}"
    ph = v.phrases()
    n = kept = dropped = 0
    total = 0.0
    for k, f in enumerate(files):
        ctx.check()
        ctx.progress(k / len(files), f"Фразы: {f.name}")
        try:
            x, sr = A.read(f)
        except A.AudioError as ex:
            E.log(f"фраза пропущена: {ex}")
            continue
        dur = len(x) / sr
        if dur < 0.5:
            continue
        pid = f"{sid}_{k:04d}"
        A.write(ph_dir / f"{pid}.wav", x, sr, "PCM_24")
        info = A.analyze(x, sr)
        row = table.get(f.name.lower(), {})
        status = "new"
        reason = _reason(row)
        if reason:
            status = "drop"
        elif _truthy(row.get("best") or row.get("лучшее")):
            status = "keep"
        kept += status == "keep"
        dropped += status == "drop"
        ph.append(dict(info, id=pid, src=sid, start=0.0, dur=round(dur, 2), status=status,
                       note=A.note_name(info["f0"]), file=f.name, reason=reason,
                       origin=row.get("source") or row.get("src") or ""))
        total += dur
        n += 1
    v.m.setdefault("sources", []).append({"id": sid, "name": folder.name + " (готовые фразы)", "path": str(folder),
                                          "added": time.time(), "cleaned": False, "phrases": n,
                                          "seconds": round(total, 1), "table": bool(table),
                                          "kept": kept, "dropped": dropped})
    v.save_phrases()
    v.save()
    if unz:
        shutil.rmtree(unz, ignore_errors=True)
    out = v.view()
    out["added"] = n
    out["kept"] = kept
    out["dropped"] = dropped
    return out


def phrase_list(slug):
    v = get(slug)
    names = {s["id"]: s["name"] for s in v.m.get("sources", [])}
    out = []
    for p in v.phrases():
        q = dict(p)
        q["path"] = str(v.dir / "phrases" / f"{p['id']}.wav")
        q["src_name"] = names.get(p["src"], "")
        out.append(q)
    return out


def mark(slug, ids, status):
    if status not in ("keep", "drop", "new"):
        raise E.EngineError("Неизвестная отметка.")
    v = get(slug)
    ids = set(ids or [])
    for p in v.phrases():
        if p["id"] in ids:
            p["status"] = status
    v.save_phrases()
    return v.stats()


def remove_source(slug, sid):
    v = get(slug)
    keep = []
    for p in v.phrases():
        if p["src"] == sid:
            (v.dir / "phrases" / f"{p['id']}.wav").unlink(missing_ok=True)
        else:
            keep.append(p)
    v._ph = keep
    v.save_phrases()
    v.m["sources"] = [s for s in v.m.get("sources", []) if s["id"] != sid]
    (v.dir / "sources" / f"{sid}.wav").unlink(missing_ok=True)
    v.save()
    return v.view()


def build_dataset(slug):
    """Отобранные фразы → папка dataset (каждая фраза выровнена по пику −3 дБ)."""
    v = get(slug)
    keep = [p for p in v.phrases() if p.get("status") == "keep"]
    if not keep:
        raise E.EngineError("В датасете нет ни одной отобранной фразы: отметьте фразы кнопкой «Оставить».")
    ds = v.dir / "dataset"
    shutil.rmtree(ds, ignore_errors=True)
    ds.mkdir(parents=True)
    total = 0.0
    for p in keep:
        x, sr = A.read(v.dir / "phrases" / f"{p['id']}.wav", mono=True)
        pk = float(np.max(np.abs(x))) or 1.0
        x = x * (10 ** (-3 / 20) / pk)
        A.write(ds / f"{p['id']}.wav", x, sr, "PCM_16")
        total += p["dur"]
    sig = sorted(p["id"] for p in keep)
    v.m["dataset"] = {"count": len(keep), "minutes": round(total / 60, 1), "built": time.time(),
                      "sig": hash_ids(sig)}
    v.save()
    return v.m["dataset"]


def hash_ids(ids):
    import hashlib
    return hashlib.sha1("|".join(ids).encode()).hexdigest()[:12]


def dataset_changed(v):
    keep = sorted(p["id"] for p in v.phrases() if p.get("status") == "keep")
    return (v.m.get("dataset") or {}).get("sig") != hash_ids(keep)


# ---------------------------------------------------------------- обучение

EPOCH_RE = re.compile(r"epoch=(\d+)\s*\|\s*step=(\d+)")
LOWEST_RE = re.compile(r"lowest_value=([\d.]+)\s*\(epoch (\d+)")


def train(ctx, slug, opts):
    v = get(slug)
    o = dict(TRAIN_DEFAULTS)
    o.update({k: val for k, val in (opts or {}).items() if k in TRAIN_DEFAULTS})
    o["sample_rate"] = int(o["sample_rate"]) if int(o["sample_rate"]) in SR_CHOICES else 48000
    o["epochs"] = max(1, min(2000, int(o["epochs"])))
    o["save_every"] = max(1, min(100, int(o["save_every"])))
    o["batch"] = max(1, min(32, int(o["batch"])))
    p = E.paths()
    if not p.applio_py.exists():
        raise E.EngineError("Движок не установлен — откройте вкладку «Движок».")
    logs = v.logs()
    prev = v.m.get("train") or {}
    fresh = bool(o["fresh"]) or not (logs / "config.json").exists()
    if not fresh and prev.get("sample_rate") and int(prev["sample_rate"]) != o["sample_rate"]:
        raise E.EngineError("Частоту обучения можно сменить только при обучении заново.")
    gpu = E.gpu_index()
    cores = str(max(1, min(16, (os.cpu_count() or 4) - 1)))
    sr = str(o["sample_rate"])

    if fresh or dataset_changed(v) or not (logs / "sliced_audios").exists():
        if dataset_changed(v) or not (v.dir / "dataset").exists():
            ctx.span(0.0, 0.02, "Собираю датасет…")
            build_dataset(slug)
        if not fresh and (logs / "config.json").exists():
            fresh = True                       # датасет другой — продолжать старое обучение нельзя
        if fresh and logs.exists():
            ctx.span(0.02, 0.03, "Убираю прежнее обучение…")
            shutil.rmtree(logs, ignore_errors=True)
        ctx.span(0.03, 0.10, "Подготовка датасета: нарезка и выравнивание…")
        ctx.run([p.applio_py, "core.py", "preprocess", "--model-name", slug, "--dataset-path", v.dir / "dataset",
                 "--sample-rate", sr, "--cpu-cores", cores, "--cut-preprocess", "Automatic",
                 "--chunk-len", "3.0", "--overlap-len", "0.3", "--normalization-mode", "none"],
                cwd=p.applio, what="Подготовка датасета")
        ctx.span(0.10, 0.20, "Разбор датасета: высота тона и тембр…")
        ctx.run([p.applio_py, "core.py", "extract", "--model-name", slug, "--f0-method", "rmvpe",
                 "--cpu-cores", cores, "--gpu", gpu, "--sample-rate", sr,
                 "--embedder-model", "contentvec", "--include-mutes", "2"],
                cwd=p.applio, what="Разбор датасета")
        fresh = True
    v.m["train"] = dict(o, fresh=False, started=time.time(), running=True, last_epoch=prev.get("last_epoch", 0)
                        if not fresh else 0, lowest=None)
    v.save()

    t0 = time.time()
    first = {"epoch": None, "t": None}
    target = o["epochs"]

    def on_line(line):
        m = EPOCH_RE.search(line)
        if not m:
            return
        ep = int(m.group(1))
        now = time.time()
        if first["epoch"] is None:
            first.update(epoch=ep, t=now)
        eta = ""
        if ep > first["epoch"]:
            per = (now - first["t"]) / (ep - first["epoch"])
            left = max(0, target - ep) * per
            eta = f", осталось около {fmt_dur(left)}"
            ctx.extra["sec_per_epoch"] = round(per, 1)
        lw = LOWEST_RE.search(line)
        if lw:
            ctx.extra["lowest"] = {"value": float(lw.group(1)), "epoch": int(lw.group(2))}
        ctx.extra["epoch"] = ep
        ctx.extra["target"] = target
        ctx.progress((ep - 0) / max(1, target), f"Обучение: эпоха {ep} из {target}{eta}")
        tr = v.m.get("train") or {}
        tr["last_epoch"] = ep
        tr["lowest"] = ctx.extra.get("lowest")
        v.m["train"] = tr
        if ep % 5 == 0:
            v.save()

    ctx.span(0.20, 0.99, "Обучение: запуск…")
    args = [p.applio_py, "core.py", "train", "--model-name", slug, "--vocoder", "HiFi-GAN",
            "--save-every-epoch", o["save_every"], "--save-only-latest", "--total-epoch", target,
            "--sample-rate", sr, "--batch-size", o["batch"], "--gpu", gpu,
            "--pretrained", "--index-algorithm", "Auto"]
    try:
        ctx.run(args, cwd=p.applio, on_line=on_line, what="Обучение")
    except E.Cancelled:
        # остановили — модель годна до последней сохранённой эпохи; соберём индекс, чтобы её можно было слушать
        v.m["train"]["running"] = False
        v.m["train"]["stopped"] = time.time()
        v.save()
        try:
            ctx.cancelled = False
            ctx.span(0.99, 1.0, "Собираю индекс тембра…")
            ctx.run([p.applio_py, "core.py", "index", "--model-name", slug, "--index-algorithm", "Auto"],
                    cwd=p.applio, what="Индекс")
        except Exception as ex:  # noqa: BLE001
            E.log(f"индекс после остановки: {ex}")
        ctx.cancelled = True
        raise
    finally:
        tr = v.m.get("train") or {}
        tr["running"] = False
        tr["finished"] = time.time()
        tr["seconds"] = round(time.time() - t0)
        v.m["train"] = tr
        v.save()
    cks = checkpoints(slug)
    if cks and not v.model()[0]:
        choose(slug, cks[-1]["file"])
    return get(slug).view(full=True)


def fmt_dur(sec):
    sec = int(sec)
    h, m = sec // 3600, (sec % 3600) // 60
    if h:
        return f"{h} ч {m} мин"
    return f"{max(1, m)} мин"


CK_RE = re.compile(r"_(\d+)e_(\d+)s\.pth$")


def checkpoints(slug):
    logs = E.paths().logs() / slug
    out = []
    if logs.is_dir():
        for f in logs.glob(f"{slug}_*e_*s.pth"):
            m = CK_RE.search(f.name)
            if m:
                out.append({"file": f.name, "epoch": int(m.group(1)), "step": int(m.group(2)),
                            "mb": round(f.stat().st_size / 1e6, 1), "path": str(f)})
    out.sort(key=lambda x: x["epoch"])
    return out


def index_file(slug):
    logs = E.paths().logs() / slug
    for name in (f"{slug}.index",):
        if (logs / name).exists():
            return logs / name
    found = sorted(logs.glob("*.index")) if logs.is_dir() else []
    return found[0] if found else None


def choose(slug, ck_file):
    """Эпоха становится основной моделью голоса."""
    v = get(slug)
    src = v.logs() / ck_file
    if not src.exists():
        raise E.EngineError("Файл эпохи не найден.")
    md = v.dir / "model"
    md.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, md / f"{slug}.pth")
    mm = {"file": f"{slug}.pth", "from": ck_file, "set": time.time()}
    m = CK_RE.search(ck_file)
    mm["epoch"] = int(m.group(1)) if m else None
    idx = index_file(slug)
    if idx:
        shutil.copyfile(idx, md / f"{slug}.index")
        mm["index"] = f"{slug}.index"
    v.m["model"] = mm
    v.save()
    return v.view(full=True)


def make_excerpt(src, dst, seconds=25.0):
    """Отрывок для сравнения эпох: первые ~25 секунд звучания."""
    x, sr = A.read(src, mono=True)
    r = A.frame_rms(x[:, 0], sr, 0.05, 0.05)
    thr = np.max(r) * 0.1 if r.size else 0
    first = int(np.argmax(r > thr)) if r.size else 0
    a = max(0, first * int(sr * 0.05) - int(0.2 * sr))
    seg = x[a:a + int(seconds * sr)]
    A.write(dst, seg, sr, "FLOAT")
    return dst


def compare(ctx, slug, test_path, files, opts=None):
    """Один и тот же отрывок — голосом нескольких эпох. Результаты: Voices\\<код>\\compare."""
    import songs as S
    v = get(slug)
    p = E.paths()
    if not files:
        raise E.EngineError("Отметьте эпохи для сравнения.")
    workers = E.ensure_workers()
    tmp = E.new_tmp("cmp")
    ctx.span(0.0, 0.05, "Готовлю отрывок…")
    make_excerpt(test_path, tmp / "test.wav")
    cdir = v.dir / "compare"
    shutil.rmtree(cdir, ignore_errors=True)
    cdir.mkdir(parents=True)
    A.conform(tmp / "test.wav", cdir / "_исходник.wav", tmp / "test.wav", match_level=False)
    o = dict(S.CONVERT_DEFAULTS)
    o.update({k: val for k, val in (opts or {}).items() if k in S.CONVERT_DEFAULTS})
    idx = index_file(slug)
    items = []
    for f in files:
        ck = v.logs() / f
        if not ck.exists():
            continue
        it = {k: o[k] for k in o if k != "match_level"}
        m = CK_RE.search(f)
        ep = int(m.group(1)) if m else 0
        it.update(input=str(tmp / "test.wav"), output=str(tmp / f"e{ep:05d}.wav"), model=str(ck),
                  index=str(idx or ""), label=f"эпоха {ep}")
        items.append((ep, f, it))
    if not items:
        raise E.EngineError("Файлы эпох не найдены.")
    (tmp / "job.json").write_text(json.dumps({"items": [i for _, _, i in items]}, ensure_ascii=False),
                                  encoding="utf-8")
    ctx.span(0.05, 0.97, "Перепеваю отрывок голосом каждой эпохи…")
    ctx.run([p.applio_py, workers / "applio_worker.py", tmp / "job.json"], cwd=p.applio, what="Сравнение эпох")
    out = []
    for ep, f, it in items:
        o_path = Path(it["output"])
        if o_path.exists():
            dst = cdir / f"эпоха {ep:04d}.wav"
            A.conform(o_path, dst, tmp / "test.wav", match_level=True)
            out.append({"epoch": ep, "file": f, "path": str(dst)})
    shutil.rmtree(tmp, ignore_errors=True)
    res = {"source": str(cdir / "_исходник.wav"), "items": out, "test": str(test_path)}
    v.m["compare"] = res
    v.save()
    return res


def delete_checkpoints(slug, keep_files):
    """Удаляет файлы эпох, кроме отмеченных (и основной модели — она лежит отдельно)."""
    v = get(slug)
    keep = set(keep_files or [])
    n = 0
    for c in checkpoints(slug):
        if c["file"] not in keep:
            (v.logs() / c["file"]).unlink(missing_ok=True)
            n += 1
    return {"deleted": n, "voice": v.view(full=True)}
