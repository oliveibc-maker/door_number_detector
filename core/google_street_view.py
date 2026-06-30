"""Google Street View API integration."""

import logging
import math
from io import BytesIO

import requests
from PIL import Image

logger = logging.getLogger(__name__)


class StreetViewFetcher:
    """Fetches images from the Google Street View API."""

    BASE_URL = "https://maps.googleapis.com/maps/api/streetview"
    METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"

    def __init__(self, api_key, size="640x640"):
        self.api_key = api_key
        self.size = size

    def get_pano_location(self, latitude, longitude):
        """Return the actual Street View camera location for the given coordinates."""
        params = {
            "location": f"{latitude},{longitude}",
            "key": self.api_key,
        }

        response = requests.get(self.METADATA_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get("status") != "OK":
            raise ValueError(f"Street View metadata unavailable: {data.get('status')}" )

        location = data.get("location", {})
        return location.get("lat"), location.get("lng")

    @staticmethod
    def calculate_heading(cam_lat, cam_lng, target_lat, target_lng):
        """Compute the compass heading from the camera to the target point."""
        lat1 = math.radians(cam_lat)
        lat2 = math.radians(target_lat)
        d_lng = math.radians(target_lng - cam_lng)

        x = math.sin(d_lng) * math.cos(lat2)
        y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(d_lng)

        heading = math.degrees(math.atan2(x, y))
        return (heading + 360) % 360

    def get_image(self, latitude, longitude, heading=None, heading_offset=0, pitch=0, fov=90):
        """Fetch a Street View image for the given coordinates."""
        try:
            try:
                cam_lat, cam_lng = self.get_pano_location(latitude, longitude)
                if heading is None:
                    heading = self.calculate_heading(cam_lat, cam_lng, latitude, longitude)
                heading = (heading + heading_offset) % 360
                logger.info(
                    f"Using Street View camera location {cam_lat},{cam_lng} heading {heading:.1f}"
                )
                location = f"{cam_lat},{cam_lng}"
            except Exception as metadata_exc:
                logger.warning(f"Could not resolve pano location: {metadata_exc}")
                location = f"{latitude},{longitude}"
                if heading is None:
                    heading = 0
                heading = (heading + heading_offset) % 360
                logger.info(f"Falling back to requested location {location} with heading {heading:.1f}")

            params = {
                "location": location,
                "size": self.size,
                "heading": heading,
                "pitch": pitch,
                "fov": fov,
                "source": "outdoor",
                "key": self.api_key,
            }

            logger.info(f"Fetching image for: {location} (target {latitude},{longitude})")
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
