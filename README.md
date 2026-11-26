# omron-data

Self-hosted import of the OMRON connect report into a private SQLite database, with a
read-only JSON API.

OMRON has no usable API for private use: the app is the only way to get your history out,
and it exports a date range at a time. This service imports those exports, merges them into
one history per person, and reads it back over a token-authenticated API.

- One container, one database file: no external services, no telemetry
- Merge, never replace: each export is a window, so imports add to the history and skip
  what is already stored
- Multiple people: measurements are owned by a Django user, and one export folder maps to
  one person
- Blood pressure is a panel with systolic, diastolic and pulse components, matching how a
  reading is actually taken

## What it imports

Blood pressure rows: `systolic_bp`, `diastolic_bp`, `pulse`, plus the note the app writes
(`AFib` lands in `Mätläge`; `Anmärkningar` is read when it has text), kept on the panel.

Body composition rows: `body_weight`, `bmi`, `body_fat_pct`, `skeletal_muscle_pct`,
`visceral_fat`, `resting_metabolism`, `impedance`, `fat_free_mass`, `body_water_pct`,
`average_weight`, `average_fat_free_mass`, and body temperature when the export carries a
temperature section. Fields the app leaves as `-` are not stored.

A file holds several sections, each with its own header, and they do not spell the same
thing the same way: `Kroppsfett (%)` and `Kroppsfett–procent` are one measurement, and the
same reading appears in two sections of the same file. Sections are identified from their
header, columns are mapped by name, and both body sections collapse onto one reading per
timestamp. Weight is read in kilograms, falling back to pounds when only pounds are present.

Reports are read in the app's Swedish locale: `25 nov. 2026` dates, decimal commas, percent signs
inside the values (`34,3%`). A file whose headers are all unrecognised is refused
rather than imported as empty.

## Run it

```sh
uv sync
uv run python manage.py migrate
uv run python manage.py import_omron /path/to/exports --owner alice --create-user --dry-run
uv run python manage.py make_token --owner alice --name laptop
uv run waitress-serve --port 8000 omron_data.wsgi:application
```

Set `OMRON_SECRET_KEY` and `OMRON_ALLOWED_HOSTS` in the environment; `OMRON_DB_PATH`
overrides the database location (default `.data/db.sqlite3`) and `OMRON_TIME_ZONE` sets the
zone the export's wall clocks are read in (default `Europe/Stockholm`).

## API

```sh
curl -H "authorization: Bearer $TOKEN" http://localhost:8000/api/v1/summary
curl -H "authorization: Bearer $TOKEN" \
  'http://localhost:8000/api/v1/measurements?metric=body_weight&since=2026-08-01T00:00:00%2B02:00'
curl -H "authorization: Bearer $TOKEN" \
  'http://localhost:8000/api/v1/measurements?level=component&metric=systolic_bp&limit=500'
```

Every response carries an envelope with `contract_version` and `generated_at`, errors
included, so a client can tell a contract change from a data change. `/api/v1/docs` renders
the endpoints; `/api/v1/openapi.json` is the schema.

| Path | What it returns |
|---|---|
| `/api/v1/` | the contract version and the endpoint list |
| `/api/v1/healthz` | liveness, and the only endpoint that needs no token |
| `/api/v1/metrics` | the metric registry with units and whether a code is a panel, a component or a plain reading |
| `/api/v1/measurements` | a page of readings, `level=panel` by default |
| `/api/v1/measurements/{id}` | one row plus its components |
| `/api/v1/summary` | per-metric count and coverage |
| `/api/v1/imports` | the import ledger, with the covered span |

`level` is `panel` (blood-pressure events and standalone body readings), `component` (the
systolic, diastolic and pulse rows inside a panel) or `all`; `include=components` inlines a
panel's components, `sort` takes `at`, `metric`, `value` or `id` and defaults to `-at`, and
`limit`/`offset` page the result. `limit` is at most 1000 and a `since`/`until` window may
span at most 1830 days.

Filtering is strict: an unknown or repeated parameter, an unknown metric, a component metric
at panel level, an unsupported sort key, a missing UTC offset or a window that is too wide is
a 422 with the reason in the body rather than an empty page. The API is read-only, so any
method other than `GET` or `HEAD` is a 405. Every query is scoped to the token's owner, and
another owner's id is a 404 rather than a 403.

## Exports are windows

Imports are additive and idempotent: the same file imported twice changes nothing, and
overlapping ranges collapse onto one measurement per timestamp and metric. Deleting a
reading in the app does not delete it here, which is the point of keeping your own copy.

Every import reports what it did, per file and in total:

```
Rapport (20 juni, 2024 – 1 juli, 2024).csv: 21 readings, 18 created, 3 skipped, 0 conflicts, 0 overwritten, 6 new panels
```

`skipped` means the reading was already stored, byte for byte, and nothing was written.
`conflicts` means the file disagreed with what is stored: the stored value is kept, and each
one is printed with its timestamp and both values so you can look at it. `--overwrite`
applies the file's values instead, and `--dry-run` reports the counts without writing
anything, so a first import can be inspected before it lands.

## Import history

Every import is recorded: the file, its checksum, how many readings it carried, and the
range it covered. That answers "have I imported September yet?", and it makes a repeated
import free — a file whose checksum is already in the ledger is not read again.

```sh
uv run python manage.py coverage --owner alice
uv run python manage.py purge_import --id 4 --owner alice --dry-run
uv run python manage.py import_omron /path/to/exports --owner alice --replay
```

`coverage` lists each metric with its first and last reading and every gap longer than a
week. `purge_import` deletes exactly the rows one import created, which is how a wrong file
is undone without disturbing anything imported before or since.

A known file is normally skipped, because reading it again cannot add anything. Two cases
want it anyway: a purge may have removed readings that a later window also contains, and a
new version of this tool may understand columns that older imports dropped. `--replay`
imports a known file again without overwriting a single stored value, so it can only fill
gaps.

## Development

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

Fixtures are synthetic. Never commit an export, a database file or a token.
