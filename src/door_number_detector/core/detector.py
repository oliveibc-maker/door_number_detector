"""Main door number detection workflow."""

import logging
import re
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from door_number_detector.core.config import Config
from door_number_detector.core.database import DatabaseManager
from door_number_detector.core.google_street_view import NoImageryAvailable, StreetViewFetcher
from door_number_detector.core.models import DetectionResult
from door_number_detector.core.ocr import DoorNumberOCR
from door_number_detector.core.parsing import BLOCKLIST
from door_number_detector.core.sweep import StreetViewSweep

logger = logging.getLogger(__name__)


class DoorNumberDetector:
    """Detects door numbers using Google Street View images and PaddleOCR."""

    def __init__(
        self,
        env_path=".env",
        *,
        config=None,
        database_manager=None,
        street_view_fetcher=None,
        ocr_engine=None,
        debug_root: Path | None = None,
    ):
        self.config = config if config is not None else Config(env_path)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.debug_root = (
            Path(debug_root) if debug_root is not None else self.config.data_dir / "ocr_debug"
        )
        self.debug_root.mkdir(parents=True, exist_ok=True)
        self.debug_dir = self.debug_root / ts
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        self.current_debug_house_dir: Path | None = None
        self.db = database_manager if database_manager is not None else DatabaseManager(self.config)
        self.street_view = (
            street_view_fetcher
            if street_view_fetcher is not None
            else StreetViewFetcher(
                self.config.google_api_key,
                size=self.config.street_view_size,
                https_proxy=self.config.https_proxy,
            )
        )

        self.ocr = DoorNumberOCR(reader=ocr_engine, logger=logger)
        self.sweep = StreetViewSweep(
            self.street_view,
            self.ocr,
            self._has_confident_result,
            debug=self.config.debug,
            logger=logger,
        )
        logger.info("Door Number Detector initialized (OCR engine: PaddleOCR)")

    def _save_debug_image(self, image, name, directory: Path | None = None):
        target_dir = directory if directory is not None else self.debug_dir
        path = target_dir / name
        if isinstance(image, np.ndarray):
            cv2.imwrite(str(path), image)
        else:
            image.save(path)
        return path

    @staticmethod
    def _is_year_like(num: str) -> bool:
        return bool(
            re.fullmatch(r"202", num)
            or re.fullmatch(r"20\d{2}", num)
            or re.fullmatch(r"19\d{2}", num)
        )

    @staticmethod
    def _is_suspicious_short(num: str) -> bool:
        """True if the candidate is a short fragment likely from a year watermark.

        Covers:
          - Two-digit year fragments: 20 (prefix), 21–26 (recent year suffixes)
          - e.g. '20', '24', '25', '23', '22', '21', '26'
        """
        if re.fullmatch(r"2[0-6]", num):
            return True
        return False

    def _is_bad_candidate(self, num: str) -> bool:
        return self._is_year_like(num) or self._is_suspicious_short(num)

    def _no_early_stop_number(self, num: str) -> bool:
        if num in ("20", "2", "24", "25", "202"):
            return True
        if re.fullmatch(r"20\d{2}", num):
            return True
        if re.fullmatch(r"30\d{2}", num):
            return True
        return False

    def _has_confident_result(self, candidates: list[dict]) -> bool:
        if not candidates:
            return False
        best = max(
            candidates,
            key=lambda c: (c["confidence"], len(c["number"]) if c["number"] else 0),
        )
        if not (best["number"] and best["confidence"] >= self.config.confidence_threshold):
            return False

        best_number = best["number"]

        if self._no_early_stop_number(best_number):
            logger.info(
                f"Early stop suppressed for '{best_number}' "
                f"(matches no-early-stop pattern) — continuing sweep."
            )
            return False

        return True

    def detect_door_number(
        self,
        latitude,
        longitude,
        heading=None,
        pitch=None,
        image=None,
        cancel_event: threading.Event | None = None,
    ) -> DetectionResult:
        """Detect the door number for a specific coordinate."""
        logger.info(f"Processing coordinate: {latitude}, {longitude}")

        try:
            if self.config.debug:
                house_name = f"{latitude:.6f}_{longitude:.6f}"
                self.current_debug_house_dir = self.debug_dir / house_name
                self.current_debug_house_dir.mkdir(parents=True, exist_ok=True)

            all_image_candidates: list[dict] = []

            if image is not None:
                logger.info("Using supplied image instead of fetching from Street View")
                number, confidence = self.ocr.extract_door_number(image)
                logger.info(f"Supplied image -> '{number}' (confidence: {confidence}%)")
                all_image_candidates.append(
                    {
                        "number": number,
                        "confidence": confidence,
                        "heading": heading,
                        "pitch": pitch,
                    }
                )
            else:
                if cancel_event is not None and cancel_event.is_set():
                    logger.info(f"Cancelled before fetching ({latitude}, {longitude}) — skipping.")
                    return DetectionResult(
                        success=False,
                        latitude=latitude,
                        longitude=longitude,
                        error="Cancelled",
                    )

                _base_heading = heading
                road_heading: float | None = None

                if _base_heading is None:
                    try:
                        cam_lat, cam_lng, road_heading = self.street_view.get_pano_location(
                            latitude, longitude
                        )
                        _base_heading = StreetViewFetcher.calculate_heading(
                            cam_lat, cam_lng, latitude, longitude
                        )
                        logger.info(
                            f"Heading pano->target: {_base_heading:.1f}° "
                            f"(road_heading from API: {road_heading})"
                        )
                    except NoImageryAvailable:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "Could not compute heading (%s). Using 0°.", type(exc).__name__
                        )
                        _base_heading = 0

                if road_heading is None:
                    logger.warning("road_heading unavailable — using camera-to-target heading.")

                # Pass 1 — direct heading, normal offsets (±30°)
                logger.info(f"Pass 1: direct heading {_base_heading:.1f}° (normal sweep ±30°)")
                all_image_candidates = self.sweep.fetch_candidates(
                    latitude,
                    longitude,
                    _base_heading,
                    pass_label="direct",
                    pitch_override=pitch,
                    cancel_event=cancel_event,
                    debug_directory=self.current_debug_house_dir or self.debug_dir,
                    save_debug_image=self._save_debug_image,
                )

            if not all_image_candidates:
                logger.warning(f"Unable to retrieve any image for {latitude}, {longitude}")
                return DetectionResult(
                    success=False,
                    latitude=latitude,
                    longitude=longitude,
                    error="Image unavailable",
                )

            confident_votes = Counter(
                c["number"]
                for c in all_image_candidates
                if c["number"] and c["confidence"] >= self.config.confidence_threshold
            )

            best_candidate = max(
                all_image_candidates,
                key=lambda c: (
                    c["confidence"],
                    confident_votes.get(c["number"], 0),
                    len(c["number"]) if c["number"] else 0,
                ),
            )

            door_number = best_candidate["number"]
            confidence = best_candidate["confidence"]
            heading = best_candidate["heading"]
            pitch = best_candidate["pitch"]

            logger.info(
                f"Best across all passes: '{door_number}' ({confidence}%) "
                f"at heading_offset={heading} pitch={pitch}"
            )

            if door_number and self._is_bad_candidate(door_number):
                sorted_candidates = sorted(
                    all_image_candidates,
                    key=lambda c: (c["confidence"], len(c["number"]) if c["number"] else 0),
                    reverse=True,
                )

                clean_alternative = next(
                    (
                        c
                        for c in sorted_candidates
                        if c["number"]
                        and c["number"] != door_number
                        and not self._is_bad_candidate(c["number"])
                        and c["number"] not in BLOCKLIST
                        and c["confidence"] >= self.config.confidence_threshold
                    ),
                    None,
                )

                if clean_alternative:
                    logger.info(
                        f"Substituting '{door_number}' ({confidence}%) "
                        f"with clean candidate '{clean_alternative['number']}' "
                        f"({clean_alternative['confidence']}%)"
                    )
                    door_number = clean_alternative["number"]
                    confidence = clean_alternative["confidence"]
                    heading = clean_alternative["heading"]
                    pitch = clean_alternative["pitch"]

                elif self._is_year_like(door_number):
                    tier2_alternative = next(
                        (
                            c
                            for c in sorted_candidates
                            if c["number"]
                            and c["number"] != door_number
                            and self._is_suspicious_short(c["number"])
                            and c["number"] not in BLOCKLIST
                            and c["confidence"] >= self.config.confidence_threshold
                        ),
                        None,
                    )

                    if tier2_alternative:
                        logger.info(
                            f"No clean alternative found — substituting Tier 1 "
                            f"'{door_number}' ({confidence}%) with Tier 2 fallback "
                            f"'{tier2_alternative['number']}' "
                            f"({tier2_alternative['confidence']}%)"
                        )
                        door_number = tier2_alternative["number"]
                        confidence = tier2_alternative["confidence"]
                        heading = tier2_alternative["heading"]
                        pitch = tier2_alternative["pitch"]
                    else:
                        logger.info(
                            f"No clean or Tier 2 alternative found — keeping Tier 1 '{door_number}'"
                        )
                else:
                    logger.info(
                        f"Best is Tier 2 suspicious short '{door_number}' "
                        f"with no clean alternative — keeping it"
                    )

            success = bool(door_number and confidence >= self.config.confidence_threshold)
            error = None
            if not success:
                error = (
                    f"Low confidence ({confidence}%). "
                    f"Requires at least {self.config.confidence_threshold}% to pass."
                )
            result = DetectionResult(
                success=success,
                latitude=latitude,
                longitude=longitude,
                door_number=door_number if success else None,
                confidence=confidence,
                heading=heading,
                pitch=pitch,
                error=error,
            )

            if not success:
                logger.warning(
                    f"Low confidence result: '{door_number}' ({confidence}%). Marked as failed."
                )
            else:
                logger.info(f"Result: {door_number} (confidence: {confidence}%)")

            self.db.save_result(result.to_dict())
            return result

        except NoImageryAvailable:
            logger.warning(
                f"No Street View imagery available for ({latitude}, {longitude}) — "
                f"skipping OCR and recording observation."
            )
            result = DetectionResult(
                success=False,
                latitude=latitude,
                longitude=longitude,
                observation="No Street View imagery available",
            )
            self.db.save_result(result.to_dict())
            return result

        except Exception as exc:
            logger.error(f"Error processing coordinate: {exc}", exc_info=True)
            return DetectionResult(
                success=False,
                latitude=latitude,
                longitude=longitude,
                error=str(exc),
            )

    def process_coordinates_batch(self, coordinates_list: list) -> list:
        results = []
        total = len(coordinates_list)
        for index, coordinate in enumerate(coordinates_list, 1):
            logger.info(f"Processing {index}/{total}")
            result = self.detect_door_number(
                coordinate["latitude"],
                coordinate["longitude"],
                coordinate.get("heading"),
                coordinate.get("pitch"),
            )
            results.append(result)
        return results

    def close(self):
        self.db.close()
        logger.info("Door Number Detector finished")
