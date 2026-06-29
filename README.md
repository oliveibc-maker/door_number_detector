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

## Setup & Getting Started

### Step 1: Activate virtual environment and install dependencies

Activate the virtual environment:
```bash
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
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
