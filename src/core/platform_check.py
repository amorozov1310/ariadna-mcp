"""Сверка вызовов в модулях BSL с точными именами из синтакс-помощника.

Граф вызовов намеренно отбрасывает встроенные функции. Здесь нужны исходные
выражения, в том числе вложенные и перенесённые на следующую строку.
"""

import json
import re
import sqlite3
import zlib
from array import array
from bisect import bisect_right
from collections import OrderedDict, defaultdict
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from heapq import merge
from threading import Lock

from .bsl_parser import (_BSL_TOKEN, _RE_MODULE_VAR_DECL, _form_context_vars,
                         _parse_param_names, read_bsl_text, without_deletions)
from .bsl_reference import module_context_vars
from .indexer import form_xml_for_module, module_file_hash, module_file_stat
from .metadata_model import MANAGER_OBJECT_KINDS, OBJECT_TYPE_MAP, indexed_manager_objects
from .platform_help import compatibility_warning, version_key


_COLLECTIONS = {k.casefold(): ru for k, (ru, _) in OBJECT_TYPE_MAP.items()}
_KEYWORDS = set("""
    если if тогда then иначе else иначеесли elsif конецесли endif для for каждого each
    из in по to цикл do конеццикла enddo пока while попытка try исключение except
    конецпопытки endtry возврат return не not и and или or новый new ждать await
    вызватьисключение raise выполнить execute добавитьобработчик addhandler
    удалитьобработчик removehandler продолжитьвызов proceedwithcall
""".split())
_NEW = {'новый', 'new'}
_EXECUTE = {'выполнить', 'execute'}
_VAR = {'перем', 'var'}
_BRANCHES = set('иначе else иначеесли elsif конецесли endif для for пока while цикл do '
                'конеццикла enddo исключение except конецпопытки endtry'.split())
_STATEMENT_BOUNDARIES = set('; : тогда then иначе else цикл do попытка try исключение except'.split())
_CONDITIONAL = re.compile(r'^\s*#\s*(if|если|elsif|иначеесли|else|иначе|endif|конецесли)\b([^/]*)')
_CLIENT_TEST = {'client then', 'клиент тогда'}
_SERVER_TEST = {'server then', 'сервер тогда'}
_CLIENT_DIRECTIVES = {'&наклиенте', '&atclient'}
_SERVER_DIRECTIVES = {'&насервере', '&atserver', '&насерверебезконтекста', '&atservernocontext'}
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
    client: bool


class _Vocabulary:
    """Имена RU/EN ведут к одной записи. Перегрузки проверяем все вместе."""

    def __init__(self, index):
        self.members = defaultdict(list)
        self.owners = set()
        self.properties = {}
        self.property_entries = defaultdict(list)
        self.member_properties = defaultdict(set)
        self.dynamic_owners = {'comобъект', 'comobject'}
        uri = index.path.resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            for kind, ru, en, owner_ru, owner_en, since, compat, availability, value_type in conn.execute(
                """SELECT kind, name_ru_cf, name_en_cf, owner_ru_cf, owner_en_cf,
                          since, compat, availability, value_type FROM entries
                   WHERE kind IN ('type','function','global_property','method','property')"""
            ):
                names = {ru, en} - {''}
                if kind == 'type':
                    self.owners.update(names)
                    continue
                owners = {''}
                if kind in ('method', 'property'):
                    owners = {s.partition('.<')[0] for s in (owner_ru, owner_en) if s}
                    if kind == 'method':
                        self.owners.update(owners)
                        if any('<' in name for name in names):
                            self.dynamic_owners.update(owners)
                for name in names:
                    if kind == 'global_property':
                        self.properties[name] = value_type.casefold() if value_type.isidentifier() else ''
                        self.property_entries[name].append((since, compat, availability))
                    elif kind == 'property':
                        for owner in owners:
                            self.member_properties[owner, name].add(
                                (value_type.casefold(), since, compat, availability))
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


