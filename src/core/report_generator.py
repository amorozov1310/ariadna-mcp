"""
Этап 5 (вариант A, IMPROVEMENT_PLAN.md): генерация report.txt из XML-
выгрузки конфигурации, когда у источника нет отчёта Конфигуратора.

Обёртка над сторонним генератором norkins/metadata
(https://github.com/norkins/metadata, пакет generate_config_report) —
переходное решение на время, пока не сделан вариант B (прямой парсинг
метаданных из XML в отдельном модуле). Пакет — необязательная зависимость
(extra ``xml-report`` в pyproject.toml); если он не установлен,
авто-генерация просто не выполняется и источник индексируется как раньше
(BSL-граф без метаданных).
"""

import json
import logging
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger('ariadna')

# Обход бага апстрима: опубликованное pip-колесо generate-config-report не
# включает generate_config_report/settings/defaults.json (в его pyproject.toml
# нет package-data для не-.py файлов пакета) — без этого файла load_settings()
# падает с FileNotFoundError на любом вызове, даже если передан свой
# generatorSettingsPath (он всё равно сначала читает DEFAULT_SETTINGS_PATH
# как базу для деслияния). Держим свою копию файла и докладываем её в
# ожидаемое место при первом использовании, если её там нет.
_VENDORED_DEFAULT_SETTINGS = Path(__file__).with_name('vendor') / 'generate_config_report_defaults.json'


def is_available() -> bool:
    """Whether the generate_config_report package (pip install .[xml-report]) is installed."""
    try:
        import generate_config_report  # noqa: F401
    except ImportError:
        return False
    return True


def _ensure_default_settings_installed() -> bool:
    """Make sure DEFAULT_SETTINGS_PATH exists, healing the missing-file bug
    above from our vendored copy. Returns False if neither is available."""
    from generate_config_report.settings import DEFAULT_SETTINGS_PATH

    if DEFAULT_SETTINGS_PATH.exists():
        return True
    if not _VENDORED_DEFAULT_SETTINGS.exists():
        logger.error(
            "generate_config_report is missing its settings/defaults.json "
            "(upstream packaging bug) and no vendored fallback was found at %s",
            _VENDORED_DEFAULT_SETTINGS)
        return False
    DEFAULT_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_VENDORED_DEFAULT_SETTINGS, DEFAULT_SETTINGS_PATH)
    logger.info(
        "generate_config_report was missing settings/defaults.json "
        "(upstream packaging bug) - restored it from the vendored copy at %s",
        DEFAULT_SETTINGS_PATH)
    return True


def generate_report(xml_path: Path, report_dest: Path) -> bool:
    """
    Run the norkins/metadata generator against one source's XML dump
    (``xml_path`` — root of the export, containing Configuration.xml,
    Catalogs/, Documents/, ...) and write a Report.txt compatible with
    ReportParser to ``report_dest``.

    Returns True if ``report_dest`` exists afterwards (generation
    succeeded — warnings, e.g. a missing extension folder, don't prevent
    a report from being written). Returns False if the generator package
    isn't installed, or generation failed outright (no Configuration.xml,
    no objects found, write error) — callers should fall back to
    BSL-only indexing, same as before Этап 5.
    """
    if not is_available():
        logger.warning(
            "generate_config_report is not installed - skipping "
            "auto-generation of report.txt for %s "
            "(pip install '.[xml-report]' to enable Этап 5, вариант A)",
            xml_path)
        return False

    if not _ensure_default_settings_installed():
        return False

    from generate_config_report.cli import main as _generator_main

    # Must be absolute: ProjectConfig only uses mainConfigPath as-is when
    # it's absolute, otherwise it joins it onto repoPath — since we pass
    # the same value for both, a relative xml_path would double up into
    # "<xml_path>/<xml_path>" and never be found.
    xml_path = xml_path.resolve()
    report_dest = report_dest.resolve()

    report_dest.parent.mkdir(parents=True, exist_ok=True)

    exit_code = None
    with tempfile.TemporaryDirectory(prefix='ariadna-report-gen-') as tmp:
        config_path = Path(tmp) / 'config.json'
        config = {
            'project': report_dest.parent.name or 'source',
            'repoPath': str(xml_path),
            'mainConfigPath': str(xml_path),
            'mainConfigRequired': True,
            # No separate extension folder here — each source (main or
            # extension) in our project model has its own independent XML
            # dump and gets its own report.txt (see ProjectManager).
            'extensionPath': '',
            'extensionRequired': False,
            'outputPath': str(report_dest.parent),
            'reportFileName': report_dest.name,
            'encoding': 'utf-8',
            'warningsAsErrors': False,
            'buildXmlOverrides': False,
        }
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')

        try:
            exit_code = _generator_main(['--config', str(config_path)])
        except Exception:
            logger.exception(
                "generate_config_report raised while processing %s", xml_path)
            return False

    if not report_dest.exists():
        logger.warning(
            "generate_config_report produced no report.txt for %s "
            "(exit code %s) - indexing will proceed without metadata "
            "for this source", xml_path, exit_code)
        return False

    logger.info(
        "Generated report.txt for %s -> %s (exit code %s)",
        xml_path, report_dest, exit_code)
    return True
