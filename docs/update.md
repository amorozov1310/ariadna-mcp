# Регулярное обновление индекса из информационной базы

Индекс Ариадны отражает выгрузку конфигурации в файлы. Чтобы он не отставал
от рабочей базы, выгрузку можно обновлять по расписанию — например, каждую
ночь — готовым скриптом: `scripts/update_from_infobase.ps1` для Windows или
`scripts/update_from_infobase.sh` для Linux (те же шаги и параметры, см.
[«Linux»](#linux)):

```
Информационная база 1С
   │  1. Конфигуратор в пакетном режиме: /DumpConfigToFiles
   │     (первый раз — полная выгрузка, дальше — только изменения, -update)
   ▼
Каталог выгрузки на машине робота (-DumpDir, хранится между запусками)
   │  2. Клиент получает манифест Ариадны и отправляет по сети только
   │     новые и изменённые файлы, а также список удалённых
   ▼
Ариадна: 3. переиндексирует источник и ждёт окончания —
         индексируются только изменившиеся модули
```

Где запускать — решаете вы: на сервере 1С, на машине одного из разработчиков,
на сервере Ариадны. Нужны платформа 1С (8.3.25 и новее), доступ к базе и
доступ по сети к Ариадне.

## 1. Подготовка в Ариадне (администратор)

1. Создайте проект и источники — как обычно, в Web UI: основная конфигурация
   (например, источник `main`) и расширения, если нужны (вид «Расширение»).
   Первую выгрузку можно загрузить архивом при создании источника или
   оставить это скрипту.
2. Если на сервере включены токены ([docs/team.md](team.md)) — создайте токен
   для робота с ролью **user**: обновлять источники и запускать индексацию
   ему можно, создавать и удалять проекты — нет.

## 2. Подготовка на машине робота

- **Платформа 1С** 8.3.25 или новее. Скрипт сам находит самую новую в
  `Program Files\1cv8`; другую укажите параметром `-Platform`.
- **Пользователь базы** с правом запускать Конфигуратор. Пароль — в
  переменной окружения `ONEC_PASSWORD` пользователя Windows, от которого
  работает задача (не в командной строке задания). Учтите: на время выгрузки
  пароль виден в списке процессов этой машины — запускайте робота там, где
  посторонних пользователей нет.
- **Токен Ариадны** — в переменной окружения `ARIADNA_TOKEN` того же
  пользователя Windows. Задать обе переменные (под этим пользователем):

  ```powershell
  [Environment]::SetEnvironmentVariable('ARIADNA_TOKEN', 'ar_...', 'User')
  [Environment]::SetEnvironmentVariable('ONEC_PASSWORD', 'пароль', 'User')
  ```

- **Каталог выгрузки** (`-DumpDir`) — постоянный, с путём латиницей
  (например, `D:\Dumps\myconf`). Его нельзя очищать между запусками: по файлу
  `ConfigDumpInfo.xml` в нём платформа выгружает только изменения. Место —
  примерно как у самой выгрузки (у крупной типовой — около 9 ГБ).
- **curl.exe** — есть в Windows 10 1803 и новее.
- **Python 3** — используется для подсчёта хешей и сборки архива разницы.
  Если его нет, скрипт автоматически передаст полный архив.

Скопируйте `scripts/update_from_infobase.ps1` и `scripts/build_source_delta.py`
в один каталог на машине робота.

## 3. Первый запуск — вручную

Первая выгрузка полная и на крупной конфигурации идёт десятки минут;
следующие — минуты. Запустите под тем же пользователем Windows, от которого
будет работать задача:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File D:\Ariadna\update_from_infobase.ps1 `
    -AriadnaUrl http://ariadna.lan:19878 -Project myconf `
    -InfoBaseServer "srv1c\myconf" -User "Робот" -DumpDir D:\Dumps\myconf
```

В конце — строка `Готово: индекс Ариадны обновлён` и код возврата 0.

### Параметры

| Параметр | Что это |
|---|---|
| `-AriadnaUrl` | адрес Web UI Ариадны, например `http://ariadna.lan:19878` |
| `-Project` | id проекта в Ариадне |
| `-Source` | источник основной конфигурации (по умолчанию `main`) |
| `-InfoBaseFile` / `-InfoBaseServer` | база: каталог файловой базы или `сервер\база` — что-то одно |
| `-User` | пользователь базы; пароль — `-Password` или переменная `ONEC_PASSWORD` |
| `-Extensions` | имена расширений одной строкой через запятую: `-Extensions "Расш1,Расш2"` |
| `-ExtensionSources` | если источник расширения в Ариадне называется не так, как расширение: `-ExtensionSources "Расш1=ext1,Расш2=ext2"` |
| `-DumpDir` | постоянный каталог выгрузки (путь латиницей) |
| `-TargetRoot` | передача в папку вместо архива (см. ниже) |
| `-ServerDump` | многопоточная выгрузка на сервере 1С (только серверная база) |
| `-Token` | токен Ариадны (по умолчанию — переменная `ARIADNA_TOKEN`) |
| `-TimeoutMinutes` | сколько ждать индексацию (по умолчанию 120) |
| `-Platform` | путь к `1cv8.exe`, если нужна не самая новая платформа |
| `-SkipDump` | не выгружать — передать то, что уже лежит в `-DumpDir` |
| `-FullUpload` | передать полную выгрузку архивом вместо разницы |

### Архивом или в папку

- **Разницей** (по умолчанию) — клиент получает список SHA-256 и размеров из
  сохранённого сервером манифеста, локально считает хеши файлов (без `Template.*`)
  и отправляет архив с новыми/изменёнными файлами и удалениями. Например, при
  изменении одного модуля передаётся этот модуль и манифест; остальные файлы
  выгрузки повторно не отправляются. Ариадна проверяет версию манифеста,
  готовит целевое дерево с жёсткими ссылками на неизменённые файлы и проверяет
  новые, затем публикует дерево вместе с манифестом и запускает обычную
  инкрементальную индексацию. Содержимое 9-ГБ выгрузки целиком не копируется.
  Если версия устарела, робот
  повторно получает манифест. Если сервер ещё не поддерживает этот API, скрипт
  автоматически отправляет полный архив.
- **Полный архив** — используйте `-FullUpload` в PowerShell или `--full-upload`
  в Linux. Передаётся вся выгрузка без макетов. Старый API `refresh` остаётся
  доступен для совместимости.
- **В папку** (`-TargetRoot`) — быстрее: `robocopy /MIR` копирует только
  изменённые файлы. Нужен доступ к папке источников проекта на сервере
  Ариадны — локально или общей папкой: `<data>\projects\<проект>\sources`
  (для Docker-установки `data` — каталог рядом с `docker-compose.yml`).
  Источник при этом должен лежать на своём обычном месте
  `sources\<источник>\xml` — так у источников, загруженных архивом в Web UI.

## 4. Расписание (Планировщик заданий Windows)

PowerShell от имени администратора, пользователь задачи — тот, у которого
заданы переменные `ARIADNA_TOKEN` и `ONEC_PASSWORD`:

```powershell
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument (
    '-NoProfile -ExecutionPolicy Bypass -File "D:\Ariadna\update_from_infobase.ps1" ' +
    '-AriadnaUrl http://ariadna.lan:19878 -Project myconf -InfoBaseServer "srv1c\myconf" ' +
    '-User "Робот" -DumpDir "D:\Dumps\myconf"')
$trigger = New-ScheduledTaskTrigger -Daily -At '02:00'
Register-ScheduledTask -TaskName 'Ариадна — обновление myconf' -Action $action -Trigger $trigger `
    -User 'DOMAIN\robot' -Password 'пароль Windows' -RunLevel Highest
```

Проверить сразу, не дожидаясь ночи: `Start-ScheduledTask -TaskName 'Ариадна — обновление myconf'`.

## Linux

`scripts/update_from_infobase.sh` и `scripts/build_source_delta.py` должны лежать
в одном каталоге. Linux-скрипт выполняет те же шаги, что Windows-скрипт, и принимает те же
параметры в виде `--ключ`: `--ariadna-url`, `--project`, `--source`,
`--file` / `--server`, `--user`, `--extensions`, `--extension-sources`,
`--dump-dir`, `--target-root`, `--server-dump`, `--platform`,
`--timeout-minutes`, `--skip-dump`, `--full-upload`; справка — `--help`.
Для разницы нужен Python 3 в `PATH`. Пароль базы и токен
Ариадны — в переменных `ONEC_PASSWORD` и `ARIADNA_TOKEN`.

Что нужно на машине робота:

- **клиент платформы 1С для Linux** (Конфигуратор `1cv8`) 8.3.25 или новее —
  скрипт сам находит самый новый в `/opt/1cv8`, другой — `--platform`;
- **графический экран** — Конфигуратору он нужен и в пакетном режиме. На
  сервере без него скрипт запускает Конфигуратор через `xvfb-run`: установите
  пакет `xvfb` (`sudo apt install xvfb`);
- `curl`, `tar`; для передачи в папку — `rsync`; `python3` — для красивой
  строки результата в журнале (без него в журнал попадёт ответ как есть).

Полная выгрузка передаётся в формате `tar.gz`, разница — в ZIP; в папку —
`rsync --delete` (зеркало, без макетов). Серверная база указывается так же, как в Windows: `сервер\база`
или `сервер:порт\база`.

### Первый запуск

```bash
export ARIADNA_TOKEN=ar_... ONEC_PASSWORD=...
./update_from_infobase.sh --ariadna-url http://ariadna.lan:19878 --project myconf \
    --server 'srv1c\myconf' --user Робот --dump-dir /var/lib/ariadna-dump/myconf \
    --extensions 'Расширение1,Расширение2'
```

### Расписание

Параметры удобнее держать в маленьком сценарии-обёртке, а секреты — в
отдельном файле, доступном только пользователю робота. Тогда ни cron, ни
systemd не нужно экранировать обратный слэш в имени базы.

`/etc/ariadna/update.env` (`chmod 600`, владелец — пользователь робота):

```bash
ARIADNA_TOKEN=ar_...
ONEC_PASSWORD=...
```

`/opt/ariadna/update-myconf.sh` (`chmod +x`):

```bash
#!/usr/bin/env bash
set -a; . /etc/ariadna/update.env; set +a
exec /opt/ariadna/update_from_infobase.sh --ariadna-url http://ariadna.lan:19878 \
    --project myconf --server 'srv1c\myconf' --user Робот \
    --dump-dir /var/lib/ariadna-dump/myconf --extensions 'Расширение1,Расширение2'
```

**cron** — `/etc/cron.d/ariadna-myconf`, каждую ночь в 02:00 от пользователя
`robot`:

```
0 2 * * * robot /opt/ariadna/update-myconf.sh >> /var/log/ariadna-update.log 2>&1
```

**или systemd** — `/etc/systemd/system/ariadna-update-myconf.service`:

```ini
[Unit]
Description=Ариадна: обновление индекса myconf

[Service]
Type=oneshot
User=robot
ExecStart=/opt/ariadna/update-myconf.sh
```

и `/etc/systemd/system/ariadna-update-myconf.timer`:

```ini
[Unit]
Description=Ариадна: обновление индекса myconf каждую ночь

[Timer]
OnCalendar=*-*-* 02:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ariadna-update-myconf.timer
sudo systemctl start ariadna-update-myconf.service     # проверить сразу
journalctl -u ariadna-update-myconf.service             # результат
```

## 5. Результат и журналы

- Журнал каждого запуска — `<DumpDir>\logs\update_<дата>.log`; журнал
  Конфигуратора — `<DumpDir>\logs\designer.log`; ответ Ариадны —
  `<DumpDir>\logs\ariadna_response.json`.
- Код возврата: `0` — индекс обновлён; `1` — ошибка выгрузки (причина в
  журнале Конфигуратора); `2` — ошибка передачи или индексации (нет доступа,
  неверный токен, ошибка индексации); `3` — неверные параметры.
- В Web UI: статус и время индексации — на «Обзоре» проекта и в «Настройки
  проекта → Данные».
- На Linux — те же файлы в `<dump-dir>/logs/` и те же коды возврата.

## Без скрипта: API

Скрипт — обёртка над одним запросом; его можно вызывать откуда угодно
(Linux, CI, свой сценарий):

```bash
# Выгрузка уже лежит в папке источника — переиндексировать и дождаться
curl -X POST -H "Authorization: Bearer ar_..." \
     http://ariadna.lan:19878/api/projects/myconf/sources/main/refresh

# Новая выгрузка архивом
curl -X POST -H "Authorization: Bearer ar_..." -F "xml_archive=@dump.zip" \
     http://ariadna.lan:19878/api/projects/myconf/sources/main/refresh

# Получить манифест источника (хеши хранятся после применения обновления)
curl -H "Authorization: Bearer ar_..." \
     http://ariadna.lan:19878/api/projects/myconf/sources/main/manifest

# Применить ZIP-разницу, построенную от версии манифеста
curl -X POST -H "Authorization: Bearer ar_..." \
     -F "base_version=<version>" -F "delta_archive=@delta.zip" \
     http://ariadna.lan:19878/api/projects/myconf/sources/main/delta
```

Поля формы: `xml_archive` — архив выгрузки (необязательно; старая папка
источника заменяется целиком), `wait` — `1` (по умолчанию) ждать окончания
или `0` вернуть ответ сразу, `timeout` — сколько ждать, секунд (по умолчанию
3600). Ответ — JSON: `status` (`ready`, `error`, `started`, `running`,
`busy`), `duration_sec`, `index` (число объектов, модулей, процедур,
вызовов). HTTP: 200 — готово, 202 — запущено или ещё идёт, 409 — проект
занят другой индексацией дольше `timeout`, 500 — индексация завершилась с
ошибкой. Роль токена — `user`.

Манифест — JSON вида `{"version":"…","files":{"путь/файл":{"sha256":"…","size":123}}}`.
В архиве разницы лежит `.ariadna-delta.json` с `base_version`, `target_version`,
полным новым `files` и `deleted`; остальные элементы архива — только изменённые
файлы. Ответ `409` означает устаревшую версию: запросите манифест заново и
пересоберите разницу. Загрузка ограничена по сжатому и распакованному размеру;
пути вне выгрузки и ссылки отклоняются.

Агент может обновить индекс сам: MCP-инструмент `reindex`, затем
`get_index_status` — дождаться готовности.
