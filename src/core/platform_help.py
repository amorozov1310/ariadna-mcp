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
import stat
import struct
from collections.abc import Callable
from contextlib import closing, contextmanager
from functools import cache
from tempfile import TemporaryDirectory
import threading
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .fsretry import replace_with_retry, retry_on_permission_error
from .metadata_model import OBJECT_TYPE_MAP

logger = logging.getLogger('ariadna')

CONTEXT_FILE = 'shcntx_ru.hbk'
QUERY_FILE = 'shquery_ru.hbk'
INDEX_FILE = 'help.db'
SCHEMA_VERSION = '2'
MAX_UPLOAD_BYTES = 256 * 1024 * 1024
UPLOAD_SIZE_ERROR = 'размер файла превышает 256 МБ'

_LAST = 0x7fffffff
_BLOCK_HEADER = 31                      # \r\n + 8 + ' ' + 8 + ' ' + 8 + ' \r\n'
_MAX_PAGE_BYTES = 2 * 1024 * 1024       # страница справки больше — не справка
_MAX_UNPACKED_BYTES = 512 * 1024 * 1024
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


@contextmanager
def _zip(data: bytes, what: str):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HbkError(f'{what}: не zip') from None
    with z:
        members = z.infolist()
        if len(members) > _MAX_FILES:
            raise HbkError(f'{what}: слишком много файлов')
        # Последняя запись с этим именем, как при поиске по имени в ZipFile.
        # В бюджет и в разбор попадают одни и те же записи, каждая один раз.
        entries = list({item.filename: item for item in members}.values())
        if sum(item.file_size for item in entries) > _MAX_UNPACKED_BYTES:
            raise HbkError(f'{what}: слишком большой объём распакованной справки')
        yield z, entries


def _read(z: zipfile.ZipFile, info: zipfile.ZipInfo, max_bytes: int = _MAX_PAGE_BYTES) -> str:
    if info.file_size > max_bytes:
        return ''
    try:
        with z.open(info) as member:
            # Верхняя граница чтения совпадает с вкладом записи в общий бюджет.
            data = member.read(info.file_size + 1)
        if len(data) != info.file_size:
            raise zipfile.BadZipFile('размер записи ZIP не совпадает с оглавлением')
        return data.decode('utf-8-sig', 'replace')
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, OSError) as e:
        raise HbkError(f'не удалось прочитать содержимое справки: {info.filename}') from e


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
_TAG = re.compile(r'<[^>]+(?:>|$)')
_BR = re.compile(r'<\s*(?:br|/p|/div|/li|/h\d|/tr)\b[^>]*(?:>|$)', re.I)
_LI = re.compile(r'<\s*li\b[^>]*(?:>|$)', re.I)
_NAME_PAIR = re.compile(r'^(.*?)\s*\(([^()]*)\)\s*$')
_RUBRIC = re.compile(r'<div class="V8SH_rubric">(.*?)</div>', re.S | re.I)
_SINCE = re.compile(r'начиная с версии\s+(\d+(?:\.\d+)+)', re.I)
_COMPAT = re.compile(r'совместимости с верси(?:ей|и)\s+(\d+(?:\.\d+)+)', re.I)


def html_text(fragment: str) -> str:
    """Текст фрагмента страницы: теги — прочь, переносы — по <br>/<p>."""
    def replace_closed(pattern: re.Pattern, replacement: str, text: str) -> str:
        # Незакрытый хвост поглощается одним совпадением и остаётся текстом.
        return pattern.sub(lambda m: replacement if m[0].endswith('>') else m[0], text)

    text = replace_closed(_BR, '\n', fragment)
    text = replace_closed(_LI, '\n• ', text)
    text = replace_closed(_TAG, '', text)
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
    sections: dict[str, tuple[str, dict[str, str]]] = {}
    if 'PackBlock' in items:
        with _zip(items['PackBlock'], 'PackBlock') as (pack, members):
            toc = _read(pack, members[0], 32 * 1024 * 1024) if members else ''
        try:
            sections = _section_of(parse_toc(toc))
        except HbkError as e:
            logger.warning("Справка платформы: оглавление не разобрано (%s)", e)
    with _zip(items['FileStorage'], 'FileStorage') as (storage, members):
        for info in members:
            name = info.filename
            if name.endswith('/') or '__categories__' in name or name.endswith('.st'):
                continue
            page = _read(storage, info)
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
    returns TEXT NOT NULL DEFAULT '', returns_types TEXT NOT NULL DEFAULT '',
    availability TEXT NOT NULL DEFAULT '',
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


