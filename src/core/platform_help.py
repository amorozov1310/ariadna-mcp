"""
Справка платформы 1С (синтакс-помощник) — разбор файлов .hbk, индекс на
версию платформы и поиск по нему. Инструмент MCP platform_help.

Содержимое справки принадлежит фирме 1С: в репозитории только разборщик.
Справку берут с установленной платформы (Windows: C:\\Program Files\\1cv8\\
<версия>\\bin, Linux: /opt/1cv8/x86_64/<версия>) или загружают файлом — у
каждой установки (в облаке — у каждого клиента) своя копия.

Формат .hbk (проверено на 8.3.27.2170) — контейнер 1С, как у файлов
конфигурации:
  - заголовок 16 байт: адрес первого свободного блока, размер страницы,
    ревизия, резерв (адрес свободного блока у разных файлов разный — как
    сигнатуру его не проверяем);
  - документ — цепочка блоков; блок начинается заголовком
    «\\r\\n» + 8 hex (размер документа) + « » + 8 hex (размер блока) + « » +
    8 hex (адрес следующего блока, 7fffffff — последний) + « \\r\\n»;
  - первый документ (с адреса 16) — оглавление: тройки uint32 LE (адрес
    заголовка элемента, адрес данных, 7fffffff); в заголовке элемента с
    20-го байта — имя в UTF-16LE;
  - элементы: Book (свойства книги), FileStorage (zip со страницами .html,
    шаблонами вызова .st и служебными __categories__), PackBlock (zip с одним
    файлом — дерево оглавления в скобочном формате 1С: русские и английские
    названия разделов и пути страниц), IndexPackBlock, PackLookup, MainData,
    IndexMainData (поисковый индекс самой справки — не используется).

Страницы синтакс-помощника (shcntx_ru.hbk):
  - h1.V8SH_pagetitle — «Владелец.Имя (Owner.Name)», у типа — «Имя (Name)»;
  - p.V8SH_title — владелец «Имя (Name)», p.V8SH_heading — элемент;
  - разделы — p.V8SH_chapter: «Синтаксис:», «Параметры:», «Возвращаемое
    значение:», «Описание:», «Доступность:», «Использование:» (у свойств),
    «Использование в версии:», «Примечание:», «Пример:», «См. также:»; у
    методов с вариантами — «Вариант синтаксиса: …» перед каждым набором
    «Синтаксис / Параметры / Описание варианта метода»;
  - параметр — div.V8SH_rubric «<Имя> (обязательный|необязательный)», затем
    «Тип: …. <br>описание»;
  - вид элемента — по пути страницы: …/methods/, …/properties/, …/events/,
    …/ctors/; «objects/Global context/…» — глобальные функции и свойства;
    страница типа — objects/…/<Type>.html вне этих папок.
Доступность берём со страницы («Доступность:»), а не из __categories__: там
служебные контексты показа справки, и у методов только серверных типов они
перечисляют и тонкий клиент.

Справка языка запросов (shquery_ru.hbk) — свободный текст: h1 («Функция
КОНЕЦПЕРИОДА»), первая строка — синтаксис, дальше описание. Русское имя —
из оглавления, английское — из имени страницы (ENDOFPERIOD, KeyWordsDISTINCT).

Индекс — отдельная SQLite на версию (не в индексе проекта):
<каталог индексов>/platform-help/<версия>/help.db. Пересобирается при смене
отпечатка разборщика (хеш этого файла) — как parser_fingerprint у кода.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import io
import json
import logging
import os
import re
import sqlite3
import struct
from collections.abc import Callable
from contextlib import closing, contextmanager
from functools import cache
from tempfile import TemporaryDirectory
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .fsretry import replace_with_retry, retry_on_permission_error
from .metadata_model import OBJECT_TYPE_MAP

logger = logging.getLogger('ariadna')

CONTEXT_FILE = 'shcntx_ru.hbk'
QUERY_FILE = 'shquery_ru.hbk'
INDEX_FILE = 'help.db'
SCHEMA_VERSION = '1'

_LAST = 0x7fffffff
_BLOCK_HEADER = 31                      # \r\n + 8 + ' ' + 8 + ' ' + 8 + ' \r\n'
_MAX_PAGE_BYTES = 2 * 1024 * 1024       # страница справки больше — не справка
_MAX_FILES = 200_000                    # защита от «zip-бомбы» в загруженном файле
_VERSION_RE = re.compile(r'[0-9]+(?:\.[0-9]+){3}')


class HbkError(ValueError):
    """Файл не похож на справку платформы 1С (.hbk)."""


@cache
def fingerprint() -> str:
    """Отпечаток разборщика: хеш этого модуля без учёта окончаний строк."""
    text = Path(__file__).read_text(encoding='utf-8').replace('\r\n', '\n')
    return hashlib.sha1(text.encode('utf-8')).hexdigest()[:16]


# ---------------------------------------------------------------- контейнер

def read_container(data: bytes) -> dict[str, bytes]:
    """Элементы контейнера .hbk: {имя: данные}."""
    if len(data) < 16 + _BLOCK_HEADER:
        raise HbkError('файл слишком короткий для справки 1С')

    def read_doc(addr: int) -> bytes:
        out = bytearray()
        total = None
        seen = set()
        while addr != _LAST:
            if addr < 0 or addr + _BLOCK_HEADER > len(data) or addr in seen:
                raise HbkError(f'повреждённая цепочка блоков (адрес {addr})')
            hdr = data[addr:addr + _BLOCK_HEADER]
            if hdr[:2] != b'\r\n' or hdr[-3:] != b' \r\n':
                raise HbkError(f'нет заголовка блока по адресу {addr}')
            try:
                size, page, nxt = (int(x, 16) for x in hdr[2:-3].split(b' '))
            except ValueError:
                raise HbkError(f'неверный заголовок блока по адресу {addr}') from None
            total = size if total is None else total
            start = addr + _BLOCK_HEADER
            take = min(page, total - len(out))
            if take < 0 or start + take > len(data) or total > len(data):
                raise HbkError('неверный размер блока справки')
            out += data[start:start + take]
            seen.add(addr)
            addr = nxt
        if total is not None and len(out) != total:
            raise HbkError('неполная цепочка блоков')
        return bytes(out)

    toc = read_doc(16)
    items: dict[str, bytes] = {}
    unpacked_size = 0
    for i in range(0, len(toc) - 11, 12):
        head_addr, data_addr, _ = struct.unpack('<III', toc[i:i + 12])
        if not head_addr and not data_addr:
            continue
        head = read_doc(head_addr)
        name = head[20:].decode('utf-16-le', 'replace').split('\0', 1)[0]
        if name in items:
            raise HbkError('повторяющееся имя элемента в контейнере')
        items[name] = read_doc(data_addr)
        unpacked_size += len(items[name])
        if unpacked_size > len(data):
            raise HbkError('суммарный размер элементов превышает размер контейнера')
    if 'FileStorage' not in items:
        raise HbkError('в файле нет страниц справки (FileStorage)')
    return items


def _zip(data: bytes, what: str) -> zipfile.ZipFile:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HbkError(f'{what}: не zip') from None
    if len(z.infolist()) > _MAX_FILES:
        raise HbkError(f'{what}: слишком много файлов')
    if sum(item.file_size for item in z.infolist()) > 512 * 1024 * 1024:
        raise HbkError(f'{what}: слишком большой объём распакованной справки')
    return z


def _read(z: zipfile.ZipFile, name: str, max_bytes: int = _MAX_PAGE_BYTES) -> str:
    info = z.getinfo(name)
    if info.file_size > max_bytes:
        return ''
    try:
        return z.read(info).decode('utf-8-sig', 'replace')
    except (zipfile.BadZipFile, RuntimeError, OSError) as e:
        raise HbkError(f'не удалось прочитать страницу справки: {name}') from e


# ---------------------------------------------------------------- оглавление

def parse_brackets(text: str):
    """Скобочный формат 1С ({1,"строка",{…}}) → вложенные списки строк."""
    stack: list[list] = [[]]
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '{':
            stack.append([])
            i += 1
        elif c == '}':
            node = stack.pop()
            if not stack:
                raise HbkError('лишняя «}» в оглавлении')
            stack[-1].append(node)
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n:
                if text[j] == '"':
                    if j + 1 < n and text[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                buf.append(text[j])
                j += 1
            stack[-1].append(''.join(buf))
            i = j + 1
        elif c in ', \r\n\t\ufeff':
            i += 1
        else:
            j = i
            while j < n and text[j] not in ',{}\r\n':
                j += 1
            stack[-1].append(text[i:j].strip())
            i = j
    return stack[0][0] if stack[0] else []


@dataclass
class TocNode:
    titles: dict[str, str]          # {'ru': …, 'en': …, '#': …}
    path: str                       # '/objects/…html' без начального «/» → нормализуется
    parent: int


def parse_toc(text: str) -> dict[int, TocNode]:
    """Дерево оглавления: id узла → (названия, путь страницы, родитель)."""
    root = parse_brackets(text)
    nodes: dict[int, TocNode] = {}
    for item in root[1:] if isinstance(root, list) else []:
        if not isinstance(item, list) or len(item) < 4:
            continue
        try:
            node_id, parent, count = int(item[0]), int(item[1]), int(item[2])
        except (TypeError, ValueError):
            continue
        if count < 0:
            continue
        body = item[3 + count] if len(item) > 3 + count else None
        if not isinstance(body, list) or len(body) < 4:
            continue
        titles: dict[str, str] = {}
        names = body[2] if isinstance(body[2], list) else []
        for pair in names[2:]:
            if isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) for x in pair):
                titles[pair[0]] = pair[1]
        path = body[3] if isinstance(body[3], str) else ''
        nodes[node_id] = TocNode(titles, path.lstrip('/'), parent)
    return nodes


def _title(node: TocNode) -> str:
    return node.titles.get('ru') or node.titles.get('#') or node.titles.get('en') or ''


def _section_of(nodes: dict[int, TocNode]) -> dict[str, tuple[str, dict[str, str]]]:
    """Путь страницы → (раздел оглавления «Раздел / Подраздел», названия узла)."""
    out: dict[str, tuple[str, dict[str, str]]] = {}
    for node_id, node in nodes.items():
        if not node.path:
            continue
        chain = []
        parent = node.parent
        guard = 0
        while parent in nodes and guard < 50:
            title = _title(nodes[parent])
            if title:
                chain.append(title)
            parent = nodes[parent].parent
            guard += 1
        out[node.path] = (' / '.join(reversed(chain)), node.titles)
    return out


# ---------------------------------------------------------------- страницы

KINDS = {
    'type': 'тип', 'method': 'метод', 'property': 'свойство', 'event': 'событие',
    'ctor': 'конструктор', 'function': 'глобальная функция',
    'global_property': 'глобальное свойство', 'query': 'язык запросов',
}
# Вид в запросе пользователя (RU/EN, ед. и мн. число) → вид в индексе.
KIND_ALIASES = {
    'тип': 'type', 'типы': 'type', 'type': 'type',
    'метод': 'method', 'методы': 'method', 'method': 'method',
    'свойство': 'property', 'свойства': 'property', 'property': 'property',
    'событие': 'event', 'события': 'event', 'event': 'event',
    'конструктор': 'ctor', 'конструкторы': 'ctor', 'ctor': 'ctor', 'constructor': 'ctor',
    'функция': 'function', 'функции': 'function', 'глобальная функция': 'function',
    'function': 'function',
    'глобальное свойство': 'global_property', 'global_property': 'global_property',
    'запрос': 'query', 'запросы': 'query', 'язык запросов': 'query', 'query': 'query',
}
_ITEM_FOLDERS = {'methods': 'method', 'properties': 'property', 'events': 'event', 'ctors': 'ctor'}
_GLOBAL_EN = 'Global context'

_CHAPTER = re.compile(r'<p class="V8SH_chapter">\s*(.*?)\s*</p>', re.S | re.I)
_TAG = re.compile(r'<[^>]+>')
_BR = re.compile(r'<\s*(br|/p|/div|/li|/h\d|/tr)\b[^>]*>', re.I)
_NAME_PAIR = re.compile(r'^(.*?)\s*\(([^()]*)\)\s*$')
_RUBRIC = re.compile(r'<div class="V8SH_rubric">(.*?)</div>', re.S | re.I)
_SINCE = re.compile(r'начиная с версии\s+(\d+(?:\.\d+)+)', re.I)
_COMPAT = re.compile(r'совместимости с верси(?:ей|и)\s+(\d+(?:\.\d+)+)', re.I)


def html_text(fragment: str) -> str:
    """Текст фрагмента страницы: теги — прочь, переносы — по <br>/<p>."""
    text = _BR.sub('\n', fragment)
    text = re.sub(r'<\s*li\b[^>]*>', '\n• ', text, flags=re.I)
    text = _TAG.sub('', text)
    text = html_lib.unescape(text).replace('\xa0', ' ')
    lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in text.split('\n')]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return '\n'.join(out).strip()


def _pair(text: str) -> tuple[str, str]:
    """«ТаблицаЗначений (ValueTable)» → (ru, en); без скобок — (text, '')."""
    text = text.strip()
    m = _NAME_PAIR.match(text)
    if m and re.fullmatch(r"[A-Za-z0-9_ .\-'<>]+", m.group(2).strip()):
        return m.group(1).strip(), m.group(2).strip()
    return text, ''


def _chapters(page: str) -> list[tuple[str, str]]:
    """[(заголовок раздела без «:», html раздела)] по p.V8SH_chapter."""
    marks = list(_CHAPTER.finditer(page))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(page)
        body = page[m.end():end]
        body = re.split(r'<HR\b', body, maxsplit=1, flags=re.I)[0]
        out.append((html_text(m.group(1)).rstrip(':').strip(), body))
    return out


def _types(text: str) -> list[str]:
    """«Тип: Строка; Число.» → ['Строка', 'Число']."""
    m = re.match(r'\s*Тип:\s*(.+?)(?:\.\s*(?:\n|$)|\n|$)', text, re.S)
    if not m:
        return []
    return [t.strip(' .') for t in re.split(r'[;,]\s*', m.group(1)) if t.strip(' .')]


def _strip_type_line(text: str) -> str:
    return re.sub(r'^\s*Тип:[^\n]*\n?', '', text, count=1).strip()


def _params(body: str) -> list[dict]:
    """Параметры раздела «Параметры»: имя, обязательность, типы, описание."""
    parts = _RUBRIC.split(body)
    out = []
    # split с группой: [до, рубрика1, после1, рубрика2, после2, …]
    for i in range(1, len(parts), 2):
        head = html_text(parts[i])
        rest = html_text(parts[i + 1]) if i + 1 < len(parts) else ''
        m = re.match(r'<\s*([^<>]+?)\s*>\s*(?:\((обязательный|необязательный)\))?', head)
        name = m.group(1) if m else head
        required = None
        if m and m.group(2):
            required = m.group(2) == 'обязательный'
        out.append({'name': name, 'required': required, 'types': _types(rest),
                    'description': _strip_type_line(rest)})
    return out


def _kind_and_owner(path: str) -> tuple[str | None, str]:
    """Вид страницы и английское имя владельца по пути страницы."""
    parts = path.split('/')
    if not parts or parts[0] != 'objects' or not path.endswith('.html'):
        return None, ''
    if len(parts) >= 3 and parts[1] == _GLOBAL_EN:
        folder = parts[2]
        if re.fullmatch(r'catalog\d+\.html', parts[-1]):
            return None, ''              # страница раздела («Функции работы с …»)
        if folder == 'methods':
            return 'function', _GLOBAL_EN
        if folder == 'properties':
            return 'global_property', _GLOBAL_EN
        return None, ''
    if len(parts) >= 3 and parts[-2] in _ITEM_FOLDERS:
        return _ITEM_FOLDERS[parts[-2]], parts[-3]
    name = parts[-1][:-5]
    if re.fullmatch(r'catalog\d+', name) or name == _GLOBAL_EN:
        return None, ''                 # страница раздела оглавления
    if len(parts) >= 2 and parts[-2] in ('formparams', 'methods', 'properties', 'events', 'ctors'):
        return None, ''
    return 'type', ''


@dataclass
class Entry:
    kind: str
    name_ru: str
    name_en: str = ''
    owner_ru: str = ''
    owner_en: str = ''
    variants: list[dict] = field(default_factory=list)   # [{title, syntax, params, description}]
    returns: str = ''
    returns_types: str = ''
    availability: str = ''
    since: str = ''
    compat: str = ''
    usage: str = ''
    value_type: str = ''
    description: str = ''
    note: str = ''
    section: str = ''
    path: str = ''
    book: str = 'context'


def parse_context_page(page: str, path: str, section: str = '') -> Entry | None:
    """Страница синтакс-помощника → Entry (None — служебная страница)."""
    kind, owner_en_path = _kind_and_owner(path)
    if kind is None:
        return None
    h1 = re.search(r'<h1[^>]*>(.*?)</h1>', page, re.S | re.I)
    title = html_text(h1.group(1)) if h1 else ''
    owner_tag = re.search(r'<p class="V8SH_title">(.*?)</p>', page, re.S | re.I)
    head_tag = re.search(r'<p class="V8SH_heading">(.*?)</p>', page, re.S | re.I)
    owner_ru, owner_en = _pair(html_text(owner_tag.group(1))) if owner_tag else ('', '')
    if kind == 'type':
        name_ru, name_en = _pair(title)
        owner_ru = owner_en = ''
    else:
        name_ru, name_en = _pair(html_text(head_tag.group(1))) if head_tag else _pair(title.split('.')[-1])
        owner_en = owner_en or owner_en_path
    if kind in ('function', 'global_property'):
        owner_ru, owner_en = 'Глобальный контекст', _GLOBAL_EN
    if not name_ru:
        return None

    entry = Entry(kind=kind, name_ru=name_ru, name_en=name_en, owner_ru=owner_ru,
                  owner_en=owner_en, section=section, path=path)
    since_text = ''
    variant: dict | None = None
    for chapter, body in _chapters(page):
        text = html_text(body)
        low = chapter.lower()
        if low.startswith('вариант синтаксиса'):
            variant = {'title': chapter.split(':', 1)[-1].strip(), 'syntax': '', 'params': [],
                       'description': ''}
            entry.variants.append(variant)
        elif low == 'синтаксис':
            if variant is None or variant['syntax']:
                variant = {'title': '', 'syntax': '', 'params': [], 'description': ''}
                entry.variants.append(variant)
            variant['syntax'] = text
        elif low == 'параметры':
            if variant is None:
                variant = {'title': '', 'syntax': '', 'params': [], 'description': ''}
                entry.variants.append(variant)
            variant['params'] = _params(body)
        elif low == 'описание варианта метода' and variant is not None:
            variant['description'] = text
        elif low == 'возвращаемое значение':
            entry.returns_types = ', '.join(_types(text))
            entry.returns = _strip_type_line(text)
        elif low == 'описание':
            if kind in ('property', 'global_property') and not entry.value_type:
                entry.value_type = ', '.join(_types(text))
                text = _strip_type_line(text)
            entry.description = text
        elif low == 'доступность':
            # Первый абзац — где доступно; дальше бывают «Внимание: …» и
            # «Возможен обмен с сервером. Сериализуется.» — это в примечание.
            first, _, rest = text.partition('\n')
            entry.availability = first.strip().rstrip('.')
            if rest.strip():
                entry.note = (rest.strip() + '\n' + entry.note).strip()
        elif low == 'использование':
            entry.usage = text.rstrip('.')
        elif low == 'использование в версии':
            since_text = text
        elif low in ('примечание', 'замечание'):
            entry.note = (entry.note + '\n' + text).strip()
    if not since_text:
        m = re.search(r'class="not_used">(.*?)</p>', page, re.S | re.I)
        since_text = html_text(m.group(1)) if m else ''
    if (m := _SINCE.search(since_text)):
        entry.since = m.group(1)
    if (m := _COMPAT.search(since_text)):
        entry.compat = m.group(1)
    if kind == 'ctor' and not entry.name_en:
        entry.name_en = path.rsplit('/', 1)[-1][:-5].removeprefix('ctor_')
    return entry


def parse_query_page(page: str, path: str, titles: dict[str, str], section: str) -> Entry | None:
    """Страница справки языка запросов → Entry вида 'query'."""
    h1 = re.search(r'<h1[^>]*>(.*?)</h1>', page, re.S | re.I)
    heading = html_text(h1.group(1)) if h1 else ''
    name_ru = (titles.get('ru') or titles.get('#') or heading).strip()
    if not name_ru:
        return None
    base = path.rsplit('/', 1)[-1]
    name_en = re.sub(r'\.html$', '', base)
    name_en = re.sub(r'^KeyWords', '', name_en)
    body = page[h1.end():] if h1 else page
    text = html_text(body)
    lines = [line for line in text.split('\n') if line]
    syntax = ''
    if lines and re.match(rf'{re.escape(name_ru.split()[-1])}\s*\(', lines[0], re.I):
        syntax = lines.pop(0)
    return Entry(kind='query', name_ru=name_ru, name_en=name_en if name_en.isascii() else '',
                 variants=[{'title': '', 'syntax': syntax, 'params': [], 'description': ''}]
                 if syntax else [],
                 description='\n'.join(lines), section=section or heading, path=path, book='query')


def parse_hbk(data: bytes, book: str = 'context'):
    """Записи справки из файла .hbk (генератор Entry)."""
    items = read_container(data)
    storage = _zip(items['FileStorage'], 'FileStorage')
    sections: dict[str, tuple[str, dict[str, str]]] = {}
    if 'PackBlock' in items:
        try:
            pack = _zip(items['PackBlock'], 'PackBlock')
            names = pack.namelist()
            if names:
                sections = _section_of(parse_toc(_read(pack, names[0], 32 * 1024 * 1024)))
        except HbkError as e:
            logger.warning("Справка платформы: оглавление не разобрано (%s)", e)
    for name in storage.namelist():
        if name.endswith('/') or '__categories__' in name or name.endswith('.st'):
            continue
        page = _read(storage, name)
        if not page:
            continue
        section, titles = sections.get(name, ('', {}))
        if book == 'query':
            if name.endswith('root.html') and not titles:
                continue
            entry = parse_query_page(page, name, titles, section)
        else:
            entry = parse_context_page(page, name, section)
        if entry is not None:
            yield entry


# ---------------------------------------------------------------- индекс

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE entries (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    name_ru TEXT NOT NULL, name_en TEXT NOT NULL DEFAULT '',
    owner_ru TEXT NOT NULL DEFAULT '', owner_en TEXT NOT NULL DEFAULT '',
    name_ru_cf TEXT NOT NULL, name_en_cf TEXT NOT NULL DEFAULT '',
    owner_ru_cf TEXT NOT NULL DEFAULT '', owner_en_cf TEXT NOT NULL DEFAULT '',
    variants TEXT NOT NULL DEFAULT '[]',
    returns TEXT NOT NULL DEFAULT '', availability TEXT NOT NULL DEFAULT '',
    since TEXT NOT NULL DEFAULT '', compat TEXT NOT NULL DEFAULT '',
    usage TEXT NOT NULL DEFAULT '', value_type TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
    section TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
    book TEXT NOT NULL DEFAULT 'context'
);
CREATE INDEX idx_entries_name_ru ON entries(name_ru_cf);
CREATE INDEX idx_entries_name_en ON entries(name_en_cf);
CREATE INDEX idx_entries_owner ON entries(owner_ru_cf, owner_en_cf);
CREATE VIRTUAL TABLE entries_fts USING fts5(
    names, owner, text, tokenize = 'unicode61 remove_diacritics 2'
);
"""


