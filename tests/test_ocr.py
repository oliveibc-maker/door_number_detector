import unittest

import numpy as np
from PIL import Image

from door_number_detector.core.ocr import DoorNumberOCR


class FakeOcrReader:
    def __init__(self, result):
        self.result = result
        self.image_shapes = []

    def ocr(self, image):
        self.image_shapes.append(image.shape)
        return self.result


class OcrServiceTests(unittest.TestCase):
    def test_upscales_image_and_ranks_candidates_by_confidence(self):
        box = [[0, 0], [1, 0], [1, 1], [0, 1]]
        reader = FakeOcrReader([[
            [box, ("12A", 0.88)],
            [box, ("8", 0.96)],
            [box, ("8", 0.91)],
        ]])
        service = DoorNumberOCR(reader=reader)
        image = Image.fromarray(np.zeros((3, 4, 3), dtype=np.uint8), mode="RGB")

        result = service.extract_door_number(image)

        self.assertEqual(result, ("8", 96))
        self.assertEqual(reader.image_shapes, [(6, 8, 3)])

    def test_returns_no_candidate_for_empty_ocr_page(self):
        reader = FakeOcrReader([[]])
        service = DoorNumberOCR(reader=reader)
        image = Image.fromarray(np.zeros((2, 2, 3), dtype=np.uint8), mode="RGB")

        self.assertEqual(service.extract_door_number(image), (None, 0))


if __name__ == "__main__":
    unittest.main()