def _sites(body, first_line, params, bindings, context):
    """Вызовы с позицией в тексте, получателем и доказанным типом переменной."""
    tokens = [m for m in _BSL_TOKEN.finditer(body) if not m[0].startswith('//')]
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
    scopes = []
    client = context is True
    may_run_on_client = context is not False
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
        if word.lstrip().startswith('#') and (condition := _CONDITIONAL.match(word)):
            # Только простые условия доказывают, что ветка клиентская.
            branch, test = condition[1], ' '.join(condition[2].split())
            if branch in {'if', 'если'}:
                scopes.append((client, may_run_on_client, True))
            if scopes:
                outer_client, outer_possible, remaining = scopes[-1]
                if branch in {'endif', 'конецесли'}:
                    scopes.pop()
                    client, may_run_on_client = outer_client, outer_possible
                else:
                    possible = outer_possible and remaining
                    may_run_on_client = possible and test not in _SERVER_TEST
                    client = possible and test in _CLIENT_TEST
                    if branch in {'else', 'иначе'}:
                        client = outer_client and possible
                    if test in _CLIENT_TEST:
                        scopes[-1] = outer_client, outer_possible, False
        if not word.isidentifier():
            continue
        # Директива может разрывать выражение. Сомнение в границе оператора
        # оставляет тип неизвестным, чтобы вызов получил «Не проверено».
        statement_start = i == start or before in _STATEMENT_BOUNDARIES
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
                    receiver, binding, client)


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
            reasons.append(f'{subject} доступен только на сервере, а вызов выполняется на клиенте')
        else:
            reasons.append(f'{subject} недоступен на клиенте, а вызов выполняется на клиенте')
    return reasons


def _diagnosis(call, module, declarations, globals_, objects, vocab, mode):
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
        obj = f'{kind}.{receiver[1]}'
        manager = f'{obj}.модульменеджера'
        if manager in declarations:
            procedures = declarations[manager]
            if name in procedures:
                return '' if procedures[name] else 'Ошибка — метод менеджера без «Экспорт»'
        elif kind in MANAGER_OBJECT_KINDS and objects is not None and obj not in objects:
            return f'Ошибка — объект метаданных {obj} отсутствует в выгрузке'
        owner = kind + 'менеджер'
    elif len(receiver) == 1:
        if f'общиймодуль.{receiver[0]}.модуль' in declarations and call.binding is None:
            return ''  # Вызовы общих модулей проверяет find_broken_calls.
        owner = call.binding if call.binding is not None else vocab.properties.get(receiver[0], '')
        if call.binding is None and receiver[0] in vocab.properties:
            reasons = _restrictions(vocab.property_entries[receiver[0]], mode, call.client,
                                    f'получатель {receiver[0]}')
    elif len(receiver) == 2 and call.binding:
        props = vocab.member_properties.get((call.binding, receiver[1]), ())
        candidates = {value_type for value_type, *_ in props}
        if len(candidates) == 1:
            target = next(iter(candidates))
            if vocab.members.get((target, name)):
                owner = target
                reasons = _restrictions([entry[1:] for entry in props], mode, call.client,
                                        f'свойство {receiver[1]}')
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
        reasons.extend(_restrictions(entries, mode, call.client, 'метод'))
    if reasons:
        return 'Предупреждение — ' + '. '.join(reasons) + (f'. {verdict}' if verdict else '')
    return verdict


def _regions(lines, procedures):
    """Границы из индекса отделяют процедуры от основной программы модуля."""
    next_line = 1
    for proc in procedures:
        first = proc['start_line']
        if first > next_line:
            yield next_line, ''.join(lines[next_line - 1:first - 1]), None, '', None
        body = ''.join(lines[first - 1:proc['end_line']])
        directives = set(proc['directive'].casefold().split())
        context = True if directives & _CLIENT_DIRECTIVES else None
        if directives & _SERVER_DIRECTIVES:
            context = False
        yield first, body, proc['params'], proc['directive'], context
        next_line = proc['end_line'] + 1
    if next_line <= len(lines):
        yield next_line, ''.join(lines[next_line - 1:]), None, '', None


_BLOCK_SIZE = 500
LEVELS = {'error': 'Ошибка', 'warning': 'Предупреждение', 'unchecked': 'Не проверено'}
_LEVEL_BY_LABEL = {label: key for key, label in LEVELS.items()}
_REPORTS = OrderedDict()
_REPORTS_LOCK = Lock()
_MAX_REPORTS = 3


def _module_input(engine, mod):
    path = engine.module_file_path(mod)
    form = form_xml_for_module(path, mod['module_type']) if path else ''
    return path, form, module_file_stat(path, form) if path else ''


