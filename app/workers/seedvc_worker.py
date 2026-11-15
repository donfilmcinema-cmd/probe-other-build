# -*- coding: utf-8 -*-
"""DZOD Vocalist 0.8.1 — второй движок перепевки: Seed-VC (singing, 44,1 кГц; github.com/Plachtaa/seed-vc, GPLv3).
Окружение Executor (torch, transformers), код Seed-VC — в Executor/seedvc/src:

    python seedvc_worker.py job.json

Чем отличается от RVC: «что поётся» берётся из признаков Whisper (распознавание речи), а не ContentVec —
артикуляция любых слов держится лучше; тембр — по образцу голоса (до 25 с пения вокалистки), без обучения;
высота — исходника (RMVPE), сдвиг — полутоны.
Задание: {"src_dir": папка кода Seed-VC, "device": "cuda"|"cpu"|null, "fp16": true,
          "items": [{"input", "output", "reference", "pitch": 0, "steps": 30, "cfg_rate": 0.7, "label"}],
          "warmup": false (только скачать и загрузить модели)}
Строки: @@P <доля> <текст> / @@R <json> / @@E <текст>."""
import json
import os
import sys
import time
import traceback
import types


def say(kind, payload):
    sys.stdout.write(f"@@{kind} {payload}\n")
    sys.stdout.flush()


DIT_REPO, DIT_FILE, DIT_CFG = ("Plachta/Seed-VC", "DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth",
                               "config_dit_mel_seed_uvit_whisper_base_f0_44k.yml")


def _stub_dac():
    """length_regulator импортирует dac (descript-audio-codec, тяжёлый, со своими зависимостями) ради
    VectorQuantize — в нашей модели (vector_quantize: false) он не используется. Заглушка вместо пакета."""
    if "dac" in sys.modules:
        return
    dac = types.ModuleType("dac")
    nn = types.ModuleType("dac.nn")
    q = types.ModuleType("dac.nn.quantize")

    class VectorQuantize:  # noqa: D401
        def __init__(self, *a, **k):
            raise RuntimeError("VectorQuantize не нужен этой модели Seed-VC")

    q.VectorQuantize = VectorQuantize
    dac.nn, nn.quantize = nn, q
    sys.modules.update({"dac": dac, "dac.nn": nn, "dac.nn.quantize": q})


