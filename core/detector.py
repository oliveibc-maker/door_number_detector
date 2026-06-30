"""Main door number detection workflow."""

import logging
import os
import re
from datetime import datetime
from pathlib import Path

import pytesseract
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
try:
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None

from core.config import Config
from core.database import DatabaseManager
from core.google_street_view import StreetViewFetcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("door_detector.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


class DoorNumberDetector:
    """Detects door numbers using Google Street View images and OCR."""

    def __init__(self, env_path=".env"):
        self.config = Config(env_path)
        self._configure_tesseract()
        self.debug_dir = Path("ocr_debug")
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        self.db = DatabaseManager(self.config)
        self.street_view = StreetViewFetcher(self.config.google_api_key, size=self.config.street_view_size)
        logger.info("Door Number Detector initialized")

    def _configure_tesseract(self):
        tesseract_path = self.config.tesseract_path
        if not tesseract_path:
            return

        if os.path.isdir(tesseract_path):
            tesseract_path = os.path.join(tesseract_path, "tesseract.exe")

        pytesseract.pytesseract.tesseract_cmd = tesseract_path

        if not os.path.exists(tesseract_path):
            logger.warning(f"Tesseract executable not found at {tesseract_path}")
        else:
            logger.info(f"Using Tesseract executable: {tesseract_path}")

    def _save_debug_image(self, image, name):
        path = self.debug_dir / name
        image.save(path)
        return path

    def _build_preprocessing_variants(self, image):
        variants = []
        gray = image.convert("L")
        variants.append(("gray", gray))
        w, h = gray.size

        if cv2 is not None and np is not None:
            arr = np.array(gray)
            clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
            clahe_img = Image.fromarray(clahe.apply(arr))
            variants.append(("clahe", clahe_img))

            blur = cv2.GaussianBlur(arr, (5, 5), 0)
            variants.append(("blur", Image.fromarray(blur)))

            _, thresh = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            variants.append(("thresh", Image.fromarray(thresh)))

            thresh_inv = cv2.bitwise_not(thresh)
            variants.append(("thresh_inv", Image.fromarray(thresh_inv)))

            kernel = np.ones((2, 2), np.uint8)
            dilated = cv2.dilate(thresh, kernel, iterations=1)
            variants.append(("dilate", Image.fromarray(dilated)))

            crop_regions = [
                (0, 0, w, h // 3),
                (0, h // 3, w, 2 * h // 3),
                (0, 2 * h // 3, w, h),
            ]
            for index, box in enumerate(crop_regions, start=1):
                cropped = gray.crop(box)
                variants.append((f"crop_{index}", cropped))
                crop_arr = np.array(cropped)
                _, crop_thresh = cv2.threshold(crop_arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                variants.append((f"crop_{index}_thresh", Image.fromarray(crop_thresh)))
        else:
            variants.extend([
                ("resized", gray.resize((w * 2, h * 2), Image.BICUBIC)),
                ("enhanced_contrast", ImageEnhance.Contrast(gray).enhance(2.5)),
                ("sharpened", gray.filter(ImageFilter.SHARPEN)),
                ("inverted", ImageOps.invert(gray)),
                ("thresh", gray.point(lambda x: 0 if x < 150 else 255, mode="1")),
            ])

        return variants

    def _find_best_candidate_from_data(self, data):
        best_candidate = None
        best_confidence = 0

        for word, conf in zip(data.get("text", []), data.get("conf", [])):
            token = str(word).strip()
            if not token:
                continue

            try:
                confidence = int(float(conf))
            except (ValueError, TypeError):
                continue

            candidate = self._parse_door_number(token)
            if candidate and confidence > best_confidence:
                best_candidate = candidate
                best_confidence = confidence

        return best_candidate, best_confidence

    def _run_ocr_on_variant(self, image, variant_name, debug_prefix):
        best_candidate = None
        best_confidence = 0
        best_text = None
        best_psm = None

        psm_modes = [7, 6, 8, 10]
        for psm in psm_modes:
            config = (
                f"--oem 3 --psm {psm} -c tessedit_char_whitelist=0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-"
            )
            try:
                data = pytesseract.image_to_data(
                    image,
                    config=config,
                    lang=self.config.ocr_language,
                    output_type=pytesseract.Output.DICT,
                )
            except Exception as exc:
                logger.warning(f"Tesseract data extraction failed for {variant_name} psm={psm}: {exc}")
                continue

            text_tokens = [str(t).strip() for t in data.get("text", []) if str(t).strip()]
            raw_text = " ".join(text_tokens)
            logger.info(f"OCR {variant_name} psm={psm} raw: {repr(raw_text)}")

            candidate, confidence = self._find_best_candidate_from_data(data)
            if candidate:
                logger.info(
                    f"Candidate {candidate} with confidence {confidence}% from {variant_name} psm={psm}"
                )
                if confidence > best_confidence:
                    best_candidate = candidate
                    best_confidence = confidence
                    best_text = raw_text
                    best_psm = psm
            if best_confidence >= self.config.confidence_threshold:
                logger.info(
                    f"Early stop on {variant_name} psm={psm} with confidence {best_confidence}%"
                )
                break

        if best_candidate is None:
            logger.info(f"No number candidate found for {variant_name}")

        if self.config.debug:
            debug_name = f"{debug_prefix}_{variant_name}.png"
            debug_path = self._save_debug_image(image, debug_name)
            logger.info(f"Saved OCR debug image: {debug_path}")

        return best_candidate, best_confidence, best_text, best_psm

    def detect_door_number(self, latitude, longitude, heading=0, pitch=0, image=None):
        """Detect the door number for a specific coordinate."""
        logger.info(f"Processing coordinate: {latitude}, {longitude}")

        try:
            best_number = None
            best_confidence = 0
            best_image = None
            best_heading = heading
            best_pitch = pitch

            if image is None:
                heading_offsets = [0, -10, 10, -20, 20]
                pitch_values = [5, 10, 15] if pitch == 0 else [pitch]

                for pitch_try in pitch_values:
                    for offset in heading_offsets:
                        logger.info(f"Trying image with heading offset {offset} and pitch {pitch_try}")
                        candidate_image = self.street_view.get_image(
                            latitude,
                            longitude,
                            heading=heading,
                            heading_offset=offset,
                            pitch=pitch_try,
                        )

                        if candidate_image is None:
                            continue

                        if self.config.debug:
                            debug_name = f"candidate_offset{offset}_pitch{pitch_try}.png"
                            debug_path = self._save_debug_image(candidate_image, debug_name)
                            logger.info(f"Saved candidate debug image: {debug_path}")

                        number, confidence = self._extract_door_number(candidate_image)
                        logger.info(
                            f"Offset {offset}, pitch {pitch_try} → {number} (confidence: {confidence}%)"
                        )

                        if confidence > best_confidence:
                            best_confidence = confidence
                            best_number = number
                            best_image = candidate_image
                            best_heading = (heading + offset) % 360
                            best_pitch = pitch_try

                        if best_confidence >= self.config.confidence_threshold:
                            logger.info(
                                f"Stopping early at offset {offset}, pitch {pitch_try} with confidence {best_confidence}%"
                            )
                            break
                    if best_confidence >= self.config.confidence_threshold:
                        break

                if best_image is None:
                    logger.warning(f"Unable to retrieve image for {latitude}, {longitude}")
                    return {
                        "success": False,
                        "latitude": latitude,
                        "longitude": longitude,
                        "door_number": None,
                        "confidence": 0,
                        "error": "Image unavailable",
                    }

                door_number = best_number
                confidence = best_confidence
                heading = best_heading
                pitch = best_pitch
            else:
                door_number, confidence = self._extract_door_number(image)

            success = bool(door_number and confidence >= self.config.confidence_threshold)
            result = {
                "success": success,
                "latitude": latitude,
                "longitude": longitude,
                "door_number": door_number if success else None,
                "confidence": confidence,
                "heading": heading,
                "pitch": pitch,
                "timestamp": datetime.now().isoformat(),
            }

            if not success:
                result["error"] = (
                    f"Low confidence ({confidence}%). "
                    f"Requires at least {self.config.confidence_threshold}% to pass."
                )
                logger.warning(
                    f"Low confidence result: {door_number} ({confidence}%). marked as failed."
                )
            else:
                logger.info(f"Result: {door_number} (confidence: {confidence}%)")

            self.db.save_result(result)
            return result

        except Exception as exc:
            logger.error(f"Error processing coordinate: {exc}", exc_info=True)
            return {
                "success": False,
                "latitude": latitude,
                "longitude": longitude,
                "error": str(exc),
            }

    def _extract_door_number(self, image):
        """Extract the door number using OCR."""
        best_number = None
        best_confidence = 0
        best_variant = None
        best_psm = None

        debug_prefix = datetime.now().strftime("%Y%m%d_%H%M%S")
        variants = self._build_preprocessing_variants(image)

        for variant_name, variant_image in variants:
            number, confidence, raw_text, psm = self._run_ocr_on_variant(
                variant_image, variant_name, debug_prefix
            )

            if number and confidence > best_confidence:
                best_number = number
                best_confidence = confidence
                best_variant = variant_name
                best_psm = psm

            if best_confidence >= self.config.confidence_threshold:
                logger.info(
                    f"Accepting {best_number} from {best_variant} at {best_confidence}% confidence"
                )
                break

        logger.info(
            f"OCR best result: {best_number} (confidence: {best_confidence}%, variant: {best_variant}, psm: {best_psm})"
        )
        return best_number, best_confidence

    def _parse_door_number(self, text):
        """Extract the first numeric token from OCR text."""
        if not text:
            return None

        numbers = re.findall(r"\b\d+[A-Z]?\b", text)
        return numbers[0] if numbers else None

    def process_coordinates_batch(self, coordinates_list):
        """Process multiple coordinates."""
        results = []
        total = len(coordinates_list)

        for index, coordinate in enumerate(coordinates_list, 1):
            logger.info(f"Processing {index}/{total}")
            result = self.detect_door_number(
                coordinate["latitude"],
                coordinate["longitude"],
                coordinate.get("heading", 0),
                coordinate.get("pitch", 0),
            )
            results.append(result)

        return results

    def close(self):
        """Release resources."""
        self.db.close()
        logger.info("Door Number Detector finished")
