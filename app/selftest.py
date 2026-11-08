# -*- coding: utf-8 -*-
"""Самопроверка программы без движка: звук, песни, датасет. Запуск:
    python app/selftest.py            (из исходников)
    "DZOD Vocalist.exe" --selftest log.txt   (собранная программа)
Работает во временной папке, настоящие песни и голоса не трогает."""
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
sys.path.insert(0, str(HERE))


def synth_voice(sr, seconds, notes=(220, 247, 262, 294, 330, 294, 262, 247), gap=0.35, seed=1):
    """Синтетическое «пение»: ноты с вибрато и паузами между фразами."""
    import numpy as np
    rng = np.random.default_rng(seed)
    out = []
    per = seconds / len(notes)
    for f in notes:
        n = int(sr * (per - gap))
        t = np.arange(n) / sr
        ff = f * (1 + 0.006 * np.sin(2 * np.pi * 5.5 * t))
        ph = 2 * np.pi * np.cumsum(ff) / sr
        x = sum((0.5 / k ** 1.2) * np.sin(k * ph) for k in range(1, 16))
        env = np.minimum(1, t / 0.03) * np.minimum(1, (t[-1] - t) / 0.05 + 1e-9)
        out.append(x * env * 0.6)
        out.append(rng.standard_normal(int(sr * gap)) * 0.0005)
    return np.concatenate(out).astype("float32")


