import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from door_number_detector.core.google_street_view import CameraPosition, NoImageryAvailable
from door_number_detector.core.models import DetectionOutcome


class FakeDatabase:
    def __init__(self):
        self.saved = []
        self.closed = False

    def save_result(self, result):
        self.saved.append(result)

    def close(self):
        self.closed = True


_DEFAULT_IMAGE = object()


class FakeStreetView:
    def __init__(self, image_result=_DEFAULT_IMAGE, cancel_event=None):
        self.requests = []
        self.resolved = []
        self.cameras = []
        self.image_calls = 0
        self.camera = CameraPosition(41.0, -8.001, None, False)
        self.image_result = (
            Image.new("RGB", (2, 2)) if image_result is _DEFAULT_IMAGE else image_result
        )
        self.cancel_event = cancel_event

    def resolve_camera(self, latitude, longitude):
        self.resolved.append((latitude, longitude))
        return self.camera

    def get_image(self, camera, **kwargs):
        self.requests.append((camera, kwargs))
        self.cameras.append(camera)
        self.image_calls += 1
        if self.cancel_event is not None:
            self.cancel_event.set()
        if isinstance(self.image_result, Exception):
            raise self.image_result
        return self.image_result

    def get_call_counts(self):
        return self.image_calls, 0


class FakeOCR:
    def __init__(self, response=None, error=None):
        self.images = []
        self.response = response
        self.error = error

    def ocr(self, image):
        self.images.append(image.shape)
        if self.error is not None:
            raise self.error
        if self.response is not None:
            return self.response
        box = [[0, 0], [1, 0], [1, 1], [0, 1]]
        if len(self.images) == 1:
            return [[[box, ("2024", 0.99)], [box, ("17", 0.90)]]]
        return [[[box, ("17", 1.0)]]]


class DetectorDependencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_directory = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {"APP_DATA_DIR": cls.data_directory.name}):
            from door_number_detector.core.detector import DoorNumberDetector

        cls.detector_type = DoorNumberDetector

    @classmethod
    def tearDownClass(cls):
        cls.data_directory.cleanup()

    def _make_detector(self, database, street_view, ocr):
        return self.detector_type(
            config=SimpleNamespace(
                data_dir=Path(self.data_directory.name), debug=False, confidence_threshold=70
            ),
            database_manager=database,
            street_view_fetcher=street_view,
            ocr_engine=ocr,
            debug_root=Path(self.data_directory.name) / "debug",
        )

    def test_detector_uses_injected_services_for_sweep_and_persistence(self):
        database = FakeDatabase()
        street_view = FakeStreetView()
        ocr = FakeOCR()
        detector = self._make_detector(database, street_view, ocr)

        try:
            result = detector.detect_door_number(41.0, -8.0, heading=90)
        finally:
            detector.close()

        self.assertTrue(result.success)
        self.assertEqual(result.door_number, "17")
        self.assertEqual(ocr.images, [(4, 4, 3), (4, 4, 3)])
        self.assertEqual(len(street_view.requests), 2)
        self.assertEqual(street_view.resolved, [(41.0, -8.0)])
        self.assertEqual(street_view.requests[0][1]["heading"], 90)
        self.assertEqual(result.image_calls, 2)
        self.assertEqual(result.metadata_calls, 0)
        self.assertEqual(database.saved[0]["door_number"], "17")
        self.assertTrue(database.closed)

    def test_unexpected_error_redacts_api_key_in_logs_and_result(self):
        config = SimpleNamespace(
            data_dir=Path(self.data_directory.name),
            debug=False,
            confidence_threshold=70,
        )
        detector = self.detector_type(
            config=config,
            database_manager=FakeDatabase(),
            street_view_fetcher=FakeStreetView(),
            ocr_engine=FakeOCR(),
            debug_root=Path(self.data_directory.name) / "debug-unexpected",
        )

        try:
            with (
                patch.object(
                    detector.sweep,
                    "fetch_candidates",
                    side_effect=RuntimeError("failed: https://example.test/?size=1&key=secret"),
                ),
                self.assertLogs("door_number_detector.core.detector", level="ERROR") as logs,
            ):
                result = detector.detect_door_number(41.0, -8.0, heading=90)
        finally:
            detector.close()

        self.assertFalse(result.success)
        self.assertNotIn("secret", result.error or "")
        self.assertEqual(result.error, "Detection failed")
        self.assertNotIn("secret", logs.records[0].getMessage())
        self.assertIn("[REDACTED]", logs.records[0].getMessage())

    def test_detector_resolves_camera_even_with_a_supplied_heading(self):
        street_view = FakeStreetView()
        detector = self._make_detector(FakeDatabase(), street_view, FakeOCR())

        detector.detect_door_number(41.0, -8.0, heading=123)

        self.assertEqual(street_view.resolved, [(41.0, -8.0)])
        self.assertEqual(street_view.requests[0][1]["heading"], 123)

    def test_missing_heading_uses_camera_to_target_heading(self):
        street_view = FakeStreetView()
        detector = self._make_detector(FakeDatabase(), street_view, FakeOCR())

        detector.detect_door_number(41.0, -8.0)

        self.assertAlmostEqual(street_view.requests[0][1]["heading"], 90, places=3)

    def test_fallback_camera_is_passed_to_the_sweep(self):
        street_view = FakeStreetView()
        street_view.camera = CameraPosition(41.0, -8.0, None, True)
        detector = self._make_detector(FakeDatabase(), street_view, FakeOCR())

        detector.detect_door_number(41.0, -8.0, heading=123)

        self.assertTrue(street_view.cameras)
        self.assertTrue(all(camera.fallback for camera in street_view.cameras))

    def test_supplied_image_does_not_touch_street_view_client(self):
        street_view = FakeStreetView()
        detector = self._make_detector(FakeDatabase(), street_view, FakeOCR())

        detector.detect_door_number(41.0, -8.0, heading=123, image=Image.new("RGB", (2, 2)))

        self.assertEqual(street_view.resolved, [])
        self.assertEqual(street_view.requests, [])
        self.assertEqual(street_view.get_call_counts(), (0, 0))

    def test_detector_returns_and_persists_each_finished_outcome(self):
        box = [[0, 0], [1, 0], [1, 1], [0, 1]]
        cases = (
            (
                DetectionOutcome.NO_IMAGERY,
                FakeStreetView(NoImageryAvailable("none")),
                FakeOCR(),
                "No Street View imagery available",
            ),
            (
                DetectionOutcome.IMAGE_UNAVAILABLE,
                FakeStreetView(None),
                FakeOCR(),
                "Image unavailable",
            ),
            (
                DetectionOutcome.LOW_CONFIDENCE,
                FakeStreetView(),
                FakeOCR(response=[[[box, ("17", 0.35)]]]),
                "Low confidence; manual review recommended.",
            ),
            (
                DetectionOutcome.FAILED,
                FakeStreetView(),
                FakeOCR(error=RuntimeError("private failure detail")),
                "Detection failed",
            ),
        )

        for outcome, street_view, ocr, observation in cases:
            with self.subTest(outcome=outcome):
                database = FakeDatabase()
                detector = self._make_detector(database, street_view, ocr)

                result = detector.detect_door_number(41.0, -8.0, heading=90)

                self.assertEqual(result.outcome, outcome)
                self.assertEqual(result.observation, observation)
                if outcome is DetectionOutcome.LOW_CONFIDENCE:
                    self.assertIn("Low confidence", result.error or "")
                else:
                    self.assertEqual(result.error, observation)
                self.assertEqual(len(database.saved), 1)
                self.assertEqual(database.saved[0]["outcome"], outcome)
                self.assertNotIn("private failure detail", result.error or "")

    def test_cancelled_sweep_reports_usage_without_persisting(self):
        cancel_event = threading.Event()
        database = FakeDatabase()
        street_view = FakeStreetView(cancel_event=cancel_event)
        detector = self._make_detector(database, street_view, FakeOCR())

        result = detector.detect_door_number(41.0, -8.0, heading=90, cancel_event=cancel_event)

        self.assertEqual(result.outcome, DetectionOutcome.CANCELLED)
        self.assertEqual(result.image_calls, 1)
        self.assertEqual(database.saved, [])


if __name__ == "__main__":
    unittest.main()
