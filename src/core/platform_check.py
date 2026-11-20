"""Сверка вызовов в модулях BSL с точными именами из синтакс-помощника.

Граф вызовов намеренно отбрасывает встроенные функции. Здесь нужны исходные
выражения, в том числе вложенные и перенесённые на следующую строку.
"""

import json
import re
import sqlite3
import zlib
from bisect import bisect_right
from collections import OrderedDict, defaultdict
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from itertools import islice
from threading import Lock

from .bsl_parser import _RE_MODULE_VAR_DECL, _form_context_vars, _parse_param_names, read_bsl_text
from .bsl_reference import module_context_vars
from .indexer import form_xml_for_module, module_file_hash, module_file_stat
from .metadata_model import OBJECT_TYPE_MAP
from .platform_help import compatibility_warning, version_key


_COLLECTIONS = {k.casefold(): ru for k, (ru, _) in OBJECT_TYPE_MAP.items()}
_KEYWORDS = set("""
    если if тогда then иначе else иначеесли elsif конецесли endif для for каждого each
    из in по to цикл do конеццикла enddo пока while попытка try исключение except
    конецпопытки endtry возврат return не not и and или or новый new ждать await
    вызватьисключение raise выполнить execute добавитьобработчик addhandler
    удалитьобработчик removehandler продолжитьвызов proceedwithcall
""".split())
# Внутри многострочной строки могут быть пустые строки и комментарии между
# строками с «|». Кавычки комментария не закрывают строковый литерал.
_STRING = r'"(?:""|[^"\r\n]|\r?\n(?:[ \t]*(?://[^\r\n]*)?\r?\n)*[ \t]*\|)*(?:"|(?=\r?$))'
_TOKEN = re.compile(r'//[^\n]*|' + _STRING + r'|\'[^\'\n]*\'|^[ \t]*[&#][^\n]*|\w+|[^\s]', re.M)
_NEW = {'новый', 'new'}
_EXECUTE = {'выполнить', 'execute'}
_VAR = {'перем', 'var'}
_BRANCHES = set('иначе else иначеесли elsif конецесли endif для for пока while цикл do '
                'конеццикла enddo исключение except конецпопытки endtry'.split())
