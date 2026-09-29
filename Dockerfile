FROM python:3.12-slim AS base

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Python-зависимости ставятся ОФЛАЙН из заранее скачанных колёс: сеть в
# RUN-шагах BuildKit на этом хосте нестабильна, а из обычного контейнера
# работает. Список — из pyproject.toml ([project].dependencies и extra
# xml-report) через scripts/docker_requirements.py, своего списка здесь нет.
# Колёса обновляются при изменении зависимостей (из корня репозитория):
#   docker run --rm -v "$PWD:/repo" -w /repo python:3.12-slim sh docker-wheels/fetch.sh
COPY pyproject.toml .
COPY scripts/docker_requirements.py scripts/
COPY docker-wheels/ /tmp/wheels/
RUN python scripts/docker_requirements.py install > /tmp/requirements.txt && \
    pip install --no-cache-dir --no-index --find-links=/tmp/wheels \
        -r /tmp/requirements.txt && \
    rm -rf /tmp/wheels /tmp/requirements.txt

# App code
COPY src/ src/

# Create data directory
RUN mkdir -p /data/projects /index

# Environment
ENV DATA_DIR=/data
ENV MCP_PORT=9877
ENV MCP_HTTP_PORT=9879
ENV WEB_PORT=9878
ENV LOG_LEVEL=INFO
ENV PYTHONUNBUFFERED=1

EXPOSE 9877 9878 9879

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s \
    CMD curl -f http://localhost:9878/health || exit 1

COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
