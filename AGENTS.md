# AGENTS.md

Instructions for automated agents working in this repository.

## Ground rules

- `README.md` and `docs/` are the source of truth for behavior. Keep them in sync with any
  change.
- `planning/` and `reference/` are private and gitignored. Never commit their contents.
- Never commit CSV, database, token or `.env` files. All fixtures must be wholly synthetic.
- Use Django migrations, never hand-edited schemas.
- No comments in code unless explicitly requested.

## Commands

Run from the repository root. Python is pinned by `.python-version` (3.14).

```sh
uv sync
uv run python manage.py migrate
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python manage.py makemigrations --check --dry-run
uv run python tools/check_no_private_data.py
```

Outside pytest the settings refuse to start without a real `OMRON_SECRET_KEY`, by design;
`tests/settings.py` supplies one for the suite. Export a key for any `manage.py` command:

```sh
export OMRON_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
```

## Layout

- `omron_data/` — settings, URLs, WSGI entry point, `app_info` context processor, login and
  error views
- `parsing/` — the OMRON connect report parser, no Django imports
- `records/` — models, metric registry, importer, management commands, JSON API, queries,
  page views, chart geometry (`reports.py`), SQLite pragmas (`db.py`), templates, static
  assets and template tags
- `templates/` — the shell (`base.html`), error pages, login, partials and the page templates
- `tests/` — parser, import, API, page, settings, packaging and hygiene tests, all with
  synthetic fixtures
- `tools/` — healthcheck, production check, private-data scan, waitress entry point, image
  audit
- `docs/` — usage, deployment, API, backup-and-recovery and development notes
- `.github/` — CI, release, Scorecard, zizmor, dependency review, Dependabot

## Definition of done for a change

`uv run pytest -q`, `ruff check`, `ruff format --check` and `makemigrations --check --dry-run`
pass, `tools/check_no_private_data.py` finds nothing, and no private data is present in the
tree.
