# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv-tool

# Apply Debian fixes to inherited packages before building either target.
FROM python:3.14.10-slim-bookworm@sha256:e60ad028afdcdd14772a3d46decf0eaf616720f9102aeb4397a2045ff6718c3d AS os-base
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Runtime dependencies only, in a virtual environment outside /app so that the
# development bind mount cannot hide it.
FROM os-base AS dependencies
COPY --from=uv-tool /uv /uvx /usr/local/bin/
ENV UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /tmp/dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# The same dependencies plus the dev group, for the development target.
FROM os-base AS development-dependencies
COPY --from=uv-tool /uv /uvx /usr/local/bin/
ENV UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /tmp/dependencies
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

FROM os-base AS runtime-base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH
WORKDIR /app

# Source is bind mounted over /app, so this target carries no application code.
FROM runtime-base AS development
COPY --from=development-dependencies /opt/venv /opt/venv
ENV OMRON_DEBUG=true \
    OMRON_DB_PATH=/app/.data/db.sqlite3 \
    OMRON_TIME_ZONE=Europe/Stockholm \
    OMRON_STATIC_ROOT=/app/staticfiles \
    TMPDIR=/tmp
RUN useradd --uid 1000 --create-home developer \
    && mkdir -p /app/.data /app/staticfiles && chown -R 1000:1000 /app
COPY . /app
USER 1000:1000
CMD ["sh", "-c", "python manage.py collectstatic --noinput && python manage.py migrate --noinput && exec python manage.py runserver 0.0.0.0:8000"]

# A plain build produces the production image.
FROM runtime-base AS production
ARG OMRON_VERSION=development
ENV OMRON_VERSION=${OMRON_VERSION} \
    OMRON_DB_PATH=/app/data/omron.sqlite3 \
    OMRON_STATIC_ROOT=/app/staticfiles
COPY --from=dependencies /opt/venv /opt/venv
RUN mkdir -p /app/data && chown 1000:1000 /app/data
COPY manage.py /app/manage.py
COPY omron_data /app/omron_data
COPY parsing /app/parsing
COPY records /app/records
COPY templates /app/templates
COPY LICENSE NOTICE /app/
COPY tools/__init__.py tools/check_production.py tools/healthcheck.py tools/serve.py /app/tools/
# collectstatic fills /app/staticfiles at build time: WhiteNoise reads that
# directory from the image layer and the container root stays read-only.
RUN OMRON_SECRET_KEY=synthetic-build-key-never-for-runtime-000000000000 \
    python manage.py collectstatic --noinput
USER 1000:1000
RUN OMRON_SECRET_KEY=synthetic-build-key-never-for-runtime-000000000000 \
    python -c "from omron_data.wsgi import application; assert application"
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "tools/healthcheck.py"]
CMD ["python", "-m", "tools.serve"]
