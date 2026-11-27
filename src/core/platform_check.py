"""Сверка вызовов в модулях BSL с точными именами из синтакс-помощника.

Граф вызовов намеренно отбрасывает встроенные функции. Здесь нужны исходные
выражения, в том числе вложенные и перенесённые на следующую строку.
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import zlib
from array import array
from bisect import bisect_left, bisect_right
from collections import OrderedDict, defaultdict
from contextlib import closing
from dataclasses import dataclass
from functools import cache, lru_cache
from heapq import merge
from itertools import groupby
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock, Thread
from weakref import WeakKeyDictionary, WeakSet, WeakValueDictionary
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

from .bsl_parser import (_BSL_TOKEN, _RE_MODULE_VAR_DECL, _form_context_vars,
                         _parse_param_names, without_deletions)
from .bsl_reference import module_context_vars
from .code_fingerprints import bundled_fingerprints
from .db import IndexRemovedError
from .fsretry import replace_with_retry, retry_on_permission_error
from .metadata_model import MANAGER_OBJECT_KINDS, canonicalize_kind, indexed_manager_objects
from .parser_fingerprint import index_uses_old_parser, parser_fingerprint
from .platform_help import (COLLECTION_KINDS, COLLECTION_MANAGER_TYPES, compatibility_warning,
                            fingerprint as help_fingerprint, methods_outside_help,
                            minimum_compatibility_mode, owner_type, read_meta)


logger = logging.getLogger('ariadna')


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
_IF = {'if', 'если'}
_ELSIF = {'elsif', 'иначеесли'}
_ELSE = {'else', 'иначе'}
_ENDIF = {'endif', 'конецесли'}
_THEN = {'then', 'тогда'}
_IF_BRANCHES = _IF | _ELSIF | _ELSE | _ENDIF
_OPTIONAL_BLOCK = {'цикл': 'loop', 'do': 'loop', 'попытка': 'try', 'try': 'try'}
_OPTIONAL_END = {'конеццикла': 'loop', 'enddo': 'loop', 'конецпопытки': 'try', 'endtry': 'try'}
_EXCEPT = {'исключение', 'except'}
_METADATA = {'метаданные', 'metadata'}
_UNDEFINED = {'неопределено', 'undefined'}
_EXIT = {'возврат', 'return', 'вызватьисключение', 'raise',
         'продолжить', 'continue', 'прервать', 'break'}
_CLIENT_DIRECTIVES = {'&наклиенте', '&atclient'}
_SERVER_DIRECTIVES = {'&насервере', '&atserver', '&насерверебезконтекста', '&atservernocontext'}
_SERVER_CONTEXTS = {
    'сервер', 'внешнее соединение', 'мобильное приложение (сервер)', 'мобильный автономный сервер',
    'server', 'external connection', 'mobile application (server)', 'mobile standalone server',
    'интеграция', 'integration',
}
@dataclass
class _Call:
    line: int
    text: str
    name: str
    receiver: list[str]
    # None — имя может быть глобальным, '' — локальная переменная неизвестного типа.
    binding: str | None
    client: bool
    split_receiver: bool = False
    optional_object: bool = False


_CLIENT_TARGETS = (
    ('thinclient', 'тонкийклиент'), ('webclient', 'вебклиент'),
    ('thickclientmanagedapplication', 'толстыйклиентуправляемоеприложение'),
    ('thickclientordinaryapplication', 'толстыйклиентобычноеприложение'),
    ('mobileclient', 'мобильныйклиент'), ('mobileappclient', 'мобильноеприложениеклиент'),
)
_SERVER_TARGETS = (
    ('server', 'сервер'), ('mobileappserver', 'мобильноеприложениесервер'),
    ('mobilestandaloneserver', 'мобильныйавтономныйсервер'),
)
_TARGETS = (*_CLIENT_TARGETS, *_SERVER_TARGETS, ('externalconnection', 'внешнеесоединение'))
_CLIENT = (1 << len(_CLIENT_TARGETS)) - 1
_SERVER = ((1 << len(_SERVER_TARGETS)) - 1) << len(_CLIENT_TARGETS)
_ALL_TARGETS = (1 << len(_TARGETS)) - 1
_SYMBOLS = {name: 1 << i for i, aliases in enumerate(_TARGETS) for name in aliases}
_SYMBOLS.update(dict.fromkeys(('client', 'atclient', 'клиент', 'наклиенте'), _CLIENT))
_SYMBOLS.update(dict.fromkeys(('server', 'atserver', 'сервер', 'насервере'), _SERVER))
_CONDITION_TOKEN = re.compile(r'\w+|[^\s]')


@lru_cache(maxsize=512)
def _condition_targets(expression):
    """Маски сред, где условие может быть истинным или ложным."""
    tokens = iter(_CONDITION_TOKEN.findall(expression))
    token = next(tokens, '')

    def consume():
        nonlocal token
        value, token = token, next(tokens, '')
        return value

    def atom():
        value = consume()
        if value in {'not', 'не'}:
            yes, no = atom()
            return no, yes
        if value == '(':
            result = disjunction()
            if consume() != ')':
                raise ValueError
            return result
        if not value.isidentifier():
            raise ValueError
        mask = _SYMBOLS.get(value)
        return (_ALL_TARGETS, _ALL_TARGETS) if mask is None else (mask, _ALL_TARGETS ^ mask)

    def conjunction():
        yes, no = atom()
        while token in {'and', 'и'}:
            consume()
            right_yes, right_no = atom()
            yes, no = yes & right_yes, no | right_no
        return yes, no

    def disjunction():
        yes, no = conjunction()
        while token in {'or', 'или'}:
            consume()
            right_yes, right_no = conjunction()
            yes, no = yes | right_yes, no & right_no
        return yes, no

    try:
        result = disjunction()
        if token in {'then', 'тогда'} and not next(tokens, ''):
            return result
    except (ValueError, RecursionError):
        pass
    return _ALL_TARGETS, _ALL_TARGETS


class _Preprocessor:
    """Текущая ветка и среды, оставшиеся для следующих веток каждого #Если."""

    def __init__(self):
        self.targets = _ALL_TARGETS
        self.stack = []

    def switch(self, branch, expression):
        if branch in _IF:
            self.stack.append((self.targets, self.targets))
        if not self.stack:
            return
        if branch in _ENDIF:
            self.targets, _ = self.stack.pop()
            return
        outer, remaining = self.stack[-1]
        yes, no = (_ALL_TARGETS, 0) if branch in _ELSE else _condition_targets(expression.strip())
        self.targets = remaining & yes
        self.stack[-1] = outer, remaining & no


