"""Tests for report parser."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.report_parser import parse_report

FIXTURE = Path(__file__).parent / 'fixtures' / 'report_ru.txt'


def test_config_info():
    config, _ = parse_report(FIXTURE)
    assert config.name == 'ДемоКонфигурация'
    assert config.synonym == 'Демо-конфигурация'
    assert config.version == '1.2.3.4'
    assert config.compatibility_mode == 'Версия8_3_24'


def test_object_counts():
    _, objects = parse_report(FIXTURE)
    kinds = {}
    for o in objects:
        kinds[o.kind] = kinds.get(o.kind, 0) + 1
    assert kinds['Справочник'] == 8
    assert kinds['Документ'] == 5
    assert kinds['РегистрСведений'] == 3
    assert kinds['РегистрНакопления'] == 2
    assert kinds['ОбщийМодуль'] == 5
    assert kinds['Перечисление'] == 3
    assert kinds['Подсистема'] == 5
    assert len(objects) == 48


def test_attribute_types():
    _, objects = parse_report(FIXTURE)
    nom = next(o for o in objects if o.name == 'Номенклатура')
    vid = next(a for a in nom.attributes if a.name == 'ВидНоменклатуры')
    assert vid.type_desc == 'СправочникСсылка.ВидыНоменклатуры'
    assert vid.kind == 'Реквизит'
    # Имена команд и макетов не перезаписывают имя справочника.
    assert nom.full_name == 'Справочник.Номенклатура'


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
    e = next(o for o in objects if o.name == 'ТипыЦен')
    assert len(e.enum_values) == 2
    assert e.enum_values[0].name == 'Закупочная'


def test_forms():
    _, objects = parse_report(FIXTURE)
    v = next(o for o in objects if o.name == 'Номенклатура')
    assert len(v.forms) == 3
    assert {f.name for f in v.forms} == {'ФормаЭлемента', 'ФормаСписка', 'ФормаВыбора'}


def test_subsystems():
    _, objects = parse_report(FIXTURE)
    p = next(o for o in objects if o.kind == 'Подсистема' and o.name == 'Склад')
    assert 'Справочник.Номенклатура' in p.subsystem_info.content
    nested = next(o for o in objects if o.kind == 'Подсистема' and o.name == 'Отчеты')
    assert nested.full_name == 'Подсистема.Продажи.Подсистемы.Отчеты'
    assert nested.subsystem_info.content == ['Отчет.АнализПродаж']


def test_register_dimensions_resources():
    _, objects = parse_report(FIXTURE)
    r = next(o for o in objects if o.full_name == 'РегистрСведений.ЦеныНоменклатуры')
    dims = [a.name for a in r.attributes if a.kind == 'Измерение']
    res = [a.name for a in r.attributes if a.kind == 'Ресурс']
    assert dims == ['Номенклатура', 'ТипЦены']
    assert res == ['Цена']


def test_tabular_sections():
    _, objects = parse_report(FIXTURE)
    docs_with_ts = [o for o in objects if o.kind == 'Документ' and o.tabular_sections]
    assert len(docs_with_ts) > 0
    for ts_name, ts_attrs in docs_with_ts[0].tabular_sections.items():
        assert len(ts_attrs) > 0


def test_defined_types():
    _, objects = parse_report(FIXTURE)
    assert len([o for o in objects if o.kind == 'ОпределяемыйТип']) == 3
    owner = next(o for o in objects if o.full_name == 'ОпределяемыйТип.ВладелецФайлов')
    assert owner.type_desc == 'СправочникСсылка.Номенклатура, ДокументСсылка.ПоступлениеТоваров'


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f'  ✅ {name}')
            except Exception as e:
                print(f'  ❌ {name}: {e}')
