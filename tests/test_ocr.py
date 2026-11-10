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

    def __init__(self):
        self.watermark_pixels_seen = False

    def ocr(self, image):
        rgb = image[:, :, ::-1]
        box = [[0, 0], [1, 0], [1, 1], [0, 1]]
        lines = []
        if (rgb == DOOR_PIXEL).all(axis=2).any():
            lines.append([box, ("17", 0.9)])
        if (rgb == WATERMARK_PIXEL).all(axis=2).any():
            self.watermark_pixels_seen = True
            lines.append([box, ("© 2026 O0G", 0.95)])
        return [lines]


def _street_view_frame(with_door_number, door_at_bottom=False):
    pixels = np.zeros((640, 640, 3), dtype=np.uint8)
    pixels[618:640, 0:65] = WATERMARK_PIXEL  # "Google" logo, bottom left
    pixels[624:640, 560:640] = WATERMARK_PIXEL  # "© 2026 Google", bottom right
    if with_door_number:
        y_start = 618 if door_at_bottom else 300
        pixels[y_start : y_start + 22, 300:340] = DOOR_PIXEL
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

    def test_masks_the_watermark_strip_without_cropping_the_image(self):
        reader = FakeOcrReader([[]])
        service = DoorNumberOCR(reader=reader)
        image = Image.fromarray(np.zeros((100, 50, 3), dtype=np.uint8), mode="RGB")

        service.extract_door_number(image)

        self.assertEqual(reader.image_shapes, [(200, 100, 3)])

    def test_google_watermark_is_not_read_as_a_door_number(self):
        service = DoorNumberOCR(reader=PixelReadingOcrReader())

        self.assertEqual(
            service.extract_door_number(_street_view_frame(with_door_number=True)),
            [Candidate("17", 90)],
        )
        self.assertEqual(
            service.extract_door_number(_street_view_frame(with_door_number=False)), []
        )

    def test_preserves_bottom_center_while_excluding_watermark_corners(self):
        reader = PixelReadingOcrReader()
        service = DoorNumberOCR(reader=reader)

        self.assertEqual(
            service.extract_door_number(
                _street_view_frame(with_door_number=True, door_at_bottom=True)
            ),
            [Candidate("17", 90)],
        )
        self.assertFalse(reader.watermark_pixels_seen)

    def test_returns_no_candidate_for_empty_ocr_page(self):
        reader = FakeOcrReader([[]])
        service = DoorNumberOCR(reader=reader)
        image = Image.fromarray(np.zeros((2, 2, 3), dtype=np.uint8), mode="RGB")

        self.assertEqual(service.extract_door_number(image), [])

    def test_extracts_raw_observations_with_original_image_geometry(self):
        number_box = [[40, 100], [80, 100], [80, 120], [40, 120]]
        letter_box = [[90, 100], [100, 100], [100, 120], [90, 120]]
        reader = FakeOcrReader([[number_box, ("20", 0.9)], [letter_box, ("A", 0.8)]])
        service = DoorNumberOCR(reader=reader)
        image = Image.fromarray(np.zeros((200, 100, 3), dtype=np.uint8), mode="RGB")

        observations = service.extract_observations(image)

        self.assertEqual([item.text for item in observations], ["20", "A"])
        self.assertEqual([item.number_candidates for item in observations], [("20",), ()])
        self.assertEqual(observations[0].box, (0.2, 0.25, 0.2, 0.05))
        self.assertEqual(observations[1].box, (0.45, 0.25, 0.05, 0.05))
        self.assertEqual([item.confidence for item in observations], [0.9, 0.8])


if __name__ == "__main__":
    unittest.main()
