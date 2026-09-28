# 1C Configuration MCP Server — Project Instructions

> **Исходная техническая спецификация проекта (фазы 1A–4).** Описывает
> замысел и архитектуру; реализация с тех пор ушла вперёд. Актуальный
> справочник по MCP-инструментам, подключению агентов и портам —
> [README.md](README.md), по запуску — [SETUP.md](SETUP.md). Там, где
> этот документ расходится с ними, верны они.

## Название проекта: `ariadna` («Ариадна»)

## Цель

Docker-образ с MCP-сервером для быстрого доступа к метаданным конфигураций 1С и анализа кода BSL. Позволяет AI-агентам (Claude Code, Cursor, Claude.ai) за миллисекунды находить объекты, реквизиты, процедуры и строить цепочки вызовов — вместо того чтобы тратить контекст и токены на полное сканирование.

### Ключевые принципы

1. **Один контейнер — много проектов.** Каждый проект (`bp30`, `erp`, `pro100agro`) — отдельная конфигурация со своим индексом. Все tools работают в рамках `project_id`.

2. **Проект = конфигурация + расширения.** Один проект может включать несколько источников данных: основную конфигурацию + любое количество расширений. Все индексируются вместе в единую базу, чтобы поиск и call tree работали сквозь всю кодовую базу.

3. **Web UI для управления проектами.** Создание, настройка, индексация, мониторинг — всё через браузер. Тут же playground для тестирования MCP tools.

4. **MCP tools = project-scoped.** Каждый вызов MCP tool получает `project_id`. AI-агент сначала выбирает проект, потом работает с ним.

### Целевые конфигурации

- **Для обучения/отладки:** ПРО100АГРО (компактная — 42 справочника, 12 документов)
- **Первый production:** Бухгалтерия предприятия 3.0
- **Конечная цель:** ERP (десятки тысяч модулей)

---

## 1. Архитектура

### 1.1 Общая схема

```
┌─────────────────────────────────────────────────────────────────┐
│                      Docker Container                            │
│                                                                  │
│  ┌──────────────────┐   ┌───────────────────────┐               │
│  │   MCP Server      │   │     Web UI             │               │
│  │   (SSE/stdio)     │   │   (FastAPI + HTMX)     │               │
│  │   :9877 internal   │   │   :9878 internal       │               │
│  └────────┬─────────┘   └──────────┬────────────┘               │
│           │                        │                             │
│           └──────────┬─────────────┘                             │
│                      │                                           │
│           ┌──────────▼──────────┐                                │
│           │    Core Engine       │                                │
│           │                      │                                │
│           │ • ProjectManager     │                                │
│           │ • ReportParser       │                                │
│           │ • XMLWalker (BSL)    │                                │
│           │ • BSLParser          │                                │
│           │ • CallAnalyzer       │                                │
│           │ • SearchEngine       │                                │
│           │ • Indexer            │                                │
│           └──────────┬──────────┘                                │
│                      │                                           │
│    ┌─────────────────┼─────────────────┐                         │
│    │                 │                 │                          │
│    ▼                 ▼                 ▼                          │
│  ┌────────┐   ┌────────┐   ┌────────┐                           │
│  │bp30.db │   │erp.db  │   │pro100  │  ← SQLite per project     │
│  │        │   │        │   │agro.db │                            │
│  └────────┘   └────────┘   └────────┘                           │
│                                                                  │
│  /data/                                                          │
│  ├── projects.json          ← Реестр проектов                    │
│  ├── projects/                                                   │
│  │   ├── bp30/                                                   │
│  │   │   ├── index.db       ← SQLite индекс                     │
│  │   │   └── sources/       ← Исходные данные                   │
│  │   │       ├── main/                                           │
│  │   │       │   ├── report.txt                                  │
│  │   │       │   └── xml/   ← XML-выгрузка основной конфиг.     │
│  │   │       └── ext_bsp/                                        │
│  │   │           ├── report.txt                                  │
│  │   │           └── xml/   ← XML-выгрузка расширения            │
│  │   ├── erp/                                                    │
│  │   │   └── ...                                                 │
│  │   └── pro100agro/                                             │
│  │       └── ...                                                 │
│  └── uploads/               ← Временная папка для загрузок       │
└─────────────────────────────────────────────────────────────────┘

Порты (Docker):
  Host 19877 → Container 9877 (MCP SSE)
  Host 19878 → Container 9878 (Web UI)
```

### 1.2 Модель данных проекта

