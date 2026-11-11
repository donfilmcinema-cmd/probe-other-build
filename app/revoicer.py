# -*- coding: utf-8 -*-
"""DZOD Revoicer — облегчённый Vocalist для DZOD Studio (решения автора 27.10.2026 16:21 и 16:30).

Studio (кнопка DZOD Revoicer в Block Vocals) запускает:

    "DZOD Revoicer.exe" --studio C:\\...\\request.json

Запрос (UTF-8 JSON):
    {
      "version": 1,
      "input": "C:\\...\\slice.wav",              # звук выделенного Slice (если Slice — часть файла, Studio рендерит диапазон)
      "source_file": "D:\\...\\RIGOR 04 Vocal.wav", # исходный файл Slice: рядом ляжет результат, от него — имя
      "result": "C:\\...\\result.json",            # куда записать ответ
      "studio": {"line": "Vocal 1", "line_id": "...", "slice_start": 12.5, "slice_length": 31.2, ...}
                                                 # любые данные Studio — возвращаются в ответе как есть;
                                                 # line, slice_start, slice_length показываются в окне
    }
Окно открывается с этим исходником. Автор выбирает вокалиста, чистит, перепевает, слушает дубли
и нажимает «Вставить в Studio» у нужного дубля. Файл кладётся рядом с source_file:
    RIGOR 04 Vocal.wav → RIGOR 04 Vocal_Resinger01.wav (_Resinger02 … — следующие),
затем пишется ответ и окно закрывается:
    {"ok": true, "files": [{"role": "dzod_lead", "path": "D:\\...\\RIGOR 04 Vocal_Resinger01.wav"}],
     "voice": "Голос 1", "studio": {...как в запросе...}}
Закрыли окно без вставки: есть дубль — {"ok": true, "confirmed": false, "files": [...]} (последний или ★ лучший,
уже лежит рядом с исходником); дублей нет — {"ok": false, "cancelled": true, "studio": {...}}.
Результат — той же частоты и ровно той же длины, что input: Slice ставится на ту же позицию, задержка 0."""
import json
import os
import re
import shutil
import time
from pathlib import Path

import engine as E
import songs as S

SUFFIX = "_Resinger"


def read_request(path):
    p = Path(path)
    try:
        req = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as ex:  # noqa: BLE001
        raise E.EngineError(f"Не удалось прочитать запрос Studio: {ex}")
    src = req.get("input") or req.get("source_file")
    if not src or not Path(src).is_file():
        raise E.EngineError(f"Файл исходника не найден: {src}")
    req["input"] = str(src)
    req.setdefault("source_file", str(src))
    req.setdefault("result", str(p.with_name("result.json")))
    req["_request_path"] = str(p)
    return req


def next_name(source_file, out_dir=None):
    """RIGOR 04 Vocal.wav → <папка>/RIGOR 04 Vocal_Resinger01.wav (первый свободный номер)."""
    src = Path(source_file)
    d = Path(out_dir) if out_dir else src.parent
    base = src.stem
    m = re.match(r"^(.*)" + SUFFIX + r"\d{2,}$", base)
    if m:                                   # перепевают уже перепетый файл — номер продолжается от исходного имени
        base = m.group(1)
    k = 1
    while True:
        dst = d / f"{base}{SUFFIX}{k:02d}.wav"
        if not dst.exists():
            return dst
        k += 1


def place(song_id, take_id, out_dir=None):
    """Кладёт дубль рядом с исходным файлом под именем …_ResingerNN.wav."""
    s = S.get(song_id)
    t = next((x for x in s.m.get("takes", []) if x["id"] == take_id), None)
    if not t:
        raise E.EngineError("Дубль не найден.")
    src = (s.m.get("studio") or {}).get("source_file") or s.m.get("origin")
    if not src:
        raise E.EngineError("Неизвестно, где лежит исходный файл.")
    folder = Path(out_dir) if out_dir else Path(src).parent
    if not folder.is_dir():
        raise E.EngineError(f"Папка исходника недоступна: {folder}")
    dst = next_name(src, folder)
    shutil.copyfile(s.dir / t["file"], dst)
    t.setdefault("placed", []).append(str(dst))
    s.save()
    E.log(f"ДУБЛЬ ПОЛОЖЕН: {t['file']} → {dst}")
    return dst, t


