# -*- coding: utf-8 -*-
"""Проверка «Перепевки с текстом» на синтетике (Linux, процессор). Запускает настоящий app/textfix.py.

    python run_test.py <папка с окружениями и моделями>

Сюжет: пение SoulX по tests/executor/two_phrases.mid → «съедаем» слово «wish» (заменяем тихим шумом) →
обычная перепевка грязного и чистого → перепевка с текстом (пометка на «wish») → сравнение.
Проверяется:
  1) вне помеченного места дубль не изменился ни на бит;
  2) в помеченном месте результат ближе к перепевке чистого исходника, чем грязный дубль (MFCC);
  3) уровень в помеченном месте — как у чистого (съеденное место больше не провал);
  4) выравнивание снова находит слово «wish» с уверенностью не ниже, чем в грязном дубле.
"""
import json
import os
import subprocess
import sys
import types
from pathlib import Path

W = Path(sys.argv[1]).resolve()
REPO = Path(__file__).resolve().parents[2]
APP = REPO / "app"
WORK = REPO / "app" / "workers"
OUT = W / "out"
OUT.mkdir(exist_ok=True)
(OUT / "segs").mkdir(exist_ok=True)
os.environ["NLTK_DATA"] = str(W / "nltk_data")
os.environ["DZOD_TEXTFIX_KEEP"] = "1"
sys.path.insert(0, str(APP))

import numpy as np            # noqa: E402
import soundfile as sf        # noqa: E402

EXEC_PY = str(W / "venv_exec" / "bin" / "python")
APPLIO_PY = str(W / "venv_applio" / "bin" / "python")
LYR = "I wish you were here tonight\nwe are the beautiful no"
RES = {}


def run(py, script, job, cwd=None):
    jf = OUT / f"job_{Path(script).stem}_{len(list(OUT.glob('job_*')))}.json"
    jf.write_text(json.dumps(job, ensure_ascii=False))
    p = subprocess.run([py, str(script), str(jf)], cwd=cwd, capture_output=True, text=True)
    for ln in p.stdout.splitlines():
        if not ln.startswith("@@H") and not ln.startswith("@@P"):
            print("   ", ln[:300])
    if p.returncode:
        print(p.stderr[-3000:])
        raise SystemExit(f"сбой: {script}")
    r = [ln for ln in p.stdout.splitlines() if ln.startswith("@@R ")]
    return json.loads(r[-1][4:]) if r else {}


# 1. пение (Straight)
print("== пение SoulX")
run(EXEC_PY, WORK / "executor_worker.py",
    {"midi": str(REPO / "tests/executor/two_phrases.mid"), "src_dir": str(W / "soulx"), "model": str(W / "models/model.pt"),
     "prompt_wav": str(W / "en_prompt.wav"), "prompt_meta": str(W / "soulx/example/audio/en_prompt.json"),
     "out_wav": str(OUT / "clean.wav"), "meta_out": str(OUT / "meta.json"), "notes_out": str(OUT / "notes.json"),
     "seg_dir": str(OUT / "segs"), "cache_dir": str(OUT / "cache"), "fp16": False, "device": "cpu",
     "style": "straight", "vibrato": 30}, cwd=str(W / "soulx"))
x, sr = sf.read(OUT / "clean.wav", dtype="float64")
x = x if x.ndim == 1 else x.mean(1)
notes = [n for n in json.load(open(OUT / "notes.json")) if n["pitch"] > 0]
wish = [n for n in notes if n["word"].lower().strip("[]") in ("wish", "w_ih1_sh")] or [notes[1]]
ws, we = wish[0]["start"], wish[-1]["end"]
print(f"   «wish» по нотам: {ws:.2f}–{we:.2f} с")

# 2. «съесть» слово: середина слова — тихий шум (-45 дБ), края 20 мс
d = x.copy()
a, b = int((ws + 0.02) * sr), int((we - 0.02) * sr)
rng = np.random.default_rng(1)
d[a:b] = rng.standard_normal(b - a) * 10 ** (-45 / 20)
sf.write(OUT / "dirty.wav", d.astype(np.float32), sr, subtype="FLOAT")