```json
// projects.json
{
  "projects": {
    "bp30": {
      "id": "bp30",
      "name": "Бухгалтерия 3.0",
      "description": "Типовая БП 3.0 + расширение для сельхоз",
      "created_at": "2026-03-17T12:00:00",
      "status": "ready",
      "sources": [
        {
          "id": "main",
          "label": "Основная конфигурация",
          "report_path": "sources/main/report.txt",
          "xml_path": "sources/main/xml"
        },
        {
          "id": "ext_agro",
          "label": "Расширение: Агро-модуль",
          "report_path": "sources/ext_agro/report.txt",
          "xml_path": "sources/ext_agro/xml"
        }
      ],
      "index_stats": {
        "total_objects": 5420,
        "total_attributes": 28900,
        "total_modules": 1200,
        "total_procedures": 45000,
        "last_indexed": "2026-03-17T14:30:00",
        "duration_sec": 12.5
      }
    }
  },
  "active_project": "bp30"
}
```

### 1.3 Рекомендуемый стек

| Компонент         | Технология              | Обоснование                                        |
|-------------------|-------------------------|----------------------------------------------------|
| MCP Server        | Python + `mcp` SDK      | Зрелый SDK, SSE/stdio                              |
| Web API + UI      | FastAPI + Jinja2 + HTMX | Минималистичный UI без фронтенд-билда              |
| Парсинг отчёта    | Pure Python             | Формат прост, lxml не нужен                        |
| Парсинг BSL       | Regex (MVP) → tree-sitter | Regex для 90% случаев, AST позже              |
| Индекс/поиск      | SQLite + FTS5           | Один файл на проект, полнотекстовый поиск          |
| Контейнеризация   | Docker + docker-compose | Volume для данных                                  |

### 1.4 Схема БД SQLite (одна база на проект)

