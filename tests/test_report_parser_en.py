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
    assert config.name == 'idm2', f"Expected 'idm2', got {config.name!r}"
    assert config.version == '1.0.0', f"Expected '1.0.0', got {config.version!r}"
    assert config.synonym == '1IDM v.2', f"Expected '1IDM v.2', got {config.synonym!r}"


def test_object_counts():
    """All major object types are indexed from English report."""
    _, objects = parse_report(FIXTURE)
    kinds = {}
    for o in objects:
        kinds[o.kind] = kinds.get(o.kind, 0) + 1

    assert kinds.get('Справочник', 0) >= 10, f"Expected >=10 Catalogs, got {kinds.get('Справочник', 0)}"
    assert kinds.get('ОбщийМодуль', 0) >= 5, f"Expected >=5 CommonModules, got {kinds.get('ОбщийМодуль', 0)}"
    assert kinds.get('Перечисление', 0) >= 3, f"Expected >=3 Enums, got {kinds.get('Перечисление', 0)}"
    assert kinds.get('РегистрСведений', 0) >= 1, f"Expected >=1 InformationRegisters, got {kinds.get('РегистрСведений', 0)}"
    assert kinds.get('Подсистема', 0) >= 3, f"Expected >=3 Subsystems, got {kinds.get('Подсистема', 0)}"
    assert kinds.get('Роль', 0) >= 1, f"Expected >=1 Roles, got {kinds.get('Роль', 0)}"
    assert kinds.get('HTTPСервис', 0) >= 1, f"Expected >=1 HTTPServices, got {kinds.get('HTTPСервис', 0)}"


def test_catalog_attributes():
    """Catalog attributes (Attributes) are correctly parsed."""
    _, objects = parse_report(FIXTURE)
    account = next((o for o in objects if o.kind == 'Справочник' and o.name == 'Account'), None)
    assert account is not None, "Catalog 'Account' not found"
    assert account.synonym == 'Учетная запись', f"Expected synonym 'Учетная запись', got {account.synonym!r}"
    assert len(account.attributes) >= 1, "Account should have attributes"

    attr_names = [a.name for a in account.attributes]
    assert 'Username' in attr_names, f"Attribute 'Username' not found, got {attr_names}"

    username = next(a for a in account.attributes if a.name == 'Username')
    assert 'String' in username.type_desc, f"Expected String type, got {username.type_desc!r}"


def test_catalog_forms():
    """Catalog forms (Forms) are correctly parsed."""
    _, objects = parse_report(FIXTURE)
    account = next((o for o in objects if o.kind == 'Справочник' and o.name == 'Account'), None)
    assert account is not None

    form_names = [f.name for f in account.forms]
    assert len(form_names) >= 1, "Account should have forms"
    # Common form names in English config
    assert any(f in form_names for f in ('ItemForm', 'ListForm', 'ChoiceForm', 'ChangePassword')), \
        f"Expected form names not found in {form_names}"


def test_common_module_flags():
    """CommonModule flags (Server, Global, Privileged, etc.) are parsed from English properties."""
    _, objects = parse_report(FIXTURE)
    mod = next((o for o in objects if o.kind == 'ОбщийМодуль' and o.name == 'agent_impl_Self'), None)
    assert mod is not None, "CommonModule 'agent_impl_Self' not found"
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
    enum = next((o for o in objects if o.kind == 'Перечисление' and o.name == 'AccountType'), None)
    assert enum is not None, "Enum 'AccountType' not found"
    assert len(enum.enum_values) >= 1, "AccountType should have enum values"

    ev_names = [ev.name for ev in enum.enum_values]
    assert 'Personal' in ev_names, f"EnumValue 'Personal' not found, got {ev_names}"


