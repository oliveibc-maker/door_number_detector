"""Helpers for keeping secrets out of logs and user-visible errors."""

import re

_API_KEY_PATTERN = re.compile(r"(?i)([?&]key=)[^&\s'\"]+")


def redact_api_key(message: str) -> str:
    """Mask the value of any ``key=`` URL query parameter in ``message``."""
    return _API_KEY_PATTERN.sub(r"\1[REDACTED]", message)
