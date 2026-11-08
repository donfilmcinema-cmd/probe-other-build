# -*- coding: utf-8 -*-
"""DZOD Vocalist — перепевка голосом модели. Запускается внутри движка (окружение Applio),
рабочая папка — папка Applio:

    python applio_worker.py job.json

Задание (JSON):  {"items": [ {input, output, model, index, pitch, index_rate, protect,
                  volume_envelope, f0_method, f0_autotune, f0_autotune_strength,
                  clean_audio, clean_strength, split_audio, proposed_pitch, embedder_model}, ... ]}
Модели распознавания (embedder, высота тона) загружаются один раз на всё задание —
так быстрее сравнивать несколько эпох.

Строки для программы:  @@P <доля> <текст>  /  @@R <json>  /  @@E <текст>
"""
import json
import os
import sys
import time
import traceback


import threading as _th
_say_lock = _th.Lock()


def say(kind, payload):
    with _say_lock:
        sys.stdout.write(f"@@{kind} {payload}\n")
        sys.stdout.flush()


def main():
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        job = json.load(f)
    here = os.getcwd()
    if here not in sys.path:
        sys.path.insert(0, here)
    import re
    import threading

    # «движок жив»: раз в 3 с, даже пока видеокарта занята (по нему программа отличает работу от зависания)
    def heartbeat():
        while True:
            time.sleep(3)
            say("H", "")
    threading.Thread(target=heartbeat, daemon=True).start()

    say("P", "0.01 Загружаю движок голоса… (первый раз — до минуты)")
    import rvc.infer.infer as inf
    from rvc.infer.infer import VoiceConverter
    vc = VoiceConverter()
    items = job["items"]
    out = []
    cur = {"lo": 0.0, "hi": 1.0, "label": "", "n": 0}
    split_re = re.compile(r"split into (\d+) chunks")
    chunk_re = re.compile(r"Converted audio chunk (\d+)")
    real_print = print

    def my_print(*a, **kw):
        text = " ".join(str(x) for x in a)
        m = split_re.search(text)
        if m:
            cur["n"] = int(m.group(1))
        m = chunk_re.search(text)
        if m and cur["n"]:
            k = int(m.group(1))
            f = cur["lo"] + (cur["hi"] - cur["lo"]) * min(1.0, k / cur["n"])
            say("P", f"{f:.3f} Перепеваю: {cur['label']} — кусок {k} из {cur['n']}")
            return
        real_print(*a, **kw)
    inf.print = my_print

    import soundfile as sf
    for i, it in enumerate(items):
        label = it.get("label") or os.path.basename(it["input"])
        lo, hi = 0.03 + 0.95 * i / len(items), 0.03 + 0.95 * (i + 1) / len(items)
        cur.update(lo=lo, hi=hi, label=label, n=0)
        try:
            seconds = sf.info(it["input"]).duration
        except Exception:
            seconds = 0
        # длинную партию — по кускам между паузами: видно ход работы, меньше памяти видеокарты
        split = bool(it.get("split_audio")) or seconds > 30
        say("P", f"{lo:.3f} Перепеваю: {label}" + (f" ({int(seconds // 60)}:{int(seconds % 60):02d})" if seconds else ""))
        t0 = time.time()
        if os.path.exists(it["output"]):
            os.remove(it["output"])
        vc.convert_audio(
            audio_input_path=it["input"],
            audio_output_path=it["output"],
            model_path=it["model"],
            index_path=it.get("index") or "",
            pitch=float(it.get("pitch", 0)),        # дробное — Fine Tune Executor (центы / 100)
            f0_method=it.get("f0_method", "rmvpe"),
            index_rate=float(it.get("index_rate", 0.6)),
            volume_envelope=float(it.get("volume_envelope", 1.0)),
            protect=float(it.get("protect", 0.33)),
            split_audio=split,
            f0_autotune=bool(it.get("f0_autotune", False)),
            f0_autotune_strength=float(it.get("f0_autotune_strength", 0.5)),
            proposed_pitch=bool(it.get("proposed_pitch", False)),
            proposed_pitch_threshold=float(it.get("proposed_pitch_threshold", 155.0)),
            clean_audio=bool(it.get("clean_audio", False)),
            clean_strength=float(it.get("clean_strength", 0.5)),
            export_format="WAV",
            embedder_model=it.get("embedder_model", "contentvec"),
            embedder_model_custom=None,
            post_process=False,
            sid=0,
        )
        if not os.path.exists(it["output"]):
            raise RuntimeError(f"Перепевка не дала файла: {label}. Подробности — в журнале движка.")
        fm = float(it.get("formant", 1.0) or 1.0)
        if abs(fm - 1.0) > 1e-3:
            # Formant (Executor): сдвиг формант без смены высоты — огибающая спектра (кепстр) растягивается
            import numpy as np
            import soundfile as sf
            from stftpitchshift import StftPitchShift
            x, sr0 = sf.read(it["output"], dtype="float64", always_2d=True)
            sh = StftPitchShift(1024, 256, sr0)
            y = np.stack([sh.shiftpitch(x[:, c], factors=1, quefrency=0.0015, distortion=fm) for c in range(x.shape[1])], 1)
            sf.write(it["output"], y[:len(x)], sr0)
        out.append({"output": it["output"], "seconds": round(time.time() - t0, 1)})
    say("R", json.dumps({"items": out}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:  # noqa: BLE001
        traceback.print_exc()
        say("E", str(ex).replace("\n", " ")[:600] or ex.__class__.__name__)
        sys.exit(1)
