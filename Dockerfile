# The API's server image (staging, prod). Locally the API runs from the uv venv;
# see the README.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.5.31 /uv /usr/local/bin/uv

WORKDIR /app
# Unbuffered so `docker compose logs` shows a line when it is logged.
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

# The venv is first on PATH, so `docker compose run --rm app python scripts/...`
# runs a script with the app's dependencies.

# Dependencies on their own layer, so a code-only change reuses it.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY . .
RUN uv sync --frozen

RUN useradd --system --no-create-home wravel
USER wravel

EXPOSE 4830
# Exec form, so uvicorn is PID 1 and gets Docker's SIGTERM directly. nginx's
# connection arrives from the Docker bridge, not 127.0.0.1, so trust forwarded
# headers from anywhere: safe only because the port is published on the host's
# loopback (docker-compose.yml).
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "4830", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