def _split_receivers(words):
    """Восстанавливает связь после точки, которую разделили ветки #Если."""
    previous = {-1}
    stack, followers, split = [], defaultdict(set), {}
    for i, word in enumerate(words):
        if word.lstrip().startswith('#'):
            if condition := _CONDITIONAL.match(word):
                branch = condition[1]
                if branch in _IF:
                    stack.append((previous, set(), False))
                elif stack:
                    entry, exits, has_else = stack[-1]
                    exits.update(previous)
                    if branch in _ENDIF:
                        stack.pop()
                        previous = exits if has_else else exits | entry
                    else:
                        previous = entry
                        stack[-1] = entry, exits, branch in _ELSE
            continue
        dots = {p for p in previous if p >= 0 and words[p] == '.'}
        for dot in dots:
            followers[dot].add(i)
        if dots and previous != {i - 1}:
            split[i] = next(iter(dots)) if len(previous) == 1 else None
        previous = {i}
    # Тип получателя можно перенести через развилку только при одном и том же
    # имени метода во всех ветках, которые продолжают выражение после точки.
    stable = {dot for dot in set(split.values()) - {None}
              if len({words[h] for h in followers[dot]}) == 1
              and all(words[h + 1:h + 2] == ['('] for h in followers[dot])}
    return {head: dot if dot in stable else None for head, dot in split.items()}


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
                    owners = {owner_type(s) for s in (owner_ru, owner_en) if s}
                    if kind == 'method':
                        self.owners.update(owners)
                        if any('<' in name for name in names):
                            self.dynamic_owners.update(owners)
                min_mode = minimum_compatibility_mode(since, compat, en if kind == 'function' else '')
                entry = (min_mode, availability)
                for name in names:
                    if kind == 'global_property':
                        self.properties[name] = value_type.casefold() if value_type.isidentifier() else ''
                        self.property_entries[name].append(entry)
                    elif kind == 'property':
                        for owner in owners:
                            self.member_properties[owner, name].add(
                                (value_type.casefold(), *entry))
                    else:
                        for owner in owners:
                            self.members[owner, name].append(entry)
        for owners, names, since, availability in methods_outside_help(index.version):
            for owner in owners:
                if not owner or owner in self.owners:
                    for name in names:
                        self.members.setdefault((owner, name), [(since, availability)])


