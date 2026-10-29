"""
Tests for Ариадна — Phase 1.
Run: python -m pytest tests/ -v
"""

import os
import sys
import shutil
import tempfile
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.report_parser import ReportParser, parse_report
from src.core.db import Database
from src.core.indexer import Indexer
from src.core.search import SearchEngine, format_search_results, format_object_details
from src.core.project_manager import ProjectManager


FIXTURE_REPORT = Path(__file__).parent / 'fixtures' / 'report_ru.txt'


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d)


@pytest.fixture
def pm(tmp_dir):
    """ProjectManager with temp data dir."""
    manager = ProjectManager(tmp_dir)
    yield manager
    manager.close_all()


@pytest.fixture
def indexed_project(pm):
    """Project with the indexed synthetic demo configuration."""
    pm.create_project('test', 'Test Project')
    source_dir = Path(pm.projects_dir) / 'test' / 'sources' / 'main'
    source_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(str(FIXTURE_REPORT), str(source_dir / 'report.txt'))
    pm.add_source('test', 'main', 'Main config')
    pm.reindex('test')
    return pm


# ============================================
# REPORT PARSER
# ============================================

class TestReportParser:

    def test_parse_file(self):
        config, objects = parse_report(str(FIXTURE_REPORT))
        assert config.name == 'ДемоКонфигурация'
        assert config.version == '1.2.3.4'
        assert len(objects) == 48

    def test_object_kinds(self):
        _, objects = parse_report(str(FIXTURE_REPORT))
        kinds = {o.kind for o in objects}
        assert 'Справочник' in kinds
        assert 'Документ' in kinds
        assert 'РегистрСведений' in kinds
        assert 'Перечисление' in kinds
        assert 'ОбщийМодуль' in kinds
        assert 'Подсистема' in kinds

    def test_object_counts(self):
        _, objects = parse_report(str(FIXTURE_REPORT))
        from collections import Counter
        counts = Counter(o.kind for o in objects)
        assert counts['Справочник'] == 8
        assert counts['Документ'] == 5
        assert counts['РегистрСведений'] == 3
        assert counts['Перечисление'] == 3
        assert counts['ОбщийМодуль'] == 5

    def test_attributes_parsed(self):
        _, objects = parse_report(str(FIXTURE_REPORT))
        cat = next(o for o in objects if o.full_name == 'Справочник.Номенклатура')
        assert len(cat.attributes) == 7
        # Check a specific attribute
        art = next(a for a in cat.attributes if a.name == 'Артикул')
        assert art.type_desc == 'Строка(25, Переменная)'
        assert art.kind == 'Реквизит'
        assert art.is_indexed is True
        assert art.usage == 'ДляЭлемента'

    def test_type_references(self):
        _, objects = parse_report(str(FIXTURE_REPORT))
        cat = next(o for o in objects if o.full_name == 'Справочник.Номенклатура')
        grp = next(a for a in cat.attributes if a.name == 'ВидНоменклатуры')
        assert grp.type_desc == 'СправочникСсылка.ВидыНоменклатуры'

    def test_forms_parsed(self):
        _, objects = parse_report(str(FIXTURE_REPORT))
        cat = next(o for o in objects if o.full_name == 'Справочник.Номенклатура')
        form_names = [f.name for f in cat.forms]
        assert 'ФормаЭлемента' in form_names
        assert 'ФормаСписка' in form_names

    def test_enum_values(self):
        _, objects = parse_report(str(FIXTURE_REPORT))
        enum = next(o for o in objects if o.full_name == 'Перечисление.ТипыЦен')
        assert len(enum.enum_values) == 2
        names = [ev.name for ev in enum.enum_values]
        assert 'Продажная' in names

    def test_common_modules(self):
        _, objects = parse_report(str(FIXTURE_REPORT))
        modules = [o for o in objects if o.kind == 'ОбщийМодуль']
        assert len(modules) == 5

        srv = next(o for o in modules if o.name == 'ОбщиеНаСервере')
        assert srv.module_info is not None
        assert srv.module_info.is_server is True
        assert srv.module_info.server_call is True
        assert srv.module_info.is_privileged is True
        assert srv.module_info.is_client is False

        cli = next(o for o in modules if o.name == 'ОбщиеНаКлиенте')
        assert cli.module_info.is_client is True
        assert cli.module_info.is_server is False

        glob = next(o for o in modules if o.name == 'ОбщегоНазначения')
        assert glob.module_info.is_global is True

    def test_subsystem_content(self):
        _, objects = parse_report(str(FIXTURE_REPORT))
        sub = next(o for o in objects if o.full_name == 'Подсистема.Склад')
        assert sub.subsystem_info is not None
        assert len(sub.subsystem_info.content) == 5
        assert 'Справочник.Склады' in sub.subsystem_info.content

    def test_register_dimensions(self):
        _, objects = parse_report(str(FIXTURE_REPORT))
        reg = next(o for o in objects if o.full_name == 'РегистрСведений.ЦеныНоменклатуры')
        dims = [a for a in reg.attributes if a.kind == 'Измерение']
        ress = [a for a in reg.attributes if a.kind == 'Ресурс']
        assert len(dims) == 2
        assert len(ress) == 1

    def test_tabular_sections(self):
        _, objects = parse_report(str(FIXTURE_REPORT))
        docs = [o for o in objects if o.kind == 'Документ' and o.tabular_sections]
        assert len(docs) > 0
        doc = docs[0]
        assert len(doc.tabular_sections) > 0
        ts_name = list(doc.tabular_sections.keys())[0]
        ts_attrs = doc.tabular_sections[ts_name]
        assert len(ts_attrs) > 0


