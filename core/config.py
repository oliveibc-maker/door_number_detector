"""Project configuration management."""

import os
from dotenv import load_dotenv


class Config:
    """Manages application configuration from .env file."""

    def __init__(self, env_file=".env"):
        """Load configuration from .env file."""
        # Load environment variables from .env file (override existing)
        load_dotenv(env_file, override=True)

    @property
    def google_api_key(self):
        return os.getenv("GOOGLE_API_KEY", "YOUR_API_KEY_HERE")

    @property
    def street_view_size(self):
        return os.getenv("STREET_VIEW_SIZE", "640x640")

    @property
    def db_type(self):
        return os.getenv("DATABASE_TYPE", "sqlite").lower()

    @property
    def db_host(self):
        return os.getenv("DATABASE_HOST", "localhost")

    @property
    def db_port(self):
        return int(os.getenv("DATABASE_PORT", "1521"))

    @property
    def db_user(self):
        return os.getenv("DATABASE_USER", "system")

    @property
    def db_password(self):
        return os.getenv("DATABASE_PASSWORD", "")

    @property
    def db_name(self):
        return os.getenv("DATABASE_NAME", "door_detector")

    @property
    def db_sid(self):
        return os.getenv("DATABASE_SID", "xe")

    @property
    def tesseract_path(self):
        return os.getenv("TESSERACT_PATH")

    @property
    def ocr_language(self):
        return os.getenv("OCR_LANGUAGE", "por+eng")

    @property
    def confidence_threshold(self):
        return int(os.getenv("CONFIDENCE_THRESHOLD", "70"))

    @property
    def debug(self):
        return os.getenv("DEBUG", "False").lower() == "true"
