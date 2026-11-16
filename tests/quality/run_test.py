# -*- coding: utf-8 -*-
"""Проверка мерила 0.8.0 (сервер, Linux, процессор). Берёт файлы проверки «Текста» (w/out): пение SoulX по тексту
и его перепевку, портит перепевку известным образом и смотрит, что мерило это видит:
  0) исходник против самого себя — всё зелёное (WER как у исходника, яркость 100 %, огибающая 0, щелчков 0);
  1) честный дубль — отчёт есть, цифры в разумных пределах;
  2) испорченный дубль: слово «wish» съедено (тихий шум), «here» — тусклое (фильтр 1,5 кГц), 3 щелчка,
     дрожание громкости ±4 дБ кадрами 40 мс — мерило должно это показать.
    python tests/quality/run_test.py <рабочая папка> <папка модели whisper>"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfiltfilt

W = Path(sys.argv[1]).resolve()
WH = Path(sys.argv[2]).resolve()
REPO = Path(__file__).resolve().parents[2]
WORK = REPO / "app" / "workers"
OUT = W / "out"
Q = OUT / "quality"
Q.mkdir(exist_ok=True)
EXEC_PY = str(W / "venv_exec" / "bin" / "python")
LYR = "I wish you were here tonight\nwe are the beautiful no"
RES = {}


def measure(src, take, tag, lyrics=LYR):
    out = Q / f"{tag}.json"
    job = {"source": str(src), "take": str(take), "lyrics": lyrics, "language": "en", "out_json": str(out),
           "whisper": str(WH), "align_model_dir": str(W / "exec" / "align"), "device": "cpu", "version": "test"}
    jf = Q / f"{tag}_job.json"
    jf.write_text(json.dumps(job), encoding="utf-8")
    r = subprocess.run([EXEC_PY, str(WORK / "quality_worker.py"), str(jf)], capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        print(r.stdout[-3000:], r.stderr[-3000:])
        raise SystemExit(f"мерило упало: {tag}")
    rep = json.loads(out.read_text(encoding="utf-8"))
    c = rep["checks"]
    print(f"== {tag}: WER {c['wer']['take']} (исходник {c['wer']['source']}), яркость {c['brightness']['median_ratio']}, "
          f"огибающая {c['dynamics'].get('median_db')}/{c['dynamics'].get('p90_db')} дБ, щелчков {c['clicks']['count']}, "
          f"всплесков {len(rep['bursts'])}, слова {[(p['word'], p['kind'], p['heard']) for p in rep['problems']]}, "
          f"{rep['time_s']} с\n   исходник: {rep['asr_source']}\n   дубль:    {rep['asr_take']}")
    return rep


def word_t(rep, w):
    for b in rep["brightness_words"]:
        if b["word"] == w:
            return b
    return None


src = OUT / "clean.wav"
take = OUT / "take_clean.wav"
# 0) сам с собой
r0 = measure(src, src, "self")
ok0 = (r0["checks"]["wer"]["take"] == r0["checks"]["wer"]["source"] and r0["checks"]["brightness"]["median_ratio"] in (1.0, None)
       and (r0["checks"]["dynamics"].get("median_db") or 0) < 0.05 and r0["checks"]["clicks"]["count"] == 0
       and not r0["problems"])
RES["self"] = r0["checks"]

# 1) честный дубль
r1 = measure(src, take, "take")
RES["take"] = r1["checks"]
ok1 = r1["checks"]["wer"]["take"] is not None and r1["checks"]["dynamics"].get("median_db") is not None

# 2) испорченный дубль
y, sr = sf.read(take, dtype="float64", always_2d=True)
y = y.mean(1)
wish, here = word_t(r1, "wish"), word_t(r1, "here")
rng = np.random.default_rng(1)
z = y.copy()
if wish:
    a, b = int(wish["t0"] * sr), int(wish["t1"] * sr)
    z[a:b] = rng.standard_normal(b - a) * 10 ** (-45 / 20)
if here:
    a, b = int(here["t0"] * sr), int(here["t1"] * sr)
    sos = butter(6, 1500, fs=sr, output="sos")
    z[a:b] = sosfiltfilt(sos, z[a:b])
for tc in (2.5, 4.0, 6.0):
    k = int(tc * sr)
    if k < len(z) - 10:
        z[k:k + 3] += np.array([0.6, -0.6, 0.4])
fr = int(0.04 * sr)
g = 10 ** (rng.uniform(-4, 4, len(z) // fr + 1) / 20)
z = z * np.repeat(g, fr)[:len(z)]
bad = Q / "take_bad.wav"
sf.write(bad, z, sr, subtype="FLOAT")
r2 = measure(src, bad, "bad")
RES["bad"] = r2["checks"]
RES["bad_problems"] = r2["problems"]
RES["here_words"] = {"take": hb1 if 'hb1' in dir() else None}
hb1, hb2 = word_t(r1, "here"), word_t(r2, "here")
RES["here_words"] = {"take": hb1, "bad": hb2}
checks2 = {
    # «wish» съедено: в списке слов дубля (или «и в исходнике», если распознавание не взяло его и там)
    "wish_found_as_problem": any(p["word"] == "wish" for p in r2["problems"] + r2["problems_also_in_source"]) or (
        (r2["checks"]["wer"]["take"] or 0) > (r1["checks"]["wer"]["take"] or 0)),
    # «here» после фильтра 1,5 кГц: доля >2 кГц в дубле падает (сравнение долей, а не отношений — у тёмного слова
    # исходника отношение не считается)
    "here_dull": bool(hb1 and hb2 and hb1["take_hf2k"] > 0 and hb2["take_hf2k"] < 0.6 * hb1["take_hf2k"]),
    "clicks_seen": r2["checks"]["clicks"]["count"] >= 2 and r2["checks"]["clicks"]["count"] > r1["checks"]["clicks"]["count"],
    "dynamics_worse": (r2["checks"]["dynamics"]["median_db"] or 0) > (r1["checks"]["dynamics"]["median_db"] or 0) + 0.5
                      and (r2["checks"]["dynamics"]["p90_db"] or 0) > (r1["checks"]["dynamics"]["p90_db"] or 0) + 1.5,
    "not_passed": not r2["passed"],
}
RES["bad_checks"] = checks2
# 3) текст только на часть песни (как T022 у автора: 4 строки из 8) — сверка лишь по участку текста
r3 = measure(src, take, "partial", lyrics="I wish you were here tonight")
RES["partial"] = r3["checks"]["wer"]
ok3 = (r3["checks"]["wer"]["covered"] is not None and r3["checks"]["wer"]["covered"][1] < r1["seconds"] - 1
       and not r3["checks"]["wer"]["suspect"])
# синтетика SoulX иногда поёт неразборчиво: если распознавание не берёт и сам исходник, проверки, которые на нём
# держатся (участок текста), — «негодные данные», а не ошибка программы
bad_data = (r0["checks"]["wer"]["source"] or 0) > 50
RES["bad_data"] = bad_data
if bad_data:
    print("!! данные проверки негодны: исходник распознаётся с WER", r0["checks"]["wer"]["source"], "— участок текста не проверяется")
    ok3 = True
RES["ok"] = {"self": ok0, "take": ok1, "partial_lyrics": ok3, **checks2}
print(json.dumps(RES, ensure_ascii=False, indent=1))
(Q / "result.json").write_text(json.dumps(RES, ensure_ascii=False, indent=1), encoding="utf-8")
ok = all(RES["ok"].values())
print("ИТОГ МЕРИЛА:", "всё в порядке" if ok else "есть ошибки")
sys.exit(0 if ok else 1)