def _metadata_lookup(words, roots, aliases):
    """Полное имя объекта, который ищет выражение."""
    match words:
        case [alias]:
            return aliases.get(alias)
        case [root, '.', method, '(', arg, ')'] if (
                root in roots and method in {'найтипополномуимени', 'findbyfullname'}):
            if arg.startswith('"') and arg.endswith('"'):
                kind, _, name = arg[1:-1].partition('.')
                if kind.isidentifier() and name.isidentifier():
                    return f'{canonicalize_kind(kind).casefold()}.{name}'
        case [root, '.', collection, '.', method, '(', arg, ')'] if (
                root in roots and collection in COLLECTION_KINDS and method in {'найти', 'find'}):
            if arg.startswith('"') and arg.endswith('"') and arg[1:-1].isidentifier():
                return f'{COLLECTION_KINDS[collection]}.{arg[1:-1]}'


def _metadata_condition(words, roots, aliases):
    """Объект и значение условия, из которого следует его наличие."""
    negated = False
    while words:
        if words[0] in {'не', 'not'}:
            negated = not negated
            words = words[1:]
        elif words[0] == '(' and words[-1] == ')':
            words = words[1:-1]
        else:
            break
    match words:
        case [*lookup, '=', undef] if undef in _UNDEFINED:
            return _metadata_lookup(lookup, roots, aliases), negated
        case [undef, '=', *lookup] if undef in _UNDEFINED:
            return _metadata_lookup(lookup, roots, aliases), negated
        case [*lookup, '<', '>', undef] if undef in _UNDEFINED:
            return _metadata_lookup(lookup, roots, aliases), not negated
        case [undef, '<', '>', *lookup] if undef in _UNDEFINED:
            return _metadata_lookup(lookup, roots, aliases), not negated
        case [root, '.', collection, '.', method, '(', *arg, ')'] if (
                root in roots and collection in COLLECTION_KINDS and method in {'содержит', 'contains'}):
            kind = COLLECTION_KINDS[collection]
            match arg:
                case [arg_root, '.', arg_collection, '.', name] if (
                        arg_root in roots and COLLECTION_KINDS.get(arg_collection) == kind
                        and name.isidentifier()):
                    obj = f'{kind}.{name}'
                case _:
                    obj = _metadata_lookup(arg, roots, aliases)
            if obj and obj.partition('.')[0] == kind:
                return obj, not negated
    return None, False


def _join_guards(left, right):
    if left is None:
        return right
    return left if right is None else left & right


def _metadata_guards(words, start, locals_):
    """Позиции вызовов, до которых можно дойти лишь при наличии объекта."""
    roots = _METADATA - locals_
    if not roots.intersection(words) or '~' in words:
        return set()
    guards = set()
    # None — ветка завершилась. В точке слияния учитываем только ветки,
    # которые доходят до неё, и только общие для них проверки.
    facts, stack, aliases = set(), [], {}
    skip_through = start - 1
    condition_facts = None
    exiting = False
    for i in range(start, len(words)):
        word = words[i]
        before = words[i - 1] if i else ''
        after = words[i + 1] if i + 1 < len(words) else ''
        if before == '.':
            continue
        active = condition_facts if i <= skip_through else facts
        if active and word in COLLECTION_KINDS and words[i + 1:i + 6:2] == ['.', '.', '(']:
            if f'{COLLECTION_KINDS[word]}.{words[i + 2]}' in active:
                guards.add(i + 4)
        if i <= skip_through:
            continue
        directive = _CONDITIONAL.match(word) if word.lstrip().startswith('#') else None
        branch = directive[1] if directive else ''
        if exiting and (word in _IF_BRANCHES | _EXCEPT or word in _OPTIONAL_END or branch or word == ';'):
            facts, exiting = None, False
        if word in _IF_BRANCHES:
            if word in _IF:
                stack.append(('if', facts, None))
            if not stack or stack[-1][0] != 'if':
                return set()
            _, remaining, complete = stack.pop()
            if word not in _IF:
                complete = _join_guards(complete, facts)
                aliases.clear()
            if word in _ENDIF:
                facts = _join_guards(complete, remaining)
            elif word in _ELSE:
                facts = remaining
                stack.append(('if', None, complete))
            else:
                skip_through = next((j for j in range(i + 1, len(words)) if words[j] in _THEN),
                                    len(words))
                obj, when_true = _metadata_condition(words[i + 1:skip_through], roots, aliases)
                facts = condition_facts = remaining
                if obj and remaining is not None:
                    if when_true:
                        facts = remaining | {obj}
                    else:
                        remaining = remaining | {obj}
                stack.append(('if', remaining, complete))
            aliases.clear()
        elif word in _OPTIONAL_BLOCK or branch in _IF:
            stack.append((_OPTIONAL_BLOCK.get(word, '#if'), facts, None))
            aliases.clear()
        elif word in _OPTIONAL_END or word in _EXCEPT or branch:
            block = _OPTIONAL_END.get(word, 'try' if word in _EXCEPT else '#if')
            if not stack or stack[-1][0] != block:
                return set()
            facts = stack[-1][1]
            if word in _OPTIONAL_END or branch in _ENDIF:
                stack.pop()
            aliases.clear()
        elif i == start or before in _STATEMENT_BOUNDARIES or before.lstrip().startswith('#'):
            if word in _EXIT:
                exiting = True  # В выражении Возврат ещё могут быть вызовы.
            elif word in _EXECUTE:
                aliases.clear()
            elif word in _VAR or (after == '=' and word.isidentifier()):
                end = next((j for j in range(i + 1, len(words)) if words[j] == ';'), len(words))
                if word in _VAR:
                    roots.difference_update(words[i + 1:end])
                else:
                    roots.discard(word)
                    aliases.pop(word, None)
                    if word not in locals_ and (obj := _metadata_lookup(words[i + 2:end], roots, aliases)):
                        aliases[word] = obj
        if before in {'(', ','} and after in {')', ','}:
            aliases.pop(word, None)  # Аргумент мог измениться по ссылке.
    return guards


