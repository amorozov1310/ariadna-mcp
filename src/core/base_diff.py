"""
Отличия проекта от базы — другого проекта той же конфигурации (обычно
типовой). Сравниваются два готовых индекса, выгрузки для этого не нужны:

  - модули — по хешу файла (modules.file_hash): изменён, добавлен, удалён;
  - процедуры изменённых модулей — по отпечатку карточки
    (card_chunks.content_hash: подпись, описание, тело без номеров строк),
    поэтому сдвиг процедуры вниз из-за вставки выше изменением не считается;
  - объекты метаданных — добавлены, удалены; у общих — реквизиты (с типом)
    и формы.

Только код из выгрузки (только для текста процедуры — get_procedure_diff —
нужны файлы обеих выгрузок). Сравнивается один источник (по умолчанию
основная конфигурация): расширения в типовой не бывает, его код — уже
доработка.
"""

import difflib
from collections import defaultdict
from dataclasses import dataclass, field

from .cards import _object_of


@dataclass
class ModuleChange:
    name: str
    object_name: str
    status: str                     # changed | added | removed
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    signature_changed: list[str] = field(default_factory=list)
    procedures: int = 0             # у нового/удалённого модуля — сколько всего


@dataclass
class ObjectChange:
    full_name: str
    kind: str
    synonym: str = ''
    status: str = 'changed'         # changed | added | removed
    attrs_added: list[tuple[str, str]] = field(default_factory=list)      # (имя, тип)
    attrs_removed: list[tuple[str, str]] = field(default_factory=list)
    attrs_retyped: list[tuple[str, str, str]] = field(default_factory=list)  # (имя, было, стало)
    forms_added: list[str] = field(default_factory=list)
    forms_removed: list[str] = field(default_factory=list)
    synonym_was: str | None = None  # не None — синоним изменён
    modules: list[ModuleChange] = field(default_factory=list)

    @property
    def metadata_changed(self) -> bool:
        return bool(self.attrs_added or self.attrs_removed or self.attrs_retyped
                    or self.forms_added or self.forms_removed or self.synonym_was is not None)

    @property
    def weight(self) -> int:
        """Объём доработки — для сортировки «больше всего изменено — первым»."""
        procs = sum(len(m.added) + len(m.removed) + len(m.changed) + (m.procedures if m.status != 'changed' else 0)
                    for m in self.modules)
        return (len(self.attrs_added) + len(self.attrs_removed) + len(self.attrs_retyped)
                + len(self.forms_added) + len(self.forms_removed) + procs)


@dataclass
class BaseDiff:
    base_project: str
    source_id: str
    base_version: str = ''
    current_version: str = ''
    modules_total: int = 0
    objects: dict[str, ObjectChange] = field(default_factory=dict)

    def by_status(self, status: str) -> list[ObjectChange]:
        """Объекты с данным статусом; изменённые — по объёму доработки."""
        found = [o for o in self.objects.values() if o.status == status]
        if status == 'changed':
            return sorted(found, key=lambda o: (-o.weight, o.full_name))
        return sorted(found, key=lambda o: o.full_name)

    def modules(self, status: str | None = None) -> list[ModuleChange]:
        return [m for o in self.objects.values() for m in o.modules
                if status is None or m.status == status]

    def totals(self) -> dict:
        changed_modules = self.modules('changed')
        changed = self.by_status('changed')
        return {
            'modules_changed': len(changed_modules),
            'modules_added': len(self.modules('added')),
            'modules_removed': len(self.modules('removed')),
            'procedures_changed': sum(len(m.changed) for m in changed_modules),
            'procedures_signature': sum(len(m.signature_changed) for m in changed_modules),
            'procedures_added': sum(len(m.added) for m in changed_modules),
            'procedures_removed': sum(len(m.removed) for m in changed_modules),
            'procedures_in_added_modules': sum(m.procedures for m in self.modules('added')),
            'objects_added': len(self.by_status('added')),
            'objects_removed': len(self.by_status('removed')),
            'objects_changed': len(changed),
            'objects_metadata_changed': sum(1 for o in changed if o.metadata_changed),
            'attributes_added': sum(len(o.attrs_added) for o in changed),
            'attributes_removed': sum(len(o.attrs_removed) for o in changed),
            'attributes_retyped': sum(len(o.attrs_retyped) for o in changed),
            'forms_added': sum(len(o.forms_added) for o in changed),
        }

    def find_object(self, name: str) -> ObjectChange | None:
        """Объект по полному имени или модуль по имени — его объект."""
        key = name.strip().casefold()
        for o in self.objects.values():
            if o.full_name.casefold() == key or any(m.name.casefold() == key for m in o.modules):
                return o
        return None


# ---------------------------------------------------------------- чтение индекса

def _source_version(conn, source_id: str) -> str:
    row = conn.execute("SELECT config_name, config_version FROM sources WHERE id = ?",
                       (source_id,)).fetchone()
    return f"{row['config_name']} {row['config_version']}".strip() if row else ''


def _modules(conn, source_id: str) -> dict[str, dict]:
    """Модули с кодом (file_hash не пуст: строка общего модуля из метаданных
    без файла — не код)."""
    return {r['name']: dict(r) for r in conn.execute(
        "SELECT id, name, file_hash FROM modules WHERE source_id = ? AND file_hash != ''",
        (source_id,))}


def _procedures(conn, module_id: int) -> dict[str, dict]:
    return {r['name_cf']: dict(r) for r in conn.execute(
        """SELECT p.name, p.name_cf, p.signature,
                  (SELECT c.content_hash FROM card_chunks c WHERE c.procedure_id = p.id
                   ORDER BY c.chunk_no LIMIT 1) AS content_hash
           FROM procedures p WHERE p.module_id = ?""", (module_id,))}


