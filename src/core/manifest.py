"""
Сведения для эксплуатации и внешних клиентов (docs/operations.md):
готовность проектов и манифест индекса.

- readiness(pm) — готовы ли проекты отвечать на запросы. Только реестр и
  строка index_state: дёшево, годится для частого опроса оркестратором
  (/ready). /health — по-прежнему «процесс жив», без обращения к индексам.
- project_manifest(pm, id) — из чего и чем построен индекс проекта:
  источники с версиями конфигураций, временем индексации, отпечатком
  парсера, покрытием поисковыми карточками; версия Ариадны, текущие отпечаток
  парсера и формат карточек. По нему интегратор решает, свежий ли индекс и
  не пора ли его пересобрать, не читая логи.

Числа без COUNT(*) по крупным таблицам (calls, procedures): итоги — из
реестра, по источникам — модули (индекс по source_id) и карточки.
"""

from .. import __version__
from . import parser_fingerprint
from .cards import CARD_FORMAT
from .project_manager import PROJECT_GONE, ProjectManager
from .xml_metadata import has_configuration

MANIFEST_SCHEMA_VERSION = '1'


def _progress(pm: ProjectManager, project_id: str) -> dict:
    try:
        return pm.get_db(project_id).get_index_progress()
    except PROJECT_GONE:
        raise
    except Exception:
        return {}


def project_readiness(pm: ProjectManager, project_id: str) -> dict:
    """{id, status, ready, reason[, progress]} одного проекта. KeyError —
    проекта нет."""
    project = pm.get_project(project_id)
    item = {'id': project.id, 'status': project.status, 'ready': project.status == 'ready'}
    if project.status == 'ready':
        item['reason'] = ''
    elif project.status == 'indexing':
        item['reason'] = 'идёт переиндексация'
        progress = _progress(pm, project_id)
        if progress:
            item['progress'] = {k: progress.get(k) for k in
                                ('progress_phase', 'progress_current', 'progress_total')}
    elif project.status == 'error':
        item['reason'] = 'последняя переиндексация завершилась ошибкой'
    elif not project.sources:
        item['reason'] = 'нет источников'
    else:
        item['reason'] = 'проект не проиндексирован'
    return item


def readiness(pm: ProjectManager) -> dict:
    """Готовность всех проектов. ready — все готовы; пустой реестр готов
    (отвечать на запросы нечему, но и ждать нечего)."""
    projects = []
    for project in pm.list_projects():
        try:
            projects.append(project_readiness(pm, project.id))
        except (KeyError, *PROJECT_GONE):
            continue          # удалён между list_projects и чтением
    return {'ready': all(p['ready'] for p in projects), 'projects': projects}


def _source_rows(conn) -> dict[str, dict]:
    rows = {r['id']: dict(r) for r in conn.execute(
        'SELECT id, config_name, config_version, indexed_at FROM sources')}
    for r in conn.execute('SELECT source_id, COUNT(*) AS n FROM modules GROUP BY source_id'):
        rows.setdefault(r['source_id'], {})['modules'] = r['n']
    # Модули с процедурами и те, чьи карточки построены текущим форматом:
    # их разница — сколько модулей ещё не ищется semantic_search_code.
    for r in conn.execute(
            """SELECT m.source_id, COUNT(*) AS total,
                      SUM(CASE WHEN cm.card_format = ? THEN 1 ELSE 0 END) AS current
               FROM modules m LEFT JOIN card_modules cm ON cm.module_id = m.id
               WHERE EXISTS (SELECT 1 FROM procedures p WHERE p.module_id = m.id)
               GROUP BY m.source_id""", (CARD_FORMAT,)):
        rows.setdefault(r['source_id'], {})['cards'] = {
            'modules_with_code': r['total'], 'modules_current': r['current'] or 0}
    return rows


def project_manifest(pm: ProjectManager, project_id: str) -> dict:
    """Манифест индекса проекта. KeyError — проекта нет."""
    project = pm.get_project(project_id)
    db = pm.get_db(project_id)
    conn = db.read_conn()
    progress = db.get_index_progress()
    parser_states = db.get_parser_states(conn)
    by_source = _source_rows(conn)
    current_fp = parser_fingerprint.parser_fingerprint()

    sources = []
    issues = []
    for src in project.sources:
        row = by_source.get(src.id, {})
        state = parser_states.get(src.id) or {}
        cards = row.get('cards', {'modules_with_code': 0, 'modules_current': 0})
        xml_dir = pm.source_path(project_id, src.xml_path) if src.xml_path else None
        if xml_dir is not None and has_configuration(xml_dir):
            metadata_from = 'xml'
        elif src.report_path:
            metadata_from = 'report'
        else:
            metadata_from = ''
        fingerprint = state.get('fingerprint', '')
        item = {
            'id': src.id,
            'label': src.label,
            'source_type': src.source_type,
            'config_name': row.get('config_name', ''),
            'config_version': row.get('config_version', ''),
            'metadata_from': metadata_from,
            'has_code': bool(src.xml_path),
            'indexed_at': row.get('indexed_at') or src.indexed_at,
            'modules': row.get('modules', 0),
            'parser_fingerprint': fingerprint,
            'parser_current': bool(fingerprint) and fingerprint == current_fp,
            'last_full_reparse_at': state.get('last_full_reparse_at', ''),
            'last_full_reparse_reason': state.get('last_full_reparse_reason', ''),
            'cards': cards,
        }
        sources.append(item)
        if not item['indexed_at']:
            issues.append(f'источник {src.id} не проиндексирован')
            continue
        if src.xml_path and not item['parser_current']:
            issues.append(f'источник {src.id} разобран другой версией парсера — '
                          f'переиндексируйте проект')
        if cards['modules_current'] < cards['modules_with_code']:
            issues.append(f'источник {src.id}: поисковые карточки построены для '
                          f"{cards['modules_current']} из {cards['modules_with_code']} "
                          f'модулей — переиндексируйте проект')

    stats = project.index_stats
    return {
        'schema_version': MANIFEST_SCHEMA_VERSION,
        'ariadna_version': __version__,
        'project_id': project.id,
        'name': project.name,
        'status': project.status,
        'ready': project.status == 'ready',
        'index': {
            'state': progress.get('status', 'empty'),
            'updated_at': progress.get('updated_at', ''),
            'last_indexed': stats.last_indexed,
            'duration_sec': stats.duration_sec,
            'objects': stats.total_objects,
            'modules': stats.total_modules,
            'procedures': stats.total_procedures,
            'calls': stats.total_calls,
        },
        'formats': {
            'parser_fingerprint': current_fp,
            'card_format': CARD_FORMAT,
        },
        'sources': sources,
        'issues': issues,
    }