def _fts_fields(entry: Entry | HelpEntry) -> tuple[str, str, str]:
    return (f'{name_words(entry.name_ru)} {name_words(entry.name_en)}',
            f'{entry.owner_ru} {entry.owner_en}',
            f'{entry.section} {entry.description[:2000]} {entry.returns[:300]}')


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
                           name_en_cf, owner_ru_cf, owner_en_cf, variants, returns, returns_types, availability,
                           since, compat, usage, value_type, description, note, section, path, book)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (e.kind, e.name_ru, e.name_en, e.owner_ru, e.owner_en, e.name_ru.casefold(),
                     e.name_en.casefold(), e.owner_ru.casefold(), e.owner_en.casefold(),
                     json.dumps(e.variants, ensure_ascii=False), e.returns, e.returns_types, e.availability,
                     e.since, e.compat, e.usage, e.value_type, e.description, e.note, e.section,
                     e.path, e.book))
                conn.execute(
                    "INSERT INTO entries_fts (rowid, names, owner, text) VALUES (?,?,?,?)",
                    (cur.lastrowid, *_fts_fields(e)))
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


def _read_counts(meta: dict[str, str]) -> dict[str, int]:
    counts = json.loads(meta['counts'])
    if not isinstance(counts, dict) or not counts or any(
            kind not in KINDS or type(count) is not int or count <= 0
            for kind, count in counts.items()):
        raise HbkError('неверное число записей по видам')
    return counts


# ---------------------------------------------------------------- поиск

_KIND_ORDER = {'function': 0, 'type': 1, 'global_property': 2, 'method': 3, 'property': 4,
               'ctor': 5, 'event': 6, 'query': 7}
COLLECTION_KINDS = {plural.casefold(): ru.casefold()
                    for plural, (ru, _) in OBJECT_TYPE_MAP.items()}
COLLECTION_MANAGER_TYPES = {plural.casefold(): ((ru + 'Менеджер').casefold(), (en + 'Manager').casefold())
                            for plural, (ru, en) in OBJECT_TYPE_MAP.items()}
# В отчёте конфигурации коллекция называется «Критерии», а свойство
# глобального контекста в коде — «КритерииОтбора».
COLLECTION_KINDS['критерииотбора'] = COLLECTION_KINDS['критерии']
COLLECTION_MANAGER_TYPES['критерииотбора'] = COLLECTION_MANAGER_TYPES['критерии']
_WORD = re.compile(r'[\wёЁ]+')
_CONSTRUCTOR = re.compile(r'(?:новый|new)\s+([^\s()][^()]*)(?:\(.*\))?', re.S)

# Методы платформы, отсутствующие в shcntx. GetPredefinedItemName оставлен
# для совместимости (описание изменений платформы 8.3.3). Остальные методы
# проверены вызовом в учебной базе на 8.3.27.2170.
_METHODS_OUTSIDE_HELP = (
    (('справочникменеджер', 'catalogmanager',
      'планвидовхарактеристикменеджер', 'chartofcharacteristictypesmanager',
      'плансчетовменеджер', 'chartofaccountsmanager',
      'планвидоврасчетаменеджер', 'chartofcalculationtypesmanager'),
     ('получитьимяпредопределенного', 'getpredefineditemname'), '', 'Сервер, толстый клиент, внешнее соединение'),
    (('объектметаданныхконфигурация', 'configurationmetadataobject'),
     ('представление', 'presentation', 'полноеимя', 'fullname'), '', ''),
    (('каноническаязаписьxml', 'xmlcanonicalizingwriter'),
     ('установитьстроку', 'setstring'), '', ''),
    (('менеджеробработкистрокиxml', 'xmlstringprocessingmanager'),
     ('удалитьнедопустимыесимволыxml', 'deletedisallowedxmlcharacters'), '8.3.20', ''),
    (('преобразованиеxsl', 'xsltransform'),
     ('загрузитьизстроки', 'loadfromstring', 'загрузитьизузла', 'loadfromnode',
      'загрузитьизфайла', 'loadfromfile'), '', 'Тонкий клиент, сервер, толстый клиент, внешнее соединение'),
    (('',), ('получитьинформациюосетевыхадаптерах', 'getnetworkadaptersinformation'),
     '8.3.18', 'Тонкий клиент, веб-клиент, толстый клиент'),
)


