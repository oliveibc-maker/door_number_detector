## Problem Statement

A person who runs door number detection can start a batch from the web app (Excel upload or SQL Server filter) or from the CLI. The three routes do not share the same steps for getting rows and starting detection:

- The web app opens an uploaded Excel workbook to check its columns and count its rows. The batch code then opens the same workbook again and checks the columns again.
- The web app gets SQL Server rows by itself and calls the low-level detection step. The CLI uses the batch code, which gets the rows itself.

This means the same rules exist in more than one place. A change to a source rule (for example, required columns) must be made twice, and the two routes can drift apart. Large workbooks are read twice on every upload.

## Solution

Make the shared batch code the one place that turns a source (Excel workbook or SQL Server filter) into detection results. The web app keeps only its own work: upload checks, job state, row count display, and deleting the uploaded file. The CLI and the web app both use the same shared batch steps.

From the user's side nothing changes, except that uploads start faster on large workbooks. The row count must still show in the web app while the batch runs.

## User Stories

1. As a web app user, I want an uploaded workbook to be read once, so that my batch starts faster.
2. As a web app user, I want an invalid workbook (empty, or missing `NOME_COMPLETO_PORTA`, `LATITUDE`, or `LONGITUDE`) to be rejected right away with a clear message, so that I do not wait for a batch that cannot run.
3. As a web app user, I want to see the total row count as soon as the batch starts, so that I can follow progress.
4. As a web app user running a SQL Server batch, I want the total row count to show once the query returns, so that progress is correct.
5. As a web app user, I want the same results (CSV and metrics) as before, so that my current process keeps working.
6. As a web app user, I want a second batch to be refused while one is running, so that jobs do not overlap.
7. As a web app user, I want cancel to keep partial results, so that I do not lose finished rows.
8. As a web app user, I want my uploaded workbook to be deleted when the batch ends, so that no customer data stays on disk.
9. As a CLI user, I want Excel and SQL Server runs to behave as before, so that my scripts keep working.
10. As a CLI user, I want the resume option to keep working with an existing CSV, so that I can continue an interrupted run.
11. As a developer, I want one place that holds the Excel column rules, so that I change them once.
12. As a developer, I want one place that holds the SQL Server row-loading steps, so that the web app and the CLI cannot drift apart.
13. As a developer, I want to test the full source-to-CSV steps through one entry point, so that tests do not depend on how the steps are split inside.
14. As a developer, I want the web app to keep upload checks, CSRF, and file cleanup, so that security controls stay as they are.
15. As an operator, I want error messages for bad input to stay safe (no file paths or internal details sent to the browser), so that the web security rules still hold.
16. As an operator, I want one active job per process to remain true, so that the SQLite and one-instance assumptions stay valid.

## Implementation Decisions

- The shared batch module becomes the only place that loads rows from a source and starts detection. It covers both Excel and SQL Server.
- Excel column rules (empty workbook, required columns) live in the shared batch code. The web app still returns HTTP 400 for a bad upload before it starts a worker, so the browser gets an immediate answer. The workbook must be parsed only once per upload; the parsed rows are reused, not read again from disk.
- The shared batch code must let the caller learn the row count before detection starts. The web app uses this to set its total. For Excel, the total is known before the worker starts. For SQL Server, the total is set after the query returns, as it is today.
- The web SQL Server batch uses the same shared route as the CLI. It does not resume from an old CSV, as today.
- The web app keeps: upload size and name checks, writing the upload to a generated path, job claim and release, worker thread, cancel handling, metrics saving, and deleting the uploaded file when the batch ends (success, error, or cancel).
- No change to the CSV columns, the metrics JSON, the SQLite schema, the CLI environment variables, or the HTTP endpoints and their status codes.
- Resume stays based on the existing CSV coordinate comparison.

## Testing Decisions

- A good test checks external behavior only: the CSV rows written, the metrics recorded, the HTTP status codes, the job state, and whether the uploaded file is deleted. It does not check which helper function was called.
- Highest seam: the shared batch entry points for Excel and SQL Server, with a fake detector and fake metrics. Add tests here for: a valid workbook, a workbook with missing columns, an empty workbook, SQL Server rows from a mocked query, resume from an existing CSV, and cancel.
- Second seam (already exists): the web request handler. Keep and adjust the existing upload tests for: 400 on bad workbook with no file left behind, 200 on a good upload, 409 while a job runs, and file deletion after the batch. Add one check that a workbook is parsed once per upload.
- Prior art: the batch tests with `FakeDetector` and `FakeMetrics`; the web handler tests that hold a worker open and check the job state; the source tests that mock the database.
- Do not call live Street View or any company database. Use small synthetic workbooks and mocked query results.

## Out of Scope

- Changing resume to use a durable row identity.
- Changing detection, OCR, or Street View sweep behavior.
- Changing the web endpoints, CSV columns, metrics JSON, or the SQLite schema.
- Startup and shutdown order (covered by a separate spec).
- Support for more than one active job or more than one app instance.

## Further Notes

- Found in an architecture review on 2026-10-25. No `GLOSSARY.md` or ADR exists in this repo, so the README terms were used.
- The web app and the startup spec both touch the web app module. Do the two changes as separate commits to keep merges simple.
- Run the CI checks listed in `AGENTS.md` with `PYTHONPATH=src` before finishing.
