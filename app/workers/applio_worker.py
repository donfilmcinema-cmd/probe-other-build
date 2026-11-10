# -*- coding: utf-8 -*-
"""DZOD Vocalist — перепевка голосом модели. Запускается внутри движка (окружение Applio),
рабочая папка — папка Applio:

    python applio_worker.py job.json

Задание (JSON):  {"items": [ {input, output, model, index, pitch, index_rate, protect,
                  volume_envelope, f0_method, f0_autotune, f0_autotune_strength,
                  clean_audio, clean_strength, split_audio, proposed_pitch, embedder_model,
                  content_input, f0_spans}, ... ]}
content_input — «перепевка с текстом»: содержание (ContentVec) из этого файла, высота и громкость — из input;
f0_spans — [[t0, t1], …] с: там голос/не голос — по content_input, провалы высоты — по соседям.
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


# ---- «Перепевка с текстом»: второй вход для содержания (ContentVec) и заполнение высоты в провалах ----
# Высота (RMVPE) и громкость берутся из input, содержание — из content_input той же длины.
# Applio не меняется: оборачиваются три метода Pipeline на время одного item.
_TF = {"content": None, "cpad": None, "spans": [], "busy": False}


def _install_textfix_hooks():
    import numpy as np
    import rvc.infer.pipeline as PL
    from scipy import signal
    if getattr(PL.Pipeline, "_dz_textfix", False):
        return
    o_pipe, o_vc, o_f0 = PL.Pipeline.pipeline, PL.Pipeline.voice_conversion, PL.Pipeline.get_f0

    def pipe(self, *a, **kw):
        audio = kw.get("audio") if "audio" in kw else a[3]
        c = _TF["content"]
        _TF["cpad"] = None
        if c is not None and len(c) == len(audio):
            cc = signal.filtfilt(PL.bh, PL.ah, c)
            _TF["cpad"] = np.pad(cc, (self.t_pad, self.t_pad), mode="reflect")
        return o_pipe(self, *a, **kw)

    def vc(self, model, net_g, sid, audio0, *rest):
        cp = _TF["cpad"]
        base = getattr(audio0, "base", None)
        if cp is not None and base is not None and getattr(base, "shape", None) == cp.shape:
            off = (audio0.ctypes.data - base.ctypes.data) // audio0.itemsize
            audio0 = np.ascontiguousarray(cp[off:off + len(audio0)])
        return o_vc(self, model, net_g, sid, audio0, *rest)

    def f0(self, x, p_len, *rest):
        coarse, f = o_f0(self, x, p_len, *rest)
        cp = _TF["cpad"]
        if cp is None or _TF["busy"] or x.shape != cp.shape or not _TF["spans"]:
            return coarse, f
        _TF["busy"] = True
        try:
            _cc, cf = o_f0(self, cp, p_len, *rest)          # голос/не голос — по подсказке
        finally:
            _TF["busy"] = False
        f = f.copy()
        n = len(f)
        off = self.t_pad / self.sample_rate
        tt = np.arange(n) * self.window / self.sample_rate - off
        inside = np.zeros(n, bool)
        for t0, t1 in _TF["spans"]:
            inside |= (tt >= t0) & (tt < t1)
        src_ok = f > 0
        ref = np.flatnonzero(src_ok & ~inside) if (src_ok & ~inside).any() else np.flatnonzero(src_ok)
        for k in np.flatnonzero(inside):
            if k >= len(cf) or cf[k] <= 0:
                f[k] = 0.0                                   # глухой звук подсказки — глухо
            elif f[k] <= 0 and len(ref):
                # провал (съедено): высота по соседям — линейно в логарифме между ближайшими голосовыми кадрами
                j = np.searchsorted(ref, k)
                a_ = ref[max(0, j - 1)]
                b_ = ref[min(len(ref) - 1, j)]
                if a_ == b_:
                    f[k] = f[a_]
                else:
                    w = (k - a_) / (b_ - a_)
                    f[k] = float(np.exp(np.log(f[a_]) * (1 - w) + np.log(f[b_]) * w))
        mel = 1127 * np.log(1 + f / 700)
        pos = mel > 0
        mel[pos] = (mel[pos] - self.f0_mel_min) * 254 / (self.f0_mel_max - self.f0_mel_min) + 1
        mel[mel <= 1] = 1
        mel[mel > 255] = 255
        return np.rint(mel).astype(int), f

    PL.Pipeline.pipeline, PL.Pipeline.voice_conversion, PL.Pipeline.get_f0 = pipe, vc, f0
    PL.Pipeline._dz_textfix = True


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
        _TF.update(content=None, cpad=None, spans=[])
        if it.get("content_input"):
            # перепевка с текстом: содержание — из content_input (та же длина), высота в spans — по правилу подсказки
            from rvc.lib.utils import load_audio_infer
            import numpy as np
            _install_textfix_hooks()
            src = load_audio_infer(it["input"], 16000)
            c = load_audio_infer(it["content_input"], 16000)
            m = np.abs(src).max() / 0.95
            c = c / m if m > 1 else c                    # тот же масштаб, что Applio даёт input
            if len(c) != len(src):
                c = np.pad(c, (0, max(0, len(src) - len(c))))[:len(src)]
            _TF.update(content=c.astype(np.float64), spans=[[float(a), float(b)] for a, b in it.get("f0_spans") or []])
            split = False                                # кусок короткий: одним проходом (иначе куски не совпадут)
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
        _TF.update(content=None, cpad=None, spans=[])
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