# ============================================
# PROJECT MANAGER
# ============================================

class TestProjectManager:

    def test_create_project(self, pm):
        proj = pm.create_project('test1', 'Test', 'desc')
        assert proj.id == 'test1'
        assert proj.name == 'Test'
        assert proj.status == 'empty'

    def test_list_projects(self, pm):
        pm.create_project('a', 'A')
        pm.create_project('b', 'B')
        projects = pm.list_projects()
        ids = [p.id for p in projects]
        assert 'a' in ids
        assert 'b' in ids

    def test_duplicate_project_raises(self, pm):
        pm.create_project('dup', 'Dup')
        with pytest.raises(ValueError):
            pm.create_project('dup', 'Dup2')

    def test_delete_project(self, pm):
        pm.create_project('del', 'Delete me')
        pm.delete_project('del')
        assert len(pm.list_projects()) == 0

    def test_add_source_volume_mount(self, pm):
        pm.create_project('p1', 'P1')
        source_dir = Path(pm.projects_dir) / 'p1' / 'sources' / 'main'
        source_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(str(FIXTURE_REPORT), str(source_dir / 'report.txt'))

        src = pm.add_source('p1', 'main', 'Config')
        assert src.report_path == 'sources/main/report.txt'

    def test_reindex(self, indexed_project):
        pm = indexed_project
        proj = pm.get_project('test')
        assert proj.status == 'ready'
        assert proj.index_stats.total_objects == 48

    def test_get_db(self, indexed_project):
        pm = indexed_project
        db = pm.get_db('test')
        assert db.conn is not None
        stats = db.get_stats()
        assert stats['metadata_objects'] == 48


# ============================================
# SEARCH ENGINE
# ============================================

class TestSearchEngine:

    def test_search_by_name(self, indexed_project):
        engine = indexed_project.get_search('test')
        results = engine.search_metadata('Склады')
        assert len(results) >= 1
        names = [r['full_name'] for r in results]
        assert 'Справочник.Склады' in names

    def test_search_cyrillic_fallback(self, indexed_project):
        """'Складе' should find 'Склады' via LIKE prefix shortening."""
        engine = indexed_project.get_search('test')
        results = engine.search_metadata('Складе')
        assert 'Справочник.Склады' in [r['full_name'] for r in results]

    def test_search_by_kind(self, indexed_project):
        engine = indexed_project.get_search('test')
        results = engine.search_metadata('Контрагент', kind='Справочник')
        assert len(results) >= 1
        assert all(r['kind'] == 'Справочник' for r in results)

    def test_object_details(self, indexed_project):
        engine = indexed_project.get_search('test')
        details = engine.get_object_details('Справочник.Номенклатура')
        assert details is not None
        # 7 реквизитов + 2 реквизита табличной части
        assert len(details['attributes']) == 9
        assert len(details['forms']) == 3

    def test_object_details_register(self, indexed_project):
        engine = indexed_project.get_search('test')
        details = engine.get_object_details('РегистрСведений.ЦеныНоменклатуры')
        assert details is not None
        kinds = {a['kind'] for a in details['attributes']}
        assert 'Измерение' in kinds
        assert 'Ресурс' in kinds

    def test_object_details_module(self, indexed_project):
        engine = indexed_project.get_search('test')
        details = engine.get_object_details('ОбщийМодуль.ОбщиеНаСервере')
        assert details is not None
        assert details['module'] is not None
        assert details['module']['is_server'] == 1

    def test_search_attributes(self, indexed_project):
        engine = indexed_project.get_search('test')
        results = engine.search_attributes('Номенклатура')
        assert len(results) >= 5

    def test_find_references(self, indexed_project):
        engine = indexed_project.get_search('test')
        refs = engine.find_references('Номенклатура')
        assert len(refs) >= 9
        types = [r['type_desc'] for r in refs]
        assert any('СправочникСсылка.Номенклатура' in t for t in types)

    def test_list_objects(self, indexed_project):
        engine = indexed_project.get_search('test')
        all_objs = engine.list_objects()
        assert len(all_objs) == 48
        assert len(engine.list_objects(limit=10)) == 10  # limited
        cats = engine.list_objects(kind='Справочник')
        assert len(cats) == 8
        assert all(r['kind'] == 'Справочник' for r in cats)

    def test_format_search_results(self, indexed_project):
        engine = indexed_project.get_search('test')
        results = engine.search_metadata('Товар')
        text = format_search_results(results)
        assert 'Found' in text
        assert 'Товар' in text

    def test_format_object_details(self, indexed_project):
        engine = indexed_project.get_search('test')
        details = engine.get_object_details('Справочник.Номенклатура')
        text = format_object_details(details)
        assert 'Номенклатура' in text
        assert 'Артикул' in text
        assert 'ФормаЭлемента' in text

    def test_search_speed(self, indexed_project):
        """Search must be under 10ms."""
        import time
        engine = indexed_project.get_search('test')
        t0 = time.time()
        for _ in range(10):
            engine.search_metadata('Товар')
        elapsed = (time.time() - t0) / 10 * 1000
        assert elapsed < 10, f"Search took {elapsed:.1f}ms, expected < 10ms"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
