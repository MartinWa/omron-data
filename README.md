<h1 align="center">omron-data</h1>

<p align="center">
  <a href="https://github.com/MartinWa/omron-data/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/MartinWa/omron-data/actions/workflows/ci.yml/badge.svg?branch=main"></a>
  <a href="https://github.com/MartinWa/omron-data/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/MartinWa/omron-data?label=release"></a>
  <a href="https://github.com/MartinWa/omron-data/pkgs/container/omron-data"><img alt="Container image on GHCR" src="https://img.shields.io/badge/ghcr.io-omron--data%3Alatest-2496ED?logo=docker&amp;logoColor=white"></a>
  <a href="LICENSE"><img alt="License: PolyForm Noncommercial 1.0.0" src="https://img.shields.io/badge/license-PolyForm%20Noncommercial%201.0.0-blue"></a>
</p>

Self-hosted import of the OMRON connect report into a private SQLite database, with a
server-rendered web UI and a read-only JSON API.

OMRON has no usable API for private use: the app is the only way to get your history out,
and it exports a date range at a time. This service imports those exports, merges them into
one history per person, and reads it back over the pages it serves — readings, dashboards,
imports and tokens — and over a token-authenticated API. The database stays yours: one file
on one host, no account anywhere else, and each person's history is their own.

- One container, one database file: no external services, no sidecar, no telemetry,
  nothing loaded from a CDN
- Merge, never replace: each export is a window, so imports add to the history and skip
  what is already stored
- Dashboards over the stored history: blood pressure, weight, body composition,
  consistency and readings per week, drawn as inline SVG in either theme, printable, and
  each chart carries its numbers in a table
- Read-only, versioned, owner-scoped API with a contract envelope on every response, where
  a blood-pressure reading is a panel with systolic, diastolic and pulse components,
  matching how it is actually taken

## Quick start

Docker Engine 24 or newer with a current Compose release, on `amd64` or `arm64`, behind a
TLS-terminating reverse proxy.

```sh
mkdir -p data && sudo chown -R 1000:1000 data
cp .env.example .env       # set OMRON_SECRET_KEY, OMRON_ALLOWED_HOSTS and OMRON_TRUSTED_PROXY
docker compose up -d
```

`OMRON_TRUSTED_PROXY` is not optional in this recipe: waitress discards `X-Forwarded-Proto`
from a peer it was not told to trust, so with it empty the app sees `http`, every browser
login fails the CSRF origin check, and nothing says why. Use `*` for a proxy on the private
network, or name the proxy's address.

Then attach your proxy to the `omron-data` network, forward it to `web:8000`,
[create the first account](docs/deployment.md#accounts-and-tokens) and log in.
[Deployment](docs/deployment.md) covers every variable, both proxy patterns, upgrades and
ownership.

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
timestamp. Weight is read in kilograms, falling back to pounds when only pounds are
present.

Reports are read in the app's Swedish locale: `25 nov. 2026` dates, decimal commas, percent
signs inside the values (`34,3%`). A file whose headers are all unrecognised is refused
rather than imported as empty.

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

## Run it

```sh
uv sync
export OMRON_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
uv run python manage.py migrate
uv run python manage.py import_omron /path/to/exports --owner alice --create-user --dry-run
uv run python manage.py make_token --owner alice --name laptop
uv run python manage.py collectstatic --noinput
uv run python -m tools.serve
```

`collectstatic` is what puts the stylesheet and the scripts where the server looks for them:
without it the pages render unstyled. WhiteNoise serves them from `staticfiles/`
(`OMRON_STATIC_ROOT` overrides the location), and it reads the directory once at startup, so
re-run `collectstatic` and restart after changing an asset. The image bakes the same files in
at build time, which is why a container never needs this step.

`OMRON_SECRET_KEY` has no default outside `OMRON_DEBUG=1`: a process that forgot it refuses
to start instead of serving with a published key. Set `OMRON_ALLOWED_HOSTS` too;
`OMRON_DB_PATH` overrides the database location (default `.data/db.sqlite3`) and
`OMRON_TIME_ZONE` sets the zone the export's wall clocks are read in (default
`Europe/Stockholm`).

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
| `/api/v1/healthz` | liveness, and the only data endpoint that needs no token |
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
method other than `GET` or `HEAD` is a 405, and every response sets `Cache-Control: no-store`.
Every query is scoped to the token's owner, and another owner's id is a 404 rather than a
403. [docs/api.md](docs/api.md) has the parameters, the guardrails and the error codes.

## Development

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

Fixtures are synthetic. [docs/development.md](docs/development.md) covers setup, the
container development stack and how the test suite is organised.

## Documents

- [Using the app](docs/usage.md) — the export, the import, and each page
- [Deployment](docs/deployment.md) — every variable, the reverse proxy, ownership, upgrades
- [Backup and recovery](docs/backup-and-recovery.md)
- [API](docs/api.md) — authentication, the envelope and the guardrails; the generated
  reference is at `/api/v1/docs`
- [Development](docs/development.md), [contributing](CONTRIBUTING.md) and the
  [security policy](SECURITY.md)

## Personal data

**This repository is public and holds no health data, and it must never hold any.** An
export is a person's whole history for the range it covers, and its dates identify them, so
redacting rows is not enough: committed fixtures, examples and documentation are wholly
synthetic. Never commit a CSV export, a database or backup file, a token, a `.env` file or a
real hostname, and never paste one into an issue or a pull request.
[SECURITY.md](SECURITY.md) lists what counts as personal data, and
`uv run python tools/check_no_private_data.py` — which the test suite runs as well — fails
when one of them is in the tree.

## License

PolyForm Noncommercial 1.0.0: source-available, not OSI open source. Run, modify and share it
for any noncommercial purpose, including a home lab, a nonprofit or research use. Commercial
use needs a separate license, arranged through this repository's Discussions. See
[LICENSE](LICENSE) and [NOTICE](NOTICE).

Contributions are accepted under the [CLA](CLA.md), which keeps your copyright and grants
the project the rights that dual licensing needs.
