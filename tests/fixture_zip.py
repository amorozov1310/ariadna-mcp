"""Каталог выгрузки из tests/fixtures → zip-архив для add_source(xml_archive=...)."""

import io
import zipfile
from pathlib import Path


def zip_dir(*dirs: Path) -> io.BytesIO:
    """Файлы каталогов dirs в одном архиве (пути относительно каждого каталога;
    одноимённый файл — из последнего)."""
    buf = io.BytesIO()
    files = {}
    for d in dirs:
        for path in Path(d).rglob('*'):
            if path.is_file():
                files[path.relative_to(d).as_posix()] = path
    with zipfile.ZipFile(buf, 'w') as zf:
        for name, path in files.items():
            zf.write(path, name)
    buf.seek(0)
    return buf
