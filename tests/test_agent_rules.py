"""
Шаблоны правил для агента (docs/agent-rules/) согласованы со схемой MCP.

Шаблоны копируют в проекты 1С (Cursor rules, CLAUDE.md, AGENTS.md), и агент
вызывает инструменты ровно так, как там написано. Если инструмент или
параметр переименуют (как module_path → module_name в 0.3.0), а шаблон
забудут, агент будет звать несуществующее. Поэтому каждый вызов вида
`инструмент(параметр, ...)` в шаблонах сверяется со схемой сервера.
"""

import asyncio
import re
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("mcp.server.mcpserver")

from src.core.project_manager import ProjectManager
from src.mcp_server.server import create_mcp_server

RULES_DIR = Path(__file__).resolve().parent.parent / 'docs' / 'agent-rules'
TEMPLATES = sorted(p for p in RULES_DIR.iterdir() if p.suffix in ('.mdc', '.md') and p.name != 'README.md')

# `get_call_tree(procedure_name, module_name, direction)` — вызов в шаблоне.
CALL = re.compile(r'`([a-z_]+)\(([^`)]*)\)`')


def _schema() -> dict[str, set[str]]:
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        try:
            tools = asyncio.run(create_mcp_server(pm).list_tools())
        finally:
            pm.close_all()
    return {t.name: set(t.input_schema.get('properties', {})) for t in tools}


def _calls(text: str) -> list[tuple[str, list[str]]]:
    calls = []
    for name, args in CALL.findall(text):
        params = [a.split('=')[0].strip() for a in args.split(',') if a.strip()]
        calls.append((name, params))
    return calls


def test_templates_exist():
    names = {p.name for p in TEMPLATES}
    assert {'ariadna-primary.mdc', 'ariadna-auxiliary.mdc', 'ariadna-snippet.md'} <= names


@pytest.mark.parametrize('path', TEMPLATES, ids=lambda p: p.name)
def test_template_calls_match_mcp_schema(path):
    schema = _schema()
    calls = _calls(path.read_text(encoding='utf-8'))
    assert calls, f"{path.name}: не найдено ни одного вызова инструмента"
    for name, params in calls:
        assert name in schema, f"{path.name}: инструмента {name} нет у сервера"
        unknown = [p for p in params if p not in schema[name]]
        assert not unknown, f"{path.name}: у {name} нет параметров {unknown}"


@pytest.mark.parametrize('path', [p for p in TEMPLATES if p.suffix == '.mdc'], ids=lambda p: p.name)
def test_cursor_rule_frontmatter_and_placeholders(path):
    text = path.read_text(encoding='utf-8')
    assert text.startswith('---\n'), f"{path.name}: нет frontmatter Cursor"
    frontmatter = text.split('---\n', 2)[1]
    assert re.search(r'^description: ".+"$', frontmatter, re.M)
    assert re.search(r'^alwaysApply: true$', frontmatter, re.M)
    # Шаблон, а не правило конкретного проекта: подстановки на месте.
    assert '<project_id>' in text