# 3. обычная перепевка грязного и чистого — той же моделью
item = {"model": str(W / "test_voice.pth"), "index": "", "pitch": 0, "index_rate": 0.0, "protect": 0.33, "f0_method": "rmvpe"}
print("== обычная перепевка")
run(APPLIO_PY, WORK / "applio_worker.py",
    {"items": [dict(item, input=str(OUT / "dirty.wav"), output=str(OUT / "take_dirty.wav")),
               dict(item, input=str(OUT / "clean.wav"), output=str(OUT / "take_clean.wav"))]}, cwd=str(W / "applio"))

# 4. перепевка с текстом — настоящий app/textfix.py; движок — окружения этого теста
import engine as E   # noqa: E402
import textfix as TF  # noqa: E402

E.log = lambda *a, **k: print("   [журнал]", *a)
E.executor_status = lambda: {"ready": True}
E.ensure_workers = lambda: WORK
env = dict(os.environ, NLTK_DATA=str(W / "nltk_data"))
E.exec_env = lambda: env
E.paths = lambda: types.SimpleNamespace(
    exec_py=EXEC_PY, applio_py=APPLIO_PY, applio=W / "applio", exec=W / "exec", exec_src=W / "soulx",
    exec_model=W / "models/model.pt", exec_prompt_wav=W / "en_prompt.wav",
    exec_prompt_meta=W / "soulx/example/audio/en_prompt.json", uv_cache=W / "uvc", python_dir=W / "uvp")
(W / "exec").mkdir(exist_ok=True)
_n = [0]


def _tmp(tag):
    _n[0] += 1
    p = OUT / f"tmp_{tag}_{_n[0]}"
    p.mkdir(parents=True, exist_ok=True)
    return p


E.new_tmp = _tmp
E.piper_ready = lambda: True
_P = E.paths()
_P.exec_piper_voice = W / "piper" / "ru_RU-irina-medium.onnx"
E.paths = lambda: _P
ctx = E.Ctx()
k0 = LYR.index("wish")
print("== перепевка с текстом (пометка «wish»)")
r = TF.revoice_text(ctx, OUT / "dirty.wav", OUT / "take_dirty.wav", item, LYR, [{"from": k0, "to": k0 + 4}],
                    OUT / "fixed.wav")
print("   план:", json.dumps(r["plan"], ensure_ascii=False)[:400])
print("   изменено:", r["changed"], f"за {r['seconds']} с")
RES["changed"] = r["changed"]

# 5. сравнение
yd, ysr = sf.read(OUT / "take_dirty.wav", dtype="float64", always_2d=True)
yc, _ = sf.read(OUT / "take_clean.wav", dtype="float64", always_2d=True)
yf, _ = sf.read(OUT / "fixed.wav", dtype="float64", always_2d=True)
n = min(len(yd), len(yc), len(yf))
yd, yc, yf = yd[:n, 0], yc[:n, 0], yf[:n, 0]
t = np.arange(n) / ysr
inside = np.zeros(n, bool)
for c0, c1 in r["changed"]:
    inside |= (t >= c0) & (t <= c1)
RES["outside_identical"] = bool(np.array_equal(yf[~inside], yd[~inside]))

import librosa  # noqa: E402
sa, sb = int(ws * ysr), int(we * ysr)


def mf(v):
    return librosa.feature.mfcc(y=v.astype(np.float32), sr=ysr, n_mfcc=20)[1:]


def dist(u, v):
    A_, B_ = mf(u[sa:sb]), mf(v[sa:sb])
    m = min(A_.shape[1], B_.shape[1])
    return float(np.mean(np.linalg.norm(A_[:, :m] - B_[:, :m], axis=0)))


def lvl(v):
    return float(20 * np.log10(np.sqrt(np.mean(v[sa:sb] ** 2)) + 1e-9))