```sql
-- ============================================
-- МЕТАДАННЫЕ
-- ============================================

CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    source_type TEXT DEFAULT 'main',     -- 'main' | 'extension'
    report_path TEXT DEFAULT '',
    xml_path    TEXT DEFAULT '',
    indexed_at  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS metadata_objects (
    id          INTEGER PRIMARY KEY,
    source_id   TEXT REFERENCES sources(id),  -- Из какого источника
    name        TEXT NOT NULL,
    full_name   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    parent_id   INTEGER REFERENCES metadata_objects(id),
    synonym     TEXT DEFAULT '',
    comment     TEXT DEFAULT '',
    UNIQUE(full_name, source_id)
);

CREATE TABLE IF NOT EXISTS attributes (
    id          INTEGER PRIMARY KEY,
    object_id   INTEGER NOT NULL REFERENCES metadata_objects(id),
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,         -- Реквизит, Измерение, Ресурс, ТабличнаяЧасть, ЗначениеПеречисления
    type_desc   TEXT DEFAULT '',
    synonym     TEXT DEFAULT '',
    comment     TEXT DEFAULT '',
    is_indexed  BOOLEAN DEFAULT FALSE,
    check_fill  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS forms (
    id          INTEGER PRIMARY KEY,
    object_id   INTEGER NOT NULL REFERENCES metadata_objects(id),
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subsystem_content (
    id           INTEGER PRIMARY KEY,
    subsystem_id INTEGER NOT NULL REFERENCES metadata_objects(id),
    content_name TEXT NOT NULL
);

-- ============================================
-- КОД (BSL) — Фаза 2
-- ============================================

CREATE TABLE IF NOT EXISTS modules (
    id           INTEGER PRIMARY KEY,
    source_id    TEXT REFERENCES sources(id),
    object_id    INTEGER REFERENCES metadata_objects(id),
    name         TEXT NOT NULL,
    module_type  TEXT NOT NULL,
    file_path    TEXT NOT NULL DEFAULT '',
    file_hash    TEXT DEFAULT '',
    is_server    BOOLEAN DEFAULT FALSE,
    is_client    BOOLEAN DEFAULT FALSE,
    is_global    BOOLEAN DEFAULT FALSE,
    is_privileged BOOLEAN DEFAULT FALSE,
    server_call  BOOLEAN DEFAULT FALSE,
    line_count   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS procedures (
    id          INTEGER PRIMARY KEY,
    module_id   INTEGER NOT NULL REFERENCES modules(id),
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,           -- Процедура | Функция
    is_export   BOOLEAN DEFAULT FALSE,
    start_line  INTEGER DEFAULT 0,
    end_line    INTEGER DEFAULT 0,
    params      TEXT DEFAULT '',
    directive   TEXT DEFAULT '',          -- &НаСервере, &НаКлиенте...
    signature   TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS calls (
    id           INTEGER PRIMARY KEY,
    caller_id    INTEGER NOT NULL REFERENCES procedures(id),
    callee_name  TEXT NOT NULL,
    callee_module TEXT DEFAULT '',
    callee_proc  TEXT NOT NULL,
    line         INTEGER DEFAULT 0,
    context      TEXT DEFAULT ''
);

-- ============================================
-- FTS5 + TRIGGERS + INDEXES
-- ============================================

CREATE VIRTUAL TABLE IF NOT EXISTS metadata_fts USING fts5(
    full_name, name, synonym, comment,
    content='metadata_objects', content_rowid='id'
);

CREATE VIRTUAL TABLE IF NOT EXISTS attributes_fts USING fts5(
    name, synonym, type_desc, comment,
    content='attributes', content_rowid='id'
);

CREATE VIRTUAL TABLE IF NOT EXISTS procedures_fts USING fts5(
    name, signature,
    content='procedures', content_rowid='id'
);

-- Auto-sync triggers for metadata_objects → metadata_fts
CREATE TRIGGER IF NOT EXISTS mo_ai AFTER INSERT ON metadata_objects BEGIN
    INSERT INTO metadata_fts(rowid, full_name, name, synonym, comment)
    VALUES (new.id, new.full_name, new.name, new.synonym, new.comment);
END;
CREATE TRIGGER IF NOT EXISTS mo_ad AFTER DELETE ON metadata_objects BEGIN
    INSERT INTO metadata_fts(metadata_fts, rowid, full_name, name, synonym, comment)
    VALUES ('delete', old.id, old.full_name, old.name, old.synonym, old.comment);
END;

-- Same pattern for attributes_fts and procedures_fts
CREATE TRIGGER IF NOT EXISTS attr_ai AFTER INSERT ON attributes BEGIN
    INSERT INTO attributes_fts(rowid, name, synonym, type_desc, comment)
    VALUES (new.id, new.name, new.synonym, new.type_desc, new.comment);
END;
CREATE TRIGGER IF NOT EXISTS attr_ad AFTER DELETE ON attributes BEGIN
    INSERT INTO attributes_fts(attributes_fts, rowid, name, synonym, type_desc, comment)
    VALUES ('delete', old.id, old.name, old.synonym, old.type_desc, old.comment);
END;

CREATE TRIGGER IF NOT EXISTS proc_ai AFTER INSERT ON procedures BEGIN
    INSERT INTO procedures_fts(rowid, name, signature)
    VALUES (new.id, new.name, new.signature);
END;
CREATE TRIGGER IF NOT EXISTS proc_ad AFTER DELETE ON procedures BEGIN
    INSERT INTO procedures_fts(procedures_fts, rowid, name, signature)
    VALUES ('delete', old.id, old.name, old.signature);
END;

CREATE INDEX IF NOT EXISTS idx_mo_kind ON metadata_objects(kind);
CREATE INDEX IF NOT EXISTS idx_mo_source ON metadata_objects(source_id);
CREATE INDEX IF NOT EXISTS idx_attr_object ON attributes(object_id);
CREATE INDEX IF NOT EXISTS idx_forms_object ON forms(object_id);
CREATE INDEX IF NOT EXISTS idx_modules_object ON modules(object_id);
CREATE INDEX IF NOT EXISTS idx_proc_module ON procedures(module_id);
CREATE INDEX IF NOT EXISTS idx_proc_name ON procedures(name);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id);
CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_proc, callee_module);

-- ============================================
-- СОСТОЯНИЕ ИНДЕКСА
-- ============================================

CREATE TABLE IF NOT EXISTS index_state (
    id                  INTEGER PRIMARY KEY DEFAULT 1,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    config_name         TEXT DEFAULT '',
    config_version      TEXT DEFAULT '',
    total_objects       INTEGER DEFAULT 0,
    total_attributes    INTEGER DEFAULT 0,
    total_forms         INTEGER DEFAULT 0,
    total_modules       INTEGER DEFAULT 0,
    total_procedures    INTEGER DEFAULT 0,
    total_calls         INTEGER DEFAULT 0,
    index_duration_sec  REAL DEFAULT 0,
    status              TEXT DEFAULT 'empty'
);
```

---

## 2. MCP Tools (project-scoped)

Каждый MCP tool, кроме `list_projects`, работает в рамках `project_id` (необязателен, если проект один). MCP-сервер держит пул подключений к SQLite-базам проектов. Текущий список (16 инструментов, с пагинацией `limit`/`offset`, `list_sources`, `diagnose_index`, `remove_source`) — в README; ниже — исходный замысел.

