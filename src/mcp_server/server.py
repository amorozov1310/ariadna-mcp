"""
MCP server (mcp SDK 2.x, Этап 6 IMPROVEMENT_PLAN.md): registers one typed
wrapper function per tool with MCPServer (formerly FastMCP in SDK 1.x —
the low-level Server's list_tools()/call_tool() decorators are gone in
2.x, see https://py.sdk.modelcontextprotocol.io/v2/migration/).

Each wrapper just assembles an args dict and delegates to
tools.execute_tool() — the actual dispatch/formatting logic, kept
transport-agnostic there so it's unit-testable without the SDK.
"""

import logging
from typing import Annotated

logger = logging.getLogger('ariadna')

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations
    from pydantic import Field
    HAS_MCP = True
except ImportError:
    HAS_MCP = False
    logger.warning("MCP SDK not installed. MCP server will not be available.")

from ..core.project_manager import ProjectManager
from .tools import execute_tool

_PID = "ID проекта (необязательно, если в реестре только один проект)"
_SID = "ID источника (пусто = все источники)"
_LIMIT = "Макс. результатов на страницу"
_OFFSET = "Пропустить первые N результатов (пагинация, см. offset= в подсказке 'use offset=N for more')"

_READ_ONLY = ToolAnnotations(readOnlyHint=True)
_MODULE = ("Имя модуля: полное (ОбщийМодуль.ОбщегоНазначения.Модуль, "
           "Документ.РеализацияТоваровУслуг.МодульОбъекта), короткое имя общего модуля "
           "(ОбщегоНазначения) или часть имени; регистр не важен. Точное совпадение "
           "выигрывает у частичного — полное имя берите из вывода search_procedures")

INSTRUCTIONS = """\
Ариадна — индекс конфигураций 1С:Предприятие (метаданные + код BSL) с графом вызовов.
Отвечает за миллисекунды и экономит контекст: не читайте файлы выгрузки напрямую, пока
вопрос решается инструментами.

Проект. Все инструменты, кроме list_projects, работают в рамках проекта. Если проектов
несколько, project_id обязателен — узнайте его через list_projects. Проект = основная
конфигурация + расширения (источники, list_sources); source_id сужает выдачу до одного.

Типовой порядок:
- объект конфигурации: search_metadata → get_object_details(full_name из выдачи);
- «где используется справочник/документ как тип реквизита»: find_references;
- процедура: search_procedures → get_procedure_code(module_path, procedure_name из выдачи);
  структура модуля без чтения кода — get_module_outline;
- «кто вызывает / что вызывает»: get_call_tree (direction=up/down); передавайте
  module_name, если имя процедуры встречается в нескольких модулях;
- текст внутри кода (запросы, присваивания, строки, комментарии): search_code.

Имена — как в конфигураторе, RU или EN (Справочник == Catalog), регистр не важен.
Выдача постраничная: строка «use offset=N for more» означает, что есть продолжение.
reindex не блокирует — прогресс в get_index_status. remove_source необратим и без
confirm=true только показывает, что будет удалено."""


