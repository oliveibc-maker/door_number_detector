# Door Number Detector

Automatic door number detection from geographic coordinates using Google Street View images and PaddleOCR.

## What it does

- Fetches Street View panoramas from the Google Street View Static API for each coordinate (latitude/longitude)
- Runs PaddleOCR to extract and validate the most likely door number from each image
- Supports two data sources: **SQL Server** (live query) or **Excel file** (batch upload)
- Provides a web interface and a CLI entry point
- Stores detection results in a SQLite database and writes a CSV + metrics JSON per run

---

## Requirements

### System requirements

- Python **3.11** (the version selected and tested for the Linux container)
- SQL Server ODBC driver (only needed for SQL Server source mode outside Docker)

### Python dependencies

Runtime dependencies are declared in `pyproject.toml` and pinned in `uv.lock`; `requirements.txt` is generated for pip-based installs. PaddleOCR downloads its models on first startup. SQL Server mode also requires the Microsoft SQL Server ODBC driver; the Docker image installs it.

---

## Setup

### Step 1: Install uv

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) as a standalone tool (not inside the project environment).

### Step 2: Create the virtual environment and install dependencies

~~~bash
uv sync --locked
~~~

This creates `.venv` with Python 3.11 (downloaded by uv if needed) and installs the locked dependencies. Run project commands through `uv run`, or activate `.venv` first.

### Step 3: Configure the environment

~~~bash
cp .env.example .env
~~~

Edit `.env` and fill in at minimum:

~~~env
# Required
GOOGLE_API_KEY=your_actual_google_api_key_here
WEB_USERNAME=your_username
WEB_PASSWORD=a_long_random_password

# Web server behaviour: "True" = background (hidden window), "False" = foreground
RUN_IN_BACKGROUND=False

# Optional — SQL Server source
SRC_DB_SERVER=...
SRC_DB_DATABASE=GEO_DB
SRC_DB_TRUSTED=True
~~~

ODBC Driver 18 encrypts connections and rejects self-signed server certificates. Prefer installing the issuing CA; for an internal server you trust, set `SRC_DB_TRUST_SERVER_CERTIFICATE=True` (optionally `SRC_DB_ENCRYPT=True|False`). Both are unset by default, which keeps the driver defaults.

Get a Google Street View API key from: https://developers.google.com/maps/documentation/streetview/get-api-key

---

## Running the application

### Option A: Windows — local development

Double-click:

~~~
scripts/windows/start.bat   — starts the web server
scripts/windows/stop.bat    — stops the web server
~~~

Or run from CMD:

~~~
.\scripts\windows\start.bat   — starts the web server
.\scripts\windows\stop.bat    — stops the web server
~~~


`RUN_IN_BACKGROUND` in `.env` controls the launcher mode:
- `False` → runs in the current window (logs visible)
- `True` → runs hidden on Windows or in the background on Linux, WSL, and macOS; logs go to `server.log` / `server_error.log`; PID saved to `server.pid`

Then open **http://127.0.0.1:8080** in your browser.
The browser will prompt for the configured web credentials. The application only binds to loopback. For remote access, terminate HTTPS at a reverse proxy on the same host and have it forward to `127.0.0.1:8080`. Uploads are limited to 25 MiB by default; adjust `MAX_UPLOAD_BYTES` if your spreadsheets require more space.
All POST requests require the `X-CSRF-Token` header. Browser requests use the token embedded in the page; authenticated API clients can fetch it from `GET /api/csrf` and must send it with each POST.

### Option B: Linux, WSL, and macOS

From the repository root, run:

~~~sh
./scripts/unix/start.sh
~~~

Use `./scripts/unix/stop.sh` to stop a background server. The scripts use the project `.venv` and `.env`.

Then open **http://127.0.0.1:8080** in your browser.

### Option C: Run the web server manually

~~~bash
uv run door-number-detector-web
~~~

Then open **http://127.0.0.1:8080** in your browser.

### Option D: Run in Docker on Linux

Create `.env` as described above, then run:

~~~bash
docker compose up --build -d
docker compose ps
docker compose logs -f door-number-detector
~~~

Compose uses Linux host networking so the app remains bound to `127.0.0.1`; configure a same-host HTTPS reverse proxy to reach it remotely. SQLite, outputs, uploads, and the PaddleOCR model cache use the persistent `door_detector_data` volume. SQL Server authentication uses `SRC_DB_TRUSTED=False` and the configured SQL credentials in `.env`. Run one instance only: job coordination is process-local and SQLite is not configured for multi-instance access.

### Option E: Run the CLI directly

Set `SOURCE`, `FILTER_BY`, and `FILTER_VALUE` in `.env` for SQL Server, or set `SOURCE=excel` and `INPUT_EXCEL` for Excel, then:

~~~bash
uv run door-number-detector
~~~

Supported sources:
- `SOURCE=sqlserver` — queries SQL Server by `localidade`, `freguesia`, `concelho`, or `rua`
- `SOURCE=excel` — processes an Excel file with columns `NOME_COMPLETO_PORTA`, `LATITUDE`, `LONGITUDE`

