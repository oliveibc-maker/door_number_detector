import io
import logging
import sys
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import patch

from door_number_detector.logging_config import RedactingFormatter, configure_logging


class LoggingConfigTests(unittest.TestCase):
    def test_formatter_redacts_api_keys_in_messages_and_tracebacks(self):
        formatter = RedactingFormatter("%(message)s")
        try:
            raise RuntimeError("https://example.test/?key=secret&size=1")
        except RuntimeError:
            record = logging.LogRecord(
                "test",
                logging.ERROR,
                __file__,
                1,
                "failed: %s",
                ("https://example.test/?size=1&KEY=secret2",),
                sys.exc_info(),
            )

        output = formatter.format(record)

        self.assertNotIn("secret", output)
        self.assertIn("[REDACTED]", output)
        self.assertIn("size=1", output)

    def test_configures_rotating_file_and_stream_handlers_once(self):
        root_logger = logging.getLogger()
        original_level = root_logger.level

        with tempfile.TemporaryDirectory() as directory, patch("sys.stderr", io.StringIO()):
            log_path = Path(directory) / "logs" / "door_detector.log"
            try:
                self.assertEqual(configure_logging(Path(directory)), log_path)
                configured_handlers = [
                    handler
                    for handler in root_logger.handlers
                    if handler.get_name() == f"door_number_detector:{log_path}"
                ]
                self.assertEqual(len(configured_handlers), 2)
                self.assertTrue(
                    any(isinstance(handler, RotatingFileHandler) for handler in configured_handlers)
                )

                logger = logging.getLogger("door_number_detector.test")
                logger.info("rotation configured")
                for handler in configured_handlers:
                    handler.flush()
                self.assertIn("rotation configured", log_path.read_text(encoding="utf-8"))

                configure_logging(Path(directory))
                repeated_handlers = [
                    handler
                    for handler in root_logger.handlers
                    if handler.get_name() == f"door_number_detector:{log_path}"
                ]
                self.assertEqual(repeated_handlers, configured_handlers)
            finally:
                for handler in list(root_logger.handlers):
                    if handler.get_name() == f"door_number_detector:{log_path}":
                        root_logger.removeHandler(handler)
                        handler.close()
                root_logger.setLevel(original_level)


if __name__ == "__main__":
    unittest.main()
