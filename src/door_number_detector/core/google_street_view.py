"""Google Street View API integration."""

import logging
import math
import re
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO

import numpy as np
import requests
from PIL import Image

logger = logging.getLogger(__name__)

_STATUS_TOKEN = re.compile(r"[A-Z_]{1,40}")


@dataclass(frozen=True)
class CameraPosition:
    latitude: float
    longitude: float
    road_heading: float | None
    fallback: bool


class NoImageryAvailable(Exception):
    """Raised ONLY when the metadata API returns ZERO_RESULTS — meaning there is
    genuinely no Street View panorama within the search radius (~50 m) of the
    requested coordinate.

    A grey placeholder returned by the image endpoint for a specific
    heading/pitch/FOV combination is NOT this error; that is handled by returning
    None from get_image() so the sweep continues to the next angle.
    """


class StreetViewFetcher:
    """Fetches images from the Google Street View API."""

    BASE_URL = "https://maps.googleapis.com/maps/api/streetview"
    METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"

    # Pixel std-dev threshold below which an image is the grey placeholder.
    # Real Street View photos always have std >> 30; the placeholder is ~0–3.
    _NO_IMAGERY_STD_THRESHOLD = 12.0
    _CAMERA_CACHE_SIZE = 512

    def __init__(self, api_key, size="640x640", https_proxy="", *, session=None):
        self.api_key = api_key
        self.size = size
        self._camera_cache: OrderedDict[tuple[float, float], CameraPosition] = OrderedDict()
        self._session = session if session is not None else requests.Session()
        if session is None and https_proxy:
            self._session.proxies.update(
                {
                    "http": https_proxy,
                    "https": https_proxy,
                }
            )
            logger.info("StreetViewFetcher: using configured proxy")

        self._image_call_count: int = 0
        self._metadata_call_count: int = 0

    # ── Call-counter helpers ───────────────────────────────────────────────────

    def get_call_counts(self) -> tuple[int, int]:
        return self._image_call_count, self._metadata_call_count

    # ── No-imagery detection ───────────────────────────────────────────────────

    @staticmethod
    def _is_no_imagery(image: Image.Image) -> bool:
        """Return True when the image is the uniform grey 'no imagery' placeholder.

        This can happen for a specific heading/pitch/FOV even when the coordinate
        has valid Street View coverage — it does NOT mean the road has no coverage.
        """
        arr = np.array(image.convert("RGB"), dtype=np.float32)
        return float(arr.std()) < StreetViewFetcher._NO_IMAGERY_STD_THRESHOLD

    # ── Metadata / pano location ───────────────────────────────────────────────

    def resolve_camera(self, latitude: float, longitude: float) -> CameraPosition:
        """Resolve a camera once, falling back to the requested coordinate on errors."""
        cache_key = (latitude, longitude)
        if cache_key in self._camera_cache:
            self._camera_cache.move_to_end(cache_key)
            return self._camera_cache[cache_key]

        params = {
            "location": f"{latitude},{longitude}",
            "key": self.api_key,
        }

        self._metadata_call_count += 1
        try:
            response = self._session.get(self.METADATA_URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning(
                "Camera lookup failed (%s); using the requested coordinate.", type(exc).__name__
            )
            return CameraPosition(latitude, longitude, None, True)

        if not isinstance(data, dict):
            logger.warning(
                "Camera lookup returned an invalid response; using the requested coordinate."
            )
            return CameraPosition(latitude, longitude, None, True)
        status = data.get("status")
        if status == "ZERO_RESULTS":
            raise NoImageryAvailable(
                f"No Street View panorama within search radius of ({latitude}, {longitude})"
            )
        if status != "OK":
            # Only a Google status token (e.g. REQUEST_DENIED) is logged, never free text.
            status_token = (
                status if isinstance(status, str) and _STATUS_TOKEN.fullmatch(status) else "invalid"
            )
            logger.warning(
                "Camera lookup returned status %s; using the requested coordinate.", status_token
            )
            return CameraPosition(latitude, longitude, None, True)

        location = data.get("location", {})
        try:
            camera_latitude = float(location["lat"])
            camera_longitude = float(location["lng"])
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "Camera lookup returned an invalid location; using the requested coordinate."
            )
            return CameraPosition(latitude, longitude, None, True)
        if (
            not math.isfinite(camera_latitude)
            or not math.isfinite(camera_longitude)
            or not -90 <= camera_latitude <= 90
            or not -180 <= camera_longitude <= 180
        ):
            logger.warning(
                "Camera lookup returned an invalid location; using the requested coordinate."
            )
            return CameraPosition(latitude, longitude, None, True)
        road_heading = data.get("heading")
        try:
            road_heading = float(road_heading) if road_heading is not None else None
        except (TypeError, ValueError):
            road_heading = None
        if road_heading is not None and not math.isfinite(road_heading):
            road_heading = None

        camera = CameraPosition(camera_latitude, camera_longitude, road_heading, False)
        self._camera_cache[cache_key] = camera
        self._camera_cache.move_to_end(cache_key)
        if len(self._camera_cache) > self._CAMERA_CACHE_SIZE:
            self._camera_cache.popitem(last=False)
        return camera

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

    @classmethod
    def heading_to_target(
        cls, camera: CameraPosition, target_latitude: float, target_longitude: float
    ) -> float:
        return cls.calculate_heading(
            camera.latitude, camera.longitude, target_latitude, target_longitude
        )

    # ── Image fetching ─────────────────────────────────────────────────────────

    def get_image(
        self,
        camera: CameraPosition,
        heading: float,
        pitch: int = 0,
        fov: int = 90,
    ) -> Image.Image | None:
        """Fetch one image from a resolved camera at an absolute heading."""
        params = {
            "location": f"{camera.latitude},{camera.longitude}",
            "size": self.size,
            "heading": heading % 360,
            "pitch": pitch,
            "fov": fov,
            "source": "outdoor",
            "key": self.api_key,
        }
        self._image_call_count += 1
        try:
            response = self._session.get(self.BASE_URL, params=params, timeout=10)
            response.raise_for_status()
            if not response.headers.get("content-type", "").startswith("image"):
                logger.warning("Street View image endpoint returned a non-image response.")
                return None

            image = Image.open(BytesIO(response.content))
            if self._is_no_imagery(image):
                logger.debug("Street View returned a grey placeholder; skipping this angle.")
                return None
            return image
        except Exception as exc:
            logger.warning("Street View image fetch failed (%s).", type(exc).__name__)
            return None
