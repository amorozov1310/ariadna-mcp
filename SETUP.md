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
docker run --rm -v "$PWD/docker-wheels:/wheels" python:3.12-slim sh /wheels/fetch.sh
```

На Windows — из PowerShell с полным путём:
`docker run --rm -v D:\путь\до\ariadna\docker-wheels:/wheels python:3.12-slim sh /wheels/fetch.sh`
(Git Bash искажает пути монтирования, файлы не попадут на диск).

### Шаг 3. Собрать и запустить

```bash
docker compose up -d --build
curl http://localhost:19878/health     # {"status":"ok"}
```

Проверяйте, что образ действительно пересобрался (`docker images ariadna`
— свежая дата): если сборка упала, `docker compose up` молча поднимет
старый образ.

### Шаг 4. Создать проект и загрузить данные

1. Откройте http://localhost:19878 и нажмите **«Создать проект»**.
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
claude mcp add --transport http --scope user ariadna http://localhost:19879/mcp
```

В Claude Code `/mcp` должен показать `ariadna` в статусе connected.
Cursor, `.mcp.json` в репозитории и legacy SSE — в README, раздел
«Подключение MCP к агенту».

### Свои порты (если 19877/19878/19879 заняты)

```bash
MCP_HOST_PORT=33000 MCP_HTTP_HOST_PORT=33002 WEB_HOST_PORT=33001 docker compose up -d --build
# Web UI:                http://localhost:33001
# MCP (streamable HTTP): http://localhost:33002/mcp
# MCP (SSE, legacy):     http://localhost:33000/sse
```

---

## Вариант 2: Без Docker (Python напрямую)

Для разработки и отладки. **Не запускайте на тех же `data/`, пока работает
контейнер** — см. README, «Индекс и Docker на Windows».

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

Web UI — http://localhost:9878, MCP — http://localhost:9879/mcp (в
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

1. http://localhost:19878 → **«Создать проект»**, ID `bp30`.
2. **«Добавить источник»**: ID `main`, тип «Основная», **файлы не
   загружайте** — каталог `xml/` подхватится с диска.
3. То же для `ext_bsp` с типом «Расширение».
4. **«Переиндексировать»**.

ID источника должен совпадать с именем каталога в `sources/`.

---

## Тесты

```bash
python -m pytest tests -q            # быстрые, ~10 с
python -m pytest tests -q -m slow    # полные корпуса из data/projects (bshp, 1idm2)
```

Slow-тесты переиндексируют `data/projects` на месте — остановите
контейнер перед запуском.

---

## Полезные команды

```bash
docker compose logs -f               # логи
docker compose stop                  # остановить
docker compose up -d --build         # пересобрать после изменений
docker restart ariadna               # если MCP/Web UI отвечают «unable to open database file»
docker compose exec ariadna bash     # зайти внутрь
curl http://localhost:19878/health   # здоровье
```

---

## Структура данных

```
data/
├── projects.json                   ← реестр проектов
└── projects/
    └── bp30/
        ├── index.db                ← SQLite-индекс (FTS5)
        └── sources/
            ├── main/
            │   ├── xml/            ← XML-выгрузка конфигурации
            │   └── report.txt      ← генерируется из xml/ при индексации
            └── ext_bsp/
                ├── xml/
                └── report.txt
```
