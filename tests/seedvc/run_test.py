# -*- coding: utf-8 -*-
"""Проверка Seed-VC (0.8.1) и сравнение с RVC по мерилу (сервер, Linux, процессор).
Исходник — пение SoulX по тексту (w/out/clean.wav), RVC-дубль — w/out/take_clean.wav (предобученный голос),
Seed-VC — тот же исходник, образец голоса — en_prompt.wav (голос-образец SoulX).
    python tests/seedvc/run_test.py <рабочая папка> <папка кода Seed-VC> <папка whisper для мерила>"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

W, SRC, WH = (Path(a).resolve() for a in sys.argv[1:4])
REPO = Path(__file__).resolve().parents[2]
WORK = REPO / "app" / "workers"
OUT = W / "out"
B = OUT / "bench"
B.mkdir(exist_ok=True)
EXEC_PY = str(W / "venv_exec" / "bin" / "python")
LYR = "I wish you were here tonight\nwe are the beautiful no"
RES = {}


def run(script, job, tag, cwd=None):
    jf = B / f"{tag}.json"
    jf.write_text(json.dumps(job), encoding="utf-8")
    r = subprocess.run([EXEC_PY, str(WORK / script), str(jf)], capture_output=True, text=True, cwd=cwd)
    res = [ln[4:] for ln in r.stdout.splitlines() if ln.startswith("@@R ")]
    if r.returncode != 0 or not res:
        print(r.stdout[-4000:], r.stderr[-4000:])
        raise SystemExit(f"упало: {tag}")
    return json.loads(res[-1])


def quality(take, tag):
    out = B / f"q_{tag}.json"
    run("quality_worker.py", {"source": str(OUT / "clean.wav"), "take": str(take), "lyrics": LYR, "language": "en",
                              "out_json": str(out), "whisper": str(WH), "align_model_dir": str(W / "exec" / "align"),
                              "device": "cpu"}, f"qjob_{tag}")
    return json.loads(out.read_text(encoding="utf-8"))


ref = W / "en_prompt.wav"
for steps in (10, 25):
    o = B / f"seedvc_{steps}.wav"
    r = run("seedvc_worker.py", {"src_dir": str(SRC), "device": "cpu", "fp16": False,
                                 "items": [{"input": str(OUT / "clean.wav"), "output": str(o), "reference": str(ref),
                                            "pitch": 0, "steps": steps, "cfg_rate": 0.7, "label": f"{steps} шагов"}]},
            f"svc_{steps}", cwd=str(SRC))
    RES[f"seedvc_{steps}_run"] = r
x, xsr = sf.read(OUT / "clean.wav")
y, ysr = sf.read(B / "seedvc_25.wav")
RES["length_ok"] = abs(len(x) / xsr - len(y) / ysr) < 0.05
RES["not_silent"] = float(np.sqrt(np.mean(y ** 2))) > 1e-3

rows = {}
for tag, f in (("source", OUT / "clean.wav"), ("RVC", OUT / "take_clean.wav"), ("Seed-VC 10", B / "seedvc_10.wav"),
               ("Seed-VC 25", B / "seedvc_25.wav")):
    q = quality(f, tag.replace(" ", "_"))
    c = q["checks"]
    rows[tag] = {"WER": c["wer"]["take"], "яркость": c["brightness"]["median_ratio"],
                 "огибающая_мед": c["dynamics"].get("median_db"), "огибающая_p90": c["dynamics"].get("p90_db"),
                 "щелчки": c["clicks"]["count"], "всплески": len(q["bursts"]), "металл": q["metal_ratio"],
                 "слова": [(p["word"], p["heard"]) for p in q["problems"]], "распознано": q["asr_take"]}
RES["table"] = rows
print(json.dumps(RES, ensure_ascii=False, indent=1))
(B / "result.json").write_text(json.dumps(RES, ensure_ascii=False, indent=1), encoding="utf-8")
ok = RES["length_ok"] and RES["not_silent"]
print("ИТОГ SEED-VC:", "работает" if ok else "есть ошибки")
sys.exit(0 if ok else 1)
