"""
Проверка окружения из conftest.py: сверка установленных версий с
pyproject.toml. Версии подставные — от настоящего окружения тест не зависит.
"""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location('ariadna_conftest', Path(__file__).parent / 'conftest.py')
conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(conftest)


def _versions(**installed):
    return lambda name: installed.get(name.replace('-', '_').lower())


def test_mismatches_list_wrong_and_missing_packages():
    reqs = ['mcp>=2,<3', 'starlette>=1.7', 'uvicorn[standard]', 'pytest', 'httpx']
    # Как у разработчика с глобальным Python: старые Starlette и mcp, нет httpx.
    problems = conftest.environment_mismatches(
        reqs, _versions(mcp='1.9.4', starlette='0.52.1', uvicorn='0.30.0', pytest='8.0'))
    assert problems == [
        'mcp 1.9.4: нужно <3,>=2',
        'starlette 0.52.1: нужно >=1.7',
        'httpx: не установлен (нужно любая версия)',
    ]


def test_matching_environment_has_no_mismatches():
    reqs = ['mcp>=2,<3', 'starlette>=1.7', 'uvicorn[standard]', 'python-multipart']
    assert conftest.environment_mismatches(
        reqs, _versions(mcp='2.2.0', starlette='1.7.0', uvicorn='0.54.0',
                        python_multipart='0.0.32')) == []


def test_marker_for_other_platform_is_ignored():
    reqs = ['pywin32>=300; sys_platform == "nonexistent"']
    assert conftest.environment_mismatches(reqs, _versions()) == []


def test_requirements_come_from_pyproject_dependencies_and_dev():
    reqs = conftest.required_packages()
    assert 'starlette>=1.7' in reqs and 'mcp>=2,<3' in reqs
    assert 'pytest' in reqs and 'httpx' in reqs
    assert not any('generate-config-report' in r for r in reqs)   # xml-report — необязателен


def test_current_environment_matches():
    """Раз сессия дошла до тестов, проверка в conftest пропустила окружение."""
    assert conftest.environment_mismatches(conftest.required_packages()) == []
