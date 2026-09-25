#!/bin/bash
set -e

echo "============================================"
echo "  Ариадна — сервер"
echo "  Data:    ${DATA_DIR:-/data}"
echo "  MCP:     :${MCP_PORT:-9877}"
echo "  Web UI:  :${WEB_PORT:-9878}"
echo "============================================"

mkdir -p "${DATA_DIR:-/data}/projects"

cd /app
exec python3 -m src.main
