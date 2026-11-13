# -*- coding: utf-8 -*-
"""Перенос папки данных (13.11, автор: всё DZOD — в «D:\\DZOD Studio Materials»).

Что лежит в папке данных: Engine (движок ~12 ГБ, Executor ~6 ГБ), Voices (голоса), Songs (песни).
Перенос:
- тот же диск — папка переименовывается целиком (мгновенно, место не нужно);
- другой диск — копирование по файлам с ходом и отменой, потом старая папка удаляется.
После переноса правятся абсолютные пути, которые помнят среды Python движка (pyvenv.cfg, *.pth)
и записи программы (json песен и голосов, свои и у второй программы). Остальное движок считает от папки.
"""
import json
import os
import shutil
import time
from pathlib import Path

import engine as E

# что не копируем между дисками: кэш пакетов (восстановится сам; в нём жёсткие ссылки — при копировании удвоится)
SKIP_COPY = {"uv-cache", "tmp"}
TEXT_EXT = {".json", ".cfg", ".pth", ".txt"}


def _same_drive(a, b):
    a, b = Path(a), Path(b)
    while not b.exists() and b.parent != b:
        b = b.parent
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _norm(p):
    return str(p).rstrip("\\/")


def _replace_in(path, pairs):
    """Заменить старый путь новым в текстовом файле (и в json-записи с двойными «\\»). Регистр букв пути не важен."""
    try:
        raw = path.read_bytes()
        if len(raw) > 20_000_000:
            return False
        t = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    out = t
    for old, new in pairs:
        for o, n in ((old, new), (old.replace("\\", "\\\\"), new.replace("\\", "\\\\")),
                     (old.replace("\\", "/"), new.replace("\\", "/"))):
            if not o:
                continue
            i, lo, res = 0, out.lower(), []
            ol = o.lower()
            while True:
                j = lo.find(ol, i)
                if j < 0:
                    res.append(out[i:])
                    break
                # только целым путём: дальше — разделитель, кавычка или конец
                k = j + len(o)
                if k < len(out) and out[k] not in "\\/\"'\r\n\t ;,]}":
                    res.append(out[i:k])
                    i = k
                    continue
                res.append(out[i:j] + n)
                i = k
            out = "".join(res)
    if out != t:
        tmp = path.with_suffix(path.suffix + ".reloc")
        tmp.write_text(out, encoding="utf-8")
        os.replace(tmp, path)
        return True
    return False


def fix_paths(new_root, old_root, extra_roots=()):
    """Поправить запомненные абсолютные пути после переноса. Возвращает число исправленных файлов."""
    new_root, pairs = Path(new_root), [(_norm(old_root), _norm(new_root))]
    files = []
    eng = new_root / "Engine"
    if eng.is_dir():
        # среды Python движка: pyvenv.cfg и *.pth (только верхний уровень site-packages)
        for cfg in eng.rglob("pyvenv.cfg"):
            files.append(cfg)
            sp = cfg.parent / ("Lib/site-packages" if E.IS_WIN else "lib")
            if sp.is_dir():
                files += list(sp.glob("*.pth")) if E.IS_WIN else list(sp.glob("python*/site-packages/*.pth"))
        files += [f for f in eng.glob("*.json")]
    for base in [new_root, *map(Path, extra_roots)]:
        for sub in ("Voices", "Songs"):
            d = base / sub
            if d.is_dir():
                for dp, dn, fs in os.walk(d):
                    dn[:] = [x for x in dn if x not in ("tmp", "cache")]
                    files += [Path(dp) / f for f in fs if Path(f).suffix.lower() in (".json",)]
    n = 0
    for f in files:
        try:
            n += _replace_in(f, pairs)
        except OSError as ex:
            E.log(f"ПЕРЕНОС: не поправлен {f}: {ex}")
    # настройки программы: тоже могут помнить пути внутри старой папки
    try:
        n += _replace_in(E.SETTINGS.path, pairs)
        E.SETTINGS.__init__()
    except OSError:
        pass
    return n


