"""Central application logging configuration."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from door_number_detector.core.config import Config


def configure_logging(data_dir: Path | None = None) -> Path:
    log_path = (Path(data_dir) if data_dir is not None else Config().data_dir) / "logs" / "door_detector.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    handler_name = f"door_number_detector:{log_path}"

    for handler in root_logger.handlers:
        if handler.get_name() == handler_name:
            return log_path

    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    handlers = [
        RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
        logging.StreamHandler(sys.stderr),
    ]
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.set_name(handler_name)
        root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
    return log_path