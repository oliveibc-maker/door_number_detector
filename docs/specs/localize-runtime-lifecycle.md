## Problem Statement

The web app has startup and shutdown rules that depend on order, and the rules are spread over two places:

- The web app must load Paddle before it installs its own stop signal (SIGTERM) handler, because Paddle installs a handler that would end the process without a clean stop.
- The OCR reader saves and restores the log level around loading PaddleOCR, because that import resets the log level to WARNING.
- Shutdown (cancel the batch, close the server, wait for the worker, close the detector) is written inline in the web app start function.

A person who changes one of these places can break log output or clean shutdown without any test failing. The existing logging test does not check this order, and no test checks shutdown.

## Solution

Keep every startup and shutdown step that changes the whole process in one place, so the order is written down once and can be tested without loading the real Paddle models. The OCR reader only reads text from images.

From the operator's side nothing changes: the server still starts the same way, still logs at INFO level, and still stops cleanly on Ctrl+C or SIGTERM.

## User Stories

1. As an operator, I want the server to stop cleanly on SIGTERM, so that a Docker stop does not lose or corrupt results.
2. As an operator, I want a running batch to be cancelled on shutdown and its partial CSV saved, so that I keep finished rows.
3. As an operator, I want the server to wait for the worker to end before the process exits, so that metrics files are written.
4. As an operator, I want the detector to be closed on shutdown, so that resources are freed.
5. As an operator, I want INFO log lines to appear after Paddle loads, so that I can follow what the app does.
6. As an operator, I want abandoned uploaded workbooks to be removed at startup, so that no customer data stays on disk.
7. As an operator, I want startup to behave the same on Windows, Linux, WSL, macOS, and Docker, so that deployment does not change.
8. As a developer, I want the startup order written in one place, so that I can see why it is that order.
9. As a developer, I want to test the startup order without downloading models, so that tests are fast and work offline.
10. As a developer, I want to test shutdown steps, so that a future change cannot remove one by mistake.
11. As a developer, I want the OCR reader to have no knowledge of process signals or app logging, so that I can change OCR without side effects.
12. As a developer, I want the CLI path and the web path to keep working, so that no entry point breaks.

## Implementation Decisions

- One module owns process-wide startup (load Paddle, configure logging, install the stop signal handler) and shutdown (set cancel, close the server, wait for the worker, close the detector). The web app start function calls it and does not repeat these steps.
- The order stays as it is today: Paddle first, then logging, then the stop signal handler. The change is where this is written and tested, not what the order is.
- The log level reset caused by the PaddleOCR import is handled in the same place as the other process-wide steps, so the OCR reader no longer saves and restores the root log level. If that cannot be done safely (for example, the import is lazy and happens later), keep the restore in the OCR reader and record the reason in the spec thread instead of forcing the move.
- The web server factory, request handling, job state, and security controls do not change.
- No change to environment variables, endpoints, CSV, metrics JSON, or SQLite.
- Keep the single-process, single-instance assumptions in the README.

## Testing Decisions

- A good test checks external behavior: which steps ran, in what order, and what state the process is in afterwards. It does not check private helper names.
- Highest seam: the startup and shutdown entry point of the web app, with Paddle, the HTTP server, the worker, and the detector replaced by fakes. Tests check: Paddle loads before the signal handler is set; INFO logging is active after load; SIGTERM leads to a clean stop; the cancel event is set; the worker is waited for; the detector is closed; orphaned uploads are removed at startup.
- Prior art: the logging config test (temporary directory, handler checks, restore of the root logger in cleanup) and the web handler tests (fake worker, state checks).
- Tests must not import the real Paddle package or download models, and must restore the root logger and signal handlers after each test.

## Out of Scope

- Changing detection, OCR scoring, or the Street View sweep.
- Moving Excel or SQL Server loading (covered by a separate spec).
- Support for more than one app instance or more than one active job.
- Changing deployment scripts or the Docker setup.

## Further Notes

- Found in an architecture review on 2026-10-25. This is a lower-confidence idea than the batch workflow spec: if the work shows that moving the log level reset is not safe, a smaller result (tests plus a single startup function) is still a good outcome.
- This spec and the batch workflow spec both touch the web app module. Do them as separate commits.
- Run the CI checks listed in `AGENTS.md` with `PYTHONPATH=src` before finishing.