def _objects(conn, source_id: str) -> dict[str, dict]:
    return {r['full_name']: dict(r) for r in conn.execute(
        "SELECT full_name, kind, synonym FROM metadata_objects WHERE source_id = ?", (source_id,))}


def _members(conn, source_id: str) -> tuple[dict, dict]:
    """{объект: {реквизит: тип}}, {объект: {форма}} — одним запросом каждое."""
    attrs: dict[str, dict[str, str]] = defaultdict(dict)
    for r in conn.execute(
            """SELECT o.full_name, a.name, a.type_desc FROM attributes a
               JOIN metadata_objects o ON o.id = a.object_id
               WHERE o.source_id = ? AND a.kind != 'ЗначениеПеречисления'""", (source_id,)):
        attrs[r['full_name']][r['name']] = r['type_desc'] or ''
    forms: dict[str, set[str]] = defaultdict(set)
    for r in conn.execute(
            """SELECT o.full_name, f.name FROM forms f
               JOIN metadata_objects o ON o.id = f.object_id WHERE o.source_id = ?""", (source_id,)):
        forms[r['full_name']].add(r['name'])
    return attrs, forms


# ---------------------------------------------------------------- сравнение

def compute_base_diff(current_conn, base_conn, base_project: str,
                      source_id: str = 'main') -> BaseDiff:
    diff = BaseDiff(base_project=base_project, source_id=source_id,
                    base_version=_source_version(base_conn, source_id),
                    current_version=_source_version(current_conn, source_id))

    bo, co = _objects(base_conn, source_id), _objects(current_conn, source_id)

    def entry(full_name: str) -> ObjectChange:
        if full_name not in diff.objects:
            meta = co.get(full_name) or bo.get(full_name) or {}
            status = ('added' if full_name in co and full_name not in bo else
                      'removed' if full_name in bo and full_name not in co else 'changed')
            diff.objects[full_name] = ObjectChange(
                full_name, meta.get('kind') or full_name.split('.', 1)[0],
                meta.get('synonym') or '', status)
        return diff.objects[full_name]

    for name in co.keys() - bo.keys():
        entry(name)
    for name in bo.keys() - co.keys():
        entry(name)

    battrs, bforms = _members(base_conn, source_id)
    cattrs, cforms = _members(current_conn, source_id)
    for name in co.keys() & bo.keys():
        ba, ca = battrs.get(name, {}), cattrs.get(name, {})
        bf, cf = bforms.get(name, set()), cforms.get(name, set())
        added = sorted(ca.keys() - ba.keys())
        removed = sorted(ba.keys() - ca.keys())
        retyped = sorted(n for n in ca.keys() & ba.keys() if ca[n] != ba[n])
        synonym_changed = (co[name]['synonym'] or '') != (bo[name]['synonym'] or '')
        if not (added or removed or retyped or cf != bf or synonym_changed):
            continue
        o = entry(name)
        o.attrs_added = [(n, ca[n]) for n in added]
        o.attrs_removed = [(n, ba[n]) for n in removed]
        o.attrs_retyped = [(n, ba[n], ca[n]) for n in retyped]
        o.forms_added = sorted(cf - bf)
        o.forms_removed = sorted(bf - cf)
        if synonym_changed:
            o.synonym_was = bo[name]['synonym'] or ''

    bm, cm = _modules(base_conn, source_id), _modules(current_conn, source_id)
    diff.modules_total = len(bm)
    for name in sorted(cm.keys() | bm.keys()):
        if name in cm and name in bm:
            if cm[name]['file_hash'] == bm[name]['file_hash']:
                continue
            bp, cp = _procedures(base_conn, bm[name]['id']), _procedures(current_conn, cm[name]['id'])
            both = bp.keys() & cp.keys()
            change = ModuleChange(
                name, _object_of(name), 'changed',
                added=sorted(cp[k]['name'] for k in cp.keys() - bp.keys()),
                removed=sorted(bp[k]['name'] for k in bp.keys() - cp.keys()),
                changed=sorted(cp[k]['name'] for k in both
                               if (cp[k]['content_hash'] or '') != (bp[k]['content_hash'] or '')
                               or cp[k]['signature'] != bp[k]['signature']),
                signature_changed=sorted(cp[k]['name'] for k in both
                                         if cp[k]['signature'] != bp[k]['signature']))
            # Без изменённых процедур модуль всё равно в списке: правка вне
            # процедур (переменные модуля, области, комментарии) — тоже доработка.
        else:
            conn, mod = (current_conn, cm[name]) if name in cm else (base_conn, bm[name])
            count = conn.execute("SELECT COUNT(*) FROM procedures WHERE module_id = ?",
                                 (mod['id'],)).fetchone()[0]
            change = ModuleChange(name, _object_of(name), 'added' if name in cm else 'removed',
                                  procedures=count)
        entry(change.object_name or name).modules.append(change)
    return diff


# ---------------------------------------------------------------- текст процедуры

def procedure_text_diff(base_lines: list[str] | None, current_lines: list[str] | None,
                        base_label: str, current_label: str, context: int = 3) -> str:
    """Унифицированный diff текста процедуры: база → текущая. None — процедуры
    (или файла) с этой стороны нет."""
    return '\n'.join(difflib.unified_diff(
        base_lines or [], current_lines or [], fromfile=base_label, tofile=current_label,
        n=context, lineterm=''))
