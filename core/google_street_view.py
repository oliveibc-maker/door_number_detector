"""Google Street View API integration."""

import logging
from io import BytesIO

import requests
from PIL import Image

logger = logging.getLogger(__name__)


class StreetViewFetcher:
    """Fetches images from the Google Street View API."""

    BASE_URL = "https://maps.googleapis.com/maps/api/streetview"

    def __init__(self, api_key, size="640x480"):
        self.api_key = api_key
        self.size = size

    def get_image(self, latitude, longitude, heading=0, pitch=0, fov=90):
        """Fetch a Street View image for the given coordinates."""
        try:
            params = {
                "location": f"{latitude},{longitude}",
                "size": self.size,
                "heading": heading,
                "pitch": pitch,
                "fov": fov,
                "key": self.api_key,
            }

            logger.info(f"Fetching image for: {latitude}, {longitude}")
            response = requests.get(self.BASE_URL, params=params, timeout=10)
            response.raise_for_status()

            if response.headers["content-type"].startswith("image"):
                image = Image.open(BytesIO(response.content))
                logger.info("Image retrieved successfully")
                return image

            logger.warning(f"Response is not an image: {response.headers['content-type']}")
            return None

        except requests.exceptions.RequestException as exc:
            logger.error(f"Error fetching image: {exc}")
            return None
        except Exception as exc:
            logger.error(f"Unexpected error: {exc}")
            return None

    def is_imagery_available(self, latitude, longitude):
        """Check whether imagery is available for a coordinate."""
        params = {"location": f"{latitude},{longitude}", "key": self.api_key}

        try:
            response = requests.head(self.BASE_URL, params=params, timeout=5)
            return response.status_code == 200
        except Exception:
            return False
