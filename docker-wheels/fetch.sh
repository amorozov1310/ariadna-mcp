#!/bin/sh
set -e
pip download --dest /wheels --quiet --retries 5 --timeout 120 \
  "mcp>=2,<3" "pydantic>=2" "fastapi>=0.110" "uvicorn[standard]" \
  jinja2 aiosqlite python-multipart "starlette>=1.7"
apt-get update -qq >/dev/null
apt-get install -y -qq git >/dev/null
pip wheel --no-deps -w /wheels --quiet \
  "generate-config-report @ git+https://github.com/norkins/metadata.git"
ls /wheels | wc -l