def _sites(body, first_line, params, bindings, context, preprocessor, objects):
    """Вызовы с позицией в тексте, получателем и доказанным типом переменной."""
    tokens = [m for m in _BSL_TOKEN.finditer(body) if not m[0].startswith('//')]
    words = [m[0].casefold() for m in tokens]
    stack, pairs = [], {}
    has_split = False
    for i, word in enumerate(words):
        if word == '(':
            stack.append(i)
        elif word == ')' and stack:
            pairs[stack.pop()] = i
        elif word == '.' and i + 1 < len(words) and words[i + 1].lstrip().startswith('#'):
            has_split = True
    split = _split_receivers(words) if has_split else {}
    split_dots = set(split.values()) - {None}
    # В том числе многострочная сигнатура и скобки в строках по умолчанию.
    start = 0
    if params is not None:
        signature = words.index('(') if '(' in words else -1
        start = pairs[signature] + 1 if signature in pairs else len(words)
        if start < len(words) and words[start] in {'экспорт', 'export'}:
            start += 1
    newlines = [m.start() for m in re.finditer('\n', body)]
    types = {}
    parameters = _parse_param_names(params or '')
    locals_ = parameters | bindings
    guards = None
    assignments = {}
    execute_pending = False
    dot_calls = {}
    procedure_targets = _ALL_TARGETS if context is None else _CLIENT if context else _ALL_TARGETS ^ _CLIENT
    for i, word in enumerate(words):
        if word.lstrip().startswith('#') and (condition := _CONDITIONAL.match(word)):
            preprocessor.switch(*condition.groups())
        if i < start or i + 1 == len(words):
            continue
        after = words[i + 1]
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
        if i in split_dots:
            j = i - 1
            while j >= 2 and words[j - 1] == '.' and words[j - 2].isidentifier():
                j -= 2
            if j >= 0 and words[j].isidentifier() and (not j or words[j - 1] != '.'):
                dot_calls[i] = (words[j:i:2], types.get(words[j], '' if words[j] in locals_ else None))
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
        if after != '(' or (before != '.' and i not in split and (word in _KEYWORDS or before in _NEW)):
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
        ambiguous = i in split and split[i] not in dot_calls
        if i in split and not ambiguous:
            receiver, binding = dot_calls[split[i]]
        end = tokens[pairs.get(i + 1, i + 1)].end()
        snippet = ' '.join(body[offset:min(end, offset + 160)].split())
        if offset + 160 < end:
            snippet += '…'
        targets = preprocessor.targets & procedure_targets
        optional_object = False
        if objects is not None and binding is None and len(receiver) == 2 and receiver[0] in COLLECTION_KINDS:
            kind = COLLECTION_KINDS[receiver[0]]
            if kind in MANAGER_OBJECT_KINDS and f'{kind}.{receiver[1]}' not in objects:
                if guards is None:
                    guards = _metadata_guards(words, start, parameters | bindings)
                optional_object = i in guards
        yield _Call(first_line + bisect_right(newlines, tokens[i].start()), snippet, word,
                    receiver, binding, bool(targets) and not targets & ~_CLIENT, ambiguous, optional_object)


@lru_cache(maxsize=128)
def _server_only(availability):
    contexts = {context.strip().casefold() for context in availability.split(',')}
    return contexts <= _SERVER_CONTEXTS


def _restrictions(entries, mode, client, subject):
    reasons = []
    if mode and entries:
        warnings = [compatibility_warning(min_mode, mode) for min_mode, _ in entries]
        if all(warnings):
            reasons.append(subject + ' ' + warnings[0])
    if client and entries and all(_server_only(a) for _, a in entries):
        if all(a.strip().casefold() in {'сервер', 'server'} for _, a in entries):
            reasons.append(f'{subject} доступен только на сервере, а вызов выполняется на клиенте')
        else:
            reasons.append(f'{subject} недоступен на клиенте, а вызов выполняется на клиенте')
    return reasons


