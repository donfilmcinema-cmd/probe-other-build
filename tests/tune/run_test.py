# -*- coding: utf-8 -*-
"""Проверка 0.9.2 «Подобрать настройки по тексту» (сервер, Linux, процессор). Исходник — пение SoulX
(w/out/clean.wav), «сырой» — w/out/dirty.wav (если есть), модель — test_voice.pth тремя «эпохами» (копии).
Настоящий app/tune.py: отрывок, перепевка сеткой, оценка распознаванием, яркость, динамика.
Ждём: 3 эпохи × 2 тембра × 3 защиты = 18 вариантов, у каждого WER и файл; есть лучший; подбор.json записан.
    python tests/tune/run_test.py <рабочая папка> <whisper>"""
import json
import os
import shutil
import sys
import types
from pathlib import Path

W, WH = (Path(a).resolve() for a in sys.argv[1:3])
REPO = Path(__file__).resolve().parents[2]
APP = REPO / "app"
OUT = W / "out"
TU_OUT = OUT / "tune"
TU_OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(APP))
LYR = "I wish you were here tonight\nwe are the beautiful no"

import engine as E      # noqa: E402
import quality as Q     # noqa: E402
import voices as V      # noqa: E402
import tune as TU       # noqa: E402

E.log = lambda *a, **k: print("   [журнал]", *a)
E.executor_status = lambda: {"ready": True}
E.ensure_workers = lambda: APP / "workers"
env = dict(os.environ, NLTK_DATA=str(W / "nltk_data"))
E.exec_env = lambda: env
E.paths = lambda: types.SimpleNamespace(exec_py=str(W / "venv_exec" / "bin" / "python"),
                                        applio_py=str(W / "venv_applio" / "bin" / "python"), applio=W / "applio",
                                        exec=W / "exec", uv_cache=W / "uvc", python_dir=W / "uvp")
_n = [0]


def _tmp(tag):
    _n[0] += 1
    p = TU_OUT / f"tmp_{tag}_{_n[0]}"
    p.mkdir(parents=True, exist_ok=True)
    return p


E.new_tmp = _tmp
Q.whisper_dir = lambda: WH
Q.whisper_ready = lambda: True
os.environ["DZOD_QUALITY_DEVICE"] = "cpu"
cks = []
for e in (200, 250, 300):
    f = TU_OUT / f"test_{e}e_{e * 10}s.pth"
    shutil.copyfile(W / "test_voice.pth", f)
    cks.append({"file": f.name, "epoch": e, "step": e * 10, "mb": 1, "path": str(f)})
V.checkpoints = lambda slug: cks
V.model_files = lambda slug: (W / "test_voice.pth", None)
ctx = E.Ctx()
base = {"pitch": 0, "index_rate": 0.6, "protect": 0.15, "volume_envelope": 0.65, "f0_method": "rmvpe",
        "f0_autotune": False, "f0_autotune_strength": 0.4, "clean_audio": False, "clean_strength": 0.4,
        "split_audio": False, "proposed_pitch": False, "match_level": True, "engine": "rvc"}
raw = OUT / "dirty.wav"
r = TU.tune(ctx, OUT / "clean.wav", "test", base, LYR, TU_OUT / "подбор", raw_source=raw if raw.exists() else None,
            language="en")
for x in r["rows"]:
    print(f"   {x['epoch_label']:>10} тембр {x['index_rate']} защита {x['protect']}: WER {x['wer']} яркость {x['bright']}"
          f" дин. {x['env_db']} {'✓' if x['in_norm'] else '✗'} «{x['heard']}»")
RES = {"window": r["window"], "words": r["words"], "wer_source": r["wer_source"], "wer_raw": r["wer_raw"],
       "rows": len(r["rows"]), "with_wer": sum(1 for x in r["rows"] if x["wer"] is not None),
       "files": sum(1 for x in r["rows"] if Path(x["file"]).exists()), "best": r["best"],
       "json": (TU_OUT / "подбор" / "подбор.json").exists(), "seconds": r["seconds"]}
ok = RES["rows"] == 18 and RES["with_wer"] == 18 and RES["files"] == 18 and RES["best"] and RES["json"] \
    and RES["words"] > 0
RES["ok"] = bool(ok)
print(json.dumps(RES, ensure_ascii=False, indent=1))
(TU_OUT / "result.json").write_text(json.dumps(RES, ensure_ascii=False, indent=1), encoding="utf-8")
print("ИТОГ ПОДБОРА:", "всё в порядке" if ok else "есть ошибки")
sys.exit(0 if ok else 1)
