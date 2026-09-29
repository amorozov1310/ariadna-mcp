"""
Пределы выдачи MCP-инструментов.

- limit у постраничных инструментов ничем не ограничивался сверху:
  limit=100000 раздувал ответ и контекст модели. Теперь потолок MAX_LIMIT,
  и об урезании говорит трейлер.
- diagnose_index выводил список «Missing from index» целиком — на большой
  конфигурации тысячи строк. Теперь первые DIAGNOSE_MISSING_SHOWN и
  «показано N из M».
"""

import io
import sys
import tempfile
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core import search as search_module
from src.core.project_manager import ProjectManager
from src.core.search import SearchEngine
from src.mcp_server import tools
from src.mcp_server.tools import execute_tool

FIXTURES = Path(__file__).parent / 'fixtures'


def _pm(tmpdir: str) -> ProjectManager:
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


def test_huge_limit_is_capped_and_reported(monkeypatch):
    seen = []
    real = SearchEngine.search_procedures

    def spy(self, *a, limit=30, **kw):
        seen.append(limit)
        return real(self, *a, limit=limit, **kw)

    monkeypatch.setattr(SearchEngine, 'search_procedures', spy)
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm(tmpdir)
        try:
            text = execute_tool(pm, 'search_procedures', {'project_id': 'p1', 'query': 'Рассчитать',
                                                          'limit': 100000})
            assert seen == [tools.MAX_LIMIT + 1]          # +1 — подсмотреть «есть ли ещё»
            assert 'РассчитатьЦену' in text
            assert text.splitlines()[-1] == (
                f"(limit=100000 урезан до {tools.MAX_LIMIT} — больше за один вызов не "
                f"выдаётся; остальное — через offset)"), text

            # В пределах потолка — как раньше, без оговорки.
            seen.clear()
            text = execute_tool(pm, 'search_procedures', {'project_id': 'p1', 'query': 'Рассчитать',
                                                          'limit': tools.MAX_LIMIT})
            assert seen == [tools.MAX_LIMIT + 1] and 'урезан' not in text

            # Все постраничные инструменты под тем же потолком.
            for tool, args in (('search_metadata', {'query': 'Номенклатура'}),
                               ('search_attributes', {'query': 'Наименование'}),
                               ('find_references', {'object_name': 'Номенклатура'}),
                               ('list_objects', {}),
                               ('search_code', {'query': 'Возврат'})):
                text = execute_tool(pm, tool, {'project_id': 'p1', 'limit': 10**6, **args})
                assert f'урезан до {tools.MAX_LIMIT}' in text.splitlines()[-1], (tool, text)
        finally:
            pm.close_all()


def test_diagnose_index_shows_first_missing_and_total(monkeypatch):
    real = search_module.diagnose_index

    def with_many_missing(pm, project_id):
        result = real(pm, project_id)
        result['missing'] = [{'module': f'ОбщийМодуль.М{i}', 'line': i, 'name': f'П{i}',
                              'signature': f'Процедура П{i}()'} for i in range(250)]
        result['missing_count'] = 250
        return result

    monkeypatch.setattr(search_module, 'diagnose_index', with_many_missing)
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = _pm(tmpdir)
        try:
            text = execute_tool(pm, 'diagnose_index', {'project_id': 'p1'})
            assert text.count('❌') == tools.DIAGNOSE_MISSING_SHOWN, text
            assert f'показано {tools.DIAGNOSE_MISSING_SHOWN} из 250' in text
            assert 'ОбщийМодуль.М99:' in text and 'ОбщийМодуль.М100:' not in text

            # Короткий список — целиком и без оговорки.
            monkeypatch.setattr(search_module, 'diagnose_index', real)
            text = execute_tool(pm, 'diagnose_index', {'project_id': 'p1'})
            assert 'показано' not in text
        finally:
            pm.close_all()
