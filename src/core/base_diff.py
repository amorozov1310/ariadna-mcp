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
    """{объект: {реквизит: тип}}, {объект: {форма}} — одним запросом каждое.
    Реквизиты — с табличными частями, их реквизитами и значениями перечислений."""
    attrs: dict[str, dict[str, str]] = defaultdict(dict)
    for r in conn.execute(
            """SELECT o.full_name, a.name, a.type_desc FROM attributes a
               JOIN metadata_objects o ON o.id = a.object_id
               WHERE o.source_id = ?""", (source_id,)):
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
            was, now = _procedures(base_conn, bm[name]['id']), _procedures(current_conn, cm[name]['id'])
            both = was.keys() & now.keys()
            change = ModuleChange(
                name, _object_of(name), 'changed',
                added=sorted(now[k]['name'] for k in now.keys() - was.keys()),
                removed=sorted(was[k]['name'] for k in was.keys() - now.keys()),
                changed=sorted(now[k]['name'] for k in both
                               if (now[k]['content_hash'] or '') != (was[k]['content_hash'] or '')
                               or now[k]['signature'] != was[k]['signature']),
                signature_changed=sorted(now[k]['name'] for k in both
                                         if now[k]['signature'] != was[k]['signature']))
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


# ---------------------------------------------------------------- конфликты обновления

# Виды пересечений доработок проекта с изменениями нового релиза — в порядке
# важности (так и выводятся).
CONFLICT_KINDS = {
    'both_changed': 'правили и вы, и релиз',
    'removed_in_release': 'вы правили — в релизе удалено',
    'name_clash': 'вы добавили — в релизе появилось с тем же именем',
    'removed_by_you': 'вы удалили — релиз изменил',
    'object_both': 'реквизиты объекта меняли и вы, и релиз',
    'module_both': 'модуль меняли и вы, и релиз, но разные процедуры',
}


@dataclass
class Conflict:
    kind: str                       # ключ CONFLICT_KINDS
    object_name: str
    target: str                     # модуль, «модуль.процедура», реквизит или объект
    module: str = ''                # для процедур — модуль (для diff текста)
    procedure: str = ''
    note: str = ''

    @property
    def title(self) -> str:
        return CONFLICT_KINDS[self.kind]


@dataclass
class UpdateConflicts:
    base_project: str
    release_project: str
    base_version: str = ''
    release_version: str = ''
    items: list[Conflict] = field(default_factory=list)
    release_totals: dict = field(default_factory=dict)

    def by_kind(self) -> dict[str, int]:
        counts = {k: 0 for k in CONFLICT_KINDS}
        for c in self.items:
            counts[c.kind] += 1
        return counts


def compute_update_conflicts(ours: BaseDiff, release: BaseDiff, release_project: str) -> UpdateConflicts:
    """Пересечение доработок проекта (ours: проект против базы) с изменениями
    релиза (release: новая типовая против той же базы) — места, где при
    обновлении будет ручное слияние. Ни один из индексов здесь не читается."""
    result = UpdateConflicts(ours.base_project, release_project, ours.base_version,
                             release.current_version, release_totals=release.totals())
    items = result.items
    release_modules = {m.name: m for m in release.modules()}

    for m in ours.modules():
        rel = release_modules.get(m.name)
        if rel is None:
            continue
        obj = m.object_name or m.name
        if m.status == 'added':
            if rel.status == 'added':
                items.append(Conflict('name_clash', obj, m.name, note='модуль'))
            continue
        if m.status == 'removed':
            if rel.status == 'changed':
                items.append(Conflict('removed_by_you', obj, m.name, note='модуль'))
            continue
        if rel.status == 'removed':
            items.append(Conflict('removed_in_release', obj, m.name,
                                  note=f'модуль; ваших правок процедур: {len(m.changed) + len(m.added)}'))
            continue
        found = False
        for proc in m.changed:
            if proc in rel.changed:
                note = 'в релизе изменена сигнатура' if proc in rel.signature_changed else ''
                items.append(Conflict('both_changed', obj, f'{m.name}.{proc}', m.name, proc, note))
                found = True
            elif proc in rel.removed:
                items.append(Conflict('removed_in_release', obj, f'{m.name}.{proc}', m.name, proc))
                found = True
        for proc in m.added:
            if proc in rel.added:
                items.append(Conflict('name_clash', obj, f'{m.name}.{proc}', m.name, proc))
                found = True
        for proc in m.removed:
            if proc in rel.changed:
                items.append(Conflict('removed_by_you', obj, f'{m.name}.{proc}', m.name, proc))
                found = True
        if not found:
            items.append(Conflict('module_both', obj, m.name,
                                  note=f'ваших процедур: {len(m.changed) + len(m.added)}, '
                                       f'процедур релиза: {len(rel.changed) + len(rel.added) + len(rel.removed)}'))

    for o in ours.objects.values():
        rel = release.objects.get(o.full_name)
        if rel is None:
            continue
        if o.status == 'added':
            if rel.status == 'added':
                items.append(Conflict('name_clash', o.full_name, o.full_name, note='объект'))
            continue
        if not o.metadata_changed:
            continue
        if rel.status == 'removed':
            items.append(Conflict('removed_in_release', o.full_name, o.full_name, note='объект'))
            continue
        if not rel.metadata_changed:
            continue
        rel_added = {n for n, _ in rel.attrs_added}
        rel_touched = {n for n, _, _ in rel.attrs_retyped} | {n for n, _ in rel.attrs_removed}
        clash = [n for n, _ in o.attrs_added if n in rel_added]
        both = [n for n, _, _ in o.attrs_retyped if n in rel_touched]
        for n in clash:
            items.append(Conflict('name_clash', o.full_name, f'{o.full_name}.{n}', note='реквизит'))
        for n in both:
            items.append(Conflict('both_changed', o.full_name, f'{o.full_name}.{n}', note='тип реквизита'))
        if not clash and not both:
            items.append(Conflict('object_both', o.full_name, o.full_name,
                                  note=f'ваших реквизитов: {len(o.attrs_added) + len(o.attrs_retyped)}, '
                                       f'реквизитов релиза: {len(rel.attrs_added) + len(rel.attrs_retyped) + len(rel.attrs_removed)}'))

    order = list(CONFLICT_KINDS)
    items.sort(key=lambda c: (order.index(c.kind), c.target))
    return result


# ---------------------------------------------------------------- текст процедуры

def procedure_text_diff(base_lines: list[str] | None, current_lines: list[str] | None,
                        base_label: str, current_label: str, context: int = 3) -> str:
    """Унифицированный diff текста процедуры: база → текущая. None — процедуры
    (или файла) с этой стороны нет."""
    return '\n'.join(difflib.unified_diff(
        base_lines or [], current_lines or [], fromfile=base_label, tofile=current_label,
        n=context, lineterm=''))
