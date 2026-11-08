# -*- coding: utf-8 -*-
"""DZOD Executor, E1 — MIDI-ноты с текстом → разметка SoulX-Singer (режим score).

Правила текста как у Omnivocal (ТЗ executor §1.2):
  • один слог на ноту; «beau-» «ti-» «ful» — слоги одного слова (дефис в конце = слово продолжается);
  • текст строкой в одной ноте («I wish you well») раскладывается по следующим нотам без текста;
  • «-» — тянуть гласную предыдущего слова на эту ноту (мелизм);
  • «[HH AH0 L OW1]» — фонемы ARPAbet напрямую (можно вперемешку с текстом);
  • sil / SIL — пауза; br / BR — вдох (у SoulX вдох = пауза);
  • «слог0» — шёпот, «слог!» — твёрдая атака, «cl» — смычка (тишина-затвор на ноте), «br» — вдох (E2: обработка
    после голоса вокалиста, флаги — поле dz_flag разметки).
Нота без текста и без очереди слов — продолжение предыдущего слова.

Формат SoulX (на сегмент): text, phoneme (en_A-B-C на слово, повтор на каждую ноту слова),
duration (с), note_pitch (MIDI, 0 — пауза), note_type (1 — пауза, 2 — начало слова, 3 — продолжение),
time [мс начала, мс конца] от начала MIDI, f0 — пусто (в режиме score высота — из нот)."""
import re
from dataclasses import dataclass, field

import mido

SP = "<SP>"
SPLIT_GAP = 2.0            # пауза длиннее — новый сегмент (как у SoulX)
SEG_MAX = 30.0             # сегмент длиннее — режем на ближайшей паузе
SIL_MIN = 0.02             # промежуток короче — не пауза, а легато (дотягиваем предыдущую ноту)
END_PAD = 0.4              # тишина в конце сегмента (контекст модели)
LEAD_PAD = 0.2             # тишина перед первой нотой сегмента (как в примерах SoulX)
ARPA = {"AA", "AE", "AH", "AO", "AW", "AY", "B", "CH", "D", "DH", "EH", "ER", "EY", "F", "G", "HH", "IH", "IY",
        "JH", "K", "L", "M", "N", "NG", "OW", "OY", "P", "R", "S", "SH", "T", "TH", "UH", "UW", "V", "W", "Y", "Z", "ZH"}
_g2p = None


def g2p(word):
    """Слово → список ARPAbet (CMUdict, для незнакомых слов — нейросеть g2p_en)."""
    global _g2p
    if _g2p is None:
        from g2p_en import G2p
        _g2p = G2p()
    w = re.sub(r"[^A-Za-z']", "", word).lower()
    if not w:
        return []
    return [p for p in _g2p(w) if p.rstrip("012") in ARPA]


def parse_phonemes(s):
    out = []
    for p in s.replace(",", " ").split():
        p = p.upper()
        if p.rstrip("012") not in ARPA:
            raise ValueError(f"Неизвестная фонема «{p}». Допустимы ARPAbet: {' '.join(sorted(ARPA))} (гласные с ударением 0/1/2).")
        if p.rstrip("012") in ("AA", "AE", "AH", "AO", "AW", "AY", "EH", "ER", "EY", "IH", "IY", "OW", "OY", "UH", "UW") and p[-1] not in "012":
            p += "1"
        out.append(p)
    return out


@dataclass
class Note:
    start: float
    end: float
    pitch: int
    lyric: str = ""
    vel: int = 100


@dataclass
class Score:
    notes: list
    warnings: list = field(default_factory=list)
    length: float = 0.0
    bends: list = field(default_factory=list)      # [(сек, -8192..8191)] — колесо высоты (pitch bend)


_BROKEN_META = []


def _tolerant_meta():
    """Битые meta-события (key signature «16 диезов», пустые SMPTE и т. п. — так пишут многие DAW)
    не роняют чтение: становятся «неизвестными» и пропускаются. Executor нужны только ноты, текст и темп."""
    import mido.midifiles.midifiles as MF
    from mido.midifiles.meta import UnknownMetaMessage, build_meta_message
    if getattr(MF, "_dzod_tolerant", False):
        return

    def safe_build(meta_type, data, delta=0):
        try:
            return build_meta_message(meta_type, data, delta)
        except Exception as ex:  # noqa: BLE001
            _BROKEN_META.append(f"0x{meta_type:02X}: {ex}")
            msg = UnknownMetaMessage(meta_type, data)
            msg.time = delta
            return msg
    MF.build_meta_message = safe_build
    MF._dzod_tolerant = True


