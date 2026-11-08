# DZOD Vocalist

Приложение для Windows 10/11: свой нейро-вокалист. Берёт вокальную партию из Suno, чистит её
и перепевает голосом обученной модели DZOD. Результат — WAV той же частоты и ровно той же длины,
что исходник: его ставят в проект Samplitude с начала, и он совпадает по времени.
Разработчик: ООО "Нейрофильм", neirofilm.ru.

## Что умеет (версия 0.1)

- **Песня.** Открыть вокал (стем Suno или полный трек) → чистка (выделить вокал из трека, отделить бэки,
  убрать реверберацию и эхо, шум) → перепевка выбранным голосом с настройками и пресетами →
  дубли с переключением «исходник / DZOD» (клавиша Tab) в одном времени.
- **Голос.** Датасет: записи (с чисткой или без) режутся на фразы, у каждой — длина, нота, громкость,
  доля верха («стекло»), перегруз; отбор с клавиатуры. Обучение одной кнопкой (Applio, HiFi-GAN, RMVPE,
  ContentVec), эпохи сохраняются по ходу. Сравнение эпох на одном отрывке и выбор основной.
  Можно добавить готовую модель RVC (.pth + .index).
- **Движок.** Ставится из программы одной кнопкой в `<корень>\Engine`.

## Режим без окна (для DZOD Studio), с 0.1.1

`"DZOD Vocalist.exe" --request request.json` — чистка и/или перепевка файла по JSON-запросу, ответ в `result.json`,
ход работы — в файле `progress`. Действия: `status`, `voices`, `clean`, `convert`, `clean_convert`. Формат — в начале
`app/core.py` и в документации проекта.

## DZOD Revoicer (с 0.3.0)
Облегчённая программа для DZOD Studio из тех же исходников (`flavor.txt` = `revoicer` в сборке,
для разработки — `DZOD_FLAVOR=revoicer`): только движок и перепевка, свой корень `D:\DZOD_Revoicer`,
свои данные `%LOCALAPPDATA%\DZOD Revoicer`. Вокалист загружается папкой (`voices.export_folder` /
`import_folder`: `<имя>.pth`, `<имя>.index`, `voice.json`). Окно из Studio:
`"DZOD Revoicer.exe" --studio request.json` — протокол в `app/revoicer.py`; результат кладётся рядом
с исходником как `<имя>_ResingerNN.wav`.

## Устройство

- Сама программа: Python 3.12, numpy, scipy, soundfile, pywebview (окно WebView2); упаковка PyInstaller
  (папкой), установщик Inno Setup. Интерфейс — HTML/CSS/JS без сборщика в `app/ui`.
- `app/engine.py` — пути, настройки, запуск процессов движка (`Ctx.run`: строки `@@P lo|hi текст`,
  `@@R json`, `@@E текст`; полосы tqdm превращаются в ход внутри шага), установка движка.
- `app/audio.py` — чтение/запись, частота, длина, громкость (`conform`), волна, нарезка на фразы, разбор фраз.
- `app/songs.py` — песни, чистка, перепевка, пресеты. `app/voices.py` — голоса, датасет, обучение, эпохи.
- `app/server.py` — локальный сервер 127.0.0.1 с одноразовым ключом. `app/main.py` — окно.
- `app/workers/` — скрипты, которые выполняются **внутри движка** (копируются в `Engine\workers` при запуске):
  `sep_worker.py` (чистка, audio-separator) и `applio_worker.py` (перепевка, `rvc.infer.infer.VoiceConverter`).

### Движок (`<корень>\Engine`)

| Что | Где | Откуда |
|---|---|---|
| uv 0.11.32 | `uv\uv.exe` | GitHub astral-sh/uv |
| Python 3.12 | `python\` | uv (python-build-standalone) |
| Applio 3.6.6 | `Applio\`, среда `Applio\env` | GitHub IAHispano/Applio, `requirements-engine.txt`, PyTorch cu128 |
| Модели голоса | `Applio\rvc\models` | `core.py prerequisites` (HuggingFace) |
| Чистка | среда `Sep\env`, модели `Sep\models` | `audio-separator[gpu]==0.47.0`, модели UVR качаются при первом использовании |

Кэш пакетов uv лежит в `Engine\uv-cache` на том же диске — PyTorch хранится один раз (жёсткие ссылки).
Шаги установки отмечаются файлами `Engine\.done_*`: прерванная установка продолжается с места остановки.
Обучение идёт в `Applio\logs\<код голоса>`; основная модель голоса копируется в `Voices\<код>\model`.

### Модели чистки (`sep_worker.py`)

- вокал из трека: `model_bs_roformer_ep_317_sdr_12.9755.ckpt`, «Максимум» — пресет `vocal_rvc`;
- бэки: `mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt`, «Максимум» — пресет `karaoke`;
- реверберация: мягко / средне — De-Reverb anvuew (less aggressive / обычная), сильно — De-Reverb-Echo V2 (Sucial);
- шум: `denoise_mel_band_roformer_aufr33_sdr_27.9959.ckpt`.
Модели работают на 44,1 кГц; результат возвращается к частоте исходника программой.

## Данные

- Корень (по умолчанию `D:\DZOD_Vocalist`, без пробелов и кириллицы): `Engine`, `Songs\<песня>`, `Voices\<код>`.
- `%LOCALAPPDATA%\DZOD Vocalist\` — `settings.json` (корень, пресеты, последние настройки), `log.txt` (журнал движка и ошибок).

## Проверки

- `python app/selftest.py` — без движка: звук (частота, длина, громкость), нарезка, высота тона, песни, датасет.
- `app/engine_test.py <песня>` — сквозная проверка с настоящим движком на CPU (сборочный сервер Windows):
  установка → чистка → датасет → 2 эпохи → перепевка → сравнение эпох.
- Сборка: GitHub Actions (`.github/workflows/build.yml`) при каждой отправке в `main`: самопроверка,
  PyInstaller, самопроверка exe, окно WebView2, установщик, релиз `v<версия>`. Отдельная задача — сквозная
  проверка движка (около часа).

## Разработка на Linux

Движок можно подменить готовыми средами: `DZOD_APPLIO_PY`, `DZOD_APPLIO_DIR`, `DZOD_SEP_PY`, `DZOD_SEP_MODELS`,
корень — `DZOD_ROOT`. `DZOD_CPU_TORCH=1` ставит PyTorch для процессора.
