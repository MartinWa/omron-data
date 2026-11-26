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
```

## Layout

- `omron_data/` — settings, URLs, WSGI entry point
- `parsing/` — the OMRON connect report parser, no Django imports
- `records/` — models, metric registry, importer, management commands, JSON API
- `tests/` — parser, import and API tests with synthetic fixtures
- `docs/` — usage, API and development notes

## Definition of done for a change

`uv run pytest -q`, `ruff check` and `ruff format --check` pass, and no private data is
present in the tree.
