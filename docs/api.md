# API

The API is read-only, versioned and owner-scoped, served at `/api/v1/`. It exposes the
same rows the pages render: blood-pressure panels with their systolic, diastolic and pulse
components, the standalone body-composition and temperature readings, per-metric coverage
and the import ledger. Every response carries a contract envelope, every query is scoped
to the token's owner, and every route answers `GET` and `HEAD` only. The generated OpenAPI
schema at `GET /api/v1/openapi.json` describes the paths, parameters and response shapes,
and a Swagger UI renders it at `GET /api/v1/docs`; this page covers what a schema cannot
express.

## Authentication

`GET /api/v1/healthz` is the only data endpoint that needs no token. The two generated
reference endpoints, `/api/v1/docs` and `/api/v1/openapi.json`, are also anonymous, because
a browser has to be able to fetch them without a header. Everything else requires a bearer
token:

```sh
curl -H "authorization: Bearer $TOKEN" http://localhost:8000/api/v1/summary
```

- The token resolves to an account, and **every** queryset is filtered by that account. No
  user identifier appears in any URL.
- Only a SHA-256 digest of the secret and its first 8 characters are stored, so a database
  copy does not hand anyone a working token. The 8-character prefix is what the tokens
  page shows beside the name.
- A missing, malformed, unknown, revoked or expired token is a `401` with
  `error.code: "invalid_credentials"` and `WWW-Authenticate: Bearer`. So is a token whose
  account has been deactivated; reactivating the account makes the same token work again.
