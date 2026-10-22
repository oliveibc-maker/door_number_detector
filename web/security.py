"""Security helpers for the built-in web server."""

import hmac
import re
import uuid
from ipaddress import ip_address
from pathlib import Path


class UploadTooLarge(ValueError):
    """Raised when a request body exceeds its configured byte limit."""


def parse_content_length(value: str | None, maximum: int) -> int:
    if value is None or re.fullmatch(r"[0-9]+", value) is None:
        raise ValueError("A valid Content-Length header is required.")
    length = int(value)
    if length <= 0:
        raise ValueError("The request body must not be empty.")
    if length > maximum:
        raise UploadTooLarge("The request body exceeds the configured limit.")
    return length


def upload_suffix(filename: str) -> str:
    suffix = Path(filename.replace("\\", "/")).suffix.lower()
    if suffix not in {".xls", ".xlsx"}:
        raise ValueError("Only .xls and .xlsx files are accepted.")
    return suffix


def new_upload_path(upload_dir: Path, filename: str) -> Path:
    return Path(upload_dir) / f"{uuid.uuid4().hex}{upload_suffix(filename)}"


def valid_csrf_token(provided: str, expected: str) -> bool:
    if not provided:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def is_loopback_host(host: str) -> bool:
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False