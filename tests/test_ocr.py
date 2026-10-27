import unittest

import numpy as np
from PIL import Image

from door_number_detector.core.candidate_selection import Candidate
from door_number_detector.core.ocr import DoorNumberOCR


class FakeOcrReader:
    def __init__(self, result):
        self.result = result
        self.image_shapes = []

    def ocr(self, image):
        self.image_shapes.append(image.shape)
        return self.result


WATERMARK_PIXEL = (255, 0, 255)
DOOR_PIXEL = (0, 255, 255)


class PixelReadingOcrReader:
    """Reads the Google watermark or a door number only if its pixels reach OCR."""

    def ocr(self, image):
        rgb = image[:, :, ::-1]
        box = [[0, 0], [1, 0], [1, 1], [0, 1]]
        lines = []
        if (rgb == DOOR_PIXEL).all(axis=2).any():
            lines.append([box, ("17", 0.9)])
        if (rgb == WATERMARK_PIXEL).all(axis=2).any():
            lines.append([box, ("© 2026 O0G", 0.95)])
        return [lines]


def _street_view_frame(with_door_number):
    pixels = np.zeros((640, 640, 3), dtype=np.uint8)
    pixels[618:640, 0:65] = WATERMARK_PIXEL  # "Google" logo, bottom left
    pixels[624:640, 560:640] = WATERMARK_PIXEL  # "© 2026 Google", bottom right
    if with_door_number:
        pixels[300:340, 300:340] = DOOR_PIXEL
    return Image.fromarray(pixels, mode="RGB")


class OcrServiceTests(unittest.TestCase):
    def test_upscales_image_and_returns_every_parsed_candidate(self):
        box = [[0, 0], [1, 0], [1, 1], [0, 1]]
        reader = FakeOcrReader(
            [
                [
                    [box, ("12A", 0.88)],
                    [box, ("8", 0.96)],
                    [box, ("8", 0.91)],
                ]
            ]
        )
        service = DoorNumberOCR(reader=reader)
        image = Image.fromarray(np.zeros((3, 4, 3), dtype=np.uint8), mode="RGB")

        result = service.extract_door_number(image)

        self.assertEqual(
            result,
            [Candidate("12A", 88), Candidate("8", 96), Candidate("8", 91)],
        )
        self.assertEqual(reader.image_shapes, [(6, 8, 3)])

    def test_excludes_the_watermark_strip_from_the_scanned_image(self):
        reader = FakeOcrReader([[]])
        service = DoorNumberOCR(reader=reader)
        image = Image.fromarray(np.zeros((100, 50, 3), dtype=np.uint8), mode="RGB")

        service.extract_door_number(image)

        self.assertEqual(reader.image_shapes, [(188, 100, 3)])

    def test_google_watermark_is_not_read_as_a_door_number(self):
        service = DoorNumberOCR(reader=PixelReadingOcrReader())

        self.assertEqual(
            service.extract_door_number(_street_view_frame(with_door_number=True)),
            [Candidate("17", 90)],
        )
        self.assertEqual(
            service.extract_door_number(_street_view_frame(with_door_number=False)), []
        )

    def test_returns_no_candidate_for_empty_ocr_page(self):
        reader = FakeOcrReader([[]])
        service = DoorNumberOCR(reader=reader)
        image = Image.fromarray(np.zeros((2, 2, 3), dtype=np.uint8), mode="RGB")

        self.assertEqual(service.extract_door_number(image), [])


if __name__ == "__main__":
    unittest.main()
