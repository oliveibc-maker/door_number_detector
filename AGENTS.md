# Agent Guide

## Project

- Python 3.11 application for detecting door numbers from Google Street View images with PaddleOCR.
- Package code lives in `src/door_number_detector/`; tests live in `tests/` and use `unittest`.
- Read `README.md` for setup, runtime configuration, deployment, outputs, and known limitations before changing those areas.

## Working Rules

- Keep changes focused; avoid unrelated refactors or new abstractions, and preserve the existing CLI, web, CSV, and database contracts unless the task explicitly requires a change.
- Keep shared detection logic in `core/`; keep web request handling in `web/`; use the existing source, batch, parsing, OCR, and sweep modules rather than duplicating their responsibilities.
- Treat `pyproject.toml` and `uv.lock` as the dependency source of truth. When dependencies change, update the lockfile and keep the generated `requirements.txt` in sync.
- Never commit credentials, API keys, real customer coordinates, uploaded workbooks, generated results, or local runtime data. Use `.env.example` for configuration examples.
- Preserve authentication, CSRF checks, loopback binding, upload validation/cleanup, output escaping, and safe SQL handling when changing web or data flows.
- Keep deployment assumptions explicit: one active job per process, one application instance for SQLite, persistent state under `APP_DATA_DIR`, and remote access only through a same-host HTTPS reverse proxy.
- Do not make live Street View or company database calls in tests. Mock external services and use synthetic or sanitized fixtures.
- Update README or flowcharts when a user-visible workflow, configuration, or architecture changes.
- Write commit subjects as `type(scope): imperative summary` in lowercase, e.g. `fix(security): protect uploads and add CSRF checks`. Common types: `feat`, `fix`, `refactor`, `docs`, `chore`.

## Setup and Checks

Use Python 3.11 and `uv`. Always work inside the project virtual environment (`.venv`, created by `uv sync`): run Python and tools through `uv run`, and never use system Python or `pip install` into it. Install the test and tooling dependencies:

```sh
uv sync --locked --no-install-project --no-default-groups --group test --group dev
```

Use the existing tooling: Ruff for linting and formatting, mypy for type checks, and `unittest` for tests. Avoid adding replacement tools. Run the CI checks before completing Python changes; set `PYTHONPATH=src`, as CI does:

```sh
export PYTHONPATH=src
uv lock --check
uv build
uv run --locked --no-sync python -m compileall -q src tests
uv run --locked --no-sync ruff check src tests setup.py
uv run --locked --no-sync ruff format --check src tests setup.py
uv run --locked --no-sync mypy
uv run --locked --no-sync python -m unittest discover -s tests -v
```

Run focused tests while iterating. Do not weaken lint, type, or test settings just to make a change pass; fix the underlying issue or explain a justified exception.

## Agent skills

### Issue tracker

Issues and specs are tracked in GitHub Issues via `gh`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the default labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

This repo uses a single-context domain-doc layout. See `docs/agents/domain.md`.
