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
    """Mirrors the client interface: one camera lookup, then image fetches."""

    def __init__(
        self,
        image_result=_DEFAULT_IMAGE,
        cancel_event=None,
        resolve_error=None,
        cancel_on_resolve_error=False,
    ):
        self.requests = []
        self.resolved = []
        self.cameras = []
        self.camera = CameraPosition(41.0, -8.001, None, False)
        self.image_result = (
            Image.new("RGB", (2, 2)) if image_result is _DEFAULT_IMAGE else image_result
        )
        self.cancel_event = cancel_event
        self.resolve_error = resolve_error
        self.cancel_on_resolve_error = cancel_on_resolve_error

    def resolve_camera(self, latitude, longitude):
        self.resolved.append((latitude, longitude))
        if self.resolve_error is not None:
            if self.cancel_on_resolve_error and self.cancel_event is not None:
                self.cancel_event.set()
            raise self.resolve_error
        return self.camera

    def get_image(self, camera, **kwargs):
        self.requests.append((camera, kwargs))
        self.cameras.append(camera)
        if self.cancel_event is not None:
            self.cancel_event.set()
        return self.image_result

    def get_call_counts(self):
        return len(self.requests), len(self.resolved)


_BOX = [[0, 0], [1, 0], [1, 1], [0, 1]]


def page(*reads):
    """One OCR page with (text, confidence) reads."""
    return [[[_BOX, read] for read in reads]]


