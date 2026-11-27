#!/usr/bin/env python3
"""Собрать ZIP разницы для /sources/{id}/delta по манифесту сервера."""

import argparse
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path


def _raise_walk_error(error):
    raise error


def _managed(path: str) -> bool:
    parts = Path(path).parts
    return not any(part.casefold().startswith('template.') or part.casefold() == 'logs' for part in parts)


def _is_reparse_point(path: Path) -> bool:
    try:
        import stat
        attrs = os.lstat(path).st_file_attributes
    except (AttributeError, OSError):
        return path.is_symlink()
    return bool(attrs & 0x400)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('server_manifest', type=Path)
    parser.add_argument('archive', type=Path)
    parser.add_argument('target_manifest', type=Path)
    args = parser.parse_args()
    baseline = json.loads(args.server_manifest.read_text(encoding='utf-8'))
    if _is_reparse_point(args.root):
        raise ValueError(f'Символическая ссылка в исходной выгрузке: {args.root}')
    files = {}
    changed = []
    for base, dirs, names in os.walk(args.root, followlinks=False, onerror=_raise_walk_error):
        for directory in dirs:
            candidate = Path(base) / directory
            if _is_reparse_point(candidate):
                raise ValueError(f'Символическая ссылка в исходной выгрузке: {candidate}')
        dirs[:] = sorted(d for d in dirs if _managed(d))
        for name in sorted(names):
            path = Path(base) / name
            if _is_reparse_point(path):
                raise ValueError(f'Символическая ссылка в исходной выгрузке: {path}')
            rel = path.relative_to(args.root).as_posix()
            if not _managed(rel):
                continue
            if len(rel) > 1024 or '\\' in rel or '..' in Path(rel).parts:
                raise ValueError(f'Недопустимый путь: {rel}')
            h = hashlib.sha256()
            with path.open('rb') as f:
                for block in iter(lambda: f.read(1024 * 1024), b''):
                    h.update(block)
            size = path.stat().st_size
            digest = h.hexdigest()
            files[rel] = {'sha256': digest, 'size': size}
            if baseline.get('files', {}).get(rel) != files[rel]:
                changed.append((rel, path))
    version = hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True,
                                        separators=(',', ':')).encode('utf-8')).hexdigest()
    target = {'version': version, 'files': dict(sorted(files.items()))}
    args.target_manifest.write_text(json.dumps(target, ensure_ascii=False, separators=(',', ':')),
                                    encoding='utf-8')
    with zipfile.ZipFile(args.archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for rel, path in changed:
            zf.write(path, rel)
    print(json.dumps({'base_version': baseline['version'], 'target_manifest': str(args.target_manifest),
                      'archive': str(args.archive), 'changed': len(changed)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'Не удалось собрать разницу: {exc}', file=sys.stderr)
        raise SystemExit(1)
