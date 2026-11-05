"""Отчёты по сравнению с типовой — файлом: «Что доработано» (BaseDiff) и
«Обновление на новый релиз» (UpdateConflicts) в Markdown или CSV.

Markdown — для чтения и работы по списку (пересечения — чек-лист), CSV — для
Excel: UTF-8 с BOM и разделитель «;», чтобы русская Excel открывала файл
двойным щелчком без мастера импорта. Ни один индекс здесь не читается —
только готовые результаты сравнения.
"""

import csv
import io
from datetime import datetime

from .base_diff import CONFLICT_KINDS, BaseDiff, UpdateConflicts

FORMATS = {'md': 'text/markdown; charset=utf-8', 'csv': 'text/csv; charset=utf-8'}

_OBJECT_STATUS = {'changed': 'доработан', 'added': 'новый', 'removed': 'удалён'}
_MODULE_STATUS = {'changed': 'изменён', 'added': 'новый', 'removed': 'удалён'}


def _csv(header: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=';', lineterminator='\r\n')
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode('utf-8-sig')


def _md_escape(text: str) -> str:
    return str(text).replace('|', '\\|').replace('\n', ' ')


def _stamp() -> str:
    return datetime.now().strftime('%d.%m.%Y %H:%M')


def _version(v: str) -> str:
    return f' ({v})' if v else ''


# ---------------------------------------------------------------- что доработано

def _diff_rows(diff: BaseDiff) -> list[list]:
    """Строка — одно изменение: объект, вид объекта, статус объекта, что
    (модуль/процедура/реквизит/форма/синоним), имя, изменение, подробности."""
    rows = []
    for status in ('changed', 'added', 'removed'):
        for o in diff.by_status(status):
            head = [o.full_name, o.synonym, _OBJECT_STATUS[status]]
            if status != 'changed':
                rows.append(head + ['объект', o.full_name, _OBJECT_STATUS[status], ''])
            if o.synonym_was is not None:
                rows.append(head + ['синоним', o.full_name, 'изменён', f'{o.synonym_was} → {o.synonym}'])
            for name, type_ in o.attrs_added:
                rows.append(head + ['реквизит', name, 'добавлен', type_])
            for name, type_ in o.attrs_removed:
                rows.append(head + ['реквизит', name, 'удалён', type_])
            for name, was, now in o.attrs_retyped:
                rows.append(head + ['реквизит', name, 'изменён тип', f'{was} → {now}'])
            for name in o.forms_added:
                rows.append(head + ['форма', name, 'добавлена', ''])
            for name in o.forms_removed:
                rows.append(head + ['форма', name, 'удалена', ''])
            for m in o.modules:
                if m.status != 'changed':
                    rows.append(head + ['модуль', m.name, _MODULE_STATUS[m.status],
                                        f'процедур: {m.procedures}'])
                    continue
                if not (m.changed or m.added or m.removed):
                    rows.append(head + ['модуль', m.name, 'изменён', 'правка вне процедур'])
                for p in m.changed:
                    rows.append(head + ['процедура', f'{m.name}.{p}', 'изменена',
                                        'изменена сигнатура' if p in m.signature_changed else ''])
                for p in m.added:
                    rows.append(head + ['процедура', f'{m.name}.{p}', 'добавлена', ''])
                for p in m.removed:
                    rows.append(head + ['процедура', f'{m.name}.{p}', 'удалена', ''])
    return rows


