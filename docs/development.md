# Development

Local setup and the checks a change has to pass. The project is a Django application with a
parser that has no Django imports (`parsing/`), the models, importer, management commands
and JSON API (`records/`), settings and the WSGI entry point (`omron_data/`), and a test
suite that builds every fixture synthetically. Python is pinned by `.python-version` and
`uv` installs it.

## Setup

```sh
uv sync
uv run python manage.py migrate
```

Settings are read from the environment. With neither `OMRON_SECRET_KEY` nor
`OMRON_DEBUG=1` set, the settings module refuses to import, which is deliberate; for a
local run either export a key or export `OMRON_DEBUG=1` and get the fixed development key.
`OMRON_TIME_ZONE` decides the zone the export's wall-clock timestamps are read in and the
zone every date is rendered in, and it defaults to `Europe/Stockholm`. The default database
is `.data/db.sqlite3` and the default static directory is `staticfiles/`; both are
gitignored.

The test suite supplies its own settings through `tests/settings.py`, so `uv run pytest -q`
needs no environment and never touches `.data/`.

## Commands

Run from the repository root:

| Command | What it checks |
| --- | --- |
| `uv sync` | Installs the locked dependencies, including the `dev` group |
| `uv run python manage.py migrate` | Applies the migrations to the local database |
| `uv run pytest -q` | The whole suite |
| `uv run ruff check .` | Lint, with the rule set in `pyproject.toml` |
| `uv run ruff format --check .` | Formatting, 100 columns |

Three more worth running before a pull request or a release, all runnable the same way:

| Command | What it checks |
| --- | --- |
| `uv run python manage.py check` | Django's own system checks |
| `uv run python tools/check_production.py` | That the installation is configured for production: `OMRON_DEBUG` off, a private secret, a writable database, every migration applied |
| `uv run python tools/check_no_private_data.py` | That the working tree carries no private data |

To run the app from a working copy, collect the static files first and then serve it with
the same entry point the image uses:

```sh
uv run python manage.py collectstatic --noinput
uv run python -m tools.serve
```

That verifies the data directory, applies migrations and serves on `OMRON_PORT` (8000) with
`OMRON_THREADS` workers (8). It also honours `OMRON_TRUSTED_PROXY`, which plain
`waitress-serve` cannot: served that way, a proxy's `X-Forwarded-Proto` is discarded and
browser `POST`s fail the CSRF origin check.

## Container development

`compose.development.yaml` is a standalone development stack. It builds the `development`
target of the Dockerfile, publishes `127.0.0.1:8030` only, needs no `.env`, and bind-mounts
your working copy at `/app`:

```sh
docker compose -f compose.development.yaml up --build
```

That target carries the dependencies but no application code, because the bind mount
provides the code; it sets `OMRON_DEBUG=true`, keeps the database at `.data/db.sqlite3` and
the collected static files in `staticfiles/` inside your working copy, and runs
`collectstatic`, `migrate` and the Django development server on start. It runs as uid 1000
with every capability dropped and `no-new-privileges`, so a file it creates is owned by
1000 on the host. Rebuild after a dependency change so the virtual environment baked into
the image is refreshed.

## Layout

| Path | Contents |
| --- | --- |
| `omron_data/` | Settings, URLs, WSGI entry point, context processors |
| `parsing/` | The OMRON connect report parser, with no Django imports |
| `records/` | Models, metric registry, importer, management commands, JSON API, pages |
| `templates/` | The shell, the pages and their partials |
| `records/static/records/` | The stylesheet and the scripts, copied to `staticfiles/` by `collectstatic` |
| `tests/` | Parser, import, page and API tests with synthetic fixtures |
| `tools/` | The operational scripts that ship inside the image |
| `docs/` | This documentation |

## The chart convention

A chart is computed in Python, never in a template. `records/reports.py` turns a list of
points into **one dict** and `templates/partials/report_chart.html` renders that dict; no
page computes a coordinate, and no template does arithmetic.

- `bar_chart(points, ...)` and `line_chart(lines, ...)` return the same frame: `kind`,
  `width`, `height`, `left`, `right`, `top`, `bottom`, `plot_bottom`, `right_edge`, `ticks`,
  `xlabels`, `aria_label` and `empty`, plus `bars` or `lines`. The frame is a 720 × 240
  viewBox with three labelled y ticks.
- Geometry is pure Python and stays finite: coordinates are rounded to two decimals, and a
  non-finite or missing value plots as `0` rather than `nan`. The axis maximum comes from
  `nice_max`, which snaps to 1, 2, 4, 5 or 10 times a power of ten, so a small history still
  gets a readable axis.
- A bucket that only partly overlaps the range is flagged in the dict (`partial`,
  `in_progress`) together with a note, and the partial renders it as `bar-partial`; the note
  becomes the bar's `<title>`, which is how a partial period is explained without a legend.
