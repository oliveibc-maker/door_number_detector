# Copilot Instructions

Follow `AGENTS.md` at the repository root; it is the source of truth for project rules, setup, checks, and commit conventions. Read `README.md` for setup, configuration, deployment, and outputs.

Key reminders:

- Always use the project `.venv` through `uv run`; never use system Python or `pip install`.
- Use the existing Ruff, mypy, and `unittest` tooling, and run the CI checks listed in `AGENTS.md` with `PYTHONPATH=src` before completing Python changes.
- Keep changes focused, and preserve the CLI, web, CSV, and database contracts and the web security controls.
- Never commit secrets, real coordinates, uploads, or generated data, and never call live Street View or company databases in tests.
- Use `type(scope): imperative summary` commit subjects.
