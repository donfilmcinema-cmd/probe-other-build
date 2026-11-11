# -*- coding: utf-8 -*-
"""Локальный сервер приложения: отдаёт интерфейс, выполняет команды, отдаёт звук проигрывателю.
Слушает только 127.0.0.1; каждый запрос проверяется по одноразовому ключу."""
import json
import math
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

import audio as A
import engine as E
import songs as S
import voices as V

AUDIO_EXT = {".wav", ".flac", ".mp3", ".ogg", ".aif", ".aiff", ".m4a"}


def clean(o):
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return f if math.isfinite(f) else None
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, Path):
        return str(o)
    return o


def log_error(text):
    E.log("ОШИБКА ПРОГРАММЫ:\n" + text)


class App:
    def __init__(self, ui_dir):
        self.ui_dir = Path(ui_dir)
        self.token = secrets.token_urlsafe(16)
        self.picker = None
        self.on_ready = None
        self.allow_close = False
        self.quit = None
        self.last_ping = 0.0
        self.lock = threading.Lock()
        self.media = set()
        self.ctx = None
        self.session = None          # окно открыто из DZOD Studio (revoicer.Session)
        self.job = {"id": 0, "state": "idle", "frac": 0.0, "msg": "", "result": None, "error": None,
                    "kind": "", "title": "", "extra": {}, "started": 0}
        threading.Thread(target=self._housekeeping, daemon=True).start()

    def _housekeeping(self):
        try:
            E.ensure_workers()
            E.clear_tmp()
        except Exception:
            pass

    # ---------- задачи ----------
    def start_job(self, kind, title, fn):
        with self.lock:
            if self.job["state"] == "running":
                raise E.EngineError(f"Сейчас идёт «{self.job['title']}» — дождитесь окончания или остановите.")
            jid = self.job["id"] + 1
            ctx = E.Ctx()
            self.ctx = ctx
            job = {"id": jid, "state": "running", "frac": 0.0, "msg": "Начинаю…", "result": None,
                   "error": None, "kind": kind, "title": title, "extra": ctx.extra, "started": time.time()}
            self.job = job

        def progress(frac, msg=""):
            if job["state"] == "running" and not job.get("stopping"):
                job["frac"] = float(frac)
                if msg:
                    job["msg"] = msg

        ctx._progress = progress

        def finish(**kw):
            # задача, сброшенная принудительно, своё состояние уже не меняет
            if job["state"] == "running":
                job.update(**kw)

        def work():
            E.log(f"ЗАДАЧА: {title}")
            try:
                res = clean(fn(ctx))
                finish(state="done", frac=1.0, result=res)
                E.log(f"ГОТОВО: {title}")
            except E.Cancelled:
                finish(state="cancelled", error="Остановлено.")
                E.log(f"ОСТАНОВЛЕНО: {title}")
            except (E.EngineError, A.AudioError) as ex:
                finish(state="cancelled" if ctx.cancelled else "error", error="Остановлено." if ctx.cancelled else str(ex))
                E.log(f"ОШИБКА: {title}: {ex}")
            except MemoryError:
                finish(state="error", error="Не хватило оперативной памяти.")
            except Exception as ex:  # noqa: BLE001
                log_error(traceback.format_exc())
                finish(state="error", error=f"Непредвиденная ошибка: {ex}. Подробности — в журнале.")

        threading.Thread(target=work, daemon=True).start()
        return {"job": jid}

    def job_view(self):
        j = dict(self.job)
        j["extra"] = dict(j.get("extra") or {})
        j["elapsed"] = round(time.time() - j["started"]) if j.get("started") else 0
        return j

    def cancel(self):
        if self.ctx and self.job["state"] == "running":
            jid = self.job["id"]
            self.job["msg"] = "Останавливаю…"
            self.job["stopping"] = True
            self.ctx.cancel()

            def watchdog():
                # если за 15 с работа не остановилась — завершить все процессы движка и освободить программу
                time.sleep(15)
                if self.job["id"] == jid and self.job["state"] == "running":
                    E.log("ОСТАНОВКА: задача не ответила за 15 с — принудительно")
                    E.kill_children_of_self()
                    time.sleep(2)
                    if self.job["id"] == jid and self.job["state"] == "running":
                        self.job.update(state="cancelled", error="Остановлено принудительно.")
            threading.Thread(target=watchdog, daemon=True).start()
        return {}

    def allowed(self, p):
        p = os.path.normcase(os.path.abspath(p))
        if p in self.media:
            return True
        try:
            rootp = os.path.normcase(os.path.abspath(str(E.root())))
            return os.path.splitext(p)[1].lower() in AUDIO_EXT and p.startswith(rootp + os.sep)
        except Exception:
            return False

    # ---------- команды ----------
    def cmd(self, name, b):
        b = b or {}
        if name == "info":
            return {"version": E.VERSION, "app": E.APP, "flavor": E.FLAVOR,
                    "studio": self.session.view() if self.session else None,
                    "dialogs": self.picker is not None, "settings": E.SETTINGS.d,
                    "engine": E.engine_status(), "default_root": E.default_root(),
                    "log": str(E.log_path()), "job": self.job_view()}
        if name == "ping":
            self.last_ping = time.time()
            return {"job": self.job_view()}
        if name == "ready":
            self.last_ping = time.time()
            if self.on_ready:
                self.on_ready()
            return {}
        if name == "settings":
            E.SETTINGS.set(**{k: v for k, v in b.items() if k not in ("root", "presets")})
            return {}
        if name == "pick":
            return self.pick(b)
        if name == "save_as":
            src, dst = b.get("src", ""), b.get("dst", "")
            if not self.allowed(src) or not os.path.isfile(src):
                raise E.EngineError("Файл недоступен.")
            if not dst:
                raise E.EngineError("Не указано, куда сохранить.")
            import shutil
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            if os.path.normcase(os.path.abspath(src)) != os.path.normcase(os.path.abspath(dst)):
                shutil.copyfile(src, dst)
            E.log(f"СОХРАНЕНО: {src} -> {dst}")
            return {"path": dst}
        if name == "open":
            return {"ok": open_path(b.get("path"), b.get("select"))}
        if name == "job":
            return self.job_view()
        if name == "quit":
            self.cancel()
            if self.quit:
                threading.Timer(0.3, self.quit).start()
            return {}
        if name == "cancel":
            return self.cancel()
        if name == "peaks":
            p = b.get("path", "")
            if not self.allowed(p):
                raise E.EngineError("Файл недоступен.")
            if b.get("t0") is not None and b.get("t1") is not None:
                return A.peaks(p, int(b.get("n", 1200)), float(b["t0"]), float(b["t1"]))
            return A.peaks(p, int(b.get("n", 1200)))
        if name == "log_tail":
            try:
                t = E.log_path().read_text(encoding="utf-8", errors="replace")
            except Exception:
                t = ""
            return {"text": t[-int(b.get("chars", 20000)):]}

        # движок
        if name == "engine":
            return E.engine_status()
        if name == "set_root":
            path = str(b.get("path") or "").strip().rstrip("\\/")
            problem = E.check_root(path)
            if problem:
                raise E.EngineError(problem)
            if self.job["state"] == "running":
                raise E.EngineError("Дождитесь окончания текущей задачи.")
            Path(path).mkdir(parents=True, exist_ok=True)
            E.SETTINGS.set(root=path)
            return E.engine_status()
        if name == "sep_models":
            return {"models": E.sep_models_status(), "folders": E.folder_sizes()}
        if name == "sep_download":
            return self.start_job("models", "Скачивание моделей чистки", lambda ctx: E.download_sep_models(ctx))
        if name == "install":
            return self.start_job("install", "Установка движка",
                                  lambda ctx: E.install_engine(ctx, bool(b.get("reinstall"))))

        # DZOD Executor (проба E1): пение по MIDI с текстом
        if name == "exec_status":
            st = E.executor_status()
            st["available"] = not E.REVOICER
            return st
        if name == "exec_install":
            return self.start_job("exec_install", "Установка Executor", lambda ctx: E.install_executor(ctx))
        if name.startswith("exec_"):
            import executor as X
        if name == "exec_check":
            return self.start_job("exec_check", "Разбор MIDI", lambda ctx: X.check_midi(ctx, b["midi"]))
        if name == "exec_sing":
            return self.start_job("exec_sing", "Executor: пение по нотам",
                                  lambda ctx: X.sing(ctx, b["midi"], b.get("voice"), b.get("opts"), b.get("preset"), b.get("params"),
                                                     b.get("styles"), b.get("curves"), b.get("range")))
        if name == "exec_presets":
            return {"presets": X.list_presets() if E.executor_status()["ready"] else []}
        if name == "exec_preset_save":
            return X.save_preset(b["name"], b.get("voice"), b.get("opts"), note=b.get("note", ""), params=b.get("params"))
        if name == "exec_guide":
            return self.start_job("exec_guide", "Guide Tone", lambda ctx: X.guide(ctx, b["midi"]))
        if name == "exec_defaults":
            return {"params": X.EXEC_DEFAULTS}
        if name == "exec_transcribe":
            return self.start_job("exec_transcribe", "MIDI из вокала",
                                  lambda ctx: X.transcribe(ctx, b["wav"], b["lyrics"], b.get("language", "auto"),
                                                           b.get("detail", "notes")))
        if name == "exec_results":
            return {"files": X.results(b["midi"]) if b.get("midi") else []}

        # песни
        if name == "songs":
            return {"songs": S.list_songs()}
        if name == "song_open":
            p = b.get("path")
            if not p or not os.path.isfile(p):
                raise E.EngineError("Файл не найден.")
            s = S.create(p, b.get("kind", "stem"))
            return s.view()
        if name == "song":
            return S.get(b["id"]).view()
        if name == "song_kind":
            return S.set_kind(b["id"], b.get("kind"))
        if name == "song_clean":
            return self.start_job("clean", "Чистка вокала", lambda ctx: S.clean(ctx, b["id"], b.get("opts")))
        if name == "song_convert":
            what = tuple(b.get("what") or ("lead",))
            return self.start_job("convert", "Перепевка",
                                  lambda ctx: S.convert(ctx, b["id"], b["voice"], b.get("opts"), what))
        if name == "song_best":
            return S.set_best(b["id"], b["take"])
        if name == "song_lyrics":
            return S.save_lyrics(b["id"], b.get("lyrics", ""))
        if name == "text_analyze":
            # перепевка с текстом: где слова текста в исходнике дубля, подозрительные, где пометки
            return self.start_job("text", "Текст по записи",
                                  lambda ctx: S.text_analyze(ctx, b["id"], b["take"], b.get("lyrics"), b.get("marks"),
                                                             b.get("language") or "auto"))
        if name == "text_fix":
            return self.start_job("text", "Исправление по тексту",
                                  lambda ctx: S.text_fix(ctx, b["id"], b["take"], b.get("lyrics"), b.get("marks") or [],
                                                         b.get("language") or "auto"))
        if name == "song_clear":
            return S.clear(b["id"], b.get("what"))
        if name == "take_place":
            # «Рядом с исходником»: копия дубля в папку исходного файла, имя …_ResingerNN.wav
            import revoicer as R
            dst, _t = R.place(b["id"], b["take"])
            return {"path": str(dst), "name": dst.name, "song": S.get(b["id"]).view()}
        if name == "studio_insert":
            if not self.session:
                raise E.EngineError("Окно открыто не из DZOD Studio.")
            r = self.session.insert(b["take"])
            if self.quit:
                threading.Timer(2.5, self.quit).start()
            return r
        if name == "take_delete":
            return S.delete_take(b["id"], b["take"])
        if name == "song_delete":
            import shutil
            s = S.get(b["id"])
            shutil.rmtree(s.dir, ignore_errors=True)
            return {"songs": S.list_songs()}
        if name == "presets":
            return {"presets": S.presets(), "defaults": S.CONVERT_DEFAULTS, "clean_defaults": S.CLEAN_DEFAULTS}
        if name == "preset_save":
            return {"presets": S.save_preset(b.get("name"), b.get("opts"))}
        if name == "preset_delete":
            return {"presets": S.delete_preset(b.get("name"))}

        # голоса
        if name == "voices":
            return {"voices": V.list_voices()}
        if name == "voice_create":
            return V.create(b.get("name"))
        if name == "voice_rename":
            return V.rename(b["slug"], b.get("name"))
        if name == "voice":
            return V.get(b["slug"]).view(full=True)
        if name == "voice_import":
            return V.import_model(b.get("name"), b.get("pth"), b.get("index"))
        if name == "voice_import_folder":
            folder = b.get("folder")
            if not folder or not os.path.isdir(folder):
                raise E.EngineError("Папка не найдена.")
            return V.import_folder(folder)
        if name == "voice_export_folder":
            dest = b.get("dest")
            if not dest or not os.path.isdir(dest):
                raise E.EngineError("Папка не найдена.")
            return V.export_folder(b["slug"], dest)
        if name == "voice_delete":
            import shutil
            v = V.get(b["slug"])
            if self.job["state"] == "running":
                raise E.EngineError("Дождитесь окончания текущей задачи.")
            shutil.rmtree(v.dir, ignore_errors=True)
            if b.get("with_training"):
                shutil.rmtree(v.logs(), ignore_errors=True)
            return {"voices": V.list_voices()}
        if name == "add_sources":
            files = [f for f in (b.get("files") or []) if f]
            return self.start_job("sources", "Добавление записей в датасет",
                                  lambda ctx: V.add_sources(ctx, b["slug"], files, b.get("clean")))
        if name == "add_phrase_folder":
            folder = b.get("folder")
            return self.start_job("sources", "Добавление готовых фраз",
                                  lambda ctx: V.add_phrase_folder(ctx, b["slug"], folder))
        if name == "phrases":
            return {"phrases": V.phrase_list(b["slug"])}
        if name == "mark":
            return V.mark(b["slug"], b.get("ids"), b.get("status"))
        if name == "source_remove":
            return V.remove_source(b["slug"], b["sid"])
        if name == "dataset_build":
            return V.build_dataset(b["slug"])
        if name == "train":
            v = V.get(b["slug"])
            return self.start_job("train", f"Обучение голоса «{v.name}»",
                                  lambda ctx: V.train(ctx, b["slug"], b.get("opts")))
        if name == "choose":
            return V.choose(b["slug"], b["file"])
        if name == "compare":
            test = b.get("test")
            if not test or not os.path.isfile(test):
                raise E.EngineError("Выберите отрывок для сравнения.")
            return self.start_job("compare", "Сравнение эпох",
                                  lambda ctx: V.compare(ctx, b["slug"], test, b.get("files"), b.get("opts")))
        if name == "ck_delete":
            return V.delete_checkpoints(b["slug"], b.get("keep"))
        raise KeyError(name)

    def pick(self, b):
        if not self.picker:
            return {"manual": True}
        kind = b.get("kind", "audio")
        start = (E.SETTINGS.get("last_save_dir") if kind == "save" else None) or E.SETTINGS.get("last_dir") or ""
        r = self.picker(kind, start, b.get("name") or "")
        if not r:
            return {"path": None, "paths": []}
        lst = r if isinstance(r, (list, tuple)) else [r]
        lst = [str(x) for x in lst]
        first = lst[0]
        if kind == "save":
            if not first.lower().endswith(".wav"):
                first += ".wav"
            E.SETTINGS.set(last_save_dir=str(Path(first).parent))
            return {"path": first, "paths": [first]}
        for x in lst:
            if os.path.isfile(x):
                self.media.add(os.path.normcase(os.path.abspath(x)))
        E.SETTINGS.set(last_dir=str(Path(first).parent if os.path.isfile(first) else first))
        return {"path": first, "paths": lst}

    def close(self):
        try:
            self.cancel()
        except Exception:
            pass
        if self.session:
            try:
                self.session.cancel()      # закрыли без вставки — Studio узнаёт, что ничего не вставлено
            except Exception:
                log_error(traceback.format_exc())


