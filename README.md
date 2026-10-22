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

### Step 1: Create and activate the virtual environment

~~~bash
# If Python 3.11 is your default python:
python -m venv .venv

# Or, using the Python Launcher (recommended if multiple versions are installed):
py -3.11 -m venv .venv

# Activate — Linux/macOS
source .venv/bin/activate

# Activate — Windows PowerShell
.\.venv\Scripts\Activate.ps1

# Activate — Windows CMD
.venv\Scripts\activate.bat
~~~

If PowerShell blocks script execution:

~~~powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
~~~

~~~bash
# Check the tested Python version.
python --version
~~~

### Step 2: Install dependencies

~~~bash
pip install uv
uv sync --locked
~~~

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

Get a Google Street View API key from: https://developers.google.com/maps/documentation/streetview/get-api-key

---

## Running the application

### Option A: Windows — local development

Double-click:

~~~
start.bat   — starts the web server
stop.bat    — stops the web server
~~~

Or run from CMD:

~~~
.\start.bat   — starts the web server
.\stop.bat    — stops the web server
~~~


`start.bat` reads `RUN_IN_BACKGROUND` from `.env`:
- `False` → runs in the current window (logs visible)
- `True` → runs hidden; logs go to `server.log` / `server_error.log`; PID saved to `server.pid`

Then open **http://127.0.0.1:8080** in your browser.
The browser will prompt for the configured web credentials. The application only binds to loopback. For remote access, terminate HTTPS at a reverse proxy on the same host and have it forward to `127.0.0.1:8080`. Uploads are limited to 25 MiB by default; adjust `MAX_UPLOAD_BYTES` if your spreadsheets require more space.
All POST requests require the `X-CSRF-Token` header. Browser requests use the token embedded in the page; authenticated API clients can fetch it from `GET /api/csrf` and must send it with each POST.

### Option B: Run the web server manually

~~~bash
python web/app.py
~~~

Then open **http://127.0.0.1:8080** in your browser.

### Option C: Run in Docker on Linux

Create `.env` as described above, then run:

~~~bash
docker compose up --build -d
docker compose ps
docker compose logs -f door-number-detector
~~~

Compose uses Linux host networking so the app remains bound to `127.0.0.1`; configure a same-host HTTPS reverse proxy to reach it remotely. SQLite, outputs, uploads, and the PaddleOCR model cache use the persistent `door_detector_data` volume. SQL Server authentication uses `SRC_DB_TRUSTED=False` and the configured SQL credentials in `.env`. Run one instance only: job coordination is process-local and SQLite is not configured for multi-instance access.

### Option D: Run the CLI directly

Set `SOURCE`, `FILTER_BY`, and `FILTER_VALUE` in `.env` for SQL Server, or set `SOURCE=excel` and `INPUT_EXCEL` for Excel, then:

~~~bash
python entrypoint.py
~~~

Supported sources:
- `SOURCE=sqlserver` — queries SQL Server by `localidade`, `freguesia`, `concelho`, or `rua`
- `SOURCE=excel` — processes an Excel file with columns `NOME_COMPLETO_PORTA`, `LATITUDE`, `LONGITUDE`

The CLI writes CSV and metrics files under `APP_DATA_DIR/outputs`. Web uploads are temporary and removed after the batch ends; completed CSV/metrics and the SQLite database are retained. Resume remains based on the existing CSV coordinate comparison, so it is suitable for this POC but not a durable source-row identity.

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

To inspect an existing CSV without starting the server, open `web/templates/results_viewer.html` directly in a browser and select or drop the CSV file. This standalone viewer reads the selected file locally; it does not submit data to the application.

---

## Project structure

~~~
.
├── Dockerfile                    # Python 3.11 Linux container
├── compose.yaml                  # Single-instance POC deployment
├── pyproject.toml / uv.lock      # Runtime dependencies and lock
├── requirements.txt              # Generated pip-compatible dependency list
├── entrypoint.py                 # CLI entry point (SQL Server or Excel batch)
├── core/
│   ├── config.py                 # Environment configuration
│   ├── database.py               # SQLite result persistence
│   ├── detector.py               # PaddleOCR detection workflow
│   ├── google_street_view.py     # Street View API client
│   ├── metrics.py                # Run-level performance and cost metrics
│   └── sources/                  # Excel and SQL Server input adapters
│       ├── excel.py
│       └── sqlserver.py
├── web/
│   ├── app.py                    # Authenticated loopback HTTP server
│   ├── healthcheck.py            # Container readiness check
│   └── templates/
│       ├── index.html            # Web application
│       └── results_viewer.html   # Standalone offline CSV viewer
├── tests/                        # Security and request lifecycle tests
├── flowcharts/                   # English/Portuguese Mermaid sources
├── start.bat / start.ps1         # Windows start scripts
├── stop.bat  / stop.ps1          # Windows stop scripts
├── .github/workflows/tests.yml   # Python 3.11 CI
├── .env.example                  # Environment template
└── data/                          # Ignored local runtime data
~~~

The Mermaid files in `flowcharts/` are the editable diagram sources.

---

## Notes

- OCR is handled exclusively by **PaddleOCR** (no Tesseract or EasyOCR required).
- The pipeline uses **Street View metadata** (free) to locate the nearest panorama and compute camera heading before fetching images (billed at ~€0.0064/image).
- Door number patterns supported: plain numbers, numbers with letters (e.g. `12A`), and keyword formats such as `LOTE`, `SECTOR`, `BLOCO`.
- Runs can be **resumed** from an existing CSV — already-processed coordinates are skipped by latitude/longitude. This is best-effort for the POC because multiple doors may share coordinates.
- Logs are written to `APP_DATA_DIR/logs/door_detector.log` (UTF-8).
- The SQLite database stores all detection results and can be queried from the web interface.
