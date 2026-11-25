"""Отпечатки исходников, сохранённые при сборке Docker-образа."""

import json
from functools import cache
from pathlib import Path

_FINGERPRINTS = Path(__file__).with_name('fingerprints.json')


@cache
def bundled_fingerprints() -> dict[str, str]:
    if Path(__file__).suffix == '.pyc' and _FINGERPRINTS.is_file():
        return json.loads(_FINGERPRINTS.read_text(encoding='utf-8'))
    return {}


def write_fingerprints() -> None:
    """Сохранить те же отпечатки, которые нативный запуск вычисляет из .py."""
    from .indexer import Indexer
    from .platform_check import _report_fingerprint

    report, parser, help_parser = _report_fingerprint()
    values = {'report': report, 'parser': parser, 'help': help_parser,
              'resolver': Indexer.resolver_fingerprint()}
    if not all(values.values()):
        raise RuntimeError('Не удалось вычислить отпечатки исходников для Docker-образа')
    _FINGERPRINTS.write_text(json.dumps(values) + '\n', encoding='utf-8')


if __name__ == '__main__':
    write_fingerprints()
