# -*- coding: utf-8 -*-
"""DZOD Vocalist — «MIDI из вокала» (ТЗ shared/docs/studio/specs/vocal-to-midi_RU.md). Окружение Applio:

    python transcribe_worker.py job.json

Окружение — Executor (torch/torchaudio, librosa, nltk + CMUdict). Высота — RMVPE из Applio (тот же, что в RVC):
код rmvpe_py (rvc/lib/predictors/RMVPE.py) и веса rmvpe; без них — pYIN.
Задание: {"input": WAV, "lyrics": текст песни, "language": "auto"|"en"|"ru", "detail": "notes"|"detailed",
          "out_json": путь, "midi_out": путь|null, "rmvpe": путь к rmvpe.pt|null, "rmvpe_py": путь к RMVPE.py|null,
          "device": "cuda"|"cpu", "align_model_dir": папка для модели выравнивания (torch hub)}
Шаги: 1) высота RMVPE (как в RVC) или pYIN; 2) выравнивание текста по звуку — CTC wav2vec2 (torchaudio MMS_FA,
многоязычная, EN и RU через латиницу); 3) слоги (EN — по гласным с правилами, RU — по гласным); 4) ноты по высоте
внутри слогов: подъезды и вибрато не дробятся, распев на несколько нот — «-»; слог без голоса — короткая нота spoken.
Строки: @@P <доля> <текст> / @@R <json> / @@E <текст> / @@H."""
import json
import os
import re
import sys
import threading
import time
import traceback

_lock = threading.Lock()


def say(kind, payload):
    with _lock:
        sys.stdout.write(f"@@{kind} {payload}\n")
        sys.stdout.flush()


# ------------------------------------------------------------------ текст: слова, слоги, латиница для выравнивания
RU = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
RU_LAT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh", "з": "z", "и": "i",
          "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
          "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
          "э": "e", "ю": "yu", "я": "ya"}
VOW_RU = set("аеёиоуыэюя")
VOW_EN = set("aeiouy")


def words_pos(lyrics):
    """[(слово как в тексте, строка, начало, конец)] — начало/конец — позиция символов в lyrics (для пометок по вхождению).
    Только слова с буквами; метки частей песни — [Verse 1], [Chorus], (x2), {Bridge} — и пустые строки не поются."""
    out = []
    li = -1
    pos = 0
    tag = re.compile(r"\[[^\]]*\]|\{[^}]*\}|\((?:x|х)?\s*\d+\s*(?:x|х|раза?)?\)", re.I)
    for line in lyrics.split("\n"):
        clean = tag.sub(lambda m: " " * len(m.group()), line)
        if re.search(r"[A-Za-zА-Яа-яЁё]", clean):
            li += 1
            for m in re.finditer(r"[A-Za-zА-Яа-яЁё']+(?:-[A-Za-zА-Яа-яЁё']+)*", clean):
                w = m.group()
                a = m.start() + (len(w) - len(w.lstrip("'")))
                w = w.strip("'")
                if re.search(r"[A-Za-zА-Яа-яЁё]", w):
                    out.append((w, li, pos + a, pos + a + len(w)))
        pos += len(line) + 1
    return out


def words_of(lyrics):
    """[(слово как в тексте, строка)]."""
    return [(w, li) for w, li, _a, _b in words_pos(lyrics)]


def detect_lang(lyrics):
    lyrics = re.sub(r"\[[^\]]*\]", " ", lyrics)
    cyr = len(re.findall(r"[А-Яа-яЁё]", lyrics))
    lat = len(re.findall(r"[A-Za-z]", lyrics))
    return "ru" if cyr > lat else "en"


def romanize(word):
    """Слово → (латиница для MMS_FA, индекс исходной буквы для каждого символа латиницы)."""
    out, src = [], []
    for i, ch in enumerate(word.lower()):
        if ch in RU_LAT:
            r = RU_LAT[ch]
        elif "a" <= ch <= "z" or ch == "'":
            r = ch
        else:
            r = ""
        for c in r:
            if c == "'":
                continue
            out.append(c)
            src.append(i)
    return "".join(out), src


