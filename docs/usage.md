# Usage

omron-data takes the CSV that the OMRON connect app exports and merges it into one
private history per person, then reads that history back through four browser pages
and a read-only JSON API. The app is the only way to get the measurements out and it
exports a date range at a time, so an import is a window that adds to what is already
stored rather than replacing it. This page follows that order: the export, the import,
what the import reports, and then each page in the UI.

## Accounts

Every account is created by an administrator; see [accounts](deployment.md#accounts-and-tokens).
Log in at `/accounts/login/`. *Remember me* is ticked by default; clear it and the
session ends when the browser closes. There is no self-service registration and no
password reset by e-mail.

**One owner per person.** Readings, import runs and API tokens all belong to an
account, and no page or endpoint ever returns another account's rows. Import for the
account that belongs to the person the export came from, and never let two people
share one account: the history of a shared account is the union of both, and a purge
made for one person deletes rows that came from the other.

The UI is available in a light and a dark theme. It follows your operating-system
setting until you use the sun or moon button in the header, which pins one theme for
that browser. The choice is stored in the browser only, not on the account.

## Get the export out of the app

1. Open the OMRON connect app and pair your blood pressure monitor or scale.
2. Open *Settings*, then *Data export*. Some versions label it *Export data* or
   *CSV export*.
3. Save the CSV file the app hands you and remember the range it covers; the file is
   usually named after that range.

The export is written in the app's Swedish locale: `25 nov. 2026` dates, decimal
commas and percent signs inside values (`34,3%`). One file holds several sections,
each with its own header row, and the same reading can appear in two of them; both
collapse onto one measurement per timestamp when imported. A cell the app leaves as
`-` is not stored.

An export is accepted when it is UTF-8 text (a byte-order mark is fine), no larger than
10 MB, and carries at least one importable row. A file whose headers are all unrecognised
is refused at upload with the parser's message rather than imported as empty, and one with
no rows is refused with *CSV export contains no importable rows*. Nothing is written to the
database until you confirm the preview.

## Import a window

Uploads through the browser never write a reading; they stage the file and show what
the import would do.

1. **`/imports/`** — choose the CSV in the *Import a CSV export* card, or drag it onto
   it, then *Preview*. The file is staged in a private temporary directory outside the
   database and outside the repository.
2. **`/imports/preview/`** — the counts the import would produce, an *already
   imported* notice when the same file has been imported before, a warnings section,
   and the first ten readings in the file with the rest behind *Show all*. Nothing
   has been written at this point, and reloading the page writes nothing either.
3. **Confirm import** applies the whole file, not only the sampled readings, and
   returns to the imports page with a one-line summary. **Cancel** discards the
   staged file, reports *Preview discarded. Nothing was imported.* and writes nothing.

A preview expires one hour after the upload. Uploading again replaces the pending
preview, so a second tab cannot confirm the older file; that case is reported as
*This preview is stale. Upload the CSV again.* and nothing is imported.

The **Replay** checkbox appears on the preview only when it can make a difference: the
file's checksum is already in the import ledger, or the import found at least one
skipped or conflicted row. With it ticked, the file is imported again without
replacing a single stored value, so it can only fill gaps. Without it, confirming an
export that is already in the ledger changes nothing.

## What an import reports

Counts are per file and in total, on the preview page and in the command-line output:

```
Rapport (20 juni, 2024 – 1 juli, 2024).csv: 9 readings, 9 created, 0 skipped, 0 conflicts, 0 overwritten, 3 new panels
```

| Count | Meaning |
| --- | --- |
| `readings` | Readings the file carried, after the two body sections collapsed onto one |
| `created` | Rows written for the first time |
| `skipped` | Rows already stored with the same value, and a reading the file itself carried twice; nothing was written |
| `conflicts` | Rows whose value differs from the stored one; the stored value was kept |
| `overwritten` | Rows the file replaced, counted only with overwrite enabled |
| `panels` | Blood-pressure events created by this import |
| `invalid` | Timestamps the file names that do not exist in the configured time zone; not imported |

**A conflict is reported rather than applied.** Every import is a snapshot of a window
and windows overlap, so a second look at the same moment can carry a different number:
a corrected reading in the app, a different device, or a file that is simply older than
what is already stored. Silently taking the newer file would let an old export rewrite
a value you already checked, so the stored value wins and the disagreement is printed
where you can see it:

```
  2024-06-25 07:30 systolic_bp: stored 128, file 129
```

The preview lists the same disagreements with the stored and the incoming value side by
side. `--overwrite` on the command line applies the file's values instead and counts
them as `overwritten`; the browser has no overwrite button, by design.

**`invalid`** is not a bad reading but an impossible moment: a wall-clock time that the
configured zone skipped, such as the hour a daylight-saving change removes. Those rows
are listed with the zone name so you can check that `OMRON_TIME_ZONE` matches the app.
Each distinct timestamp counts once.

## Overlapping windows and the ledger

Imports are additive and idempotent. One row is stored per owner, metric and timestamp,
so an export that overlaps an earlier one fills in what is new and leaves the rest
alone; importing the same file twice changes nothing. Deleting a reading in the app does
not delete it here, which is the point of keeping your own copy.

Every import is recorded in a ledger: the file name, its SHA-256 checksum, how many
readings it carried, the four counts and the span it covered. A file whose checksum is
already in the ledger is not parsed again, which makes a repeated import free and
answers "have I imported September yet?".

Two situations want a known file anyway: a purge may have removed readings that a later
window also contains, and a newer version of this tool may understand columns that older
imports dropped. `--replay` imports it again without overwriting anything, so it can
only fill gaps.

## The readings page

`/readings/` is the home page: one card per event, newest first, 25 events per page. The
line above the list states how much is stored, for example *12 readings in 4 events
stored for your account*, and adds how many events match the filters when any are set.

An **event** is one moment. Everything measured at that timestamp is grouped into one
card: a blood-pressure reading, a body-composition reading, or both when the app
recorded them at the same minute. The card header carries the date and time, the
event's title, its note when it has one, and one badge per group. An event with a single
metric is titled with that metric; a scale reading that carries several is titled
*Weigh-in*, and its rows sit one per line rather than under a repeated heading.

**A blood-pressure event is a panel with components.** The panel is the event itself and
holds the note the app wrote; its components are the systolic, diastolic and pulse rows
taken at that moment, because they describe one measurement and are useless apart. The
card's badge shows the pair (`130/85 mmHg`), and expanding the card lists the components
in a table with metric, value, unit and note. The `blood_pressure` row carries no value
of its own; the numbers live on the three components. Body composition and body
temperature are plain readings, one row per metric.

| Filter | Effect |
| --- | --- |
| *Metric* | One metric from the registry, or *All metrics*. Choosing a component (`Systolic`, `Pulse`) keeps the whole panel that contains it, so the event is never shown as a bare number |
| *From* / *To* | Inclusive local dates. *From* starts at midnight, *To* runs to the end of that day |
| *Search* | Case-insensitive substring of the note; it does not search numbers or file names |

The filters combine, survive paging, and are repeated in the URL, so a view can be
bookmarked. *Clear* removes them. The form submits itself when a field changes and keeps
a *Filter* button for a browser without scripting. When nothing matches, the page says
so and offers *Clear filters*; an account with no readings at all is told to import a
CSV export instead.

Printing from the browser hides the header, the filters, the pager and the messages,
opens every card, and prints on a white sheet even in the dark theme.

## The imports page

`/imports/` holds both halves of importing: the **Import a CSV export** card at the top and
the ledger of past runs below it, newest run first and 25 runs per page. Choosing a file in
the card, or dropping one on it, posts it to the same page, which stages it and sends you to
the preview. Nothing is written here.

| Column | Meaning |
| --- | --- |
| *File* | Name of the CSV that was imported |
| *Imported* | When the run happened |
| *Readings* | Readings the file carried |
| *Created*, *Skipped*, *Conflicts*, *Overwritten*, *Panels* | The counts described above; a non-zero conflict count is badged |
| *Covered* | First and last timestamp in that file, or `—` when the file carried none |
| *Actions* | *Purge*, behind a second confirmation |

**Purge** deletes exactly the measurements one run created, then the run itself, so a
wrong file is undone without disturbing anything imported before or since. It asks for a
tick in a confirmation box first, and detaches a component that another run created instead
of deleting it. Readings shared with a later window stay missing until that window is
imported again with replay; the command-line equivalent, `purge_import`, prints that as a
reminder.

Below the ledger, the **Coverage** card summarises what is stored: readings, events, the
first and last reading, and one row per metric with its count, its first and last
timestamp and every gap longer than seven days, expandable per metric. Readings here
exclude the blood-pressure panel, which groups the systolic, diastolic and pulse values
taken at one moment.

## The tokens page

`/tokens/` creates and manages the secrets the API accepts. Only a SHA-256 digest and
the first eight characters are stored.

| Action | What happens |
| --- | --- |
| Create | Give the token a name you will recognise; the name is unique per account. The secret is shown once, with a *Copy* button, and cannot be recovered afterwards. *Expires in days* is optional: leave it empty for a token that does not expire on its own |
| Revoke | Clients using it stop working on their next request. The row stays, so you keep the record of what existed |
| Delete | Only offered once a token is revoked or expired, and removes the row for good so the name can be used again |

A working token cannot be deleted: revoke it first. An expired token needs no revoke; it
is refused by the API and can be deleted straight away. Replace a lost secret with a new
token rather than trying to recover the old one. See [API](api.md) for how a request
authenticates.

## The dashboards pages

`/dashboards/` turns the stored history into six KPI cards, three section cards that summarise
and link to the dedicated pages, and a list of extremes. Every number on every page comes from
the stored rows, so a reading you have not imported cannot appear in a chart. The line under
the heading states the window, how many readings it holds, how long ago the newest of them was
taken (*last reading today*, or *last reading N days ago*) and when the last import ran. Every
chart element carries its numbers as a hover title — each line names its series, each dot its
reading with date, time and value, each bar its bucket and its count — and a visible tooltip
follows the mouse or the keyboard focus over every dot, line and bar, while the dots are
keyboard-focusable, so the same text is reachable without a mouse.

### The sub-navigation

Every dashboards page carries a sub-navigation under the page head — *Overview*,
*Blood pressure*, *Weight & composition* and *Consistency* — drawn with the same segmented
control as the range chips. Its links keep the current `?range=` and `?metric=` parameters, so
switching sections does not lose the selected window, and the current page's item is marked
with `aria-current="page"`. The *Dashboards* item in the main navigation stays active on all
of these pages.

| Page | Shows |
| --- | --- |
| `/dashboards/` | The KPI row, the three section cards (blood pressure: latest pair and AHA category; weight & composition: latest weight and its delta; consistency: readings in range) with links to the pages below, and the extremes list |
| `/dashboards/blood_pressure/` | The blood-pressure panel: systolic and diastolic with dots coloured by AHA category and outlier rings, the pulse chart on its own scale, legends, and the capped data table |
| `/dashboards/weight/` | Two cards stacked full-width one under the other, like the blood-pressure page: the weight card (daily line plus the dashed 7-day rolling average, legend and table), then the body-composition card — body fat and skeletal muscle together by default behind one *Body fat & muscle* pill, with the switcher for the other metrics |
| `/dashboards/consistency/` | The full-width consistency heatmap card (week headings above the columns, weekday labels on the left, its own table), then the readings-per-week and readings-per-weekday cards side by side in a two-column grid, each with its table |
| `/dashboards/<metric>/` | The drill-down for any other metric, described under [the metric drill-down](#the-metric-drill-down) |

### The range control

Six chips select the window: *Last 7 days*, *30 days*, *90 days*, *1 year*, *All time* and
*Custom*, with 30 days as the default. The fixed windows are the time up to today, so
*Last 7 days* means today and the 6 days before it, and *30 days* means today and the
29 days before it. The choice lives in the URL (`?range=7d`) and survives the
sub-navigation, the body-composition switch and a drill-down, so a view can be bookmarked.
Because those switches are same-page links, the page remembers the scroll position and puts
you back where you were after the reload. An unknown value falls back to the default.

The *Custom* chip is a button that opens a panel as wide as the toolbar bar, with the two date
fields (FROM/TO) and an *Apply* button in a row (wrapping on narrow screens); applying submits
`?range=custom&from=YYYY-MM-DD&to=YYYY-MM-DD` as a normal page load, while Esc or a click
outside closes the popover without changing anything. While a custom window is active the
chip names it, for example *Custom · 10 – 24 Nov 2026*. The end is clamped to
today, and a missing, invalid or reversed `from`/`to` falls back to the default window.
The popover's date fields carry the current window's start and end (last 30 days when no
window is selected yet), and the other chips drop `from`/`to` from their links. A custom window's KPI
deltas compare against the window of the same length immediately before `from`, named
*versus the previous period* for screen readers. A window that holds no readings shows a
notice with a *Show all time* link instead of empty charts, and an account with no readings
at all is sent to the import page. KPI deltas name their comparison out loud for screen
readers — *versus the previous 7 days*, *versus the previous 30 days* and so on.

### What each card shows

| Card | Page | Shows |
| --- | --- | --- |
| *Latest blood pressure*, *7-day average*, *Latest weight*, *Weight change*, *Readings*, *Last import* | `/dashboards/` | The KPI row. Every value carries its unit and the precision the metric deserves: whole mmHg for blood pressure, one decimal for kilograms and percentages. The *7-day average* adds a sub-line, *based on N readings*, and compares the systolic 7-day mean with the mean of the last 7 days of the previous window as an absolute `+20 mmHg`-style delta; *Latest weight* and *Weight change* compare kilograms (`−0.4 kg`); *Readings* stays a percentage. A delta is coloured by what it means, not just by its arrow: red when the move is worse (a rise in blood pressure), green when it is better, grey when the metric carries no good or bad direction (weight, counts). The comparison is spelled out for a screen reader. *All time* has no earlier window, and *Weight change* is the first reading against the last inside the range |
| *Blood pressure* | `/dashboards/blood_pressure/` | Two panels sharing one x axis. The first draws systolic and diastolic as lines with a dot per reading — each dot coloured by its AHA category (below), so the axis follows the data alone — a dashed 7-day rolling average of each that starts at the first reading, and a legend naming the lines and the category colours; readings at or above 140 systolic or 90 diastolic also get a ringed outlier dot. The second draws the pulse alone on its own `/min` scale. Axes for whole-number units (mmHg, `/min`, kcal, ohm) tick in whole numbers only. The card head names the latest pair and its AHA category, for example *Latest 129/73 · AHA category: Normal* |
| *Weight* | `/dashboards/weight/` | One line of daily weigh-ins with a dot per reading, plus the dashed 7-day rolling average, which starts at the first reading in the range (a trailing 7-day window of the available readings), with a legend naming both lines. The last reading gets an emphasised dot. Both lines break only across gaps longer than 7 days |
| *Body composition* | `/dashboards/weight/` | By default *Body fat* and *Skeletal muscle* are drawn together on one chart — both are percentages, so they share one percent axis, and a percentage never shares an axis with a kilogram. The two lines differ in colour (teal versus amber), dash pattern and dot shape (solid circles versus dashed squares), each is followed by its own dashed, thinner 7-day rolling average line, and the legend names all four (*Body fat 7-day average (%)* and *Skeletal muscle 7-day average (%)* among them), and the table below lists date, body fat, skeletal muscle and note per reading, with links to each metric's readings page filtered to the selected window. The switcher in the card head has one *Body fat & muscle* pill for the pair — marked current whether the URL carries `?metric=body_fat_pct` or `?metric=skeletal_muscle_pct`, both of which show the same combined chart — plus *Visceral fat*, *Resting metabolism* and *Impedance* one at a time; the choice is in the URL (`?metric=`). Each single metric plots against the fixed display domain in the table below with its own dashed *<metric> 7-day average* line named in the legend; when the data falls outside the domain the domain widens instead of clipping the line |
| *Consistency* | `/dashboards/consistency/` | A full-width heatmap of the selected window — the range chips and custom range reshape it, and *All time* spans the whole history — one cell per day in four levels from none to three or more readings — the square cells grow with the card up to a 26px cap and the grid centers itself when narrower, while a long range keeps the 14px minimum and scrolls horizontally; the grid is capped at 54 week columns (the most recent 378 days of the range), noted in the card head — with a heading above the grid labeling week columns by their week-start date (thinned so the labels never collide, never on adjacent columns of a small grid) and weekday labels (Mon–Sun) on the left. The card head names the window with its counts. Hovering a cell gives its date and count; a visually hidden paragraph summarises the whole heatmap for a screen reader, and the table below lists the days that have readings |
| *Readings per week* | `/dashboards/consistency/` | One bar per week with the number of readings in it; only the current week, still in progress, is shaded — a week truncated by the range start renders solid, though its title and table note still say how many of its days fall inside the range. Bars keep a zero baseline, because a bar is a magnitude and every bar left on the page is a count. A range longer than 54 weeks aggregates the bars per month (capped to the most recent 54 months, noted under the chart). Sits side by side with the weekday card below |
| *Readings per weekday* | `/dashboards/consistency/` | One bar per weekday, Monday to Sunday, with the number of readings taken on that weekday in the selected window, and a table listing the same seven counts |
| *Extremes* | `/dashboards/` | Latest, high and low for each metric, each value with its unit and its date, plus how many readings it is based on. A metric with a single reading shows only the latest, without repeating it as high and low. Each name links to that metric's drill-down |

### Blood-pressure reference

Each dot on the blood-pressure chart is coloured by the AHA/ACC 2017 category of its reading,
from
[Understanding Blood Pressure Readings](https://www.heart.org/en/health-topics/high-blood-pressure/understanding-blood-pressure-readings):
green for normal (systolic below 120), yellow for elevated (120–129), orange for stage 1
(130–139), red for stage 2 (140 and above), with the diastolic equivalents 80/90 mmHg — so the
y axis
follows the data alone and one high reading never stretches the chart. So that the categories
never depend on colour alone, a legend under the chart names every category with its colour
(*Normal <120 mmHg*, *Elevated 120–129 mmHg*, *Stage 1 130–139 mmHg*, *Stage 2 ≥140 mmHg*),
both dots of a reading share that reading's category, each dot keeps its hover text with the exact pair and category (e.g. *141/92 mmHg — Stage 2*), and a visible tooltip follows the mouse or the arrow keys — each chart is a single tab stop. A reading's category is the higher of
its systolic and its diastolic category (a crisis is above 180 systolic or 120 diastolic), and
the card head reports the latest reading's category on the same scale.

A home blood-pressure average of 135/85 mmHg or more over seven days corresponds to an office
reading of 140/90 mmHg under the 2017 ACC/AHA guideline
([Whelton et al., Hypertension](https://www.ahajournals.org/doi/10.1161/HYP.0000000000000065)),
which is the threshold the *7-day average* KPI is read against; the note under the chart says
so.

### Composition display domains

These are display domains that frame the chart so ordinary variation fills the panel; they are
not medical norms, and the domain widens when a reading falls outside it.

| Metric | Domain | Why that band |
| --- | --- | --- |
| Body fat | 20–45 % | The band that covers lean to high adult values on the scale |
| Skeletal muscle | 15–45 % | The band that covers low to high muscle percentages the scale reports |
| Visceral fat | 1–20 | The low end of the device's rating scale, where the differences that matter happen |
| Resting metabolism | 1500–2500 kcal | The typical adult basal-metabolism band |
| Impedance | 200–600 ohm | The typical whole-body bioimpedance range at the scale's frequency |

### Table caps

Below each chart, *Show the numbers* expands the same data as a table, which is also what
prints. The blood-pressure, weight, composition and readings-per-week tables list at most the
200 most recent rows and say so (*Showing the 200 most recent of N …*) with a link to the full
list: the readings page filtered to that metric for blood pressure and weight, the readings
page for the weekly card, and the readings page filtered to that metric and the selected window for composition. *Print* calls the
browser's print dialog; printing hides the navigation, the sub-navigation, the toolbar and the
range chips and opens every data table, so the page prints as a report even in the dark theme.

### The metric drill-down

`/dashboards/<metric>/` follows one metric with the same range chips: a line chart of every
reading in the window — a dot per reading, one tab stop for the chart, arrow keys stepping across the points — with the same display domain as the composition card for those five
metrics, and lines that break across gaps longer than 7 days — its highest, lowest and latest
value with unit, and *Show the numbers* with the readings themselves, newest first, value and
note. The table lists the 500 most recent and says how many are hidden beyond that; the chart
is not capped. An unknown metric in the path is a 404, and a metric with no readings in the
selected window says so and offers *All time*.

Two slugs are pages of their own rather than generic drill-downs: `/dashboards/blood_pressure/`
renders the blood-pressure panel described above (the panel rows themselves carry no value, so
the page is built from the systolic, diastolic and pulse components), and
`/dashboards/body_weight/` draws two series — daily weight and the dashed 7-day rolling
average (starting at the first reading, breaking only across gaps longer than 7 days), with
a legend — and adds a *7-day average* column to its table, exactly like the
weight card on `/dashboards/weight/`.

## From the command line

The browser is not the only way in. Every command below is run from the repository root
of a working copy, or through `docker compose exec web` in a deployed container; see
[deployment](deployment.md#configuration) for the container form.

| Command | What it does |
| --- | --- |
| `uv run python manage.py import_omron PATH --owner alice` | Import every `*.csv` in a directory (or one file) for one owner. `--create-user` creates the account if it does not exist |
| `import_omron PATH --owner alice --dry-run` | Report the counts and write nothing |
| `import_omron PATH --owner alice --replay` | Import a file whose checksum is known, without overwriting stored values |
| `import_omron PATH --owner alice --overwrite` | Apply the file's values where they differ from what is stored |
| `uv run python manage.py coverage --owner alice` | Per metric: count, first, last, and every gap longer than seven days |
| `uv run python manage.py purge_import --id 4 --owner alice` | Delete the rows one import run created, then the run. `--dry-run` reports the row count first |
| `uv run python manage.py make_token --owner alice --name laptop` | Issue an API token and print the secret once |

`--replay` and `--overwrite` cannot be combined: replay only fills gaps, overwrite
replaces stored readings. A batch stops at the first file it cannot parse and reports
what was imported before the error, so a failed file never leaves a half-written window
behind.
