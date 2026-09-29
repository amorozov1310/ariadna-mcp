"""
Релизные свойства сервера: версия из одного источника (src/VERSION),
безопасность транспорта, isError у ошибок инструментов.
"""

import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import src
from src.core.project_manager import ProjectManager

ROOT = Path(__file__).parent.parent


# ============================================
# Версия
# ============================================

def test_version_single_source():
    assert src.__version__ == (ROOT / 'src' / 'VERSION').read_text(encoding='utf-8').strip()
    assert src.__version__ == '0.2.0'
    pyproject = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    assert 'dynamic = ["version"]' in pyproject
    assert 'version = {file = "src/VERSION"}' in pyproject


def test_mcp_server_reports_package_version():
    pytest.importorskip("mcp.server.mcpserver")
    from src.mcp_server.server import create_mcp_server
    with tempfile.TemporaryDirectory() as tmpdir:
        pm = ProjectManager(tmpdir)
        try:
            assert create_mcp_server(pm).version == src.__version__
        finally:
            pm.close_all()


# ============================================
# Порты: по умолчанию только 127.0.0.1
# ============================================

def test_compose_publishes_ports_on_loopback_by_default():
    compose = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
    port_lines = [line.strip() for line in compose.splitlines()
                  if line.strip().startswith('- "') and ':98' in line]
    assert len(port_lines) == 3, port_lines
    for line in port_lines:
        assert line.startswith('- "${BIND_ADDR:-127.0.0.1}:'), line
