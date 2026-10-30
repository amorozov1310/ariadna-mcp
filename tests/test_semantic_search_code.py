"""
semantic_search_code (docs/adr/0002-semantic-search-code.md): поиск процедур
по смыслу запроса. Проверяется и логика (tools.semantic_search_payload,
semantic_search.format_text), и ответ через настоящий MCP-клиент: текст и
structuredContent описывают один результат, структура проходит опубликованную
outputSchema, ошибки — isError.
"""

import asyncio
import io
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager
from src.mcp_server import semantic_search
from src.mcp_server.tools import execute_tool, semantic_search_payload

FIXTURES = Path(__file__).parent / 'fixtures'


def _zip(src: Path) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in src.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(src))
    buf.seek(0)
    return buf


@pytest.fixture
def pm():
    with tempfile.TemporaryDirectory() as tmpdir:
        manager = ProjectManager(tmpdir)
        manager.create_project('p1', 'p1')
        with open(FIXTURES / 'report_ru.txt', 'rb') as f:
            manager.add_source('p1', 'main', 'Main', report_file=f,
                               xml_archive=_zip(FIXTURES / 'xml_ru'), xml_archive_filename='x.zip')
        manager.add_source('p1', 'ext', 'Ext', source_type='extension',
                           xml_archive=_zip(FIXTURES / 'xml_ru_ext'), xml_archive_filename='x.zip')
        manager.reindex('p1')
        try:
            yield manager
        finally:
            manager.close_all()


def _search(pm, **kw):
    return semantic_search_payload(pm, {'project_id': 'p1', **kw})


def test_finds_procedure_by_description(pm):
    data = _search(pm, query='склад по умолчанию для новых документов')
    assert data['search_mode'] == 'lexical' and data['mode_reason']
    assert data['schema_version'] == '1' and data['project_id'] == 'p1'
    names = [(i['module_name'], i['procedure_name']) for i in data['items']]
    assert ('Справочник.Склады.МодульМенеджера', 'СкладПоУмолчанию') in names[:3]
    item = next(i for i in data['items'] if i['procedure_name'] == 'СкладПоУмолчанию')
    assert item['kind'] == 'function' and item['is_export'] is True
    assert item['relative_path'] == 'Catalogs/Склады/Ext/ManagerModule.bsl'
    assert item['start_line'] <= item['match_start_line'] <= item['match_end_line'] <= item['end_line']
    assert item['content_hash'].startswith('sha256:')
    assert item['evidence_kind'] == 'source_excerpt' and item['snippet']
    assert item['entity_id'].startswith('proc:')
    assert data['freshness']['index_state'] == 'ready'


def test_exact_name_is_marked_and_first(pm):
    data = _search(pm, query='ТипСкладаПоУмолчанию')
    assert data['items'][0]['procedure_name'] == 'ТипСкладаПоУмолчанию'
    assert data['items'][0]['match_methods'] == ['exact_name']


def test_entity_id_survives_reindex_and_differs_between_sources(pm):
    before = {(i['module_name'], i['procedure_name'], i['source_id']): i['entity_id']
              for i in _search(pm, query='ПриСозданииНаСервере', limit=50)['items']}
    pm.reindex('p1')
    after = {(i['module_name'], i['procedure_name'], i['source_id']): i['entity_id']
             for i in _search(pm, query='ПриСозданииНаСервере', limit=50)['items']}
    assert before == after
    base = [v for (m, p, s), v in after.items() if m == 'ОбщийМодуль.ФормыДокументовБазовый.Модуль']
    assert len(base) == len(set(base))


def test_pagination_and_limit_cap(pm):
    first = _search(pm, query='склад', limit=2)
    assert len(first['items']) == 2 and first['has_more'] and first['next_offset'] == 2
    second = _search(pm, query='склад', limit=2, offset=2)
    assert [i['rank'] for i in second['items']] == [3, 4]
    assert {i['entity_id'] for i in first['items']}.isdisjoint(i['entity_id'] for i in second['items'])
    text = semantic_search.format_text(first)
    assert '(use offset=2 for more)' in text

    capped = _search(pm, query='склад', limit=500)
    assert capped['limit'] == semantic_search.MAX_LIMIT
    assert any('урезан' in w for w in capped['warnings'])


def test_source_filter_and_unknown_source(pm):
    data = _search(pm, query='запись в журнал', source_id='ext')
    assert data['items'] and all(i['source_id'] == 'ext' for i in data['items'])
    assert _search(pm, query='склад', source_id='нет').startswith("Error: source 'нет' not found")


def test_missing_card_index_is_an_error_not_empty_result(pm):
    conn = pm.get_db('p1').conn
    conn.execute('DELETE FROM card_chunks')
    conn.commit()
    error = _search(pm, query='склад')
    assert isinstance(error, str) and error.startswith('Error') and 'reindex' in error


def test_unreadable_file_falls_back_to_card_header(pm):
    shutil.rmtree(pm.projects_dir / 'p1' / 'sources' / 'main' / 'xml' / 'CommonModules' / 'ЦеныСервер')
    data = _search(pm, query='РассчитатьЦену', limit=5)
    item = next(i for i in data['items'] if i['module_name'] == 'ОбщийМодуль.ЦеныСервер.Модуль')
    assert item['evidence_kind'] == 'card_header' and item['snippet'] == ''
    assert any('недоступна' in w for w in data['warnings'])


def test_empty_query_and_text_form(pm):
    assert _search(pm, query='   ') == 'Error: query is required'
    text = execute_tool(pm, 'semantic_search_code', {'project_id': 'p1', 'query': 'оформление заказа'})
    assert text.startswith('Режим: lexical')
    assert 'ОбщийМодуль.ЗаказыСервер.Модуль.ОформитьЗаказ' in text
    assert 'get_procedure_code' in text


def test_mcp_answer_text_and_structured_content(pm):
    pytest.importorskip('mcp.server.mcpserver')
    from mcp import Client
    from src.mcp_server.server import SemanticSearchResult, create_mcp_server

    mcp = create_mcp_server(pm)

    async def go():
        async with Client(mcp) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            ok = await client.call_tool('semantic_search_code', {'project_id': 'p1',
                                                                 'query': 'оформление заказа'})
            bad = await client.call_tool('semantic_search_code', {'project_id': 'нет', 'query': 'x'})
            return tools['semantic_search_code'], ok, bad

    tool, ok, bad = asyncio.run(go())
    assert tool.annotations.read_only_hint is True
    assert set(tool.output_schema['properties']) >= {'search_mode', 'items', 'freshness', 'warnings'}

    assert not ok.is_error
    data = SemanticSearchResult.model_validate(ok.structured_content)
    text = ok.content[0].text
    assert text == semantic_search.format_text(ok.structured_content)
    assert data.items[0].procedure_name == 'ОформитьЗаказ'
    for item in data.items:
        assert f'{item.module_name}.{item.procedure_name}' in text

    assert bad.is_error and "project 'нет' not found" in bad.content[0].text
