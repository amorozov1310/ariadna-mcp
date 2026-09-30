"""
Песочница над реальным корпусом: медленные тесты читают выгрузки из data/,
но ничего туда не пишут.

Раньше tests/test_slow_corpus.py работал через ProjectManager(data/): индексы
появлялись в data/projects/*/index.db (в Docker они давно на томе
ariadna-index — это гигабайты мусора), статус и статистика писались в общий
data/projects.json (прерванный прогон оставлял проекту status='indexing'), а
report.txt генерировался в каталог реального источника.

sandbox_project_manager строит во временном каталоге собственный
projects.json с теми же проектами и источниками, но с абсолютными путями
report_path/xml_path к реальным каталогам (ProjectManager.source_path берёт
абсолютный путь как есть). Индексы, реестр и сгенерированный report.txt —
во временном каталоге; корпус только читается. data_snapshot/
assert_data_unchanged проверяют это после каждого теста — только то, что
мог изменить сам тест (записи своих проектов, index.db*, их report.txt),
чтобы правки других проектов в Web UI во время теста его не роняли.
"""

import json
from pathlib import Path

from src.core.project_manager import ProjectManager


def sandbox_project_manager(real_data_dir: Path, project_ids: list[str],
                            work_dir: Path) -> ProjectManager:
    """ProjectManager во work_dir с проектами project_ids из реального реестра.

    Реестр real_data_dir/projects.json только читается. Проекта, которого в
    нём нет, в песочнице тоже не будет (get_project → KeyError)."""
    registry = json.loads((real_data_dir / 'projects.json').read_text(encoding='utf-8'))
    data_dir = work_dir / 'data'
    projects = {}
    for pid in project_ids:
        entry = registry.get('projects', {}).get(pid)
        if entry is None:
            continue
        real_project_dir = (real_data_dir / 'projects' / pid).resolve()
        sources = []
        for src in entry.get('sources', []):
            src = dict(src)
            for key in ('report_path', 'xml_path'):
                if src.get(key):
                    src[key] = str(real_project_dir / src[key])
            src['indexed_at'] = ''
            sources.append(src)
        projects[pid] = {**entry, 'status': 'empty', 'index_stats': {}, 'sources': sources}
        (data_dir / 'projects' / pid / 'sources').mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / 'projects.json').write_text(
        json.dumps({'projects': projects}, ensure_ascii=False, indent=2), encoding='utf-8')
    return ProjectManager(str(data_dir), index_dir=str(work_dir / 'index'))


def _registry_entries(real_data_dir: Path, project_ids: list[str]) -> dict:
    registry = real_data_dir / 'projects.json'
    if not registry.exists():
        return {pid: None for pid in project_ids}
    projects = json.loads(registry.read_text(encoding='utf-8')).get('projects', {})
    return {pid: projects.get(pid) for pid in project_ids}


def data_snapshot(real_data_dir: Path, project_ids: list[str]) -> dict:
    """То, что медленный тест мог бы изменить в data/:

    - записи проектов project_ids в projects.json — по содержимому. Не
      mtime и размер всего файла: пока идёт тест, в Web UI могут создать
      или удалить другой проект (контейнер останавливать не нужно), и это
      не повод ронять тест;
    - index.db* в data/projects/*/ — любого проекта;
    - report.txt источников проектов project_ids (mtime и размер)."""
    projects = real_data_dir / 'projects'
    reports = [p for pid in project_ids for p in (projects / pid).glob('sources/*/report.txt')]
    return {
        'registry': _registry_entries(real_data_dir, project_ids),
        'index files': sorted(str(p) for p in projects.glob('*/index.db*')),
        'reports': {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in reports},
    }


def _diff_entry(before, after, path: str) -> list[str]:
    """Изменившиеся поля записи проекта: «status: 'ready' → 'indexing'»."""
    if isinstance(before, dict) and isinstance(after, dict):
        out = []
        for key in sorted(set(before) | set(after), key=str):
            out += _diff_entry(before.get(key), after.get(key), f'{path}.{key}' if path else str(key))
        return out
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        out = []
        for i, (b, a) in enumerate(zip(before, after)):
            label = b.get('id', i) if isinstance(b, dict) else i
            out += _diff_entry(b, a, f'{path}[{label}]')
        return out
    return [] if before == after else [f'{path}: {before!r} → {after!r}']


def assert_data_unchanged(real_data_dir: Path, before: dict) -> None:
    project_ids = list(before['registry'])
    after = data_snapshot(real_data_dir, project_ids)
    problems = []
    for pid in project_ids:
        old, new = before['registry'][pid], after['registry'][pid]
        if old == new:
            continue
        if old is None or new is None:
            problems.append(f"data/projects.json, проект {pid}: запись "
                            f"{'появилась' if old is None else 'удалена'}")
            continue
        for change in _diff_entry(old, new, ''):
            problems.append(f"data/projects.json, проект {pid}, поле {change}")
    appeared = sorted(set(after['index files']) - set(before['index files']))
    if appeared:
        problems.append(f"появились файлы индекса: {', '.join(appeared)}")
    for path in sorted(set(before['reports']) | set(after['reports'])):
        if before['reports'].get(path) != after['reports'].get(path):
            problems.append(f"изменился {path}: {before['reports'].get(path)} → "
                            f"{after['reports'].get(path)}")
    assert not problems, "тест изменил data/:\n  " + '\n  '.join(problems)
