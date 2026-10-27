# Serves the API (and the built frontend in web/dist when present).
# Recorded mode needs no credentials: NIRVANA_MODE=recorded serves recordings/*.json.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY bridges ./bridges
COPY vendor ./vendor
COPY recordings ./recordings
COPY web ./web
ENV NIRVANA_HOME=/tmp/nirvana NIRVANA_MODE=recorded PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uv run --no-dev nirvana serve --host 0.0.0.0 --port ${PORT}"]