def load(src_dir, device, fp16):
    import torch
    import yaml
    os.chdir(src_dir)
    sys.path.insert(0, src_dir)
    os.environ.setdefault("HF_HUB_CACHE", os.path.join(src_dir, "checkpoints", "hf_cache"))
    _stub_dac()
    from hf_utils import load_custom_model_from_hf
    from modules.commons import build_model, load_checkpoint, recursive_munch
    from modules.rmvpe import RMVPE
    from modules.campplus.DTDNN import CAMPPlus
    from modules.bigvgan import bigvgan
    from modules.audio import mel_spectrogram
    from transformers import AutoFeatureExtractor, WhisperModel
    dev = torch.device(device)
    say("P", "0.02 Seed-VC: модели (первый раз — скачивание ~1,5 ГБ)…")
    ckpt, cfg = load_custom_model_from_hf(DIT_REPO, DIT_FILE, DIT_CFG)
    config = yaml.safe_load(open(cfg, "r"))
    mp = recursive_munch(config["model_params"])
    mp.dit_type = "DiT"
    model = build_model(mp, stage="DiT")
    model, _, _, _ = load_checkpoint(model, None, ckpt, load_only_params=True, ignore_modules=[], is_distributed=False)
    for k in model:
        model[k].eval()
        model[k].to(dev)
    model.cfm.estimator.setup_caches(max_batch_size=1, max_seq_length=8192)
    f0x = RMVPE(load_custom_model_from_hf("lj1995/VoiceConversionWebUI", "rmvpe.pt", None), is_half=False, device=dev)
    camp = CAMPPlus(feat_dim=80, embedding_size=192)
    camp.load_state_dict(torch.load(load_custom_model_from_hf("funasr/campplus", "campplus_cn_common.bin", None),
                                    map_location="cpu"))
    camp.eval().to(dev)
    voc = bigvgan.BigVGAN.from_pretrained(mp.vocoder.name, use_cuda_kernel=False)
    voc.remove_weight_norm()
    voc = voc.eval().to(dev)
    wname = mp.speech_tokenizer.name
    wm = WhisperModel.from_pretrained(wname, torch_dtype=torch.float16 if dev.type == "cuda" else torch.float32).to(dev)
    del wm.decoder
    wfe = AutoFeatureExtractor.from_pretrained(wname)

    def semantic(w16):
        inp = wfe([w16.squeeze(0).cpu().numpy()], return_tensors="pt", return_attention_mask=True)
        feats = wm._mask_input_features(inp.input_features, attention_mask=inp.attention_mask).to(dev)
        with torch.no_grad():
            out = wm.encoder(feats.to(wm.encoder.dtype), head_mask=None, output_attentions=False,
                             output_hidden_states=False, return_dict=True)
        s = out.last_hidden_state.to(torch.float32)
        return s[:, :w16.size(-1) // 320 + 1]

    sp = config["preprocess_params"]["spect_params"]
    sr = config["preprocess_params"]["sr"]
    mel_args = {"n_fft": sp["n_fft"], "win_size": sp["win_length"], "hop_size": sp["hop_length"],
                "num_mels": sp["n_mels"], "sampling_rate": sr, "fmin": sp["fmin"],
                "fmax": None if sp["fmax"] == "None" else 8000, "center": False}

    def mel_fn(x):
        return mel_spectrogram(x, **mel_args)

    return {"model": model, "semantic": semantic, "f0": f0x.infer_from_audio, "camp": camp, "voc": voc,
            "mel": mel_fn, "sr": sr, "hop": sp["hop_length"], "dev": dev, "fp16": fp16 and dev.type == "cuda"}


def _crossfade(a, b, n):
    import numpy as np
    w = 0.5 * (1 - np.cos(np.linspace(0, np.pi, n)))
    b = b.copy()
    b[:n] = a[:n] * (1 - w) + b[:n] * w
    return b


def convert(M, src_path, ref_path, out_path, pitch=0, steps=30, cfg_rate=0.7):
    """Как inference.py Seed-VC (f0_condition, без автоподстройки высоты), куски по 30 с с перекрытием."""
    import librosa
    import numpy as np
    import soundfile as sf
    import torch
    import torchaudio
    dev, sr, hop = M["dev"], M["sr"], M["hop"]
    src = librosa.load(src_path, sr=sr)[0]
    ref = librosa.load(ref_path, sr=sr)[0][:sr * 25]
    s = torch.tensor(src).unsqueeze(0).float().to(dev)
    r = torch.tensor(ref).unsqueeze(0).float().to(dev)
    max_ctx = sr // hop * 30
    ovf = 16
    ovw = ovf * hop
    s16 = torchaudio.functional.resample(s, sr, 16000)
    if s16.size(-1) <= 16000 * 30:
        S_alt = M["semantic"](s16)
    else:
        ov, parts, buf, pos = 5, [], None, 0
        while pos < s16.size(-1):
            if buf is None:
                ch = s16[:, pos:pos + 16000 * 30]
            else:
                ch = torch.cat([buf, s16[:, pos:pos + 16000 * (30 - ov)]], dim=-1)
            S = M["semantic"](ch)
            parts.append(S if pos == 0 else S[:, 50 * ov:])
            buf = ch[:, -16000 * ov:]
            pos += 30 * 16000 if pos == 0 else ch.size(-1) - 16000 * ov
        S_alt = torch.cat(parts, dim=1)
    r16 = torchaudio.functional.resample(r, sr, 16000)
    S_ori = M["semantic"](r16)
    mel, mel2 = M["mel"](s.float()), M["mel"](r.float())
    tl = torch.LongTensor([mel.size(2)]).to(dev)
    tl2 = torch.LongTensor([mel2.size(2)]).to(dev)
    fb = torchaudio.compliance.kaldi.fbank(r16, num_mel_bins=80, dither=0, sample_frequency=16000)
    style = M["camp"]((fb - fb.mean(dim=0, keepdim=True)).unsqueeze(0))
    F0_ori = torch.from_numpy(M["f0"](r16[0], thred=0.03)).to(dev)[None]
    F0_alt = torch.from_numpy(M["f0"](s16[0], thred=0.03)).to(dev)[None]
    f0 = F0_alt.clone()
    if pitch:
        f0[F0_alt > 1] = F0_alt[F0_alt > 1] * (2 ** (float(pitch) / 12))
    model = M["model"]
    with torch.no_grad():
        cond = model.length_regulator(S_alt, ylens=tl, n_quantizers=3, f0=f0)[0]
        pcond = model.length_regulator(S_ori, ylens=tl2, n_quantizers=3, f0=F0_ori)[0]
        win = max_ctx - mel2.size(2)
        done, chunks, prev = 0, [], None
        while done < cond.size(1):
            c = cond[:, done:done + win]
            last = done + win >= cond.size(1)
            cat = torch.cat([pcond, c], dim=1)
            with torch.autocast(device_type=dev.type, dtype=torch.float16 if M["fp16"] else torch.float32,
                                enabled=dev.type == "cuda"):
                vt = model.cfm.inference(cat, torch.LongTensor([cat.size(1)]).to(dev), mel2, style, None, steps,
                                         inference_cfg_rate=cfg_rate)
                vt = vt[:, :, mel2.size(-1):]
            w = M["voc"](vt.float()).squeeze().float().cpu().numpy()
            if prev is None:
                if last:
                    chunks.append(w)
                    break
                chunks.append(w[:-ovw])
            elif last:
                chunks.append(_crossfade(prev, w, ovw))
                break
            else:
                chunks.append(_crossfade(prev, w[:-ovw], ovw))
            prev = w[-ovw:]
            done += vt.size(2) - ovf
    y = np.concatenate(chunks).astype(np.float32)
    n = int(round(len(src)))
    y = np.pad(y, (0, max(0, n - len(y))))[:n]          # длина — как у исходника (стыки в Studio)
    sf.write(out_path, y, sr, subtype="FLOAT")
    if dev.type == "cuda":
        torch.cuda.empty_cache()
    return {"output": out_path, "sr": sr, "seconds": round(len(y) / sr, 2)}


def main():
    try:
        job = json.load(open(sys.argv[1], encoding="utf-8"))
        import torch
        dev = job.get("device") or ("cuda" if torch.cuda.is_available() else "cpu")
        t0 = time.time()
        M = load(job["src_dir"], dev, bool(job.get("fp16", True)))
        t_load = round(time.time() - t0, 1)
        out = []
        items = job.get("items") or []
        for k, it in enumerate(items):
            say("P", f"{0.1 + 0.85 * k / max(1, len(items)):.3f} Seed-VC: {it.get('label', 'перепевка')}…")
            t1 = time.time()
            r = convert(M, it["input"], it["reference"], it["output"], it.get("pitch", 0), int(it.get("steps", 30)),
                        float(it.get("cfg_rate", 0.7)))
            r["time_s"] = round(time.time() - t1, 1)
            out.append(r)
        say("R", json.dumps({"items": out, "device": str(dev), "t_load": t_load}, ensure_ascii=False))
    except Exception as ex:  # noqa: BLE001
        traceback.print_exc()
        say("E", f"Seed-VC: {ex}")
        sys.exit(1)


if __name__ == "__main__":
    main()
