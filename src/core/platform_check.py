"""Сверка вызовов в модулях BSL с точными именами из синтакс-помощника.

Граф вызовов намеренно отбрасывает встроенные функции. Здесь нужны исходные
выражения, в том числе вложенные и перенесённые на следующую строку.
"""

import re
import sqlite3
from bisect import bisect_right
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from itertools import islice
from sys import maxsize

from .bsl_parser import _parse_param_names, read_bsl_text
from .indexer import form_xml_for_module, module_file_hash, module_file_stat
from .metadata_model import OBJECT_TYPE_MAP
from .platform_help import compatibility_warning


_COLLECTIONS = {k.casefold(): ru for k, (ru, _) in OBJECT_TYPE_MAP.items()}
_KEYWORDS = set("""
    если if тогда then иначе else иначеесли elsif конецесли endif для for каждого each
    из in по to цикл do конеццикла enddo пока while попытка try исключение except
    конецпопытки endtry возврат return не not и and или or новый new ждать await
    вызватьисключение raise выполнить execute добавитьобработчик addhandler
    удалитьобработчик removehandler продолжитьвызов proceedwithcall
""".split())
_TOKEN = re.compile(r'//[^\n]*|"(?:""|[^"])*(?:"|$)|\'[^\'\n]*\'|^[ \t]*[&#][^\n]*|\w+|[^\s]', re.M)
_NEW = {'новый', 'new'}
_BRANCHES = set('иначе else иначеесли elsif конецесли endif для for пока while цикл do '
                'конеццикла enddo исключение except конецпопытки endtry'.split())


@dataclass
class _Call:
    line: int
    text: str
    name: str
    receiver: list[str]
    # None — имя может быть глобальным, '' — локальная переменная неизвестного типа.
    binding: str | None


class _Vocabulary:
    """Имена RU/EN ведут к одной записи. Перегрузки проверяем все вместе."""

    def __init__(self, index):
        self.members = defaultdict(list)
        self.owners = set()
        self.properties = {}
        self.dynamic_owners = {'comобъект', 'comobject'}
        uri = index.path.resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            for kind, ru, en, owner_ru, owner_en, since, compat, availability, value_type in conn.execute(
                """SELECT kind, name_ru_cf, name_en_cf, owner_ru_cf, owner_en_cf,
                          since, compat, availability, value_type FROM entries
                   WHERE kind IN ('type','function','global_property','method')"""
            ):
                names = {ru, en} - {''}
                if kind == 'type':
                    self.owners.update(names)
                    continue
                owners = {''}
                if kind == 'method':
                    owners = {s.partition('.<')[0] for s in (owner_ru, owner_en) if s}
                    self.owners.update(owners)
                    if any('<' in name for name in names):
                        self.dynamic_owners.update(owners)
                for name in names:
                    if kind == 'global_property':
                        self.properties[name] = value_type.casefold() if value_type.isidentifier() else ''
                    for owner in owners:
                        self.members[owner, name].append((since, compat, availability))


def _sites(body, first_line, params):
    """Вызовы с позицией в тексте, получателем и доказанным типом переменной."""
    tokens = [m for m in _TOKEN.finditer(body) if not m[0].startswith('//')]
    words = [m[0].casefold() for m in tokens]
    stack, pairs = [], {}
    for i, word in enumerate(words):
        if word == '(':
            stack.append(i)
        elif word == ')' and stack:
            pairs[stack.pop()] = i
    # В том числе многострочная сигнатура и скобки в строках по умолчанию.
    start = 0
    if params is not None:
        signature = words.index('(') if '(' in words else -1
        if signature not in pairs:
            return
        start = pairs[signature] + 1
    newlines = [m.start() for m in re.finditer('\n', body)]
    types = {}
    locals_ = _parse_param_names(params or '')
    assignments = {}
    for i in range(start, len(words) - 1):
        word, after = words[i:i + 2]
        before = words[i - 1] if i else ''
        if i in assignments:
            var, owner = assignments.pop(i)
            types[var] = owner
        # Через развилки, метки и директивы препроцессора тип не переносим.
        if word in _BRANCHES or word.lstrip().startswith(('#', '~')):
            types.clear()
        if not word.isidentifier():
            continue
        if after == '=' and before != '.':
            locals_.add(word)
        if after == '=' and i + 3 < len(words) and words[i + 2] in _NEW and before != '.':
            # Только прямое присваивание «Имя = Новый Тип[(...)] ;».
            end = i + 4
            if words[end:end + 1] == ['(']:
                end = pairs.get(end, len(words)) + 1
            if words[end:end + 1] == [';'] and words[i + 3].isidentifier():
                assignments[end] = (word, words[i + 3])
        if after != '.':
            # Включает присваивание и передачу по ссылке.
            types.pop(word, None)
        if after != '(' or word in _KEYWORDS or before in _NEW:
            continue
        j = i
        while j >= 2 and words[j - 1] == '.' and words[j - 2].isidentifier():
            j -= 2
        receiver = words[j:i:2]
        binding = types.get(words[j], '' if words[j] in locals_ else None)
        offset = tokens[j].start()
        if j and words[j - 1] == '.':
            # Для цепочки после результата выражения показываем исходную строку.
            receiver = ['…', *receiver]
            offset = body.rfind('\n', 0, offset) + 1
        end = tokens[pairs.get(i + 1, i + 1)].end()
        snippet = ' '.join(body[offset:min(end, offset + 160)].split())
        if offset + 160 < end:
            snippet += '…'
        yield _Call(first_line + bisect_right(newlines, tokens[i].start()), snippet, word,
                    receiver, binding)


