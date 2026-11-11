# -*- coding: utf-8 -*-
"""DZOD Vocalist / DZOD Revoicer — точка входа (какая программа — решает flavor.txt, см. engine.py).

  без ключей      окно приложения
  --browser       интерфейс в обычном браузере (если встроенное окно недоступно)
  --selftest LOG  самопроверка без окна, протокол пишется в LOG
  --smoke-ui LOG  проверка, что окно открывается и интерфейс загружается
  --request JSON  обработка файла по запросу без окна (для DZOD Studio), см. core.py
  --studio JSON   окно перепевки, открытое из DZOD Studio по Slice; ответ — в result.json, см. revoicer.py
"""
import os
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
sys.path.insert(0, str(HERE))

import engine as E  # noqa: E402
import server  # noqa: E402

UI = HERE / "ui"
TITLE = E.APP
AUDIO_TYPES = ("Аудио (*.wav;*.flac;*.mp3;*.ogg;*.aif;*.aiff;*.m4a)", "Все файлы (*.*)")


def arg_after(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("--"):
            return sys.argv[i + 1]
    return default


def file_logger(path):
    f = open(path, "w", encoding="utf-8") if path else None

    def log(msg):
        if f:
            f.write(msg + "\n")
            f.flush()
        try:
            print(msg, flush=True)
        except Exception:
            pass
    return log


def run_selftest():
    import selftest
    return 0 if selftest.run(log=file_logger(arg_after("--selftest"))) else 1


def run_browser(app, url):
    webbrowser.open(url)
    app.last_ping = time.time() + 30
    while time.time() - app.last_ping < 12:
        time.sleep(1)


def run_window(app, url, smoke_log=None, title=TITLE):
    import webview

    log = file_logger(smoke_log) if smoke_log else (lambda m: None)
    state = {"ready": False}
    win = webview.create_window(title, url, width=1240, height=820, min_size=(1080, 700),
                                background_color="#0F1012")

    def picker(kind, start_dir, name=""):
        start = start_dir if start_dir and os.path.isdir(start_dir) else ""
        if kind == "save":
            return win.create_file_dialog(webview.SAVE_DIALOG, directory=start, save_filename=name or "Вокал.wav",
                                          file_types=("WAV (*.wav)",))
        if kind == "folder":
            return win.create_file_dialog(webview.FOLDER_DIALOG, directory=start)
        if kind == "pth":
            return win.create_file_dialog(webview.OPEN_DIALOG, directory=start,
                                          file_types=("Модель RVC (*.pth)", "Все файлы (*.*)"))
        if kind == "index":
            return win.create_file_dialog(webview.OPEN_DIALOG, directory=start,
                                          file_types=("Индекс RVC (*.index)", "Все файлы (*.*)"))
        if kind == "midi":
            return win.create_file_dialog(webview.OPEN_DIALOG, directory=start,
                                          file_types=("MIDI с текстом (*.mid;*.midi)", "Все файлы (*.*)"))
        multi = kind == "audios"
        return win.create_file_dialog(webview.OPEN_DIALOG, directory=start, allow_multiple=multi,
                                      file_types=AUDIO_TYPES)

    app.picker = picker

    def ready():
        state["ready"] = True
        if smoke_log:
            log("интерфейс загрузился")
            threading.Timer(1.5, win.destroy).start()

    app.on_ready = ready

    def closing():
        # во время работы (особенно обучения) окно не закрывается сразу: спрашиваем в самом интерфейсе
        if smoke_log or app.allow_close or app.job.get("state") != "running":
            return True
        threading.Thread(target=lambda: _safe_js(win, "window.askQuit && window.askQuit()"), daemon=True).start()
        return False

    def quit_now():
        app.allow_close = True
        win.destroy()

    app.quit = quit_now

    try:
        win.events.closing += closing
    except Exception:
        pass
    if smoke_log:
        def watchdog():
            time.sleep(90)
            if not state["ready"]:
                log("интерфейс не загрузился за 90 секунд")
                os._exit(3)
        threading.Thread(target=watchdog, daemon=True).start()
    webview.start(gui="edgechromium") if sys.platform == "win32" else webview.start()
    if smoke_log:
        log("окно закрыто; результат: " + ("OK" if state["ready"] else "НЕ ЗАГРУЗИЛСЯ"))
        return 0 if state["ready"] else 3
    return 0


def _safe_js(win, code):
    try:
        win.evaluate_js(code)
    except Exception:
        pass


def main():
    if "--selftest" in sys.argv:
        return run_selftest()
    if "--request" in sys.argv:
        import core
        req = arg_after("--request")
        if not req:
            return 2
        return core.run_request(req)
    app = server.App(UI)
    title = TITLE
    if "--studio" in sys.argv:
        import revoicer as R
        rq = arg_after("--studio")
        try:
            req = R.read_request(rq)
            app.session = R.Session(req)
        except Exception as ex:  # noqa: BLE001
            server.log_error(traceback.format_exc())
            if rq:
                try:
                    import json
                    res = json.loads(Path(rq).read_text(encoding="utf-8-sig")).get("result") or str(Path(rq).with_name("result.json"))
                except Exception:  # noqa: BLE001
                    res = str(Path(rq).with_name("result.json"))
                R.write_result(res, {"ok": False, "error": str(ex)})
            return 1
        line = (req.get("studio") or {}).get("line")
        title = f"{TITLE} — {line}" if line else TITLE
    httpd, url = server.serve(app)
    E.log(f"Запуск {TITLE} {E.VERSION}")
    code = 0
    try:
        if "--browser" in sys.argv:
            run_browser(app, url)
        else:
            try:
                code = run_window(app, url, arg_after("--smoke-ui") if "--smoke-ui" in sys.argv else None, title)
            except Exception:
                server.log_error(traceback.format_exc())
                if "--smoke-ui" in sys.argv:
                    file_logger(arg_after("--smoke-ui"))("ОШИБКА ОКНА:\n" + traceback.format_exc())
                    code = 4
                else:
                    run_browser(app, url)
    finally:
        app.close()
        httpd.shutdown()
    return code


if __name__ == "__main__":
    sys.exit(main())
