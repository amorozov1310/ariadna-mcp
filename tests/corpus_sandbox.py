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
assert_data_unchanged проверяют это после каждого теста.
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


def data_snapshot(real_data_dir: Path) -> dict:
    """То, что медленный тест мог бы изменить в data/: projects.json (mtime и
    размер), файлы индексов в data/projects/*/ и report.txt источников."""
    registry = real_data_dir / 'projects.json'
    st = registry.stat() if registry.exists() else None
    projects = real_data_dir / 'projects'
    return {
        'projects.json': (st.st_mtime_ns, st.st_size) if st else None,
        'index files': sorted(str(p) for p in projects.glob('*/index.db*')),
        'reports': sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size)
                          for p in projects.glob('*/sources/*/report.txt')),
    }


def assert_data_unchanged(real_data_dir: Path, before: dict) -> None:
    after = data_snapshot(real_data_dir)
    assert after == before, f"тест изменил data/: было {before}, стало {after}"
