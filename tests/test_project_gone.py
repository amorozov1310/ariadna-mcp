"""
Запрос к проекту, который удаляют в этот момент.

delete_project под нагрузкой MCP работает правильно: запросы, заставшие
удаление, получают ошибку. Но Database.read_conn бросал обычный
sqlite3.OperationalError «индекс удалён: <путь>»: server.py писал его как
неожиданный сбой (ERROR с трассировкой — 3 записи за 5 прогонов удаления),
а агент получал внутренний путь сервера. Теперь это IndexRemovedError
(а у get_db — ProjectNotFoundError): MCP отвечает «проект удалён во время
запроса» с isError, Web UI — 404, в логе одна строка INFO.
"""

import asyncio
import io
import logging
import sys
import tempfile
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.core.db import IndexRemovedError
from src.core.project_manager import ProjectManager, ProjectNotFoundError

FIXTURES = Path(__file__).parent / 'fixtures'


def _indexed(tmpdir: str) -> ProjectManager:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in (FIXTURES / 'xml_ru').rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(FIXTURES / 'xml_ru'))
    buf.seek(0)
    pm = ProjectManager(tmpdir)
    pm.create_project('p1', 'p1')
    with open(FIXTURES / 'report_ru.txt', 'rb') as f:
        pm.add_source('p1', 'main', 'Main', report_file=f,
                      xml_archive=buf, xml_archive_filename='x.zip')
    pm.reindex('p1')
    return pm


def _catch_deletion(monkeypatch, pm: ProjectManager, how: str):
    """Запрос «застаёт удаление»: Database проекта списан (retire), как
    после _close_project_db_everywhere, — или проекта уже нет в реестре."""
    if how == 'retired':
        db = pm.get_db('p1')
        db.retire()
        monkeypatch.setattr(pm, 'get_db', lambda project_id: db)
        return db.db_path

    def gone(project_id):
        raise ProjectNotFoundError(project_id)
    monkeypatch.setattr(pm, 'get_db', gone)
    return str(pm._index_path('p1'))


def _no_errors_logged(caplog):
    bad = [r for r in caplog.records if r.levelno >= logging.ERROR or r.exc_info]
    assert not bad, [(r.levelname, r.getMessage()) for r in bad]


def test_index_removed_error_is_operational_error_without_path():
    e = IndexRemovedError('/index/zz_check/index.db')
    assert isinstance(e, __import__('sqlite3').OperationalError)
    assert 'индекс удалён' in str(e) and '/index/' not in str(e)
    assert e.path == '/index/zz_check/index.db'


@pytest.mark.parametrize('how', ['retired', 'not_in_registry'])
@pytest.mark.parametrize('tool, args', [
    ('search_procedures', {'query': 'Рассчитать'}),
    ('get_index_status', {}),
])
def test_mcp_tool_reports_deleted_project_without_traceback(monkeypatch, caplog, how, tool, args):
    pytest.importorskip("mcp.server.mcpserver")
    from mcp import Client
    from src.mcp_server.server import create_mcp_server
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _indexed(tmpdir)
        try:
            index_path = _catch_deletion(monkeypatch, pm, how)
            mcp = create_mcp_server(pm)

            async def call():
                async with Client(mcp) as client:
                    return await client.call_tool(tool, {'project_id': 'p1', **args})

            with caplog.at_level(logging.DEBUG):
                result = asyncio.run(call())
            text = '\n'.join(c.text for c in result.content if getattr(c, 'text', None))
            assert result.is_error, text
            assert 'удалён' in text and 'list_projects' in text, text
            assert index_path not in text and 'index.db' not in text, text
            _no_errors_logged(caplog)
            assert any(r.levelno == logging.INFO and 'удалён' in r.getMessage()
                       for r in caplog.records)
        finally:
            pm.close_all()


@pytest.mark.parametrize('how', ['retired', 'not_in_registry'])
@pytest.mark.parametrize('url, json', [
    ('/api/projects/p1/status', True),
    ('/projects/p1', False),
    ('/projects/p1/search?q=Рассчитать', False),
    ('/projects/p1/playground', False),
])
def test_web_ui_answers_404_for_deleted_project(monkeypatch, caplog, how, url, json):
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _indexed(tmpdir)
        monkeypatch.setattr(app_module, '_pm', pm)
        try:
            index_path = _catch_deletion(monkeypatch, pm, how)
            headers = {'Host': '127.0.0.1:19878'}
            if json:
                headers['Accept'] = 'application/json'
            client = TestClient(app_module.app, headers=headers)
            with caplog.at_level(logging.DEBUG):
                r = client.get(url)
            assert r.status_code == 404, (url, r.status_code, r.text[:300])
            assert 'удалён' in r.text and index_path not in r.text
            _no_errors_logged(caplog)
        finally:
            pm.close_all()


def test_playground_tool_call_answers_404(monkeypatch, caplog):
    from fastapi.testclient import TestClient
    from src.web import app as app_module
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _indexed(tmpdir)
        monkeypatch.setattr(app_module, '_pm', pm)
        try:
            _catch_deletion(monkeypatch, pm, 'retired')
            client = TestClient(app_module.app, headers={'Host': '127.0.0.1:19878',
                                                         'Accept': 'application/json'})
            with caplog.at_level(logging.DEBUG):
                r = client.post('/api/projects/p1/playground',
                                json={'tool': 'search_procedures', 'params': {'query': 'Рассчитать'}})
            assert r.status_code == 404, r.text
            _no_errors_logged(caplog)
        finally:
            pm.close_all()
