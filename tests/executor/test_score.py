# -*- coding: utf-8 -*-
"""Проверка разбора MIDI с текстом (запуск в среде с g2p_en и mido):  python tests/executor/test_score.py"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "app", "workers"))
sys.path.insert(0, HERE)
import exec_score as SC  # noqa: E402
import make_test_midi  # noqa: E402

ok = True


def check(name, cond, extra=""):
    global ok
    print(("OK    " if cond else "ОШИБКА ") + name + (f"  ({extra})" if extra else ""))
    ok = ok and cond


d = tempfile.mkdtemp()
mid = os.path.join(d, "t.mid")
make_test_midi.build(mid)
sc = SC.read_midi(mid)
check("17 нот прочитано, смена темпа учтена", len(sc.notes) == 17 and abs(sc.notes[8].start - 6.391) < 0.01, f"{len(sc.notes)}, {sc.notes[8].start:.3f}")
segs = SC.to_meta(sc)
s = segs[0]
cols = [s[k].split() for k in ("duration", "text", "phoneme", "note_pitch", "note_type")]
check("столбцы разметки одной длины", len({len(c) for c in cols}) == 1, str([len(c) for c in cols]))
check("сумма длительностей = время сегмента", abs(sum(map(float, cols[0])) - (s["time"][1] - s["time"][0]) / 1000) < 0.01)
text = s["text"].split()
check("дефисы: to- night → tonight ×2", text.count("tonight") == 2)
check("строка «we are the» разложена по нотам", text[text.index("we"):text.index("we") + 3] == ["we", "are", "the"])
check("beau- ti- ful → одно слово на 3 нотах (2,3,3)",
      [t for t, w in zip(s["note_type"].split(), text) if w == "beautiful"] == ["2", "3", "3"])
check("[N OW1] → en_N-OW1", "en_N-OW1" in s["phoneme"].split())
check("sil → пауза", s["note_pitch"].split()[text.index("beautiful") + 3] == "0")
check("мелизм «-» → тип 3", s["note_type"].split()[text.index("here") + 1] == "3")
check("ARPAbet как в SoulX (you're → en_Y-UH1-R)", SC.g2p("you're") == ["Y", "UH1", "R"])
# битые meta-события (так пишут многие DAW): key signature 16 диезов / −8, пустой SMPTE, размер с нулями
import struct
raw = open(mid, "rb").read()
i = raw.index(b"MTrk")
n = struct.unpack(">I", raw[i + 4:i + 8])[0]
bad = (b"\x00\xff\x59\x02\x10\x01" + b"\x00\xff\x59\x02\xf8\x00" + b"\x00\xff\x54\x00"
       + b"\x00\xff\x58\x04\x00\x00\x00\x00")
bmid = os.path.join(d, "bad_meta.mid")
open(bmid, "wb").write(raw[:i + 4] + struct.pack(">I", n + len(bad)) + bad + raw[i + 8:])
sc2 = SC.read_midi(bmid)
segs2 = SC.to_meta(sc2)
check("битые key signature / SMPTE / размер пропущены, ноты и текст те же",
      len(sc2.notes) == 17 and segs2[0]["text"] == segs[0]["text"], str(sc2.warnings))
check("предупреждение о пропущенных событиях", any("Битые служебные" in w for w in sc2.warnings), str(sc2.warnings))
bad2 = os.path.join(d, "not_midi.mid")
open(bad2, "wb").write(b"RIFF....not a midi")
try:
    SC.read_midi(bad2)
    check("не-MIDI → понятная ошибка", False)
except ValueError as ex:
    check("не-MIDI → понятная ошибка по-русски", "Не удалось прочитать MIDI" in str(ex), str(ex)[:120])
print("ИТОГ:", "всё в порядке" if ok else "есть ошибки")
sys.exit(0 if ok else 1)
