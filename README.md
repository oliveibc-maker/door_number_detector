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

### Install Python dependencies
```bash
pip install -r requirements.txt
```

## Configuration

Set your Google API key before running the app:
```bash
export GOOGLE_API_KEY="your_key_here"
```

You can also edit the default configuration in [config.ini](config.ini).

## Run the CLI
```bash
python entrypoint.py
```

## Run the web app
```bash
python web_app.py
```

Then open http://127.0.0.1:8000 in your browser.

## Project structure

- [main.py](main.py) - compatibility wrapper for the CLI entry point
- [entrypoint.py](entrypoint.py) - recommended CLI launcher
- [web_app.py](web_app.py) - simple web server
- [src/door_number_detector](src/door_number_detector) - main package
  - [config.py](src/door_number_detector/config.py) - configuration handling
  - [database.py](src/door_number_detector/database.py) - database access
  - [detector.py](src/door_number_detector/detector.py) - detection workflow
  - [google_street_view.py](src/door_number_detector/google_street_view.py) - Street View requests
- [templates/index.html](templates/index.html) - web UI
- [config.ini](config.ini) - runtime configuration

## Notes

- SQLite is the default database option.
- The application writes logs to [door_detector.log](door_detector.log).
- Generated build artifacts and local data are ignored by [.gitignore](.gitignore).
