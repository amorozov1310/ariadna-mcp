FROM python:3.12-slim AS base

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Python deps installed OFFLINE from pre-downloaded wheels: BuildKit's network
# is broken on this host (pip in RUN steps can't reach PyPI), while runtime
# containers CAN. Refresh the wheels when deps change:
#   docker run --rm -v <repo>/docker-wheels:/wheels python:3.12-slim \
#     pip download --dest /wheels "mcp>=2,<3" "pydantic>=2" "fastapi>=0.110" \
#     "uvicorn[standard]" jinja2 aiosqlite python-multipart starlette
# generate_config_report*.whl (Этап 5, optional xml-report extra) is built from
# git+https://github.com/norkins/metadata.git the same way (needs git + --no-deps).
COPY pyproject.toml .
COPY docker-wheels/ /tmp/wheels/
RUN pip install --no-cache-dir --no-index --find-links=/tmp/wheels \
    "mcp>=2,<3" \
    "pydantic>=2" \
    "fastapi>=0.110" \
    "uvicorn[standard]" \
    "jinja2" \
    "aiosqlite" \
    "python-multipart" \
    "starlette" \
    "generate-config-report" && \
    rm -rf /tmp/wheels

# App code
COPY src/ src/

# Create data directory
RUN mkdir -p /data/projects

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
