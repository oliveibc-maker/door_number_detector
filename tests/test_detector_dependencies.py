import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image


class FakeDatabase:
    def __init__(self):
        self.saved = []
        self.closed = False

    def save_result(self, result):
        self.saved.append(result)

    def close(self):
        self.closed = True


class FakeStreetView:
    def __init__(self):
        self.requests = []

    def get_image(self, latitude, longitude, **kwargs):
        self.requests.append((latitude, longitude, kwargs))
        return Image.new("RGB", (2, 2))


class FakeOCR:
    def __init__(self):
        self.images = []

    def ocr(self, image):
        self.images.append(image.shape)
        box = [[0, 0], [1, 0], [1, 1], [0, 1]]
        return [[[box, ("17", 0.95)]]]


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

    def test_detector_uses_injected_services_for_sweep_and_persistence(self):
        config = SimpleNamespace(
            data_dir=Path(self.data_directory.name),
            debug=False,
            confidence_threshold=70,
        )
        database = FakeDatabase()
        street_view = FakeStreetView()
        ocr = FakeOCR()
        detector = self.detector_type(
            config=config,
            database_manager=database,
            street_view_fetcher=street_view,
            ocr_engine=ocr,
            debug_root=Path(self.data_directory.name) / "debug",
        )

        try:
            result = detector.detect_door_number(41.0, -8.0, heading=90)
        finally:
            detector.close()

        self.assertTrue(result.success)
        self.assertEqual(result.door_number, "17")
        self.assertEqual(ocr.images, [(4, 4, 3)])
        self.assertEqual(len(street_view.requests), 1)
        self.assertEqual(database.saved[0]["door_number"], "17")
        self.assertTrue(database.closed)


if __name__ == "__main__":
    unittest.main()