def _restrictions(entries, mode, client, subject):
    warnings = [compatibility_warning(since, compat, mode) for since, compat, _ in entries]
    reasons = []
    if warnings and all(warnings):
        reasons.append(subject + ' ' + warnings[0])
    if client and entries and all(a.casefold() == 'сервер' for _, _, a in entries):
        reasons.append(f'{subject} доступен только на сервере, а процедура выполняется на клиенте')
    return reasons


def _diagnosis(call, module, declarations, globals_, vocab, mode, client):
    name, receiver = call.name, call.receiver
    owner = ''
    implicit = False
    reasons = []
    if not receiver:
        if name in declarations[module] or name in globals_:
            return ''
        if module.endswith('.модульменеджера'):
            owner = module.split('.')[0] + 'менеджер'
        elif module.endswith('.модульобъекта'):
            owner = module.split('.')[0] + 'объект'
        elif '.' in module and not module.startswith('общиймодуль.'):
            implicit = True
    elif len(receiver) == 2 and receiver[0] in _COLLECTIONS and call.binding is None:
        kind = _COLLECTIONS[receiver[0]].casefold()
        if name in declarations.get(f'{kind}.{receiver[1]}.модульменеджера', ()):
            return ''
        owner = kind + 'менеджер'
    elif len(receiver) == 1:
        if f'общиймодуль.{receiver[0]}.модуль' in declarations and call.binding is None:
            return ''  # Вызовы общих модулей проверяет find_broken_calls.
        owner = call.binding if call.binding is not None else vocab.properties.get(receiver[0], '')
        if call.binding is None and receiver[0] in vocab.properties:
            reasons = _restrictions(vocab.members['', receiver[0]], mode, client,
                                    f'получатель {receiver[0]}')
    entries = vocab.members.get((owner, name), ())
    if not receiver and not entries:
        entries = vocab.members.get(('', name), ())
    if receiver and (owner not in vocab.owners or (owner in vocab.dynamic_owners and not entries)):
        verdict = 'Не проверено — тип получателя или состав его методов неизвестен'
    elif not entries:
        if implicit or (owner and owner not in vocab.owners):
            return 'Не проверено — неизвестен полный состав методов контекста'
        label = f' типа {owner}' if owner else ' глобального контекста'
        verdict = f'Ошибка — в справке нет метода{label} с таким именем'
    else:
        verdict = ''
        reasons.extend(_restrictions(entries, mode, client, 'метод'))
    if reasons:
        return (verdict + '. ' if verdict else '') + 'Предупреждение — ' + '. '.join(reasons)
    return verdict


def _regions(lines, procedures):
    """Границы из индекса отделяют процедуры от основной программы модуля."""
    next_line = 1
    for proc in procedures:
        first = proc['start_line']
        if first > next_line:
            yield next_line, ''.join(lines[next_line - 1:first - 1]), None, False
        body = ''.join(lines[first - 1:proc['end_line']])
        directives = proc['directive'].casefold().split()
        client = bool({'&наклиенте', '&atclient'}.intersection(directives))
        if re.search(r'^\s*#\s*(?:если|if)\b', body, re.I | re.M):
            client = False
        yield first, body, proc['params'], client
        next_line = proc['end_line'] + 1
    if next_line <= len(lines):
        yield next_line, ''.join(lines[next_line - 1:]), None, False


def check_calls(engine, index, mode, source_id, limit, offset):
    """Страница замечаний в стабильном порядке исходников, с просмотром на одно вперёд."""
    conn = engine.db.read_conn()
    declarations = defaultdict(set)
    globals_ = set()
    for row in conn.execute("""SELECT m.name_cf, p.name_cf, m.is_global AND p.is_export
                               FROM modules m LEFT JOIN procedures p ON p.module_id=m.id"""):
        declarations[row[0]].add(row[1])
        if row[2]:
            globals_.add(row[1])
    vocab = _Vocabulary(index)

    def findings():
        modules = conn.execute("""SELECT m.id, m.name_cf, m.file_path, m.abs_path, m.source_id,
                                      m.file_hash, m.module_type, s.stat FROM modules m
                               LEFT JOIN module_stat s ON s.module_id=m.id
                               WHERE (? IS NULL OR m.source_id=?)
                                 AND m.file_path != ''
                               ORDER BY m.source_id, m.file_path, m.id""", (source_id, source_id))
        for mod in modules:
            where = f"@{mod['source_id']} {mod['file_path']}"
            path = engine.module_file_path(mod)
            try:
                form = form_xml_for_module(path, mod['module_type']) if path else ''
                if (path and mod['stat'] != module_file_stat(path, form)
                        and mod['file_hash'] != module_file_hash(path, form)):
                    yield f'{where} — Не проверено — файл изменился, обновите индекс'
                    continue
                source = read_bsl_text(path) if path else None
            except OSError:
                source = None
            if source is None:
                yield f'{where} — Не проверено — нет исходного текста'
                continue
            lines = source.splitlines(keepends=True)
            procedures = conn.execute("""SELECT start_line, end_line, directive, params FROM procedures
                                         WHERE module_id=? ORDER BY start_line, id""", (mod['id'],))
            for first, body, params, client in _regions(lines, procedures):
                for call in _sites(body, first, params):
                    message = _diagnosis(call, mod['name_cf'], declarations,
                                         globals_, vocab, mode, client)
                    if message:
                        yield f'{where}:{call.line} — {message}\n  {call.text}'

    items = list(islice(findings(), min(offset, maxsize), min(offset + limit + 1, maxsize)))
    return items[:limit], len(items) > limit
