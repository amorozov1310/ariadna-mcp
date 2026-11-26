#!/usr/bin/env python3
"""Собрать ZIP разницы для /sources/{id}/delta по манифесту сервера."""

import argparse
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('server_manifest', type=Path)
    parser.add_argument('archive', type=Path)
    parser.add_argument('target_manifest', type=Path)
    args = parser.parse_args()
    baseline = json.loads(args.server_manifest.read_text(encoding='utf-8'))
    files = {}
    changed = []
    for base, dirs, names in os.walk(args.root, followlinks=False):
        for directory in dirs:
            candidate = Path(base) / directory
            if candidate.is_symlink() or getattr(candidate, 'is_junction', lambda: False)():
                raise ValueError(f'Символическая ссылка в исходной выгрузке: {candidate}')
        dirs[:] = sorted(d for d in dirs if not d.startswith('Template.') and d.casefold() != 'logs')
        for name in sorted(names):
            if name.startswith('Template.'):
                continue
            path = Path(base) / name
            if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                raise ValueError(f'Символическая ссылка в исходной выгрузке: {path}')
            rel = path.relative_to(args.root).as_posix()
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
