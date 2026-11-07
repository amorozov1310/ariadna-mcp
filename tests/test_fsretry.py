"""
Повторы rename/replace/rmtree, пока файл ненадолго занят другим процессом.

На Windows под нагрузкой быстрые тесты падали в ~2 прогонах из 5:
add_source распаковывает выгрузку в staging и переименовывает его в xml, а
только что записанные файлы в этот момент проверял антивирус —
«[WinError 5] Отказано в доступе» на переименовании (так же падала запись
tokens.json). Здесь занятость воспроизводится детерминированно: первые
вызовы os.replace / shutil.rmtree бросают PermissionError.
"""

import io
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from src.core import fsretry
from src.core.archives import extract_archive

FIXTURES = Path(__file__).parent / 'fixtures'


def _zip() -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path in (FIXTURES / 'xml_ru').rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(FIXTURES / 'xml_ru'))
    buf.seek(0)
    return buf


def _busy(monkeypatch, module, name: str, times: int) -> list:
    """Первые times вызовов module.name — PermissionError, как при файле,
    открытом антивирусом."""
    real = getattr(module, name)
    calls = []

    def flaky(*a, **kw):
        calls.append(a)
        if len(calls) <= times:
            raise PermissionError(5, 'Отказано в доступе', str(a[0]))
        return real(*a, **kw)

    monkeypatch.setattr(module, name, flaky)
    monkeypatch.setattr(fsretry, 'RETRY_DELAY_SEC', 0)
    return calls


def test_extract_archive_survives_briefly_locked_rename(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / 'sources' / 'main' / 'xml'
        calls = _busy(monkeypatch, os, 'replace', 3)
        extract_archive(_zip(), 'x.zip', dest)
        assert len(calls) == 4
        assert (dest / 'Configuration.xml').is_file()
        assert [p.name for p in dest.parent.iterdir()] == ['xml']   # staging не остался


def test_extract_archive_survives_briefly_locked_old_dest(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        dest = Path(tmpdir) / 'xml'
        extract_archive(_zip(), 'x.zip', dest)
        (dest / 'old.txt').write_text('прежняя выгрузка', encoding='utf-8')
        calls = _busy(monkeypatch, shutil, 'rmtree', 2)
        extract_archive(_zip(), 'x.zip', dest)
        assert len(calls) >= 3
        assert not (dest / 'old.txt').exists()
        assert (dest / 'Configuration.xml').is_file()


def test_lock_that_does_not_go_away_still_raises(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        src, dst = Path(tmpdir) / 'a', Path(tmpdir) / 'b'
        src.write_text('x', encoding='utf-8')
        calls = _busy(monkeypatch, os, 'replace', 10 ** 6)
        with pytest.raises(PermissionError):
            fsretry.replace_with_retry(src, dst)
        assert len(calls) == fsretry.RETRY_ATTEMPTS
        assert src.exists() and not dst.exists()


def test_token_store_and_glossary_save_retry(monkeypatch):
    from src.auth import TokenStore
    from src.core.glossary import Glossary
    with tempfile.TemporaryDirectory() as tmpdir:
        calls = _busy(monkeypatch, os, 'replace', 2)
        TokenStore(Path(tmpdir)).create('dev', 'admin')
        assert len(calls) == 3
        assert (Path(tmpdir) / 'tokens.json').is_file()

        calls.clear()
        glossary = Glossary(Path(tmpdir) / 'glossary.json')
        glossary.save('Справочник.Товары', 'Справочник', ['номенклатура'])
        assert len(calls) == 3
        assert glossary.entries()


def test_remove_source_survives_briefly_locked_files(monkeypatch):
    from src.core.project_manager import ProjectManager
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        try:
            pm.create_project('p1', 'p1')
            pm.add_source('p1', 'main', 'Main', xml_archive=_zip(), xml_archive_filename='x.zip')
            calls = _busy(monkeypatch, shutil, 'rmtree', 2)
            pm.remove_source('p1', 'main')
            assert len(calls) == 3
            assert pm.get_project('p1').sources == []
            assert not (pm.projects_dir / 'p1' / 'sources' / 'main').exists()
        finally:
            pm.close_all()
