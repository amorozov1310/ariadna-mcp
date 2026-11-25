FROM python:3.12-slim AS base

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Python-зависимости ставятся ОФЛАЙН из заранее скачанных колёс: сеть в
# RUN-шагах BuildKit на этом хосте нестабильна, а из обычного контейнера
# работает. Список — из pyproject.toml ([project].dependencies) через
# scripts/docker_requirements.py, своего списка здесь нет.
# Колёса обновляются при изменении зависимостей (из корня репозитория):
#   docker run --rm -v "$PWD:/repo" -w /repo python:3.12-slim sh docker-wheels/fetch.sh
RUN --mount=type=bind,source=pyproject.toml,target=/app/pyproject.toml \
    --mount=type=bind,source=scripts/docker_requirements.py,target=/app/scripts/docker_requirements.py \
    --mount=type=bind,source=docker-wheels,target=/tmp/wheels \
    python scripts/docker_requirements.py install > /tmp/requirements.txt && \
    pip install --no-cache-dir --no-index --find-links=/tmp/wheels \
        -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

FROM base AS bytecode
COPY src/ src/
RUN python -B -m src.core.code_fingerprints && \
    python -m compileall -b -f -q --invalidation-mode unchecked-hash src && \
    find src -type f -name '*.py' -delete && \
    find src -type d -name '__pycache__' -prune -exec rm -rf {} +

FROM base
# Слой с исходниками остаётся на стадии сборки. Файлы данных копируются вместе с .pyc.
COPY --from=bytecode /app/src/ src/
COPY LICENSE .

# Create data directory
RUN mkdir -p /data/projects /index

# Environment
ENV DATA_DIR=/data
# Внутри контейнера — на всех интерфейсах (проброс портов); наружу порты
# открывает только BIND_ADDR в docker-compose.yml (по умолчанию 127.0.0.1).
# Без Docker по умолчанию 127.0.0.1 (src/main.py).
ENV LISTEN_ADDR=0.0.0.0
ENV MCP_HTTP_PORT=9879
ENV WEB_PORT=9878
ENV LOG_LEVEL=INFO
ENV PYTHONUNBUFFERED=1

EXPOSE 9878 9879

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s \
    CMD curl -f http://localhost:9878/health || exit 1

COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
