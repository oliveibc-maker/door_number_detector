"""Image OCR and candidate ranking for door-number detection."""

import logging
from collections import Counter

import cv2
import numpy as np
from PIL import Image

from core.parsing import parse_ocr_text


class DoorNumberOCR:
    def __init__(self, reader=None, logger: logging.Logger | None = None):
        if reader is None:
            from paddleocr import PaddleOCR

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

    def extract_door_number(self, image: Image.Image) -> tuple[str | None, int]:
        cv_image = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        scan_image = self._upscale(cv_image, scale=2)
        candidates: list[tuple[str, float]] = []

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
                    candidates.append((number, confidence))
                    self.logger.info(
                        "PaddleOCR [full_2x] '%s' <- '%s' (%.0f%%)",
                        number,
                        text,
                        confidence * 100,
                    )

        if not candidates:
            self.logger.info("No candidates found")
            return None, 0

        best_confidence: dict[str, float] = {}
        confidence_sum: dict[str, float] = {}
        for number, confidence in candidates:
            if number not in best_confidence or confidence > best_confidence[number]:
                best_confidence[number] = confidence
            confidence_sum[number] = confidence_sum.get(number, 0.0) + confidence

        vote_counts = Counter(number for number, _ in candidates)
        best_number = max(
            best_confidence,
            key=lambda number: (
                best_confidence[number],
                vote_counts[number],
                confidence_sum[number],
            ),
        )
        confidence_pct = int(best_confidence[best_number] * 100)
        self.logger.info(
            "Best: '%s' (%s%%) conf_sum=%.2f votes=%s/%s",
            best_number,
            confidence_pct,
            confidence_sum[best_number],
            vote_counts[best_number],
            len(candidates),
        )
        return best_number, confidence_pct