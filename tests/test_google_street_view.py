import logging
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import requests
from PIL import Image

from door_number_detector.core.google_street_view import (
    CameraPosition,
    NoImageryAvailable,
    StreetViewFetcher,
)

API_KEY = "test-secret-key-123"


class FakeResponse:
    def __init__(self, *, data=None, content=b"", headers=None, error=None):
        self.data = data or {}
        self.content = content
        self.headers = headers or {}
        self.error = error

    def raise_for_status(self):
        if self.error is not None:
            raise self.error

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, responses=(), error=None):
        self.responses = list(responses)
        self.error = error
        self.calls = []
        self.proxies = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        if self.error is not None:
            raise self.error
        if not self.responses:
            raise AssertionError("No fake response configured")
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def metadata_response(status="OK"):
    return FakeResponse(
        data={"status": status, "location": {"lat": 41.1, "lng": -8.1}, "heading": 35}
    )


def image_response(image=None):
    image = image or Image.new("RGB", (20, 20), (80, 120, 160))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return FakeResponse(content=buffer.getvalue(), headers={"content-type": "image/png"})


class FakeDatabase:
    def __init__(self):
        self.saved = []

    def save_result(self, result):
        self.saved.append(result)

    def close(self):
        pass


def _fetcher_with(session):
    return StreetViewFetcher(API_KEY, session=session)


def _formatted(records):
    formatter = logging.Formatter("%(message)s")
    return "\n".join(formatter.format(record) for record in records)