### 2.1 Управление проектами

```yaml
Tool: list_projects
  Input: (нет)
  Output: Список проектов с id, name, status, stats

# set_active_project был в исходном замысле и удалён: состояние между
# вызовами не сохранялось, project_id всё равно требовался.
```

### 2.2 Поиск по метаданным

```yaml
Tool: search_metadata
  Input:
    project_id: str
    query: str
    kind: str | None       # Фильтр: "Справочник", "Документ"...
    limit: int = 20
  Output: Список: full_name, kind, synonym, comment

Tool: get_object_details
  Input:
    project_id: str
    full_name: str         # "Справочник.Номенклатура"
  Output: Карточка: реквизиты, ТЧ с типами, формы, модули

Tool: search_attributes
  Input:
    project_id: str
    query: str
    type_filter: str | None
  Output: Список: attr_name, object_full_name, type_desc

Tool: find_references
  Input:
    project_id: str
    object_name: str       # "Справочник.Номенклатура"
  Output: Где используется в типах реквизитов других объектов
```

### 2.3 Анализ кода (Фаза 2)

```yaml
Tool: search_procedures
  Input:
    project_id: str
    query: str
    module_filter: str | None
    export_only: bool = False
  Output: Список процедур с модулем, директивой, строкой

Tool: get_procedure_code
  Input:
    project_id: str
    module_path: str
    procedure_name: str
  Output: Полный код процедуры

Tool: get_call_tree
  Input:
    project_id: str
    procedure_name: str
    module_name: str | None
    direction: str = "down"    # "down" | "up"
    depth: int = 3
  Output: Текстовое дерево вызовов

Tool: search_code
  Input:
    project_id: str
    query: str
    file_pattern: str | None
    limit: int = 30
  Output: Список: file_path, line, context

Tool: get_module_outline
  Input:
    project_id: str
    module_path: str
  Output: Структура модуля
```

### 2.4 Управление индексом

```yaml
Tool: get_index_status
  Input:
    project_id: str
  Output: Статус, дата, статистика

Tool: reindex
  Input:
    project_id: str
    source_id: str | None    # None = все источники
  Output: Результат переиндексации
```

---

## 3. Web UI

### 3.1 Страницы

| Маршрут                          | Описание                                              |
|----------------------------------|-------------------------------------------------------|
| `GET /`                          | Список проектов, кнопка «Создать проект»              |
| `GET /projects/{id}`             | Дашборд проекта: статистика, источники, индексация    |
| `GET /projects/{id}/search`      | Поиск по метаданным и коду проекта                    |
| `GET /projects/{id}/object/{name}` | Карточка объекта                                    |
| `GET /projects/{id}/calltree`    | Интерактивное дерево вызовов                          |
| `GET /projects/{id}/playground`  | Playground: тестирование MCP tools                    |
| `POST /api/projects`             | Создать проект                                        |
| `POST /api/projects/{id}/sources`| Добавить источник (upload report/xml)                 |
| `POST /api/projects/{id}/reindex`| Запустить индексацию                                  |
| `DELETE /api/projects/{id}`      | Удалить проект                                        |

### 3.2 Главная — Список проектов

```
┌───────────────────────────────────────────────────────────────┐
│  Ариадна                                  [+ Создать]   │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │ ★ bp30 — Бухгалтерия 3.0              ✅ Ready         │ │
│  │   2 источника · 5420 объектов · 45K процедур            │ │
│  │   Обновлён: 17.03.2026 14:30                            │ │
│  └─────────────────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │   pro100agro — ПРО100АГРО              ✅ Ready         │ │
│  │   1 источник · 159 объектов · 0 процедур                │ │
│  │   Обновлён: 17.03.2026 12:00                            │ │
│  └─────────────────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │   erp — 1С:ERP                         ⏳ Indexing...   │ │
│  │   3 источника · индексация 45%                          │ │
│  └─────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────┘
```

### 3.3 Дашборд проекта

