"""Tests for report parser."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.report_parser import parse_report

FIXTURE = Path(__file__).parent / 'fixtures' / 'report_ru.txt'


def test_config_info():
    config, _ = parse_report(FIXTURE)
    assert config.name == 'ПРО100АГРО'
    assert config.version == '1.1.0.5'


def test_object_counts():
    _, objects = parse_report(FIXTURE)
    kinds = {}
    for o in objects:
        kinds[o.kind] = kinds.get(o.kind, 0) + 1
    assert kinds['Справочник'] == 42
    assert kinds['Документ'] == 12
    assert kinds['РегистрСведений'] == 26
    assert kinds['ОбщийМодуль'] == 5
    assert kinds['Перечисление'] == 10


def test_attribute_types():
    _, objects = parse_report(FIXTURE)
    vred = next(o for o in objects if o.name == 'ВредоносныеОбъекты')
    grp = next(a for a in vred.attributes if a.name == 'ГруппаВредоносногоОбъекта')
    assert grp.type_desc == 'СправочникСсылка.ГруппыВредоносныхОбъектов'
    assert grp.kind == 'Реквизит'


def test_common_modules():
    _, objects = parse_report(FIXTURE)
    srv = next(o for o in objects if o.name == 'ОбщиеНаСервере')
    assert srv.module_info.is_server is True
    assert srv.module_info.is_privileged is True
    assert srv.module_info.is_client is False
    glb = next(o for o in objects if o.name == 'ОбщегоНазначения')
    assert glb.module_info.is_global is True


def test_enums():
    _, objects = parse_report(FIXTURE)
    e = next(o for o in objects if o.name == 'ВидыЦенСемян')
    assert len(e.enum_values) == 2
    assert e.enum_values[0].name == 'ПлановаяЦена'


def test_forms():
    _, objects = parse_report(FIXTURE)
    v = next(o for o in objects if o.name == 'ВредоносныеОбъекты')
    assert len(v.forms) == 3
    assert {f.name for f in v.forms} == {'ФормаЭлемента', 'ФормаСписка', 'ФормаВыбора'}


def test_subsystems():
    _, objects = parse_report(FIXTURE)
    p = next(o for o in objects if o.kind == 'Подсистема' and o.name == 'Поля')
    assert 'Справочник.Поля' in p.subsystem_info.content


def test_register_dimensions_resources():
    _, objects = parse_report(FIXTURE)
    r = next(o for o in objects if o.full_name == 'РегистрСведений.АгрохимическиеПоказателиПоля')
    dims = [a for a in r.attributes if a.kind == 'Измерение']
    res = [a for a in r.attributes if a.kind == 'Ресурс']
    assert len(dims) > 0
    assert len(res) > 0


def test_tabular_sections():
    _, objects = parse_report(FIXTURE)
    docs_with_ts = [o for o in objects if o.kind == 'Документ' and o.tabular_sections]
    assert len(docs_with_ts) > 0
    for ts_name, ts_attrs in docs_with_ts[0].tabular_sections.items():
        assert len(ts_attrs) > 0


def test_defined_types():
    _, objects = parse_report(FIXTURE)
    assert len([o for o in objects if o.kind == 'ОпределяемыйТип']) == 9


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f'  ✅ {name}')
            except Exception as e:
                print(f'  ❌ {name}: {e}')