class FakeOCR:
    """Returns scripted pages in order; the last page repeats for later images."""

    def __init__(self, pages=None, error=None, cancel_event=None):
        self.images = []
        self.pages = pages or [page(("2024", 0.99), ("17", 0.90)), page(("17", 1.0))]
        self.error = error
        self.cancel_event = cancel_event

    def ocr(self, image):
        self.images.append(image.shape)
        if self.error is not None:
            if self.cancel_event is not None:
                self.cancel_event.set()
            raise self.error
        return self.pages[min(len(self.images), len(self.pages)) - 1]


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
        self.assertEqual(result.outcome, DetectionOutcome.FOUND)
        self.assertEqual(result.door_number, "17")
        self.assertEqual(ocr.images, [(4, 4, 3), (4, 4, 3)])
        self.assertEqual(len(street_view.requests), 2)
        self.assertEqual(street_view.resolved, [(41.0, -8.0)])
        self.assertEqual(street_view.requests[0][1]["heading"], 90)
        self.assertEqual(result.image_calls, 2)
        self.assertEqual(result.metadata_calls, 1)
        self.assertEqual(database.saved[0]["door_number"], "17")
        self.assertEqual(database.saved[0]["outcome"], DetectionOutcome.FOUND)
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

    def test_real_number_beats_year_watermark_in_the_same_image(self):
        street_view = FakeStreetView()
        ocr = FakeOCR([page(("2024", 0.99), ("17", 0.90))])
        detector = self._make_detector(FakeDatabase(), street_view, ocr)

        result = detector.detect_door_number(41.0, -8.0, heading=90)

        self.assertEqual(result.door_number, "17")
        self.assertEqual(result.confidence, 90)
        # A year-like best never stops the sweep early, so every angle is tried.
        self.assertEqual(len(street_view.requests), 91)

    def test_sweep_continues_past_watermark_fragment_to_a_later_number(self):
        street_view = FakeStreetView()
        ocr = FakeOCR([page(("23", 0.99)), page(("17", 0.90))])
        detector = self._make_detector(FakeDatabase(), street_view, ocr)

        result = detector.detect_door_number(41.0, -8.0, heading=90)

        self.assertEqual(result.door_number, "17")
        self.assertGreater(len(street_view.requests), 1)

    def test_sweep_stops_on_a_clean_confident_number(self):
        street_view = FakeStreetView()
        ocr = FakeOCR([page(("17", 0.95))])
        detector = self._make_detector(FakeDatabase(), street_view, ocr)

        result = detector.detect_door_number(41.0, -8.0, heading=90)

        self.assertEqual(result.door_number, "17")
        self.assertEqual(len(street_view.requests), 1)

    def test_supplied_image_uses_the_same_selection_rules(self):
        street_view = FakeStreetView()
        ocr = FakeOCR([page(("2024", 0.99), ("17", 0.90))])
        detector = self._make_detector(FakeDatabase(), street_view, ocr)

        result = detector.detect_door_number(
            41.0, -8.0, heading=10, pitch=5, image=Image.new("RGB", (2, 2))
        )

        self.assertEqual(result.door_number, "17")
        self.assertEqual((result.heading, result.pitch), (10, 5))

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
        grey = None  # the client returns nothing for a grey placeholder
        cases = (
            (
                DetectionOutcome.NO_IMAGERY,
                FakeStreetView(resolve_error=NoImageryAvailable("none")),
                FakeOCR(),
                "No Street View imagery available",
                (0, 1),
            ),
            (
                DetectionOutcome.IMAGE_UNAVAILABLE,
                FakeStreetView(grey),
                FakeOCR(),
                "Image unavailable",
                (91, 1),
            ),
            (
                DetectionOutcome.LOW_CONFIDENCE,
                FakeStreetView(),
                FakeOCR([page(("17", 0.35))]),
                "Low confidence; manual review recommended.",
                (91, 1),
            ),
            (
                DetectionOutcome.FAILED,
                FakeStreetView(),
                FakeOCR(error=RuntimeError("private failure detail")),
                "Detection failed",
                (1, 1),
            ),
        )

        for outcome, street_view, ocr, observation, usage in cases:
            with self.subTest(outcome=outcome):
                database = FakeDatabase()
                detector = self._make_detector(database, street_view, ocr)

                result = detector.detect_door_number(41.0, -8.0, heading=90)

                self.assertEqual(result.outcome, outcome)
                self.assertFalse(result.success)
                self.assertEqual(result.observation, observation)
                self.assertEqual((result.image_calls, result.metadata_calls), usage)
                if outcome is DetectionOutcome.LOW_CONFIDENCE:
                    self.assertIn("Low confidence", result.error or "")
                else:
                    self.assertEqual(result.error, observation)
                self.assertEqual(len(database.saved), 1)
                self.assertEqual(database.saved[0]["outcome"], outcome)
                self.assertNotIn("private failure detail", result.error or "")

    def test_cancelled_before_the_sweep_makes_no_calls_and_saves_nothing(self):
        cancel_event = threading.Event()
        cancel_event.set()
        database = FakeDatabase()
        street_view = FakeStreetView()
        detector = self._make_detector(database, street_view, FakeOCR())

        result = detector.detect_door_number(41.0, -8.0, heading=90, cancel_event=cancel_event)

        self.assertEqual(result.outcome, DetectionOutcome.CANCELLED)
        self.assertEqual((result.image_calls, result.metadata_calls), (0, 0))
        self.assertEqual(street_view.get_call_counts(), (0, 0))
        self.assertEqual(database.saved, [])

    def test_cancelled_sweep_reports_usage_without_persisting(self):
        cancel_event = threading.Event()
        database = FakeDatabase()
        street_view = FakeStreetView(cancel_event=cancel_event)
        detector = self._make_detector(database, street_view, FakeOCR())

        result = detector.detect_door_number(41.0, -8.0, heading=90, cancel_event=cancel_event)

        self.assertEqual(result.outcome, DetectionOutcome.CANCELLED)
        self.assertEqual((result.image_calls, result.metadata_calls), (1, 1))
        self.assertEqual(database.saved, [])

    def test_cancelled_no_imagery_exception_reports_usage_without_persisting(self):
        cancel_event = threading.Event()
        database = FakeDatabase()
        street_view = FakeStreetView(
            cancel_event=cancel_event,
            resolve_error=NoImageryAvailable("none"),
            cancel_on_resolve_error=True,
        )
        detector = self._make_detector(database, street_view, FakeOCR())

        result = detector.detect_door_number(41.0, -8.0, heading=90, cancel_event=cancel_event)

        self.assertEqual(result.outcome, DetectionOutcome.CANCELLED)
        self.assertEqual((result.image_calls, result.metadata_calls), (0, 1))
        self.assertEqual(database.saved, [])

    def test_cancelled_failure_exception_reports_usage_without_persisting(self):
        cancel_event = threading.Event()
        database = FakeDatabase()
        street_view = FakeStreetView()
        ocr = FakeOCR(error=RuntimeError("cancelled during OCR"), cancel_event=cancel_event)
        detector = self._make_detector(database, street_view, ocr)

        result = detector.detect_door_number(41.0, -8.0, heading=90, cancel_event=cancel_event)

        self.assertEqual(result.outcome, DetectionOutcome.CANCELLED)
        self.assertEqual((result.image_calls, result.metadata_calls), (1, 1))
        self.assertEqual(database.saved, [])


if __name__ == "__main__":
    unittest.main()