- **Colours come from CSS only.** Neither the Python nor the partial writes a colour: the
  only paint attribute is `fill="none"` on a polyline. The partial emits classes
  (`chart-bar`, `bar-partial`, `chart-line`, `series-1` to `series-3`, `chart-dot`) and the
  palette lives in `records/static/records/app.css`, so both themes and the print
  stylesheet get a readable chart for free.
- The SVG carries `role="img"`, a non-empty `aria-label` and a `<title>` per bar, so a
  screen reader and a printed page get the numbers even without hover.
- The card builders (`_blood_pressure_card`, `_weight_card`, `_composition_card`,
  `_consistency_card`, `_weekly_card`, `_extremes_card`) sit on top of those two functions
  and return the card's `chart` dict plus its `rows` for the table behind *Show the
  numbers*. `build_dashboards(owner, params, today)` and
  `build_metric_dashboard(owner, metric, params, today)` assemble the whole page context
  without a request object, so a page test and a shell check can call them directly.

`tests/test_reports.py` renders the partial with `render_to_string` and asserts the key set,
the finite coordinates, that one point still draws a marker, and that no `nan`, `inf` or
`None` reaches the markup. `tests/test_dashboards.py` covers the page: the range chips and
their default, the six KPI numbers, one metric at a time in the composition card, the
heatmap grid, every SVG carrying a role and a label, that no markup carries a literal colour
or a `#hex`, that the drill-down is a 404 for an unknown metric, and that the page stays
under forty queries.

## The opt-in parity check

`tests/test_parity.py` reads a directory of real OMRON connect exports named by
`OMRON_LOCAL_CSV` and checks four things a synthetic fixture cannot: that every section of
every file is recognised, that no `(timestamp, metric)` appears twice after both body
sections collapse, that no data row is silently dropped, and that every metric the parser
produces is registered.

```sh
OMRON_LOCAL_CSV=/path/to/exports uv run pytest tests/test_parity.py -q
```

It is skipped when the variable is unset, which is why the committed suite stays wholly
synthetic and why CI never needs a real file. Never add its output, a fixture copied from it
or a redacted row to the repository: an export is a person's whole history for the range it
covers, and its dates identify them.

## The private-data scan

`uv run python tools/check_no_private_data.py` fails on two kinds of finding:

- by name: a `.csv`, `.sqlite3`, `.db`, `.dump`, `.sql`, archive or key file, `.env` other
  than `.env.example`, a `tokens.*` file, and anything under `planning/` or `reference/`
- by content, in any file under 2 MB: a bearer token, a private key block, an e-mail address
  outside `example.com`, and an absolute host path such as a home directory

A line that legitimately has to look like private data can opt out by carrying the marker
`hygiene: allow`. `planning/`, `reference/`, `.data/`, `data/`, `staticfiles/` and the cache
directories are skipped entirely. `tests/test_repo_hygiene.py` runs the same scan over the
whole tree, so a commit that carries a real export fails the suite rather than the scanner
alone.

## The test suite

| Module | Covers |
| --- | --- |
| `conftest.py` | Two synthetic owners with tokens, authenticated clients, and a list of panel ids |
| `settings.py` | The settings module the suite runs with |
| `test_report.py` | The parser: section detection, aliases, locale, the two body sections collapsing |
| `test_import.py` | The importer: counts, panels and components, conflicts, overwrite, replay, invalid timestamps |
| `test_import_pages.py` | The upload card on `/imports/`, preview, confirm, cancel, the replay checkbox, and purging from the ledger |
| `test_ledger.py` | The import ledger and what the command line reports per file and in total |
| `test_readings_page.py` | The readings page: filters, event grouping, badges, paging, the empty states |
| `test_reports.py` | Chart geometry and the SVG partial |
| `test_dashboards.py` | The dashboards page: range chips, KPIs, each card, the heatmap, the drill-down, print |
| `test_tokens_page.py` | Creating, revoking, expiring and deleting a token, and what is stored |
| `test_admin.py` | The read-only admin for measurements, runs and tokens |
| `test_api_auth.py` | Bearer tokens, revoked and expired tokens, deactivated owners, shared prefixes |
| `test_api_contract.py` | The envelope, every parameter guardrail, level and include, paging, `Cache-Control` |
| `test_shell.py` | The base template, the navigation, the theme bootstrap, static assets, formatting filters |
| `test_error_pages.py` | The 404 and 500 pages |
| `test_settings.py` | Every setting's parsing and its fail-loud default |
| `test_compose.py` | What Compose passes to the container and what the overlays add |
| `test_repo_hygiene.py` | Ignore rules, `.env.example` against the settings module, the Dockerfile, the Compose files, and the private-data scan |
| `test_parity.py` | The opt-in real-export check above |

Fixtures are synthetic and live in the tests that use them. Never commit an export, a
database file or a token.

## Definition of done

`uv run pytest -q`, `uv run ruff check .` and `uv run ruff format --check .` pass, no private
data is present in the tree, and `README.md` and `docs/` describe the behaviour that is
actually there. [CONTRIBUTING.md](../CONTRIBUTING.md) has the paperwork, including the
contributor agreement, and [AGENTS.md](../AGENTS.md) has the rules for an automated change.
