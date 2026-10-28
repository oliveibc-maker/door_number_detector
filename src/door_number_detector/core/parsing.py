"""Pure text parsing and filtering for OCR candidates."""

import re
from collections.abc import Callable

KEYWORD_PATTERN = re.compile(
    r"\b(LOTE|LOT|SECTOR|SEC|BLOCO|BL|APARTAMENTO|APT|FRACCAO|FRACAO)\s*(\d{1,4})\b",
    re.IGNORECASE,
)
DOOR_NUMBER_PATTERN = re.compile(r"[A-Za-z]?\d{1,4}(?:[ -]?[A-Za-z])?(?![A-Za-z])")
# Google's copyright, as OCR misreads it: "© 2023 Google", "2023Go0gle", "0-2023Gocqle".
# Street View prints it in the bottom strip and sometimes inside the panorama itself,
# so a whole OCR line that looks like it is dropped instead of being split into
# fragments such as "O0G" or "0".
GOOGLE_WATERMARK_PATTERN = re.compile(r"(?:19|20)\d{2}\s*G[O0Q]\b|G[O0CQ]{2}[G6CQ]L")
BLOCKLIST: set[str] = {
    "2024G",
    "2023G",
    "2022G",
    "2025G",
    "2026G",
    "G000",
    "G001",
    "G002",
    "G003",
    "G004",
}


def parse_ocr_text(text: str, log_info: Callable[[str], None] | None = None) -> list[str]:
    upper_text = text.strip().upper()
    if GOOGLE_WATERMARK_PATTERN.search(upper_text):
        if log_info:
            log_info(f"OCR: ignored Google watermark text '{text.strip()}'")
        return []
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
        number = number.replace(" ", "").replace("-", "")
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
