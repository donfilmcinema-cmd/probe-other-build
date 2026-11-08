# -*- coding: utf-8 -*-
"""DZOD Executor (E1) — пение по MIDI с текстом моделью SoulX-Singer. Запускается в окружении Executor:

    python executor_worker.py job.json

Задание: {"midi", "src_dir" (код SoulX-Singer), "model" (model.pt), "prompt_wav", "prompt_meta",
          "out_wav", "meta_out", "notes_out", "seg_dir", "cache_dir", "fp16": true, "pitch_shift": 0 (Transpose),
          "style": "dynamic"|"straight", "vibrato": 50, "vib_rate": 5.5, "vib_delay": 0.25, "bend_range": 0,
          "device": "cuda"|"cpu",
          "only_meta": false, "auto_shift": false}
auto_shift у SoulX сдвигает ВСЕ ноты к медиане образца на любое число полутонов (меняет тональность) —
Executor поёт ноты как написаны, поэтому по умолчанию выключен.
Ответ @@R: {"out", "seconds_audio", "t_load", "t_synth", "rtf", "vram_gb", "segments", "warnings", "length"}
Строки: @@P <доля> <текст> / @@R <json> / @@E <текст> / @@H (жив)."""
import json
import os
import sys
import threading
import time
import traceback

_lock = threading.Lock()


def say(kind, payload):
    with _lock:
        sys.stdout.write(f"@@{kind} {payload}\n")
        sys.stdout.flush()


def main():
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        job = json.load(f)
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)

    def heartbeat():
        while True:
            time.sleep(3)
            say("H", "")
    threading.Thread(target=heartbeat, daemon=True).start()

    say("P", "0.02 Читаю ноты и текст…")
    import exec_score as SC
    score = SC.read_midi(job["midi"])
    if not score.notes:
        raise RuntimeError("В MIDI нет нот.")
    segs = SC.to_meta(score)
    if not segs:
        raise RuntimeError("В MIDI нет нот с текстом для пения.")
    notes = SC.notes_timeline(score)
    style = job.get("style", "dynamic")
    curves = job.get("curves") or {}
    spoken = [[float(x["start"]), float(x["end"])] for x in (job.get("styles") or []) if x.get("style") == "spoken"]
    # режим высоты по фразам: score — живая высота модели (Dynamic); melody — наша кривая: Straight, pitch bend,
    # кривые Pitch / Vibrato или Spoken на этой фразе
    for seg in segs:
        t0, t1 = seg["time"][0] / 1000.0, seg["time"][1] / 1000.0
        touch = lambda pts: any(t0 <= float(q[0]) < t1 for q in (pts or []))  # noqa: E731
        bends_here = job.get("bend_range") and any(t0 <= b[0] < t1 and b[1] != 0 for b in score.bends)
        pitch_here = touch(curves.get("pitch")) or (curves.get("pitch") and
                                                    abs(SC.curve_at(curves["pitch"], (t0 + t1) / 2, 0)) > 1)
        vib_here = touch(curves.get("vibrato"))
        sp_here = any(a < t1 and b > t0 for a, b in spoken)
        seg["_control"] = "melody" if (style == "straight" or bends_here or pitch_here or vib_here or sp_here) else "score"
        if seg["_control"] == "melody":
            base_vib = float(job.get("vibrato", 50)) if style == "straight" else 0.0
            seg["f0"] = SC.build_f0(seg, notes, score.bends, float(job.get("bend_range", 0)), base_vib,
                                    float(job.get("vib_rate", 5.5)), float(job.get("vib_delay", 0.25)),
                                    vib_curve=curves.get("vibrato"), pitch_curve=curves.get("pitch"), spoken=spoken)
    control = "melody" if all(sg["_control"] == "melody" for sg in segs) else (
        "score" if all(sg["_control"] == "score" for sg in segs) else "mixed")
    with open(job["meta_out"], "w", encoding="utf-8") as f:
        json.dump(segs, f, ensure_ascii=False, indent=1)
    if job.get("notes_out"):
        with open(job["notes_out"], "w", encoding="utf-8") as f:
            json.dump(notes, f, ensure_ascii=False)
    for w in score.warnings:
        say("P", "0.03 " + w)
    if job.get("only_meta"):
        say("R", json.dumps({"segments": len(segs), "warnings": score.warnings, "length": score.length}, ensure_ascii=False))
        return

    # кэш фраз: изменённая фраза пересчитывается, остальные берутся готовыми (ТЗ §2.4)
    import hashlib
    st_m = os.stat(job["model"])
    with open(job["prompt_wav"], "rb") as f:
        p_sig = hashlib.sha1(f.read()).hexdigest()
    base_key = json.dumps({"v": 3, "model": [st_m.st_size, int(st_m.st_mtime)], "prompt": p_sig,
                           "shift": int(job.get("pitch_shift", 0)), "fp16": bool(job.get("fp16", True))}, sort_keys=True)
    keys = []
    for seg in segs:
        core_seg = {k: seg[k] for k in ("duration", "text", "phoneme", "note_pitch", "note_type", "f0", "_control")}
        keys.append(hashlib.sha1((base_key + json.dumps(core_seg, sort_keys=True)).encode()).hexdigest()[:24])
    cache = job.get("cache_dir")
    if cache:
        os.makedirs(cache, exist_ok=True)
    have = [bool(cache and os.path.exists(os.path.join(cache, k + ".wav"))) for k in keys]
    rng = job.get("range")
    # SING SELECTION: поются только фразы, задевающие диапазон; остальные — из кэша, если были, иначе тишина
    wanted = [(not rng) or (sg["time"][0] / 1000.0 < float(rng[1]) and sg["time"][1] / 1000.0 > float(rng[0])) for sg in segs]
    todo = [i for i in range(len(segs)) if wanted[i] and not have[i]]
    import numpy as np
    import soundfile as sf
    sr = 24000
    model = None
    t_load = 0.0
    if todo:
        say("P", "0.05 Загружаю модель пения… (первый раз — до минуты)")
        model, dp, prompt, sr, fp16, dev, config, t_load = _load(job, control)
    else:
        say("P", "0.5 Ноты и текст не менялись — пение из кэша; манеры и кривые применятся заново.")
        dev, fp16 = "cache", bool(job.get("fp16", True))
    total = int(segs[-1]["time"][1] / 1000 * sr)
    merged = np.zeros(total, dtype=np.float32)
    seg_files = []
    t1 = time.time()
    for i, seg in enumerate(segs):
        cf = os.path.join(cache, keys[i] + ".wav") if cache else None
        a = int(seg["time"][0] / 1000 * sr)
        if i in todo:
            say("P", f"{0.1 + 0.88 * todo.index(i) / len(todo):.3f} Пою фразу {i + 1} из {len(segs)}"
                     f"{' (остальные — из кэша)' if len(todo) < len(segs) else ''}…")
            tgt = dp.process({k: v for k, v in seg.items() if not k.startswith("_")}, None)
            import torch
            with torch.no_grad():
                audio = model.infer({"prompt": prompt, "target": tgt}, auto_shift=bool(job.get("auto_shift", False)),
                                    pitch_shift=int(job.get("pitch_shift", 0)), n_steps=config.infer.n_steps,
                                    cfg=config.infer.cfg, control=seg["_control"], use_fp16=fp16)
            audio = audio.squeeze().float().cpu().numpy()
            if cf:
                sf.write(cf, audio, sr, subtype="FLOAT")
        elif have[i]:
            audio, _ = sf.read(cf, dtype="float32")
        else:
            seg_files.append({"path": None, "start": a / sr, "key": keys[i], "cached": False, "silent": True,
                              "end": seg["time"][1] / 1000.0})
            continue
        n = min(len(audio), total - a)
        merged[a:a + n] = audio[:n]
        if job.get("seg_dir"):
            fseg = os.path.join(job["seg_dir"], f"seg_{i:03d}.wav")
            sf.write(fseg, audio[:n], sr, subtype="FLOAT")
            seg_files.append({"path": fseg, "start": a / sr, "end": seg["time"][1] / 1000.0, "key": keys[i],
                              "cached": i not in todo, "silent": False})
    t_synth = time.time() - t1
    sf.write(job["out_wav"], merged, sr, subtype="FLOAT")
    vram = None
    if model is not None and str(dev).startswith("cuda"):
        import torch
        vram = round(torch.cuda.max_memory_allocated() / 2 ** 30, 2)
    secs = total / sr
    say("P", "0.99 Готово.")
    say("R", json.dumps({"out": job["out_wav"], "seconds_audio": round(secs, 2), "t_load": round(t_load, 1),
                         "t_synth": round(t_synth, 1), "rtf": round(t_synth / max(secs, 1e-6), 3), "vram_gb": vram,
                         "device": dev, "fp16": fp16, "segments": len(segs), "sung": len(todo), "control": control,
                         "phrases": [{"start": sg["time"][0] / 1000.0, "end": sg["time"][1] / 1000.0, "sung": i in todo,
                                      "control": sg["_control"], "silent": not wanted[i] and not have[i]}
                                     for i, sg in enumerate(segs)],
                         "seg_files": seg_files, "warnings": score.warnings, "length": score.length}, ensure_ascii=False))