def diff_report(project_id: str, diff: BaseDiff, fmt: str) -> bytes:
    if fmt == 'csv':
        return _csv(['Объект', 'Синоним', 'Статус объекта', 'Что', 'Имя', 'Изменение', 'Подробности'],
                    _diff_rows(diff))
    t = diff.totals()
    lines = [
        f'# Что доработано: «{project_id}»{_version(diff.current_version)}',
        '',
        f'База — «{diff.base_project}»{_version(diff.base_version)}. Источник: {diff.source_id}. '
        f'Сформировано {_stamp()}.',
        '',
    ]
    if diff.versions_differ:
        lines += ['> Версии проекта и базы различаются: в отчёт попали и изменения типовой '
                  'между этими релизами.', '']
    lines += [
        '| Показатель | Количество |',
        '|---|---:|',
        f'| Изменено типовых модулей (из {diff.modules_total}) | {t["modules_changed"]} |',
        f'| Изменено типовых процедур (сигнатура — у {t["procedures_signature"]}) | {t["procedures_changed"]} |',
        f'| Новых процедур | {t["procedures_added"] + t["procedures_in_added_modules"]} |',
        f'| Новых модулей / удалено | {t["modules_added"]} / {t["modules_removed"]} |',
        f'| Новых объектов / удалено | {t["objects_added"]} / {t["objects_removed"]} |',
        f'| Новых реквизитов у типовых / удалено / изменён тип | '
        f'{t["attributes_added"]} / {t["attributes_removed"]} / {t["attributes_retyped"]} |',
        '',
    ]
    titles = {'changed': 'Доработанные типовые объекты', 'added': 'Новые объекты',
              'removed': 'Удалённые объекты'}
    for status, title in titles.items():
        objects = diff.by_status(status)
        lines += [f'## {title} ({len(objects)})', '']
        if not objects:
            lines += ['нет', '']
            continue
        for o in objects:
            synonym = f' — {o.synonym}' if o.synonym else ''
            lines.append(f'### {o.full_name}{synonym}')
            lines.append('')
            if o.synonym_was is not None:
                lines.append(f'- синоним: «{o.synonym_was}» → «{o.synonym}»')
            if o.attrs_added:
                lines.append('- реквизиты добавлены: ' + ', '.join(f'{n} ({t_})' for n, t_ in o.attrs_added))
            if o.attrs_removed:
                lines.append('- реквизиты удалены: ' + ', '.join(n for n, _ in o.attrs_removed))
            if o.attrs_retyped:
                lines.append('- изменён тип: ' + ', '.join(f'{n} ({a} → {b})' for n, a, b in o.attrs_retyped))
            if o.forms_added:
                lines.append('- формы добавлены: ' + ', '.join(o.forms_added))
            if o.forms_removed:
                lines.append('- формы удалены: ' + ', '.join(o.forms_removed))
            for m in o.modules:
                if m.status != 'changed':
                    lines.append(f'- модуль `{m.name}` {_MODULE_STATUS[m.status]}, процедур: {m.procedures}')
                    continue
                parts = []
                if m.changed:
                    parts.append('изменены: ' + ', '.join(
                        p + (' (сигнатура)' if p in m.signature_changed else '') for p in m.changed))
                if m.added:
                    parts.append('добавлены: ' + ', '.join(m.added))
                if m.removed:
                    parts.append('удалены: ' + ', '.join(m.removed))
                lines.append(f'- модуль `{m.name}`: ' + ('; '.join(parts) or 'правка вне процедур'))
            lines.append('')
    return '\n'.join(lines).encode('utf-8')


# ---------------------------------------------------------------- обновление

_HINTS = {
    'both_changed': 'слить вручную',
    'removed_in_release': 'найти, куда релиз перенёс код, и перенести правку',
    'intercepted_removed': 'перенести перехват на новое место',
    'intercepted_changed': 'проверить перехват против новой версии процедуры',
    'name_clash': 'переименовать своё — после обновления будет дубль',
    'removed_by_you': 'решить, нужно ли удаление',
    'object_both': 'для сведения, обычно сливается само',
    'module_both': 'для сведения, обычно сливается само',
}


def update_report(project_id: str, result: UpdateConflicts, fmt: str) -> bytes:
    if fmt == 'csv':
        rows = [[i, CONFLICT_KINDS[c.kind], c.object_name, c.target, c.module, c.procedure, c.note,
                 _HINTS[c.kind], '', '']
                for i, c in enumerate(result.items, 1)]
        return _csv(['№', 'Вид', 'Объект', 'Где', 'Модуль', 'Процедура', 'Пометка', 'Что делать',
                     'Решение', 'Ответственный'], rows)
    t = result.release_totals
    counts = result.by_kind()
    lines = [
        f'# Обновление «{project_id}» на «{result.release_project}»{_version(result.release_version)}',
        '',
        f'База — «{result.base_project}»{_version(result.base_version)}. Сформировано {_stamp()}.',
        '',
        f'Релиз изменил модулей {t.get("modules_changed", 0)} (новых {t.get("modules_added", 0)}, '
        f'удалено {t.get("modules_removed", 0)}); процедур изменено {t.get("procedures_changed", 0)}, '
        f'добавлено {t.get("procedures_added", 0)}, удалено {t.get("procedures_removed", 0)}; '
        f'новых объектов {t.get("objects_added", 0)}. Ниже — только места, которые тронуты и '
        f'доработками проекта, и релизом.',
        '',
        '| Вид пересечения | Количество | Что делать |',
        '|---|---:|---|',
    ]
    lines += [f'| {title[:1].upper()}{title[1:]} | {counts[kind]} | {_HINTS[kind]} |'
              for kind, title in CONFLICT_KINDS.items()]
    lines += ['', f'Всего: {len(result.items)}.', '']
    if not result.items:
        lines += ['Доработки проекта не пересекаются с изменениями релиза.', '']
    for kind, title in CONFLICT_KINDS.items():
        items = [c for c in result.items if c.kind == kind]
        if not items:
            continue
        lines += [f'## {title[:1].upper()}{title[1:]} ({len(items)})', '']
        for c in items:
            note = f' — {_md_escape(c.note)}' if c.note else ''
            lines.append(f'- [ ] `{c.target}`{note}')
        lines.append('')
    return '\n'.join(lines).encode('utf-8')


def filename(prefix: str, project_id: str, fmt: str) -> str:
    safe = ''.join(ch if ch.isalnum() or ch in '-_.' else '_' for ch in project_id)
    return f'{prefix}_{safe}_{datetime.now():%Y%m%d}.{fmt}'
