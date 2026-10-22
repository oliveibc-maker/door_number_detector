FROM python:3.11-slim-bookworm@sha256:f06d01d4ba3f8ad5af45a3ec87df19c1d317d8e8961edc0fabda61811af6f7cf

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_CACHE_DIR=/tmp/uv-cache \
    PATH=/opt/venv/bin:$PATH \
    HOME=/data/home \
    APP_DATA_DIR=/data \
    DATABASE_PATH=/data/door_detector.db \
    WEB_HOST=127.0.0.1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        libodbc2 \
        unixodbc \
    && install -d /usr/share/keyrings \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/microsoft-prod.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md setup.py ./
RUN uv sync --locked --no-dev --no-install-project && uv cache clean

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data/home \
    && chown -R app:app /data

COPY --chown=app:app . .
RUN uv sync --locked --no-dev --no-editable && uv cache clean

USER app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=5m --retries=3 \
    CMD ["python", "-m", "web.healthcheck"]

CMD ["door-number-detector-web"]