def _load(job, control):
    src = job["src_dir"]
    sys.path.insert(0, src)
    os.chdir(src)
    import soundfile as sf
    import torch
    import torchaudio.functional as AF
    from soulxsinger.utils.file_utils import load_config
    import soulxsinger.utils.data_processor as DPM
    from cli.inference import build_model

    def load_wav(path, sample_rate):
        # образец читаем через soundfile: torchaudio.load под Windows зависит от ffmpeg/torchcodec
        x, sr0 = sf.read(path, dtype="float32", always_2d=True)
        w = torch.from_numpy(x.mean(axis=1))[None]
        return AF.resample(w, sr0, sample_rate) if sr0 != sample_rate else w
    DPM.load_wav = load_wav
    DataProcessor = DPM.DataProcessor

    dev = job.get("device") or ("cuda" if torch.cuda.is_available() else "cpu")
    fp16 = bool(job.get("fp16", True)) and dev.startswith("cuda")
    t0 = time.time()
    config = load_config(os.path.join(src, "soulxsinger", "config", "soulxsinger.yaml"))
    model = build_model(job["model"], config, device=dev, use_fp16=fp16)
    t_load = time.time() - t0
    sr = int(config.audio.sample_rate)
    dp = DataProcessor(hop_size=config.audio.hop_size, sample_rate=sr,
                       phoneset_path=os.path.join(src, "soulxsinger", "utils", "phoneme", "phone_set.json"), device=dev)
    with open(job["prompt_meta"], "r", encoding="utf-8") as f:
        pmeta = json.load(f)[0]
    prompt = dp.process(pmeta, job["prompt_wav"])
    if dev.startswith("cuda"):
        torch.cuda.reset_peak_memory_stats()
    return model, dp, prompt, sr, fp16, dev, config, t_load


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:  # noqa: BLE001
        traceback.print_exc()
        say("E", str(ex) or ex.__class__.__name__)
        sys.exit(1)
