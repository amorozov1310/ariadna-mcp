# Ариадна — инструкция по локальному запуску

Три варианта: Docker (рекомендуется), Python напрямую (для разработки),
свои данные через volume. Справочник по MCP-инструментам и подключению
агентов — в [README.md](README.md).

## Вариант 1: Docker (рекомендуется)

### Шаг 1. Получить исходники

```bash
tar xzf ariadna.tar.gz
cd ariadna
```

### Шаг 2. Подготовить зависимости образа (один раз)

Python-зависимости ставятся в образ офлайн из каталога `docker-wheels/`.
Если в нём нет `.whl`-файлов, скачайте их — сеть используется из
обычного контейнера, а не из сборки:

```bash
docker run --rm -v "$PWD:/repo" -w /repo python:3.12-slim sh docker-wheels/fetch.sh
```

Запускать из корня репозитория: монтируется весь репозиторий, потому что
список зависимостей берётся из `pyproject.toml` (своего списка нет ни в
`fetch.sh`, ни в `Dockerfile`).

**На Windows — только из PowerShell**, с полным путём:
`docker run --rm -v D:\путь\до\ariadna:/repo -w /repo python:3.12-slim sh docker-wheels/fetch.sh`.
Git Bash искажает пути в `-v`, и колёса не попадут на диск.

Повторяйте команду после изменения зависимостей в `pyproject.toml` (и после
обновления с версии, где они изменились). Колёса сначала скачиваются во
временный каталог и только потом заменяют прежние — при сбое сети старый
набор остаётся, а устаревшие версии не копятся рядом с новыми.

### Шаг 3. Собрать и запустить

```bash
docker compose up -d --build
curl http://127.0.0.1:19878/health     # {"status":"ok"}
```

Проверяйте, что образ действительно пересобрался (`docker images ariadna`
— свежая дата): если сборка упала, `docker compose up` молча поднимет
старый образ.

### Шаг 4. Создать проект и загрузить данные

1. Откройте http://127.0.0.1:19878 и нажмите **«Создать проект»**.
2. ID: `bp30` (латиница), название: `Бухгалтерия 3.0`.
3. На странице проекта — **«Добавить источник»**: ID `main`, тип
   «Основная», загрузите XML-выгрузку конфигурации в `.zip`
   (Конфигуратор → «Конфигурация → Выгрузить конфигурацию в файлы»).
   Отчёт по конфигурации выгружать не нужно — он строится из XML сам.
4. Расширения — тем же способом, тип «Расширение».
5. Нажмите **«Переиндексировать»**. Прогресс виден на странице; ERP
   индексируется порядка 6–8 минут.

### Шаг 5. Проверить

- **Поиск** — раздел «Поиск», запрос `Номенклатура`.
- **Playground** — выберите `search_metadata`, выполните запрос: это тот же
  код, что отвечает MCP-клиентам.

### Шаг 6. Подключить агента

```bash
claude mcp add --transport http --scope user ariadna http://127.0.0.1:19879/mcp
```

В Claude Code `/mcp` должен показать `ariadna` в статусе connected.
Cursor, Codex CLI, `.mcp.json` в репозитории и legacy SSE — в README,
раздел «Подключение MCP к агенту». Без агента тоже можно — см. в README
раздел «Web UI».

### Свои порты (если 19877/19878/19879 заняты)

Порты публикуются только на `127.0.0.1` (`BIND_ADDR`, по умолчанию
`127.0.0.1`). Доступ с других машин — `BIND_ADDR=0.0.0.0` плюс
`MCP_ALLOWED_HOSTS` с адресом или именем машины (иначе MCP и Web UI
отклонят чужой `Host`), только в доверенной сети: авторизации нет
(подробнее — README, «Сервер на другой машине»).

```bash
MCP_HOST_PORT=33000 MCP_HTTP_HOST_PORT=33002 WEB_HOST_PORT=33001 docker compose up -d --build
# Web UI:                http://127.0.0.1:33001
# MCP (streamable HTTP): http://127.0.0.1:33002/mcp
# MCP (SSE, legacy):     http://127.0.0.1:33000/sse
```

---

## Вариант 2: Без Docker (Python напрямую)

Для разработки и отладки. Нативный запуск без `INDEX_DIR` держит индексы в
`data/projects/{id}/index.db`, а контейнер — на томе `ariadna-index`, так что
индексы у них разные. Выгрузки и `projects.json` общие — **не запускайте
на тех же `data/`, пока работает контейнер**. См. README, «Индексы в Docker».

### Требования

- Python 3.11+

### Шаг 1. Окружение и зависимости

