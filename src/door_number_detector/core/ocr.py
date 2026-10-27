"""Image OCR and candidate ranking for door-number detection."""

import logging
import os

import cv2
import numpy as np
from PIL import Image

from door_number_detector.core.candidate_selection import Candidate
from door_number_detector.core.parsing import parse_ocr_text

# Street View draws its logo and copyright text in the bottom strip of every image.
WATERMARK_STRIP_FRACTION = 0.06


class DoorNumberOCR:
    def __init__(self, reader=None, logger: logging.Logger | None = None):
        if reader is None:
            os.environ["FLAGS_use_mkldnn"] = "0"
            os.environ["PADDLE_DISABLE_MKLDNN"] = "1"
            os.environ["FLAGS_enable_pir_in_executor"] = "0"
            root_level = logging.getLogger().level
            from paddleocr import PaddleOCR

            logging.getLogger().setLevel(root_level)  # Paddle import resets it to WARNING.
            logger = logger or logging.getLogger(__name__)
            logger.info("Loading PaddleOCR model (first run downloads models)...")
            reader = PaddleOCR(
                lang="en",
                use_angle_cls=True,
                use_gpu=False,
                show_log=False,
                det_db_thresh=0.2,
                det_db_box_thresh=0.4,
            )
        self.reader = reader
        self.logger = logger or logging.getLogger(__name__)

    @staticmethod
    def _upscale(image: np.ndarray, scale: int = 2) -> np.ndarray:
        height, width = image.shape[:2]
        return cv2.resize(
            image,
            (width * scale, height * scale),
            interpolation=cv2.INTER_CUBIC,
        )

    def extract_door_number(self, image: Image.Image) -> list[Candidate]:
        cv_image = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        height = cv_image.shape[0]
        cv_image = cv_image[: height - int(height * WATERMARK_STRIP_FRACTION)]
        scan_image = self._upscale(cv_image, scale=2)
        candidates: list[Candidate] = []

        self.logger.info("OCR [PaddleOCR]: full image 2x upscale...")
        ocr_result = self.reader.ocr(scan_image)
        if ocr_result:
            if (
                isinstance(ocr_result[0], list)
                and len(ocr_result[0]) >= 2
                and isinstance(ocr_result[0][1], tuple)
            ):
                page = ocr_result
            else:
                page = ocr_result[0]

            for line in page or []:
                bbox, (text, confidence) = line
                for number in parse_ocr_text(text, self.logger.info):
                    candidates.append(Candidate(number, int(confidence * 100)))
                    self.logger.info(
                        "PaddleOCR [full_2x] '%s' <- '%s' (%.0f%%)",
                        number,
                        text,
                        confidence * 100,
                    )

        if not candidates:
            self.logger.info("No candidates found")
            return []
        return candidates