```
┌───────────────────────────────────────────────────────────────┐
│  ← Проекты    bp30 — Бухгалтерия 3.0     [Поиск] [Playground]│
│                                                               │
│  Статус: ✅ Ready (обновлён 17.03.2026 14:30, 12.5с)        │
│                                                               │
│  ┌──────────┬──────────┬──────────┬──────────┐               │
│  │ Объектов │ Реквизитов│ Модулей │ Процедур │               │
│  │  5,420   │  28,900  │  1,200  │  45,000  │               │
│  └──────────┴──────────┴──────────┴──────────┘               │
│                                                               │
│  Источники:                                                   │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │ 📦 main — Основная конфигурация                        │ │
│  │   report.txt ✅  xml/ ✅   4,800 объектов               │ │
│  ├─────────────────────────────────────────────────────────┤ │
│  │ 📦 ext_agro — Расширение: Агро-модуль                  │ │
│  │   report.txt ✅  xml/ ✅   620 объектов                  │ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                               │
│  [+ Добавить источник]  [🔄 Переиндексировать]  [🗑 Удалить] │
└───────────────────────────────────────────────────────────────┘
```

### 3.4 Playground — тестирование MCP tools

Позволяет выбрать tool, увидеть описание параметров, ввести значения, выполнить и увидеть результат. Работает в рамках текущего проекта. Хранит историю последних 6 запросов в сессии.

```
┌───────────────────────────────────────────────────────────────┐
│  ← bp30    MCP Tools Playground                               │
│                                                               │
│  Tool: [▼ search_metadata          ]                         │
│                                                               │
│  📖 Полнотекстовый поиск по объектам конфигурации.           │
│     Ищет по имени, синониму, комментарию.                     │
│                                                               │
│  project_id: bp30  (автозаполнено)                            │
│  query:      [Номенклатура____________]                       │
│  kind:       [▼ Любой / Справочник / Документ / ...]         │
│  limit:      [20_]                                            │
│                                                               │
│  [▶ Выполнить]                                                │
│                                                               │
│  ┌─ Результат ──────────────────────────────────────────────┐│
│  │ Found 3 results:                                          ││
│  │ 1. Справочник.Номенклатура (Номенклатура)                ││
│  │ 2. РегистрСведений.ЦеныНоменклатуры — Цены на товары    ││
│  │ 3. Документ.ПоступлениеТоваров.ТЧ.Товары.Номенклатура   ││
│  └──────────────────────────────────────────────────────────┘│
│  ⏱ 0.3ms · project: bp30                                     │
│                                                               │
│  ┌─ История (последние 6) ──────────────────────────────────┐│
│  │ 14:32 search_metadata("Номенклатура") → 3 results         ││
│  │ 14:31 get_object_details("Справочник.Поля") → ok          ││
│  │ 14:30 search_attributes("Культура") → 18 results          ││
│  │ 14:28 find_references("Культуры") → 12 results            ││
│  └──────────────────────────────────────────────────────────┘│
└───────────────────────────────────────────────────────────────┘
```

---

## 4. Структура проекта

```
ariadna/
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── README.md
│
├── src/
│   ├── __init__.py
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   ├── project_manager.py    # CRUD проектов, projects.json, пул БД
│   │   ├── db.py                 # SQLite: схема, миграции, FTS
│   │   ├── report_parser.py      # Парсинг отчёта конфигурации (UTF-16LE)
│   │   ├── xml_walker.py         # Обход XML-выгрузки для .bsl файлов (Фаза 2)
│   │   ├── bsl_parser.py         # Парсинг BSL-модулей (Фаза 2)
│   │   ├── call_analyzer.py      # Граф вызовов (Фаза 2)
│   │   ├── indexer.py            # Оркестрация: источники → SQLite
│   │   └── search.py             # FTS5 запросы к индексу
│   │
│   ├── mcp_server/
│   │   ├── __init__.py
│   │   ├── server.py             # MCP init, transport, tool routing
│   │   └── tools.py              # MCP tools (project-scoped)
│   │
│   └── web/
│       ├── __init__.py
│       ├── app.py                # FastAPI app
│       ├── routes_projects.py    # CRUD проектов, загрузка файлов
│       ├── routes_search.py      # Поиск, карточки объектов
│       ├── routes_playground.py  # Tool playground
│       ├── templates/
│       │   ├── base.html
│       │   ├── projects_list.html
│       │   ├── project_dashboard.html
│       │   ├── project_create.html
│       │   ├── search.html
│       │   ├── object_detail.html
│       │   ├── calltree.html
│       │   └── playground.html
│       └── static/
│           └── style.css
│
├── tests/
│   ├── test_report_parser.py
│   ├── test_project_manager.py
│   ├── test_search.py
│   └── fixtures/
│       └── ОтчетПоКонфигурации.txt
│
└── scripts/
    ├── entrypoint.sh
    └── generate_test_data.py
```

---

## 5. Формат входных данных

### 5.1 Отчёт по конфигурации (ОСНОВНОЙ источник метаданных)