```bash
python -m venv .venv
source .venv/bin/activate      # Linux/macOS
# .venv\Scripts\activate       # Windows

pip install -e ".[dev]"
# генерация отчёта из XML (иначе источники без report.txt индексируются без метаданных):
pip install -e ".[xml-report]"
```

### Шаг 2. Тестовые данные (необязательно)

```bash
python scripts/generate_test_data.py ./data
```

Создаёт проект `pro100agro` из отчёта-фикстуры и индексирует его:
метаданные есть, кода BSL нет (граф вызовов и поиск по коду будут пустыми).

### Шаг 3. Запустить

**Web UI + MCP:**

```bash
DATA_DIR=./data WEB_PORT=9878 MCP_PORT=9877 MCP_HTTP_PORT=9879 python -m src.main
```

**Только Web UI с автоперезагрузкой при правке кода:**

```bash
DATA_DIR=./data uvicorn src.web.app:app --host 0.0.0.0 --port 9878 --reload
```

Web UI — http://127.0.0.1:9878, MCP — http://127.0.0.1:9879/mcp (в
нативном запуске порты без префикса `1`).

---

## Вариант 3: Свои данные через volume (БП, ERP)

Когда выгрузка уже лежит на диске и загружать zip через браузер
неудобно (ERP — гигабайты).

### Шаг 1. Разложить выгрузки

```bash
mkdir -p data/projects/bp30/sources/main/xml
mkdir -p data/projects/bp30/sources/ext_bsp/xml

cp -r /путь/к/выгрузке_конфигурации/*  data/projects/bp30/sources/main/xml/
cp -r /путь/к/выгрузке_расширения/*    data/projects/bp30/sources/ext_bsp/xml/
```

Внутри `xml/` должен лежать `Configuration.xml`.

### Шаг 2. Запустить и зарегистрировать

```bash
docker compose up -d --build
```

1. http://127.0.0.1:19878 → **«Создать проект»**, ID `bp30`.
2. **«Добавить источник»**: ID `main`, тип «Основная», **файлы не
   загружайте** — каталог `xml/` подхватится с диска.
3. То же для `ext_bsp` с типом «Расширение».
4. **«Переиндексировать»**.

ID источника должен совпадать с именем каталога в `sources/`.

---

## Тесты

Тесты запускаются в окружении, собранном по `pyproject.toml`, — в `.venv`
из «Варианта 2», а не в глобальном Python:

```bash
python -m venv .venv
source .venv/bin/activate      # Linux/macOS
# .venv\Scripts\activate       # Windows
pip install -e ".[dev]"
```

Перед запуском `tests/conftest.py` сверяет установленные версии с
`[project].dependencies` и extra `dev`. Если что-то не подходит (например,
в глобальном Python Starlette 0.52 и mcp 1.x), сессия сразу прерывается со
списком пакетов и этими командами — вместо «400 != 200» в тесте Host и
молча пропущенных тестов SDK.

```bash
python -m pytest tests -q            # быстрые, ~10 с
python -m pytest tests -q -m slow    # полные корпуса из data/projects (bshp, 1idm2)
```

Slow-тесты читают выгрузки из `data/projects`, но ничего в `data/` не
пишут: реестр, индексы и сгенерированный `report.txt` — во временном
каталоге, который удаляется после теста. Контейнер останавливать не нужно;
после каждого теста проверяется, что `data/projects.json` и
`data/projects/*/` не изменились.

---

## Полезные команды

```bash
docker compose logs -f               # логи
docker compose stop                  # остановить
docker compose up -d --build         # пересобрать после изменений
docker restart ariadna               # если MCP/Web UI отвечают «unable to open database file» (индексы в ./data, без INDEX_DIR)
docker compose down && docker volume rm ariadna-index   # удалить индексы Docker; затем up -d и reindex проектов
docker compose exec ariadna bash     # зайти внутрь
curl http://127.0.0.1:19878/health   # здоровье
```

---

## Структура данных

```
data/
├── projects.json                   ← реестр проектов
└── projects/
    └── bp30/
        ├── index.db                ← SQLite-индекс (FTS5), только без Docker
        └── sources/
            ├── main/
            │   ├── xml/            ← XML-выгрузка конфигурации
            │   └── report.txt      ← генерируется из xml/ при индексации
            └── ext_bsp/
                ├── xml/
                └── report.txt
```

В Docker индексы лежат на именованном томе `ariadna-index`
(`/index/{проект}/index.db` в контейнере, `INDEX_DIR=/index` в
`docker-compose.yml`). При первом старте после обновления существующие
`data/projects/*/index.db` один раз копируются на том — ход переноса виден
в `docker compose logs`; старые файлы после этого можно удалить вручную.
Индекс с нуля — `reindex` проекта или `docker volume rm ariadna-index`
(при остановленном контейнере). Подробнее — README, «Индексы в Docker».