def name_words(name: str) -> str:
    """«НачалоМесяца» → «НачалоМесяца Начало Месяца»: слова имени для поиска
    своими словами («начало месяца»)."""
    words = re.findall(r'[A-ZА-ЯЁ]?[a-zа-яё0-9]+|[A-ZА-ЯЁ]+(?![a-zа-яё])', name)
    return ' '.join([name] + words) if len(words) > 1 else name


def _write_index(sources: dict[str, bytes], out: Path, version: str, origin: str,
                 progress: Callable[[str], None] | None) -> dict:
    """Индекс справки: sources — {'context': байты shcntx_ru.hbk, 'query': …}.
    out — новый файл во временном каталоге хранилища."""
    started = time.monotonic()
    conn = sqlite3.connect(out)
    counts: dict[str, int] = {}
    try:
        conn.executescript(_SCHEMA)
        total = 0
        for book, data in sources.items():
            label = 'Язык запросов' if book == 'query' else 'Синтакс-помощник'
            if progress:
                progress(f'{label} · чтение оглавления')
            before = total
            for e in parse_hbk(data, book):
                total += 1
                if progress and total % 500 == 0:
                    progress(f'{label} · обработано записей: {total}')
                counts[e.kind] = counts.get(e.kind, 0) + 1
                cur = conn.execute(
                    """INSERT INTO entries (kind, name_ru, name_en, owner_ru, owner_en, name_ru_cf,
                           name_en_cf, owner_ru_cf, owner_en_cf, variants, returns, availability,
                           since, compat, usage, value_type, description, note, section, path, book)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (e.kind, e.name_ru, e.name_en, e.owner_ru, e.owner_en, e.name_ru.casefold(),
                     e.name_en.casefold(), e.owner_ru.casefold(), e.owner_en.casefold(),
                     json.dumps(e.variants, ensure_ascii=False), e.returns, e.availability,
                     e.since, e.compat, e.usage, e.value_type, e.description, e.note, e.section,
                     e.path, e.book))
                conn.execute(
                    "INSERT INTO entries_fts (rowid, names, owner, text) VALUES (?,?,?,?)",
                    (cur.lastrowid, f'{name_words(e.name_ru)} {name_words(e.name_en)}',
                     f'{e.owner_ru} {e.owner_en}',
                     f'{e.section} {e.description[:2000]} {e.returns[:300]}'))
            if total == before:
                filename = CONTEXT_FILE if book == 'context' else QUERY_FILE
                raise HbkError(f'в книге нет страниц справки, ожидается {filename}')
        if not total:
            raise HbkError('не переданы книги справки')
        seconds = round(time.monotonic() - started, 1)
        meta = {'schema': SCHEMA_VERSION, 'version': version, 'origin': origin,
                'fingerprint': fingerprint(), 'built_at': datetime.now().isoformat(timespec='seconds'),
                'build_seconds': str(seconds), 'counts': json.dumps(counts, ensure_ascii=False),
                'books': ','.join(sorted(sources))}
        conn.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
        conn.commit()
        conn.execute("PRAGMA optimize")
    finally:
        conn.close()
    logger.info("Справка платформы %s: %d записей за %.1f с (%s)", version, total, seconds, origin)
    return {'entries': total, 'counts': counts, 'seconds': seconds}


def build_index(sources: dict[str, bytes], out: Path, version: str, origin: str,
                progress: Callable[[str], None] | None = None) -> dict:
    """Готовый индекс целиком заменяет предыдущий после успешной сборки обеих книг."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=out.parent) as tmp_dir:
        staged = Path(tmp_dir) / INDEX_FILE
        result = _write_index(sources, staged, version, origin, progress)
        replace_with_retry(staged, out)
    return result


def read_meta(path: Path) -> dict[str, str]:
    """meta готового индекса ({} — не индекс справки или не читается)."""
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            return dict(conn.execute("SELECT key, value FROM meta").fetchall())
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


# ---------------------------------------------------------------- поиск

_KIND_ORDER = {'function': 0, 'type': 1, 'global_property': 2, 'method': 3, 'property': 4,
               'ctor': 5, 'event': 6, 'query': 7}
_MANAGER_TYPES = {plural.casefold(): (ru + 'Менеджер').casefold()
                  for plural, (ru, _) in OBJECT_TYPE_MAP.items()}
_WORD = re.compile(r'[\wёЁ]+')


@dataclass
class HelpEntry:
    id: int
    kind: str
    name_ru: str
    name_en: str
    owner_ru: str
    owner_en: str
    variants: list[dict]
    returns: str
    availability: str
    since: str
    compat: str
    usage: str
    value_type: str
    description: str
    note: str
    section: str
    path: str

    @property
    def full_ru(self) -> str:
        return f'{self.owner_ru}.{self.name_ru}' if self.owner_ru and self.kind not in (
            'function', 'global_property') else self.name_ru

    @property
    def full_en(self) -> str:
        if not self.name_en:
            return ''
        if self.owner_en and self.kind not in ('function', 'global_property'):
            return f'{self.owner_en}.{self.name_en}'
        return self.name_en


class PlatformHelp:
    """Индекс справки одной версии платформы (только чтение)."""

    def __init__(self, path: Path):
        self.path = path
        self.version = path.parent.name

    @staticmethod
    def _row(r: sqlite3.Row) -> HelpEntry:
        return HelpEntry(r['id'], r['kind'], r['name_ru'], r['name_en'], r['owner_ru'], r['owner_en'],
                         json.loads(r['variants'] or '[]'), r['returns'], r['availability'],
                         r['since'], r['compat'], r['usage'], r['value_type'], r['description'],
                         r['note'], r['section'], r['path'])

    def search(self, query: str, kind: str | None = None, limit: int = 10,
               offset: int = 0) -> tuple[list[HelpEntry], bool]:
        """Поиск: точное «Тип.Метод» / имя (RU или EN), иначе — по словам
        (имена, разделы, описание). Возвращает (страница, есть ли продолжение)."""
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            return self._search(conn, query, kind, max(1, limit), max(0, offset))

    def _search(self, conn, query, kind, limit, offset):
        query = query.strip().removesuffix('()').strip()
        if not query:
            return [], False
        cf = query.casefold()
        kind = KIND_ALIASES.get((kind or '').strip().casefold(), kind)
        ctor = re.fullmatch(r'(?:новый|new)\s+(.+)', cf)
        owner, dot, name = cf.rpartition('.')
        if ctor:
            if kind and kind != 'ctor':
                return [], False
            owner, name, kind = ctor[1], '', 'ctor'
        elif dot:
            owner, name = owner.strip(), name.strip()
            if not owner or not name:
                return [], False
            collection, sep, obj = owner.partition('.')
            if sep and obj.isidentifier() and collection in _MANAGER_TYPES:
                owner = _MANAGER_TYPES[collection]

        conditions, params = [], []
        if kind:
            conditions.append('kind = ?')
            params.append(kind)
        if owner:
            conditions.append("""(owner_ru_cf = ? OR owner_en_cf = ? OR
                                  owner_ru_cf LIKE ? ESCAPE '!' OR owner_en_cf LIKE ? ESCAPE '!')""")
            pattern = owner.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '.<%>'
            params.extend([owner, owner, pattern, pattern])
        if name:
            conditions.append('(name_ru_cf = ? OR name_en_cf = ?)')
            params.extend([name, name])
        where = ' AND '.join(conditions)
        order = "CASE kind " + ' '.join(f"WHEN '{k}' THEN {v}" for k, v in _KIND_ORDER.items()) + " END"
        rows = conn.execute(f"SELECT * FROM entries WHERE {where} ORDER BY {order}, owner_ru_cf, id "
                            "LIMIT ? OFFSET ?", [*params, limit + 1, offset]).fetchall()
        # Существование точного имени проверяем и за последней страницей:
        # иначе при следующем offset поиск начнёт подмешивать похожие имена.
        if rows or owner or (offset and conn.execute(
                f"SELECT 1 FROM entries WHERE {where} LIMIT 1", params).fetchone()):
            return [self._row(r) for r in rows[:limit]], len(rows) > limit

        from .cards import query_stems
        terms = []
        for word in _WORD.findall(query):
            stems = query_stems(word) or [word.casefold()]
            terms.append('(' + ' OR '.join(f'"{s}"*' for s in dict.fromkeys(stems)) + ')')
        if not terms:
            return [], False
        kind_sql = ' AND e.kind = ?' if kind else ''
        for joiner in (' AND ', ' OR '):
            match = joiner.join(terms)
            args = [match, kind] if kind else [match]
            source = ('FROM entries_fts JOIN entries e ON e.id = entries_fts.rowid '
                      'WHERE entries_fts MATCH ?' + kind_sql)
            rows = conn.execute(f'SELECT e.* {source} ORDER BY bm25(entries_fts, 10, 2, 1), e.id '
                                'LIMIT ? OFFSET ?', [*args, limit + 1, offset]).fetchall()
            if rows or len(terms) == 1 or (offset and conn.execute(
                    f'SELECT 1 {source} LIMIT 1', args).fetchone()):
                break
        return [self._row(r) for r in rows[:limit]], len(rows) > limit


def _cut(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n].rstrip() + '…'


def compatibility_warning(since: str, compat: str, mode: str) -> str:
    """Ограничения из раздела «Использование в версии» для режима проекта."""
    minimum = compat or since
    if not mode or not minimum or version_key(minimum) <= version_key(mode):
        return ''
    reason = (f'требует режима совместимости {compat} или новее' if compat
              else f'появился в {since}')
    return reason + f', а у конфигурации режим совместимости {mode}'


def format_entry(e: HelpEntry, full: bool = True, mode: str = '') -> str:
    """Карточка записи справки для агента: сжато, длинное описание обрезано."""
    head = e.full_ru + (f' ({e.full_en})' if e.full_en and e.full_en != e.full_ru else '')
    lines = [f'{head} — {KINDS.get(e.kind, e.kind)}']
    warning = compatibility_warning(e.since, e.compat, mode)
    if warning:
        lines.append(f'Недоступно в режиме проекта: элемент {warning}.')
    if not full:
        syntax = next((v['syntax'] for v in e.variants if v.get('syntax')), '')
        if syntax:
            lines.append(f'  {syntax.splitlines()[0]}')
        if e.availability:
            lines.append(f'  Доступность: {e.availability}')
        if e.description:
            lines.append(f'  {_cut(e.description.splitlines()[0], 160)}')
        return '\n'.join(lines)
    for v in e.variants:
        prefix = f'Вариант «{v["title"]}»: ' if v.get('title') else 'Синтаксис: '
        if v.get('syntax'):
            lines.append(prefix + v['syntax'].replace('\n', ' '))
        for p in v.get('params', []):
            flag = '' if p.get('required') is None else (', обязательный' if p['required'] else ', необязательный')
            types = ', '.join(p.get('types') or [])
            desc = _cut(p.get('description', '').replace('\n', ' '), 160)
            lines.append(f'  <{p["name"]}>' + (f': {types}' if types else '') + flag
                         + (f' — {desc}' if desc else ''))
        if v.get('description') and v.get('title'):
            lines.append(f'  {_cut(v["description"].replace(chr(10), " "), 200)}')
    if e.value_type:
        lines.append(f'Тип значения: {e.value_type}')
    if e.usage:
        lines.append(f'Использование: {e.usage}')
    if e.returns:
        lines.append(f'Возвращает: {_cut(e.returns.replace(chr(10), " "), 240)}')
    if e.availability:
        lines.append(f'Доступность: {e.availability}')
    if e.since:
        lines.append(f'С версии {e.since}' + (f' (режим совместимости {e.compat} и новее)' if e.compat else ''))
    elif e.compat:
        lines.append(f'Режим совместимости {e.compat} и новее')
    if e.description:
        lines.append(f'Описание: {_cut(e.description, 600)}')
    if e.note:
        lines.append(f'Примечание: {_cut(e.note, 300)}')
    if e.section and e.kind == 'query':
        lines.append(f'Раздел: {e.section}')
    return '\n'.join(lines)


# ---------------------------------------------------------------- хранилище версий

def version_from_path(path: Path) -> str:
    """Версия платформы по пути: первая часть вида 8.3.27.2170 (с конца)."""
    for part in reversed(path.resolve().parts if path.exists() else path.parts):
        if _VERSION_RE.fullmatch(part):
            return part
    return ''


def version_key(version: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in version.split('.'))


def default_search_roots() -> list[Path]:
    """Где искать установленную платформу: ARIADNA_PLATFORM (пути через
    os.pathsep), по умолчанию стандартные места Windows и Linux, в Docker — /platform."""
    roots: list[Path] = []
    env = os.environ.get('ARIADNA_PLATFORM', '')
    if env:
        return [Path(p) for p in env.split(os.pathsep) if p.strip()]
    for var in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432'):
        if os.environ.get(var):
            roots.append(Path(os.environ[var]) / '1cv8')
    roots += [Path('/opt/1cv8'), Path('/opt/1C/v8.3'), Path('/platform')]
    out, seen = [], set()
    for r in roots:
        key = str(r).casefold()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def find_installed(roots: list[Path] | None = None) -> dict[str, Path]:
    """Установленные платформы: {версия: каталог с shcntx_ru.hbk}. Корень —
    сам каталог с .hbk, каталог версии (с bin) или каталог всех версий."""
    found: dict[str, Path] = {}
    for root in roots if roots is not None else default_search_roots():
        try:
            if not root.is_dir():
                continue
            candidates = [root, root / 'bin']
            for child in root.iterdir():
                if child.is_dir():
                    candidates += [child, child / 'bin']
                    if child.name in ('x86_64', 'i386'):
                        candidates += [g for g in child.iterdir() if g.is_dir()]
        except OSError:
            continue
        for c in candidates:
            if (c / CONTEXT_FILE).is_file():
                version = version_from_path(c)
                if version and version not in found:
                    found[version] = c
    return found


class PlatformHelpStore:
    """Индексы справки по версиям: <root>/<версия>/help.db. Общие для всех
    проектов установки (в облаке — своё хранилище у каждого клиента)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.Lock()
        self._building: dict[str, str] = {}        # версия → откуда строится
        self._errors: dict[str, str] = {}          # версия → причина последней неудачи

    def path(self, version: str) -> Path:
        if not _VERSION_RE.fullmatch(version):
            raise ValueError(f'версия платформы — вида 8.3.27.2170, а не «{version}»')
        return self.root / version / INDEX_FILE

    def versions(self) -> list[dict]:
        """Загруженные версии, новые сверху: версия, откуда, когда построен, записей."""
        out = []
        if self.root.is_dir():
            for d in self.root.iterdir():
                meta = read_meta(d / INDEX_FILE) if _VERSION_RE.fullmatch(d.name) else {}
                if meta:
                    try:
                        size = (d / INDEX_FILE).stat().st_size
                    except FileNotFoundError:
                        continue  # версию удалили после чтения meta
                    counts = json.loads(meta.get('counts') or '{}')
                    out.append({'version': d.name, 'origin': meta.get('origin', ''),
                                'built_at': meta.get('built_at', ''),
                                'entries': sum(counts.values()), 'counts': counts,
                                'stale': meta.get('fingerprint') != fingerprint()
                                or meta.get('schema') != SCHEMA_VERSION,
                                'size': size})
        return sorted(out, key=lambda v: version_key(v['version']), reverse=True)

    def newest(self) -> str:
        versions = self.versions()
        return versions[0]['version'] if versions else ''

    def get(self, version: str) -> PlatformHelp | None:
        path = self.path(version)
        return PlatformHelp(path) if path.is_file() else None

    def status(self) -> dict:
        with self._lock:
            return {'building': dict(self._building), 'errors': dict(self._errors)}

    def build_from_dir(self, directory: Path, version: str = '', origin: str = '') -> dict:
        """Построить индекс из каталога платформы (shcntx_ru.hbk, shquery_ru.hbk)."""
        directory = Path(directory)
        version = version or version_from_path(directory)
        if not version:
            raise ValueError(f'не удалось определить версию платформы по пути {directory}')
        sources = {}
        for book, name in (('context', CONTEXT_FILE), ('query', QUERY_FILE)):
            f = directory / name
            if f.is_file():
                sources[book] = f.read_bytes()
        if 'context' not in sources:
            raise HbkError(f'в {directory} нет {CONTEXT_FILE}')
        return self._build(version, sources, origin or str(directory))

    def build_from_bytes(self, version: str, context: bytes, query: bytes | None = None,
                         origin: str = 'загружено') -> dict:
        sources = {'context': context}
        if query is not None:
            sources['query'] = query
        return self._build(version, sources, origin)

    def install_index(self, version: str, data: bytes) -> dict:
        """Готовый индекс справки (help.db, построенный другой Ариадной)."""
        self.root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=self.root) as tmp_dir:
            tmp = Path(tmp_dir) / INDEX_FILE
            tmp.write_bytes(data)
            meta = read_meta(tmp)
            if meta.get('schema') != SCHEMA_VERSION:
                raise HbkError('выберите help.db, построенный совместимой версией Ариадны')
            version = version or meta.get('version', '')
            if not isinstance(version, str):
                raise HbkError('в индексе не указана версия платформы')
            if meta.get('version') != version:
                raise HbkError(f'индекс построен для {meta.get("version")}, ожидалась версия {version}')
            with self._operation(version, 'Проверка индекса') as target:
                try:
                    with closing(sqlite3.connect(tmp.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
                        conn.row_factory = sqlite3.Row
                        if conn.execute("PRAGMA quick_check(1)").fetchone()[0] != 'ok':
                            raise HbkError('файл SQLite повреждён')
                        conn.execute("SELECT name_ru_cf, name_en_cf, owner_ru_cf, owner_en_cf FROM entries LIMIT 0")
                        counts = dict(conn.execute("SELECT kind, COUNT(*) FROM entries GROUP BY kind"))
                        if not counts or counts != json.loads(meta['counts']):
                            raise HbkError('число записей справки не совпадает с описанием индекса')
                        PlatformHelp._row(conn.execute("SELECT * FROM entries LIMIT 1").fetchone())
                        conn.execute("SELECT rowid FROM entries_fts WHERE entries_fts MATCH 'тип' LIMIT 1").fetchone()
                except (sqlite3.Error, KeyError, IndexError, TypeError, ValueError) as e:
                    raise HbkError(f'повреждён индекс справки: {e}') from e
                replace_with_retry(tmp, target)
        return {'entries': sum(counts.values())}

    @contextmanager
    def _operation(self, version: str, stage: str):
        path = self.path(version)
        with self._lock:
            if version in self._building:
                raise ValueError(f'справка {version} уже строится')
            self._building[version] = stage
            self._errors.pop(version, None)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            yield path
        except Exception as e:
            with self._lock:
                self._errors[version] = str(e)
            raise
        finally:
            with self._lock:
                self._building.pop(version, None)

    def _build(self, version: str, sources: dict[str, bytes], origin: str) -> dict:
        def progress(message: str):
            with self._lock:
                self._building[version] = message

        with self._operation(version, 'Построение индекса') as target:
            return build_index(sources, target, version, origin, progress)

    def remove(self, version: str) -> bool:
        path = self.path(version)
        with self._lock:
            if version in self._building:
                raise ValueError(f'справка {version} сейчас строится')
            existed = path.is_file()
            retry_on_permission_error(lambda: path.unlink(missing_ok=True))
            self._errors.pop(version, None)
            if path.parent.is_dir() and not any(path.parent.iterdir()):
                path.parent.rmdir()
            return existed

    def sync_installed(self, roots: list[Path] | None = None, background: bool = True) -> list[str]:
        """Найти установленные платформы и построить недостающие или устаревшие
        индексы. Возвращает версии, которые будут построены."""
        have = {v['version']: v for v in self.versions()}
        todo = [(v, d) for v, d in find_installed(roots).items()
                if v not in have or have[v]['stale']]

        def run():
            for version, directory in todo:
                try:
                    self.build_from_dir(directory, version)
                except Exception:
                    logger.exception("Справка платформы %s не построена (%s)", version, directory)

        if todo and background:
            threading.Thread(target=run, daemon=True, name='platform-help').start()
        elif todo:
            run()
        return [v for v, _ in todo]