Формируется в Конфигураторе: «Конфигурация → Отчёт по конфигурации». Файл UTF-16LE с BOM.

#### Структура: иерархический текст с табуляцией

```
- Конфигурации.ИмяКонфигурации
\t\tИмя: "ИмяКонфигурации"
\t\tВерсия: "3.0.150.28"
\t\t- Справочники.Номенклатура
\t\t\tИмя: "Номенклатура"
\t\t\tСиноним: "Номенклатура"
\t\t\t- Справочники.Номенклатура.Реквизиты.Артикул
\t\t\t\tТип:
\t\t\t\t\t"Строка(50, Переменная)"
\t\t\t- Справочники.Номенклатура.ТабличныеЧасти.ТЧ
\t\t\t\t- ...ТабличныеЧасти.ТЧ.Реквизиты.Поле
\t\t\t- Справочники.Номенклатура.Формы.ФормаЭлемента
```

#### Правила парсинга

1. `- ТипОбъекта.ИмяОбъекта` — определение объекта
2. `Ключ: "Значение"` — свойство
3. `Тип:` → следующая строка `"Строка(50)"` или `"СправочникСсылка.Контрагенты"`
4. `Состав:` → массив элементов в кавычках на следующих строках
5. Уровень вложенности = количество табов

#### Ключевые свойства общих модулей (для разрешения вызовов)

```
Глобальный: "Истина"/"Ложь"
Сервер: "Истина"/"Ложь"
КлиентУправляемоеПриложение: "Истина"/"Ложь"
ВызовСервера: "Истина"/"Ложь"
Привилегированный: "Истина"/"Ложь"
```

### 5.2 XML-выгрузка (для BSL-кода, Фаза 2)

```
ConfigDump/
├── CommonModules/МодульИмя/Ext/Module.bsl
├── Catalogs/Имя/Ext/ObjectModule.bsl
├── Documents/Имя/Ext/ObjectModule.bsl
│                   └── Forms/ФормаДок/Ext/Form/Module.bsl
└── ...
```

### 5.3 Комбинирование источников в проекте

Один проект может содержать несколько source. **Каждое расширение — отдельный source со своим report.txt и своей XML-выгрузкой:**

```
Проект bp30:
  source "main"      → report.txt (Бухгалтерия 3.0)      + xml/ выгрузка
  source "ext_bsp"   → report.txt (расширение БСП)        + xml/ выгрузка
  source "ext_agro"  → report.txt (расширение Агро-модуль) + xml/ выгрузка
```

Все индексируются в одну SQLite базу. Каждый объект/модуль помечен `source_id`, чтобы при переиндексации одного расширения не трогать остальные.

### 5.4 Способы загрузки данных

Поддерживаются **оба способа**:

**Способ A — Upload через Web UI:**
1. Создать проект в UI
2. Добавить source → загрузить report.txt + xml.zip (или xml.tar.gz)
3. Файлы сохраняются в `/data/projects/{id}/sources/{source_id}/`
4. Запустить индексацию

**Способ B — Монтирование volume:**
1. Положить файлы на хосте в заранее известную структуру:
   ```
   ./data/projects/bp30/sources/main/report.txt
   ./data/projects/bp30/sources/main/xml/
   ./data/projects/bp30/sources/ext_agro/report.txt
   ./data/projects/bp30/sources/ext_agro/xml/
   ```
2. Примонтировать `./data:/data` в docker-compose
3. Создать проект через UI (или API), указав существующие пути
4. Запустить индексацию

> **Для больших конфигураций (ERP, УТ)** рекомендуется volume — upload гигабайтных XML-выгрузок через браузер неудобен.

---

## 6. project_manager.py — ядро мультипроектности

```python
class ProjectManager:
    """
    Manages multiple 1C projects within one container.
    Stores registry in /data/projects.json.
    Each project gets its own SQLite database at /data/projects/{id}/index.db.
    """

    def __init__(self, data_dir: str = '/data'):
        self.data_dir = Path(data_dir)
        self.registry_path = self.data_dir / 'projects.json'
        self._db_pool: dict[str, Database] = {}

    def list_projects(self) -> list[ProjectInfo]: ...
    def create_project(self, id: str, name: str, description: str = '') -> ProjectInfo: ...
    def get_project(self, project_id: str) -> ProjectInfo: ...
    def delete_project(self, project_id: str) -> None: ...

    def add_source(self, project_id: str, source_id: str, label: str,
                   report_file: UploadFile | None, xml_archive: UploadFile | None) -> SourceInfo: ...
    def remove_source(self, project_id: str, source_id: str) -> None: ...

    def reindex(self, project_id: str, source_id: str | None = None) -> IndexStats: ...

    def get_db(self, project_id: str) -> Database:
        """Get or open SQLite connection for project (connection pool)."""
        ...

    def get_search(self, project_id: str) -> SearchEngine:
        """Get SearchEngine instance for project."""
        ...
```

