"""
Загруженные архивы XML-выгрузки (src/core/archives.py): опасные пути,
ссылки и «zip-бомбы» отклоняются до записи первого файла, а прежняя
выгрузка источника при отказе остаётся на месте.
"""

import io
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.archives import UnsafeArchiveError, extract_archive
from src.core.project_manager import ProjectManager


def _zip(entries: dict[str, bytes], symlinks: dict[str, str] | None = None) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o120777 << 16)   # S_IFLNK
            zf.writestr(info, target)
    buf.seek(0)
    return buf


def _tar(add) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tf:
        add(tf)
    buf.seek(0)
    return buf


def _file(tf: tarfile.TarFile, name: str, data: bytes = b'x'):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tf.addfile(info, io.BytesIO(data))


def _link(tf: tarfile.TarFile, name: str, target: str, kind=tarfile.SYMTYPE):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    tf.addfile(info)


@pytest.fixture
def root():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _leftovers(parent: Path) -> list[str]:
    return sorted(p.name for p in parent.iterdir() if p.name.startswith('.'))


def test_valid_zip_and_tar_are_extracted(root):
    extract_archive(_zip({'Configuration.xml': b'<a/>', 'Catalogs/X.xml': b'<b/>'}), 'x.zip', root / 'z')
    assert (root / 'z' / 'Catalogs' / 'X.xml').read_bytes() == b'<b/>'

    extract_archive(_tar(lambda tf: _file(tf, 'CommonModules/M/Ext/Module.bsl', b'code')),
                    'x.tar.gz', root / 't')
    assert (root / 't' / 'CommonModules' / 'M' / 'Ext' / 'Module.bsl').read_bytes() == b'code'
    assert _leftovers(root) == []


def test_unknown_extension_is_detected(root):
    extract_archive(_zip({'a.xml': b'1'}), 'dump.bin', root / 'z')
    extract_archive(_tar(lambda tf: _file(tf, 'a.xml')), 'dump.bin', root / 't')
    assert (root / 'z' / 'a.xml').exists() and (root / 't' / 'a.xml').exists()
    with pytest.raises(UnsafeArchiveError):
        extract_archive(io.BytesIO(b'not an archive at all'), 'dump.bin', root / 'g')
    assert not (root / 'g').exists() and _leftovers(root) == []


@pytest.mark.parametrize('name', ['../evil.xml', 'a/../../evil.xml', '/abs/evil.xml',
                                  'C:/evil.xml', '..\\evil.xml'])
def test_zip_path_outside_destination_is_rejected(root, name):
    with pytest.raises(UnsafeArchiveError, match='вне каталога'):
        extract_archive(_zip({'ok.xml': b'1', name: b'2'}), 'x.zip', root / 'xml')
    assert not (root / 'xml').exists()
    assert not (root / 'evil.xml').exists() and _leftovers(root) == []


def test_zip_symlink_is_rejected(root):
    with pytest.raises(UnsafeArchiveError, match='символическая ссылка'):
        extract_archive(_zip({'ok.xml': b'1'}, symlinks={'link': '/etc/passwd'}), 'x.zip', root / 'xml')
    assert not (root / 'xml').exists()


@pytest.mark.parametrize('add', [
    lambda tf: _file(tf, '../evil.xml'),
    lambda tf: _file(tf, '/abs/evil.xml'),
    lambda tf: _link(tf, 'link', '/etc/passwd'),
    lambda tf: _link(tf, 'hard', 'ok.xml', tarfile.LNKTYPE),
    lambda tf: _link(tf, 'dev', '', tarfile.CHRTYPE),
])
def test_tar_unsafe_members_are_rejected(root, add):
    def build(tf):
        _file(tf, 'ok.xml')
        add(tf)
    with pytest.raises(UnsafeArchiveError):
        extract_archive(_tar(build), 'x.tgz', root / 'xml')
    assert not (root / 'xml').exists()
    assert not (root / 'evil.xml').exists() and _leftovers(root) == []


def test_file_count_and_size_limits(root, monkeypatch):
    monkeypatch.setenv('ARIADNA_ARCHIVE_MAX_FILES', '2')
    with pytest.raises(UnsafeArchiveError, match='ARIADNA_ARCHIVE_MAX_FILES'):
        extract_archive(_zip({'a': b'1', 'b': b'2', 'c': b'3'}), 'x.zip', root / 'xml')
    monkeypatch.delenv('ARIADNA_ARCHIVE_MAX_FILES')

    monkeypatch.setenv('ARIADNA_ARCHIVE_MAX_BYTES', '1000')
    # Сжимается в сотни байт, распаковывается в 10 КБ.
    with pytest.raises(UnsafeArchiveError, match='ARIADNA_ARCHIVE_MAX_BYTES'):
        extract_archive(_zip({'bomb.xml': b'0' * 10_000}), 'x.zip', root / 'xml')
    with pytest.raises(UnsafeArchiveError, match='ARIADNA_ARCHIVE_MAX_BYTES'):
        extract_archive(_tar(lambda tf: _file(tf, 'bomb.xml', b'0' * 10_000)), 'x.tgz', root / 'xml')
    assert not (root / 'xml').exists()


def test_bad_limit_values_fall_back_to_defaults(root, monkeypatch):
    monkeypatch.setenv('ARIADNA_ARCHIVE_MAX_FILES', 'много')
    monkeypatch.setenv('ARIADNA_ARCHIVE_MAX_BYTES', '0')
    extract_archive(_zip({'a': b'1'}), 'x.zip', root / 'xml')
    assert (root / 'xml' / 'a').exists()


def test_rejected_update_keeps_previous_dump():
    """update_source раньше удалял xml/ до распаковки — отклонённый или битый
    архив оставлял источник без выгрузки."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        try:
            pm.create_project('p1', 'p1')
            pm.add_source('p1', 'main', 'Main', xml_archive=_zip({'old.xml': b'old'}),
                          xml_archive_filename='x.zip')
            xml = Path(tmpdir) / 'projects' / 'p1' / 'sources' / 'main' / 'xml'
            assert (xml / 'old.xml').exists()

            with pytest.raises(ValueError):   # Web UI отвечает на ValueError кодом 400
                pm.update_source('p1', 'main', xml_archive=_zip({'../evil.xml': b'x'}),
                                 xml_archive_filename='x.zip')
            assert (xml / 'old.xml').read_bytes() == b'old'

            pm.update_source('p1', 'main', xml_archive=_zip({'new.xml': b'new'}),
                             xml_archive_filename='x.zip')
            assert sorted(p.name for p in xml.iterdir()) == ['new.xml']   # без остатков старого
            assert _leftovers(xml.parent) == []
        finally:
            pm.close_all()
