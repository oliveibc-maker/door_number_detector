"""Project configuration management."""

import os
from pathlib import Path

from dotenv import load_dotenv


def _optional_yes_no(name: str) -> str | None:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return None
    if value in {"true", "yes"}:
        return "yes"
    if value in {"false", "no"}:
        return "no"
    raise ValueError(f"{name} must be True or False.")


class Config:
    """Manages application configuration from .env file."""

    def __init__(self, env_file=".env"):
        load_dotenv(env_file, override=False)

    # ── Google ─────────────────────────────────────────────────────────────────
    @property
    def google_api_key(self):
        api_key = os.getenv("GOOGLE_API_KEY", "").strip()
        if not api_key or api_key.lower() in {"your_api_key_here", "your_key_here"}:
            raise ValueError("Set GOOGLE_API_KEY to a valid Google Street View API key.")
        return api_key

    @property
    def web_username(self):
        return os.getenv("WEB_USERNAME", "").strip()

    @property
    def web_password(self):
        return os.getenv("WEB_PASSWORD", "")

    @property
    def web_host(self):
        return os.getenv("WEB_HOST", "127.0.0.1").strip()

    @property
    def web_port(self):
        return int(os.getenv("WEB_PORT", "8080"))

    @property
    def data_dir(self):
        configured = Path(os.getenv("APP_DATA_DIR", str(Path.cwd() / "data"))).expanduser()
        if not configured.is_absolute():
            configured = Path.cwd() / configured
        return configured.resolve()

    @property
    def database_path(self):
        configured = os.getenv("DATABASE_PATH", "").strip()
        if not configured:
            return self.data_dir / "door_detector.db"
        return Path(configured).expanduser().resolve()

    @property
    def max_upload_bytes(self):
        value = int(os.getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
        if value <= 0:
            raise ValueError("MAX_UPLOAD_BYTES must be a positive integer.")
        return value

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
    def src_db_encrypt(self):
        """Optional ODBC Encrypt override ("yes"/"no"); None keeps the driver default."""
        return _optional_yes_no("SRC_DB_ENCRYPT")

    @property
    def src_db_trust_server_certificate(self):
        """Optional TrustServerCertificate override; only for self-signed internal servers."""
        return _optional_yes_no("SRC_DB_TRUST_SERVER_CERTIFICATE")

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
    def debug(self):
        return os.getenv("DEBUG", "False").lower() == "true"

    @property
    def https_proxy(self):
        return os.getenv("HTTPS_PROXY", "")
