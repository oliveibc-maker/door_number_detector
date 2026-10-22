"""Main door number detection workflow."""

import os
os.environ["FLAGS_use_mkldnn"] = "0"
os.environ["PADDLE_DISABLE_MKLDNN"] = "1"
os.environ["FLAGS_enable_pir_in_executor"] = "0"

import io
import logging
import re
import sys
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from core.config import Config
from core.database import DatabaseManager
from core.google_street_view import StreetViewFetcher, NoImageryAvailable
from core.models import DetectionResult
from core.ocr import DoorNumberOCR


# ── UTF-8 safe logging (fixes cp1252 crash on Windows) ────────────────────────
def _build_logger() -> logging.Logger:
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    log_path = Config().data_dir / "logs" / "door_detector.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)

    utf8_stream = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    stream_handler = logging.StreamHandler(utf8_stream)
    stream_handler.setFormatter(fmt)

    log = logging.getLogger(__name__)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    log.addHandler(file_handler)
    log.addHandler(stream_handler)
    log.propagate = False
    return log


logger = _build_logger()

# ── Door number patterns ───────────────────────────────────────────────────────

from core.parsing import BLOCKLIST, parse_ocr_text


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
            Path(debug_root)
            if debug_root is not None
            else self.config.data_dir / "ocr_debug"
        )
        self.debug_root.mkdir(parents=True, exist_ok=True)
        self.debug_dir = self.debug_root / ts
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        self.current_debug_house_dir: Path | None = None
        self.db = (
            database_manager
            if database_manager is not None
            else DatabaseManager(self.config)
        )
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
            re.fullmatch(r"202", num)      or
            re.fullmatch(r"20\d{2}", num)  or
            re.fullmatch(r"19\d{2}", num)
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

    _FOV_SWEEP: list[tuple[int, list[int], list[int]]] = [
        (10, [0, -5, 5, -10, 10, -15, 15, -20, 20, -25, 25, -30, 30], [0, -5, 5, -10, 10, -15, 15]),
    ]

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

    # ── Per-position iteration ─────────────────────────────────────────────────
    def _fetch_candidates(
        self,
        latitude: float,
        longitude: float,
        heading,
        pass_label: str = "center",
        pitch_override: int | None = None,
        sweep: list | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[dict]:
        fov_sweep = sweep if sweep is not None else self._FOV_SWEEP
        all_image_candidates: list[dict] = []
        done        = False
        _no_imagery = False

        for fov_try, fov_heading_offsets, fov_pitch_values in fov_sweep:
            if done:
                break
            if cancel_event is not None and cancel_event.is_set():
                logger.info(f"[{pass_label}] Cancelled — aborting FOV sweep.")
                break

            pitch_values = [pitch_override] if pitch_override is not None else fov_pitch_values

            for pitch_try in pitch_values:
                if done:
                    break
                if cancel_event is not None and cancel_event.is_set():
                    logger.info(f"[{pass_label}] Cancelled — aborting pitch loop.")
                    done = True
                    break

                for offset in fov_heading_offsets:
                    if cancel_event is not None and cancel_event.is_set():
                        logger.info(
                            f"[{pass_label}] Cancelled at offset={offset} — "
                            f"aborting heading sweep."
                        )
                        done = True
                        break

                    logger.info(
                        f"[{pass_label}] Trying FOV={fov_try} offset={offset} pitch={pitch_try}"
                    )

                    try:
                        candidate_image = self.street_view.get_image(
                            latitude,
                            longitude,
                            heading=heading,
                            heading_offset=offset,
                            pitch=pitch_try,
                            fov=fov_try,
                        )
                    except NoImageryAvailable:
                        logger.warning(
                            f"[{pass_label}] No Street View imagery for "
                            f"({latitude}, {longitude}) — aborting sweep."
                        )
                        _no_imagery = True
                        done = True
                        break

                    if candidate_image is None:
                        logger.warning(
                            f"[{pass_label}] No image returned for "
                            f"offset={offset} pitch={pitch_try}"
                        )
                        continue

                    if self.config.debug:
                        pass_dir = (self.current_debug_house_dir or self.debug_dir) / pass_label
                        pass_dir.mkdir(parents=True, exist_ok=True)
                        debug_name = f"fov{fov_try}_offset{offset}_pitch{pitch_try}.png"
                        self._save_debug_image(candidate_image, debug_name, directory=pass_dir)

                    number, confidence = self.ocr.extract_door_number(candidate_image)
                    logger.info(
                        f"[{pass_label}] FOV={fov_try} offset={offset} pitch={pitch_try} -> "
                        f"'{number}' (confidence: {confidence}%)"
                    )

                    all_image_candidates.append({
                        "number":     number,
                        "confidence": confidence,
                        "heading":    offset,
                        "pitch":      pitch_try,
                        "fov":        fov_try,
                    })

                    if self._has_confident_result(all_image_candidates):
                        logger.info(
                            f"[{pass_label}] Confident result at FOV={fov_try} "
                            f"offset={offset} pitch={pitch_try} — stopping sweep."
                        )
                        done = True
                        break

        if _no_imagery:
            raise NoImageryAvailable(
                f"No Street View imagery available for ({latitude}, {longitude})"
            )

        return all_image_candidates

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
                all_image_candidates.append({
                    "number":     number,
                    "confidence": confidence,
                    "heading":    heading,
                    "pitch":      pitch,
                })
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
                        logger.warning(f"Could not compute heading: {exc}. Using 0°.")
                        _base_heading = 0

                if road_heading is None:
                    logger.warning(
                        "road_heading unavailable — using camera-to-target heading."
                    )

                # Pass 1 — direct heading, normal offsets (±30°)
                logger.info(f"Pass 1: direct heading {_base_heading:.1f}° (normal sweep ±30°)")
                all_image_candidates = self._fetch_candidates(
                    latitude, longitude, _base_heading,
                    pass_label="direct",
                    pitch_override=pitch,
                    sweep=self._FOV_SWEEP,
                    cancel_event=cancel_event,
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
                c["number"] for c in all_image_candidates
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
            confidence  = best_candidate["confidence"]
            heading     = best_candidate["heading"]
            pitch       = best_candidate["pitch"]

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
                        c for c in sorted_candidates
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
                    confidence  = clean_alternative["confidence"]
                    heading     = clean_alternative["heading"]
                    pitch       = clean_alternative["pitch"]

                elif self._is_year_like(door_number):
                    tier2_alternative = next(
                        (
                            c for c in sorted_candidates
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
                        confidence  = tier2_alternative["confidence"]
                        heading     = tier2_alternative["heading"]
                        pitch       = tier2_alternative["pitch"]
                    else:
                        logger.info(
                            f"No clean or Tier 2 alternative found — keeping "
                            f"Tier 1 '{door_number}'"
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
        total   = len(coordinates_list)
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
