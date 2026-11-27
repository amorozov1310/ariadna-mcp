"""
Словарь проекта: слова пользователей → объект, реквизит или процедура
конфигурации. «Пайщики», «долги пайщикам» → РегистрНакопления.X — так поиск
находит то, чего нет ни в имени, ни в синониме объекта.

Записи делает человек в Web UI или агент инструментом add_glossary_term по
ходу работы. Хранится в data/projects/{id}/glossary.json, а не в индексе:
индекс пересоздаётся из выгрузки, словарь — знание людей, оно переживает
переиндексацию и попадает в резервную копию вместе с data/.

Совпадение со словами запроса — по основам слов, как у поиска по карточкам
(cards.query_stems, cards.word_roots): «пайщикам» совпадает с «пайщики».
Термин из нескольких слов совпадает, если в запросе есть каждое из них.
"""

import json
import re
import threading
from weakref import WeakValueDictionary
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .cards import QUERY_STOPWORDS, query_stems, word_roots
from .fsretry import replace_with_retry

FILENAME = 'glossary.json'
TARGET_OBJECT, TARGET_ATTRIBUTE, TARGET_PROCEDURE = 'object', 'attribute', 'procedure'

_WORD = re.compile(r'\w+')
_SPLIT_TERMS = re.compile(r'[,;\n]+')
_MIN_SHARED = 4          # общая часть основ, при которой слова считаются одним
_locks = WeakValueDictionary()
_locks_guard = threading.Lock()


@dataclass
class GlossaryEntry:
    id: str
    target: str                      # полное имя: Справочник.X, Справочник.X.Реквизит, Модуль.Процедура
    target_kind: str                 # object | attribute | procedure
    terms: list[str] = field(default_factory=list)
    note: str = ''
    author: str = ''                 # «агент», «Web UI»
    updated_at: str = ''


def split_terms(text: str | list[str]) -> list[str]:
    """«пайщики, долги пайщикам» → ['пайщики', 'долги пайщикам']; без пустых и повторов."""
    items = text if isinstance(text, list) else _SPLIT_TERMS.split(text or '')
    out: list[str] = []
    for item in items:
        term = ' '.join(str(item).split())
        if term and term.casefold() not in {t.casefold() for t in out}:
            out.append(term)
    return out


def _keys(word: str) -> set[str]:
    """Основы и корни слова для сравнения; слово целиком — если оно короткое
    или не кириллическое (НДС, EAN)."""
    word = word.casefold()
    return set(query_stems(word)) | word_roots(word) | {word}


def _same_word(a: set[str], b: set[str]) -> bool:
    for x in a:
        for y in b:
            short, long = (x, y) if len(x) <= len(y) else (y, x)
            if long.startswith(short) and (len(short) >= _MIN_SHARED or short == long):
                return True
    return False


def _words(text: str) -> list[str]:
    words = [w for w in _WORD.findall(text.casefold()) if len(w) >= 2]
    meaningful = [w for w in words if w not in QUERY_STOPWORDS and len(w) >= 3]
    return meaningful or words


def term_score(term: str, query: str) -> int:
    """Сколько слов термина есть в запросе; 0 — термин не совпал (нужны все)."""
    term_words = _words(term)
    query_keys = [_keys(w) for w in _words(query)]
    if not term_words or not query_keys:
        return 0
    for w in term_words:
        if not any(_same_word(_keys(w), q) for q in query_keys):
            return 0
    return len(term_words)


def _lock(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(path.resolve()), threading.Lock())


class Glossary:
    """Словарь одного проекта (файл glossary.json). Потокобезопасен в пределах
    процесса: Web UI и MCP пишут из разных потоков."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _read(self) -> list[GlossaryEntry]:
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            return []
        entries = []
        for raw in data.get('entries', []) if isinstance(data, dict) else []:
            try:
                entries.append(GlossaryEntry(**{k: raw[k] for k in raw if k in GlossaryEntry.__dataclass_fields__}))
            except TypeError:
                continue
        return entries

    def _write(self, entries: list[GlossaryEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + '.tmp')
        tmp.write_text(json.dumps({'entries': [asdict(e) for e in entries]},
                                  ensure_ascii=False, indent=2), encoding='utf-8')
        replace_with_retry(tmp, self.path)     # Windows: файл занят антивирусом

    def entries(self) -> list[GlossaryEntry]:
        return sorted(self._read(), key=lambda e: e.target.casefold())

    def get(self, entry_id: str) -> GlossaryEntry | None:
        return next((e for e in self._read() if e.id == entry_id), None)

    def for_target(self, target: str) -> GlossaryEntry | None:
        key = target.casefold()
        return next((e for e in self._read() if e.target.casefold() == key), None)

    def save(self, target: str, target_kind: str, terms: list[str], note: str = '',
             author: str = '', merge: bool = True) -> GlossaryEntry:
        """Запись по цели: новая или существующая. merge — добавить термины к
        прежним (агент дописывает по ходу работы); иначе заменить (правка в
        Web UI). Пустое примечание при merge прежнее не стирает."""
        with _lock(self.path):
            entries = self._read()
            entry = next((e for e in entries if e.target.casefold() == target.casefold()), None)
            now = datetime.now().isoformat(timespec='seconds')
            if entry is None:
                entry = GlossaryEntry(id=uuid.uuid4().hex[:8], target=target, target_kind=target_kind)
                entries.append(entry)
            entry.target, entry.target_kind = target, target_kind
            entry.terms = split_terms((entry.terms + terms) if merge else terms)
            if note or not merge:
                entry.note = note
            entry.author, entry.updated_at = author, now
            self._write(entries)
            return entry

    def remove(self, entry_id: str) -> GlossaryEntry | None:
        with _lock(self.path):
            entries = self._read()
            entry = next((e for e in entries if e.id == entry_id), None)
            if entry is not None:
                self._write([e for e in entries if e.id != entry_id])
            return entry

    def match(self, query: str, kinds: tuple[str, ...] | None = None) -> list[tuple[GlossaryEntry, str]]:
        """Записи, чей термин есть в запросе: (запись, совпавший термин), лучшие
        первыми — термин из большего числа слов точнее."""
        found = []
        for entry in self._read():
            if kinds and entry.target_kind not in kinds:
                continue
            best = max(((term_score(t, query), t) for t in entry.terms),
                       key=lambda x: x[0], default=(0, ''))
            if best[0]:
                found.append((best[0], entry, best[1]))
        found.sort(key=lambda x: (-x[0], x[1].target))
        return [(entry, term) for _, entry, term in found]
