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

## Releasing

A release is an annotated `X.Y.Z` tag on `main` with a clean working tree; pushing the tag
triggers `.github/workflows/release.yml`, which builds, attests and publishes the image.
`tools/tag_release.ps1` creates the tag on a clean `main`, bumping the patch version of the
latest stable tag unless `-Version X.Y.Z` is given, and `-Push` pushes it:

```powershell
pwsh tools/tag_release.ps1 -Push
```

It is a host-side maintainer script and is not copied into the image.

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
  `width`, `height`, `left`, `right`, `top`, `bottom`, `plot_bottom`, `plot_width`,
  `right_edge`, `ticks`, `xlabels`, `aria_label` and `empty`, plus `bars` for a bar chart.
  A line chart's frame also carries `y_min`, `y_max` (the resolved domain), `max_gap` and
  `lines`; every line carries `label`, `title`, `class`, `segments`, `dots` and `marks`.
  `bar_chart` takes an optional `label_of(point)` to override the bucket labels in both the
  bar `<title>`s and the x tick labels — the weekday chart uses it to label its seven bars
  Mon–Sun. A single-point line is never dropped: its one-point segment is invisible, but the
  lone dot still renders (a zero-point line is the only one skipped, so an empty series
  never reaches the legend).
  Both take an optional keyword `height=None`: the default frame is a 720 × 240 viewBox
  (`CHART_HEIGHT`) with three labelled y ticks, and the dashboard card builders pass
  `height=TALL_CHART_HEIGHT` (380) so every chart on a dedicated dashboards page renders a
  720 × 380 viewBox — except the readings-per-weekday chart, which keeps the default frame —
  and the returned `height` is the value actually used, and the SVG scales
  responsively (`width: 100%; height: auto`, so no CSS caps it).
- **Units are raw in Python and spaced once in the template layer.** `bar_chart` and
  `line_chart` take the raw unit (`"kg"`, `"%"`, `"mmHg"`), `_tick_label` appends it through
  the `axis_unit` filter in `records/templatetags/formatting.py` — `%` stays tight, every
  other unit gets one leading space, an empty unit stays empty — and the same filter is used
  in the markup wherever a unit follows a value. `_metric_value_text(unit, value)` in
  `reports.py` formats a value with the per-unit precision map (`mmHg` 0, `kg` 1, `%` 1,
  `°C` 1) plus that unit, and is what extremes, KPI texts and table rows use, so a rendered
  number never shows raw float precision.
- **Domains.** A bar chart keeps a zero baseline, because every bar left on the page is a
  count and a bar is a magnitude from zero. A line chart derives its domain from the data —
  `lo = min(values + [y_min])`, `hi = max(values + [y_max])` — and `nice_domain` pads it
  outward to a round step (`nice_step` over 1, 2, 2.5, 5, 10 × 10^e) with an even number of
  steps, so the three ticks land at `y_min`, the middle and `y_max`, all multiples of the
  step. A truncated line axis is legitimate when the story is change over time, so both end
  ticks stay on the chart and the numbers stay behind *Show the numbers*; a flat series gets
  a span of `max(abs(value) * 0.1, 1.0)` so the domain never degenerates. For units whose
  `UNIT_PRECISION` is 0 (`mmHg`, `/min`, `kcal`, `ohm`) `line_chart` calls
  `nice_domain(lo, hi, integer=True)`, which skips the fractional 2.5×10⁰ step candidate in
  `nice_step` and never returns a step below 1, so the y ticks are whole numbers.
