"""Оформление — только в style.css: в шаблонах нет блоков <style>, а атрибут
style="…" допустим лишь для вычисляемых значений (ширина полосы, цвет из
данных). Так новые страницы не расходятся с дизайн-системой."""

import re
from pathlib import Path

TEMPLATES = Path(__file__).parent.parent / 'src' / 'web' / 'templates'
# Разрешено: значение из шаблонной переменной или JS-подстановки.
COMPUTED = re.compile(r'^\s*(width|background):\s*(\{\{.*\}\}%?|\$\{.*\})\s*;?\s*$')


def test_no_style_blocks():
    found = [p.name for p in TEMPLATES.glob('*.html') if '<style' in p.read_text(encoding='utf-8')]
    assert not found, f'блоки <style> в шаблонах: {found}'


def test_inline_style_only_for_computed_values():
    bad = []
    for path in TEMPLATES.glob('*.html'):
        for no, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            for value in re.findall(r'style="([^"]*)"', line):
                if not COMPUTED.match(value):
                    bad.append(f'{path.name}:{no}: style="{value}"')
    assert not bad, 'оформление — классом в style.css:\n' + '\n'.join(bad)


def test_decorative_icons_hidden_from_screen_readers():
    bad = []
    for path in TEMPLATES.glob('*.html'):
        for no, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if re.search(r'<svg class="icon(?![^>]*aria-hidden)', line):
                bad.append(f'{path.name}:{no}')
    assert not bad, 'значок без aria-hidden="true":\n' + '\n'.join(bad)