def _sizes(src):
    total, files = 0, []
    for dp, dn, fs in os.walk(src):
        rel = Path(dp).relative_to(src)
        if rel.parts[:2] and len(rel.parts) >= 2 and rel.parts[0] == "Engine" and rel.parts[1] in SKIP_COPY:
            dn[:] = []
            continue
        if rel.parts and rel.parts[0] == "Engine" and len(rel.parts) == 1:
            dn[:] = [x for x in dn if x not in SKIP_COPY]
        for f in fs:
            p = Path(dp) / f
            try:
                sz = p.stat().st_size
            except OSError:
                sz = 0
            files.append((p, sz))
            total += sz
    return total, files


def check_target(new, old):
    new, old = _norm(new), _norm(old)
    problem = E.check_root(new)
    if problem:
        return problem
    if new.lower() == old.lower():
        return "Это та же папка."
    if new.lower().startswith(old.lower() + "\\") or new.lower().startswith(old.lower() + "/"):
        return "Новая папка не может быть внутри старой."
    if Path(new).exists() and any(Path(new).iterdir()):
        return "Новая папка уже не пустая — выберите пустую или несуществующую."
    return ""


def move_root(ctx, new, old=None):
    """Перенести папку данных old → new и переключить программу на неё."""
    old = _norm(old or E.root())
    new = _norm(new)
    problem = check_target(new, old)
    if problem:
        raise E.EngineError(problem)
    if not Path(old).is_dir():
        raise E.EngineError(f"Старой папки нет: {old}")
    t0 = time.time()
    other = None
    try:
        other = E.other_root()
    except Exception:  # noqa: BLE001
        pass
    ctx.progress(0.02, "Проверка…")
    Path(new).parent.mkdir(parents=True, exist_ok=True)
    if Path(new).exists():
        Path(new).rmdir()            # пустая — проверено выше
    how = "переименование"
    if _same_drive(old, Path(new).parent):
        ctx.progress(0.1, "Перенос (тот же диск — мгновенно)…")
        try:
            os.rename(old, new)
        except OSError as ex:
            raise E.EngineError("Папку не удалось перенести: её держит другая программа — закройте DZOD Revoicer / "
                                "DZOD Vocalist, DZOD Studio и окна проводника с этой папкой и повторите. "
                                f"Ничего не изменено. ({ex})")
    else:
        how = "копирование"
        total, files = _sizes(old)
        free = E.disk_free_gb(new)
        if free is not None and free * 1e9 < total * 1.05:
            raise E.EngineError(f"На новом диске свободно {free:.0f} ГБ, а нужно {total / 1e9:.0f} ГБ.")
        done = 0
        try:
            for i, (f, sz) in enumerate(files):
                ctx.check()
                rel = f.relative_to(old)
                dst = Path(new) / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
                done += sz
                if i % 50 == 0:
                    ctx.progress(0.05 + 0.85 * done / max(1, total),
                                 f"Копирование: {done / 1e9:.1f} из {total / 1e9:.1f} ГБ")
        except BaseException:
            shutil.rmtree(new, ignore_errors=True)       # отмена или ошибка — новая папка убирается, старая цела
            raise
    ctx.progress(0.92, "Пути движка и записей…")
    n = fix_paths(new, old, [other] if other and Path(other).is_dir() and _norm(other).lower() != new.lower() else [])
    E.SETTINGS.set(root=new, migrate_declined=None)
    if how == "копирование":
        ctx.progress(0.96, "Удаление старой папки…")
        shutil.rmtree(old, ignore_errors=True)
    E.log(f"ПЕРЕНОС ПАПКИ ДАННЫХ: {old} → {new} ({how}), исправлено файлов: {n}, {time.time() - t0:.1f} с")
    ctx.progress(1.0, "Перенесено")
    return {"old": old, "new": new, "how": how, "fixed": n, "engine": E.engine_status()}
