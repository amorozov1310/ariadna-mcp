"""
Tests for Этап 5 (вариант A, IMPROVEMENT_PLAN.md): auto-generating
report.txt from an XML dump via the norkins/metadata generator
(src/core/report_generator.py) when a source has no report.txt of its own.

tests/fixtures/xml_report_gen/ is a minimal real XML export (a trimmed
Configuration.xml + one small Catalog copied verbatim from the bshp
corpus, renamed to an ASCII filename) — enough for the generator to
discover one metadata object end to end.
"""

import io
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.project_manager import ProjectManager
from src.core.report_parser import parse_report
from src.core import report_generator

FIXTURE_XML = Path(__file__).parent / 'fixtures' / 'xml_report_gen'

requires_generator = pytest.mark.skipif(
    not report_generator.is_available(),
    reason="generate_config_report not installed (pip install '.[xml-report]')",
)


def _zip_fixture() -> io.BytesIO:
    """Zip tests/fixtures/xml_report_gen/ into an in-memory archive, as if
    it were uploaded through the Web UI's XML-zip upload."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in FIXTURE_XML.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(FIXTURE_XML))
    buf.seek(0)
    return buf


@requires_generator
def test_generate_report_produces_parseable_report():
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / 'report.txt'
        ok = report_generator.generate_report(FIXTURE_XML, dest)

        assert ok is True
        assert dest.exists()

        config, objects = parse_report(dest)
        assert config.name == 'TestReportGenConfig'
        assert len(objects) == 1
        assert objects[0].kind == 'Справочник'
        assert objects[0].full_name == 'Справочник.РолиКонтактныхЛиц'


def test_generate_report_returns_false_when_package_missing(monkeypatch):
    monkeypatch.setattr(report_generator, 'is_available', lambda: False)
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / 'report.txt'
        ok = report_generator.generate_report(FIXTURE_XML, dest)

        assert ok is False
        assert not dest.exists()


@requires_generator
def test_generate_report_missing_configuration_xml_returns_false():
    """A source without Configuration.xml at its root — no report should
    be produced, and indexing falls back to BSL-only (D7 fixtures under
    xml_ru/xml_en have no Configuration.xml, same shape)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        empty_xml = Path(tmpdir) / 'xml'
        empty_xml.mkdir()
        dest = Path(tmpdir) / 'out' / 'report.txt'
        ok = report_generator.generate_report(empty_xml, dest)

        assert ok is False
        assert not dest.exists()


@requires_generator
def test_project_manager_generates_report_from_xml_only():
    """Source added with only an XML-zip (no report.txt upload) still ends
    up with metadata after reindex — Этап 5 acceptance criterion."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(data_dir=tmpdir)
        try:
            pm.create_project('test', 'Test Project')
            source = pm.add_source(
                'test', 'main', 'Main', source_type='main',
                xml_archive=_zip_fixture(), xml_archive_filename='xml.zip',
            )
            assert source.report_path == '', "no report.txt uploaded yet"

            stats = pm.reindex('test')

            proj = pm.get_project('test')
            assert proj.status == 'ready'
            src = next(s for s in proj.sources if s.id == 'main')
            assert src.report_path == 'sources/main/report.txt'
            assert (Path(tmpdir) / 'projects' / 'test' / src.report_path).exists()
            assert stats.total_objects > 0

            results = pm.get_search('test').search_metadata('РолиКонтактныхЛиц')
            assert len(results) > 0
        finally:
            pm.close_all()