def write_result(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _lyrics_for_line(req):
    """Текст песни из прошлых окон той же Line (тот же исходный файл или та же Line в том же Space)."""
    st = req.get("studio") or {}
    best, when = "", 0.0
    for d in E.songs_dir().iterdir():
        f = d / "song.json"
        if not f.exists():
            continue
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        ms = m.get("studio") or {}
        same = ms.get("source_file") == req.get("source_file") or (
            st.get("line") and (ms.get("info") or {}).get("line") == st.get("line")
            and (ms.get("info") or {}).get("space") == st.get("space"))
        if same and m.get("lyrics") and (ms.get("opened") or 0) > when:
            best, when = m["lyrics"], ms.get("opened") or 0
    return best


class Session:
    """Окно, открытое из DZOD Studio: один исходник, ответ — один раз."""

    def __init__(self, req):
        self.req = req
        self.answered = False
        song = S.create(req["input"], "stem", name=Path(req["source_file"]).stem)
        song.m["studio"] = {"source_file": req["source_file"], "info": req.get("studio") or {},
                            "opened": time.time()}
        # текст песни — с Line: из запроса Studio ("lyrics") или из прошлого окна той же Line
        song.m["lyrics"] = req.get("lyrics") or _lyrics_for_line(req) or ""
        song.save()
        self.song_id = song.id

    def view(self):
        st = self.req.get("studio") or {}
        return {"song": self.song_id, "source_file": self.req["source_file"], "line": st.get("line"),
                "slice_start": st.get("slice_start"), "slice_length": st.get("slice_length"),
                "slice": st.get("slice"), "sample_rate": st.get("sample_rate"), "space": st.get("space"),
                "source": st.get("source"), "range": st.get("range"),
                "preselect": {"voice": self.req.get("voice"), "convert": self.req.get("convert") or {}},
                "next_name": next_name(self.req["source_file"]).name}

    def insert(self, take_id):
        if self.answered:
            raise E.EngineError("Результат уже передан в Studio.")
        dst, t = place(self.song_id, take_id)
        write_result(self.req["result"], {"ok": True, "files": [{"role": "dzod_lead", "path": str(dst)}],
                                          "voice": t.get("voice_name"), "opts": t.get("opts"),
                                          "studio": self.req.get("studio") or {}})
        self.answered = True
        return {"path": str(dst), "name": dst.name}

    def cancel(self):
        """Окно закрыли без «В Studio». Если дубль есть — готовая работа не теряется: последний (или ★ лучший)
        дубль кладётся рядом с исходником (…_ResingerNN.wav) и передаётся Studio с пометкой confirmed: false —
        Studio может предложить вставить его. Дублей нет — «cancelled», как раньше."""
        if self.answered:
            return
        res = {"ok": False, "cancelled": True, "studio": self.req.get("studio") or {}}
        try:
            s = S.get(self.song_id)
            takes = s.m.get("takes", [])
            best = next((t for t in takes if t["id"] == s.m.get("best")), None)
            t = best or (max(takes, key=lambda x: x.get("made", 0)) if takes else None)
            if t:
                done = [x for x in t.get("placed", []) if Path(x).exists()]
                dst = Path(done[-1]) if done else place(self.song_id, t["id"])[0]
                res = {"ok": True, "confirmed": False, "files": [{"role": "dzod_lead", "path": str(dst)}],
                       "voice": t.get("voice_name"), "opts": t.get("opts"), "takes": len(takes),
                       "studio": self.req.get("studio") or {}}
                E.log(f"ОКНО ЗАКРЫТО БЕЗ «В STUDIO»: последний дубль передан Studio без подтверждения — {dst}")
        except Exception as ex:  # noqa: BLE001 — ответ Studio важнее: пишем хотя бы «cancelled»
            E.log(f"ОКНО ЗАКРЫТО: не удалось положить дубль ({ex}) — ответ «cancelled»")
        write_result(self.req["result"], res)
        self.answered = True