def syllables(word, lang):
    """Слово → список (начало, конец) по буквам исходного слова (конец не включён). Гласная = слог."""
    w = word.lower()
    vow = VOW_RU if lang == "ru" else VOW_EN
    letters = [(i, c) for i, c in enumerate(w) if c.isalpha()]
    if not letters:
        return []
    # ядра: в RU каждая гласная — слог; в EN — группа гласных («y» в начале слова — согласная)
    nuclei, i, silent = [], 0, set()
    while i < len(letters):
        c = letters[i][1]
        isv = c in vow and not (lang == "en" and c == "y" and i == 0)
        if isv:
            j = i
            if lang != "ru":
                while j + 1 < len(letters) and letters[j + 1][1] in vow:
                    j += 1
            nuclei.append((i, j))
            i = j + 1
        else:
            i += 1
    if lang == "en" and len(nuclei) > 1:
        a, b = nuclei[-1]
        if a == b == len(letters) - 1 and letters[a][1] == "e" and letters[a - 1][1] not in "l":
            nuclei.pop()                                     # make, love, tune — немое e
        elif (a == len(letters) - 2 and letters[a][1] == "e" and letters[-1][1] in "sd"
              and len(letters) > 3 and letters[a - 1][1] not in "td"):
            nuclei.pop()                                     # loves, played — es/ed без слога
    if lang == "en":
        # число слогов по словарю произношения CMUdict: лишние ядра — немые «e» внутри слова (some|thing, lone|ly)
        want = cmu_syllables(w)
        while want and len(nuclei) > want:
            cand = [k for k, (a, b) in enumerate(nuclei[:-1]) if a == b and letters[a][1] == "e" and k > 0] or \
                   [k for k, (a, b) in enumerate(nuclei) if a == b and letters[a][1] == "e"]
            if not cand:
                break
            silent.add(nuclei[cand[0]][0])
            nuclei.pop(cand[0])
        while want and len(nuclei) < want:
            grp = [k for k, (a, b) in enumerate(nuclei) if b > a]
            if not grp:
                break
            k = grp[-1]
            a, b = nuclei[k]
            nuclei[k:k + 1] = [(a, a), (a + 1, b)]             # be|ing, i|de|a
    if len(nuclei) <= 1:
        return [(letters[0][0], letters[-1][0] + 1)]
    # границы между ядрами: одна согласная — к следующему слогу; несколько — первая к предыдущему
    cuts = []
    for (a1, b1), (a2, _b2) in zip(nuclei, nuclei[1:]):
        se = [x for x in silent if b1 < x < a2]
        if se:
            b1 = max(se)                                     # немое e остаётся в предыдущем слоге: some|thing
        cons = a2 - b1 - 1
        cut = b1 + 1 if cons <= 1 else b1 + 2
        if lang == "en" and cons >= 2:
            pair = letters[b1 + 1][1] + letters[b1 + 2][1]
            if pair in ("th", "sh", "ch", "ph", "wh", "ck", "ng"):
                cut = b1 + 1 if pair != "ck" and pair != "ng" else b1 + 3
        if lang == "ru":
            # открытый слог: согласные — к следующему; й/ь/ъ и сонорный перед согласной — к предыдущему
            cut = b1 + 1
            cl = [letters[b1 + 1 + q][1] for q in range(cons)]
            soft = [q for q, c in enumerate(cl) if c in "йьъ"]
            if soft:
                cut = b1 + 2 + soft[-1]                       # толь|ко, объ|яв, май|ский
            elif cons >= 2 and cl[0] == cl[1]:
                cut = b1 + 2                                  # Рос|си|я
            elif cons >= 2 and cl[0] in "рлмн":
                cut = b1 + 2                                  # сер|дце
        cuts.append(min(max(cut, b1 + 1), a2))
    bounds = [0] + cuts + [len(letters)]
    return [(letters[bounds[k]][0], letters[bounds[k + 1] - 1][0] + 1) for k in range(len(bounds) - 1)]


_CMU = None


def cmu_syllables(word):
    """Число слогов по CMUdict (nltk), None — слова нет в словаре."""
    global _CMU
    if _CMU is None:
        try:
            from nltk.corpus import cmudict
            _CMU = cmudict.dict()
        except Exception:  # noqa: BLE001
            _CMU = {}
    pr = _CMU.get(word.lower().strip("'"))
    if not pr:
        return None
    return sum(1 for ph in pr[0] if ph[-1].isdigit())


