#!/bin/bash
set -e

echo "============================================"
echo "  Ариадна — сервер"
echo "  Data:    ${DATA_DIR:-/data}"
echo "  Index:   ${INDEX_DIR:-${DATA_DIR:-/data}/projects}"
echo "  MCP:     :${MCP_HTTP_PORT:-9879}/mcp"
echo "  Web UI:  :${WEB_PORT:-9878}"
echo "============================================"

mkdir -p "${DATA_DIR:-/data}/projects"
if [ -n "${INDEX_DIR}" ]; then mkdir -p "${INDEX_DIR}"; fi

cd /app
exec python3 -m src.main
