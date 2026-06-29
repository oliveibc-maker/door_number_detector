"""Main door number detection workflow."""

import logging
import re
from datetime import datetime

import pytesseract

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
        self.db = DatabaseManager(self.config)
        self.street_view = StreetViewFetcher(self.config.google_api_key)
        logger.info("Door Number Detector initialized")

    def detect_door_number(self, latitude, longitude, heading=0, pitch=0):
        """Detect the door number for a specific coordinate."""
        logger.info(f"Processing coordinate: {latitude}, {longitude}")

        try:
            image = self.street_view.get_image(latitude, longitude, heading, pitch)

            if image is None:
                logger.warning(f"Unable to retrieve image for {latitude}, {longitude}")
                return {
                    "success": False,
                    "latitude": latitude,
                    "longitude": longitude,
                    "door_number": None,
                    "confidence": 0,
                    "error": "Image unavailable",
                }

            door_number, confidence = self._extract_door_number(image)
            result = {
                "success": True,
                "latitude": latitude,
                "longitude": longitude,
                "door_number": door_number,
                "confidence": confidence,
                "heading": heading,
                "pitch": pitch,
                "timestamp": datetime.now().isoformat(),
            }

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
        try:
            gray_image = image.convert("L")
            text = pytesseract.image_to_string(gray_image)
            door_number = self._parse_door_number(text)
            confidence = 85 if door_number else 0
            return door_number, confidence
        except Exception as exc:
            logger.error(f"OCR error: {exc}")
            return None, 0

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