# ------------------------------------------------------------------ звук
def load_mono16(path):
    import numpy as np
    import soundfile as sf
    import librosa
    x, sr = sf.read(path, dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    if sr != 16000:
        x = librosa.resample(x, orig_sr=sr, target_sr=16000)
    return np.ascontiguousarray(x, dtype=np.float32)


def f0_track(x16, job):
    """F0 по кадрам 10 мс (Гц, 0 — нет голоса)."""
    import numpy as np
    rp, code = job.get("rmvpe"), job.get("rmvpe_py")
    if rp and code and os.path.exists(rp) and os.path.exists(code):
        import importlib.util
        spec = importlib.util.spec_from_file_location("dzod_rmvpe", code)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        import torch
        dev = job.get("device") or ("cuda" if torch.cuda.is_available() else "cpu")
        m = mod.RMVPE0Predictor(rp, device=dev)
        f0 = m.infer_from_audio(x16, thred=0.03)
        return np.asarray(f0, dtype=np.float64), "rmvpe"
    import librosa
    f0, vflag, _ = librosa.pyin(x16, fmin=65, fmax=1100, sr=16000, frame_length=1024, hop_length=160)
    f0 = np.where(vflag & np.isfinite(f0), f0, 0.0)
    return f0, "pyin"


def align(x16, words, lang, job):
    """Принудительное выравнивание: для каждого слова — начало/конец каждой буквы (с) или None."""
    import torch
    import torchaudio
    from torchaudio.pipelines import MMS_FA as bundle
    dev = job.get("device") or "cpu"
    if job.get("align_model_dir"):
        torch.hub.set_dir(job["align_model_dir"])
    labels = bundle.get_labels(star="*")
    dic = {c: i for i, c in enumerate(labels)}
    if job.get("emis_in"):                        # отладка: готовые эмиссии (повтор без модели)
        import numpy as np
        emission = torch.from_numpy(np.load(job["emis_in"]))
    else:
        model = bundle.get_model(with_star=True).to(dev).eval()
        # эмиссии кусками по 30 с с перекрытием 1 с (память на длинных песнях)
        sr = bundle.sample_rate
        wav = torch.from_numpy(x16)
        chunk, ov = 30 * sr, sr
        em, pos = [], 0
        with torch.inference_mode():
            while pos < len(wav):
                a = max(0, pos - ov)
                seg = wav[a:pos + chunk].unsqueeze(0).to(dev)
                e, _ = model(seg)
                e = torch.log_softmax(e, dim=-1)[0].cpu()
                fr = e.shape[0] / seg.shape[1]          # кадров на отсчёт
                skip = int(round((pos - a) * fr))
                em.append(e[skip:])
                pos += chunk
        emission = torch.cat(em, 0)
    if job.get("emis_out"):
        import numpy as np
        np.save(job["emis_out"], emission.numpy().astype("float32"))
    n_fr = emission.shape[0]
    sec_per_fr = len(x16) / 16000.0 / n_fr
    roman, srcmap, ok = [], [], []
    for w, _li in words:
        r, src = romanize(w)
        r2 = "".join(c for c in r if c in dic)
        roman.append(r2)
        srcmap.append([s for c, s in zip(r, src) if c in dic])
        ok.append(bool(r2))
    # цепочка: * слово * слово … * (звёздочка поглощает лишнее: подпевки, проигрыши, слова не из текста)
    tokens, owner = [dic["*"]], [(-1, -1)]
    for wi, r in enumerate(roman):
        for ci, c in enumerate(r):
            tokens.append(dic[c])
            owner.append((wi, ci))
        if job.get("star", "between") == "between" or wi == len(roman) - 1:
            tokens.append(dic["*"])
            owner.append((-1, -1))
    if n_fr < len(tokens):
        raise RuntimeError("Звук слишком короткий для этого текста (или в тексте слишком много слов).")
    ali, scores = torchaudio.functional.forced_align(emission.unsqueeze(0), torch.tensor([tokens], dtype=torch.int32), blank=0)
    tspans = torchaudio.functional.merge_tokens(ali[0], scores[0].exp())
    if len(tspans) != len(tokens):
        raise RuntimeError(f"Выравнивание не сошлось ({len(tspans)} из {len(tokens)} букв).")
    spans = [[ts.start, ts.end - 1] for ts in tspans]
    tscore = [float(ts.score) for ts in tspans]
    letters, wscore = [], []
    for wi, (w, _li) in enumerate(words):
        sc = [tscore[k] for k, (ow, _ci) in enumerate(owner) if ow == wi]
        wscore.append(sum(sc) / len(sc) if sc else 0.0)
        lt = [None] * len(w)
        for k, (ow, ci) in enumerate(owner):
            if ow == wi and spans[k][0] is not None:
                li = srcmap[wi][ci]
                a, b = spans[k][0] * sec_per_fr, (spans[k][1] + 1) * sec_per_fr
                if lt[li] is None:
                    lt[li] = [a, b]
                else:
                    lt[li] = [min(lt[li][0], a), max(lt[li][1], b)]
        letters.append(lt)
    # слово, которого нет в звуке: выравнивание сжимает его в пару кадров с уверенностью намного ниже остальных
    med = sorted(wscore)[len(wscore) // 2] if wscore else 0
    for wi, lt in enumerate(letters):
        known = [x for x in lt if x]
        bad = not known                                             # слово без единого кадра
        if job.get("debug"):
            say("P", f"0.79 слово «{words[wi][0]}»: {known[0][0] if known else -1:.2f}–{known[-1][1] if known else -1:.2f} с, "
                     f"уверенность {wscore[wi]:.3f} (медиана {med:.3f}){' — нет в звуке' if bad else ''}")
        if bad:
            letters[wi] = [None] * len(lt)
    job["_wscore"] = wscore
    return letters


# ------------------------------------------------------------------ ноты
def to_midi(f):
    import numpy as np
    return np.where(f > 0, 12 * np.log2(np.maximum(f, 1e-6) / 440.0) + 69, np.nan)


def segment_notes(t0, t1, midi, fps, detailed):
    """Ноты внутри [t0, t1): [(начало, конец, высота-дробная)]. Подъезды/вибрато — одна нота."""
    import numpy as np
    a, b = int(t0 * fps), int(t1 * fps)
    m = midi[a:b]
    if len(m) == 0 or np.all(np.isnan(m)):
        return []
    # сглаживание 120 мс (вибрато), только по голосу
    k = max(1, min(len(m), int(0.12 * fps)))       # короткий отрезок — окно не длиннее него
    v = ~np.isnan(m)
    mm = np.where(v, m, 0.0)
    sm = np.convolve(mm, np.ones(k), "same") / np.maximum(np.convolve(v.astype(float), np.ones(k), "same"), 1e-9)
    sm[~v] = np.nan
    thr = 0.6 if detailed else 0.8
    min_len = int((0.06 if detailed else 0.12) * fps)
    notes, cur_start, ref, run = [], None, None, 0
    idx = np.flatnonzero(v)
    first, last = idx[0], idx[-1]
    cur_start = first
    sc = int(0.06 * fps)                                           # без подъезда первых 60 мс
    ref = np.nanmedian(sm[first + sc:min(last + 1, first + sc + int(0.15 * fps))]) if last - first > sc + 2 else np.nanmedian(sm[first:last + 1])
    i = first
    while i <= last:
        if not np.isnan(sm[i]) and abs(sm[i] - ref) > thr:
            run += 1
            if run >= min_len:
                cut = i - run + 1
                new = np.nanmedian(sm[cut:i + 1])
                # точная граница: где сырая высота переходит середину между нотами (сглаживание сдвигает на ~k/2)
                mid, sgn = (ref + new) / 2, np.sign(new - ref)
                lo, hi = max(cur_start + 1, cut - k), min(i + 1, cut + k)
                cross = [q for q in range(lo, hi) if not np.isnan(m[q]) and (m[q] - mid) * sgn > 0]
                if cross:
                    cut = cross[0]
                if cut - cur_start >= min_len:
                    notes.append((cur_start, cut))
                    cur_start = cut
                ref = new
                run = 0
        else:
            run = 0
        i += 1
    notes.append((cur_start, last + 1))
    out = []
    for s, e in notes:
        seg = m[s:e]
        seg = seg[~np.isnan(seg)]
        if len(seg) == 0:
            continue
        n = len(seg)
        core = seg[int(n * 0.25):max(int(n * 0.25) + 1, int(n * 0.9))]      # без подъезда в начале
        out.append([(a + s) / fps, (a + e) / fps, float(np.median(core))])
    # соседние с одинаковой нотой — слить
    merged = []
    for nt in out:
        if merged and round(merged[-1][2]) == round(nt[2]):
            merged[-1][1] = nt[1]
            merged[-1][2] = (merged[-1][2] + nt[2]) / 2
        else:
            merged.append(nt)
    return merged


def main():
    import numpy as np
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        job = json.load(f)

    def heartbeat():
        while True:
            time.sleep(3)
            say("H", "")
    threading.Thread(target=heartbeat, daemon=True).start()
    t_start = time.time()
    lyrics = job.get("lyrics") or ""
    lang = job.get("language") or "auto"
    if lang == "auto":
        lang = detect_lang(lyrics)
    words = words_of(lyrics)
    notes_only = not words                        # без текста — только ноты (слоги пустые)
    detailed = job.get("detail") == "detailed"
    say("P", "0.05 Читаю звук…")
    x16 = load_mono16(job["input"])
    dur = len(x16) / 16000.0
    say("P", "0.12 Высота голоса (F0)…")
    if job.get("f0_in"):                          # отладка: готовая высота (повтор без модели)
        f0, f0_kind = np.array(json.load(open(job["f0_in"])), dtype=float), "file"
    else:
        f0, f0_kind = f0_track(x16, job)
    if job.get("f0_out"):
        json.dump([float(v) if np.isfinite(v) else 0.0 for v in f0], open(job["f0_out"], "w"))
    fps = 100.0
    midi = to_midi(f0)
    say("P", "0.40 Привязываю текст к звуку (выравнивание)…")
    if notes_only:
        letters = []
    elif job.get("letters_in"):                     # отладка: готовое выравнивание (повтор без модели)
        letters = [[tuple(x) if x else None for x in lt] for lt in json.load(open(job["letters_in"], encoding="utf-8"))]
    else:
        letters = align(x16, words, lang, job)
    if job.get("align_out"):
        # перепевка с текстом: каждое слово текста — где в тексте, где в звуке (или null), уверенность
        ws = job.get("_wscore") or []
        al = []
        for wi, ((w, li, ca, cb), lt) in enumerate(zip(words_pos(lyrics), letters)):
            known = [x for x in lt if x]
            al.append({"text": w, "line": li, "from": ca, "to": cb,
                       "t0": round(known[0][0], 3) if known else None, "t1": round(known[-1][1], 3) if known else None,
                       "letters": [[round(x[0], 3), round(x[1], 3)] if x else None for x in lt],
                       "score": round(ws[wi], 4) if wi < len(ws) else None})
        json.dump({"language": lang, "words": al}, open(job["align_out"], "w", encoding="utf-8"), ensure_ascii=False)
    if job.get("letters_out"):
        json.dump([[list(x) if x else None for x in lt] for lt in letters], open(job["letters_out"], "w", encoding="utf-8"))
    say("P", "0.80 Слоги и ноты…")
    # RMS для velocity
    hop = 160
    rms = np.sqrt(np.convolve(x16 ** 2, np.ones(hop * 3) / (hop * 3), "same")[::hop] + 1e-12)
    notes, wout = [], []
    syl_list = []                                 # (начало, конец, текст слога, индекс слова, слог в слове)
    for wi, ((w, li), lt) in enumerate(zip(words, letters)):
        known = [x for x in lt if x is not None]
        if not known:
            continue
        wout.append({"text": w, "t": round(known[0][0], 3), "dur": round(known[-1][1] - known[0][0], 3), "line": li})
        syl = syllables(w, lang)
        for si, (a, b) in enumerate(syl):
            span = [lt[i] for i in range(a, b) if lt[i] is not None]
            if not span:
                continue
            syl_list.append([span[0][0], span[-1][1], w[a:b], len(wout) - 1, si, len(syl)])
    # 1) ноты — по голосу, независимо от текста: участки голоса (паузы ≥ 30 мс делят), внутри — смена высоты
    voiced = ~np.isnan(midi)
    regions, k0 = [], None
    gap = int(0.03 * fps)
    for k in range(len(voiced) + 1):
        v = k < len(voiced) and voiced[k]
        if v and k0 is None:
            k0 = k
        elif not v and k0 is not None:
            # короткая дырка — не пауза
            if k < len(voiced) and np.any(voiced[k:k + gap]):
                continue
            regions.append((k0 / fps, k / fps))
            k0 = None
    raw = []
    for a, b in regions:
        if b - a < 0.05:
            continue
        raw.extend(segment_notes(a, b, midi, fps, detailed))
    # октавные ошибки высоты: короткая нота на октаву от обеих соседних (а соседние близки между собой) — сдвиг на октаву
    for q in range(1, len(raw) - 1):
        a, b, pch = raw[q]
        pa, pb = raw[q - 1][2], raw[q + 1][2]
        near = raw[q][0] - raw[q - 1][1] < 0.3 and raw[q + 1][0] - raw[q][1] < 0.3
        if near and b - a < 0.35 and abs(pa - pb) <= 5:
            mid = (pa + pb) / 2
            for k in (-12, 12):
                if abs(pch + k - mid) <= 4 and abs(pch - mid) >= 9:
                    raw[q] = (a, b, pch + k)
                    break
    # тихие «ноты» (вдох, шум, хвост ревера) — не голос: на 25 дБ тише голоса или очень низкие при тихом уровне
    if raw:
        rdb = 20 * np.log10(rms + 1e-9)
        lv = [float(np.mean(rdb[int(a * fps):max(int(a * fps) + 1, int(b * fps))])) for a, b, _p in raw]
        ref_lv = float(np.percentile(lv, 75))
        raw = [nt for nt, l in zip(raw, lv) if l > ref_lv - 25 and not (nt[2] < 40 and l < ref_lv - 12)]
    # 2) слоги к нотам. Нота слога — та, что начинается рядом с началом слога (за 350 мс до — выравнивание по пению
    #    часто запаздывает; до 150 мс после); иначе нота, внутри которой слог начался: первый слог берёт её целиком,
    #    остальные делят её по началу слога (легато на той же высоте). Ноты без слога — распев «-»; слог без голоса — spoken.
    notes_v = [[a, b, pch, None] for a, b, pch in raw]
    # сила согласной в начале ноты: новый слог в пении почти всегда начинается с согласной — провал громкости
    # или короткая потеря голоса; смена высоты внутри гласной (распев) провала не даёт
    db = 20 * np.log10(rms + 1e-9)
    nf = len(db)

    def onset_ev(t):
        k = int(round(t * fps))
        if k <= 3 or k >= nf - 3:
            return 1.0
        w = db[max(0, k - 6):min(nf, k + 4)]
        left = db[max(0, k - 25):max(1, k - 3)]
        right = db[min(nf - 1, k + 2):min(nf, k + 25)]
        base = min(float(np.max(left)), float(np.max(right))) if len(left) and len(right) else float(np.max(w))
        depth = base - float(np.min(w))
        vk = voiced[max(0, k - 6):min(len(voiced), k + 4)]
        if len(vk) and not np.all(vk):
            depth += 6.0
        return float(np.clip((depth - 2.0) / 6.0, 0.0, 1.0))

    notes_v = [[a, b, pch, None, 0.0, False] for a, b, pch in raw]
    # легато на той же высоте: внутри ноты — провалы (согласные) → кандидаты-кусочки; без слога вернутся в ноту
    extra = []
    for nv in notes_v:
        k0, k1 = int((nv[0] + 0.16) * fps), int((nv[1] - 0.16) * fps)   # у краёв ноты согласная — её начало
        k = k0
        while k < k1:
            t = k / fps
            e = onset_ev(t)
            if e >= 0.6:
                seg = db[max(0, k - 4):k + 5]
                kk = max(0, k - 4) + int(np.argmin(seg))
                extra.append((kk / fps, e))
                k = kk + int(0.12 * fps)
            else:
                k += 1
    for t, e in extra:
        for q, nv in enumerate(notes_v):
            if nv[0] + 0.1 < t < nv[1] - 0.1:
                notes_v.insert(q + 1, [t, nv[1], nv[2], None, e, True])
                nv[1] = t
                break
    notes_v.sort(key=lambda nv: nv[0])
    for q, nv in enumerate(notes_v):
        if not nv[5]:
            gap_before = q == 0 or nv[0] - notes_v[q - 1][1] > 0.025
            # согласная может стоять чуть раньше или позже смены высоты
            nv[4] = 1.0 if gap_before else max(onset_ev(nv[0] + d / fps) for d in range(-5, 13, 2))
    # слоги к нотам — монотонное сопоставление (динамическое программирование). Цена: расхождение начала ноты
    # с началом слога по выравниванию (несимметрично: выравнивание по пению запаздывает) + нота с согласной без слога
    # (почти всегда это новый слог) + слог без ноты (spoken)
    if os.environ.get("DZ_TR_DEBUG"):
        print("NOTES", " ".join(f"{nv[2]:.0f}@{nv[0]:.2f}/ev{nv[4]:.2f}" for nv in notes_v), file=sys.stderr)
        print("SYLS", " ".join(f"{sy[2]}@{sy[0]:.2f}" for sy in syl_list), file=sys.stderr)
    n_s, n_n = len(syl_list), len(notes_v)
    G, SP, WIN = 1.0, 0.8, 40
    TE, TL, TOL = 0.3, 2.0, 0.05

    def tcost(d):
        """d = начало ноты − начало слога по выравниванию. По пению выравнивание запаздывает, но почти не спешит:
        нота раньше — дёшево, нота позже (больше 50 мс) — дорого."""
        if d <= TOL:
            return TE * min(1.2, abs(d))
        return TE * TOL + TL * min(1.2, d - TOL)
    pen = [0.0] * (n_n + 1)
    for q in range(n_n):
        pen[q + 1] = pen[q] + G * notes_v[q][4]             # накопленный штраф ничейных нот с согласной
    INF = 1e18
    best = [[INF] * (n_n + 1) for _ in range(n_s + 1)]     # best[i][j]: слоги < i расставлены, последняя занятая нота < j
    back = [[None] * (n_n + 1) for _ in range(n_s + 1)]
    best[0] = [0.0] * (n_n + 1)                              # ноты до первого слога — без штрафа (вдох, призвук)
    for i in range(n_s):
        t0 = syl_list[i][0]
        for j in range(n_n + 1):
            if best[i][j] >= INF:
                continue
            # слог i — spoken (ноту не занимает)
            c = best[i][j] + SP
            if c < best[i + 1][j]:
                best[i + 1][j] = c
                back[i + 1][j] = (j, None)
            # слог i — на ноту q ≥ j (ноты j..q-1 — распев предыдущего слога)
            for q in range(j, min(n_n, j + WIN)):
                skip = (pen[q] - pen[j]) if i > 0 else 0.0
                c = best[i][j] + skip + tcost(notes_v[q][0] - t0)
                if c < best[i + 1][q + 1]:
                    best[i + 1][q + 1] = c
                    back[i + 1][q + 1] = (j, q)
    # ноты после последнего слога — распев, после паузы — штраф
    end_j = min(range(n_n + 1), key=lambda j: best[n_s][j] + (pen[n_n] - pen[j]) if best[n_s][j] < INF else INF)
    spoken_add = []
    j = end_j
    for i in range(n_s, 0, -1):
        pj, q = back[i][j]
        sy = syl_list[i - 1]
        if q is None:
            spoken_add.append([sy[0], max(sy[0] + 0.08, min(sy[1], sy[0] + 0.25)), None, i - 1])
        else:
            notes_v[q][3] = i - 1
        j = pj
    # кусочки легато без слога — обратно в предыдущую ноту
    for q in range(len(notes_v) - 1, 0, -1):
        nv = notes_v[q]
        if nv[5] and nv[3] is None and abs(notes_v[q - 1][1] - nv[0]) < 1e-6:
            notes_v[q - 1][1] = nv[1]
            del notes_v[q]
    # голос до первого слога: примыкает — к первой ноте слога, иначе отбрасывается (вдох, призвук)
    first = next((q for q, nv in enumerate(notes_v) if nv[3] is not None), None)
    if first:
        for q in range(first - 1, -1, -1):
            if notes_v[q][1] >= notes_v[first][0] - 0.05 and round(notes_v[q][2]) == round(notes_v[first][2]):
                notes_v[first][0] = notes_v[q][0]
            notes_v[q][3] = "drop"
    split = [nv[:3] for nv in notes_v if nv[3] != "drop"] + spoken_add
    owner = [nv[3] for nv in notes_v if nv[3] != "drop"] + [None] * len(spoken_add)
    prev_pitch, cur_syl = None, None
    order = sorted(range(len(split)), key=lambda q: split[q][0])
    for q in order:
        nt = split[q]
        spoken = len(nt) > 3
        si = nt[3] if spoken else owner[q]
        a, b, pch = nt[0], nt[1], nt[2]
        if spoken:
            pch = prev_pitch if prev_pitch is not None else (float(np.nanmedian(midi)) if np.any(voiced) else 60.0)
        if si is not None:
            sy = syl_list[si]
            text, wi, sidx, nsyl = sy[2], sy[3], sy[4], sy[5]
            label = text if nsyl == 1 else (text + "-" if sidx < nsyl - 1 else text)
            if not spoken and a - 0.08 <= sy[0] < a:
                a = sy[0]                                               # нота с согласной слога (не дальше 80 мс)
            cur_syl = (wi,)
        else:
            if notes_only:
                label, wi = "", -1
            elif cur_syl is None:
                continue                                                # голос до первого слова (вдох, призвук)
            else:
                label, wi = "-", cur_syl[0]
        ra, rb = int(a * fps), max(int(a * fps) + 1, int(b * fps))
        lv = 20 * np.log10(float(np.mean(rms[ra:rb])) + 1e-9)
        vel = int(np.clip(30 + (lv + 40) / 34 * 90, 1, 127))
        notes.append({"t": round(a, 3), "dur": round(max(0.03, b - a), 3), "pitch": int(round(pch)),
                      "cents": int(round((pch - round(pch)) * 100)), "vel": vel, "syl": label, "word": wi,
                      "spoken": spoken})
        prev_pitch = pch
    # наложения нот: обрезать по следующей
    notes.sort(key=lambda n: n["t"])
    for a, b in zip(notes, notes[1:]):
        if a["t"] + a["dur"] > b["t"]:
            a["dur"] = round(max(0.03, b["t"] - a["t"]), 3)
    starts = [w["t"] for w in wout] if wout else [n["t"] for n in notes]
    ioi = np.diff(starts) if len(starts) > 2 else np.array([0.5])
    med = float(np.median(ioi[ioi > 0.08])) if np.any(ioi > 0.08) else 0.5
    bpm = 60.0 / med
    while bpm < 70:
        bpm *= 2
    while bpm > 160:
        bpm /= 2
    res = {"version": 1, "language": lang, "detail": "detailed" if detailed else "notes", "f0": f0_kind,
           "notes": notes, "words": wout, "tempo_hint": round(bpm, 1), "seconds": round(dur, 2),
           "missing_words": [w for (w, _l), lt in zip(words, letters) if not any(lt)],
           "t_total": round(time.time() - t_start, 1)}
    with open(job["out_json"], "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    if job.get("midi_out"):
        write_midi(job["midi_out"], notes, res["tempo_hint"])
    say("P", "0.99 Готово.")
    say("R", json.dumps({"out_json": job["out_json"], "notes": len(notes), "words": len(wout), "language": lang,
                         "f0": f0_kind, "tempo_hint": res["tempo_hint"], "t_total": res["t_total"],
                         "missing_words": res["missing_words"][:20]}, ensure_ascii=False))


def write_midi(path, notes, bpm, tpb=960):
    """MIDI тип 0: темп, ноты, текст нот (lyrics) — как читает DZOD Executor и DZOD Studio."""
    import struct
    tempo = int(60_000_000 / bpm)
    ev = []
    for n in notes:
        a = int(round(n["t"] * bpm / 60 * tpb))
        b = max(a + 1, int(round((n["t"] + n["dur"]) * bpm / 60 * tpb)))
        ev.append((a, 1, ("lyric", n["syl"])))
        ev.append((a, 2, ("on", n["pitch"], n["vel"])))
        ev.append((b, 0, ("off", n["pitch"])))
    ev.sort(key=lambda e: (e[0], e[1]))

    def vlq(v):
        out = [v & 0x7F]
        v >>= 7
        while v:
            out.append((v & 0x7F) | 0x80)
            v >>= 7
        return bytes(reversed(out))
    data = bytearray(b"\x00\xff\x51\x03" + tempo.to_bytes(3, "big"))
    last = 0
    for t, _o, e in ev:
        data += vlq(t - last)
        last = t
        if e[0] == "lyric":
            txt = e[1].encode("utf-8")
            data += b"\xff\x05" + vlq(len(txt)) + txt
        elif e[0] == "on":
            data += bytes([0x90, e[1] & 0x7F, max(1, e[2]) & 0x7F])
        else:
            data += bytes([0x80, e[1] & 0x7F, 0])
    data += b"\x00\xff\x2f\x00"
    with open(path, "wb") as f:
        f.write(b"MThd" + struct.pack(">IHHH", 6, 0, 1, tpb) + b"MTrk" + struct.pack(">I", len(data)) + bytes(data))


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:  # noqa: BLE001
        traceback.print_exc()
        say("E", str(ex) or ex.__class__.__name__)
        sys.exit(1)
