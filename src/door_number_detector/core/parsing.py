"""Pure text parsing and filtering for OCR candidates."""

import re
from collections.abc import Callable


KEYWORD_PATTERN = re.compile(
    r"\b(LOTE|LOT|SECTOR|SEC|BLOCO|BL|APARTAMENTO|APT|FRACCAO|FRACAO)\s*(\d{1,4})\b",
    re.IGNORECASE,
)
DOOR_NUMBER_PATTERN = re.compile(r"[A-Za-z]?\d{1,4}[A-Za-z]?")
BLOCKLIST: set[str] = {
    "2024G", "2023G", "2022G", "2025G", "2026G",
    "G000", "G001", "G002", "G003", "G004",
}


def parse_ocr_text(text: str, log_info: Callable[[str], None] | None = None) -> list[str]:
    upper_text = text.strip().upper()
    keyword_hits = KEYWORD_PATTERN.findall(upper_text)
    if keyword_hits:
        combined = "-".join(f"{keyword}{number}" for keyword, number in keyword_hits)
        if combined in BLOCKLIST:
            if log_info:
                log_info(f"OCR: blocked keyword fragment '{combined}'")
            return []
        return [combined]

    candidates = []
    for number in DOOR_NUMBER_PATTERN.findall(upper_text):
        if re.match(r"0\d", number):
            if log_info:
                log_info(f"OCR: skipped leading-zero fragment '{number}'")
            continue
        if number in BLOCKLIST:
            if log_info:
                log_info(f"OCR: blocked fragment '{number}' (blocklist)")
            continue
        candidates.append(number)
    return candidates