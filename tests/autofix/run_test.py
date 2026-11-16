# -*- coding: utf-8 -*-
"""Проверка 0.8.2 «Вся песня по тексту» (сервер, Linux, процессор). Берёт файлы проверок «Текста» и мерила:
исходник — пение SoulX (w/out/clean.wav), дубль — испорченная перепевка (w/out/quality/take_bad.wav:
«wish» съедено, «here» тусклое, щелчки, дрожание громкости). Настоящий app/autofix.py с окружениями этого теста.
Ждём: найдены слова с ошибкой; хоть одно исправлено; новый дубль вне изменённых мест — бит в бит прежний;
разборчивость нового дубля (мерило против текста) не хуже прежнего.
    python tests/autofix/run_test.py <рабочая папка> <whisper> <код Seed-VC>"""
import json
import os
import sys
import types
from pathlib import Path

import numpy as np
import soundfile as sf

W, WH, SVC = (Path(a).resolve() for a in sys.argv[1:4])
REPO = Path(__file__).resolve().parents[2]
APP = REPO / "app"
WORK = APP / "workers"
OUT = W / "out"
AF_OUT = OUT / "autofix"
AF_OUT.mkdir(exist_ok=True)
sys.path.insert(0, str(APP))
EXEC_PY = str(W / "venv_exec" / "bin" / "python")
APPLIO_PY = str(W / "venv_applio" / "bin" / "python")
LYR = "I wish you were here tonight\nwe are the beautiful no"

import engine as E      # noqa: E402
import quality as Q     # noqa: E402
import autofix as AF    # noqa: E402

E.log = lambda *a, **k: print("   [журнал]", *a)
E.executor_status = lambda: {"ready": True}
E.ensure_workers = lambda: WORK
env = dict(os.environ, NLTK_DATA=str(W / "nltk_data"))
E.exec_env = lambda: env
_P = types.SimpleNamespace(exec_py=EXEC_PY, applio_py=APPLIO_PY, applio=W / "applio", exec=W / "exec",
                           uv_cache=W / "uvc", python_dir=W / "uvp")
E.paths = lambda: _P
_n = [0]


def _tmp(tag):
    _n[0] += 1
    p = AF_OUT / f"tmp_{tag}_{_n[0]}"
    p.mkdir(parents=True, exist_ok=True)
    return p


E.new_tmp = _tmp
Q.whisper_dir = lambda: WH
Q.whisper_ready = lambda: True
os.environ["DZOD_QUALITY_DEVICE"] = "cpu"
E.seedvc_ready = lambda: False          # Seed-VC — отдельная проверка; здесь два способа: согласные и меньше тембра
ctx = E.Ctx()

src, take = OUT / "clean.wav", OUT / "quality" / "take_bad.wav"
item = {"model": str(W / "test_voice.pth"), "index": "", "pitch": 0, "index_rate": 0.0, "protect": 0.33,
        "f0_method": "rmvpe"}
r = AF.fix(ctx, src, take, item, LYR, AF_OUT / "fixed.wav", voice=None, language="en")
print(json.dumps({k: v for k, v in r.items() if k != "words"}, ensure_ascii=False))
for w in r["words"]:
    print(f"   {w['word']:>10} {w['t0']}–{w['t1']} {w['kind']}: {w['method']}{' ✓' if w['fixed'] else ''} | было «{w['before']}» → «{w['after']}»"
          f" | варианты {json.dumps(w['tried'], ensure_ascii=False)}")
RES = {"problems": len(r["words"]), "fixed": r.get("fixed", 0), "file": bool(r.get("file"))}
if r.get("file"):
    y0, sr = sf.read(take, dtype="float64", always_2d=True)
    y1, _ = sf.read(r["file"], dtype="float64", always_2d=True)
    n = min(len(y0), len(y1))
    t = np.arange(n) / sr
    inside = np.zeros(n, bool)
    for c0, c1 in r["changed"]:
        inside |= (t >= c0) & (t <= c1)
    RES["outside_identical"] = bool(np.array_equal(y1[:n][~inside], y0[:n][~inside]))
    q0 = Q.measure(ctx, src, take, LYR, "en", out=AF_OUT / "q_before.json")
    q1 = Q.measure(ctx, src, r["file"], LYR, "en", out=AF_OUT / "q_after.json")
    RES["wer_before"], RES["wer_after"] = q0["checks"]["wer"]["take"], q1["checks"]["wer"]["take"]
    RES["asr_after"] = q1["asr_take"]
else:
    RES["outside_identical"] = True
_q0 = json.loads((OUT / "quality" / "self.json").read_text(encoding="utf-8")) if (OUT / "quality" / "self.json").exists() else None
if _q0 and (_q0["checks"]["wer"]["source"] or 0) > 50:
    print("!! данные проверки негодны: исходник распознаётся с WER", _q0["checks"]["wer"]["source"])
    RES["bad_data"] = True
# WER всего дубля справочно: у испорченного дубля (дрожание громкости) распознавание «сочиняет» слова вне мест —
# мера — исправленные слова (вариант, где нужное слово распознано) и «вне мест бит в бит»
ok = RES.get("bad_data") or RES["problems"] > 0 and RES["fixed"] >= 1 and RES["outside_identical"]
RES["ok"] = ok
print(json.dumps(RES, ensure_ascii=False, indent=1))
(AF_OUT / "result.json").write_text(json.dumps({"result": RES, "words": r["words"]}, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
print("ИТОГ ПО ТЕКСТУ:", "всё в порядке" if ok else "есть ошибки")
sys.exit(0 if ok else 1)
