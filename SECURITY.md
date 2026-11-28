# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub advisory reporting:

<https://github.com/MartinWa/omron-data/security/advisories/new>

Do not open a public issue for a security problem. Include the affected version,
the impact and a minimal reproduction. Reports are acknowledged within 7 days;
fixes are coordinated privately and the advisory is published after disclosure.

This service stores a person's health history, so a leak is not a small bug. Two
kinds of report are serious enough to send privately straight away:

- a token that is shown, logged or resolved to the wrong owner, or a query that
  returns another owner's rows
- a path traversal in the import path: the directory `import_omron` is pointed at,
  or a page that takes a path from the request

A report is easier to act on with the tree clean on your side, so run
`uv run pytest -q` and `uv run ruff check .` before you send it. A fix that arrives
as a pull request has to pass the same two commands.

## Personal data

Health records are personal data and this repository is public, so it must contain
none of it:

- no real OMRON connect exports, blood pressure or body composition readings,
  dates or notes
- no real names, accounts, e-mail addresses, tokens or token hashes
- no real hostnames, volume paths, usernames, groups or UIDs
- no database or backup files

Fixtures, screenshots, examples and documentation are wholly synthetic; redacting
real data is not sufficient. An optional local parity check reads a directory of
real exports named by `OMRON_LOCAL_CSV` and is skipped when unset; committed tests
never touch a real file. `.gitignore` excludes `.csv`, `*.sqlite3`, `.data/`,
`planning/` and `reference/`, so a commit that carries one of them got there past
the ignore rules and is worth reporting.

## Self-hosting

Run the app behind a TLS reverse proxy; it publishes no ports. Set
`OMRON_SECRET_KEY` and `OMRON_ALLOWED_HOSTS`, and keep the SQLite database and its
backups private: `OMRON_DB_PATH` decides where the file lives, `.data/db.sqlite3`
by default, and that directory is the thing to protect. Mark a token you no longer
need as revoked instead of leaving it live. Login is rate-limited in the app: five wrong
passwords for one username from one client address lock that pair out for an hour, and both
the failures and the lockouts are logged and stored in the same database.

Never paste a real export into an issue, a pull request or a paste site, not even a
single row: one export is a person's whole history for the range it covers, and its
dates identify them.
