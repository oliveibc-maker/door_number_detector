"""Google Street View API integration."""

import logging
import math
from io import BytesIO

import numpy as np
import requests
from PIL import Image

logger = logging.getLogger(__name__)


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

    BASE_URL     = "https://maps.googleapis.com/maps/api/streetview"
    METADATA_URL = "https://maps.googleapis.com/maps/api/streetview/metadata"

    # Pixel std-dev threshold below which an image is the grey placeholder.
    # Real Street View photos always have std >> 30; the placeholder is ~0–3.
    _NO_IMAGERY_STD_THRESHOLD = 12.0

    def __init__(self, api_key, size="640x640", https_proxy=""):
        self.api_key = api_key
        self.size    = size
        self._pano_cache: dict[tuple[float, float], tuple[float, float, float | None]] = {}
        self._session = requests.Session()
        if https_proxy:
            self._session.proxies.update({
                "http":  https_proxy,
                "https": https_proxy,
            })
            logger.info(f"StreetViewFetcher: using proxy {https_proxy}")

        self._image_call_count:    int = 0
        self._metadata_call_count: int = 0

    # ── Call-counter helpers ───────────────────────────────────────────────────

    def reset_call_counts(self) -> None:
        self._image_call_count    = 0
        self._metadata_call_count = 0

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

    def get_pano_location(self, latitude, longitude):
        """Return (cam_lat, cam_lng, road_heading) for the nearest Street View pano.

        Raises NoImageryAvailable ONLY when the API confirms no panorama exists
        within the search radius (ZERO_RESULTS status).
        Results are cached so the same coordinate never triggers more than one
        metadata API call across the entire FOV sweep.
        """
        cache_key = (latitude, longitude)
        if cache_key in self._pano_cache:
            return self._pano_cache[cache_key]

        params = {
            "location": f"{latitude},{longitude}",
            "key":      self.api_key,
        }

        self._metadata_call_count += 1
        response = self._session.get(self.METADATA_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        status = data.get("status")
        if status == "ZERO_RESULTS":
            # Genuinely no panorama within ~50 m — do not cache, raise immediately.
            raise NoImageryAvailable(
                f"No Street View panorama within search radius of ({latitude}, {longitude})"
            )
        if status != "OK":
            raise ValueError(f"Street View metadata unavailable: {status}")

        location = data.get("location", {})
        result = (
            location.get("lat"),
            location.get("lng"),
            data.get("heading"),
        )
        self._pano_cache[cache_key] = result
        return result

    @staticmethod
    def calculate_heading(cam_lat, cam_lng, target_lat, target_lng):
        """Compute the compass heading from the camera to the target point."""
        lat1  = math.radians(cam_lat)
        lat2  = math.radians(target_lat)
        d_lng = math.radians(target_lng - cam_lng)

        x = math.sin(d_lng) * math.cos(lat2)
        y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(d_lng)

        heading = math.degrees(math.atan2(x, y))
        return (heading + 360) % 360

    # ── Image fetching ─────────────────────────────────────────────────────────

    def get_image(self, latitude, longitude, heading=None, heading_offset=0, pitch=0, fov=90):
        """Fetch a Street View image for the given coordinates.

        Returns None when:
          - the image is the grey placeholder for this specific angle (the sweep
            will simply continue to the next heading/pitch/FOV combination), OR
          - a transient network / HTTP error occurs.

        Raises NoImageryAvailable ONLY when get_pano_location() confirms there is
        no panorama at all for this coordinate (ZERO_RESULTS from metadata API).
        This only happens when heading is pre-specified and the cache is cold.
        """
        try:
            # ── Resolve pano location (cached after first call) ────────────────
            try:
                cam_lat, cam_lng, _ = self.get_pano_location(latitude, longitude)
                # NoImageryAvailable propagates — do not catch it here.
                if heading is None:
                    heading = self.calculate_heading(cam_lat, cam_lng, latitude, longitude)
                heading  = (heading + heading_offset) % 360
                logger.info(
                    f"Using Street View camera location {cam_lat},{cam_lng} "
                    f"heading {heading:.1f}"
                )
                location = f"{cam_lat},{cam_lng}"
            except NoImageryAvailable:
                raise
            except Exception as metadata_exc:
                logger.warning(f"Could not resolve pano location: {metadata_exc}")
                location = f"{latitude},{longitude}"
                if heading is None:
                    heading = 0
                heading = (heading + heading_offset) % 360
                logger.info(
                    f"Falling back to requested location {location} "
                    f"with heading {heading:.1f}"
                )

            # ── Fetch image ───────────────────────────────────────────────────
            params = {
                "location": location,
                "size":     self.size,
                "heading":  heading,
                "pitch":    pitch,
                "fov":      fov,
                "source":   "outdoor",
                "key":      self.api_key,
            }

            logger.info(f"Fetching image for: {location} (target {latitude},{longitude})")
            self._image_call_count += 1
            response = self._session.get(self.BASE_URL, params=params, timeout=10)
            response.raise_for_status()

            if not response.headers.get("content-type", "").startswith("image"):
                logger.warning(
                    f"Response is not an image: {response.headers.get('content-type')}"
                )
                return None

            image = Image.open(BytesIO(response.content))

            # ── Pixel-level grey placeholder check ────────────────────────────
            # A grey image for a specific angle means the camera has no data at
            # that heading/pitch/FOV — NOT that the road has no coverage.
            # Return None so the sweep moves on to the next angle.
            if self._is_no_imagery(image):
                logger.debug(
                    f"Grey placeholder at ({latitude}, {longitude}) "
                    f"heading={heading:.1f} pitch={pitch} fov={fov} — skipping angle."
                )
                return None

            logger.info("Image retrieved successfully")
            return image

        except NoImageryAvailable:
            raise
        except requests.exceptions.RequestException as exc:
            logger.error(f"Error fetching image: {exc}")
            return None
        except Exception as exc:
            logger.error(f"Unexpected error: {exc}")
            return None

    def is_imagery_available(self, latitude, longitude) -> bool:
        """Check whether Street View imagery is available for a coordinate."""
        try:
            self.get_pano_location(latitude, longitude)
            return True
        except (NoImageryAvailable, ValueError):
            return False
        except Exception:
            return False
