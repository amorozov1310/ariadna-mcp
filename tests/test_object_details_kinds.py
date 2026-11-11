"""get_object_details для подсистем и определяемых типов: состав подсистемы,
её дочерние подсистемы и тип определяемого типа с квалификаторами — раньше
карточка была только из имени и синонима."""

import io
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

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


# Выгрузка → (подсистема с составом и одной дочерней, объект её состава,
# число объектов состава, справочник).
CASES = {
    'xml_meta_ru': ('Подсистема.Продажи', '.РеализацияТоваров', 3, 'Справочник.Номенклатура'),
    'xml_meta_en': ('Подсистема.Operations', None, 3, 'Справочник.Products'),
}


@pytest.fixture(scope='module', params=list(CASES))
def pm(request):
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        pm.create_project('p1', 'p1')
        pm.add_source('p1', 'main', 'Main', xml_archive=_zip(FIXTURES / request.param),
                      xml_archive_filename='x.zip')
        pm.reindex('p1')
        pm.case = CASES[request.param]
        pm.fixture = request.param
        try:
            yield pm
        finally:
            pm.close_all()


def _details(pm, full_name, detail='full'):
    return execute_tool(pm, 'get_object_details',
                        {'project_id': 'p1', 'full_name': full_name, 'detail': detail})


def test_defined_type_shows_type_with_qualifiers(pm):
    if pm.fixture != 'xml_meta_ru':
        pytest.skip('определяемые типы — только в русской выгрузке фикстур')
    text = _details(pm, 'ОпределяемыйТип.Сумма')
    assert 'Тип: ' in text
    type_line = next(l for l in text.splitlines() if l.startswith('Тип: '))
    assert '15, 2' in type_line                    # Число(15, 2) / Number(15, 2)
    # Ссылочный определяемый тип — список типов.
    owner = _details(pm, 'ОпределяемыйТип.ВладелецФайлов', detail='brief')
    assert next(l for l in owner.splitlines() if l.startswith('Тип: ')).count('.') >= 1


def test_subsystem_shows_content_and_children(pm):
    subsystem, member, count, _ = pm.case
    text = _details(pm, subsystem)
    assert f'Состав ({count}):' in text
    if member:
        assert any(l.strip().endswith(member) for l in text.splitlines())
    assert 'Дочерние подсистемы (1):' in text
    child = next(l.strip() for l in text.splitlines() if l.strip().startswith(subsystem + '.'))
    child_name = child.split(' ')[0]
    # Дочерняя подсистема — своя карточка, вложенные в неё не показываются у родителя.
    assert 'Подсистема' in _details(pm, child_name).splitlines()[0]


def test_other_objects_unchanged(pm):
    text = _details(pm, pm.case[3], detail='brief')
    assert 'Object not found' not in text
    assert 'Состав' not in text and 'Дочерние подсистемы' not in text
    assert not any(l.startswith('Тип: ') for l in text.splitlines())
