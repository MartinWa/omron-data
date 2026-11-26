# Contributing

Thanks for helping. How to run the project and its checks is in the
[README](README.md#run-it); the layout and the agent rules are in
[AGENTS.md](AGENTS.md). This page covers the rules and the paperwork.

## Before opening a pull request

Run these from the repository root and make sure they pass. Python is pinned by
`.python-version`, and `uv sync` creates the environment the rest of them use.

```sh
uv sync
uv run python manage.py migrate
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

Discuss larger changes in an issue first.

## Rules

- Use Django migrations for every schema change.
- Every fixture and test file must be wholly synthetic. Write an export-shaped CSV
  by hand for a test; never copy a real one, not even a few redacted rows.
- `planning/` and `reference/` are private and never committed. Keep their contents
  out of the tree and out of a patch.
- Never commit a CSV, a database file or a token. [SECURITY.md](SECURITY.md) says
  what counts as personal data and why it matters more here than in most projects.
- New behaviour needs a test, and code stays free of comments unless a change
  explicitly requires them.
- Commit messages read `Area: imperative summary`, for example
  `parsing: reject a file with no recognised header`.

## Contributor agreement

Before your first pull request can be merged, sign the
[Contributor License Agreement](CLA.md). Signatures are collected by
[CLA Assistant](https://cla-assistant.io), a free GitHub app, and stored outside
this repository, so contributor accounts never enter it; click the signing link
once and the signature covers your future contributions too. You keep the
copyright in your contribution.

The agreement matters because the project is dual licensed: it is published under
the PolyForm Noncommercial License 1.0.0 and also offered commercially, and the
agreement is what lets a contribution be part of both. If you contribute in the
course of employment, read section 6 first: your employer may own the work, and an
authorised person has to sign for the entity. There is no Developer Certificate of
Origin (`Signed-off-by`), because a DCO certifies distribution under the current
licence only and does not permit relicensing.

## License

Contributions are licensed under the PolyForm Noncommercial License 1.0.0 (see
[LICENSE](LICENSE) and [NOTICE](NOTICE)) and under the terms of the Contributor
License Agreement.