def create_mcp_server(pm: ProjectManager) -> 'MCPServer | None':
    """Create and configure the MCP server with all tools."""
    if not HAS_MCP:
        return None

    try:
        from importlib.metadata import version as _pkg_version
        server_version = _pkg_version('ariadna')
    except Exception:
        server_version = '0.1.0'

    mcp = MCPServer("ariadna", title="Ариадна", instructions=INSTRUCTIONS,
                    version=server_version)

    def run(name: str, **kwargs) -> str:
        try:
            return execute_tool(pm, name, kwargs)
        except Exception as e:
            logger.error(f"Tool {name} failed: {e}")
            return f"Error: {e}"

    # ── Project / index ──

    @mcp.tool(
        description="Список всех проектов 1С с id, названием, статусом и статистикой. "
                     "Не требует project_id — вызывай первым, если id проекта неизвестен.",
        annotations=_READ_ONLY,
    )
    def list_projects() -> str:
        return run('list_projects')

    @mcp.tool(
        description="Список источников данных проекта (основная конфигурация + расширения) "
                     "с версией конфигурации и временем последней индексации.",
        annotations=_READ_ONLY,
    )
    def list_sources(
        project_id: Annotated[str | None, Field(description=_PID)] = None,
    ) -> str:
        return run('list_sources', project_id=project_id)

    @mcp.tool(
        description="Статистика индекса проекта: количество объектов, реквизитов, модулей, "
                     "процедур; статус (включая прогресс фоновой переиндексации).",
        annotations=_READ_ONLY,
    )
    def get_index_status(
        project_id: Annotated[str | None, Field(description=_PID)] = None,
    ) -> str:
        return run('get_index_status', project_id=project_id)

    @mcp.tool(
        description="Запустить переиндексацию проекта (всех источников или одного). "
                     "Возвращается немедленно — индексация идёт в фоне; прогресс и "
                     "завершение отслеживай через get_index_status.",
    )
    def reindex(
        project_id: Annotated[str | None, Field(description=_PID)] = None,
        source_id: Annotated[str | None, Field(description="ID источника (пусто = все)")] = None,
    ) -> str:
        return run('reindex', project_id=project_id, source_id=source_id)

    @mcp.tool(
        description="Удалить источник из проекта — файлы на диске И данные в индексе. "
                     "Деструктивно и необратимо: без confirm=true ничего не удаляет, а "
                     "возвращает сводку того, что было бы удалено (кол-во объектов, "
                     "процедур и т.д.) — повтори вызов с confirm=true, чтобы выполнить.",
        annotations=ToolAnnotations(destructiveHint=True),
    )
    def remove_source(
        source_id: Annotated[str, Field(description="ID источника для удаления")],
        project_id: Annotated[str | None, Field(description=_PID)] = None,
        confirm: Annotated[bool, Field(description="Подтверждение удаления — без true ничего не удаляется")] = False,
    ) -> str:
        return run('remove_source', project_id=project_id, source_id=source_id, confirm=confirm)

    # ── Metadata search ──

    @mcp.tool(
        description="Первый шаг при любом вопросе про объект конфигурации (справочник, "
                     "документ, регистр...). Полнотекстовый поиск с фильтром по типу "
                     "(kind принимает и RU, и EN название: Справочник == Catalog).",
        annotations=_READ_ONLY,
    )
    def search_metadata(
        query: Annotated[str, Field(description="Поисковый запрос")],
        project_id: Annotated[str | None, Field(description=_PID)] = None,
        kind: Annotated[str | None, Field(description="Фильтр: Справочник, Документ, РегистрСведений... (RU или EN)")] = None,
        source_id: Annotated[str | None, Field(description=_SID)] = None,
        limit: Annotated[int, Field(description=_LIMIT)] = 20,
        offset: Annotated[int, Field(description=_OFFSET)] = 0,
    ) -> str:
        return run('search_metadata', project_id=project_id, query=query, kind=kind,
                    source_id=source_id, limit=limit, offset=offset)

    @mcp.tool(
        description="Полная карточка объекта: реквизиты с типами, табличные части, формы, "
                     "флаги общего модуля. detail=\"brief\" (по умолчанию) — счётчики + первые "
                     "30 реквизитов + список табличных частей без состава (экономит токены); "
                     "detail=\"full\" — всё целиком. При наличии в нескольких источниках "
                     "возвращает первое совпадение и список остальных.",
        annotations=_READ_ONLY,
    )
    def get_object_details(
        full_name: Annotated[str, Field(description="Полное имя: Справочник.Номенклатура")],
        project_id: Annotated[str | None, Field(description=_PID)] = None,
        source_id: Annotated[str | None, Field(description="ID источника (опционально — если объект есть в нескольких источниках)")] = None,
        detail: Annotated[str, Field(description='"brief" (по умолчанию) или "full"')] = 'brief',
    ) -> str:
        return run('get_object_details', project_id=project_id, full_name=full_name,
                    source_id=source_id, detail=detail)

    @mcp.tool(
        description="Поиск реквизитов по имени или типу. Находит, где используется "
                     "определённый тип данных (например, все реквизиты типа СправочникСсылка.Контрагенты).",
        annotations=_READ_ONLY,
    )
    def search_attributes(
        query: Annotated[str, Field(description="Имя реквизита или часть типа")],
        project_id: Annotated[str | None, Field(description=_PID)] = None,
        type_filter: Annotated[str | None, Field(description="Фильтр по типу: СправочникСсылка.Контрагенты")] = None,
        source_id: Annotated[str | None, Field(description=_SID)] = None,
        limit: Annotated[int, Field(description=_LIMIT)] = 30,
        offset: Annotated[int, Field(description=_OFFSET)] = 0,
    ) -> str:
        return run('search_attributes', project_id=project_id, query=query,
                    type_filter=type_filter, source_id=source_id, limit=limit, offset=offset)

    @mcp.tool(
        description="Где объект используется в типах реквизитов ДРУГИХ объектов (обратные "
                     "ссылки на справочник/документ). Для «кто вызывает эту процедуру в коде» "
                     "используй get_call_tree с direction=up.",
        annotations=_READ_ONLY,
    )
    def find_references(
        object_name: Annotated[str, Field(description="Имя объекта без вида: Номенклатура, Контрагенты. Совпадение по подстроке типа — реквизиты ровно этого типа идут первыми")],
        project_id: Annotated[str | None, Field(description=_PID)] = None,
        source_id: Annotated[str | None, Field(description=_SID)] = None,
        limit: Annotated[int, Field(description=_LIMIT)] = 50,
        offset: Annotated[int, Field(description=_OFFSET)] = 0,
    ) -> str:
        return run('find_references', project_id=project_id, object_name=object_name,
                    source_id=source_id, limit=limit, offset=offset)

    @mcp.tool(
        description="Список всех объектов конфигурации, с опциональным фильтром по типу и "
                     "источнику. Для поиска по имени/тексту используй search_metadata — оно точнее.",
        annotations=_READ_ONLY,
    )
    def list_objects(
        project_id: Annotated[str | None, Field(description=_PID)] = None,
        kind: Annotated[str | None, Field(description="Тип: Справочник, Документ, РегистрСведений... (RU или EN)")] = None,
        source_id: Annotated[str | None, Field(description=_SID)] = None,
        limit: Annotated[int, Field(description=_LIMIT)] = 100,
        offset: Annotated[int, Field(description=_OFFSET)] = 0,
    ) -> str:
        return run('list_objects', project_id=project_id, kind=kind,
                    source_id=source_id, limit=limit, offset=offset)

    # ── BSL code analysis ──

    @mcp.tool(
        description="Поиск процедур и функций BSL по части имени (регистр не важен), с фильтром "
                     "по модулю и экспортности. Точные совпадения имени идут первыми. Каждая строка "
                     "выдачи — полное имя модуля и процедуры: передавайте их в get_procedure_code / "
                     "get_call_tree. Для текста ВНУТРИ кода (присваивания, запросы) — search_code.",
        annotations=_READ_ONLY,
    )
    def search_procedures(
        query: Annotated[str, Field(description="Имя или часть имени процедуры")],
        project_id: Annotated[str | None, Field(description=_PID)] = None,
        module_filter: Annotated[str | None, Field(description="Часть имени модуля: ОбщегоНазначения, РеализацияТоваровУслуг")] = None,
        export_only: Annotated[bool, Field(description="Только экспортные")] = False,
        source_id: Annotated[str | None, Field(description=_SID)] = None,
        limit: Annotated[int, Field(description=_LIMIT)] = 30,
        offset: Annotated[int, Field(description=_OFFSET)] = 0,
    ) -> str:
        return run('search_procedures', project_id=project_id, query=query,
                    module_filter=module_filter, export_only=export_only,
                    source_id=source_id, limit=limit, offset=offset)

    @mcp.tool(
        description="Исходный код одной процедуры/функции с номерами строк. Модуль и имя "
                     "процедуры удобнее всего взять из вывода search_procedures.",
        annotations=_READ_ONLY,
    )
    def get_procedure_code(
        module_path: Annotated[str, Field(description=_MODULE)],
        procedure_name: Annotated[str, Field(description="Имя процедуры или функции (регистр не важен)")],
        project_id: Annotated[str | None, Field(description=_PID)] = None,
        source_id: Annotated[str | None, Field(description="ID источника — если модуль с таким именем есть и в конфигурации, и в расширении")] = None,
    ) -> str:
        return run('get_procedure_code', project_id=project_id,
                    module_path=module_path, procedure_name=procedure_name, source_id=source_id)

    @mcp.tool(
        description="Кто вызывает / что вызывает процедура — дерево вверх (direction=up) или "
                     "вниз (direction=down) по цепочке. Для «где используется реквизит» "
                     "(а не вызов процедуры) используй find_references.",
        annotations=_READ_ONLY,
    )
    def get_call_tree(
        procedure_name: Annotated[str, Field(description="Имя процедуры (регистр не важен)")],
        project_id: Annotated[str | None, Field(description=_PID)] = None,
        module_name: Annotated[str | None, Field(description=_MODULE + ". Без него строятся деревья для всех одноимённых процедур")] = None,
        direction: Annotated[str, Field(description="down (кого вызывает) | up (кто вызывает)")] = 'down',
        depth: Annotated[int, Field(description="Глубина дерева")] = 3,
        source_id: Annotated[str | None, Field(description="ID источника (для фильтрации корня дерева)")] = None,
        max_nodes: Annotated[int, Field(description="Жёсткий лимит узлов на всё дерево — при превышении вывод помечается как усечённый")] = 200,
    ) -> str:
        return run('get_call_tree', project_id=project_id, procedure_name=procedure_name,
                    module_name=module_name, direction=direction, depth=depth,
                    source_id=source_id, max_nodes=max_nodes)

    @mcp.tool(
        description="Поиск строки в исходном тексте BSL-модулей — присваивания, тексты запросов "
                     "(ВЫБРАТЬ...), строковые литералы, комментарии. Ищется подстрока без учёта регистра "
                     "(не регулярное выражение); каждое вхождение — модуль:строка и ±1 строка контекста. "
                     "Для поиска процедуры по имени используй search_procedures — точнее и быстрее.",
        annotations=_READ_ONLY,
    )
    def search_code(
        query: Annotated[str, Field(description="Текст для поиска")],
        project_id: Annotated[str | None, Field(description=_PID)] = None,
        file_pattern: Annotated[str | None, Field(description="Часть пути к файлу в выгрузке: CommonModules/, Documents/РеализацияТоваровУслуг")] = None,
        source_id: Annotated[str | None, Field(description=_SID)] = None,
        limit: Annotated[int, Field(description=_LIMIT)] = 30,
        offset: Annotated[int, Field(description=_OFFSET)] = 0,
    ) -> str:
        return run('search_code', project_id=project_id, query=query,
                    file_pattern=file_pattern, source_id=source_id, limit=limit, offset=offset)

    @mcp.tool(
        description="Структура модуля: список процедур/функций с директивами, экспортностью, "
                     "номерами строк — обзор модуля без чтения всего файла.",
        annotations=_READ_ONLY,
    )
    def get_module_outline(
        module_path: Annotated[str, Field(description=_MODULE)],
        project_id: Annotated[str | None, Field(description=_PID)] = None,
        source_id: Annotated[str | None, Field(description="ID источника (если одинаковое имя модуля в разных источниках)")] = None,
    ) -> str:
        return run('get_module_outline', project_id=project_id,
                    module_path=module_path, source_id=source_id)

    @mcp.tool(
        description="Диагностика индекса: процедуры в файлах против проиндексированных (пропущенные "
                     "и лишние) и качество графа вызовов — доля разрешённых вызовов по видам, худшие "
                     "модули, частые нерезолвленные имена. Читает все файлы выгрузки — на крупной "
                     "конфигурации занимает время. Не для обычного поиска.",
        annotations=_READ_ONLY,
    )
    def diagnose_index(
        project_id: Annotated[str | None, Field(description=_PID)] = None,
    ) -> str:
        return run('diagnose_index', project_id=project_id)

    return mcp