def _diagnosis(call, module, declarations, globals_, objects, vocab, mode):
    if call.split_receiver:
        return 'Не проверено — препроцессор разделяет получателя и имя метода'
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
    elif len(receiver) == 2 and receiver[0] in COLLECTION_KINDS and call.binding is None:
        kind = COLLECTION_KINDS[receiver[0]]
        obj = f'{kind}.{receiver[1]}'
        manager = f'{obj}.модульменеджера'
        if manager in declarations:
            procedures = declarations[manager]
            if name in procedures:
                return '' if procedures[name] else 'Ошибка — метод менеджера без «Экспорт»'
        elif kind in MANAGER_OBJECT_KINDS and objects is not None and obj not in objects:
            missing = f'объект метаданных {obj} отсутствует в выгрузке'
            if call.optional_object:
                return f'Предупреждение — {missing} (необязательная зависимость)'
            return f'Ошибка — {missing}'
        owner = COLLECTION_MANAGER_TYPES[receiver[0]][0]
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
_REPORT_JOBS = {}
_REPORT_ERRORS = WeakKeyDictionary()
_CLOSING_DATABASES = WeakSet()
_RESTORE_LOCKS = WeakValueDictionary()
_PUBLISH_LOCKS = WeakValueDictionary()
_MAX_REPORTS = 3
_MAX_ARCHIVES = 3
_CACHE_ARCHIVE = re.compile(r'platform-check(?:-[0-9a-f]{64})?\.zip')


class ReportUnavailable(ValueError):
    """Снимок пока нельзя выдать. Текст исключения предназначен пользователю."""


def _archive_path(db_path, key):
    return Path(db_path).with_name(f'platform-check-{key}.zip')


def _publish_lock(directory):
    directory = directory.resolve()
    with _REPORTS_LOCK:
        return _PUBLISH_LOCKS.setdefault(directory, Lock())


def _unlink_archive(path):
    """Удалить архив; занятый (на Windows его может держать читатель) оставить
    до следующего прохода, не прерывая очистку остальных."""
    try:
        retry_on_permission_error(lambda: path.unlink(missing_ok=True))
    except OSError:
        logger.info('Архив проверки платформы занят и не удалён: %s', path.name)


def prune_report_archives(db_path, keep=()):
    """Удалить архивы предыдущего индекса и общий ZIP старого формата."""
    directory = Path(db_path).parent
    keep = {_archive_path(db_path, key).name for key in keep}
    with _publish_lock(directory):
        for path in directory.glob('platform-check*.zip'):
            if _CACHE_ARCHIVE.fullmatch(path.name) and path.name not in keep:
                _unlink_archive(path)


def _trim_archives(published):
    """После публикации оставить только _MAX_ARCHIVES самых свежих архивов индекса.

    Каждая смена справки (в том числе повторная загрузка той же версии) даёт
    новый ключ и новый архив; без этого их число росло бы до следующего reindex.
    Свежие по времени записи сохраняются, чтобы откат на предыдущую справку не
    требовал полной сборки. Вызывается под блокировкой публикации."""
    archives = []
    for path in published.parent.glob('platform-check*.zip'):
        if path != published and _CACHE_ARCHIVE.fullmatch(path.name):
            try:
                archives.append((path.stat().st_mtime_ns, path))
            except OSError:
                pass
    archives.sort(reverse=True)
    for _, path in archives[_MAX_ARCHIVES - 1:]:
        _unlink_archive(path)


def wait_report_jobs(databases):
    """Не запускать новых задач для закрываемых индексов и дождаться текущих."""
    databases = set(databases)
    with _REPORTS_LOCK:
        _CLOSING_DATABASES.update(databases)
        jobs = [job for (db, _), job in _REPORT_JOBS.items() if db in databases]
    for job in jobs:
        job.join()