- **Segments, dots, marks and per-point tones.** A line splits into `segments` wherever
  consecutive x differ by more than `max_gap` (x is days from the range start) or a y is
  `None`; a single-point segment still produces a dot. `dots_all` on a line draws a dot per
  point, and the dot's shape follows the series class — `series-1` a circle, `series-2` and
  `series-4` a rect, `series-3` a polygon — so colour-vision-deficient readers can tell the
  series apart by shape as well as hue, and the line styles differ by dash pattern
  (`series-2` and `series-4` dashed, `series-3` dotted, an `avg` line stroked in its own
  `--avg-line` slate colour, dashed `10 6`, thinner at 0.85 opacity, with a matching
  `.legend-swatch.avg`, so it stays distinct from every series colour at small widths). A point
  may also be a 3-tuple `(x, y, tone)`, and the tone becomes the dot's extra class —
  `cat-ok`/`cat-warn`/`cat-danger` for the blood-pressure AHA categories — combined with
  `dot-latest` on the emphasised last dot; a 4-tuple `(x, y, tone, title)` also carries a
  human label per point, which becomes that dot's `<title>` (for example
  `25 Nov 2026 08:00 — Systolic 141 mmHg`). Every drawn line carries its label as `title`,
  rendered as a `<title>` inside each `<polyline>`, so hovering the line names the series. The
  `<svg>` itself is the single tab stop (`tabindex="0"`, it already carries the `aria-label`);
  dots and marks carry no tabindex — `chart-tips.js` moves an internal active point across the
  navigable markers (`.chart-dot:not(.avg):not(.outlier)`, `.chart-bar` — decorative outlier
  rings and average markers are skipped, and the tip hides when the active point has no
  `<title>`) with the arrow keys (Home/End jump to the ends) and marks it with
  `.chart-point-active`. A line may carry `marks` — outlier readings —
  rendered as decorative `chart-dot … outlier` rings (`fill: none; pointer-events: none`, no
  `<title>`, so the category-coloured dot underneath keeps the hover and AT text). The
  `<svg>` only gets `tabindex="0"` when the chart actually has markers (any bars, or lines with
  dots), and `chart-tips.js` no-ops on keydown when there are none. The blood-pressure category
  thresholds are data in `records/models.py` (`REFERENCE_BANDS`), never hex in Python, and each
  band also carries a `tone` class (`cat-elevated` / `cat-stage1` / `cat-danger`, with `cat-ok`
  below 120) that `_reference_tone`, `_diastolic_tone` and the category legend use for the dot
  and legend-swatch fills, so all four AHA categories are visually distinct; dots get a thin
  surface-coloured halo stroke so the fill reads clearly. Systolic and diastolic are paired by
  timestamp and both dots of a reading get the higher category (`_category_rank` +
  `RANK_TONES`), with the pair and category name in the dot's `<title>` (`141/92 mmHg —
  Stage 2`); single-component readings fall back to their own value's band. The
  y domain derives from the data (and any explicit `y_min`/`y_max`) alone, so colouring a
  point never moves the axis.
- Geometry is pure Python and stays finite: coordinates are rounded to two decimals, and a
  non-finite or missing value plots as `0` rather than `nan`.
- A bucket that only partly overlaps the range carries a note in the dict, but only the
  bucket that contains the range end and is still in progress (`end == today`,
  `partial`/`in_progress`) renders as `bar-partial`; a bucket truncated by the range start
  keeps its `partial <bucket>, N of M days` note in the bar's `<title>` and table row yet
  renders solid. The note becomes the bar's `<title>`, which is how a partial period is
  explained without a legend.
- **Tick labels omit the year** unless the range crosses a year boundary and the tick lands in
  January (`multi_year` on `_day_ticks` and `_tick_bucket_label`); full dates with the year
  stay in tooltips and tables. X tick labels anchor `start` on the first tick and `end` on the
  last (`middle` in between), so the edge labels never clip outside the card.
- **Colours come from CSS only.** Neither the Python nor the partial writes a colour: the
  only paint attribute is `fill="none"` on a polyline. The partial emits classes
  (`chart-bar`, `bar-partial`, `chart-line`, `series-1` to `series-4`, `avg`, `chart-dot`,
  `outlier`, `dot-latest`, and the category tones `cat-ok`/`cat-warn`/`cat-danger` on dots
  and `legend-swatch`es) and the palette lives in `records/static/records/app.css` — one
  variable per series (`--accent`, `--success`, `--warning`, `--series-alt`, plus `--danger`
  for the tones) declared for the light, both dark and the print palettes, each keeping at
  least 3:1 contrast on the card surface — so both themes and the print stylesheet get a
  readable chart for free.
- The SVG carries `role="img"`, a non-empty `aria-label` and a `<title>` per bar, line, dot
  and mark, so a screen reader and a printed page get the numbers even without hover. Each
  SVG also ends with a hidden `.chart-tip` group (a rect and a text); `records/chart-tips.js`,
  loaded via `{% block scripts %}` on all five dashboard templates, listens for
  `mouseover`/`focusin` on `.chart-dot`, `.chart-bar` and `.chart-line`, shows the tip above the
  shape with its child `<title>` text clamped inside the viewBox (flipping below near the top),
  and hides it again on `mouseout`/`focusout` — the native `<title>` stays as the fallback. The
  line chart renders all polylines in a `<g class="chart-lines">` first and every dot and mark in
  a separate `<g class="chart-dots">` after it, with `pointer-events: all` on `.chart-dot`, so at
  line crossings the dots always win pointer events over the strokes and the tooltip fires on
  the dot, not the line. Average-line markers (`.chart-dot.avg`) set `pointer-events: none`,
  so a lone rolling-average marker at the same coordinates as a reading never swallows its
  hover.
- The card builders (`_blood_pressure_card`, `_weight_card`, `_composition_card`,
  `_consistency_card`, `_weekly_card`, `_weekday_card`, `_extremes_card`) sit on top of those
  two functions. `_rolling_average(points)` is a single pass: points are grouped by day, sorted
  once, and a `deque` with a running sum keeps the trailing 7-day window, averaging every
  reading day from the first one on; an `avg` line only breaks where `max_gap` splits it.
  `_consistency_card` grids the resolved range, but clamps the grid to `MAX_HEATMAP_COLUMNS`
  (54) week columns — the most recent 378 days — and flags `clamped` so the card head can say
  so; `HEATMAP_DAYS` remains only as the lower bound in `_fetch_window`, so the DB fetch always
  covers the whole resolved range, including `all` (where `start` is the earliest reading).
  Date arithmetic that can approach `date.min` (`resolve_range`'s previous-window subtraction,
  `_bucket_start`, `_fetch_window`, the heatmap clamp) goes through `_days_before`, which
  clamps to `date.min` instead of raising `OverflowError`. The consistency page stacks the heatmap card
  (`_consistency_card`, full width like the blood-pressure and weight pages, with a heading
  row above the grid labeling week columns by their week-start date, thinned to every
  `step`-th column (`step = max(1, ceil(columns / 8))`, forced to at least 2 on grids of six
  columns or fewer, so labels never sit on adjacent columns), and the grid stretches to the
  card width — each week is a `.heatmap-col` flex column (`flex: 1 1 14px; min-width: 14px`)
  of square `.heatmap-grid .heat-cell`s (`aspect-ratio: 1`), so cells grow with the card up to a
  26px `max-width` and the grid centers itself (`justify-content: safe center`) when narrower,
  while the whole grid scrolls horizontally in `.heatmap-scroll` once the range is too long for
  the 14px minimum, with the `.heatmap-weeks` heading row stretching the same way and Mon–Sun
  labels on the left) above a `.report-grid` holding the
  `_weekly_card` and `_weekday_card` side by side, both on the tall 380 frame.
  `_weekly_card` bounds itself before allocating anything: `_bucket_count` counts the buckets of
  the resolved range arithmetically, and only when that exceeds `MAX_HEATMAP_COLUMNS` (54) weeks
  does it switch to month buckets (`partial_buckets(..., "month")`), clamping the window to the
  most recent 54 months if even that exceeds 54 — the full weekly bucket map is never built for
  a long range (`resolve_range` no longer carries one), the heading and table caption switch to
  "Readings per month", and an `aggregate_note` sits under the chart.
  `_day_start` converts local midnight to UTC itself and clamps to `datetime.min` UTC on
  overflow, so a `date.min` bound can never raise while preparing a query. Drill-down charts
  (`build_metric_dashboard`) draw a dot per point (`dots_all`) with a per-point `<title>`, so
  every chart's tab stop is useful. When the combined composition table is capped, the notice
  links both metrics' readings views (`/readings/?metric=…&from=…&to=…`) instead of one
  combined link.
  `_composition_card` draws the two percentage metrics in `COMBINED_COMPOSITION` (body fat
  and skeletal muscle) together on one shared percent axis when the selected metric is one
  of them — a `series-1` and a `series-4` line, so colour, dash pattern and dot shape all
  differ, each followed by its `_rolling_average()` line (`series-1 avg` / `series-4 avg`,
  dashed thinner, named in the legend) — and a single line for any other metric plus its own
  `series-2 avg` 7-day-average line
  and return the card's `chart` dict plus its `rows` for the table behind *Show the numbers*,
  capped at `CARD_ROW_LIMIT` most recent rows with a `rows_total` and a `rows_url` for the
  full list. `_dashboard_scaffold(owner, params, today, active)` builds the shared page
  context — `parse_dashboard_params` reads `range`, `metric` and the ISO dates `from`/`to`
  (invalid ones become `None`); `resolve_range` resolves them: `custom` uses `from`/`to` as
  the window, clamps the end to today, falls back to `DEFAULT_RANGE` when they are missing,
  invalid or reversed, and gives the KPI delta the equal-length window immediately before
  `from` (the previous-window subtraction is guarded by `_days_before`, so a `from` near
  `date.min` cannot raise `OverflowError`); the 7-day KPI lookbacks are clamped to their own
  periods (`seven_start = max(start, end − 6)` and the mirror for the previous window), so a
  1-day custom range compares disjoint days; `RANGE_DAYS` drives the day-based keys and
  `RANGE_VERSUS` words the KPI "previous …" comparisons; the range options (`RANGE_KEYS`, `7d` first, `custom` last) come
  with `custom_from`/`custom_to`/`query_metric` for the shared toolbar partial
  (`templates/partials/dashboard_toolbar.html`, included by all five dashboard templates:
  the range pills, where Custom is a `popovertarget` button that opens the
  `custom-range-popover` popover — a GET form with the hidden `range=custom` (plus `metric`
  when the query carries one), the two date inputs and the Apply button laid out in one row —
  opened at the full width of the toolbar bar and aligned under it (the toolbar carries
  `anchor-name: --dash-toolbar` and the popover anchors to it with `top: anchor(bottom)` and
  `left: anchor(left)` plus `inline-size: anchor-size(inline)`, so the explicit width makes
  it stretch exactly to the bar width while staying aligned under its left edge; browsers
  without anchor positioning keep the UA-centered popover, so no fallback repositions it). The
  popover is elevated with a layered
  `box-shadow: var(--shadow), var(--shadow-pop)` — `--shadow-pop` is a stronger shadow in the
  light and both dark palettes and `none` in print — and while it is open the Custom button
  takes the `.is-current` look via
  `body:has(#custom-range-popover:popover-open) button[popovertarget="custom-range-popover"]`,
  with `scroll.js` syncing the button's `aria-expanded` from the popover's `toggle` event. At
  `max-width: 640px` the toolbar keeps a single row: the pills container scrolls horizontally
  (`flex-wrap: nowrap` on the toolbar and the `.segmented`, `overflow-x: auto` with a hidden
  scrollbar and `scroll-snap-type: x proximity` on `.report-toolbar-range`) and *Print* stays
  right-aligned and unshrunk, so the page never overflows horizontally; the
  composition switcher (`COMPOSITION_SWITCHER`: one *Body fat & muscle* pill for the
  `COMBINED_COMPOSITION` pair, marked current for either of its two `?metric=` values, plus
  the three single-metric pills), the
  `dashboard_nav` sub-navigation (whose links keep `range` and
  `metric` — and, for a custom range, `from`/`to` — via `filtered_url` and `_range_query`),
  the back link and the window bookkeeping. Each of the five dashboard templates loads
  `records/print.js` and `records/scroll.js` in its `{% block scripts %}`; `scroll.js` stores
  `window.scrollY` under `omron-scroll:<pathname>` in `sessionStorage` when a link to the same
  path is clicked and restores it on `DOMContentLoaded`/`pageshow`, so switching range pills
  or composition pills does not jump back to the top — and
  `build_dashboards(owner, params, today)` (overview: KPIs, section summary cards,
  extremes), `build_dashboard_weight(...)` (weight + composition cards),
  `build_dashboard_consistency(...)` (heatmap + weekly + weekday cards) and
  `build_metric_dashboard(owner, metric, params, today)` (drill-down; `metric == PANEL` is
  special-cased to the blood-pressure panel page, and `body_weight` gets a second,
  `series-2 avg` 7-day-average line with a legend and an `average` column) assemble the whole
  page context without a request object, so a page test and a shell check can call them
  directly. The overview's `build_dashboards` stays light: it computes only the KPI cards, the
  section summary cards (from the in-window latest BP pair and latest weight) and the extremes
  list — no chart dicts. `last_reading_days` is measured against the real `today`, not the
  range end, so a historical custom range reports the true age. The readings view reuses the
  same guarded `_day_start` for its `from`/`to` query bounds, so `?from=0001-01-01` cannot raise
  while converting local midnight to UTC. The composition card's
  `links`/`rows_url` point at `/readings/?metric=…&from=…&to=…` — the paginated readings view
  filtered to the metric and the resolved window — not the 500-row-capped drill-down.

`tests/test_reports.py` renders the partial with `render_to_string` and asserts the key set,
the finite coordinates, the domain rules (a 131–132 kg series stays inside 126–136, a flat
series keeps its height, a single point still draws a dot, a high point gets a data-only
axis, gap splitting, per-point dot tones), the custom-range parsing and resolution (valid,
clamped, reversed and invalid `from`/`to`), and that no `nan`, `inf` or
`None` reaches the markup. `tests/test_dashboards.py` covers the pages: the sub-navigation
and its active item on every page, the range chips, their default and the Custom button with
its popover date form on all five pages, the six KPI numbers
and their units, the blood-pressure page with its two panels, category-coloured dots, the
category legend and the latest category, the weight page and the weight drill-down with the
7-day average
line and column, the composition card with its combined body-fat and skeletal-muscle chart
and its per-series 7-day average lines (or one other metric at a time, with its own average
line) and its domain, the hover `<title>`s on every dot, line and bar plus one tab stop per
chart with arrow-key point navigation, the scroll and chart-tip scripts loaded on all five
dashboard pages, the table
caps, the full-width heatmap card with its thinned week headings and the side-by-side
weekly and weekday bar cards on the consistency page (also for a custom
window), the heatmap reshaping with the selected range, the heatmap's hidden summary, the rolling average starting at the first reading day, a single-point line
rendering its lone dot, every SVG carrying a
role and a label, that no markup carries a literal colour, a `#hex` or a raw float, that the
drill-down is a 404 for an unknown metric, and that the page stays under forty queries.
`tests/test_shell.py` checks the category-dot contrast (≥ 3:1 on the card surface) and the
chart-series contrast (≥ 3:1 on the card surface) in the light, both dark and the print
palettes, alongside the base template and navigation.

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
| `test_dashboards.py` | The dashboards pages: sub-navigation, range chips, KPIs, the section pages, the blood-pressure page, the weight drill-down, the heatmap, print |
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
