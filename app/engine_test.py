# -*- coding: utf-8 -*-
"""Сквозная проверка с настоящим движком (для сборочного сервера Windows, без видеокарты):
установка движка → чистка песни → датасет → короткое обучение → выбор эпохи → перепевка → сравнение эпох.

    set DZOD_ROOT=C:\\dzv & set DZOD_CPU_TORCH=1 & python app/engine_test.py song.wav

Это проверка проводки (команды, пути, файлы), а не качества звука: на процессоре обучение
идёт 2 эпохи на нескольких фразах."""
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import audio as A  # noqa: E402
import engine as E  # noqa: E402
import songs as S  # noqa: E402
import voices as V  # noqa: E402

OK = True


def check(name, cond, extra=""):
    global OK
    print(("OK    " if cond else "ОШИБКА ") + name + (f"  ({extra})" if extra else ""), flush=True)
    if not cond:
        OK = False
        print(f"::error::engine_test: {name} {extra}", flush=True)


def ctx():
    last = {"t": 0, "m": ""}

    def progress(f, m=""):
        now = time.time()
        if m != last["m"] or now - last["t"] > 20:
            print(f"   {f * 100:5.1f}%  {m}", flush=True)
            last.update(t=now, m=m)
    return E.Ctx(progress)


def step(title, fn):
    print(f"\n=== {title} ===", flush=True)
    t0 = time.time()
    try:
        r = fn()
        print(f"   ({time.time() - t0:.0f} с)", flush=True)
        return r
    except Exception as ex:  # noqa: BLE001
        traceback.print_exc()
        check(title, False, str(ex)[:500])
        tail = E.log_path().read_text(encoding="utf-8", errors="replace")[-6000:] if E.log_path().exists() else ""
        print("--- конец журнала ---\n" + tail, flush=True)
        raise


