"""
Контракт MCP 1.x: снимок схемы инструментов (tests/contract_1x.json) и
проверка совместимости с ним.

Что гарантирует 1.x (CHANGELOG 1.0.0, docs/adr/0001-standalone-1x.md):
имена инструментов, имена/типы/обязательность/умолчания параметров,
аннотации и вид ответа не меняются; добавлять можно новые инструменты и
необязательные параметры, расширять список допустимых значений.

Два теста:
  - test_no_breaking_changes — падает только на несовместимом изменении;
  - test_snapshot_is_current — падает на любом расхождении: совместимое
    дополнение нужно осознанно занести в снимок командой
        python tests/test_tool_contract.py --update
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

pytest.importorskip('mcp.server.mcpserver')

from src.core.project_manager import ProjectManager
from src.mcp_server.server import create_mcp_server

SNAPSHOT = Path(__file__).parent / 'contract_1x.json'
_ABSENT = '<нет умолчания>'


def _type(prop: dict) -> str:
    """«string», «integer», «string|null» — без заголовков и описаний."""
    variants = prop.get('anyOf') or [prop]
    names = []
    for v in variants:
        t = v.get('type', 'any')
        names.append('|'.join(t) if isinstance(t, list) else t)
    return '|'.join(sorted(names))


def _enum(prop: dict) -> list | None:
    for v in prop.get('anyOf') or [prop]:
        if 'enum' in v:
            return sorted(v['enum'])
    return None


def current_contract() -> dict:
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        try:
            tools = asyncio.run(create_mcp_server(pm).list_tools())
        finally:
            pm.close_all()
    contract = {}
    for tool in tools:
        schema = tool.input_schema
        required = set(schema.get('required', []))
        params = {}
        for name, prop in schema.get('properties', {}).items():
            param = {'type': _type(prop), 'required': name in required,
                     'default': prop.get('default', _ABSENT)}
            enum = _enum(prop)
            if enum is not None:
                param['enum'] = enum
            params[name] = param
        ann = tool.annotations
        output = (tool.output_schema or {}).get('properties', {})
        contract[tool.name] = {
            'params': params,
            'read_only': bool(ann and ann.read_only_hint),
            'destructive': bool(ann and ann.destructive_hint),
            'output': {k: _type(v) for k, v in output.items()},
        }
    return dict(sorted(contract.items()))


def breaking_changes(snapshot: dict, current: dict) -> list[str]:
    problems = []
    for tool, old in snapshot.items():
        new = current.get(tool)
        if new is None:
            problems.append(f'{tool}: инструмент удалён или переименован')
            continue
        for key in ('read_only', 'destructive'):
            if old[key] != new[key]:
                problems.append(f'{tool}: изменилась аннотация {key}: {old[key]} → {new[key]}')
        for field, ftype in old['output'].items():
            if new['output'].get(field) != ftype:
                problems.append(f'{tool}: изменился ответ ({field}: {ftype} → {new["output"].get(field)})')
        for name, p in old['params'].items():
            q = new['params'].get(name)
            if q is None:
                problems.append(f'{tool}({name}): параметр удалён или переименован')
                continue
            for key in ('type', 'required', 'default'):
                if p[key] != q[key]:
                    problems.append(f'{tool}({name}): изменилось {key}: {p[key]!r} → {q[key]!r}')
            if 'enum' in p and not set(p['enum']) <= set(q.get('enum', p['enum'])):
                problems.append(f'{tool}({name}): из допустимых значений убраны '
                                f'{sorted(set(p["enum"]) - set(q.get("enum", [])))}')
        for name, q in new['params'].items():
            if name not in old['params'] and q['required']:
                problems.append(f'{tool}({name}): новый параметр обязательный — '
                                'в 1.x можно добавлять только необязательные')
    return problems


def test_snapshot_has_the_16_tools_of_1_0():
    snapshot = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
    assert {
        'list_projects', 'list_sources', 'get_index_status', 'reindex', 'remove_source',
        'search_metadata', 'get_object_details', 'search_attributes', 'find_references',
        'list_objects', 'search_procedures', 'get_procedure_code', 'get_call_tree',
        'search_code', 'get_module_outline', 'diagnose_index',
    } <= set(snapshot)


def test_no_breaking_changes():
    snapshot = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
    problems = breaking_changes(snapshot, current_contract())
    assert not problems, ('Несовместимые изменения контракта 1.x (допустимы только в 2.0):\n'
                          + '\n'.join(problems))


def test_snapshot_is_current():
    snapshot = json.loads(SNAPSHOT.read_text(encoding='utf-8'))
    current = current_contract()
    assert current == snapshot, (
        'Схема инструментов разошлась со снимком. Если изменение совместимое '
        '(новый инструмент, необязательный параметр), обновите снимок: '
        'python tests/test_tool_contract.py --update')


def test_breaking_change_detection():
    """Самопроверка правил на искусственных изменениях."""
    base = {'t': {'params': {'q': {'type': 'string', 'required': True, 'default': _ABSENT},
                             'mode': {'type': 'string', 'required': False, 'default': 'a',
                                      'enum': ['a', 'b']}},
                  'read_only': True, 'destructive': False, 'output': {'result': 'string'}}}

    def changed(fn):
        new = json.loads(json.dumps(base))
        fn(new['t'])
        return breaking_changes(base, new)

    assert changed(lambda t: None) == []
    assert changed(lambda t: t['params'].update(extra={'type': 'integer', 'required': False,
                                                       'default': 5})) == []
    assert changed(lambda t: t['params']['mode']['enum'].append('c')) == []
    assert changed(lambda t: t['params'].pop('q'))
    assert changed(lambda t: t['params']['q'].update(type='integer'))
    assert changed(lambda t: t['params']['mode'].update(default='b'))
    assert changed(lambda t: t['params']['mode'].update(required=True))
    assert changed(lambda t: t['params']['mode'].update(enum=['a']))
    assert changed(lambda t: t['params'].update(extra={'type': 'string', 'required': True,
                                                       'default': _ABSENT}))
    assert changed(lambda t: t.update(read_only=False))
    assert changed(lambda t: t.update(output={'result': 'object'}))
    assert breaking_changes(base, {})


if __name__ == '__main__':
    if sys.argv[1:] == ['--update']:
        SNAPSHOT.write_text(json.dumps(current_contract(), ensure_ascii=False, indent=2) + '\n',
                            encoding='utf-8', newline='\n')
        print(f'снимок обновлён: {SNAPSHOT}')
    else:
        print('Использование: python tests/test_tool_contract.py --update')
