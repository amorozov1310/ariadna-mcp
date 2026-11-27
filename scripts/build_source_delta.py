#!/usr/bin/env python3
"""Собрать zip-разницу между локальной выгрузкой и манифестом Ариадны."""

import hashlib
import json
import sys
import zipfile
from pathlib import Path

META = '.ariadna-delta.json'


def manifest_version(files):
    raw = json.dumps(files, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def main():
    if len(sys.argv) != 4:
        raise SystemExit('usage: build_source_delta.py DUMP_DIR SERVER_MANIFEST OUTPUT_ZIP')
    root, server_manifest, out = map(Path, sys.argv[1:])
    base = json.loads(server_manifest.read_text(encoding='utf-8'))
    local = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise SystemExit(f'Символическая ссылка в выгрузке не поддерживается: {path}')
        if (not path.is_file() or path.name.startswith('Template.')
                or path.name == '.ariadna-manifest.json'):
            continue
        digest = hashlib.sha256()
        size = 0
        with path.open('rb') as f:
            while chunk := f.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        local[path.relative_to(root).as_posix()] = {'sha256': digest.hexdigest(), 'size': size}
    before = base['files']
    changed = [name for name, value in local.items() if before.get(name) != value]
    deleted = sorted(set(before) - set(local))
    meta = {'base_version': base['version'], 'target_version': manifest_version(local),
            'files': local, 'deleted': deleted}
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        archive.writestr(META, json.dumps(meta, ensure_ascii=False, separators=(',', ':')))
        for name in changed:
            archive.write(root / Path(name), name)
    print(json.dumps({'changed_files': len(changed), 'deleted_files': len(deleted),
                      'archive_bytes': out.stat().st_size, 'target_version': meta['target_version']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
