FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
RUN groupadd --gid 10001 tradingagent \
    && useradd --uid 10001 --gid 10001 --no-create-home tradingagent
COPY --from=builder /install /usr/local
WORKDIR /app
COPY migrations ./migrations
COPY alembic.ini ./
USER 10001:10001
ENTRYPOINT ["tradingagent"]

