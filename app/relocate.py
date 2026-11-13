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


def _is_link(d):
    try:
        return d.is_symlink() or bool(getattr(os.path, "isjunction", lambda _x: False)(str(d)))
    except OSError:
        return True


def _base_python(python_dir):
    """Настоящий Python движка (папка cpython-3.12.N-…; ссылки «cpython-3.12-…» uv после переноса битые)."""
    best = None
    if python_dir.is_dir():
        for d in sorted(python_dir.iterdir()):
            if not d.name.startswith("cpython-") or _is_link(d):
                continue
            exe = d / ("python.exe" if E.IS_WIN else "bin/python3")
            if exe.exists():
                best = exe
    return best


def _is_env(env):
    return (env / "pyvenv.cfg").exists() and os.path.lexists(env / ("Scripts/python.exe" if E.IS_WIN else "bin/python"))


def _home(env):
    try:
        for ln in (env / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
            k, _, v = ln.partition("=")
            if k.strip().lower() == "home":
                return Path(v.strip())
    except OSError:
        pass
    return None


def stale_envs(engine):
    """Среды Python, у которых «home» указывает в несуществующее место (папку переносили)."""
    engine = Path(engine)
    q = E.Paths(engine)
    out = []
    for env in (q.applio_env, q.sep_env, q.exec_env):
        if _is_env(env):
            h = _home(env)
            if h is None or not h.exists():
                out.append(env)
    return out


def repair_envs(engine, envs=None, log=E.log):
    """Пересоздать запускающие файлы сред (python.exe uv хранит путь к Python внутри себя), библиотеки не трогаются:
    uv venv --allow-existing. Плюс убрать битые ссылки на Python в папке движка."""
    engine = Path(engine)
    q = E.Paths(engine)
    envs = envs if envs is not None else [e for e in (q.applio_env, q.sep_env, q.exec_env) if _is_env(e)]
    if not envs:
        return []
    if q.python_dir.is_dir():
        for d in q.python_dir.iterdir():
            if _is_link(d) and not d.exists():
                try:
                    os.rmdir(d) if E.IS_WIN else d.unlink()
                except OSError:
                    pass
    base = _base_python(q.python_dir)
    uv = engine / "uv" / ("uv.exe" if E.IS_WIN else "uv")
    if base is None or not uv.exists():
        raise E.EngineError(f"Не найден Python или uv движка в {engine} — переустановите движок (вкладка «Движок»).")
    env_vars = dict(os.environ, UV_CACHE_DIR=str(q.uv_cache), UV_PYTHON_INSTALL_DIR=str(q.python_dir),
                    UV_NO_PROGRESS="1", PYTHONUTF8="1")
    import subprocess
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    done = []
    for env in envs:
        r = subprocess.run([str(uv), "venv", "--allow-existing", "--python", str(base), str(env)], env=env_vars,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=flags)
        py = env / ("Scripts/python.exe" if E.IS_WIN else "bin/python")
        t = subprocess.run([str(py), "-c", "import sys; print(sys.prefix)"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=flags)
        ok = t.returncode == 0
        log(f"ПЕРЕНОС: среда {env.name} в {env.parent.name}: " + ("исправлена" if ok else
            f"НЕ ЗАПУСКАЕТСЯ: {r.stdout[-300:]} {r.stderr[-300:]} {t.stderr[-300:]}"))
        if not ok:
            raise E.EngineError(f"Среда Python «{env.parent.name}» после переноса не запускается — "
                                "переустановите движок (вкладка «Движок» → «Переустановить»).")
        done.append(str(env))
    return done


def auto_repair():
    """При запуске задачи: папку перенесли (кнопкой или руками) — среды Python поправить молча."""
    try:
        eng = E.paths().engine
        bad = stale_envs(eng)
        if bad:
            fix_paths(eng.parent, _guess_old(bad[0], eng.parent))
            repair_envs(eng, bad)
    except E.EngineError:
        raise
    except Exception as ex:  # noqa: BLE001
        E.log(f"ПЕРЕНОС: проверка сред не удалась: {ex}")


def _guess_old(env, new_root):
    """Старый корень по «home» среды: …\\<корень>\\Engine\\python\\… → <корень>."""
    h = str(_home(env) or "")
    k = h.lower().find(os.sep + "engine" + os.sep + "python")
    return h[:k] if k > 0 else str(new_root)


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
    ctx.progress(0.94, "Среды Python движка…")
    fixed_envs = repair_envs(Path(new) / "Engine") if (Path(new) / "Engine").is_dir() else []
    E.SETTINGS.set(root=new, migrate_declined=None)
    if how == "копирование":
        ctx.progress(0.96, "Удаление старой папки…")
        shutil.rmtree(old, ignore_errors=True)
    E.log(f"ПЕРЕНОС ПАПКИ ДАННЫХ: {old} → {new} ({how}), исправлено файлов: {n}, сред: {len(fixed_envs)}, "
          f"{time.time() - t0:.1f} с")
    ctx.progress(1.0, "Перенесено")
    return {"old": old, "new": new, "how": how, "fixed": n, "engine": E.engine_status()}