---

## 7. Пошаговый план реализации

### Фаза 1A — Фундамент + мультипроектность (Milestone 1)

**Цель:** проект создаётся, отчёт парсится, индекс работает, поиск за миллисекунды.

| #   | Задача                                                     | Файлы                     |
|-----|------------------------------------------------------------|---------------------------|
| 1.1 | Инициализация проекта: pyproject.toml, структура           | Корень                    |
| 1.2 | Схема БД SQLite + FTS5 с source_id                         | `core/db.py`              |
| 1.3 | ProjectManager: CRUD проектов, projects.json               | `core/project_manager.py` |
| 1.4 | ProjectManager: добавление sources, хранение файлов        | `core/project_manager.py` |
| 1.5 | Парсер отчёта UTF-16LE → структуры данных                  | `core/report_parser.py`   |
| 1.6 | Indexer: оркестрация sources → SQLite (multi-source)       | `core/indexer.py`         |
| 1.7 | SearchEngine с FTS5 + LIKE fallback для кириллицы          | `core/search.py`          |
| 1.8 | Тесты на реальном отчёте ПРО100АГРО                        | `tests/`                  |

**Критерий:** `ProjectManager.create_project("test")`, добавить source с отчётом, `reindex()`, `search("Культура")` → результат < 10мс.

### Фаза 1B — Web UI для управления проектами (Milestone 2)

| #   | Задача                                                     | Файлы                     |
|-----|------------------------------------------------------------|---------------------------|
| 1.9 | FastAPI app + базовый шаблон (Jinja2 + HTMX + CSS)        | `web/app.py`, `templates/`|
| 1.10| Список проектов (главная)                                  | `projects_list.html`      |
| 1.11| Создание проекта (форма)                                   | `project_create.html`     |
| 1.12| Дашборд проекта: статистика, sources, индексация           | `project_dashboard.html`  |
| 1.13| Загрузка файлов (report.txt, xml.zip) через UI             | `routes_projects.py`      |
| 1.14| Запуск индексации с прогресс-баром (HTMX SSE)              | `routes_projects.py`      |

### Фаза 1C — Поиск + Playground (Milestone 3)

| #   | Задача                                                     | Файлы                     |
|-----|------------------------------------------------------------|---------------------------|
| 1.15| Страница поиска по метаданным (HTMX)                       | `search.html`             |
| 1.16| Карточка объекта                                           | `object_detail.html`      |
| 1.17| MCP Tools Playground: выбор tool, форма параметров, выполнение | `playground.html`      |
| 1.18| Playground: подсказки по каждому tool                       | `routes_playground.py`    |

### Фаза 2 — BSL-парсинг и дерево вызовов (Milestone 4)

| #   | Задача                                                     | Файлы                     |
|-----|------------------------------------------------------------|---------------------------|
| 2.1 | BSL regex-парсер: процедуры, функции, директивы            | `core/bsl_parser.py`      |
| 2.2 | XML walker: поиск .bsl файлов в выгрузке                   | `core/xml_walker.py`      |
| 2.3 | Извлечение вызовов, разрешение по модулям                   | `core/call_analyzer.py`   |
| 2.4 | Построение call tree (up/down, depth)                       | `core/call_analyzer.py`   |
| 2.5 | Интеграция BSL-индексации + multi-source                    | `core/indexer.py`         |
| 2.6 | UI: дерево вызовов                                          | `calltree.html`           |

### Фаза 3 — MCP-сервер (Milestone 5)

| #   | Задача                                                     | Файлы                     |
|-----|------------------------------------------------------------|---------------------------|
| 3.1 | MCP-сервер SSE transport                                    | `mcp_server/server.py`    |
| 3.2 | Все tools с project_id                                      | `mcp_server/tools.py`     |
| 3.3 | Тесты MCP tools                                             | `tests/`                  |

### Фаза 4 — Docker (Milestone 6)

| #   | Задача                                                     | Файлы                     |
|-----|------------------------------------------------------------|---------------------------|
| 4.1 | Dockerfile (multi-stage, slim)                              | `Dockerfile`              |
| 4.2 | docker-compose с port mapping                               | `docker-compose.yml`      |
| 4.3 | entrypoint.sh                                               | `scripts/`                |
| 4.4 | README                                                      | `README.md`               |

