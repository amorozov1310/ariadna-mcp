"""
Проверка окружения перед тестами.

Тесты рассчитаны на версии из pyproject.toml ([project].dependencies и
extra dev). В чужом окружении — например, глобальный Python со Starlette
0.52 и mcp 1.x — они падали с неочевидными ошибками («400 != 200» в проверке
Host) или молча пропускались (тесты SDK через importorskip). Поэтому сессия
сразу прерывается одним сообщением: что не так и как собрать окружение.
"""

import sys
import tomllib
from importlib import metadata
from pathlib import Path
from typing import Callable

import pytest

PYPROJECT = Path(__file__).resolve().parent.parent / 'pyproject.toml'


def required_packages(pyproject: Path = PYPROJECT) -> list[str]:
    project = tomllib.loads(pyproject.read_text(encoding='utf-8'))['project']
    return list(project['dependencies']) + list(project['optional-dependencies']['dev'])


def _installed_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def environment_mismatches(requirements: list[str],
                           version_of: Callable[[str], str | None] = _installed_version) -> list[str]:
    """Несоответствия установленных пакетов требованиям, по строке на пакет:
    «имя: не установлен (нужно …)» или «имя 0.52: нужно >=1.7». Пустой
    список — всё подходит. version_of подменяется в тестах."""
    from packaging.requirements import Requirement
    from packaging.version import InvalidVersion, Version

    problems = []
    for line in requirements:
        req = Requirement(line)
        if req.marker is not None and not req.marker.evaluate():
            continue
        wanted = str(req.specifier) or 'любая версия'
        installed = version_of(req.name)
        if installed is None:
            problems.append(f"{req.name}: не установлен (нужно {wanted})")
            continue
        try:
            ok = req.specifier.contains(Version(installed), prereleases=True)
        except InvalidVersion:
            ok = False
        if not ok:
            problems.append(f"{req.name} {installed}: нужно {wanted}")
    return problems


def _setup_hint() -> str:
    activate = r'.venv\Scripts\activate' if sys.platform == 'win32' else 'source .venv/bin/activate'
    return ("Соберите окружение по pyproject.toml (см. SETUP.md, «Разработка без Docker»):\n"
            "  python -m venv .venv\n"
            f"  {activate}\n"
            '  pip install -e ".[dev]"')


def pytest_sessionstart(session):
    problems = environment_mismatches(required_packages())
    if problems:
        pytest.exit(
            f"Окружение не соответствует pyproject.toml (Python {sys.executable}):\n  "
            + '\n  '.join(problems) + '\n\n' + _setup_hint(),
            returncode=4)
