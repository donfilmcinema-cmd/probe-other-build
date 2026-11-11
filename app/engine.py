# -*- coding: utf-8 -*-
"""DZOD Vocalist — основа: пути, настройки, запуск процессов движка, установка движка.

Движок — это две отдельные среды Python, которые программа ставит сама в папку
<корень>\\Engine:
  Applio  (голос: обучение и перепевка, Applio 3.6.6)
  Sep     (чистка: модели UVR через audio-separator)
Сама программа лёгкая: numpy, scipy, soundfile, pywebview.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

VERSION = "0.7.3"
IS_WIN = sys.platform == "win32"
_HERE0 = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _flavor():
    """Какая программа собрана: «vocalist» (полная) или «revoicer» (DZOD Revoicer для DZOD Studio).
    В сборке Revoicer рядом лежит flavor.txt; для разработки — переменная DZOD_FLAVOR."""
    f = os.environ.get("DZOD_FLAVOR")
    if not f:
        try:
            f = (_HERE0 / "flavor.txt").read_text(encoding="utf-8").strip()
        except OSError:
            f = "vocalist"
    return "revoicer" if f.lower() == "revoicer" else "vocalist"


FLAVOR = _flavor()
REVOICER = FLAVOR == "revoicer"
APP = "DZOD Revoicer" if REVOICER else "DZOD Vocalist"
ROOT_NAME = "DZOD_Revoicer" if REVOICER else "DZOD_Vocalist"

APPLIO_TAG = "3.6.6"
APPLIO_ZIP = f"https://github.com/IAHispano/Applio/archive/refs/tags/{APPLIO_TAG}.zip"
UV_VER = "0.11.32"
UV_ZIP = f"https://github.com/astral-sh/uv/releases/download/{UV_VER}/uv-x86_64-pc-windows-msvc.zip"
TORCH_INDEX = "https://download.pytorch.org/whl/cu128"
SEP_PACKAGES = ["audio-separator[gpu]==0.47.0", "audioread", "torch==2.11.0", "torchaudio==2.11.0"]
PY_VER = "3.12"
# DZOD Executor (E1): пение по MIDI с текстом — SoulX-Singer (Apache 2.0), код закреплён на коммите
EXEC_COMMIT = "81aeb3ae772c70093c3de74dc23c92d983801ae4"
EXEC_ZIP = f"https://github.com/Soul-AILab/SoulX-Singer/archive/{EXEC_COMMIT}.zip"
EXEC_HF_REPO = "Soul-AILab/SoulX-Singer"
EXEC_TORCH_INDEX = "https://download.pytorch.org/whl/cu121"
# «Перепевка с текстом», русский: подсказка речью Piper (GPLv3, ставится по требованию в среду Executor).
# Голос подсказки в результат не попадает — из него берётся только содержание (какие звуки).
PIPER_PKG = "piper-tts==1.3.0"
PIPER_VOICE = "ru_RU-irina-medium"
PIPER_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ru/ru_RU/irina/medium/" + PIPER_VOICE
EXEC_PACKAGES = ["torch==2.2.0", "torchaudio==2.2.0", "numpy<2.0.0", "transformers==4.41.2", "accelerate==1.11.0",
                 "omegaconf==2.3.0", "librosa==0.11.0", "scipy==1.15.3", "soundfile==0.13.1", "tqdm",
                 "g2p_en==2.1.0", "nltk==3.9.2", "mido==1.3.3", "huggingface_hub>=0.20.0"]
NO_WINDOW = 0x08000000 if IS_WIN else 0

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
WORKERS_SRC = HERE / "workers"


class EngineError(Exception):
    """Ошибка, понятная пользователю (текст показывается как есть)."""


class Cancelled(EngineError):
    pass


# ---------------------------------------------------------------- данные программы

def data_dir():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    d = Path(base) / APP
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path():
    return data_dir() / "log.txt"


_log_lock = threading.Lock()


def log(text):
    try:
        with _log_lock:
            p = log_path()
            if p.exists() and p.stat().st_size > 8_000_000:
                p.replace(p.with_suffix(".old.txt"))
            with open(p, "a", encoding="utf-8") as f:
                f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + text.rstrip() + "\n")
    except Exception:
        pass


class Settings:
    def __init__(self):
        self.path = data_dir() / "settings.json"
        try:
            self.d = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self.d = {}
        self.lock = threading.Lock()

    def get(self, k, default=None):
        return self.d.get(k, default)

    def set(self, **kw):
        with self.lock:
            self.d.update(kw)
            data = json.dumps(self.d, ensure_ascii=False, indent=1)
            tmp = self.path.with_suffix(".tmp")
            # на Windows файл иногда держит антивирус или проводник — несколько попыток, потом запись напрямую
            for attempt in range(6):
                try:
                    tmp.write_text(data, encoding="utf-8")
                    os.replace(tmp, self.path)
                    return
                except OSError:
                    time.sleep(0.15 * (attempt + 1))
            try:
                self.path.write_text(data, encoding="utf-8")
            except OSError as ex:
                log(f"НАСТРОЙКИ НЕ СОХРАНЕНЫ: {ex}")


SETTINGS = Settings()


def default_root():
    if IS_WIN:
        return f"D:\\{ROOT_NAME}" if os.path.isdir("D:\\") else f"C:\\{ROOT_NAME}"
    return str(Path.home() / ROOT_NAME)


def check_root(path):
    """Корень должен быть без пробелов и русских букв — этого требуют модели движка."""
    p = str(path or "").strip()
    if not p:
        return "Укажите папку."
    if not os.path.isabs(p):
        return f"Нужен полный путь, например D:\\{ROOT_NAME}."
    if any(ord(c) > 127 for c in p):
        return "В пути не должно быть русских букв — движок с такими путями работает ненадёжно."
    if " " in p:
        return "В пути не должно быть пробелов — движок с такими путями работает ненадёжно."
    return ""


def root():
    return Path(os.environ.get("DZOD_ROOT") or SETTINGS.get("root") or default_root())


def songs_dir():
    d = root() / "Songs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def voices_dir():
    d = root() / "Voices"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- пути движка

def other_root():
    """Папка второй программы (DZOD Vocalist ↔ DZOD Revoicer): из её настроек, иначе — её папка по умолчанию."""
    other_app = "DZOD Vocalist" if REVOICER else "DZOD Revoicer"
    other_name = "DZOD_Vocalist" if REVOICER else "DZOD_Revoicer"
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    r = None
    try:
        r = json.loads((Path(base) / other_app / "settings.json").read_text(encoding="utf-8")).get("root")
    except Exception:  # noqa: BLE001
        pass
    if not r:
        r = default_root().replace(ROOT_NAME, other_name)
    return Path(r)


def _engine_ready_in(e):
    """Основной движок установлен в папке e (метки шагов + Python сред Applio и чистки)."""
    q = Paths(e)
    return all((e / f".done_{s}").exists() for s, _n, _w in STEPS) and q.applio_py.exists() and q.sep_py.exists()


def _exec_ready_in(e):
    """Executor установлен в движке e."""
    q = Paths(e)
    return all((e / f".done_{s}").exists() for s, _n, _w in EXEC_STEPS) and q.exec_py.exists() and q.exec_model.exists()


def _sharing():
    return not (os.environ.get("DZOD_ROOT") or os.environ.get("DZOD_NO_SHARE"))


class Paths:
    """Где лежит движок. Для разработки на Linux пути можно подменить переменными DZOD_*.

    Общий движок (11.11, автор: «нельзя установить один раз для обоих?»): тяжёлые части ставятся один раз на две программы.
    - Executor (~6 ГБ) — общий в обе стороны: если в своей папке его нет, а у второй программы он готов — берётся оттуда.
    - Основной движок (~12 ГБ) — Revoicer берёт движок Vocalist (у Vocalist есть всё, что нужно Revoicer);
      Vocalist движок Revoicer не берёт: в нём нет файлов для обучения голоса.
    Песни, голоса, временные файлы и рабочие скрипты — у каждой программы свои."""

    def __init__(self, engine_dir=None):
        own = root() / "Engine"
        e = Path(engine_dir) if engine_dir else own
        self.shared_engine = None
        self.shared_exec = None
        if engine_dir is None and _sharing():
            try:
                oe = other_root() / "Engine"
                if REVOICER and oe != own and not _engine_ready_in(own) and _engine_ready_in(oe):
                    e = oe
                    self.shared_engine = str(oe)
            except Exception:  # noqa: BLE001 — нет доступа к папке второй программы: работаем со своей
                pass
        self.engine = e
        self.uv = Path(os.environ.get("DZOD_UV") or (e / "uv" / ("uv.exe" if IS_WIN else "uv")))
        self.uv_cache = e / "uv-cache"
        self.python_dir = e / "python"
        self.applio = Path(os.environ.get("DZOD_APPLIO_DIR") or (e / "Applio"))
        self.applio_env = self.applio / "env"
        self.sep = e / "Sep"
        self.sep_env = self.sep / "env"
        self.sep_models = Path(os.environ.get("DZOD_SEP_MODELS") or (self.sep / "models"))
        self.exec = e / "Executor"
        if engine_dir is None and _sharing():
            try:
                oe = other_root() / "Engine"
                if oe != e and not _exec_ready_in(e) and _exec_ready_in(oe):
                    self.exec = oe / "Executor"
                    self.shared_exec = str(oe)
            except Exception:  # noqa: BLE001
                pass
        self.exec_src = self.exec / "SoulX-Singer"
        self.exec_env = self.exec / "env"
        self.exec_model = self.exec / "models" / "model.pt"
        self.exec_nltk = self.exec / "nltk_data"
        self.exec_prompt_wav = self.exec / "prompt" / "en_prompt.wav"
        self.exec_prompt_meta = self.exec / "prompt" / "en_prompt.json"
        self.exec_piper = self.exec / "piper"
        self.exec_piper_voice = self.exec_piper / f"{PIPER_VOICE}.onnx"
        self.tmp = own / "tmp"                 # временные файлы и рабочие скрипты — всегда свои
        self.workers = own / "workers"
        self.status = e / "status.json"

    @staticmethod
    def _py(env):
        return env / ("Scripts/python.exe" if IS_WIN else "bin/python")

    @property
    def applio_py(self):
        return Path(os.environ.get("DZOD_APPLIO_PY") or self._py(self.applio_env))

    @property
    def sep_py(self):
        return Path(os.environ.get("DZOD_SEP_PY") or self._py(self.sep_env))

    @property
    def exec_py(self):
        return Path(os.environ.get("DZOD_EXEC_PY") or self._py(self.exec_env))

    def logs(self):
        return self.applio / "logs"


def paths():
    return Paths()


def ensure_workers():
    """Рабочие скрипты движка копируются из программы при каждом запуске — так обновляются вместе с ней."""
    p = paths()
    p.workers.mkdir(parents=True, exist_ok=True)
    for f in WORKERS_SRC.glob("*.py"):
        dst = p.workers / f.name
        try:
            if not dst.exists() or dst.read_bytes() != f.read_bytes():
                shutil.copyfile(f, dst)
        except OSError:
            pass
    return p.workers


def new_tmp(tag):
    p = paths().tmp / f"{tag}_{int(time.time() * 1000) % 10**9}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def clear_tmp(keep_hours=24):
    t = paths().tmp
    if not t.is_dir():
        return
    now = time.time()
    for d in t.iterdir():
        try:
            if now - d.stat().st_mtime > keep_hours * 3600:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


# ---------------------------------------------------------------- процессы

def child_env(extra=None):
    env = dict(os.environ)
    env.update({
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "UV_CACHE_DIR": str(paths().uv_cache),
        "UV_PYTHON_INSTALL_DIR": str(paths().python_dir),
        "UV_LINK_MODE": "hardlink",
        "UV_NO_PROGRESS": "1",
        "TQDM_MININTERVAL": "1",
        "PYTHONWARNINGS": "ignore",
    })
    for k in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX"):
        env.pop(k, None)
    # ffmpeg.exe и ffprobe.exe кладёт в папку Applio шаг «Модели голоса»; они нужны и чистке
    ap = paths().applio
    if (ap / ("ffmpeg.exe" if IS_WIN else "ffmpeg")).exists():
        env["PATH"] = str(ap) + os.pathsep + env.get("PATH", "")
    if extra:
        env.update(extra)
    return env


PCT = re.compile(r"(\d{1,3})%\|")


class Ctx:
    """Связь задачи с окном: ход работы, отмена, запуск процессов движка."""

    def __init__(self, progress=None):
        self._progress = progress or (lambda f, m="": None)
        self.cancelled = False
        self.proc = None
        self.lo, self.hi = 0.0, 1.0
        self.msg = ""
        self.extra = {}

    # ход работы внутри отрезка [lo, hi] общей полосы
    def span(self, lo, hi, msg=None):
        self.lo, self.hi = lo, hi
        if msg:
            self.msg = msg
        self._progress(lo, self.msg)

    def progress(self, frac, msg=None):
        if msg:
            self.msg = msg
        self._progress(self.lo + (self.hi - self.lo) * max(0.0, min(1.0, frac)), self.msg)

    def check(self):
        if self.cancelled:
            raise Cancelled("Остановлено.")

    def cancel(self):
        self.cancelled = True
        threading.Thread(target=kill_tree, args=(self.proc,), daemon=True).start()

    def run(self, args, cwd=None, env=None, on_line=None, sub_pct=True, what="движок"):
        """Запускает процесс движка; строки @@P двигают полосу, @@R — результат, @@E — ошибка.
        Возвращает разобранный @@R (или None). Весь вывод пишется в журнал."""
        self.check()
        args = [str(a) for a in args]
        log("ЗАПУСК: " + " ".join(args) + (f"   (в {cwd})" if cwd else ""))
        try:
            proc = subprocess.Popen(
                args, cwd=str(cwd) if cwd else None, env=env or child_env(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                creationflags=NO_WINDOW, start_new_session=not IS_WIN)
        except FileNotFoundError:
            raise EngineError(f"Не найдена программа {args[0]}. Проверьте движок на вкладке «Движок».")
        self.proc = proc
        state = {"result": None, "error": None, "tail": []}
        base = {"lo": 0.0, "hi": None, "msg": ""}

        def handle(line, from_err):
            line = line.rstrip()
            if not line:
                return
            if line.startswith("@@P "):
                parts = line[4:].split(" ", 1)
                try:
                    lo, _, hi = parts[0].partition("|")
                    base["lo"] = float(lo)
                    base["hi"] = float(hi) if hi else None
                    base["msg"] = parts[1] if len(parts) > 1 else base["msg"]
                    self.progress(base["lo"], base["msg"] or None)
                except ValueError:
                    pass
                return
            if line.startswith("@@R "):
                try:
                    state["result"] = json.loads(line[4:])
                except ValueError:
                    state["result"] = {}
                return
            if line.startswith("@@E "):
                state["error"] = line[4:]
                return
            if line.startswith("@@H"):
                self.extra["alive"] = time.time()
                return
            m = PCT.search(line)
            if m and sub_pct:
                # полосы tqdm в журнал не пишем (их много), а показываем ход внутри шага
                pct = min(100, int(m.group(1)))
                if "iB" in line or "B/s" in line:
                    self.progress(base["lo"], f"{base['msg'] or 'Работаю…'} (скачиваю модель: {pct}%)")
                elif base["hi"] is not None:
                    self.progress(base["lo"] + (base["hi"] - base["lo"]) * pct / 100, f"{base['msg']} {pct}%")
            else:
                state["tail"].append(line)
                del state["tail"][:-40]
                log(line)
            if on_line:
                on_line(line)

        def pump(stream, from_err):
            buf = b""
            while True:
                chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
                if not chunk:
                    break
                buf += chunk
                parts = re.split(rb"[\r\n]", buf)
                buf = parts.pop()
                for p in parts:
                    handle(p.decode("utf-8", "replace"), from_err)
            if buf:
                handle(buf.decode("utf-8", "replace"), from_err)

        threads = [threading.Thread(target=pump, args=(proc.stdout, False), daemon=True),
                   threading.Thread(target=pump, args=(proc.stderr, True), daemon=True)]
        for t in threads:
            t.start()
        while True:
            try:
                code = proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if self.cancelled:
                    kill_tree(proc)
        for t in threads:
            t.join(2 if self.cancelled else 5)
        self.proc = None
        log(f"КОД ВЫХОДА: {code}")
        if self.cancelled:
            raise Cancelled("Остановлено.")
        if state["error"]:
            raise EngineError(friendly(state["error"], state["tail"]))
        if code != 0:
            raise EngineError(friendly(f"{what}: процесс завершился с ошибкой (код {code}).", state["tail"]))
        return state["result"]


def friendly(msg, tail):
    """Подсказка к частым ошибкам движка."""
    text = (msg + "\n" + "\n".join(tail or [])).lower()
    if "out of memory" in text or "cuda oom" in text or "outofmemoryerror" in text:
        return ("Не хватило памяти видеокарты. Закройте другие программы, которые её используют "
                "(браузер с видео, игры), и попробуйте снова. Для обучения можно уменьшить «Размер пачки».")
    if "no space left" in text or "not enough space" in text or "winerror 112" in text:
        return "Закончилось место на диске. Освободите место и попробуйте снова."
    if "urlopen error" in text or "connection" in text and "error" in text or "max retries" in text:
        return "Нет связи с сервером загрузки. Проверьте интернет и попробуйте снова."
    last = [t for t in (tail or []) if t.strip()][-3:]
    extra = ("\nПоследние строки журнала:\n" + "\n".join(last)) if last else ""
    return msg + extra


def kill_tree(proc):
    """Завершает процесс движка со всеми дочерними (на Windows python из venv — это ещё и дочерний python)."""
    if not proc or proc.poll() is not None:
        return
    pids = [proc.pid]
    try:
        import psutil
        try:
            kids = psutil.Process(proc.pid).children(recursive=True)
        except psutil.Error:
            kids = []
        for k in kids:
            pids.append(k.pid)
            try:
                k.kill()
            except psutil.Error:
                pass
    except ImportError:
        pass
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=NO_WINDOW, timeout=15)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    log(f"ОСТАНОВКА: завершены процессы {pids}")


def kill_children_of_self():
    """Крайняя мера: завершить все процессы, запущенные программой (движок), кроме неё самой."""
    try:
        import psutil
        for k in psutil.Process(os.getpid()).children(recursive=True):
            try:
                k.kill()
            except psutil.Error:
                pass
    except Exception as ex:  # noqa: BLE001
        log(f"не удалось завершить дочерние процессы: {ex}")


# ---------------------------------------------------------------- установка движка

def download(url, dst, ctx, label):
    ctx.check()
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    log(f"СКАЧИВАЮ {url}")
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP}/{VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                ctx.check()
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    ctx.progress(got / total, f"{label}: {got / 1e6:.0f} из {total / 1e6:.0f} МБ")
    except Cancelled:
        raise
    except Exception as ex:
        raise EngineError(f"Не удалось скачать {label}: {ex}. Проверьте интернет и попробуйте снова.")
    os.replace(tmp, dst)
    return dst


def disk_free_gb(path):
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free / 1e9
    except OSError:
        return None


STEPS = [
    ("uv", "Установщик пакетов", 0.03),
    ("python", "Python 3.12", 0.05),
    ("applio_code", "Applio 3.6.6", 0.04),
    ("applio_env", "Библиотеки голоса (PyTorch для видеокарт NVIDIA, около 4 ГБ)", 0.45),
    ("applio_models", "Модели голоса (около 1,5 ГБ)", 0.18),
    ("sep_env", "Библиотеки чистки", 0.17),
    ("gpu", "Проверка видеокарты", 0.08),
]


def marker(name):
    p = paths()
    if name.startswith("exec"):             # метки Executor — в движке, где лежит Executor (может быть общий)
        return p.exec.parent / f".done_{name}"
    return p.engine / f".done_{name}"


def engine_status():
    p = paths()
    steps = [{"id": s, "name": n, "done": marker(s).exists()} for s, n, _ in STEPS]
    try:
        st = json.loads(p.status.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    applio_ok = p.applio_py.exists() and (p.applio / "core.py").exists()
    sep_ok = p.sep_py.exists()
    ready = applio_ok and sep_ok and all(x["done"] for x in steps)
    if os.environ.get("DZOD_APPLIO_PY") and os.environ.get("DZOD_SEP_PY"):
        ready = applio_ok and sep_ok      # разработка: движок подставлен вручную
    return {
        "root": str(root()), "root_ok": check_root(str(root())) == "", "root_problem": check_root(str(root())),
        "engine": str(p.engine), "ready": ready, "steps": steps,
        "gpu": st.get("gpu"), "free_gb": disk_free_gb(root()),
        "applio": applio_ok, "sep": sep_ok,
        "shared": p.shared_engine,          # движок второй программы (общий), иначе None
    }


def install_engine(ctx, reinstall=False):
    p = paths()
    problem = check_root(str(root()))
    if problem:
        raise EngineError("Папка движка не подходит: " + problem)
    free = disk_free_gb(root())
    need = 6 if os.environ.get("DZOD_CPU_TORCH") else 15
    if free is not None and free < need:
        raise EngineError(f"На диске свободно {free:.0f} ГБ, а движку нужно около 15 ГБ. Освободите место "
                          "или выберите другую папку.")
    p.engine.mkdir(parents=True, exist_ok=True)
    if reinstall:
        for s, _, _ in STEPS:
            marker(s).unlink(missing_ok=True)
        for d in (p.applio_env, p.sep_env):
            shutil.rmtree(d, ignore_errors=True)
    ensure_workers()
    acc = 0.0
    for sid, name, w in STEPS:
        ctx.span(acc, acc + w, name + "…")
        acc += w
        if marker(sid).exists():
            continue
        globals()["_step_" + sid](ctx, p)
        marker(sid).write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    ctx.span(1.0, 1.0, "Движок установлен.")
    return engine_status()


def _uv(ctx, args, what):
    p = paths()
    return ctx.run([p.uv, *args], env=child_env(), what=what, on_line=_uv_line(ctx))


def _uv_line(ctx):
    seen = {"n": 0}

    def f(line):
        s = line.strip()
        m = re.match(r"(Downloading|Downloaded)\s+(\S+)(?:\s+\(([^)]+)\))?", s)
        if m:
            seen["n"] += 1
            word = "Скачиваю" if m.group(1) == "Downloading" else "Скачано"
            size = f" ({m.group(3)})" if m.group(3) else ""
            ctx.progress(min(0.9, seen["n"] / 60), f"{word}: {m.group(2)}{size}")
        elif s.startswith("Installed") or s.startswith("Prepared"):
            ctx.progress(0.95, s.replace("Installed", "Установлено").replace("Prepared", "Подготовлено")
                         .replace("packages", "пакетов").replace(" in ", " за "))
    return f


def _step_uv(ctx, p):
    if p.uv.exists():
        return
    if not IS_WIN:
        found = shutil.which("uv")
        if not found:
            raise EngineError("uv не найден (на Linux поставьте его сами: pip install uv).")
        p.uv.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(found, p.uv)
        return
    z = download(UV_ZIP, p.engine / "dl" / "uv.zip", ctx, "Установщик пакетов")
    with zipfile.ZipFile(z) as zf:
        for n in zf.namelist():
            if n.lower().endswith(("uv.exe", "uvx.exe")):
                p.uv.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(n) as src, open(p.uv.parent / Path(n).name, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    if not p.uv.exists():
        raise EngineError("В архиве установщика пакетов нет uv.exe.")


def _step_python(ctx, p):
    _uv(ctx, ["python", "install", PY_VER], "Установка Python")


def _step_applio_code(ctx, p):
    if (p.applio / "core.py").exists():
        return
    z = download(APPLIO_ZIP, p.engine / "dl" / "applio.zip", ctx, "Applio")
    ctx.progress(0.9, "Распаковываю Applio…")
    tmp = p.engine / "dl" / "applio_x"
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(z) as zf:
        zf.extractall(tmp)
    top = next(d for d in tmp.iterdir() if d.is_dir())
    p.applio.mkdir(parents=True, exist_ok=True)
    for item in top.iterdir():
        dst = p.applio / item.name
        if dst.exists():
            continue                    # logs (обученные модели) и прочее не трогаем
        shutil.move(str(item), str(dst))
    shutil.rmtree(tmp, ignore_errors=True)
    if not (p.applio / "core.py").exists():
        raise EngineError("Applio распакован не полностью: нет core.py.")


def _venv(ctx, env_dir):
    if (env_dir / ("Scripts/python.exe" if IS_WIN else "bin/python")).exists():
        return
    _uv(ctx, ["venv", "--python", PY_VER, "--python-preference", "only-managed", "--seed", env_dir],
        "Создание среды Python")


def _torch_args():
    if os.environ.get("DZOD_CPU_TORCH"):        # проверка на сборочном сервере без видеокарты
        return ["--extra-index-url", "https://download.pytorch.org/whl/cpu", "--index-strategy", "unsafe-best-match"]
    return ["--extra-index-url", TORCH_INDEX, "--index-strategy", "unsafe-best-match"]


def _step_applio_env(ctx, p):
    _venv(ctx, p.applio_env)
    req = p.applio / "requirements-engine.txt"
    if not req.exists():
        req = p.applio / "requirements.txt"
    _uv(ctx, ["pip", "install", "-p", p.applio_py, "-r", req, *_torch_args()], "Установка библиотек голоса")


def _step_applio_models(ctx, p):
    # Revoicer не обучает голоса — предобученные сети для обучения ему не нужны
    args = [p.applio_py, "core.py", "prerequisites",
            "--no-pretraineds-hifigan" if REVOICER else "--pretraineds-hifigan", "--models"]
    args.append("--exe" if IS_WIN else "--no-exe")
    ctx.progress(0.05, "Скачиваю модели голоса (распознавание высоты тона, тембра, предобученные сети)…")
    ctx.run(args, cwd=p.applio, what="Загрузка моделей голоса")


def _step_sep_env(ctx, p):
    _venv(ctx, p.sep_env)
    pk = list(SEP_PACKAGES)
    if os.environ.get("DZOD_CPU_TORCH"):
        pk[0] = pk[0].replace("[gpu]", "[cpu]")
    _uv(ctx, ["pip", "install", "-p", p.sep_py, *pk, *_torch_args()], "Установка библиотек чистки")
    p.sep_models.mkdir(parents=True, exist_ok=True)


GPU_PROBE = ("import json,torch\n"
             "ok=torch.cuda.is_available()\n"
             "d={'cuda':ok,'torch':torch.__version__}\n"
             "if ok:\n"
             "  pr=torch.cuda.get_device_properties(0)\n"
             "  d.update(name=pr.name, vram_gb=round(pr.total_memory/2**30,1))\n"
             "print('@@R '+json.dumps(d))\n")


def _step_gpu(ctx, p):
    info = ctx.run([p.applio_py, "-c", GPU_PROBE], what="Проверка видеокарты") or {}
    try:
        st = json.loads(p.status.read_text(encoding="utf-8"))
    except Exception:
        st = {}
    st["gpu"] = info
    st["checked"] = time.strftime("%Y-%m-%d %H:%M:%S")
    p.status.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    if not info.get("cuda") and not os.environ.get("DZOD_CPU_TORCH"):
        log("ВНИМАНИЕ: видеокарта NVIDIA не найдена — всё будет работать на процессоре, очень медленно.")


# ---------------------------------------------------------------- DZOD Executor (проба, этап E1)
EXEC_STEPS = [
    ("exec_code", "Код SoulX-Singer", 0.05),
    ("exec_env", "Библиотеки пения (PyTorch 2.2 для NVIDIA, около 3 ГБ)", 0.5),
    ("exec_model", "Модель пения SoulX-Singer (около 3 ГБ)", 0.4),
    ("exec_check", "Проверка", 0.05),
]


def piper_ready():
    p = paths()
    return marker("exec_piper").exists() and p.exec_piper_voice.exists()


def install_piper(ctx):
    """Piper для подсказки русских слов: пакет в среду Executor + голос (~60 МБ). Нужен интернет один раз."""
    if piper_ready():
        return
    p = paths()
    ctx.span(0.0, 0.7, "Piper — синтез речи для русских подсказок…")
    _uv(ctx, ["pip", "install", "-p", p.exec_py, PIPER_PKG, "numpy<2.0.0"], "Установка Piper")
    ctx.span(0.7, 1.0, "Голос Piper (русский)…")
    for ext in (".onnx", ".onnx.json"):
        download(PIPER_URL + ext, p.exec_piper / (PIPER_VOICE + ext), ctx, "Голос Piper")
    marker("exec_piper").write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")


def executor_status():
    p = paths()
    steps = [{"id": s, "name": n, "done": marker(s).exists()} for s, n, _ in EXEC_STEPS]
    ready = all(x["done"] for x in steps) and p.exec_py.exists() and p.exec_model.exists()
    return {"ready": ready, "steps": steps, "dir": str(p.exec), "shared": p.shared_exec}


def install_executor(ctx):
    p = paths()
    if not engine_status()["ready"]:
        raise EngineError("Сначала установите основной движок (вкладка «Движок»).")
    p.exec.mkdir(parents=True, exist_ok=True)
    ensure_workers()
    acc = 0.0
    for sid, name, w in EXEC_STEPS:
        ctx.span(acc, acc + w, name + "…")
        acc += w
        if marker(sid).exists():
            continue
        globals()["_step_" + sid](ctx, p)
        marker(sid).write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    ctx.span(1.0, 1.0, "Executor установлен.")
    return executor_status()


def exec_env():
    p = paths()
    return child_env({"NLTK_DATA": str(p.exec_nltk), "HF_HOME": str(p.exec / "hf")})


def _step_exec_code(ctx, p):
    if (p.exec_src / "cli" / "inference.py").exists():
        return
    z = download(EXEC_ZIP, p.engine / "dl" / "soulx.zip", ctx, "SoulX-Singer")
    tmp = p.engine / "dl" / "soulx_x"
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(z) as zf:
        zf.extractall(tmp)
    top = next(d for d in tmp.iterdir() if d.is_dir())
    shutil.rmtree(p.exec_src, ignore_errors=True)
    shutil.move(str(top), str(p.exec_src))
    shutil.rmtree(tmp, ignore_errors=True)
    if not (p.exec_src / "cli" / "inference.py").exists():
        raise EngineError("SoulX-Singer распакован не полностью.")


def _step_exec_env(ctx, p):
    _venv(ctx, p.exec_env)
    idx = ["--extra-index-url", "https://download.pytorch.org/whl/cpu" if os.environ.get("DZOD_CPU_TORCH") else EXEC_TORCH_INDEX,
           "--index-strategy", "unsafe-best-match"]
    _uv(ctx, ["pip", "install", "-p", p.exec_py, *EXEC_PACKAGES, *idx], "Установка библиотек пения")
    p.exec_nltk.mkdir(parents=True, exist_ok=True)
    code = ("import nltk,sys\n"
            "ok=all(nltk.download(x, download_dir=sys.argv[1], quiet=True) for x in ('cmudict','averaged_perceptron_tagger_eng'))\n"
            "sys.exit(0 if ok else 3)\n")
    ctx.run([p.exec_py, "-c", code, p.exec_nltk], env=exec_env(), what="Словарь произношения (CMUdict)")


def _step_exec_model(ctx, p):
    p.exec_model.parent.mkdir(parents=True, exist_ok=True)
    code = ("import sys,shutil\nfrom huggingface_hub import hf_hub_download\n"
            "f=hf_hub_download(sys.argv[1],'model.pt',local_dir=sys.argv[2])\nprint('@@R {}')\n")
    ctx.progress(0.05, "Скачиваю модель пения (около 3 ГБ)…")
    ctx.run([p.exec_py, "-c", code, EXEC_HF_REPO, p.exec_model.parent], env=exec_env(), what="Загрузка модели пения")
    if not p.exec_model.exists():
        raise EngineError("Модель пения не скачалась (нет model.pt).")


def _step_exec_check(ctx, p):
    # встроенный английский образец голоса SoulX → WAV (mp3 под Windows читается не везде)
    p.exec_prompt_wav.parent.mkdir(parents=True, exist_ok=True)
    src = p.exec_src / "example" / "audio"
    code = ("import sys,soundfile as sf,shutil\n"
            "x,sr=sf.read(sys.argv[1]); sf.write(sys.argv[2],x,sr); shutil.copyfile(sys.argv[3],sys.argv[4])\n"
            "import torch,json; print('@@R '+json.dumps({'cuda':torch.cuda.is_available()}))\n")
    ctx.run([p.exec_py, "-c", code, src / "en_prompt.mp3", p.exec_prompt_wav, src / "en_prompt.json", p.exec_prompt_meta],
            env=exec_env(), what="Проверка Executor")


# Нейросети чистки: (файл, для чего, при каком выборе). Список «Максимум» — из ансамблей audio-separator 0.47.
SEP_MODELS = [
    ("mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt", "Отделение бэк-вокала", "Быстро и Максимум"),
    ("UVR-DeEcho-DeReverb.pth", "Реверберация: «Без ревера — как в UVR»", ""),
    ("dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt", "Реверберация: «Roformer»", ""),
    ("denoise_mel_band_roformer_aufr33_sdr_27.9959.ckpt", "Шум и «стекло»", ""),
    ("mel_band_roformer_karaoke_gabox_v2.ckpt", "Отделение бэк-вокала", "Максимум"),
    ("mel_band_roformer_karaoke_becruily.ckpt", "Отделение бэк-вокала", "Максимум"),
    ("model_bs_roformer_ep_317_sdr_12.9755.ckpt", "Вокал из полного трека", "Быстро"),
    ("melband_roformer_big_beta6x.ckpt", "Вокал из полного трека", "Максимум"),
    ("mel_band_roformer_vocals_fv4_gabox.ckpt", "Вокал из полного трека", "Максимум"),
]


def sep_models_status():
    d = paths().sep_models
    out = []
    for f, what, when in SEP_MODELS:
        p = d / f
        ok = p.exists() and p.stat().st_size > 1_000_000
        out.append({"file": f, "what": what, "when": when, "ok": ok,
                    "mb": round(p.stat().st_size / 1e6) if ok else None})
    return out


def download_sep_models(ctx):
    p = paths()
    if not p.sep_py.exists():
        raise EngineError("Сначала установите движок.")
    names = [m["file"] for m in sep_models_status() if not m["ok"]]
    if not names:
        return {"models": sep_models_status()}
    workers = ensure_workers()
    tmp = new_tmp("dl")
    job = {"mode": "download", "models": names, "models_dir": str(p.sep_models)}
    (tmp / "job.json").write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    p.sep_models.mkdir(parents=True, exist_ok=True)
    ctx.run([p.sep_py, workers / "sep_worker.py", tmp / "job.json"], cwd=tmp, what="Скачивание моделей")
    shutil.rmtree(tmp, ignore_errors=True)
    return {"models": sep_models_status()}


def folder_sizes():
    """Сколько места занимают части папки программы (для вкладки «Движок»)."""
    out = {}
    for name, d in (("engine", root() / "Engine"), ("voices", root() / "Voices"), ("songs", root() / "Songs")):
        total = 0
        if d.is_dir():
            for dp, _, fs in os.walk(d):
                for f in fs:
                    try:
                        total += os.path.getsize(os.path.join(dp, f))
                    except OSError:
                        pass
        out[name] = {"path": str(d), "gb": round(total / 1e9, 2), "exists": d.is_dir()}
    return out


def gpu_index():
    st = engine_status().get("gpu") or {}
    return "0" if st.get("cuda") else "-"