class _Report:
    """Один снимок проверки на источник. Хранит текст блоками и позиции по уровням."""

    def __init__(self, conn, index, mode, source_id):
        self.lock = Lock()
        self.mode = mode
        self.vocab = _Vocabulary(index)
        self.declarations = defaultdict(dict)
        self.globals = set()
        self.objects = indexed_manager_objects(conn)
        for module, name, is_export, is_global in conn.execute(
                """SELECT m.name_cf, p.name_cf, p.is_export, m.is_global
                   FROM modules m LEFT JOIN procedures p ON p.module_id=m.id"""):
            procedures = self.declarations[module]
            if name:
                procedures[name] = procedures.get(name, False) or bool(is_export)
            if is_global and is_export:
                self.globals.add(name)
        self.modules = conn.execute(
            """SELECT m.id, m.name_cf, m.file_path, m.abs_path, m.source_id, m.object_id,
                      m.file_hash, m.module_type, s.stat, o.kind FROM modules m
               LEFT JOIN module_stat s ON s.module_id=m.id
               LEFT JOIN metadata_objects o ON o.id=m.object_id
               WHERE (? IS NULL OR m.source_id=?) AND m.file_path != ''
               ORDER BY m.source_id, m.file_path, m.id""", (source_id, source_id)).fetchall()
        self.inputs = None

    def _module_findings(self, mod, procedures, bindings, form_attrs, path, form, stat):
        where = f"@{mod['source_id']} {mod['file_path']}"
        try:
            if path and mod['stat'] != stat and mod['file_hash'] != module_file_hash(path, form):
                yield 'unchecked', f'{where} — Не проверено — файл изменился, обновите индекс'
                return
            source = read_bsl_text(path) if path else None
        except OSError:
            source = None
        if source is None:
            yield 'unchecked', f'{where} — Не проверено — нет исходного текста'
            return
        lines = without_deletions(source).splitlines(keepends=True)
        header_end = procedures[0]['start_line'] - 1 if procedures else len(lines)
        for line in lines[:header_end]:
            if match := _RE_MODULE_VAR_DECL.match(line.lstrip()):
                bindings.update(part.split()[0].casefold() for part in match[1].split(',') if part.strip())
        for first, body, params, directive, context in _regions(lines, procedures):
            visible = bindings | _form_context_vars(directive, form_attrs)
            for call in _sites(body, first, params, visible, context):
                message = _diagnosis(call, mod['name_cf'], self.declarations,
                                     self.globals, self.objects, self.vocab, self.mode)
                if message:
                    level = _LEVEL_BY_LABEL[message.partition(' — ')[0]]
                    yield level, f'{where}:{call.line} — {message}\n  {call.text}'

    def _build(self, engine, conn):
        self.blocks = []
        self.positions = {level: array('I') for level in LEVELS}
        self.selections = {}
        self.starts = []
        self.count = 0
        inputs, block = [], []
        for mod in self.modules:
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
            inputs.append(snapshot)
            self.starts.append(self.count)
            for level, msg in self._module_findings(mod, procedures, bindings, form_attrs, *snapshot):
                self.positions[level].append(self.count)
                self.count += 1
                block.append(msg)
                if len(block) == _BLOCK_SIZE:
                    self._pack(block)
        if block:
            self._pack(block)
        self.inputs = inputs

    def _pack(self, block):
        self.blocks.append(zlib.compress(json.dumps(block, ensure_ascii=False).encode(), level=1))
        block.clear()

    def _select(self, levels):
        if levels is None or len(levels) == len(LEVELS):
            return range(self.count)
        if len(levels) == 1:
            return self.positions[next(iter(levels))]
        if levels not in self.selections:
            self.selections[levels] = array('I', merge(*(self.positions[level] for level in levels)))
        return self.selections[levels]

    def page(self, engine, conn, limit, offset, levels):
        if self.inputs is None:
            self._build(engine, conn)
        else:
            positions = self._select(levels)[offset:offset + limit]
            window = {bisect_right(self.starts, pos) - 1 for pos in positions}
            if not self.count and not offset and levels is None:
                window = range(len(self.modules))
            # Между страницами проверяем файлы выдачи. При изменении
            # строим новый снимок, чтобы пересчитать уровни и смещения.
            if any(_module_input(engine, self.modules[i]) != self.inputs[i] for i in window):
                self.inputs = None
                self._build(engine, conn)

        selection = self._select(levels)
        items, prev = [], -1
        for pos in selection[offset:offset + limit]:
            block, index = divmod(pos, _BLOCK_SIZE)
            if block != prev:
                values = json.loads(zlib.decompress(self.blocks[block]))
                prev = block
            items.append(values[index])
        counts = {level: len(positions) for level, positions in self.positions.items()}
        return items, offset + limit < len(selection), counts, len(self.modules)


def check_calls(engine, index, mode, source_id, limit, offset, levels=None):
    """Страница замечаний и счётчики всего снимка в устойчивом порядке исходников."""
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
        return report.page(engine, conn, limit, offset, levels)