def run(log=print):
    ok = True
    tmp = Path(tempfile.mkdtemp(prefix="dzv_selftest_"))
    os.environ["DZOD_ROOT"] = str(tmp / "root")
    os.environ["LOCALAPPDATA"] = str(tmp / "appdata")

    def check(name, cond, extra=""):
        nonlocal ok
        log(("OK    " if cond else "ОШИБКА ") + name + (f"  ({extra})" if extra else ""))
        if not cond:
            ok = False
            print(f"::error::selftest: {name} {extra}")

    try:
        import numpy as np
        import audio as A
        import engine as E
        import songs as S
        import voices as V
        E.SETTINGS = E.Settings()           # настройки — во временной папке, настоящие не трогаем

        log(f"DZOD Vocalist {E.VERSION}, Python {sys.version.split()[0]}")

        # --- путь движка
        check("путь без пробелов принимается", E.check_root("D:\\DZOD_Vocalist") == "" or os.name != "nt")
        check("путь с пробелом отклоняется", E.check_root("D:\\Claude Materials") != "")
        check("путь с кириллицей отклоняется", E.check_root("C:\\Пользователи\\x") != "")

        # --- звук: частота, длина, громкость
        sr = 48000
        x = synth_voice(sr, 8.0)
        src = tmp / "src.wav"
        A.write(src, np.stack([x, x * 0.9], 1), sr)
        y = A.resample(x[:, None] * 0.25, sr, 40000)        # как будто движок вернул 40 кГц и тише
        y = y[: len(y) - 3000]                               # и чуть короче
        eng = tmp / "eng.wav"
        A.write(eng, y, 40000, "FLOAT")
        out = tmp / "out.wav"
        A.conform(eng, out, src, match_level=True)
        i_src, i_out = A.info(src), A.info(out)
        check("результат: та же частота", i_out["sr"] == i_src["sr"], f"{i_out['sr']}")
        check("результат: ровно та же длина", i_out["frames"] == i_src["frames"], f"{i_out['frames']} / {i_src['frames']}")
        a = A.active_rms(A.read(src, mono=True)[0][:, 0], sr)
        b = A.active_rms(A.read(out, mono=True)[0][:, 0], sr)
        check("результат: громкость как у исходника", abs(20 * np.log10(b / a)) < 0.6, f"{20 * np.log10(b / a):+.2f} дБ")

        pk = A.peaks(src, 400)
        check("волна для экрана", len(pk["max"]) == 400 and abs(pk["seconds"] - 8.0) < 0.01)

        # --- фразы и высота тона
        segs = A.split_phrases(x, sr)
        check("короткие паузы не рвут фразу", len(segs) == 1 and (segs[0][1] - segs[0][0]) / sr > 7, f"фраз: {len(segs)}")
        parts = [synth_voice(sr, 3.0, notes=(220, 262, 330), seed=k) for k in range(4)]
        sil = np.zeros(int(sr * 1.5), dtype="float32")
        x4 = np.concatenate([np.concatenate([p, sil]) for p in parts])
        segs4 = A.split_phrases(x4, sr)
        check("длинные паузы делят на фразы", len(segs4) == 4, f"фраз: {len(segs4)}")
        long = synth_voice(sr, 40.0, notes=tuple([220, 247] * 20), gap=0.2)
        segsl = A.split_phrases(long, sr)
        check("длинное пение режется на куски не длиннее 12 с",
              len(segsl) >= 4 and max((b - a) / sr for a, b in segsl) <= 12.3, f"фраз: {len(segsl)}")
        f0 = A.f0_median(x[: sr * 1], sr)
        check("высота тона первой ноты ≈ 220 Гц", abs(f0 - 220) < 6, f"{f0:.1f} Гц")
        check("имя ноты", A.note_name(220) == "A3" and A.note_name(440) == "A4")
        an = A.analyze(x[:sr], sr)
        check("разбор фразы", not an["clip"] and an["rms_db"] < 0 and an["f0"] > 0, str(an))

        # --- песня
        song = S.create(src, "stem")
        check("песня создана", song.file("source").exists())
        again = S.create(src, "stem")
        check("та же песня открывается повторно, а не копируется", again.id == song.id)
        check("список песен", any(s["id"] == song.id for s in S.list_songs()))
        S.save_preset("Мой пресет", {"pitch": 2, "index_rate": 0.7})
        names = [p["name"] for p in S.presets()]
        check("пресеты", "Мой пресет" in names and "Стандарт" in names)

        # --- голос и датасет (без чистки — движок не нужен)
        v = V.create("DZOD Гранж")
        check("код голоса латиницей", v["slug"] == "dzod_granzh", v["slug"])
        ctx = E.Ctx()
        f2 = tmp / "take2.wav"
        A.write(f2, x4[:, None], sr)
        r = V.add_sources(ctx, v["slug"], [str(src), str(f2)], None)
        check("записи нарезаны на фразы", r["added"] == 5, f"фраз: {r['added']}")
        ph = V.phrase_list(v["slug"])
        check("у фраз есть файлы", all(Path(p["path"]).exists() for p in ph))
        st = V.mark(v["slug"], [p["id"] for p in ph[:4]], "keep")
        check("отбор фраз", st["keep"] == 4)
        vv = V.get(v["slug"])
        check("датасет помечен как изменённый", V.dataset_changed(vv))
        ds = V.build_dataset(v["slug"])
        check("датасет собран", ds["count"] == 4 and len(list((vv.dir / "dataset").glob("*.wav"))) == 4)
        check("после сборки — не изменён", not V.dataset_changed(V.get(v["slug"])))
        V.remove_source(v["slug"], V.get(v["slug"]).m["sources"][0]["id"])
        check("запись убрана вместе с фразами", len(V.phrase_list(v["slug"])) < len(ph))

        # --- папка готовых фраз с table.csv
        pf = tmp / "phrases_in"
        pf.mkdir()
        for k in range(4):
            A.write(pf / f"ph{k}.wav", parts[k][:, None], sr)
        (pf / "table.csv").write_text("file,duration_s,lufs,best,reject_reason\nph0.wav,3,-20,1,\nph1.wav,3,-21,0,клиппинг\nph2.wav,3,-20,1,\n", encoding="utf-8")
        before = len(V.phrase_list(v["slug"]))
        rf = V.add_phrase_folder(E.Ctx(), v["slug"], pf)
        check("готовые фразы: по одной на файл", rf["added"] == 4 and len(V.phrase_list(v["slug"])) == before + 4, str(rf["added"]))
        check("готовые фразы: table.csv — лучшие оставлены, отбракованные выброшены", rf["kept"] == 2 and rf["dropped"] == 1,
              f"{rf['kept']} / {rf['dropped']}")
        # формат ветки vocal-train: архивы best_*.zip / spare_*.zip, таблица через «;», звёздочка ★, колонка reasons
        import zipfile
        vt = tmp / "vocal-train"
        vt.mkdir()
        with zipfile.ZipFile(vt / "best_01.zip", "w") as zf:
            zf.write(pf / "ph0.wav", "a_0.wav")
            zf.write(pf / "ph2.wav", "a_2.wav")
        with zipfile.ZipFile(vt / "spare_1.zip", "w") as zf:
            zf.write(pf / "ph3.wav", "a_3.wav")
        (vt / "table.csv").write_text("\ufefffile;src;dur_s;reasons;ok;score;best\na_0.wav;t1;3;;да;6;★\n"
                                      "a_2.wav;t1;3;;да;5;★\na_3.wav;t2;3;;да;4;\na_9.wav;t2;3;\"клиппинг; мало голоса\";нет;0;\n",
                                      encoding="utf-8")
        rz = V.add_phrase_folder(E.Ctx(), v["slug"], vt)
        check("ветка vocal-train: архивы распакованы, ★ оставлены", rz["added"] == 3 and rz["kept"] == 2 and rz["dropped"] == 0,
              f"{rz['added']} / {rz['kept']} / {rz['dropped']}")
        check("ветка vocal-train: источник фразы из колонки src",
              any(p.get("origin") == "t2" for p in V.phrase_list(v["slug"])))

        # --- запросы без окна (для DZOD Studio)
        import json as _j
        import core
        rq = tmp / "rq"
        rq.mkdir()
        (rq / "status.json").write_text(_j.dumps({"version": 1, "action": "voices", "result": str(rq / "r1.json")}), encoding="utf-8")
        code = core.run_request(rq / "status.json")
        r1 = _j.loads((rq / "r1.json").read_text(encoding="utf-8"))
        check("запрос «голоса»", code == 0 and r1["ok"] and any(x["slug"] == v["slug"] for x in r1["voices"]), str(r1)[:200])
        for ver in (2, 3):
            (rq / f"v{ver}.json").write_text(_j.dumps({"version": ver, "action": "status", "result": str(rq / f"rv{ver}.json")}), encoding="utf-8")
            code = core.run_request(rq / f"v{ver}.json")
            rv = _j.loads((rq / f"rv{ver}.json").read_text(encoding="utf-8"))
            if ver == 2:
                check("запрос версии 2 (sing v2 от Studio) принят, api = 2", code == 0 and rv["ok"] and rv.get("api") == 2, str(rv)[:200])
            else:
                check("запрос версии 3 — понятный отказ", code == 1 and not rv["ok"] and "Обновите" in rv.get("error", ""), str(rv)[:200])
        (rq / "bad.json").write_text(_j.dumps({"action": "convert", "input": str(src), "voice": v["slug"],
                                                 "output_dir": str(rq / "out")}), encoding="utf-8")
        code = core.run_request(rq / "bad.json")
        r2 = _j.loads((rq / "out" / "result.json").read_text(encoding="utf-8"))
        check("запрос с ошибкой: понятный ответ и код 1", code == 1 and not r2["ok"] and r2["error"], r2.get("error", ""))

        # --- вокалист папкой: DZOD Vocalist сохраняет → DZOD Revoicer загружает
        fk = tmp / "fake"
        fk.mkdir()
        (fk / "m.pth").write_bytes(b"\x00" * 2048)
        (fk / "added_m.index").write_bytes(b"\x01" * 512)
        tv = V.import_model("Голос 1", str(fk / "m.pth"), str(fk / "added_m.index"))
        ex = V.export_folder(tv["slug"], str(tmp / "export"))
        exd = Path(ex["path"])
        meta = _j.loads((exd / "voice.json").read_text(encoding="utf-8"))
        check("вокалист сохранён в папку: модель, индекс, паспорт",
              exd.name == "Голос 1" and (exd / meta["model"]).exists() and meta.get("index") and (exd / meta["index"]).exists(),
              str(sorted(x.name for x in exd.iterdir())))
        im = V.import_folder(str(exd))
        check("вокалист из папки: тот же голос обновляется, а не дублируется",
              im.get("replaced") and sum(1 for x in V.list_voices() if x["name"] == "Голос 1") == 1, str(im)[:200])
        raw = tmp / "Голос Б"
        raw.mkdir()
        (raw / "G_100.pth").write_bytes(b"\x00" * 9000)          # чекпойнт обучения — не модель
        (raw / "golos_b.pth").write_bytes(b"\x00" * 3000)
        (raw / "trained_x.index").write_bytes(b"\x00" * 100)
        (raw / "added_x.index").write_bytes(b"\x00" * 222)
        ib = V.import_folder(str(raw))
        vb = V.get(ib["slug"]).view(full=True)
        check("папка без паспорта: имя по папке, модель не G_, индекс added",
              ib["name"] == "Голос Б" and vb["has_model"] and V.model_files(ib["slug"])[0].stat().st_size == 3000
              and V.model_files(ib["slug"])[1].stat().st_size == 222,
              str(V.model_files(ib["slug"])))

        # --- DZOD Revoicer: окно из Studio, вставка …_ResingerNN.wav рядом с исходником
        import revoicer as R
        proj = tmp / "Space RIGOR"
        proj.mkdir()
        srcv = proj / "RIGOR 04 Vocal.wav"
        A.write(srcv, x[:, None], sr)
        req = {"version": 1, "input": str(srcv), "source_file": str(srcv), "result": str(rq / "studio_res.json"),
               "studio": {"line": "Vocal 1", "line_id": "L7", "slice_start": 12.5, "slice_length": 8.0}}
        (rq / "studio_req.json").write_text(_j.dumps(req, ensure_ascii=False), encoding="utf-8")
        ses = R.Session(R.read_request(rq / "studio_req.json"))
        check("Revoicer: имя первого результата", ses.view()["next_name"] == "RIGOR 04 Vocal_Resinger01.wav", ses.view()["next_name"])
        sg = S.get(ses.song_id)
        shutil.copyfile(srcv, sg.dir / "take.wav")
        sg.m.setdefault("takes", []).append({"id": "t1", "file": "take.wav", "voice_name": "Голос 1", "opts": {}})
        sg.save()
        ins = ses.insert("t1")
        res = _j.loads((rq / "studio_res.json").read_text(encoding="utf-8"))
        check("Revoicer: файл рядом с исходником, ответ Studio",
              ins["name"] == "RIGOR 04 Vocal_Resinger01.wav" and (proj / ins["name"]).exists() and res["ok"]
              and res["files"][0]["path"] == str(proj / ins["name"]) and res["studio"]["line_id"] == "L7", str(res)[:300])
        ses.cancel()
        check("Revoicer: после вставки отмена ответ не портит", _j.loads((rq / "studio_res.json").read_text(encoding="utf-8"))["ok"])
        check("Revoicer: следующий номер", R.next_name(srcv).name == "RIGOR 04 Vocal_Resinger02.wav")
        check("Revoicer: перепевка перепетого — номер от исходного имени",
              R.next_name(proj / "RIGOR 04 Vocal_Resinger01.wav").name == "RIGOR 04 Vocal_Resinger02.wav")
        req["result"] = str(rq / "studio_res2.json")
        (rq / "studio_req2.json").write_text(_j.dumps(req, ensure_ascii=False), encoding="utf-8")
        ses2 = R.Session(R.read_request(rq / "studio_req2.json"))
        check("Revoicer: тот же Slice открывается с прежними дублями", ses2.song_id == ses.song_id)
        ses2.cancel()
        r3 = _j.loads((rq / "studio_res2.json").read_text(encoding="utf-8"))
        check("Revoicer: закрыли без вставки — ответ «отменено»", r3 == {"ok": False, "cancelled": True, "studio": req["studio"]}, str(r3))
        dst2, _t = R.place(ses.song_id, "t1")
        check("«Рядом с исходником» без Studio", dst2.name == "RIGOR 04 Vocal_Resinger02.wav" and dst2.exists())

        # --- DZOD Executor (проба E1): модуль и рабочие файлы на месте, движок пения не установлен
        import executor as X  # noqa: F401
        check("Executor: модуль и рабочие файлы в программе",
              (E.WORKERS_SRC / "executor_worker.py").exists() and (E.WORKERS_SRC / "exec_score.py").exists())
        check("Executor: без установки — «не установлен»", not E.executor_status()["ready"])

        # --- состояние движка (он здесь не установлен)
        es = E.engine_status()
        check("состояние движка читается", "steps" in es and len(es["steps"]) == len(E.STEPS))
        check("рабочие скрипты движка в комплекте", (E.WORKERS_SRC / "sep_worker.py").exists()
              and (E.WORKERS_SRC / "applio_worker.py").exists())
    except Exception:
        log("ОШИБКА: " + traceback.format_exc())
        print("::error::selftest: исключение " + traceback.format_exc().splitlines()[-1])
        ok = False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    log("ИТОГ: " + ("всё в порядке" if ok else "есть ошибки"))
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
