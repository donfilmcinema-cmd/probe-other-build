# -*- coding: utf-8 -*-
"""DZOD Vocalist — чистка вокала. Запускается внутри движка (окружение Sep):

    python sep_worker.py job.json

Задание (JSON):
    input        исходный файл
    work_dir     папка для промежуточных файлов (латиница, без пробелов)
    models_dir   папка моделей разделения
    mix          true — на входе полный трек, сначала выделить вокал
    backing      true — отделить бэк-вокал от ведущего
    quality      "fast" | "max" — одна модель или ансамбль на шагах вокала и бэков
    dereverb     "off" | "soft" | "mid" | "strong"
    denoise      true — убрать шум и «стекло»
    backing_same true — бэки чистить так же, как лид (реверберация, шум)

Строки для программы:  @@P <доля> <текст>   — ход работы
                       @@R <json>           — результат (пути файлов)
                       @@E <текст>          — ошибка
"""
import json
import logging
import os
import sys
import time
import traceback

# Модели (файлы из набора UVR / audio-separator). Перед заменой проверять: audio-separator -l
VOCALS_FAST = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
VOCALS_MAX = "vocal_rvc"                     # ансамбль-пресет, подобран для данных под RVC
KARAOKE_FAST = "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt"
KARAOKE_MAX = "karaoke"                      # ансамбль из трёх моделей
# Реверберация.
# «uvr» — пресет автора из Ultimate Vocal Remover 5.6 (27.10.2026): VR Architecture, UVR-DeEcho-DeReverb,
# Window Size 512, Aggression 50, No Reverb Only; TTA, Post-Process и High End Process выключены (как по умолчанию в UVR).
# Тот же код VR, что в UVR (audio-separator перенесён из UVR), та же формула агрессии (значение / 100).
UVR_DEREVERB = "UVR-DeEcho-DeReverb.pth"
UVR_WINDOW = 512
UVR_AGGRESSION = 50
ROFORMER_DEREVERB = "dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt"
OLD_DEREVERB = {"soft": "uvr", "mid": "uvr", "strong": "uvr"}   # прежние варианты 0.1–0.2 → пресет UVR
DENOISE = "denoise_mel_band_roformer_aufr33_sdr_27.9959.ckpt"
WORK_SR = 44100                              # модели обучены на 44,1 кГц; к частоте исходника вернёт программа

# какой стем — «нужный» результат шага, какой — «остаток»
KEEP = {"vocals", "no reverb", "noreverb", "dry", "no echo", "no noise"}
REST = {"instrumental", "reverb", "no dry", "echo", "noise", "other"}


import threading as _th
_say_lock = _th.Lock()


def say(kind, payload):
    with _say_lock:
        sys.stdout.write(f"@@{kind} {payload}\n")
        sys.stdout.flush()


def stem_of(path):
    """«in_(No Reverb)_model.wav» -> «no reverb». Берётся последняя скобка: её добавила эта модель."""
    name = os.path.basename(path)
    a = name.rfind("_(")
    b = name.find(")", a + 2)
    if a < 0 or b < 0:
        return ""
    return name[a + 2:b].strip().lower()


class Chain:
    def __init__(self, job):
        self.job = job
        self.work = job["work_dir"]
        os.makedirs(self.work, exist_ok=True)
        from audio_separator.separator import Separator
        self.Separator = Separator
        self.n = 0
        self.loaded = {}

    def run(self, src, model, label, vr=None):
        """Один проход модели: возвращает (нужный, остаток)."""
        self.n += 1
        out_dir = os.path.join(self.work, f"s{self.n}")
        os.makedirs(out_dir, exist_ok=True)
        extra = {}
        if vr:
            extra["vr_params"] = {"batch_size": 1, "window_size": int(vr.get("window_size", 512)),
                                  "aggression": int(vr.get("aggression", 5)), "enable_tta": False,
                                  "enable_post_process": False, "post_process_threshold": 0.2,
                                  "high_end_process": False}
        common = dict(**extra,
            log_level=logging.WARNING,
            model_file_dir=self.job["models_dir"],
            output_dir=out_dir,
            output_format="WAV",
            normalization_threshold=0.999,
            amplification_threshold=0.0,
            sample_rate=WORK_SR,
            use_soundfile=True,
        )
        if model in ("vocal_rvc", "karaoke"):
            sep = self.Separator(ensemble_preset=model, **common)
            sep.load_model()
        else:
            sep = self.Separator(**common)
            sep.load_model(model_filename=model)
        files = sep.separate(src)
        files = [f if os.path.isabs(f) else os.path.join(out_dir, f) for f in files]
        keep = rest = None
        for f in files:
            st = stem_of(f)
            if st in KEEP and keep is None:
                keep = f
            elif st in REST and rest is None:
                rest = f
        if keep is None or rest is None:
            # модель назвала стемы иначе — опираемся на порядок: первый — основной
            names = ", ".join(os.path.basename(f) for f in files)
            if len(files) >= 2 and keep is None and rest is None:
                keep, rest = files[0], files[1]
            elif keep is None:
                raise RuntimeError(f"{label}: не найден нужный стем среди: {names}")
        # короткие имена: длинные цепочки имён упираются в предел длины пути Windows
        short_keep = os.path.join(self.work, f"s{self.n}_keep.wav")
        os.replace(keep, short_keep)
        short_rest = None
        if rest:
            short_rest = os.path.join(self.work, f"s{self.n}_rest.wav")
            os.replace(rest, short_rest)
        return short_keep, short_rest