class StreetViewClientTests(unittest.TestCase):
    def test_no_panorama_is_the_only_metadata_failure_that_raises(self):
        fetcher = StreetViewFetcher(
            API_KEY, session=FakeSession([metadata_response("ZERO_RESULTS")])
        )

        with self.assertRaises(NoImageryAvailable) as raised:
            fetcher.resolve_camera(41.0, -8.0)

        self.assertNotIn(API_KEY, str(raised.exception))
        self.assertEqual(fetcher.get_call_counts(), (0, 1))

    def test_non_ok_status_uses_coordinate_fallback(self):
        fetcher = StreetViewFetcher(
            API_KEY, session=FakeSession([metadata_response("REQUEST_DENIED")])
        )

        self.assertEqual(fetcher.resolve_camera(41.0, -8.0), CameraPosition(41.0, -8.0, None, True))

    def test_http_and_network_lookup_errors_use_fallback_without_leaking_key(self):
        for error_type in (requests.exceptions.HTTPError, requests.exceptions.ConnectionError):
            with self.subTest(error_type=error_type.__name__):
                error = error_type(f"failure at https://example.test/?key={API_KEY}")
                fetcher = StreetViewFetcher(API_KEY, session=FakeSession(error=error))

                with self.assertLogs("door_number_detector.core.google_street_view") as captured:
                    camera = fetcher.resolve_camera(41.0, -8.0)

                self.assertTrue(camera.fallback)
                self.assertNotIn(API_KEY, _formatted(captured.records))
                self.assertEqual(fetcher.get_call_counts(), (0, 1))

    def test_failed_camera_lookups_are_not_cached(self):
        session = FakeSession(error=requests.exceptions.ConnectionError("offline"))
        fetcher = StreetViewFetcher(API_KEY, session=session)

        fetcher.resolve_camera(41.0, -8.0)
        fetcher.resolve_camera(41.0, -8.0)

        self.assertEqual(len(session.calls), 2)
        self.assertEqual(fetcher.get_call_counts(), (0, 2))

    def test_invalid_metadata_body_falls_back_cleanly(self):
        fetcher = StreetViewFetcher(
            API_KEY, session=FakeSession([FakeResponse(data={"status": "OK"})])
        )

        camera = fetcher.resolve_camera(41.0, -8.0)

        self.assertEqual(camera, CameraPosition(41.0, -8.0, None, True))

    def test_successful_camera_lookups_are_cached(self):
        session = FakeSession([metadata_response()])
        fetcher = StreetViewFetcher(API_KEY, session=session)

        first = fetcher.resolve_camera(41.0, -8.0)
        second = fetcher.resolve_camera(41.0, -8.0)

        self.assertEqual(first, second)
        self.assertEqual(len(session.calls), 1)

    def test_owned_session_keeps_the_configured_proxy(self):
        session = FakeSession([metadata_response()])
        with patch(
            "door_number_detector.core.google_street_view.requests.Session", return_value=session
        ):
            StreetViewFetcher(API_KEY, https_proxy="http://proxy.example.test")

        self.assertEqual(
            session.proxies,
            {"http": "http://proxy.example.test", "https": "http://proxy.example.test"},
        )

    def test_cache_evicts_the_least_recently_used_camera(self):
        with patch.object(StreetViewFetcher, "_CAMERA_CACHE_SIZE", 2):
            session = FakeSession([metadata_response()])
            fetcher = StreetViewFetcher(API_KEY, session=session)
            fetcher.resolve_camera(1.0, 1.0)
            fetcher.resolve_camera(2.0, 2.0)
            fetcher.resolve_camera(3.0, 3.0)
            fetcher.resolve_camera(1.0, 1.0)

        self.assertEqual(len(session.calls), 4)

    def test_image_fetch_uses_camera_without_a_metadata_request(self):
        session = FakeSession([image_response()])
        fetcher = StreetViewFetcher(API_KEY, session=session)
        camera = CameraPosition(41.1, -8.1, 35, False)

        image = fetcher.get_image(camera, heading=90, pitch=5, fov=10)

        self.assertIsNotNone(image)
        self.assertEqual(len(session.calls), 1)
        url, params, timeout = session.calls[0]
        self.assertEqual(url, StreetViewFetcher.BASE_URL)
        self.assertEqual(params["location"], "41.1,-8.1")
        self.assertEqual(params["heading"], 90)
        self.assertEqual(params["pitch"], 5)
        self.assertEqual(params["fov"], 10)
        self.assertEqual(timeout, 10)
        self.assertEqual(fetcher.get_call_counts(), (1, 0))

    def test_grey_placeholder_and_non_image_responses_return_nothing(self):
        grey = Image.fromarray(np.full((20, 20, 3), 120, dtype=np.uint8), mode="RGB")
        sessions = (
            FakeSession([image_response(grey)]),
            FakeSession([FakeResponse(headers={"content-type": "text/plain"})]),
        )
        camera = CameraPosition(41.1, -8.1, None, False)

        for session in sessions:
            with self.subTest(session=session):
                fetcher = StreetViewFetcher(API_KEY, session=session)
                self.assertIsNone(fetcher.get_image(camera, 90))

    def test_http_and_network_image_errors_return_nothing_without_key_leaks(self):
        camera = CameraPosition(41.1, -8.1, None, False)
        for error_type in (requests.exceptions.HTTPError, requests.exceptions.ConnectionError):
            with self.subTest(error_type=error_type.__name__):
                session = FakeSession(error=error_type(f"url?key={API_KEY}"))
                fetcher = StreetViewFetcher(API_KEY, session=session)
                with self.assertLogs("door_number_detector.core.google_street_view") as captured:
                    image = fetcher.get_image(camera, 90)
                self.assertIsNone(image)
                self.assertNotIn(API_KEY, _formatted(captured.records))

    def test_heading_math_handles_cardinal_directions_and_wraparound(self):
        self.assertAlmostEqual(StreetViewFetcher.calculate_heading(0, 0, 1, 0), 0)
        self.assertAlmostEqual(StreetViewFetcher.calculate_heading(0, 0, 0, 1), 90)
        self.assertAlmostEqual(StreetViewFetcher.calculate_heading(0, 179, 0, -179), 90)
        self.assertAlmostEqual(StreetViewFetcher.calculate_heading(0, 0, 0, -1), 270)

    def test_usage_counts_match_requests_sent(self):
        session = FakeSession([metadata_response(), image_response()])
        fetcher = StreetViewFetcher(API_KEY, session=session)
        camera = fetcher.resolve_camera(41.0, -8.0)
        fetcher.get_image(camera, 90)

        self.assertEqual(fetcher.get_call_counts(), (1, 1))
        self.assertEqual(len(session.calls), 2)

    def test_detector_logs_and_results_do_not_expose_api_key(self):
        with tempfile.TemporaryDirectory() as data_directory:
            with patch.dict(os.environ, {"APP_DATA_DIR": data_directory}):
                from door_number_detector.core.detector import DoorNumberDetector

            session = FakeSession(error=requests.exceptions.HTTPError(f"url?key={API_KEY}"))
            database = FakeDatabase()
            detector = DoorNumberDetector(
                config=SimpleNamespace(
                    data_dir=Path(data_directory), debug=False, confidence_threshold=70
                ),
                database_manager=database,
                street_view_fetcher=_fetcher_with(session),
                ocr_engine=object(),
                debug_root=Path(data_directory) / "debug",
            )

            with self.assertLogs(level="DEBUG") as captured:
                result = detector.detect_door_number(41.0, -8.0)

        self.assertFalse(result.success)
        self.assertGreater(len(session.calls), 0)
        self.assertNotIn(API_KEY, _formatted(captured.records))
        self.assertNotIn(API_KEY, result.error or "")


if __name__ == "__main__":
    unittest.main()