def read_midi(path):
    """Ноты (монофония, по времени) и текст из lyrics/text-событий; учитывает смену темпа."""
    _tolerant_meta()
    _BROKEN_META.clear()
    try:
        mid = mido.MidiFile(path, clip=True)
    except (OSError, EOFError, ValueError, KeyError, IndexError, TypeError) as ex:
        raise ValueError(f"Не удалось прочитать MIDI «{path}»: файл повреждён или это не MIDI ({ex}). "
                         "Сохраните MIDI заново из DAW (тип 0 или 1) или пойте через DZOD Studio.") from ex
    tpb = mid.ticks_per_beat
    tempo_map = [(0, 500000)]
    events = []                                  # (tick, kind, data)
    for tr in mid.tracks:
        t = 0
        for msg in tr:
            t += msg.time
            if msg.type == "set_tempo":
                tempo_map.append((t, msg.tempo))
            elif msg.type in ("lyrics", "text"):
                txt = msg.text
                try:
                    txt = txt.encode("latin1").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
                events.append((t, msg.type, txt.strip()))
            elif msg.type == "note_on" and msg.velocity > 0:
                events.append((t, "on", (msg.note, msg.velocity)))
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                events.append((t, "off", msg.note))
            elif msg.type == "pitchwheel":
                events.append((t, "bend", msg.pitch))
    tempo_map.sort()

    def sec(tick):
        s, last_t, last_tempo = 0.0, 0, 500000
        for tt, tempo in tempo_map:
            if tt > tick:
                break
            s += (tt - last_t) / tpb * last_tempo / 1e6
            last_t, last_tempo = tt, tempo
        return s + (tick - last_t) / tpb * last_tempo / 1e6

    # текст нот — события lyrics; если их нет (некоторые редакторы) — text-события
    lyr_kind = "lyrics" if any(e[1] == "lyrics" for e in events) else "text"
    bends = sorted((t, d) for t, k, d in events if k == "bend")
    events = [e for e in events if e[1] in ("on", "off", lyr_kind)]
    events = [(t, "lyric" if k == lyr_kind else k, d) for t, k, d in events]
    events.sort(key=lambda e: (e[0], {"off": 0, "lyric": 1, "on": 2}[e[1]]))
    open_, notes, lyrics = {}, [], []
    for t, kind, d in events:
        if kind == "on":
            open_[d[0]] = (t, d[1])
        elif kind == "off" and d in open_:
            t0, vel = open_.pop(d)
            if t > t0:
                notes.append([t0, t, d, vel])
        elif kind == "lyric" and d:
            lyrics.append((t, d))
    notes.sort()
    tol = max(1, tpb // 32)
    res, li = [], 0
    for t0, t1, p, vel in notes:
        while li < len(lyrics) and lyrics[li][0] < t0 - tol:
            li += 1
        lyr = ""
        if li < len(lyrics) and abs(lyrics[li][0] - t0) <= tol:
            lyr = lyrics[li][1]
            li += 1
        res.append(Note(sec(t0), sec(t1), p, lyr, vel))
    sc = Score(res)
    sc.bends = [(sec(t), v) for t, v in bends]
    if _BROKEN_META:
        kinds = {"0x59": "тональность (key signature)", "0x58": "размер (time signature)", "0x54": "SMPTE offset"}
        names = sorted({kinds.get(b[:4], "meta " + b[:4]) for b in _BROKEN_META})
        sc.warnings.append(f"Битые служебные события MIDI пропущены: {', '.join(names)} — на пение не влияет.")
    sc.length = max((n.end for n in res), default=0.0)
    # монофония: наложения обрезаем по началу следующей ноты
    for a, b in zip(res, res[1:]):
        if a.end > b.start:
            a.end = b.start
            sc.warnings.append(f"Ноты накладываются в {b.start:.2f} с — предыдущая укорочена (Executor поёт одним голосом).")
    sc.notes = [n for n in res if n.end - n.start > 0.01]
    return sc


def words_for_notes(notes, warnings, flags=None):
    """Ноты → [(текст слова, фонемы, тип)] на каждую ноту по правилам Omnivocal.
    flags (dict) заполняется: индекс ноты → whisper | attack | breath | closure."""
    flags = {} if flags is None else flags
    out, queue, prev = [], [], None        # prev = (word, phones) текущего слова
    pending = None                         # слово с дефисом: собираем слоги
    for ni, n in enumerate(notes):
        lyr = n.lyric.strip()
        if not lyr and queue:
            lyr = queue.pop(0)
        elif lyr and " " in lyr and not lyr.startswith("["):
            parts = lyr.split()
            lyr, queue = parts[0], parts[1:] + queue
        if lyr in ("sil", "SIL", "br", "BR"):
            if lyr.lower() == "br":
                flags[ni] = "breath"
            out.append(("sil", None, 1))
            prev = pending = None
            continue
        if lyr == "-" or not lyr:
            if prev is None:
                warnings.append(f"Нота {n.start:.2f} с без текста в начале фразы — пою «la».")
                prev = ("la", g2p("la"))
                out.append((prev[0], prev[1], 2))
            else:
                out.append((prev[0], prev[1], 3))
            continue
        for mark, what in (("0", "whisper"), ("!", "attack")):
            if lyr.endswith(mark) and len(lyr) > 1 and not lyr.startswith("["):
                lyr = lyr[:-1]
                flags[ni] = what
        if lyr.lower() == "cl":
            flags[ni] = "closure"          # смычка: затвор — тишина на длину ноты
            out.append(("sil", None, 1))
            prev = None
            continue
        m = re.fullmatch(r"(.*?)\[([^\]]+)\](.*)", lyr)
        if m:
            phones = parse_phonemes(m.group(2))
            word = (m.group(1) + m.group(3)).strip("-") or "[" + "_".join(phones) + "]"   # без пробелов: text делится по пробелам
            prev = (word, phones)
            pending = None
            out.append((word, phones, 2))
            continue
        cont = lyr.endswith("-") and len(lyr) > 1
        syl = lyr.rstrip("-")
        if pending is not None:
            pending["syl"].append(syl)
            pending["idx"].append(len(out))
            out.append(None)
            if not cont:
                word = "".join(pending["syl"])
                phones = g2p(word)
                for k, i in enumerate(pending["idx"]):
                    out[i] = (word, phones, 2 if k == 0 else 3)
                prev, pending = (word, phones), None
            continue
        if cont:
            pending = {"syl": [syl], "idx": [len(out)]}
            out.append(None)
            continue
        phones = g2p(syl)
        if not phones:
            warnings.append(f"Не удалось прочитать «{syl}» ({n.start:.2f} с) — пою «la».")
            phones = g2p("la")
        prev = (syl, phones)
        out.append((syl, phones, 2))
    if pending is not None:          # слово с дефисом в конце партии
        word = "".join(pending["syl"])
        phones = g2p(word)
        for k, i in enumerate(pending["idx"]):
            out[i] = (word, phones, 2 if k == 0 else 3)
    if queue:
        warnings.append(f"Слов больше, чем нот: не спето «{' '.join(queue)}».")
    return out


def to_meta(score, language="English"):
    """Score → список сегментов SoulX-Singer."""
    flags = {}
    words = words_for_notes(score.notes, score.warnings, flags)
    items = []                                    # (start, end, text, phone, pitch, type, velocity, флаг)
    t = 0.0
    for ni, (n, (w, ph, ty)) in enumerate(zip(score.notes, words)):
        if n.start - t > SIL_MIN:
            items.append([t, n.start, SP, SP, 0, 1, 0, "-"])
        elif items and n.start > t:
            items[-1][1] = n.start                 # легато: закрыть микропромежуток
        fl = flags.get(ni, "-")
        if w == "sil":
            items.append([n.start, n.end, SP, SP, 0, 1, 0, fl])
        else:
            items.append([n.start, n.end, w, "en_" + "-".join(ph), n.pitch, ty, n.vel, fl])
        t = n.end
    segs, cur = [], []
    last_end = [0.0]

    def flush():
        while cur and cur[0][2] == SP:
            cur.pop(0)
        while cur and cur[-1][2] == SP:
            cur.pop()
        if not cur:
            return
        start, end = cur[0][0], cur[-1][1]
        lead = max(0.0, min(LEAD_PAD, start - last_end[0]))
        rows = ([[start - lead, start, SP, SP, 0, 1, 0, "-"]] if lead > 0.01 else []) + cur + [[end, end + END_PAD, SP, SP, 0, 1, 0, "-"]]
        start -= lead if lead > 0.01 else 0.0
        last_end[0] = end + END_PAD
        segs.append({
            "index": f"executor_{len(segs)}",
            "language": language,
            "time": [int(round(start * 1000)), int(round((end + END_PAD) * 1000))],
            "duration": " ".join(f"{r[1] - r[0]:.3f}" for r in rows),
            "text": " ".join(r[2] for r in rows),
            "phoneme": " ".join(r[3] for r in rows),
            "note_pitch": " ".join(str(r[4]) for r in rows),
            "note_type": " ".join(str(r[5]) for r in rows),
            "f0": "",
            "dz_velocity": " ".join(str(r[6]) for r in rows),     # для «Velocity → Power» (SoulX эти поля не читает)
            "dz_flag": " ".join(r[7] for r in rows),
        })
        cur.clear()

    for it in items:
        if it[2] == SP and it[1] - it[0] >= SPLIT_GAP:
            flush()
            continue
        if cur and it[2] == SP and it[1] - cur[0][0] > SEG_MAX:
            flush()
            continue
        cur.append(it)
    flush()
    return segs


# ------------------------------------------------------------------ E2: кривая высоты для стиля Straight / вибрато / pitch bend
FPS = 50.0                  # кадры SoulX: 24000 / 480


def notes_timeline(score):
    """Ноты для обработки после голоса и для кривой высоты: start, end, pitch, vel, flag, word."""
    flags = {}
    words = words_for_notes(score.notes, [], flags)
    out = []
    for i, (n, (w, _ph, _ty)) in enumerate(zip(score.notes, words)):
        silent = w == "sil"
        out.append({"start": round(n.start, 4), "end": round(n.end, 4), "pitch": 0 if silent else n.pitch,
                    "vel": n.vel, "flag": flags.get(i, ""), "word": "" if silent else w})
    return out


def curve_at(points, t, default):
    """Кривая [[время, значение], …] (линейно, по краям — крайние значения) в моментах t (numpy)."""
    import numpy as np
    if not points:
        return np.full(np.shape(t), float(default))
    pts = sorted((float(a), float(b)) for a, b in points)
    return np.interp(t, [a for a, _ in pts], [b for _, b in pts])


def in_ranges(ranges, t):
    import numpy as np
    m = np.zeros(np.shape(t), bool)
    for r in ranges or ():
        m |= (t >= r[0]) & (t < r[1])
    return m


def build_f0(seg, notes, bends=(), bend_range=0, vibrato=50, vib_rate=5.5, vib_delay=0.25, glide=0.06,
             vib_curve=None, pitch_curve=None, spoken=None):
    """Кривая высоты (Гц, по кадрам SoulX) для сегмента: ноты как написаны, плавный переход между нотами легато,
    вибрато (глубина: vibrato 0–100 % → 0–±80 центов, частота vib_rate Гц, начало через vib_delay с) и pitch bend
    (диапазон bend_range полутонов). Возвращает строку для поля f0 разметки."""
    import numpy as np
    t0 = seg["time"][0] / 1000.0
    durs = [float(x) for x in seg["duration"].split()]
    n_fr = int(round(sum(durs) * FPS))
    t = t0 + (np.arange(n_fr) + 0.5) / FPS
    midi = np.zeros(n_fr)
    voiced = np.zeros(n_fr, bool)
    vib = np.zeros(n_fr)
    sung = [x for x in notes if x["pitch"] > 0 and x["end"] > t0 and x["start"] < t[-1] + 1]
    depth = max(0.0, min(100.0, vibrato)) / 100.0 * 0.8           # полутоны
    depth_t = (np.clip(curve_at(vib_curve, t, vibrato), 0, 100) / 100.0 * 0.8) if vib_curve else np.full(n_fr, depth)
    if vib_curve:
        depth = max(depth, float(depth_t.max()))
    for i, x in enumerate(sung):
        m = (t >= x["start"]) & (t < x["end"])
        midi[m] = x["pitch"]
        voiced[m] = True
        dur = x["end"] - x["start"]
        if depth > 0 and dur > vib_delay + 0.15:
            tt = t[m] - x["start"]
            ramp = np.clip((tt - vib_delay) / 0.3, 0, 1) * np.clip((x["end"] - t[m]) / 0.08, 0, 1)
            vib[m] = depth_t[m] * ramp * np.sin(2 * np.pi * vib_rate * np.maximum(tt - vib_delay, 0))
        # переход от предыдущей ноты легато
        if i and abs(sung[i - 1]["end"] - x["start"]) < 0.02:
            a = x["start"] - glide / 2
            g = (t >= a) & (t < a + glide)
            k = (t[g] - a) / glide
            midi[g] = sung[i - 1]["pitch"] + (x["pitch"] - sung[i - 1]["pitch"]) * (0.5 - 0.5 * np.cos(np.pi * k))
            voiced[g] = True
    if spoken:
        sp = in_ranges(spoken, t)
        vib[sp] = 0.0
        for x in sung:
            m = sp & (t >= x["start"]) & (t < x["end"])
            if m.any():
                k = (t[m] - x["start"]) / max(x["end"] - x["start"], 1e-3)
                midi[m] = x["pitch"] - 2.0 * k                     # речевой спад к концу слога
    if pitch_curve:
        midi = midi + curve_at(pitch_curve, t, 0.0) / 100.0
    if bends and bend_range:
        bt = np.array([b[0] for b in bends])
        bv = np.array([b[1] for b in bends]) / 8192.0 * bend_range
        idx = np.searchsorted(bt, t, side="right") - 1
        midi = midi + np.where(idx >= 0, bv[np.clip(idx, 0, None)], 0.0)
    f0 = np.where(voiced, 440.0 * 2 ** ((midi + vib - 69) / 12.0), 0.0)
    return " ".join(f"{v:.1f}" for v in f0)