To resume an interrupted CLI run, set `RESUME_CSV` to the previous output CSV. Its rows are copied into the new CSV and source rows with the same coordinates are skipped. Resume compares latitude/longitude only, so it is suitable for this POC but not a durable source-row identity. The web interface always starts a fresh run.

The CLI writes CSV and metrics files under `APP_DATA_DIR/outputs`. Web uploads are temporary and removed after the batch ends; completed CSV/metrics and the SQLite database (`APP_DATA_DIR/door_detector.db` unless `DATABASE_PATH` is set) are retained.

---

## Output

Each run writes two files under `APP_DATA_DIR/outputs`:

| File | Description |
|---|---|
| `predictions_<filter>_<timestamp>.csv` | Per-row results (`;` separated, UTF-8 BOM) |
| `predictions_<filter>_<timestamp>.metrics.json` | Aggregated performance and cost metrics |

### CSV columns

| Column | Description |
|---|---|
| `LATITUDE` | Input latitude |
| `LONGITUDE` | Input longitude |
| `NOME_COMPLETO_PORTA` | Expected door number from source |
| `PREDICTION` | Detected door number (empty if not found) |
| `CONFIDENCE` | OCR confidence (%) |
| `PREDICTION_FOUND` | `YES` / `NO` / `NO_IMAGERY` |
| `MATCH` | `YES` if the prediction matches the expected value, otherwise `NO` |
| `OBSERVATION` | Additional result detail, including no-imagery status |

### Offline CSV viewer

To inspect an existing CSV without starting the server, open `src/door_number_detector/web/templates/results_viewer.html` directly in a browser and select or drop the CSV file. This standalone viewer reads the selected file locally; it does not submit data to the application.

---

## Project structure

~~~
.
├── Dockerfile                    # Python 3.11 Linux container
├── compose.yaml                  # Single-instance POC deployment
├── pyproject.toml / uv.lock      # Runtime dependencies and lock
├── requirements.txt              # Generated pip-compatible dependency list
├── src/door_number_detector/
│   ├── cli.py                    # CLI entry point (door-number-detector)
│   ├── logging_config.py         # Rotating root logger with API-key redaction
│   ├── runtime.py                # Process startup and shutdown order
│   ├── core/
│   │   ├── batch.py              # Shared CSV/resume/detection orchestration
│   │   ├── config.py             # Environment configuration
│   │   ├── database.py           # SQLite result persistence
│   │   ├── detector.py           # Detection coordinator
│   │   ├── google_street_view.py # Street View API client
│   │   ├── metrics.py            # Per-run performance and cost metrics
│   │   ├── models.py             # Typed detection result
│   │   ├── ocr.py                # PaddleOCR image processing and scoring
│   │   ├── parsing.py            # OCR text parsing
│   │   ├── redaction.py          # API-key redaction helper
│   │   ├── sweep.py              # Street View image sweep
│   │   └── sources/              # Excel and SQL Server adapters
│   └── web/
│       ├── app.py                # Authenticated loopback HTTP server (door-number-detector-web)
│       ├── healthcheck.py        # Container readiness check
│       ├── security.py           # Upload, CSRF, and loopback helpers
│       └── templates/            # Web app and offline CSV viewer
├── tests/                        # unittest suite for core, sources, OCR, parsing and web
├── flowcharts/                   # English/Portuguese Mermaid sources
├── scripts/
│   ├── windows/                  # Windows start/stop scripts
│   └── unix/                     # Linux, WSL, and macOS start/stop scripts
├── .github/workflows/tests.yml   # Python 3.11 CI
├── .env.example                  # Environment template
└── data/                          # Ignored local runtime data
~~~

The Mermaid files in `flowcharts/` are the editable diagram sources.

## Development checks

Install only the test and tooling groups:

~~~bash
uv sync --locked --no-install-project --no-default-groups --group test --group dev
~~~

Run the same checks used in CI:

~~~bash
export PYTHONPATH=src
uv lock --check
uv build
uv run --locked --no-sync python -m compileall -q src tests
uv run --locked --no-sync ruff check src tests setup.py
uv run --locked --no-sync ruff format --check src tests setup.py
uv run --locked --no-sync mypy
uv run --locked --no-sync python -m unittest discover -s tests -v
~~~

---

## Notes

- OCR is handled exclusively by **PaddleOCR** (no Tesseract or EasyOCR required).
- The pipeline uses **Street View metadata** (free) to locate the nearest panorama and compute camera heading before fetching images (billed at ~€0.0064/image).
- Door number patterns supported: plain numbers, numbers with letters (e.g. `12A`), and keyword formats such as `LOTE`, `SECTOR`, `BLOCO`.
- CLI runs can be **resumed** with `RESUME_CSV` — already-processed coordinates are skipped by latitude/longitude. This is best-effort for the POC because multiple doors may share coordinates.
- Logs are written to `APP_DATA_DIR/logs/door_detector.log` (UTF-8).
- The SQLite database stores all detection results and can be queried from the web interface.