_STATEMENT_BOUNDARIES = set('; : тогда then иначе else цикл do попытка try исключение except'.split())
_DELETION = re.compile(r'^[ \t]*#[ \t]*(удаление|delete|конецудаления|enddelete)\b', re.I | re.M)
_SERVER_CONTEXTS = {
    'сервер', 'внешнее соединение', 'мобильное приложение (сервер)', 'мобильный автономный сервер',
    'server', 'external connection', 'mobile application (server)', 'mobile standalone server',
    'интеграция', 'integration',
}
# Методы платформы, отсутствующие в shcntx. GetPredefinedItemName оставлен
# для совместимости (описание изменений платформы 8.3.3). Остальные методы
# проверены вызовом в учебной базе на 8.3.27.2170.
_METHODS_OUTSIDE_HELP = (
    (('справочникменеджер', 'catalogmanager'),
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
        self.property_entries = defaultdict(list)
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
                        self.property_entries[name].append((since, compat, availability))
                    else:
                        for owner in owners:
                            self.members[owner, name].append((since, compat, availability))
        for owners, names, since, availability in _METHODS_OUTSIDE_HELP:
            if since and version_key(since) > version_key(index.version):
                continue
            for owner in owners:
                if not owner or owner in self.owners:
                    for name in names:
                        self.members.setdefault((owner, name), [(since, '', availability)])


def _without_deletions(source):
    """Убирает удалённый код из всего модуля, сохраняя номера строк."""
    if not _DELETION.search(source):
        return source
    parts, depth, cursor = [], 0, 0
    for token in _TOKEN.finditer(source):
        directive = _DELETION.match(token[0])
        if not directive:
            continue
        if directive[1].casefold() in {'удаление', 'delete'}:
            if not depth:
                parts.append(source[cursor:token.start()])
                cursor = token.start()
            depth += 1
        elif depth:
            depth -= 1
            if not depth:
                parts.append(re.sub(r'[^\r\n]+', '', source[cursor:token.end()]))
                cursor = token.end()
    parts.append(re.sub(r'[^\r\n]+', '', source[cursor:]) if depth else source[cursor:])
    return ''.join(parts)


def _sites(body, first_line, params, bindings):
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
        if start < len(words) and words[start] in {'экспорт', 'export'}:
            start += 1
    newlines = [m.start() for m in re.finditer('\n', body)]
    types = {}
    locals_ = _parse_param_names(params or '') | bindings
    assignments = {}
    execute_pending = False
    for i in range(start, len(words) - 1):
        word, after = words[i:i + 2]
        before = words[i - 1] if i else ''
        if i in assignments:
            var, owner = assignments.pop(i)
            types[var] = owner
        if execute_pending and word == ';':
            types.clear()
            execute_pending = False
        # Через развилки, метки и директивы препроцессора тип не переносим.
        if before != '.' and (word in _BRANCHES or word.lstrip().startswith(('#', '~'))):
            types.clear()
        if not word.isidentifier():
            continue
        statement_start = (i == start or before in _STATEMENT_BOUNDARIES
                           or before.lstrip().startswith(('#', '&')))
        if statement_start and word in _EXECUTE:
            execute_pending = True
            continue
        if statement_start and word in _VAR:
            j = i + 1
            while j < len(words) and words[j].isidentifier():
                locals_.add(words[j])
                if words[j + 1:j + 2] != [',']:
                    break
                j += 2
        elif after == '=' and statement_start:
            locals_.add(word)
            types.pop(word, None)
            # Только прямое присваивание «Имя = Новый Тип[(...)] ;».
            if i + 3 < len(words) and words[i + 2] in _NEW and words[i + 3].isidentifier():
                end = i + 4
                if words[end:end + 1] == ['(']:
                    end = pairs.get(end, len(words)) + 1
                if words[end:end + 1] == [';']:
                    assignments[end] = (word, words[i + 3])
        elif before in {'(', ','} and after in {')', ','}:
            # Переменная в позиции аргумента может быть изменена по ссылке.
            types.pop(word, None)
        if after != '(' or (before != '.' and (word in _KEYWORDS or before in _NEW)):
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


@lru_cache(maxsize=128)
def _server_only(availability):
    contexts = {context.strip().casefold() for context in availability.split(',')}
    return contexts <= _SERVER_CONTEXTS


def _restrictions(entries, mode, client, subject):
    reasons = []
    if mode and entries:
        warnings = [compatibility_warning(since, compat, mode) for since, compat, _ in entries]
        if all(warnings):
            reasons.append(subject + ' ' + warnings[0])
    if client and entries and all(_server_only(a) for _, _, a in entries):
        if all(a.strip().casefold() in {'сервер', 'server'} for _, _, a in entries):
            reasons.append(f'{subject} доступен только на сервере, а процедура выполняется на клиенте')
        else:
            reasons.append(f'{subject} недоступен на клиенте, а процедура выполняется на клиенте')
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
            reasons = _restrictions(vocab.property_entries[receiver[0]], mode, client,
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
            yield next_line, ''.join(lines[next_line - 1:first - 1]), None, '', False
        body = ''.join(lines[first - 1:proc['end_line']])
        directives = proc['directive'].casefold().split()
        client = bool({'&наклиенте', '&atclient'}.intersection(directives))
        if re.search(r'^\s*#\s*(?:если|if)\b', body, re.I | re.M):
            client = False
        yield first, body, proc['params'], proc['directive'], client
        next_line = proc['end_line'] + 1
    if next_line <= len(lines):
        yield next_line, ''.join(lines[next_line - 1:]), None, '', False


_BLOCK_SIZE = 500
_REPORTS = OrderedDict()
_REPORTS_LOCK = Lock()
_MAX_REPORTS = 3


def _module_input(engine, mod):
    path = engine.module_file_path(mod)
    form = form_xml_for_module(path, mod['module_type']) if path else ''
    return path, form, module_file_stat(path, form) if path else ''


class _Report:
    """Продолжает разбор с последнего вызова. Сжатые блоки ограничивают
    расход памяти на больших выгрузках и позволяют читать любую страницу."""

    def __init__(self, conn, index, mode, source_id):
        self.lock = Lock()
        self.mode = mode
        self.vocab = _Vocabulary(index)
        self.declarations = defaultdict(set)
        self.globals = set()
        for module, name, global_export in conn.execute(
                """SELECT m.name_cf, p.name_cf, m.is_global AND p.is_export
                   FROM modules m LEFT JOIN procedures p ON p.module_id=m.id"""):
            self.declarations[module].add(name)
            if global_export:
                self.globals.add(name)
        self.modules = conn.execute(
            """SELECT m.id, m.name_cf, m.file_path, m.abs_path, m.source_id, m.object_id,
                      m.file_hash, m.module_type, s.stat, o.kind FROM modules m
               LEFT JOIN module_stat s ON s.module_id=m.id
               LEFT JOIN metadata_objects o ON o.id=m.object_id
               WHERE (? IS NULL OR m.source_id=?) AND m.file_path != ''
               ORDER BY m.source_id, m.file_path, m.id""", (source_id, source_id)).fetchall()
        self._reset()

    def _reset(self):
        self.blocks = []
        self.tail = []
        self.inputs = []
        self.current = iter(())

    def _module_findings(self, mod, procedures, bindings, form_attrs, path, form, stat):
        where = f"@{mod['source_id']} {mod['file_path']}"
        try:
            if path and mod['stat'] != stat and mod['file_hash'] != module_file_hash(path, form):
                yield f'{where} — Не проверено — файл изменился, обновите индекс'
                return
            source = read_bsl_text(path) if path else None
        except OSError:
            source = None
        if source is None:
            yield f'{where} — Не проверено — нет исходного текста'
            return
        lines = _without_deletions(source).splitlines(keepends=True)
        header_end = procedures[0]['start_line'] - 1 if procedures else len(lines)
        for line in lines[:header_end]:
            if match := _RE_MODULE_VAR_DECL.match(line.lstrip()):
                bindings.update(part.split()[0].casefold() for part in match[1].split(',') if part.strip())
        for first, body, params, directive, client in _regions(lines, procedures):
            visible = bindings | _form_context_vars(directive, form_attrs)
            for call in _sites(body, first, params, visible):
                message = _diagnosis(call, mod['name_cf'], self.declarations,
                                     self.globals, self.vocab, self.mode, client)
                if message:
                    yield f'{where}:{call.line} — {message}\n  {call.text}'

    def page(self, engine, conn, limit, offset):
        stop = offset + limit + 1
        # Для этой страницы достаточно сверить исходники от начала до её конца.
        # Иначе правка на диске могла изменить смещение всех следующих замечаний.
        for mod, (start, snapshot) in zip(self.modules, self.inputs):
            if start >= stop:
                break
            if _module_input(engine, mod) != snapshot:
                self._reset()
                break

        count = len(self.blocks) * _BLOCK_SIZE + len(self.tail)
        while count < stop:
            room = min(stop - count, _BLOCK_SIZE - len(self.tail))
            added = list(islice(self.current, room))
            self.tail.extend(added)
            count += len(added)
            if len(self.tail) == _BLOCK_SIZE:
                self.blocks.append(zlib.compress(json.dumps(self.tail, ensure_ascii=False).encode(), level=1))
                self.tail.clear()
            if len(added) == room:
                continue
            next_module = len(self.inputs)
            if next_module == len(self.modules):
                break
            mod = self.modules[next_module]
            snapshot = _module_input(engine, mod)
            procedures = conn.execute(
                """SELECT start_line, end_line, directive, params FROM procedures
                   WHERE module_id=? ORDER BY start_line, id""", (mod['id'],)).fetchall()
            module_type = mod['module_type']
            bindings = set(module_context_vars(mod['kind'], module_type))
            if module_type == 'МодульОбъекта':
                bindings.update(row[0] for row in conn.execute(
                    "SELECT name_cf FROM attributes WHERE object_id=? AND instr(name_cf, '.')=0",
                    (mod['object_id'],)))
            form_attrs = set()
            if module_type == 'МодульФормы':
                form_attrs = {row[0] for row in conn.execute(
                    "SELECT name_cf FROM form_attributes WHERE module_id=?", (mod['id'],))}
            self.inputs.append((count, snapshot))
            self.current = self._module_findings(mod, procedures, bindings, form_attrs, *snapshot)

        end = min(offset + limit, count)
        items = []
        pos = offset
        while pos < end:
            block, start = divmod(pos, _BLOCK_SIZE)
            values = json.loads(zlib.decompress(self.blocks[block])) if block < len(self.blocks) else self.tail
            take = min(_BLOCK_SIZE - start, end - pos)
            items.extend(values[start:start + take])
            pos += take
        return items, count > offset + limit


def check_calls(engine, index, mode, source_id, limit, offset):
    """Страница замечаний в стабильном порядке исходников, с просмотром на одно вперёд."""
    conn = engine.db.read_conn()
    state = conn.execute("SELECT updated_at, status FROM index_state WHERE id=1").fetchone()
    key = (engine.db, tuple(state) if state else (), module_file_stat(engine.db.db_path),
           module_file_stat(engine.db.db_path + '-wal'), tuple(sorted(engine.source_roots.items())),
           index.path, module_file_stat(index.path), mode, source_id)
    with _REPORTS_LOCK:
        report = _REPORTS.get(key)
        if report is not None:
            _REPORTS.move_to_end(key)
    if report is None:
        report = _Report(conn, index, mode, source_id)
        with _REPORTS_LOCK:
            report = _REPORTS.setdefault(key, report)
            if len(_REPORTS) > _MAX_REPORTS:
                _REPORTS.popitem(last=False)
    with report.lock:
        return report.page(engine, conn, limit, offset)
