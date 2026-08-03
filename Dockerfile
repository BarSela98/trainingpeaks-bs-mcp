# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/tp-mcp

WORKDIR /build

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Resolve only from the committed lock file. --no-editable makes the installed
# project independent of /build when the virtual environment is copied.
RUN python -m pip install "uv==0.7.3" \
    && uv sync --frozen --no-dev --extra cloud --no-editable


FROM python:3.12-slim AS runtime

ENV HOST=0.0.0.0 \
    PORT=8080 \
    PATH=/opt/tp-mcp/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --system --gid 10001 tp-mcp \
    && useradd --system --uid 10001 --gid tp-mcp --create-home --home-dir /home/tp-mcp tp-mcp

COPY --from=builder /opt/tp-mcp /opt/tp-mcp

USER 10001:10001
WORKDIR /home/tp-mcp

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8080') + '/health', timeout=3).read()"]

CMD ["tp-mcp", "serve-http"]
