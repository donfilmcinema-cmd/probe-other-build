# -*- coding: utf-8 -*-
"""Речь Piper в WAV (для проверки русского): python piper_say.py job.json  {voice, text, out}."""
import json
import sys

import numpy as np
import soundfile as sf
from piper import PiperVoice

job = json.load(open(sys.argv[1], encoding="utf-8"))
v = PiperVoice.load(job["voice"])
x = np.concatenate([c.audio_float_array for c in v.synthesize(job["text"])])
x = np.concatenate([np.zeros(int(0.3 * v.config.sample_rate)), x, np.zeros(int(0.3 * v.config.sample_rate))])
sf.write(job["out"], x.astype(np.float32), v.config.sample_rate, subtype="FLOAT")
print("@@R " + json.dumps({"out": job["out"], "seconds": round(len(x) / v.config.sample_rate, 2)}))
