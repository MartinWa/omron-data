FROM python:3.14-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY manage.py ./
COPY omron_data ./omron_data
COPY parsing ./parsing
COPY records ./records

RUN useradd --uid 1000 --create-home app && mkdir -p /app/data && chown -R 1000:1000 /app
USER 1000:1000

ENV OMRON_DB_PATH=/app/data/omron.sqlite3

CMD ["waitress-serve", "--port", "8000", "omron_data.wsgi:application"]