def main():
    song_file = sys.argv[1]
    print(f"DZOD Vocalist {E.VERSION}; корень {E.root()}", flush=True)
    st = step("Установка движка", lambda: E.install_engine(ctx()))
    check("движок установлен", st["ready"], str(st["steps"]))
    print("   видеокарта:", st.get("gpu"), flush=True)

    song = S.create(song_file, "mix")
    res = step("Чистка (полный трек → вокал → бэки → реверберация)",
               lambda: S.clean(ctx(), song.id, {"backing": True, "dereverb": "uvr", "dereverb_aggr": 50, "quality": "fast"}))
    roles = {f["role"]: f["path"] for f in res["files"]}
    check("чистка: есть лид, бэки, музыка, реверберация",
          all(r in roles for r in ("lead", "backing", "music", "reverb")), ", ".join(roles))
    src_frames = A.info(roles["source"])["frames"]
    for r in ("lead", "backing"):
        i = A.info(roles[r])
        check(f"чистка: {r} той же длины и частоты", i["frames"] == src_frames and i["sr"] == res["info"]["sr"],
              f"{i['frames']} / {src_frames}")

    v = V.create("CI голос")
    slug = v["slug"]
    r = step("Датасет из почищенного лида", lambda: V.add_sources(ctx(), slug, [roles["lead"]], None))
    ph = V.phrase_list(slug)
    check("датасет: есть фразы", len(ph) >= 1, f"{len(ph)}")
    V.mark(slug, [p["id"] for p in ph], "keep")

    step("Обучение: 2 эпохи на процессоре",
         lambda: V.train(ctx(), slug, {"sample_rate": 40000, "epochs": 2, "save_every": 1, "batch": 2, "fresh": True}))
    cks = V.checkpoints(slug)
    check("обучение: сохранены эпохи", len(cks) >= 1, ", ".join(c["file"] for c in cks))
    check("обучение: есть индекс", V.index_file(slug) is not None)
    vv = V.get(slug).view(full=True)
    check("обучение: голос получил модель", vv["has_model"], str(vv.get("model")))

    conv = step("Перепевка лида голосом модели",
                lambda: S.convert(ctx(), song.id, slug, {"index_rate": 0.6}, ("lead", "backing")))
    takes = conv["takes"]
    check("перепевка: два дубля (лид и бэки)", len(takes) == 2, str([t["file"] for t in takes]))
    for t in takes:
        i = A.info(t["path"])
        check(f"перепевка: {t['part']} той же длины и частоты", i["frames"] == src_frames and i["sr"] == res["info"]["sr"],
              f"{i['frames']} / {src_frames}")
        x, sr = A.read(t["path"], mono=True)
        check(f"перепевка: {t['part']} не тишина", A.active_rms(x[:, 0], sr) > 1e-4)

    # перенос папки данных в путь с пробелами (13.11, автор: «D:\\DZOD Studio Materials»): дальше всё — в новой папке
    mv = os.environ.get("DZOD_TEST_MOVE")
    if mv:
        import json as _j
        import numpy as _np
        import relocate as RL
        old_root = str(E.root())
        before, _ = A.read(takes[0]["path"], mono=True)
        n_voices = len(V.list_voices()) if hasattr(V, "list_voices") else None
        r = step(f"Перенос папки данных → {mv}", lambda: RL.move_root(ctx(), mv))
        os.environ["DZOD_ROOT"] = mv                       # в проверке корень задан переменной — переключаем и её
        check("перенос: старой папки нет, новая есть", not Path(old_root).exists() and Path(mv).is_dir(), str(r)[:300])
        cfgs = list((Path(mv) / "Engine").rglob("pyvenv.cfg"))
        check("перенос: среды Python смотрят в новую папку",
              cfgs and all(old_root.lower() not in c.read_text(encoding="utf-8").lower() for c in cfgs), str(cfgs))
        st2 = E.engine_status()
        check("перенос: движок готов в новой папке", st2["ready"], str(st2["steps"]))
        check("перенос: голос на месте", V.get(slug).view(full=True)["has_model"])
        if n_voices is not None:
            check("перенос: голосов столько же", len(V.list_voices()) == n_voices)
        conv2 = step("Перепевка после переноса (путь с пробелами)",
                     lambda: S.convert(ctx(), song.id, slug, {"index_rate": 0.6}, ("lead",)))
        after, _ = A.read(conv2["takes"][0]["path"], mono=True)
        m_ = min(len(before), len(after))
        cc = float(_np.corrcoef(before[:m_, 0], after[:m_, 0])[0, 1])
        check("перенос: звук тот же (корреляция с дублем до переноса ≥ 0,98)", cc >= 0.98, f"{cc:.4f}")
        step("Обучение после переноса: 1 эпоха (путь с пробелами)",
             lambda: V.train(ctx(), slug, {"sample_rate": 40000, "epochs": 1, "save_every": 1, "batch": 2, "fresh": True}))
        cks = V.checkpoints(slug)
        check("обучение после переноса: эпоха сохранена", len(cks) >= 1, ", ".join(c["file"] for c in cks))

    cmp_ = step("Сравнение эпох", lambda: V.compare(ctx(), slug, roles["lead"], [c["file"] for c in cks]))
    check("сравнение: по файлу на эпоху", len(cmp_["items"]) == len(cks), str(cmp_["items"]))
    if cks:
        V.choose(slug, cks[0]["file"])
        check("выбор эпохи", V.get(slug).m["model"]["from"] == cks[0]["file"])

    # остановка перепевки посреди работы (жалоба автора 27.10: «Остановить» не срабатывала)
    import threading
    import psutil
    print("\n=== Остановка перепевки посреди работы ===", flush=True)
    c = E.Ctx()
    res_c = {}

    def run_conv():
        try:
            S.convert(c, song.id, slug, {"index_rate": 0.6}, ("lead", "backing"))
            res_c["state"] = "done"
        except E.Cancelled:
            res_c["state"] = "cancelled"
        except Exception as ex:  # noqa: BLE001
            res_c["state"] = f"error: {ex}"
    th = threading.Thread(target=run_conv, daemon=True)
    th.start()
    t0 = time.time()
    while c.proc is None and th.is_alive() and time.time() - t0 < 120:
        time.sleep(0.2)
    time.sleep(3)
    t1 = time.time()
    c.cancel()
    th.join(30)
    took = time.time() - t1
    check("остановка: задача завершилась", not th.is_alive() and res_c.get("state") in ("cancelled", "done"),
          f"{res_c} за {took:.1f} с")
    check("остановка: быстрее 15 с", took < 15, f"{took:.1f} с")
    time.sleep(2)
    left = [k for k in psutil.Process().children(recursive=True) if k.is_running() and k.status() != psutil.STATUS_ZOMBIE]
    check("остановка: процессов движка не осталось", not left, str([k.pid for k in left]))

    # запрос без окна, как его будет делать DZOD Studio
    import json
    import tempfile
    import core
    rq = Path(tempfile.mkdtemp(prefix="dzv_req_"))
    req = {"version": 1, "action": "clean_convert", "input": song_file, "output_dir": str(rq / "out"),
           "name": "Clip", "voice": slug, "parts": ["lead"],
           "clean": {"mix": True, "backing": True, "dereverb": "roformer", "quality": "fast"}, "progress": str(rq / "progress.txt")}
    (rq / "req.json").write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
    code = step("Запрос --request clean_convert", lambda: core.run_request(rq / "req.json"))
    res_ = json.loads((rq / "out" / "result.json").read_text(encoding="utf-8"))
    check("запрос: код 0 и ok", code == 0 and res_.get("ok"), str(res_)[:300])
    roles_ = {f["role"]: f["path"] for f in res_.get("files", [])}
    check("запрос: есть чистый лид и перепетый лид", "lead" in roles_ and "dzod_lead" in roles_, ", ".join(roles_))
    if "dzod_lead" in roles_:
        i = A.info(roles_["dzod_lead"])
        check("запрос: перепетый лид той же длины", i["frames"] == A.info(song_file)["frames"], str(i))
    check("запрос: ход работы дошёл до конца", (rq / "progress.txt").read_text(encoding="utf-8").startswith("1.000"))

    # DZOD Executor (проба E1): установка, пение двух английских фраз по MIDI, голос CI-модели
    import executor as X
    import numpy as np
    midi = Path(__file__).resolve().parent.parent / "tests" / "executor" / "two_phrases.mid"
    if midi.exists() and os.environ.get("DZOD_TEST_EXECUTOR", "1") == "1":
        st = step("Executor: установка (SoulX-Singer, на процессоре)", lambda: E.install_executor(ctx()))
        check("Executor установлен", st["ready"], str(st["steps"]))
        import subprocess
        tr = subprocess.run([str(E.paths().exec_py), str(midi.parent / "test_score.py")], env=E.exec_env(),
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(tr.stdout[-3000:], tr.stderr[-2000:], flush=True)
        check("Executor: тесты разбора MIDI (в т. ч. битые meta-события)", tr.returncode == 0)
        chk = step("Executor: разбор MIDI", lambda: X.check_midi(ctx(), midi))
        check("Executor: разметка — 1 фраза без предупреждений", chk.get("segments") == 1 and not chk.get("warnings"), str(chk)[:300])
        r = step("Executor: пение по MIDI → голос CI", lambda: X.sing(ctx(), midi, slug, {"index_rate": 0.6}))
        m = r["measure"]
        print("   замер:", json.dumps(m, ensure_ascii=False)[:600], flush=True)
        i = A.info(r["file"])
        check("Executor: WAV 48 кГц нужной длины", i["sr"] == 48000 and abs(i["frames"] / 48000 - m["seconds_audio"]) < 0.01, str(i))
        x, sr = A.read(r["raw"], mono=True)
        check("Executor: SoulX спел не тишину", A.active_rms(x[:, 0], sr) > 1e-3)
        # высота спетого по нотам разметки (октава не важна: SoulX может сдвинуть регистр под образец)
        meta = json.loads(Path(r["dir"], Path(midi).stem + " - разметка.json").read_text(encoding="utf-8"))
        hit = tot = 0
        for seg in meta:
            t = seg["time"][0] / 1000
            for d, pch in zip(map(float, seg["duration"].split()), map(int, seg["note_pitch"].split())):
                if pch > 0 and d >= 0.3:
                    a0, a1 = int((t + d * 0.25) * sr), int((t + d * 0.85) * sr)
                    f = A.f0_median(x[a0:a1, 0], sr)
                    if f > 0:
                        tot += 1
                        diff = (12 * np.log2(f / 440.0) + 69 - pch) % 12
                        hit += min(diff, 12 - diff) <= 1.0
                t += d
        check("Executor: спето по нотам (≥ 60 % нот в пределах полутона)", tot >= 5 and hit / tot >= 0.6, f"{hit} из {tot}")
        # E2: повтор — всё из кэша; Straight — своя кривая высоты; параметры Executor
        r2 = step("Executor: повтор (кэш фраз)", lambda: X.sing(ctx(), midi, slug, {"index_rate": 0.6}))
        m2 = r2["measure"]
        check("Executor: повтор — ни одной фразы заново", m2["soulx"].get("sung") == 0 and m2["rvc_phrases"] == 0, str(m2)[:300])
        r3 = step("Executor: Straight + параметры", lambda: X.sing(ctx(), midi, slug, {"index_rate": 0.6},
                  params={"style": "straight", "vibrato": 30, "formant": 60, "attack": 70, "air": 60, "power": 80,
                          "presence": 40, "fine_tune": 20}))
        check("Executor: Straight — режим melody", r3["measure"]["soulx"].get("control") == "melody", str(r3["measure"]["soulx"]))
        x3, sr3 = A.read(r3["raw"], mono=True)
        hit = tot = 0
        for seg in json.loads(Path(r3["dir"], Path(midi).stem + " - разметка.json").read_text(encoding="utf-8")):
            t = seg["time"][0] / 1000
            for d, pch in zip(map(float, seg["duration"].split()), map(int, seg["note_pitch"].split())):
                if pch > 0 and d >= 0.3:
                    f = A.f0_median(x3[int((t + d * 0.25) * sr3):int((t + d * 0.85) * sr3), 0], sr3)
                    if f > 0:
                        tot += 1
                        diff = (12 * np.log2(f / 440.0) + 69 - pch) % 12
                        hit += min(diff, 12 - diff) <= 0.7
                t += d
        check("Executor: Straight — ноты точно (≥ 80 % в пределах 70 центов)", tot >= 5 and hit / tot >= 0.8, f"{hit} из {tot}")
        # E2b: манеры, кривые, SING SELECTION
        sty = [{"start": 6.3, "end": 8.0, "style": "whisper"}, {"start": 1.0, "end": 2.0, "style": "belt"}]
        cur = {"pitch": [[2.0, 0], [2.3, -100], [2.6, 0]], "power": [[0, 65], [4, 90]], "air": [[0, 50], [5, 70]]}
        r5 = step("Executor: манеры и кривые вне диапазона (тишина)",
                  lambda: X.sing(ctx(), midi, slug, {"index_rate": 0.6}, styles=sty, curves=cur, rng=[30, 40]))
        x5, sr5 = A.read(r5["file"], mono=True)
        check("Executor: диапазон мимо фраз — ничего не спето, тишина",
              not r5["changed"] and float(np.abs(x5).max()) < 1e-3, str(r5["phrases"]))
        r6 = step("Executor: манеры и кривые, диапазон по фразе",
                  lambda: X.sing(ctx(), midi, slug, {"index_rate": 0.6}, styles=sty, curves=cur, rng=[1, 3]))
        check("Executor: SING SELECTION — фраза спета, режим melody (кривая Pitch)",
              r6["changed"] and r6["phrases"][0]["control"] == "melody", str(r6["phrases"]))
        x6, sr6 = A.read(r6["file"], mono=True)
        check("Executor: манеры — звук есть", A.active_rms(x6[:, 0], sr6) > 1e-3)
        # манера меняет звук, кэш не мешает: фраза 1 — Normal → Whisper → Shout → Soft (пение — из кэша)
        outs = {}
        for st in ("normal", "whisper", "shout", "soft"):
            rs = step(f"Executor: фраза 1 — манера {st}", lambda st=st: X.sing(ctx(), midi, slug, {"index_rate": 0.6},
                      styles=[{"start": 0.0, "end": 5.0, "style": st}]))
            xs, srs = A.read(rs["file"], mono=True)
            outs[st] = (xs[:int(5 * srs), 0], rs)
        b0 = outs["normal"][0]
        for st in ("whisper", "shout", "soft"):
            y = outs[st][0]
            m = min(len(y), len(b0))
            d = 20 * np.log10(np.sqrt(np.mean((y[:m] - b0[:m]) ** 2)) / max(np.sqrt(np.mean(b0[:m] ** 2)), 1e-9) + 1e-12)
            check(f"Executor: манера {st} меняет звук (разница с Normal ≥ −12 дБ), голос из кэша",
                  d > -12 and outs[st][1]["measure"]["rvc_phrases"] == 0 and outs[st][1]["changed"], f"{d:.1f} дБ")
        g = step("Executor: Guide Tone", lambda: X.guide(ctx(), midi))
        check("Executor: Guide Tone", Path(g["file"]).exists() and g["notes"] >= 10, str(g))
        # MIDI из вокала: пение Straight (известные ноты и слоги) → распознать → сравнить с MIDI
        lyr = "I wish you were here tonight\nwe are the beautiful no"
        tr = step("MIDI из вокала (синтетика Executor)", lambda: X.transcribe(ctx(), r3["raw"], lyr, "auto", "notes"))
        print("   нот:", len(tr["notes"]), "слов:", len(tr["words"]), "темп:", tr["tempo_hint"], "время:", tr.get("t_total"),
              "f0:", tr.get("f0"), "не найдены:", tr.get("missing_words"), flush=True)
        print("   " + " | ".join(f'{n["syl"]}:{n["pitch"]}@{n["t"]:.2f}' for n in tr["notes"]), flush=True)
        ref = []
        for seg in json.loads(Path(r3["dir"], Path(midi).stem + " - разметка.json").read_text(encoding="utf-8")):
            t = seg["time"][0] / 1000
            for d, pch, ty in zip(map(float, seg["duration"].split()), map(int, seg["note_pitch"].split()), seg["note_type"].split()):
                if pch > 0:
                    ref.append((t, d, pch, ty))
                t += d
        ok_p = 0
        for (t, d, pch, ty) in ref:
            mid_t = t + d / 2
            got = [n for n in tr["notes"] if n["t"] <= mid_t < n["t"] + n["dur"]]
            ok_p += bool(got) and got[0]["pitch"] == pch
        errs = []
        for (t, d, pch, ty) in ref:
            if ty == "2":
                near = min(tr["notes"], key=lambda n: abs(n["t"] - t))
                errs.append(abs(near["t"] - t))
        med = float(np.median(errs)) if errs else 9
        check("MIDI из вокала: ≥ 90 % нот той же высоты", ok_p >= 0.9 * len(ref), f"{ok_p} из {len(ref)}")
        check("MIDI из вокала: начала нот ±30 мс (медиана)", med <= 0.03, f"медиана {med * 1000:.0f} мс, макс {max(errs) * 1000:.0f} мс")
        check("MIDI из вокала: все слова найдены, MIDI записан", not tr.get("missing_words") and Path(tr["midi"]).exists(),
              str(tr.get("missing_words")))
        # слоги тестового MIDI two_phrases.mid по нотам (распев — «-»)
        want = "I wish you were here - to- night we are the beau- ti- ful no -".split()
        ok_s = 0
        for k, (t, d, pch, ty) in enumerate(ref):
            got = [n for n in tr["notes"] if n["t"] <= t + d / 2 < n["t"] + n["dur"]]
            ok_s += bool(got) and k < len(want) and got[0]["syl"].lower() == want[k].lower()
        check("MIDI из вокала: слоги на своих нотах (≥ 90 %)", ok_s >= 0.9 * len(ref), f"{ok_s} из {len(ref)}")
        sy = [n["syl"] for n in tr["notes"]]
        check("MIDI из вокала: слоги beau- ti- ful по порядку", "beau-" in sy and sy.index("beau-") + 2 < len(sy)
              and "ful" in sy[sy.index("beau-"):], " ".join(sy))

    print("\nИТОГ: " + ("всё в порядке" if OK else "есть ошибки"), flush=True)
    return OK


if __name__ == "__main__":
    try:
        ok = main()
    except Exception:  # noqa: BLE001
        ok = False
    sys.exit(0 if ok else 1)