RES["mfcc_dirty_vs_clean"] = round(dist(yd, yc), 2)
RES["mfcc_fixed_vs_clean"] = round(dist(yf, yc), 2)
RES["level_dB"] = {"clean": round(lvl(yc), 1), "dirty": round(lvl(yd), 1), "fixed": round(lvl(yf), 1)}

# выравнивание «wish» на дублях
for name, f in (("dirty", OUT / "take_dirty.wav"), ("fixed", OUT / "fixed.wav"), ("clean", OUT / "take_clean.wav")):
    pj = run(EXEC_PY, WORK / "textfix_worker.py",
             {"step": "prepare", "input": str(f), "lyrics": LYR, "marks": [], "language": "en",
              "hint_midi": str(OUT / f"_h_{name}.mid"), "plan_out": str(OUT / f"_plan_{name}.json"),
              "rmvpe": str(W / "applio/rvc/models/predictors/rmvpe.pt"),
              "rmvpe_py": str(W / "applio/rvc/lib/predictors/RMVPE.py"), "align_model_dir": str(W / "exec" / "align")})
    wd = json.load(open(OUT / f"_plan_{name}.json"))["words"][1]
    RES[f"wish_{name}"] = {"t0": wd["t0"], "t1": wd["t1"], "score": wd["score"], "suspect": wd["suspect"]}

okA = (RES["outside_identical"] and RES["mfcc_fixed_vs_clean"] < RES["mfcc_dirty_vs_clean"]
       and abs(RES["level_dB"]["fixed"] - RES["level_dB"]["clean"]) < abs(RES["level_dB"]["dirty"] - RES["level_dB"]["clean"]))


def word_at(f, lyrics, wi, lang, tag):
    run(EXEC_PY, WORK / "textfix_worker.py",
        {"step": "prepare", "input": str(f), "lyrics": lyrics, "marks": [], "language": lang,
         "hint_midi": str(OUT / f"_h_{tag}.mid"), "plan_out": str(OUT / f"_plan_{tag}.json"),
         "rmvpe": str(W / "applio/rvc/models/predictors/rmvpe.pt"),
         "rmvpe_py": str(W / "applio/rvc/lib/predictors/RMVPE.py"), "align_model_dir": str(W / "exec" / "align")})
    wd = json.load(open(OUT / f"_plan_{tag}.json"))["words"][wi]
    return {"t0": wd["t0"], "t1": wd["t1"], "score": wd["score"], "suspect": wd["suspect"]}


def outside_same(fa, fb, changed):
    a_, asr = sf.read(fa, dtype="float64", always_2d=True)
    b_, _ = sf.read(fb, dtype="float64", always_2d=True)
    m = min(len(a_), len(b_))
    tt = np.arange(m) / asr
    ins = np.zeros(m, bool)
    for c0, c1 in changed:
        ins |= (tt >= c0) & (tt <= c1)
    return bool(np.array_equal(a_[:m][~ins], b_[:m][~ins]))


# ---- Б. замена слова с другим числом слогов: «wish» → «never» (чистый исходник)
print("== замена слова: «wish» → «never»")
rB = TF.revoice_text(ctx, OUT / "clean.wav", OUT / "take_clean.wav", item, LYR,
                     [{"from": k0, "to": k0 + 4, "replace": "never"}], OUT / "replaced.wav")
LYR_B = LYR.replace("wish", "never", 1)
RES["B_outside_identical"] = outside_same(OUT / "replaced.wav", OUT / "take_clean.wav", rB["changed"])
RES["B_never_on_take"] = word_at(OUT / "take_clean.wav", LYR_B, 1, "en", "B_take")
RES["B_never_on_replaced"] = word_at(OUT / "replaced.wav", LYR_B, 1, "en", "B_rep")
okB = RES["B_outside_identical"] and (RES["B_never_on_replaced"]["score"] or 0) > (RES["B_never_on_take"]["score"] or 0)

