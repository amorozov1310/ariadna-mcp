"""Ариадна — MCP-сервер и Web UI для поиска по конфигурациям 1С."""

from pathlib import Path

# Единственный источник версии — src/VERSION: его же читает pyproject.toml
# (dynamic version), а в Docker пакет не устанавливается, src/ просто
# копируется, так что importlib.metadata там версии не знает.
__version__ = (Path(__file__).with_name('VERSION')).read_text(encoding='utf-8').strip()
