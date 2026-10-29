"""
Tests for report_parser with ScriptVariant = English configuration.

Covers the case where 1C configuration language is set to English and
the metadata report uses English keywords (Catalogs, Attributes, Forms, etc.)
instead of Russian (Справочники, Реквизиты, Формы, ...).
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.report_parser import parse_report

FIXTURE = Path(__file__).parent / 'fixtures' / 'report_en.txt'


def test_config_info():
    """Config name/version/synonym are read from English report."""
    config, _ = parse_report(FIXTURE)
    assert config.name == 'DemoApp', f"Expected 'DemoApp', got {config.name!r}"
    assert config.version == '2.0.1', f"Expected '2.0.1', got {config.version!r}"
    assert config.synonym == 'Demo application', f"Expected 'Demo application', got {config.synonym!r}"


def test_object_counts():
    """All major object types are indexed from English report."""
    _, objects = parse_report(FIXTURE)
    kinds = {}
    for o in objects:
        kinds[o.kind] = kinds.get(o.kind, 0) + 1

    assert kinds.get('Справочник', 0) == 12, f"Expected 12 Catalogs, got {kinds.get('Справочник', 0)}"
    assert kinds.get('ОбщийМодуль', 0) == 6, f"Expected 6 CommonModules, got {kinds.get('ОбщийМодуль', 0)}"
    assert kinds.get('Перечисление', 0) == 3, f"Expected 3 Enums, got {kinds.get('Перечисление', 0)}"
    assert kinds.get('РегистрСведений', 0) == 1, f"Expected 1 InformationRegister, got {kinds.get('РегистрСведений', 0)}"
    assert kinds.get('Подсистема', 0) == 4, f"Expected 4 Subsystems, got {kinds.get('Подсистема', 0)}"
    assert kinds.get('Роль', 0) == 3, f"Expected 3 Roles, got {kinds.get('Роль', 0)}"
    assert kinds.get('HTTPСервис', 0) == 1, f"Expected 1 HTTPService, got {kinds.get('HTTPСервис', 0)}"
    assert kinds.get('Задача', 0) == 1, f"Expected 1 Task, got {kinds.get('Задача', 0)}"
    assert len(objects) == 41, f"Expected 41 objects, got {len(objects)}"


def test_catalog_attributes():
    """Catalog attributes (Attributes) are correctly parsed."""
    _, objects = parse_report(FIXTURE)
    users = next((o for o in objects if o.kind == 'Справочник' and o.name == 'Users'), None)
    assert users is not None, "Catalog 'Users' not found"
    assert users.synonym == 'Пользователи', f"Expected synonym 'Пользователи', got {users.synonym!r}"
    assert len(users.attributes) >= 1, "Users should have attributes"

    attr_names = [a.name for a in users.attributes]
    assert 'Login' in attr_names, f"Attribute 'Login' not found, got {attr_names}"

    login = next(a for a in users.attributes if a.name == 'Login')
    assert 'String' in login.type_desc, f"Expected String type, got {login.type_desc!r}"


def test_catalog_forms():
    """Catalog forms (Forms) are correctly parsed."""
    _, objects = parse_report(FIXTURE)
    users = next((o for o in objects if o.kind == 'Справочник' and o.name == 'Users'), None)
    assert users is not None

    form_names = [f.name for f in users.forms]
    assert form_names == ['ItemForm', 'ListForm', 'ChoiceForm', 'ResetPassword'], \
        f"Unexpected form names {form_names}"


def test_common_module_flags():
    """CommonModule flags (Server, Global, Privileged, etc.) are parsed from English properties."""
    _, objects = parse_report(FIXTURE)
    mod = next((o for o in objects if o.kind == 'ОбщийМодуль' and o.name == 'IntegrationServer'), None)
    assert mod is not None, "CommonModule 'IntegrationServer' not found"
    assert mod.module_info is not None

    mi = mod.module_info
    assert mi.is_server is True, "Expected Server=True"
    assert mi.is_client is False, "Expected ClientManagedApplication=False"
    assert mi.is_global is False, "Expected Global=False"
    assert mi.is_privileged is False, "Expected Privileged=False"
    assert mi.server_call is False, "Expected ServerCall=False"
    assert mi.is_external is True, "Expected ExternalConnection=True"


def test_enum_values():
    """EnumValues are correctly parsed from English report."""
    _, objects = parse_report(FIXTURE)
    enum = next((o for o in objects if o.kind == 'Перечисление' and o.name == 'UserType'), None)
    assert enum is not None, "Enum 'UserType' not found"
    assert len(enum.enum_values) >= 1, "UserType should have enum values"

    ev_names = [ev.name for ev in enum.enum_values]
    assert 'Internal' in ev_names, f"EnumValue 'Internal' not found, got {ev_names}"


def test_information_register_dimensions_resources():
    """InformationRegister Dimensions and Resources are parsed correctly."""
    _, objects = parse_report(FIXTURE)
    reg = next((o for o in objects if o.kind == 'РегистрСведений' and o.name == 'JobStatus'), None)
    assert reg is not None, "InformationRegister 'JobStatus' not found"

    dims = [a for a in reg.attributes if a.kind == 'Измерение']
    ress = [a for a in reg.attributes if a.kind == 'Ресурс']

    assert len(dims) >= 1, f"Expected dimensions, got {dims}"
    assert len(ress) >= 1, f"Expected resources, got {ress}"

    dim_names = [d.name for d in dims]
    res_names = [r.name for r in ress]
    assert 'Job' in dim_names, f"Dimension 'Job' not found in {dim_names}"
    assert 'State' in res_names, f"Resource 'State' not found in {res_names}"


def test_tabular_sections():
    """TabularSections and their Attributes are parsed correctly."""
    _, objects = parse_report(FIXTURE)
    cat = next((o for o in objects if o.kind == 'Справочник' and o.name == 'EventKinds'), None)
    assert cat is not None, "Catalog 'EventKinds' not found"
    assert len(cat.tabular_sections) >= 1, "EventKinds should have tabular sections"

    ts_names = list(cat.tabular_sections.keys())
    assert 'Schedule' in ts_names, f"TabularSection 'Schedule' not found in {ts_names}"

    ts_attrs = cat.tabular_sections['Schedule']
    assert len(ts_attrs) >= 1, "Schedule should have attributes"
    attr_names = [a.name for a in ts_attrs]
    assert 'IntervalDays' in attr_names, f"Attribute 'IntervalDays' not found in {attr_names}"


def test_subsystem_content():
    """Subsystem Content: array is parsed correctly."""
    _, objects = parse_report(FIXTURE)
    sub = next((o for o in objects if o.kind == 'Подсистема' and o.name == 'Administration'), None)
    assert sub is not None, "Subsystem 'Administration' not found"
    assert sub.subsystem_info is not None
    assert len(sub.subsystem_info.content) >= 1, "Administration subsystem should have content"

    # Content items are qualified names like "Catalog.Users", "Role.FullAccess"
    assert sub.subsystem_info.content == [
        'Catalog.Users', 'Catalog.Departments', 'Role.FullAccess', 'DataProcessor.ImportUsers'], \
        f"Unexpected content {sub.subsystem_info.content}"


def test_nested_subsystems():
    """Nested subsystems (Subsystems.X.Subsystems.Y) are parsed as separate top-level objects."""
    _, objects = parse_report(FIXTURE)
    sub_names = {o.name for o in objects if o.kind == 'Подсистема'}
    # Operations.Jobs is a nested subsystem
    assert 'Jobs' in sub_names, f"Nested subsystem 'Jobs' not found in {sub_names}"
    assert 'Operations' in sub_names, f"Parent subsystem 'Operations' not found in {sub_names}"
    jobs = next(o for o in objects if o.kind == 'Подсистема' and o.name == 'Jobs')
    assert jobs.full_name == 'Подсистема.Operations.Subsystems.Jobs'


def test_http_services():
    """HTTPServices are indexed with correct kind."""
    _, objects = parse_report(FIXTURE)
    http_svcs = [o for o in objects if o.kind == 'HTTPСервис']
    assert len(http_svcs) >= 1, "Expected at least one HTTPService"

    api = next((o for o in http_svcs if o.name == 'Api'), None)
    assert api is not None, "HTTPService 'Api' not found"


def test_full_names_use_russian_singular():
    """full_name uses Russian singular kind prefix (e.g. Справочник.Users, not Catalogs.Users)."""
    _, objects = parse_report(FIXTURE)
    users = next((o for o in objects if o.name == 'Users' and o.kind == 'Справочник'), None)
    assert users is not None
    assert users.full_name == 'Справочник.Users', \
        f"Expected 'Справочник.Users', got {users.full_name!r}"

    # CommonModule full_name
    mod = next((o for o in objects if o.kind == 'ОбщийМодуль' and o.name == 'IntegrationServer'), None)
    assert mod is not None
    assert mod.full_name == 'ОбщийМодуль.IntegrationServer', \
        f"Expected 'ОбщийМодуль.IntegrationServer', got {mod.full_name!r}"


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    tests = [
        test_config_info,
        test_object_counts,
        test_catalog_attributes,
        test_catalog_forms,
        test_common_module_flags,
        test_enum_values,
        test_information_register_dimensions_resources,
        test_tabular_sections,
        test_subsystem_content,
        test_nested_subsystems,
        test_http_services,
        test_full_names_use_russian_singular,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f'  ✅ {t.__name__}')
            passed += 1
        except Exception as e:
            print(f'  ❌ {t.__name__}: {e}')
            failed += 1
    print(f'\n{passed} passed, {failed} failed')
