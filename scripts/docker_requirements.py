"""
Зависимости для Docker-образа — из pyproject.toml, единственного источника.

[project].dependencies и extra xml-report печатаются по одной на строку в
одном из трёх видов:

  download — для `pip download` (docker-wheels/fetch.sh): всё, кроме
             зависимостей по прямой ссылке (`имя @ git+https://…`);
  git      — только ссылки из зависимостей по прямой ссылке: fetch.sh
             собирает из них колёса `pip wheel --no-deps`;
  install  — для офлайн-`pip install` в Dockerfile: всё, у зависимостей по
             ссылке — только имя (колесо уже лежит в docker-wheels, а по
             самой ссылке сборка без сети не пройдёт).

Нужен только Python ≥ 3.11 (tomllib): запускается и в python:3.12-slim без
установленных пакетов.
"""

import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / 'pyproject.toml'
EXTRAS = ('xml-report',)
MODES = ('download', 'git', 'install')


def requirements(pyproject: Path = PYPROJECT) -> list[str]:
    project = tomllib.loads(pyproject.read_text(encoding='utf-8'))['project']
    reqs = list(project['dependencies'])
    for extra in EXTRAS:
        reqs += project['optional-dependencies'][extra]
    return reqs


def _split_url(req: str) -> tuple[str, str | None]:
    """«имя @ ссылка» (PEP 508) → (имя, ссылка); иначе (req, None)."""
    if '@' in req:
        name, url = req.split('@', 1)
        return name.strip(), url.strip()
    return req, None


def render(mode: str, reqs: list[str]) -> list[str]:
    if mode not in MODES:
        raise ValueError(f"неизвестный режим {mode!r}, допустимы: {', '.join(MODES)}")
    out = []
    for req in reqs:
        name, url = _split_url(req)
        if mode == 'download' and url is None:
            out.append(req)
        elif mode == 'git' and url is not None:
            out.append(url)
        elif mode == 'install':
            out.append(name)
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in MODES:
        print(f"Использование: python {Path(__file__).name} {{{'|'.join(MODES)}}}",
              file=sys.stderr)
        return 2
    for line in render(argv[0], requirements()):
        print(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
