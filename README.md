# Door Number Detector

This project detects door numbers from geographic coordinates using Google Street View images and OCR with Tesseract.

## What it does

- Fetches Street View images from the Google Street View API
- Runs OCR on the image to extract door numbers
- Stores detection results in a database
- Provides a simple web interface and a CLI entry point

## Requirements

### System requirements
- Python 3.8+
- Tesseract OCR installed

### Install Tesseract

Ubuntu/Debian:
```bash
sudo apt-get install tesseract-ocr
```

macOS:
```bash
brew install tesseract
```

Windows:
- Download the installer from the Tesseract GitHub releases page.

> Note: Tesseract OCR itself is a native executable and must be installed on your system.
> The Python package `pytesseract` is included in `requirements.txt`, but it only provides the Python bindings.
> You still need the Tesseract engine installed separately.
> On Windows, set `TESSERACT_PATH` in `.env` to the full path to `tesseract.exe`, for example:
> `TESSERACT_PATH=C:\Users\mafapereira\AppData\Local\Programs\Tesseract-OCR\tesseract.exe`
> On Linux/macOS, either install `tesseract` globally or ensure it is on `PATH`.

## Setup & Getting Started

### Step 1: Activate virtual environment and install dependencies

Activate the virtual environment:
```bash
source .venv/bin/activate  # On Windows PowerShell: .\.venv\Scripts\Activate.ps1
                           # On Windows CMD: .venv\Scripts\activate.bat
```

If PowerShell blocks script execution, run:
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

If `python` still resolves to the Windows Store alias, use the real installed interpreter path instead, for example:
```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" -m venv .venv
```

Install Python dependencies:
```bash
pip install -r requirements.txt
```

### Step 2: Configure your environment

Copy the environment template and add your Google Street View API key:
```bash
cp .env.example .env
```

Then edit `.env` and add your real Google Street View API key:
```env
GOOGLE_API_KEY=your_actual_google_api_key_here
```

**⚠️ Important:** Replace `your_actual_google_api_key_here` with your real API key from Google Cloud Console.

Get your free API key from: https://developers.google.com/maps/documentation/streetview/get-api-key

### Step 3: Run the application

Make sure the virtual environment is activated, then run one of these:

#### Option A: Run the CLI
```bash
python entrypoint.py
```

This will process a default coordinate and display the detection result.

#### Option B: Run the web app
```bash
python web/app.py
```

Then open http://127.0.0.1:8000 in your browser to use the web interface.

## Project structure

- [entrypoint.py](entrypoint.py) - CLI entry point
- [web/](web/) - web application
  - [app.py](web/app.py) - Flask web server
  - [templates/](web/templates/) - HTML templates
- [core/](core/) - main package
  - [config.py](core/config.py) - configuration management (.env)
  - [database.py](core/database.py) - database access
  - [detector.py](core/detector.py) - detection workflow
  - [google_street_view.py](core/google_street_view.py) - Street View API requests
- [.env.example](.env.example) - environment template (copy to .env)

## Notes

- SQLite is the default database option.
- The application writes logs to [door_detector.log](door_detector.log).
- Configuration is loaded from `.env` file (copy from `.env.example`).
- Generated build artifacts and local data are ignored by [.gitignore](.gitignore).


NOTAS

Tesseract wasnt detecting the digits

easyocr is having some problems in some cases

will try paddle next that seems better

references: https://toon-beerten.medium.com/ocr-comparison-tesseract-versus-easyocr-vs-paddleocr-vs-mmocr-a362d9c79e66