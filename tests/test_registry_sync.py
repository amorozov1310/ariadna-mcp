"""
Синхронизация реестра проектов между экземплярами ProjectManager.

В одном процессе жили три независимых ProjectManager — MCP SSE, MCP HTTP и
Web UI (до 0.3.0; src/main.py, src/web/app.py). Каждый каждый загружал projects.json
один раз и дальше отдавал кэш: MCP не видел проектов и источников,
добавленных через Web UI, а его записи затирали их устаревшей копией.
Теперь реестр перечитывается, если файл изменился (mtime_ns + размер).
"""

import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.core.project_manager import ProjectManager
from src.mcp_server.tools import execute_tool

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
def two_pms():
    """(pm_web, pm_mcp) на одном data_dir; у проекта p1 есть источник main."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm_web = ProjectManager(tmpdir)
        pm_mcp = ProjectManager(tmpdir)
        try:
            pm_web.create_project('p1', 'P1')
            with open(FIXTURES / 'report_ru.txt', 'rb') as f:
                pm_web.add_source('p1', 'main', 'Main', report_file=f,
                                  xml_archive=_zip(FIXTURES / 'xml_ru'),
                                  xml_archive_filename='x.zip')
            yield pm_web, pm_mcp
        finally:
            pm_web.close_all()
            pm_mcp.close_all()


def _disk(pm: ProjectManager) -> dict:
    return json.loads(pm.registry_path.read_text(encoding='utf-8'))['projects']


def test_mcp_sees_project_created_in_web_after_first_call():
    with tempfile.TemporaryDirectory() as tmpdir:
        pm_web = ProjectManager(tmpdir)
        pm_mcp = ProjectManager(tmpdir)
        try:
            assert pm_mcp.list_projects() == []     # кэш MCP загружен пустым
            pm_web.create_project('p2', 'P2')
            text = execute_tool(pm_mcp, 'list_projects', {})
            assert 'p2' in text, text
        finally:
            pm_web.close_all()
            pm_mcp.close_all()


def test_mcp_reindex_keeps_source_added_in_web(two_pms):
    pm_web, pm_mcp = two_pms
    pm_mcp.list_projects()                          # MCP закэшировал p1 только с main
    pm_web.add_source('p1', 'ext', 'Ext', source_type='extension',
                      xml_archive=_zip(FIXTURES / 'xml_ru_ext'), xml_archive_filename='x.zip')

    pm_mcp.reindex('p1')

    sources = {s['id'] for s in _disk(pm_mcp)['p1']['sources']}
    assert sources == {'main', 'ext'}, sources
    # И проиндексирован новый источник тоже — MCP увидел его до прогона.
    ext = next(s for s in _disk(pm_mcp)['p1']['sources'] if s['id'] == 'ext')
    assert ext['indexed_at']


def test_mcp_remove_source_keeps_project_created_in_web(two_pms):
    pm_web, pm_mcp = two_pms
    pm_web.add_source('p1', 'ext', 'Ext', source_type='extension',
                      xml_archive=_zip(FIXTURES / 'xml_ru_ext'), xml_archive_filename='x.zip')
    pm_mcp.list_projects()
    pm_web.create_project('p2', 'P2')

    text = execute_tool(pm_mcp, 'remove_source',
                        {'project_id': 'p1', 'source_id': 'ext', 'confirm': True})
    assert 'removed' in text, text

    disk = _disk(pm_mcp)
    assert 'p2' in disk, "проект из Web UI затёрт устаревшим реестром MCP"
    assert {s['id'] for s in disk['p1']['sources']} == {'main'}


def test_registry_reload_during_reindex_keeps_reindex_result(two_pms, monkeypatch):
    """Посреди прогона Web UI пишет реестр: создаёт проект и добавляет
    источник в тот же p1. reindex держит свой объект проекта — он должен
    записать итоговый status='ready' и не потерять ни чужой проект, ни
    источник, добавленный во время прогона."""
    pm_web, pm_mcp = two_pms
    from src.core.indexer import Indexer
    original = Indexer.resolve_calls

    def resolve_and_write_elsewhere(self):
        pm_web.create_project('p2', 'P2')
        pm_web.add_source('p1', 'ext', 'Ext', source_type='extension',
                          xml_archive=_zip(FIXTURES / 'xml_ru_ext'), xml_archive_filename='x.zip')
        pm_mcp.list_projects()                      # перечитывание реестра посреди прогона
        return original(self)

    monkeypatch.setattr(Indexer, 'resolve_calls', resolve_and_write_elsewhere)
    pm_mcp.reindex('p1')

    disk = _disk(pm_mcp)
    assert disk['p1']['status'] == 'ready', disk['p1']['status']
    assert disk['p1']['index_stats']['last_indexed']
    assert 'p2' in disk
    assert {s['id'] for s in disk['p1']['sources']} == {'main', 'ext'}
    assert pm_mcp.get_project('p1').status == 'ready'
    assert pm_web.get_project('p1').status == 'ready'


def test_own_write_does_not_force_reload(two_pms):
    """После собственной записи кэш остаётся актуальным — объекты не
    пересоздаются на каждом вызове."""
    pm_web, _ = two_pms
    before = pm_web.get_project('p1')
    pm_web.update_project('p1', name='Новое имя')
    assert pm_web.get_project('p1') is before