- Secrets are created on `/tokens/` in the UI or with `manage.py make_token`, and are shown
  once. A token may carry an expiry date and stops working when it passes. See
  [usage](usage.md#the-tokens-page).

## Envelope

Every response, errors included, carries `contract_version` (the string `"1.0"`) and
`generated_at`, the UTC instant the response was produced. Additive fields keep this
version; a breaking shape change needs a new one. A client can therefore tell a contract
change from a data change.

Collections add `items`, the complete filtered `count`, the `next` and `previous`
continuation paths and `has_more`. Those paths keep every other query parameter, so
paging never loses a filter.

Errors add an `error` object with `code`, `message` and `details`; `details` is `null` when
there is nothing more to say, a list of offending parameter names when there is:

```json
{
  "contract_version": "1.0",
  "generated_at": "2026-11-26T16:25:21Z",
  "error": {
    "code": "validation_error",
    "message": "Unknown query parameter: foo",
    "details": ["foo"]
  }
}
```

| Status | `error.code` |
| --- | --- |
| 400 | `bad_request` |
| 401 | `invalid_credentials` |
| 403 | `permission_denied` |
| 404 | `not_found` |
| 405 | `method_not_allowed` |
| 422 | `validation_error` |
| 500 | `internal_error` |
| 503 | `service_unavailable` |

`500` is what an unexpected failure returns, with the traceback in the server log and not
in the body. `403` and `503` are defined for a permission check and a transient condition
that no endpoint produces today.

Every response, including every refusal, sets `Cache-Control: no-store`.

## Timestamps

Timestamps are ISO 8601 in UTC with a trailing `Z` and whole seconds, for example
`2024-06-20T06:00:00Z`. Query parameters need an offset (`+02:00` or `Z`) and are
converted; a bare date or a datetime without an offset is rejected.

The export records wall-clock times with no offset, so they are absolute only relative to
the installation's `OMRON_TIME_ZONE`, which is read at import time. A client should not
assume the `Z` value equals the clock in the export. Values are JSON numbers, truncated to
the stored three decimals; a missing value is `null`, never `0`. A blood-pressure panel
has no value of its own, so its `value` is `null` and its numbers are on its components.

## Endpoints

| Path | Parameters | Returns |
| --- | --- | --- |
| `GET /api/v1/` | none | the contract version, the service name and the endpoint list |
| `GET /api/v1/healthz` | none | `status: "ok"`; the only data endpoint that needs no token |
| `GET /api/v1/metrics` | none | the metric registry: `code`, `unit` and `kind` for all 16 codes |
| `GET /api/v1/measurements` | `metric`, `since`, `until`, `level`, `include`, `sort`, `limit`, `offset` | a page of readings, `level=panel` by default |
| `GET /api/v1/measurements/{id}` | none | one row plus its components |
| `GET /api/v1/summary` | none | per metric: `count`, `first`, `last` |
| `GET /api/v1/imports` | `limit`, `offset` | the import ledger, newest first, plus the stored span |
| `GET /api/v1/docs`, `GET /api/v1/openapi.json` | none | the generated reference and its OpenAPI 3.1 schema |

`kind` in the registry is `panel` for `blood_pressure`, `component` for `systolic_bp`,
`diastolic_bp` and `pulse`, and `reading` for the other thirteen codes.

`summary` returns one row per metric that has data, sorted by metric code, so a code with
no readings is absent rather than zero. `imports` returns one row per import run with the
file name, the checksum, the counts, the span that file covered and when it was imported;
the page itself also carries `first_at` and `last_at`, the bounds of everything stored for
the owner.

`/api/v1/measurements/{id}` returns the row plus a `components` list: three rows for a
blood-pressure panel, an empty list for anything else. A component's `parent_id` points at
its panel. Another owner's id is a `404`, not a `403`.

## Parameters

| Parameter | Default | Accepted |
| --- | --- | --- |
| `metric` | none | A code from the registry. Empty is the same as omitting it |
| `since` / `until` | none | An ISO 8601 datetime with a UTC offset, inclusive at both ends |
| `level` | `panel` | `panel`, `component` or `all` |
| `include` | none | `components` or `true` to inline a panel's components; `false` is the default behaviour |
| `sort` | `-at` | `at`, `metric`, `value` or `id`, optionally prefixed with `-` for descending |
| `limit` | `100` | 1 to 1000 |
| `offset` | `0` | 0 to 2147483647 |

`imports` accepts `limit` and `offset` only. The routes with no parameters listed above
(`/`, `/healthz`, `/metrics`, `/summary`, `/measurements/{id}`) reject **any** query
parameter with a 422 rather than ignoring it.

### `level` and `include`

- `level=panel` returns top-level rows: blood-pressure panels and standalone body
  readings. This is the default and what a reader of the history usually wants.
- `level=component` returns only the systolic, diastolic and pulse rows that belong to a
  panel.
- `level=all` returns both, so a page of it mixes panels with their own components.
- `include=components` adds each panel's components to the item itself, saving a request
  per panel; a component item never carries components of its own.

A component metric is only meaningful at component or all level, and the panel metric only
at panel or all level; asking the other way round is a 422 with the reason, not an empty
page.

### `sort` and paging

Ties are always broken by `id` ascending, so a page boundary cannot move between requests.
With a descending sort, null values sort last; with an ascending sort they sort first.
`limit` and `offset` page the result and the envelope's `next`/`previous` carry the
continuation, keeping the other parameters in place.

### The time window

`since` and `until` are both optional, and when only one is given it is the only bound.
When both are given, `since` must be less than or equal to `until` and the span may not
exceed **1830 days**, about five years. A client that wants more has to page through
windows.

## What is a 422

Filtering is strict: no value is coerced, clamped or silently dropped. These are the
conditions and the message each produces. All of them are `422` with
`error.code: "validation_error"`.

| Condition | `error.message` |
| --- | --- |
| A parameter the endpoint does not accept | `Unknown query parameter: foo` |
| The same parameter twice | `Repeated query parameter: metric` |
| A metric code that is not in the registry | `Unsupported metric: systolic` |
| A component metric at panel level | `systolic_bp is a component; use level=component or level=all` |
| The panel metric at component level | `blood_pressure is a panel; use level=panel or level=all` |
| A level other than the three | `Unsupported level: event` |
| A sort key other than the four | `Unsupported sort key: note` |
| `limit` below 1 or above 1000 | `limit must be between 1 and 1000` |
| A negative `offset` | `offset must be non-negative` |
| An `offset` above 2147483647 | `offset must be at most 2147483647` |
| `since` later than `until` | `since must be less than or equal to until` |
| A window wider than 1830 days | `range may not exceed 1830 days` |
| A date or a datetime without an offset | `Validation failed`, with the reason in `details` |
| A non-integer `limit` or `offset` | `Validation failed`, with the reason in `details` |
| An `include` value other than the three | `Validation failed`, with the reason in `details` |

The first twelve name the offending value or parameter in `message`; the rest come from
the parameter parser and carry a per-field explanation in `details`.

## Refusals

- Any method other than `GET` or `HEAD` on an API path is a `405` with
  `error.message: "This API is read-only"`. `HEAD` is answered like `GET`, including the
  envelope and `Cache-Control: no-store`.
- An unknown path under `/api/v1/` is a `404` with the same envelope, not an HTML error
  page.
- A `404` is also what another owner's measurement id returns, so the API never confirms
  that a row exists for somebody else.