---

## 8. Docker и порты

### Принцип: внутренние порты — нестандартные, внешние — настраиваемые

Внутри контейнера используются порты **9877** (MCP) и **9878** (Web UI) — не конфликтуют с популярными сервисами. Маппинг на хост настраивается в docker-compose.

```yaml
# docker-compose.yml
version: "3.8"
services:
  ariadna:
    build: .
    ports:
      - "${MCP_HOST_PORT:-19877}:9877"    # MCP SSE
      - "${WEB_HOST_PORT:-19878}:9878"    # Web UI
    volumes:
      - ./data:/data                       # Всё хранилище
    environment:
      - DATA_DIR=/data
      - MCP_PORT=9877
      - WEB_PORT=9878
      - LOG_LEVEL=INFO
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9878/health"]
      interval: 30s
      timeout: 10s
```

```bash
# Использование
docker-compose up -d

# Web UI: http://localhost:19878
# MCP:    http://localhost:19877/sse

# Свои порты:
MCP_HOST_PORT=33000 WEB_HOST_PORT=33001 docker-compose up -d
```

**Подключение к Claude Code** (актуальный транспорт — streamable HTTP на
порту 19879; SSE на 19877 оставлен для старых клиентов):
```bash
claude mcp add --transport http --scope user ariadna http://localhost:19879/mcp
```

---

## 9. Конфигурация

| Переменная        | По умолчанию  | Описание                              |
|-------------------|---------------|---------------------------------------|
| `DATA_DIR`        | `/data`       | Корневая директория данных            |
| `MCP_PORT`        | `9877`        | Порт MCP-сервера внутри контейнера    |
| `WEB_PORT`        | `9878`        | Порт Web UI внутри контейнера         |
| `MCP_HOST_PORT`   | `19877`       | Порт MCP на хосте (docker-compose)    |
| `WEB_HOST_PORT`   | `19878`       | Порт Web UI на хосте (docker-compose) |
| `LOG_LEVEL`       | `INFO`        | Уровень логирования                   |

---

## 10. Ключевые технические решения

### 10.1 Парсинг отчёта

Конечный автомат, обрабатывающий строки по одной. Определяет тип строки по паттерну:
- `- Тип.Имя` → объект
- `Ключ: "Значение"` → свойство
- `Тип:` + следующая строка → тип реквизита
- `Состав:` + следующие строки → массив

Маппинг 30+ типов объектов (Справочники→Справочник, Документы→Документ, ...).

### 10.2 Multi-source индексация

При `reindex(project_id, source_id=None)`:
1. Очищаем всю базу проекта
2. Для каждого source: парсим отчёт → вставляем с source_id
3. Для каждого source: сканируем xml/ → парсим BSL → вставляем с source_id

При `reindex(project_id, source_id="ext_agro")`:
1. Удаляем только записи с source_id="ext_agro"
2. Парсим только этот source
3. FTS5 перестраивается автоматически через триггеры

### 10.3 Разрешение вызовов (Фаза 2)

При построении call tree модули из расширений «перекрывают» модули основной конфигурации:
1. Ищем процедуру в текущем модуле
2. Ищем в глобальных общих модулях (приоритет: extension → main)
3. Для `Модуль.Метод()` — ищем модуль по имени через все sources

### 10.4 FTS5 + LIKE fallback

FTS5 с дефолтным токенизатором плохо обрабатывает короткие кириллические запросы. Решение:
1. Пробуем FTS5 `MATCH`
2. Если 0 результатов — fallback на `LIKE '%query%'`
3. Оба варианта < 5мс на базах до 100К записей

---

## 11. Критерии успеха MVP

1. Через Web UI: создать проект, загрузить 2 отчёта (конфиг + расширение), проиндексировать — всё за < 1 минуту
2. Поиск по метаданным — ответ за < 50мс
3. Playground — все tools работают и показывают результаты
4. Несколько проектов одновременно — каждый со своим индексом
5. Docker: `docker-compose up` и Web UI на http://localhost:19878

---

## 12. Будущие улучшения

- Поддержка EDT-формата
- Парсинг запросов 1С (модель данных)
- Визуальный граф зависимостей (D3.js)
- Webhook на git push для переиндексации
- Поддержка OneScript (.os файлы)
- Экспорт метаданных в Markdown
- Интеграция с SonarQube BSL Plugin
