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

- Python **3.10**
- SQL Server ODBC driver (only needed for the SQL Server source mode)

### Python dependencies

All dependencies are installed via `pip` from `requirements.txt`. No external system tools are required.

---

## Setup

### Step 1: Create and activate the virtual environment

~~~bash
# Create venv with Python 3.10
python3.10 -m venv .venv310

# Activate — Linux/macOS
source .venv310/bin/activate

# Activate — Windows PowerShell
.\.venv310\Scripts\Activate.ps1

# Activate — Windows CMD
.venv310\Scripts\activate.bat
~~~

If PowerShell blocks script execution:

~~~powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv310\Scripts\Activate.ps1
~~~

### Step 2: Install dependencies

~~~bash
pip install -r requirements.txt
~~~

### Step 3: Configure the environment

~~~bash
cp .env.example .env
~~~

Edit `.env` and fill in at minimum:

~~~env
# Required
GOOGLE_API_KEY=your_actual_google_api_key_here

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

### Option A: Windows — use the provided scripts (recommended)

Double-click or run from CMD:

~~~
start.bat   — starts the web server
stop.bat    — stops the web server
~~~

`start.bat` reads `RUN_IN_BACKGROUND` from `.env`:
- `False` → runs in the current window (logs visible)
- `True` → runs hidden; logs go to `server.log` / `server_error.log`; PID saved to `server.pid`

Then open **http://127.0.0.1:8080** in your browser.

### Option B: Run the web server manually

~~~bash
python web/app.py
~~~

Then open **http://127.0.0.1:8080** in your browser.

### Option C: Run the CLI directly

Edit the `main()` function in `entrypoint.py` to set your source and filter, then:

~~~bash
python entrypoint.py
~~~

Supported sources:
- `SOURCE = "sqlserver"` — queries SQL Server by `localidade`, `freguesia`, `concelho`, or `rua`
- `SOURCE = "excel"` — processes an Excel file with columns `NOME_COMPLETO_PORTA`, `LATITUDE`, `LONGITUDE`

---

## Output

Each run produces two files next to the output CSV:

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
| `MATCH` | `✓ MATCH` / `UPGRADE` / `? NOT FOUND` |
| `MAPS_LINK` | Google Maps link for the coordinate |
| `COORDINATES` | `LATITUDE,LONGITUDE` |
| `NO_STREET_VIEW_IMAGERY` | `YES` if no Street View coverage exists |

---

## Project structure

~~~
.
├── entrypoint.py                 # CLI entry point (SQL Server or Excel batch)
├── web/
│   ├── app.py                    # Web server (Python built-in HTTP, port 8080)
│   └── templates/
│       └── index.html            # Web UI
├── core/
│   ├── config.py                 # Configuration (.env loader)
│   ├── database.py               # SQLite access
│   ├── detector.py               # Detection workflow + PaddleOCR orchestration
│   ├── google_street_view.py     # Street View API client
│   └── metrics.py                # Run-level performance and cost metrics
├── start.bat / start.ps1         # Windows start scripts
├── stop.bat  / stop.ps1          # Windows stop scripts
├── .env.example                  # Environment template
└── requirements.txt
~~~

---

## Notes

- OCR is handled exclusively by **PaddleOCR** (no Tesseract or EasyOCR required).
- The pipeline uses **Street View metadata** (free) to locate the nearest panorama and compute camera heading before fetching images (billed at ~€0.0064/image).
- Door number patterns supported: plain numbers, numbers with letters (e.g. `12A`), and keyword formats such as `LOTE`, `SECTOR`, `BLOCO`.
- Runs can be **resumed** from an existing CSV — already-processed coordinates are skipped automatically.
- Logs are written to `door_detector.log` (UTF-8).
- The SQLite database stores all detection results and can be queried from the web interface.
