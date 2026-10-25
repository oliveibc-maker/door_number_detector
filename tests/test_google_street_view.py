import logging
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from door_number_detector.core.google_street_view import StreetViewFetcher

API_KEY = "test-secret-key-123"


class FailingSession:
    def __init__(self, error_type):
        self.error_type = error_type
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        query = "&".join(f"{name}={value}" for name, value in (params or {}).items())
        raise self.error_type(f"403 Client Error: Forbidden for url: {url}?{query}")


class FakeDatabase:
    def save_result(self, result):
        pass

    def close(self):
        pass


def _fetcher_with(session):
    fetcher = StreetViewFetcher(API_KEY)
    fetcher._session = session
    return fetcher


def _formatted(records):
    formatter = logging.Formatter("%(message)s")
    return "\n".join(formatter.format(record) for record in records)


class StreetViewKeyRedactionTests(unittest.TestCase):
    def test_metadata_request_errors_do_not_expose_api_key(self):
        for error_type in (requests.exceptions.HTTPError, requests.exceptions.ConnectionError):
            with self.subTest(error_type=error_type.__name__):
                fetcher = _fetcher_with(FailingSession(error_type))

                with self.assertRaises(ValueError) as raised:
                    fetcher.get_pano_location(41.0, -8.0)

                self.assertNotIn(API_KEY, str(raised.exception))
                self.assertIn("[REDACTED]", str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertTrue(raised.exception.__suppress_context__)

    def test_detection_logs_do_not_expose_api_key(self):
        with tempfile.TemporaryDirectory() as data_directory:
            with patch.dict(os.environ, {"APP_DATA_DIR": data_directory}):
                from door_number_detector.core.detector import DoorNumberDetector

            session = FailingSession(requests.exceptions.HTTPError)
            detector = DoorNumberDetector(
                config=SimpleNamespace(
                    data_dir=Path(data_directory), debug=False, confidence_threshold=70
                ),
                database_manager=FakeDatabase(),
                street_view_fetcher=_fetcher_with(session),
                ocr_engine=object(),
                debug_root=Path(data_directory) / "debug",
            )

            with self.assertLogs(level="DEBUG") as captured:
                result = detector.detect_door_number(41.0, -8.0)

        self.assertFalse(result.success)
        self.assertGreater(session.calls, 0)
        self.assertNotIn(API_KEY, _formatted(captured.records))
        self.assertNotIn(API_KEY, result.error or "")


if __name__ == "__main__":
    unittest.main()