def open_path(path, select=False):
    """Открывает папку или файл средствами системы; select — показать файл в проводнике."""
    if not path or not Path(path).exists():
        return False
    try:
        if sys.platform == "win32":
            if select and Path(path).is_file():
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif os.environ.get("DISPLAY"):
            subprocess.Popen(["xdg-open", str(Path(path).parent if select else path)])
        return True
    except Exception:
        return False


TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml",
         ".png": "image/png", ".ttf": "font/ttf", ".otf": "font/otf", ".ico": "image/x-icon",
         ".wav": "audio/wav", ".flac": "audio/flac", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
         ".aif": "audio/aiff", ".aiff": "audio/aiff", ".m4a": "audio/mp4"}


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _authed(self, q):
            return (self.headers.get("X-Token") or (q.get("t") or [""])[0]) == app.token

        def do_GET(self):
            try:
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if u.path == "/media":
                    return self._media(q)
                rel = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
                f = (app.ui_dir / rel).resolve()
                if app.ui_dir.resolve() not in f.parents or not f.is_file():
                    return self._send(404, b"not found", "text/plain")
                ctype = TYPES.get(f.suffix.lower()) or mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                if f.suffix in (".html", ".js", ".css"):
                    ctype += "; charset=utf-8"
                self._send(200, f.read_bytes(), ctype)
            except (BrokenPipeError, ConnectionError):
                pass

        do_HEAD = do_GET

        def _media(self, q):
            p = (q.get("p") or [""])[0]
            if not self._authed(q) or not app.allowed(p) or not os.path.isfile(p):
                return self._send(403, b"forbidden", "text/plain")
            size = os.path.getsize(p)
            start, end = 0, size - 1
            rng = self.headers.get("Range", "")
            code = 200
            if rng.startswith("bytes="):
                a, _, b2 = rng[6:].partition("-")
                try:
                    start = int(a) if a else max(0, size - int(b2))
                    end = min(int(b2), size - 1) if (a and b2) else size - 1
                    code = 206
                except ValueError:
                    pass
            if start > end or start >= size:
                return self._send(416, b"", "text/plain", {"Content-Range": f"bytes */{size}"})
            n = end - start + 1
            self.send_response(code)
            self.send_header("Content-Type", TYPES.get(os.path.splitext(p)[1].lower(), "application/octet-stream"))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(n))
            self.send_header("Cache-Control", "no-store")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(p, "rb") as f:
                f.seek(start)
                while n > 0:
                    chunk = f.read(min(1 << 16, n))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    n -= len(chunk)

        def do_POST(self):
            try:
                u = urlparse(self.path)
                if not u.path.startswith("/api/") or not self._authed(parse_qs(u.query)):
                    return self._send(403, {"_failed": True, "error": "forbidden"})
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
                try:
                    self._send(200, clean(app.cmd(u.path[5:], body)))
                except (E.EngineError, A.AudioError) as ex:
                    self._send(200, {"_failed": True, "error": str(ex)})
                except KeyError as ex:
                    self._send(200, {"_failed": True, "error": f"Неизвестная команда или нет поля: {ex}"})
                except Exception as ex:  # noqa: BLE001
                    log_error(traceback.format_exc())
                    self._send(200, {"_failed": True, "error": f"Непредвиденная ошибка: {ex}"})
            except (BrokenPipeError, ConnectionError):
                pass
    return H


def serve(app, port=0):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/?t={app.token}"
