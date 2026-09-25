"""Tests for report parser, indexer, search, and project manager."""

import os
import sys
import shutil
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.report_parser import parse_report, ReportParser
from src.core.db import Database
from src.core.indexer import Indexer
from src.core.search import SearchEngine, format_search_results, format_object_details
from src.core.project_manager import ProjectManager

FIXTURE_REPORT = Path(__file__).parent / 'fixtures' / 'report_ru.txt'
TEST_DATA_DIR = '/tmp/test_1c_mcp'


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up test data before each test."""
    if os.path.exists(TEST_DATA_DIR):
        shutil.rmtree(TEST_DATA_DIR)
    yield
    if os.path.exists(TEST_DATA_DIR):
        shutil.rmtree(TEST_DATA_DIR)


# ============================================
# REPORT PARSER
# ============================================

class TestReportParser:
    def test_parse_config_info(self):
        config, objects = parse_report(FIXTURE_REPORT)
        assert config.name == 'ПРО100АГРО'
        assert config.version == '1.1.0.5'

    def test_parse_object_count(self):
        config, objects = parse_report(FIXTURE_REPORT)
        assert len(objects) > 100
        kinds = set(o.kind for o in objects)
        assert 'Справочник' in kinds
        assert 'Документ' in kinds
        assert 'РегистрСведений' in kinds

    def test_parse_catalogs(self):
        _, objects = parse_report(FIXTURE_REPORT)
        catalogs = [o for o in objects if o.kind == 'Справочник']
        assert len(catalogs) == 42
        # Check specific catalog
        vred = next((o for o in catalogs if o.name == 'ВредоносныеОбъекты'), None)
        assert vred is not None
        assert vred.full_name == 'Справочник.ВредоносныеОбъекты'
        assert vred.synonym == 'Вредоносные объекты'
        assert len(vred.attributes) > 5

    def test_parse_attribute_types(self):
        _, objects = parse_report(FIXTURE_REPORT)
        vred = next(o for o in objects if o.name == 'ВредоносныеОбъекты')
        group_attr = next(a for a in vred.attributes if a.name == 'ГруппаВредоносногоОбъекта')
        assert group_attr.type_desc == 'СправочникСсылка.ГруппыВредоносныхОбъектов'
        assert group_attr.kind == 'Реквизит'

    def test_parse_documents(self):
        _, objects = parse_report(FIXTURE_REPORT)
        docs = [o for o in objects if o.kind == 'Документ']
        assert len(docs) == 12

    def test_parse_registers(self):
        _, objects = parse_report(FIXTURE_REPORT)
        info_regs = [o for o in objects if o.kind == 'РегистрСведений']
        accum_regs = [o for o in objects if o.kind == 'РегистрНакопления']
        assert len(info_regs) == 26
        assert len(accum_regs) == 4

    def test_parse_register_dimensions_and_resources(self):
        _, objects = parse_report(FIXTURE_REPORT)
        reg = next(o for o in objects if o.full_name == 'РегистрСведений.АгрохимическиеПоказателиПоля')
        dimensions = [a for a in reg.attributes if a.kind == 'Измерение']
        resources = [a for a in reg.attributes if a.kind == 'Ресурс']
        assert len(dimensions) > 0
        assert len(resources) > 0

    def test_parse_common_modules(self):
        _, objects = parse_report(FIXTURE_REPORT)
        modules = [o for o in objects if o.kind == 'ОбщийМодуль']
        assert len(modules) == 5
        server_mod = next(o for o in modules if o.name == 'ОбщиеНаСервере')
        assert server_mod.module_info is not None
        assert server_mod.module_info.is_server is True
        assert server_mod.module_info.server_call is True
        assert server_mod.module_info.is_privileged is True
        assert server_mod.module_info.is_client is False

    def test_parse_global_module(self):
        _, objects = parse_report(FIXTURE_REPORT)
        glob = next(o for o in objects if o.name == 'ОбщегоНазначения')
        assert glob.module_info.is_global is True
        assert glob.module_info.is_server is True
        assert glob.module_info.is_client is True

    def test_parse_enums(self):
        _, objects = parse_report(FIXTURE_REPORT)
        enums = [o for o in objects if o.kind == 'Перечисление']
        assert len(enums) == 10
        prices = next(o for o in enums if o.name == 'ВидыЦенСемян')
        assert len(prices.enum_values) == 2
        assert prices.enum_values[0].name == 'ПлановаяЦена'

    def test_parse_forms(self):
        _, objects = parse_report(FIXTURE_REPORT)
        vred = next(o for o in objects if o.name == 'ВредоносныеОбъекты')
        assert len(vred.forms) == 3
        form_names = {f.name for f in vred.forms}
        assert 'ФормаЭлемента' in form_names
        assert 'ФормаСписка' in form_names

    def test_parse_subsystems(self):
        _, objects = parse_report(FIXTURE_REPORT)
        subs = [o for o in objects if o.kind == 'Подсистема']
        assert len(subs) > 5
        polya = next(o for o in subs if o.name == 'Поля')
        assert polya.subsystem_info is not None
        assert len(polya.subsystem_info.content) > 0
        assert 'Справочник.Поля' in polya.subsystem_info.content

    def test_parse_tabular_sections(self):
        _, objects = parse_report(FIXTURE_REPORT)
        # Find a document with tabular sections
        docs_with_ts = [o for o in objects if o.kind == 'Документ' and o.tabular_sections]
        assert len(docs_with_ts) > 0
        doc = docs_with_ts[0]
        assert len(doc.tabular_sections) > 0
        # Check that TS has attributes
        for ts_name, ts_attrs in doc.tabular_sections.items():
            assert len(ts_attrs) > 0

    def test_parse_defined_types(self):
        _, objects = parse_report(FIXTURE_REPORT)
        dtypes = [o for o in objects if o.kind == 'ОпределяемыйТип']
        assert len(dtypes) == 9


# ============================================
# DATABASE
# ============================================

class TestDatabase:
    def test_create_and_schema(self):
        db = Database(f'{TEST_DATA_DIR}/test.db')
        db.connect()
        db.init_schema()
        # Check tables exist
        tables = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = {r['name'] for r in tables}
        assert 'metadata_objects' in table_names
        assert 'attributes' in table_names
        assert 'sources' in table_names
        db.close()

    def test_reset(self):
        db = Database(f'{TEST_DATA_DIR}/test.db')
        db.connect()
        db.init_schema()
        db.conn.execute("INSERT INTO sources (id, label) VALUES ('test', 'Test')")
        db.conn.commit()
        assert db.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        db.reset()
        assert db.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
        db.close()


# ============================================
# INDEXER
# ============================================

class TestIndexer:
    def test_index_report(self):
        db = Database(f'{TEST_DATA_DIR}/idx.db')
        db.connect()
        db.init_schema()
        indexer = Indexer(db)
        stats = indexer.index_report(str(FIXTURE_REPORT), source_id='main')
        assert stats['objects'] == 159
        assert stats['attributes'] > 400
        assert stats['forms'] > 100
        assert stats['modules'] == 5
        assert stats['config_name'] == 'ПРО100АГРО'
        db.close()

    def test_source_registered(self):
        db = Database(f'{TEST_DATA_DIR}/idx.db')
        db.connect()
        db.init_schema()
        indexer = Indexer(db)
        indexer.index_report(str(FIXTURE_REPORT), source_id='main', source_label='Main Config')
        row = db.conn.execute("SELECT * FROM sources WHERE id='main'").fetchone()
        assert row is not None
        assert row['label'] == 'Main Config'
        assert row['config_name'] == 'ПРО100АГРО'
        db.close()

    def test_fts_populated(self):
        db = Database(f'{TEST_DATA_DIR}/idx.db')
        db.connect()
        db.init_schema()
        Indexer(db).index_report(str(FIXTURE_REPORT), source_id='main')
        fts_count = db.conn.execute("SELECT COUNT(*) FROM metadata_fts").fetchone()[0]
        assert fts_count == 159
        db.close()


# ============================================
# SEARCH
# ============================================

class TestSearch:
    @pytest.fixture
    def engine(self):
        db = Database(f'{TEST_DATA_DIR}/search.db')
        db.connect()
        db.init_schema()
        Indexer(db).index_report(str(FIXTURE_REPORT), source_id='main')
        yield SearchEngine(db)
        db.close()

    def test_search_fts(self, engine):
        results = engine.search_metadata('Удобрен')
        assert len(results) > 5
        assert any('Удобрен' in r['full_name'] for r in results)

    def test_search_like_fallback(self, engine):
        """'Поле' should work via LIKE fallback (stemming to 'Пол')."""
        results = engine.search_metadata('Поле')
        assert len(results) > 0

    def test_search_by_kind(self, engine):
        results = engine.search_metadata('Культуры', kind='Справочник')
        assert len(results) >= 1
        assert all(r['kind'] == 'Справочник' for r in results)

    def test_search_attributes(self, engine):
        results = engine.search_attributes('Культура')
        assert len(results) > 0
        assert any('Культур' in r.get('type_desc', '') or 'Культур' in r.get('name', '')
                    for r in results)

    def test_get_object_details(self, engine):
        details = engine.get_object_details('Справочник.ВредоносныеОбъекты')
        assert details is not None
        assert details['object']['kind'] == 'Справочник'
        assert len(details['attributes']) > 5
        assert len(details['forms']) == 3

    def test_get_object_details_register(self, engine):
        details = engine.get_object_details('РегистрСведений.ЦеныСемян')
        assert details is not None
        attrs = details['attributes']
        dims = [a for a in attrs if a['kind'] == 'Измерение']
        resources = [a for a in attrs if a['kind'] == 'Ресурс']
        assert len(dims) > 0
        assert len(resources) > 0

    def test_get_object_details_common_module(self, engine):
        details = engine.get_object_details('ОбщийМодуль.ОбщиеНаСервере')
        assert details is not None
        assert details['module'] is not None
        assert details['module']['is_server'] == 1

    def test_find_references(self, engine):
        refs = engine.find_references('Культуры')
        assert len(refs) > 5
        assert any('СправочникСсылка.Культуры' in r['type_desc'] for r in refs)

    def test_list_objects(self, engine):
        all_objs = engine.list_objects(limit=200)
        assert len(all_objs) > 100
        cats = engine.list_objects(kind='Справочник')
        assert len(cats) == 42

    def test_format_search_results(self, engine):
        results = engine.search_metadata('Семена')
        text = format_search_results(results)
        assert 'Found' in text
        assert 'Справочник.Семена' in text

    def test_format_object_details(self, engine):
        details = engine.get_object_details('Справочник.ВредоносныеОбъекты')
        text = format_object_details(details)
        assert 'ВредоносныеОбъекты' in text
        assert 'Реквизиты' in text


# ============================================
# PROJECT MANAGER
# ============================================

class TestProjectManager:
    @pytest.fixture
    def pm(self):
        pm = ProjectManager(TEST_DATA_DIR)
        yield pm
        pm.close_all()

    def test_create_project(self, pm):
        proj = pm.create_project('test1', 'Test Project')
        assert proj.id == 'test1'
        assert proj.name == 'Test Project'
        assert proj.status == 'empty'

    def test_list_projects(self, pm):
        pm.create_project('p1', 'P1')
        pm.create_project('p2', 'P2')
        projects = pm.list_projects()
        assert len(projects) == 2

    def test_delete_project(self, pm):
        pm.create_project('to_delete', 'Delete Me')
        assert len(pm.list_projects()) == 1
        pm.delete_project('to_delete')
        assert len(pm.list_projects()) == 0

    def test_add_source_volume_mount(self, pm):
        pm.create_project('proj', 'Test')
        source_dir = Path(TEST_DATA_DIR) / 'projects' / 'proj' / 'sources' / 'main'
        source_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURE_REPORT, source_dir / 'report.txt')
        source = pm.add_source('proj', 'main', 'Main Config')
        assert source.report_path == 'sources/main/report.txt'

    def test_reindex(self, pm):
        pm.create_project('proj', 'Test')
        source_dir = Path(TEST_DATA_DIR) / 'projects' / 'proj' / 'sources' / 'main'
        source_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURE_REPORT, source_dir / 'report.txt')
        pm.add_source('proj', 'main', 'Main Config')
        stats = pm.reindex('proj')
        assert stats.total_objects == 159
        proj = pm.get_project('proj')
        assert proj.status == 'ready'

    def test_multi_source_reindex(self, pm):
        """Simulate base config + extension."""
        pm.create_project('multi', 'Multi-source')
        for sid in ['main', 'ext1']:
            source_dir = Path(TEST_DATA_DIR) / 'projects' / 'multi' / 'sources' / sid
            source_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(FIXTURE_REPORT, source_dir / 'report.txt')
            pm.add_source('multi', sid, f'Source {sid}',
                          source_type='main' if sid == 'main' else 'extension')
        stats = pm.reindex('multi')
        # Both sources indexed (same report, but treated as separate sources)
        assert stats.total_objects > 159  # duplicate objects with different source_id

    def test_search_after_reindex(self, pm):
        pm.create_project('search_proj', 'Search Test')
        source_dir = Path(TEST_DATA_DIR) / 'projects' / 'search_proj' / 'sources' / 'main'
        source_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURE_REPORT, source_dir / 'report.txt')
        pm.add_source('search_proj', 'main', 'Main')
        pm.reindex('search_proj')
        engine = pm.get_search('search_proj')
        results = engine.search_metadata('Семена')
        assert len(results) > 0

    def test_project_isolation(self, pm):
        """Two projects should have independent data."""
        for pid in ['a', 'b']:
            pm.create_project(pid, f'Project {pid}')
            source_dir = Path(TEST_DATA_DIR) / 'projects' / pid / 'sources' / 'main'
            source_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(FIXTURE_REPORT, source_dir / 'report.txt')
            pm.add_source(pid, 'main', 'Main')
            pm.reindex(pid)

        engine_a = pm.get_search('a')
        engine_b = pm.get_search('b')
        results_a = engine_a.search_metadata('Семена')
        results_b = engine_b.search_metadata('Семена')
        assert len(results_a) == len(results_b)  # Same data, independent DBs

    def test_duplicate_project_raises(self, pm):
        pm.create_project('dup', 'First')
        with pytest.raises(ValueError):
            pm.create_project('dup', 'Second')

    def test_invalid_project_id(self, pm):
        with pytest.raises(ValueError):
            pm.create_project('bad id!', 'Bad')

    def test_get_nonexistent_project(self, pm):
        with pytest.raises(KeyError):
            pm.get_project('nope')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
