"""
Гигиена публичного репозитория: в отслеживаемых файлах нет названий
конкретных прикладных решений 1С и рабочих проектов, на которых сервер
отлаживался (фикстуры синтетические, примеры в документации — условные).

Запрещённые слова и пары слов хранятся только SHA-256-отпечатками, чтобы
сам тест их не называл. Слово ищется целиком и по частям CamelCase
(«ИмяСПродуктом» → «имя», «с», «продуктом»); сравнение без учёта регистра.
Чтобы добавить слово: sha256(слово.lower().encode()).hexdigest()[:16].
"""

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

DENY_WORDS = {
    'd24f1f612642b77b', 'c9c4dd63e6e9a5b4', '324427caecc7b13e', 'b760bc4758c6b4f6',
    '226a193ae269296d', 'e632ee71420b3323', '25b6a3a263ab1218', '8a428bcb4692cba5',
    '5846fe047fcafa59', '4b5a3684173ae793', '0eeb42a84a8f0e22', '0d18ee06581b5b75',
    'fb0f94c89b7b4f91', 'effd0d19fdfc9cba', '645658798a6be386', '39a2dacc3eeb1678',
    '66a14a0bcafc116e', '316dc359a2199e98', '1540248efd49f68d', 'de9189c1fd2afed4',
    '62f0ea8d1d6648fe', 'c13d31876758ebba', '2932a92b45e232a4', '06dc9d7ed4c6394e',
    'c3f8a5b3b382f9fd', 'b4bf5d7e5fcf89ef', 'd01a7dbd2e27f574', '26fe93b24b17f9ad',
    '6e285c9d3a9680be', '20a5eb1c43abd650', 'f312795e322c8746',
}
DENY_PAIRS = {
    '0de6c984fb710adf', 'cbeb270136924128', '872ea215235caf65',
    'c607c3dc877689a3', '7235e6824c354959', '19785d4b211236b0',
}

_WORD = re.compile(r'[0-9A-Za-zА-Яа-яЁё]+')
_PART = re.compile(r'[A-ZА-ЯЁ]?[a-zа-яё]+|[A-ZА-ЯЁ]+(?![a-zа-яё])|\d+')


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip('не git-checkout — проверять нечего')
    return [ROOT / name for name in out.decode('utf-8').split('\0') if name]


def _read_text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw.decode('utf-16')
    if b'\0' in raw[:8192]:
        return None  # двоичный файл (шрифты, картинки)
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return None


def _violations(path: Path, text: str):
    for n, line in enumerate(text.splitlines(), 1):
        words = _WORD.findall(line)
        for word in words:
            candidates = {word.lower()} | {p.lower() for p in _PART.findall(word)}
            for c in candidates:
                if _digest(c) in DENY_WORDS:
                    yield f'{path.relative_to(ROOT).as_posix()}:{n}: «{c}»'
        for a, b in zip(words, words[1:]):
            pair = f'{a.lower()} {b.lower()}'
            if _digest(pair) in DENY_PAIRS:
                yield f'{path.relative_to(ROOT).as_posix()}:{n}: «{pair}»'


def test_no_product_names_in_tracked_files():
    found = []
    for path in _tracked_files():
        text = _read_text(path)
        if text is not None:
            found.extend(_violations(path, text))
    assert not found, 'Названия продуктов в репозитории:\n' + '\n'.join(found[:50])


def test_denylist_catches_camel_case_parts():
    """Самопроверка разбора: слово из списка находится и внутри CamelCase-имени."""
    probe = 'x' * 3
    DENY_WORDS.add(_digest(probe))
    try:
        hits = list(_violations(ROOT / 'probe.txt', 'Справочники.ИмпортXxx.Прочитать()'))
    finally:
        DENY_WORDS.discard(_digest(probe))
    assert hits == ['probe.txt:1: «xxx»'], hits