def owner_type(owner: str) -> str:
    """Тип владельца из справки без шаблона имени объекта метаданных."""
    return owner.partition('.<')[0]


def _owner_names(owner: str) -> tuple[tuple[str, str], tuple[str, str]]:
    """Точные имена RU/EN и имена типов, для которых подходят страницы с шаблоном."""
    root, _, item = owner.partition('.')
    if root.isidentifier() and item.isidentifier():
        if managers := COLLECTION_MANAGER_TYPES.get(root):
            return managers, managers
        return (owner, owner), (root, root)
    return (owner, owner), (owner, owner)


def methods_outside_help(version: str):
    """Известные вызовы без страницы справки, доступные в указанной версии."""
    for owners, names, since, availability in _METHODS_OUTSIDE_HELP:
        if not since or version_key(since) <= version_key(version):
            yield owners, names, since, availability


def is_method_outside_help(owner: str, name: str, version: str) -> bool:
    _, types = _owner_names(owner.casefold())
    name = name.casefold()
    return any(name in names and any(t in owners for t in types)
               for owners, names, _, _ in methods_outside_help(version))


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
    returns_types: str
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
        # Справка первой схемы остаётся доступной до завершения пересборки.
        returns_types = r['returns_types'] if 'returns_types' in r.keys() else ''
        return HelpEntry(r['id'], r['kind'], r['name_ru'], r['name_en'], r['owner_ru'], r['owner_en'],
                         json.loads(r['variants']), r['returns'], returns_types, r['availability'],
                         r['since'], r['compat'], r['usage'], r['value_type'], r['description'],
                         r['note'], r['section'], r['path'])

    def search(self, query: str, kind: str | None = None, limit: int = 10,
               offset: int = 0) -> tuple[list[HelpEntry], bool]:
        """Точное имя (RU или EN) с приоритетом BSL, затем поиск по словам.
        Возвращает (страница, есть ли продолжение)."""
        with closing(sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            return self._search(conn, query, kind, max(1, limit), max(0, offset))

    def _search(self, conn, query, kind, limit, offset):
        query = query.strip().removesuffix('()').strip()
        if not query:
            return [], False
        cf = query.casefold()
        kind = KIND_ALIASES.get((kind or '').strip().casefold(), kind)
        ctor = _CONSTRUCTOR.fullmatch(cf)
        owner, dot, name = cf.rpartition('.')
        if ctor:
            if kind and kind != 'ctor':
                return [], False
            owner, name, kind = ctor[1].strip(), '', 'ctor'
        elif dot:
            owner, name = '.'.join(part.strip() for part in owner.split('.')), name.strip()
            if not owner or not name:
                return [], False

        conditions, params = [], []
        if kind:
            conditions.append('kind = ?')
            params.append(kind)
        if owner:
            conditions.append("""(owner_ru_cf = ? OR owner_en_cf = ? OR
                                  owner_ru_cf LIKE ? ESCAPE '!' OR owner_en_cf LIKE ? ESCAPE '!')""")
            exact, template = _owner_names(owner)
            params.extend(exact)
            params.extend(t.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '.<%>'
                          for t in template)
        if name:
            conditions.append('(name_ru_cf = ? OR name_en_cf = ?)')
            params.extend([name, name])
            if not kind and not owner:
                # У одноимённой функции языка запросов отдельная карточка (kind="query").
                conditions.append("""(kind != 'query' OR NOT EXISTS (
                    SELECT 1 FROM entries WHERE kind != 'query' AND (name_ru_cf = ? OR name_en_cf = ?)
                ))""")
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


# У этих функций без нижней границы в справке режим проверен в Конфигураторе
# 8.3.27.2170 после обновления конфигурации БД. Регулярный поиск компилируется
# с 8.3.9. Четыре клиентские функции компилируются в том числе в 8.2.13.
_VERIFIED_MIN_MODES = {
    ('strfindbyregularexpression', '8.3.23'): '8.3.9',
    ('doqueryboxasync', '8.3.18'): '',
    ('beginattachingcomputerinformationextension', '8.3.18'): '',
    ('begininstallingcomputerinformationextension', '8.3.18'): '',
    ('begingettingnetworkadaptersinformation', '8.3.18'): '',
}


def minimum_compatibility_mode(since: str, compat: str, global_function_en: str = '') -> str:
    """Нижняя граница режима с учётом проверенных особенностей платформы."""
    if not compat:
        return _VERIFIED_MIN_MODES.get((global_function_en.casefold(), since), since)
    return max(since, compat, key=version_key)


def compatibility_warning(min_mode: str, mode: str) -> str:
    """Ограничения из раздела «Использование в версии» для режима проекта."""
    if not mode or not min_mode or version_key(min_mode) <= version_key(mode):
        return ''
    return (f'требует режима совместимости {min_mode} или новее, '
            f'а у конфигурации режим совместимости {mode}')


def format_entry(e: HelpEntry, full: bool = True, mode: str = '') -> str:
    """Карточка записи справки для агента: сжато, длинное описание обрезано."""
    head = e.full_ru + (f' ({e.full_en})' if e.full_en and e.full_en != e.full_ru else '')
    lines = [f'{head} — {KINDS.get(e.kind, e.kind)}']
    min_mode = minimum_compatibility_mode(e.since, e.compat, e.name_en if e.kind == 'function' else '')
    warning = compatibility_warning(min_mode, mode)
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
    if e.returns_types or e.returns:
        description = _cut(e.returns.replace('\n', ' '), 240)
        result = ' — '.join(part for part in (e.returns_types, description) if part)
        lines.append(f'Возвращает: {result}')
    if e.availability:
        lines.append(f'Доступность: {e.availability}')
    if e.since:
        lines.append(f'С версии {e.since}')
    if min_mode:
        lines.append(f'Режим совместимости {min_mode} и новее')
    if e.description:
        lines.append(f'Описание: {_cut(e.description, 600)}')
    if e.note:
        lines.append(f'Примечание: {_cut(e.note, 300)}')
    if e.section and e.kind == 'query':
        lines.append(f'Раздел: {e.section}')
    return '\n'.join(lines)


def _valid_variants(variants) -> bool:
    if not isinstance(variants, list):
        return False
    for variant in variants:
        if not isinstance(variant, dict) or not all(
                isinstance(variant.get(key, ''), str) for key in ('title', 'syntax', 'description')):
            return False
        params = variant.get('params', [])
        if not isinstance(params, list):
            return False
        for param in params:
            if not isinstance(param, dict) or not isinstance(param.get('name'), str):
                return False
            types = param.get('types', [])
            if (not isinstance(types, list) or not all(isinstance(t, str) for t in types)
                    or not isinstance(param.get('description', ''), str)
                    or (param.get('required') is not None and type(param['required']) is not bool)):
                return False
    return True


def _validate_index(conn: sqlite3.Connection, version: str, counts: dict[str, int]) -> None:
    if conn.execute("PRAGMA quick_check(1)").fetchone()[0] != 'ok':
        raise HbkError('файл SQLite повреждён')
    tables = dict(conn.execute("SELECT name, type FROM pragma_table_list WHERE schema='main'"))
    if (tables.get('meta'), tables.get('entries'), tables.get('entries_fts')) != (
            'table', 'table', 'virtual'):
        raise HbkError('неверная схема таблиц справки')
    meta = dict(conn.execute("SELECT key, value FROM meta"))
    if meta.get('schema') != SCHEMA_VERSION or meta.get('version') != version:
        raise HbkError('неверная схема или версия справки')
    text_columns = set(HelpEntry.__dataclass_fields__) - {'id'}
    text_columns.update({'book', 'name_ru_cf', 'name_en_cf', 'owner_ru_cf', 'owner_en_cf'})
    columns = {col[1]: (col[2].upper(), col[3], col[5])
               for col in conn.execute("PRAGMA table_info(entries)")}
    if columns != {'id': ('INTEGER', 0, 1), **dict.fromkeys(text_columns, ('TEXT', 1, 0))}:
        raise HbkError('неверная схема записей справки')
    actual_counts = dict(conn.execute("SELECT kind, COUNT(*) FROM entries GROUP BY kind"))
    if counts != actual_counts:
        raise HbkError('число записей справки не совпадает с описанием индекса')
    if conn.execute("SELECT COUNT(*) FROM entries_fts").fetchone()[0] != sum(counts.values()):
        raise HbkError('число записей полнотекстового индекса не совпадает со справкой')
    conn.execute("INSERT INTO entries_fts(entries_fts) VALUES ('integrity-check')")
    conn.row_factory = sqlite3.Row
    for row in conn.execute("""
        SELECT e.*, f.names AS fts_names, f.owner AS fts_owner, f.text AS fts_text
        FROM entries e LEFT JOIN entries_fts f ON f.rowid = e.id
    """):
        if any(not isinstance(row[key], str) for key in text_columns):
            raise HbkError(f'запись {row["id"]}: неверные данные справки')
        entry = PlatformHelp._row(row)
        if not _valid_variants(entry.variants):
            raise HbkError(f'запись {entry.id}: неверные варианты синтаксиса')
        if (row['fts_names'], row['fts_owner'], row['fts_text']) != _fts_fields(entry):
            raise HbkError(f'запись {entry.id}: полнотекстовый индекс не соответствует справке')


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
        return self.version_listing()[0]

    def version_listing(self) -> tuple[list[dict], str]:
        """Список версий и сообщение об ошибке доступа к каталогу."""
        try:
            directories = list(self.root.iterdir())
        except FileNotFoundError:
            return [], ''
        except OSError as e:
            logger.warning('Не удалось прочитать каталог справки платформы: %s', e)
            return [], 'Не удалось прочитать каталог справки платформы. Проверьте права доступа.'
        versions = [info for d in directories if _VERSION_RE.fullmatch(d.name)
                    if (info := self._version_info(d.name))]
        return sorted(versions, key=lambda v: version_key(v['version']), reverse=True), ''

    def _version_info(self, version: str) -> dict | None:
        path = self.path(version)
        size, meta, counts = 0, {}, {}
        stamp = None
        corrupt = False
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                return None
            size = info.st_size
            stamp = (info.st_dev, info.st_ino, info.st_mtime_ns, size)
            meta = read_meta(path)
            if meta.get('version') != version or not meta.get('schema'):
                raise HbkError('неверная версия или схема справки')
            counts = _read_counts(meta)
        except FileNotFoundError:
            return None
        except (OSError, KeyError, TypeError, ValueError, RecursionError):
            corrupt = True
        return {'version': version, 'origin': meta.get('origin', ''),
                'built_at': meta.get('built_at', ''),
                'entries': sum(counts.values()), 'counts': counts, 'size': size,
                'stale': meta.get('fingerprint') != fingerprint() or meta.get('schema') != SCHEMA_VERSION,
                'corrupt': corrupt, '_file_stamp': stamp}

    def newest(self) -> str:
        return next((v['version'] for v in self.versions() if not v['corrupt']), '')

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
        with self._operation(version, 'Чтение книг справки') as target:
            return self._build_from_dir(directory, target, version, origin)

    def _build_from_dir(self, directory: Path, target: Path, version: str, origin: str = '') -> dict:
        sources = {}
        for book, name in (('context', CONTEXT_FILE), ('query', QUERY_FILE)):
            path = directory / name
            if not path.is_file():
                continue
            size = path.stat().st_size
            if size > MAX_UPLOAD_BYTES:
                raise HbkError(f'{name}: {UPLOAD_SIZE_ERROR}')
            with path.open('rb') as stream:
                # Дополнительный байт позволяет заметить рост книги после stat().
                data = stream.read(size + 1)
            if len(data) > MAX_UPLOAD_BYTES:
                raise HbkError(f'{name}: {UPLOAD_SIZE_ERROR}')
            if len(data) != size:
                raise HbkError(f'{name}: размер файла изменился во время чтения')
            sources[book] = data
        if 'context' not in sources:
            raise HbkError(f'в {directory} нет {CONTEXT_FILE}')
        return self._build(version, target, sources, origin or str(directory))

    def build_from_bytes(self, version: str, context: bytes, query: bytes | None = None,
                         origin: str = 'загружено') -> dict:
        sources = {'context': context}
        if query is not None:
            sources['query'] = query
        with self._operation(version, 'Построение индекса') as target:
            return self._build(version, target, sources, origin)

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
            if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
                raise HbkError('в индексе не указана корректная версия платформы')
            if meta.get('version') != version:
                raise HbkError(f'индекс построен для {meta.get("version")}, ожидалась версия {version}')
            with self._operation(version, 'Проверка индекса') as target:
                try:
                    counts = _read_counts(meta)
                    with closing(sqlite3.connect(tmp.resolve().as_uri() + '?mode=rw', uri=True)) as conn:
                        _validate_index(conn, version, counts)
                        conn.rollback()  # Команда проверки FTS не должна менять загруженный файл.
                except (sqlite3.Error, KeyError, IndexError, TypeError, ValueError, RecursionError) as e:
                    raise HbkError(f'повреждён индекс справки: {e}') from e
                replace_with_retry(tmp, target)
        return {'entries': sum(counts.values()), 'version': version}

    @contextmanager
    def _operation(self, version: str, stage: str, *, skip_if: Callable[[], bool] | None = None):
        path = self.path(version)
        with self._lock:
            building = version in self._building
            skip = skip_if is not None and (building or skip_if())
            if not skip:
                if building:
                    raise ValueError(f'справка {version} уже строится')
                self._building[version] = stage
                self._errors.pop(version, None)
        if skip:
            yield None
            return
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

    def _build(self, version: str, target: Path, sources: dict[str, bytes], origin: str) -> dict:
        def progress(message: str):
            with self._lock:
                self._building[version] = message

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

    def _skip_auto_build(self, version: str, previous: dict | None) -> bool:
        current = self._version_info(version)
        if current is None:
            return False
        # Между снимком и началом сборки мог быть загружен другой индекс,
        # в том числе совместимый индекс с предыдущим отпечатком разборщика.
        return (previous is None or current['_file_stamp'] != previous['_file_stamp']
                or not (current['stale'] or current['corrupt']))

    def sync_installed(self, roots: list[Path] | None = None, background: bool = True) -> list[str]:
        """Найти установленные платформы и построить недостающие или устаревшие
        индексы. Возвращает версии, выбранные для проверки и сборки."""
        have = {v['version']: v for v in self.versions()}
        todo = [(v, d) for v, d in find_installed(roots).items()
                if v not in have or have[v]['stale'] or have[v]['corrupt']]

        def run():
            for version, directory in todo:
                try:
                    with self._operation(version, 'Чтение книг справки',
                                         skip_if=lambda: self._skip_auto_build(version, have.get(version))) as target:
                        if target is not None:
                            self._build_from_dir(directory, target, version)
                except Exception:
                    logger.exception("Справка платформы %s не построена (%s)", version, directory)

        if todo and background:
            threading.Thread(target=run, daemon=True, name='platform-help').start()
        elif todo:
            run()
        return [v for v, _ in todo]
