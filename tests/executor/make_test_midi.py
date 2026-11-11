# -*- coding: utf-8 -*-
"""Две английские фразы для проверки E1 (MIDI с текстом, как из Cubase/Samplitude/Studio)."""
import sys
import mido

def build(path, word="wish"):
    mid = mido.MidiFile(ticks_per_beat=480)
    tr = mido.MidiTrack(); mid.tracks.append(tr)
    tr.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(92), time=0))
    tr.append(mido.MetaMessage("track_name", name="Executor test", time=0))
    q = 480
    # фраза 1: слог на ноту, слово через дефисы, мелизм «-»
    # word="never" — эталон для проверки замены слова (два слога на месте «wish», та же высота)
    # другое односложное слово (проверка «poom → pay») — на той же ноте
    w2 = [("wish", 67, q)] if word == "wish" else (
        [("ne-", 67, q // 2), ("ver", 67, q // 2)] if word == "never" else [(word, 67, q)])
    p1 = [("I", 64, q)] + w2 + [("you", 69, q // 2), ("were", 67, q // 2), ("here", 69, q), ("-", 71, q), ("to-", 72, q // 2), ("night", 71, q * 2)]
    # пауза 2,5 доли — новая фраза; фраза 2: строка текста в первой ноте, [фонемы], sil
    p2 = [("we are the", 62, q // 2), ("", 64, q // 2), ("", 65, q), ("beau-", 67, q), ("ti-", 69, q // 2), ("ful", 67, q), ("sil", 0, q // 2), ("[N OW1]", 64, q), ("-", 62, q * 2)]
    gap = 0
    for i, (phrase, rest) in enumerate(((p1, 0), (p2, int(q * 2.5)))):
        gap = rest
        if i == 1:
            tr.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(100), time=0))
        for lyr, pitch, dur in phrase:
            if lyr == "sil":
                tr.append(mido.MetaMessage("lyrics", text="sil", time=gap))
                tr.append(mido.Message("note_on", note=60, velocity=1, time=0))
                tr.append(mido.Message("note_off", note=60, velocity=0, time=dur))
                gap = 0
                continue
            if lyr:
                tr.append(mido.MetaMessage("lyrics", text=lyr, time=gap)); gap = 0
            tr.append(mido.Message("note_on", note=pitch, velocity=96, time=gap))
            tr.append(mido.Message("note_off", note=pitch, velocity=0, time=dur))
            gap = 0
    mid.save(path)

if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "wish")