class _Report:
    """Один снимок проекта. Источники занимают непрерывные диапазоны модулей."""

    def __init__(self):
        self.lock = Lock()
        self.archive_stamp = None
        self.selections = {}

    def _load_modules(self, conn):
        self.modules = conn.execute(
            """SELECT m.id, m.name_cf, m.file_path, m.source_id, m.object_id,
                      m.module_type, o.kind FROM modules m
               LEFT JOIN metadata_objects o ON o.id=m.object_id
               WHERE m.file_path != '' ORDER BY m.source_id, m.file_path, m.id""").fetchall()
        self.sources = {}
        start = 0
        for sid, rows in groupby(self.modules, key=lambda row: row['source_id']):
            end = start + sum(1 for _ in rows)
            self.sources[sid] = (start, end)
            start = end

    def _module_findings(self, mod, procedures, bindings, form_attrs, diagnosis, objects, source):
        where = f"@{mod['source_id']} {mod['file_path']}"
        if source is None:
            yield 'unchecked', f'{where} — Не проверено — нет исходного текста'
            return
        lines = without_deletions(source).splitlines(keepends=True)
        header_end = procedures[0]['start_line'] - 1 if procedures else len(lines)
        for line in lines[:header_end]:
            if match := _RE_MODULE_VAR_DECL.match(line.lstrip()):
                bindings.update(part.split()[0].casefold() for part in match[1].split(',') if part.strip())
        preprocessor = _Preprocessor()
        for first, body, params, directive, context in _regions(lines, procedures):
            visible = bindings | _form_context_vars(directive, form_attrs)
            for call in _sites(body, first, params, visible, context, preprocessor, objects):
                message = diagnosis(call, mod['name_cf'])
                if message:
                    level = _LEVEL_BY_LABEL[message.partition(' — ')[0]]
                    yield level, f'{where}:{call.line} — {message}\n  {call.text}'

    def _build(self, engine, conn, index, mode):
        self._load_modules(conn)
        vocab = _Vocabulary(index)
        declarations = defaultdict(dict)
        globals_ = set()
        objects = indexed_manager_objects(conn)
        for module, name, is_export, is_global in conn.execute(
                """SELECT m.name_cf, p.name_cf, p.is_export, m.is_global
                   FROM modules m LEFT JOIN procedures p ON p.module_id=m.id"""):
            procedures = declarations[module]
            if name:
                procedures[name] = procedures.get(name, False) or bool(is_export)
            if is_global and is_export:
                globals_.add(name)

        def diagnosis(call, module):
            return _diagnosis(call, module, declarations, globals_, objects, vocab, mode)

        self.blocks = []
        self.positions = {level: array('I') for level in LEVELS}
        self.selections.clear()
        self.starts = []
        self.count = 0
        block = []
        for mod in self.modules:
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
            self.starts.append(self.count)
            source = engine.db.read_module_text(mod['id'])
            for level, msg in self._module_findings(mod, procedures, bindings, form_attrs,
                                                    diagnosis, objects, source):
                self.positions[level].append(self.count)
                self.count += 1
                block.append(msg)
                if len(block) == _BLOCK_SIZE:
                    self._pack(block)
        if block:
            self._pack(block)

    def _pack(self, block):
        self.blocks.append(zlib.compress(json.dumps(block, ensure_ascii=False).encode(), level=1))
        block.clear()

    def restore(self, path, key, conn):
        try:
            with path.open('rb') as stream, ZipFile(stream) as archive:
                saved_key, count, starts = json.loads(archive.read('report.json'))
                if saved_key != key:
                    return
                self._load_modules(conn)
                if len(starts) != len(self.modules):
                    return
                positions = {level: array('I', archive.read(level)) for level in LEVELS}
                if sum(map(len, positions.values())) != count:
                    return
                blocks = [archive.read(f'blocks/{i}') for i in range((count + _BLOCK_SIZE - 1) // _BLOCK_SIZE)]
                stamp = _stat_signature(os.fstat(stream.fileno()))
            self.count, self.starts, self.positions, self.blocks = count, starts, positions, blocks
            self.archive_stamp = stamp
        except FileNotFoundError:
            pass
        except (OSError, BadZipFile, KeyError, ValueError, TypeError, RuntimeError, zlib.error):
            logger.warning('Не удалось прочитать кэш диагностики %s', path, exc_info=True)

    def save(self, path, key, before_replace):
        try:
            # Соседний временный каталог: замена целого архива атомарна
            # и при одновременной записи из разных процессов.
            with TemporaryDirectory(dir=path.parent) as tmp_dir:
                temp = Path(tmp_dir) / path.name
                with ZipFile(temp, 'w') as archive:
                    header = json.dumps([key, self.count, self.starts], ensure_ascii=False)
                    archive.writestr('report.json', header.encode(), compress_type=ZIP_DEFLATED, compresslevel=1)
                    for level, positions in self.positions.items():
                        archive.writestr(level, positions.tobytes(), compress_type=ZIP_DEFLATED, compresslevel=1)
                    for i, block in enumerate(self.blocks):
                        archive.writestr(f'blocks/{i}', block)
                # Очистка при reindex ждёт замену файлов прошлого индекса.
                with _publish_lock(path.parent):
                    before_replace()
                    replace_with_retry(temp, path)
                    self.archive_stamp = _stat_signature(path.stat())
                    _trim_archives(path)
        except OSError:
            logger.warning('Не удалось сохранить кэш диагностики %s', path, exc_info=True)
            return False
        return True

    def has_archive(self, path):
        try:
            return self.archive_stamp is not None and self.archive_stamp == _stat_signature(path.stat())
        except OSError:
            return False

    def _select(self, levels):
        if levels is None or len(levels) == len(LEVELS):
            return range(self.count)
        if len(levels) == 1:
            return self.positions[next(iter(levels))]
        if levels not in self.selections:
            self.selections[levels] = array('I', merge(*(self.positions[level] for level in levels)))
        return self.selections[levels]

    def _page_selection(self, source_id, limit, offset, levels):
        lo, hi = (0, len(self.modules)) if source_id is None else self.sources.get(source_id, (0, 0))
        lower = self.starts[lo] if lo < len(self.starts) else self.count
        upper = self.starts[hi] if hi < len(self.starts) else self.count
        selection = self._select(levels)
        first = bisect_left(selection, lower)
        stop = bisect_left(selection, upper, lo=first)
        return lo, hi, lower, upper, selection[first + offset:min(first + offset + limit, stop)], stop - first

    def page(self, source_id, limit, offset, levels):
        with self.lock:
            lo, hi, lower, upper, positions, count = self._page_selection(source_id, limit, offset, levels)
        items, prev = [], -1
        for pos in positions:
            block, index = divmod(pos, _BLOCK_SIZE)
            if block != prev:
                values = json.loads(zlib.decompress(self.blocks[block]))
                prev = block
            items.append(values[index])
        counts = {level: bisect_left(rows, upper) - bisect_left(rows, lower)
                  for level, rows in self.positions.items()}
        return items, offset + limit < count, counts, hi - lo


@cache
def _report_fingerprint():
    report = bundled_fingerprints().get('report')
    if report is None:
        code = Path(__file__).read_text(encoding='utf-8').replace('\r\n', '\n')
        report = hashlib.sha256(code.encode()).hexdigest()
    return report, parser_fingerprint(), help_fingerprint()


def _file_identity(path):
    stat = Path(path).stat()
    return stat.st_dev, stat.st_ino


def _stat_signature(stat):
    return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size


@lru_cache(maxsize=32)
def _help_revision(path, stamp):
    meta = read_meta(path)
    return stamp, meta.get('schema', ''), meta.get('built_at', '')


def _cache_key(engine, index, mode, revision):
    parts = (_report_fingerprint(), sys.byteorder, _file_identity(engine.db.db_path), revision,
             index.version,
             _help_revision(index.path, _stat_signature(index.path.stat())), mode)
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def _index_revision(conn):
    state = conn.execute("SELECT updated_at, status FROM index_state WHERE id=1").fetchone()
    return tuple(state) if state else ('', 'empty')


def check_index(conn, project_id):
    revision = _index_revision(conn)
    if revision[1] == 'indexing':
        raise ReportUnavailable('Дождитесь окончания переиндексации и повторите проверку.')
    if index_uses_old_parser(conn):
        raise ReportUnavailable(old_index_message(project_id))
    return revision


def old_index_message(project_id):
    return (f'Индекс проекта «{project_id}» построен старой версией Ариадны. '
            'Переиндексируйте проект целиком (reindex).')


def _remember(cache_id, report):
    _REPORTS[cache_id] = report
    _REPORTS.move_to_end(cache_id)
    if len(_REPORTS) > _MAX_REPORTS:
        _REPORTS.popitem(last=False)


def _known_report(cache_id, *, warm):
    report = _REPORTS.get(cache_id)
    if cache_id in _REPORT_JOBS and (warm or report is None):
        raise ReportUnavailable('Отчёт уже готовится. Повторите вызов через минуту-две.')
    if report is not None:
        _REPORTS.move_to_end(cache_id)
        return report
    db, key = cache_id
    errors = _REPORT_ERRORS.get(db, {})
    if key in errors:
        error = errors[key] if warm else errors.pop(key)
        raise ReportUnavailable(f'Error: не удалось подготовить отчёт проверки платформы. {error}')


def _check_revision(engine, conn, index, mode, key, project_id):
    revision = check_index(conn, project_id)
    if engine.db.file_replaced() or key != _cache_key(engine, index, mode, revision):
        raise ReportUnavailable('Индекс или справка изменились. Повторите проверку.')


def _request_report(engine, index, mode, revision, project_id, *, warm=False, invalid=None):
    db = engine.db
    conn = db.read_conn()
    key = _cache_key(engine, index, mode, revision)
    cache_id = db, key
    path = _archive_path(db.db_path, key)
    with _REPORTS_LOCK:
        report = _known_report(cache_id, warm=warm)
        if not warm and report is not None and report is not invalid:
            return key, report
        restore_lock = _RESTORE_LOCKS.setdefault(cache_id, Lock())

    def prepare(report):
        # Рабочий поток отчёта живёт дольше HTTP/MCP-вызова, который его создал.
        # Удерживаем Database и читателя до конца запросов; пропускаем сжатие
        # пула, так как close_all() ждёт эти задачи под блокировкой пула.
        try:
            with db.read_lease(trim_pool=False):
                prepare_with_reader(report)
        finally:
            try:
                db.close_thread_reader()
            finally:
                with _REPORTS_LOCK:
                    del _REPORT_JOBS[cache_id]

    def prepare_with_reader(report):
        try:
            reader = db.read_conn()

            def verify():
                _check_revision(engine, reader, index, mode, key, project_id)

            verify()
            if report is None:
                report = _Report()
                report._build(engine, reader, index, mode)
            if revision[1] == 'ready':
                report.save(path, key, before_replace=verify)
            verify()
            with _REPORTS_LOCK:
                _remember(cache_id, report)
        except IndexRemovedError:
            logger.info('Проект %s: индекс закрыт во время подготовки отчёта', project_id)
        except (ReportUnavailable, FileNotFoundError) as exc:
            logger.info('Проект %s: подготовка отчёта прервана (%s)', project_id, exc)
        except Exception as exc:
            logger.exception('Проект %s: снимок проверки платформы не построен', project_id)
            with _REPORTS_LOCK:
                _REPORT_ERRORS.setdefault(db, {})[key] = str(exc) or type(exc).__name__

    with restore_lock:
        with _REPORTS_LOCK:
            if invalid is not None and _REPORTS.get(cache_id) is invalid:
                del _REPORTS[cache_id]
            report = _known_report(cache_id, warm=warm)
        if report is not None:
            if not warm or report.has_archive(path):
                return key, report
        elif invalid is None and revision[1] == 'ready':
            restored = _Report()
            restored.restore(path, key, conn)
            if restored.archive_stamp is not None:
                with _REPORTS_LOCK:
                    _remember(cache_id, restored)
                return key, restored
        with _REPORTS_LOCK:
            if db in _CLOSING_DATABASES:
                raise ReportUnavailable('Индекс закрывается. Повторите проверку.')
            thread = Thread(target=prepare, args=(report,), daemon=True, name=f'platform-check-{project_id}')
            _REPORT_JOBS[cache_id] = thread
            try:
                thread.start()
            except RuntimeError:
                del _REPORT_JOBS[cache_id]
                raise
    raise ReportUnavailable('Отчёт готовится. Повторите вызов через минуту-две.')


def warm_report(engine, index, mode, ready_at, warmed_key=None):
    """Подготовить снимок для состояния, которое reindex опубликует последним."""
    conn = engine.db.read_conn()
    if index_uses_old_parser(conn) or not conn.execute(
            "SELECT 1 FROM modules WHERE file_path != '' LIMIT 1").fetchone():
        return
    revision = _index_revision(conn)
    key = _cache_key(engine, index, mode, (ready_at, 'ready'))
    if key == warmed_key:
        return key
    report = _Report()
    report._build(engine, conn, index, mode)

    def verify():
        if (engine.db.file_replaced() or revision != _index_revision(conn)
                or key != _cache_key(engine, index, mode, (ready_at, 'ready'))):
            raise ReportUnavailable('Индекс или справка изменились. Повторите проверку.')

    if report.save(_archive_path(engine.db.db_path, key), key, before_replace=verify):
        return key


def warm_ready_report(engine, index, mode, project_id, warmed_key=None):
    conn = engine.db.read_conn()
    try:
        revision = check_index(conn, project_id)
        if revision[1] == 'ready' and conn.execute(
                "SELECT 1 FROM modules WHERE file_path != '' LIMIT 1").fetchone():
            if warmed_key is not None and warmed_key == _cache_key(engine, index, mode, revision):
                return
            _request_report(engine, index, mode, revision, project_id, warm=True)
    except ReportUnavailable:
        pass  # Уже готовится либо сообщение будет выдано при запросе отчёта.


def check_calls(engine, index, mode, source_id, limit, offset, levels, project_id, revision):
    """Страница замечаний и счётчики всего снимка в устойчивом порядке исходников."""
    key, report = _request_report(engine, index, mode, revision, project_id)
    try:
        page = report.page(source_id, limit, offset, levels)
    except (IndexError, ValueError, zlib.error):
        key, report = _request_report(engine, index, mode, revision, project_id, invalid=report)
        page = report.page(source_id, limit, offset, levels)
    conn = engine.db.read_conn()
    _check_revision(engine, conn, index, mode, key, project_id)
    return page
