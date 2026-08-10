# syntax=docker/dockerfile:1
FROM python:3.13-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
WORKDIR /app/backend

# Dependencies first: this layer is cached until pyproject/uv.lock change.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Then the source. mcp_server/ is a sibling of backend/ because McpExecutor spawns
# REPO_ROOT / "mcp_server" / "server.py" (mcp_client.py), and REPO_ROOT is
# config.py's own Path(__file__).parents[3] -- from
# /app/backend/src/copilot/config.py that resolves to /app, not /app/backend. So
# mcp_server/ must land at /app/mcp_server for that spawn to find it.
COPY backend/src ./src
COPY backend/README.md* ./
COPY mcp_server /app/mcp_server
COPY data/ai_library /app/data/ai_library
RUN uv sync --frozen --no-dev

ENV PATH="/app/backend/.venv/bin:$PATH" PORT=8000
EXPOSE 8000

# Non-root: the task has no reason to run privileged.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",8000)}/healthz').read()"

CMD ["sh", "-c", "uvicorn copilot.api.main:app --host 0.0.0.0 --port ${PORT}"]
