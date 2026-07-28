FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de AS builder
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.8.22@sha256:9874eb7afe5ca16c363fe80b294fe700e460df29a55532bbfea234a0f12eddb1 /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de
RUN groupadd --gid 10001 tradingagent \
    && useradd --uid 10001 --gid 10001 --no-create-home tradingagent
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY migrations ./migrations
COPY alembic.ini ./
USER 10001:10001
ENTRYPOINT ["/app/.venv/bin/tradingagent"]