def test_information_register_dimensions_resources():
    """InformationRegister Dimensions and Resources are parsed correctly."""
    _, objects = parse_report(FIXTURE)
    reg = next((o for o in objects if o.kind == 'РегистрСведений' and o.name == 'AgentTaskStatus'), None)
    assert reg is not None, "InformationRegister 'AgentTaskStatus' not found"

    dims = [a for a in reg.attributes if a.kind == 'Измерение']
    ress = [a for a in reg.attributes if a.kind == 'Ресурс']

    assert len(dims) >= 1, f"Expected dimensions, got {dims}"
    assert len(ress) >= 1, f"Expected resources, got {ress}"

    dim_names = [d.name for d in dims]
    res_names = [r.name for r in ress]
    assert 'AgentTask' in dim_names, f"Dimension 'AgentTask' not found in {dim_names}"
    assert 'Status' in res_names, f"Resource 'Status' not found in {res_names}"


def test_tabular_sections():
    """TabularSections and their Attributes are parsed correctly."""
    _, objects = parse_report(FIXTURE)
    cat = next((o for o in objects if o.kind == 'Справочник' and o.name == 'EventType'), None)
    assert cat is not None, "Catalog 'EventType' not found"
    assert len(cat.tabular_sections) >= 1, "EventType should have tabular sections"

    ts_names = list(cat.tabular_sections.keys())
    assert 'RegisterEvent' in ts_names, f"TabularSection 'RegisterEvent' not found in {ts_names}"

    ts_attrs = cat.tabular_sections['RegisterEvent']
    assert len(ts_attrs) >= 1, "RegisterEvent should have attributes"
    attr_names = [a.name for a in ts_attrs]
    assert 'NumberOfDays' in attr_names, f"Attribute 'NumberOfDays' not found in {attr_names}"


def test_subsystem_content():
    """Subsystem Content: array is parsed correctly."""
    _, objects = parse_report(FIXTURE)
    sub = next((o for o in objects if o.kind == 'Подсистема' and o.name == 'Organization'), None)
    assert sub is not None, "Subsystem 'Organization' not found"
    assert sub.subsystem_info is not None
    assert len(sub.subsystem_info.content) >= 1, "Organization subsystem should have content"

    # Content items are like "Report.PersonsNoAccess", "Enum.OrgUnitType"
    assert any('.' in item for item in sub.subsystem_info.content), \
        f"Content items should be qualified names, got {sub.subsystem_info.content[:3]}"


def test_nested_subsystems():
    """Nested subsystems (Subsystems.X.Subsystems.Y) are parsed as separate top-level objects."""
    _, objects = parse_report(FIXTURE)
    sub_names = {o.name for o in objects if o.kind == 'Подсистема'}
    # ProcessMgmt.Processes is a nested subsystem
    assert 'Processes' in sub_names, f"Nested subsystem 'Processes' not found in {sub_names}"
    assert 'ProcessMgmt' in sub_names, f"Parent subsystem 'ProcessMgmt' not found in {sub_names}"


def test_http_services():
    """HTTPServices are indexed with correct kind."""
    _, objects = parse_report(FIXTURE)
    http_svcs = [o for o in objects if o.kind == 'HTTPСервис']
    assert len(http_svcs) >= 1, "Expected at least one HTTPService"

    agent = next((o for o in http_svcs if o.name == 'Agent'), None)
    assert agent is not None, "HTTPService 'Agent' not found"


def test_full_names_use_russian_singular():
    """full_name uses Russian singular kind prefix (e.g. Справочник.Account, not Catalogs.Account)."""
    _, objects = parse_report(FIXTURE)
    account = next((o for o in objects if o.name == 'Account' and o.kind == 'Справочник'), None)
    assert account is not None
    assert account.full_name == 'Справочник.Account', \
        f"Expected 'Справочник.Account', got {account.full_name!r}"

    # CommonModule full_name
    mod = next((o for o in objects if o.kind == 'ОбщийМодуль' and o.name == 'agent_impl_Self'), None)
    assert mod is not None
    assert mod.full_name == 'ОбщийМодуль.agent_impl_Self', \
        f"Expected 'ОбщийМодуль.agent_impl_Self', got {mod.full_name!r}"


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
