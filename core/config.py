"""Project configuration management."""

import os
from dotenv import load_dotenv


class Config:
    """Manages application configuration from .env file."""

    def __init__(self, env_file=".env"):
        load_dotenv(env_file, override=True)

    # ── Google ─────────────────────────────────────────────────────────────────
    @property
    def google_api_key(self):
        return os.getenv("GOOGLE_API_KEY", "YOUR_API_KEY_HERE")

    @property
    def street_view_size(self):
        return os.getenv("STREET_VIEW_SIZE", "640x640")

    # ── Output DB (SQLAlchemy — stores detection results) ──────────────────────
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

    # ── Source DB (SQL Server — GEO_DB / SGM_PORTA) ────────────────────────────
    @property
    def src_db_server(self):
        """SQL Server hostname or host\\instance (e.g. myserver\\SQLEXPRESS)."""
        return os.getenv("SRC_DB_SERVER", "")

    @property
    def src_db_database(self):
        return os.getenv("SRC_DB_DATABASE", "GEO_DB")

    @property
    def src_db_driver(self):
        return os.getenv("SRC_DB_DRIVER", "ODBC Driver 17 for SQL Server")

    @property
    def src_db_trusted(self):
        """True → Windows Auth.  False → use SRC_DB_USER / SRC_DB_PASSWORD."""
        return os.getenv("SRC_DB_TRUSTED", "True").lower() == "true"

    @property
    def src_db_user(self):
        return os.getenv("SRC_DB_USER", "")

    @property
    def src_db_password(self):
        return os.getenv("SRC_DB_PASSWORD", "")

    # ── Detection ──────────────────────────────────────────────────────────────
    @property
    def confidence_threshold(self):
        return int(os.getenv("CONFIDENCE_THRESHOLD", "70"))

    @property
    def road_offset_meters(self):
        return float(os.getenv("ROAD_OFFSET_METERS", "5.0"))

    @property
    def debug(self):
        return os.getenv("DEBUG", "False").lower() == "true"

    @property
    def https_proxy(self):
        return os.getenv("HTTPS_PROXY", "")