# ---- В. русский: исходник — речь Piper (голос dmitri), подсказка — Piper (irina); «съедено» слово «чудное»
okC = True
if (W / "piper" / "ru_RU-dmitri-medium.onnx").exists():
    print("== русский: «чудное»")
    LYR_RU = "Я помню чудное мгновенье\nпередо мной явилась ты"
    run(EXEC_PY, REPO / "tests/textfix/piper_say.py",
        {"voice": str(W / "piper/ru_RU-dmitri-medium.onnx"), "text": LYR_RU.replace("\n", ", "), "out": str(OUT / "ru_clean.wav")})
    wc = word_at(OUT / "ru_clean.wav", LYR_RU, 2, "ru", "C_src")
    xr, srr = sf.read(OUT / "ru_clean.wav", dtype="float64")
    a, b = int((wc["t0"] + 0.03) * srr), int((wc["t1"] - 0.03) * srr)
    dr = xr.copy()
    dr[a:b] = rng.standard_normal(b - a) * 10 ** (-45 / 20)
    sf.write(OUT / "ru_dirty.wav", dr.astype(np.float32), srr, subtype="FLOAT")
    run(APPLIO_PY, WORK / "applio_worker.py",
        {"items": [dict(item, input=str(OUT / "ru_dirty.wav"), output=str(OUT / "ru_take_dirty.wav")),
                   dict(item, input=str(OUT / "ru_clean.wav"), output=str(OUT / "ru_take_clean.wav"))]}, cwd=str(W / "applio"))
    k1 = LYR_RU.index("чудное")
    rC = TF.revoice_text(ctx, OUT / "ru_dirty.wav", OUT / "ru_take_dirty.wav", item, LYR_RU,
                         [{"from": k1, "to": k1 + 6}], OUT / "ru_fixed.wav")
    RES["C_plan"] = [{k: q.get(k) for k in ("found", "where", "t0", "t1", "text")} for q in rC["plan"]]
    RES["C_outside_identical"] = outside_same(OUT / "ru_fixed.wav", OUT / "ru_take_dirty.wav", rC["changed"])
    for name in ("take_dirty", "fixed", "take_clean"):
        RES[f"C_word_{name}"] = word_at(OUT / f"ru_{name}.wav", LYR_RU, 2, "ru", f"C_{name}")
    yd2, ysr2 = sf.read(OUT / "ru_take_dirty.wav", dtype="float64", always_2d=True)
    yc2, _ = sf.read(OUT / "ru_take_clean.wav", dtype="float64", always_2d=True)
    yf2, _ = sf.read(OUT / "ru_fixed.wav", dtype="float64", always_2d=True)
    s2, e2 = int(wc["t0"] * ysr2), int(wc["t1"] * ysr2)
    lv = lambda v: round(float(20 * np.log10(np.sqrt(np.mean(v[s2:e2, 0] ** 2)) + 1e-9)), 1)  # noqa: E731
    RES["C_level_dB"] = {"clean": lv(yc2), "dirty": lv(yd2), "fixed": lv(yf2)}
    okC = (RES["C_outside_identical"]
           and (RES["C_word_fixed"]["score"] or 0) > (RES["C_word_take_dirty"]["score"] or 0)
           and abs(RES["C_level_dB"]["fixed"] - RES["C_level_dB"]["clean"]) < abs(RES["C_level_dB"]["dirty"] - RES["C_level_dB"]["clean"]))
else:
    print("== русский: Piper не установлен — пропуск")

RES["ok"] = {"A_eaten_en": okA, "B_replace_en": okB, "C_eaten_ru": okC}
print("== ИТОГ", json.dumps(RES, ensure_ascii=False, indent=1))
ok = okA and okB and okC
json.dump(RES, open(OUT / "result.json", "w"), ensure_ascii=False, indent=1)
print("ПРОВЕРКА:", "OK" if ok else "НЕ ПРОШЛА")
sys.exit(0 if ok else 1)