def plan_of(job):
    plan = []
    if job.get("mix"):
        plan.append("vocals")
    if job.get("backing"):
        plan.append("karaoke")
    if job.get("dereverb", "off") != "off":
        plan.append("dereverb")
    if job.get("denoise"):
        plan.append("denoise")
    return plan


def process_one(ch, src, job, lo, hi):
    """Чистка одного файла; ход работы — в отрезке [lo, hi]."""
    plan = plan_of(job)
    both = bool(job.get("backing") and job.get("backing_same"))
    total = len(plan) + (plan.count("dereverb") + plan.count("denoise")) * (1 if both else 0)
    st = {"done": 0}
    quality = job.get("quality", "fast")
    tag = job.get("_tag", "")

    def step(msg):
        a = lo + (hi - lo) * min(0.98, st["done"] / max(1, total))
        b = lo + (hi - lo) * min(0.99, (st["done"] + 1) / max(1, total))
        say("P", f"{a:.3f}|{b:.3f} {tag}{msg}")

    lead, backing, res = src, None, {}
    if "vocals" in plan:
        step("Выделяю вокал из трека…")
        lead, res["music"] = ch.run(lead, VOCALS_MAX if quality == "max" else VOCALS_FAST, "Выделение вокала")
        st["done"] += 1
    if "karaoke" in plan:
        step("Отделяю бэк-вокал от ведущего…")
        lead, backing = ch.run(lead, KARAOKE_MAX if quality == "max" else KARAOKE_FAST, "Отделение бэков")
        res["backing"] = backing
        st["done"] += 1
    if "dereverb" in plan:
        mode = OLD_DEREVERB.get(job["dereverb"], job["dereverb"])
        if mode == "roformer":
            model, vr, how = ROFORMER_DEREVERB, None, "нейросеть Roformer"
        else:
            aggr = int(job.get("dereverb_aggr", UVR_AGGRESSION))
            model, vr, how = UVR_DEREVERB, {"window_size": UVR_WINDOW, "aggression": aggr}, f"UVR DeEcho-DeReverb, агрессия {aggr}"
        step(f"Убираю реверберацию ({how})…")
        lead, res["reverb"] = ch.run(lead, model, "Реверберация", vr)
        st["done"] += 1
        if backing and both:
            step("Убираю реверберацию с бэков…")
            backing, _ = ch.run(backing, model, "Реверберация бэков", vr)
            res["backing"] = backing
            st["done"] += 1
    if "denoise" in plan:
        step("Убираю шум с ведущего…")
        lead, res["noise"] = ch.run(lead, DENOISE, "Шум")
        st["done"] += 1
        if backing and both:
            step("Убираю шум с бэков…")
            backing, _ = ch.run(backing, DENOISE, "Шум бэков")
            res["backing"] = backing
            st["done"] += 1
    res["lead"] = lead
    return res


def download_all(job):
    """Скачивает модели заранее (каталог — models_dir); имена — job["models"]."""
    from audio_separator.separator import Separator
    names = job["models"]
    sep = Separator(log_level=logging.WARNING, model_file_dir=job["models_dir"], info_only=True)
    for i, name in enumerate(names):
        lo, hi = i / len(names), (i + 1) / len(names)
        say("P", f"{lo:.3f}|{hi:.3f} Скачиваю модель {i + 1} из {len(names)}: {name}")
        sep.download_model_files(name)
    say("R", json.dumps({"downloaded": names}, ensure_ascii=False))


def main():
    job_path = sys.argv[1]
    with open(job_path, "r", encoding="utf-8") as f:
        job = json.load(f)
    def heartbeat():
        while True:
            time.sleep(3)
            say("H", "")
    _th.Thread(target=heartbeat, daemon=True).start()
    if job.get("mode") == "download":
        return download_all(job)
    t0 = time.time()
    if not plan_of(job):
        raise RuntimeError("Не выбрано ни одного шага чистки.")
    say("P", "0.01 Загружаю модели чистки…")
    inputs = job.get("inputs") or [job["input"]]
    results = []
    base_work = job["work_dir"]
    for i, src in enumerate(inputs):
        sub = dict(job)
        sub["work_dir"] = os.path.join(base_work, f"f{i}") if len(inputs) > 1 else base_work
        sub["_tag"] = f"[{i + 1}/{len(inputs)}] " if len(inputs) > 1 else ""
        ch = Chain(sub)
        lo, hi = i / len(inputs), (i + 1) / len(inputs)
        results.append(process_one(ch, src, sub, lo, hi))
    out = dict(results[0]) if len(inputs) == 1 else {"items": results}
    out["seconds"] = round(time.time() - t0, 1)
    say("R", json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:  # noqa: BLE001
        traceback.print_exc()
        say("E", str(ex).replace("\n", " ")[:600] or ex.__class__.__name__)
        sys.exit(1)
