"""Main door number detection workflow."""

import os
os.environ["FLAGS_use_mkldnn"] = "0"   # disables oneDNN — fixes Windows crash
os.environ["PADDLE_DISABLE_MKLDNN"] = "1"
os.environ["FLAGS_enable_pir_in_executor"] = "0"  # disables PIR execution path on Windows

import io
from collections import defaultdict
import logging
import math
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from core.config import Config
from core.database import DatabaseManager
from core.google_street_view import StreetViewFetcher


# ── UTF-8 safe logging (fixes cp1252 crash on Windows) ────────────────────────
def _build_logger() -> logging.Logger:
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    file_handler = logging.FileHandler("door_detector.log", encoding="utf-8")
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


class DoorNumberDetector:
    """Detects door numbers using Google Street View images and OCR."""

    def __init__(self, env_path=".env"):
        self.config = Config(env_path)
        self._configure_tesseract()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.debug_root = Path("ocr_debug")
        self.debug_root.mkdir(parents=True, exist_ok=True)
        self.debug_dir = self.debug_root / ts
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        self.current_debug_house_dir: Path | None = None
        self.db = DatabaseManager(self.config)
        self.street_view = StreetViewFetcher(
            self.config.google_api_key, size=self.config.street_view_size
        )

        # ── OCR engine selection ───────────────────────────────────────────────
        self._ocr_engine = os.getenv("OCR_ENGINE", "easyocr").lower()

        if self._ocr_engine == "paddleocr":
            try:
                from paddleocr import PaddleOCR
                logger.info("Loading PaddleOCR model (first run downloads models)...")
                self._paddle_reader = PaddleOCR(
                    lang="en",
                    use_angle_cls=True,
                    use_gpu=False,
                    show_log=False,
                    det_db_thresh=0.2,
                    det_db_box_thresh=0.4,
                )
                logger.info("PaddleOCR initialized")
            except ImportError as exc:
                raise ImportError(
                    "PaddleOCR is not installed. Run: pip install paddleocr"
                ) from exc
        else:
            import easyocr
            logger.info("Loading EasyOCR model (first run downloads ~100MB)...")
            self._ocr_reader = easyocr.Reader(["en"], gpu=False)
            logger.info("EasyOCR initialized")

        logger.info(f"Door Number Detector initialized (OCR engine: {self._ocr_engine})")

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

    def _save_debug_image(self, image, name, directory: Path | None = None):
        target_dir = directory if directory is not None else self.debug_dir
        path = target_dir / name
        if isinstance(image, np.ndarray):
            cv2.imwrite(str(path), image)
        else:
            image.save(path)
        return path

    def _upscale(self, image: np.ndarray, scale: int = 3) -> np.ndarray:
        """Bicubic upscale — makes small distant text readable."""
        h, w = image.shape[:2]
        return cv2.resize(image, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)

    def _extract_door_number(self, image: Image.Image) -> tuple[str | None, int]:
        cv_img = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        orig_h, orig_w = cv_img.shape[:2]

        all_candidates: list[tuple[str, float]] = []

        logger.info(f"OCR [{self._ocr_engine}]: full image 2x upscale...")
        full_2x = self._upscale(cv_img, scale=2)

        def _run_easyocr(scan_img: np.ndarray, tag: str) -> list:
            results = self._ocr_reader.readtext(
                scan_img,
                paragraph=False,
                detail=1,
                min_size=5,
                text_threshold=0.2,
                low_text=0.2,
                link_threshold=0.2,
                canvas_size=2560,
                mag_ratio=2.0,
            )
            filtered = []
            for bbox, text, conf in results:
                for num in re.findall(r"\d{1,4}", text.strip()):
                    if len(num) > 1 and num.startswith("0"):
                        logger.info(f"OCR: skipped leading-zero fragment '{num}'")
                        continue
                    all_candidates.append((num, conf))
                    logger.info(f"EasyOCR [{tag}] '{num}' <- '{text}' ({conf * 100:.0f}%)")
                filtered.append((bbox, text, conf))
            return filtered

        def _run_paddleocr(scan_img: np.ndarray, tag: str) -> list:
            ocr_result = self._paddle_reader.ocr(scan_img)
            if not ocr_result:
                return []
            if isinstance(ocr_result[0], list) and len(ocr_result[0]) >= 2 and isinstance(ocr_result[0][1], tuple):
                page = ocr_result
            else:
                page = ocr_result[0]
            if not page:
                return []
            normalized_results = []
            for line in page:
                bbox, (text, conf) = line
                for num in re.findall(r"\d{1,4}", text.strip()):
                    if len(num) > 1 and num.startswith("0"):
                        logger.info(f"OCR: skipped leading-zero fragment '{num}'")
                        continue
                    all_candidates.append((num, conf))
                    logger.info(f"PaddleOCR [{tag}] '{num}' <- '{text}' ({conf * 100:.0f}%)")
                normalized_results.append((bbox, text, conf))
            return normalized_results

        if self._ocr_engine == "paddleocr":
            _run_paddleocr(full_2x, "full_2x")
        else:
            _run_easyocr(full_2x, "full_2x")

        if not all_candidates:
            logger.info("No candidates found")
            return None, 0

        best_conf: dict[str, float] = {}
        conf_sum:  dict[str, float] = {}

        for num, conf in all_candidates:
            if num not in best_conf or conf > best_conf[num]:
                best_conf[num] = conf
            conf_sum[num] = conf_sum.get(num, 0.0) + conf

        def score(num: str) -> tuple:
            vote_count = sum(1 for n, _ in all_candidates if n == num)
            return (best_conf[num], vote_count, conf_sum[num])

        best_number         = max(best_conf.keys(), key=score)
        best_confidence_pct = int(best_conf[best_number] * 100)

        vote_counts = Counter(num for num, _ in all_candidates)
        logger.info(
            f"Best: '{best_number}' ({best_confidence_pct}%) "
            f"conf_sum={conf_sum[best_number]:.2f} "
            f"votes={vote_counts[best_number]}/{len(all_candidates)}"
        )
        return best_number, best_confidence_pct

    # ── Road offset helper ─────────────────────────────────────────────────────
    @staticmethod
    def _road_offset(
        lat: float, lng: float, base_heading: float, distance_m: float
    ) -> tuple[float, float]:
        bearing = math.radians((base_heading + 90) % 360)
        dlat = distance_m * math.cos(bearing) / 111_000
        dlng = distance_m * math.sin(bearing) / (111_000 * math.cos(math.radians(lat)))
        return lat + dlat, lng + dlng

    # ── FOV sweep configuration ────────────────────────────────────────────────
    _FOV_SWEEP: list[tuple[int, list[int], list[int]]] = [
        #  fov   heading_offsets                                      pitch_values (center-first)
        # (  40,   [0, -15, 15, -30, 30],                             [0, -5, 5, -10, 10]          ),  # removed
        # (  30,   [0, -10, 10, -20, 20, -30, 30],                    [0, -5, 5, -10, 10]          ),  # removed
        (  20,   [0, -5, 5, -10, 10, -20, 20, -30, 30],              [0, -5, 5, -10, 10]          ),
        (  10,   [0, -5, 5, -10, 10, -15, 15, -20, 20, -25, 25],    [0, -5, 5, -10, 10]          ),
    ]

    # ── Early-exit thresholds ──────────────────────────────────────────────────
    _EARLY_EXIT_MIN_DIGITS: int = 4
    _EARLY_EXIT_MIN_AGREE:  int = 3

    def _is_suspicious_number(self, num: str) -> bool:
        """All numbers need corroboration — no length-based free pass."""
        return True

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

        if not self._is_suspicious_number(best_number):
            return True

        agreeing = sum(
            1 for c in candidates
            if c["number"] == best_number
            and c["confidence"] >= self.config.confidence_threshold
        )
        return agreeing >= self._EARLY_EXIT_MIN_AGREE

    def _has_any_confident_candidate(self, candidates: list[dict]) -> bool:
        """True if at least one candidate clears the confidence threshold."""
        return any(
            c["number"] and c["confidence"] >= self.config.confidence_threshold
            for c in candidates
        )

    # ── Per-position iteration ─────────────────────────────────────────────────
    def _fetch_candidates(
        self,
        latitude: float,
        longitude: float,
        heading,
        pass_label: str = "center",
        pitch_override: int | None = None,
    ) -> list[dict]:
        """Run all FOV/heading/pitch iterations for a given position.

        Exits early as soon as a confident result is found — both between FOV
        levels AND after every single image (inner early-exit).
        """
        all_image_candidates: list[dict] = []
        done = False

        for fov_try, fov_heading_offsets, fov_pitch_values in self._FOV_SWEEP:
            if done:
                break

            pitch_values = [pitch_override] if pitch_override is not None else fov_pitch_values

            for pitch_try in pitch_values:
                if done:
                    break

                for offset in fov_heading_offsets:
                    logger.info(
                        f"[{pass_label}] Trying FOV={fov_try} offset={offset} pitch={pitch_try}"
                    )
                    candidate_image = self.street_view.get_image(
                        latitude,
                        longitude,
                        heading=heading,
                        heading_offset=offset,
                        pitch=pitch_try,
                        fov=fov_try,
                    )

                    if candidate_image is None:
                        logger.warning(
                            f"[{pass_label}] No image returned for offset={offset} pitch={pitch_try}"
                        )
                        continue

                    if self.config.debug:
                        pass_dir = (self.current_debug_house_dir or self.debug_dir) / pass_label
                        pass_dir.mkdir(parents=True, exist_ok=True)
                        debug_name = f"fov{fov_try}_offset{offset}_pitch{pitch_try}.png"
                        self._save_debug_image(candidate_image, debug_name, directory=pass_dir)

                    number, confidence = self._extract_door_number(candidate_image)
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

                    # Inner early-exit: check after every single image, not just
                    # between FOV levels — avoids unnecessary API calls.
                    if self._has_confident_result(all_image_candidates):
                        logger.info(
                            f"[{pass_label}] Confident result at FOV={fov_try} "
                            f"offset={offset} pitch={pitch_try} — stopping sweep."
                        )
                        done = True
                        break

        return all_image_candidates

    def detect_door_number(self, latitude, longitude, heading=None, pitch=None, image=None):
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
                number, confidence = self._extract_door_number(image)
                logger.info(f"Supplied image -> '{number}' (confidence: {confidence}%)")
                all_image_candidates.append({
                    "number":     number,
                    "confidence": confidence,
                    "heading":    heading,
                    "pitch":      pitch,
                })
            else:
                _base_heading = heading
                road_heading: float | None = None

                if _base_heading is None:
                    try:
                        # get_pano_location returns (cam_lat, cam_lng, road_heading).
                        # road_heading is the GSV car travel direction (road direction).
                        # It may be None if the API does not return it for this pano.
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
                    except Exception as exc:
                        logger.warning(f"Could not compute heading: {exc}. Using 0°.")
                        _base_heading = 0

                # ── Pass 1: direct pano→target heading ────────────────────────
                # Correct for building coordinates (pano far from road → vector
                # points at the building facade).
                # For road-snapped coordinates both the pano and the target sit on
                # the road so this vector points along the road — in that case
                # pass 1 finds nothing and the result is returned as not found.
                logger.info(f"Pass 1: direct heading {_base_heading:.1f}°")
                all_image_candidates = self._fetch_candidates(
                    latitude, longitude, _base_heading,
                    pass_label="direct",
                    pitch_override=pitch,
                )

                # ── Road offset fallback (disabled) ───────────────────────────
                # If pass 1 finds nothing, shift the viewpoint along the road
                # and retry. Useful when the pano is not directly in front of
                # the building. To enable, set offset_m in config and uncomment.
                #
                
                # if not self._has_any_confident_candidate(all_image_candidates):
                #     offset_m = self.config.road_offset_meters
                #     r_lat, r_lng = self._road_offset(latitude, longitude, _base_heading, +offset_m)
                #     logger.info(
                #         f"Pass 2: no confident result — shifting {offset_m}m RIGHT "
                #         f"to ({r_lat:.6f}, {r_lng:.6f})"
                #     )
                #     all_image_candidates.extend(self._fetch_candidates(
                #         r_lat, r_lng, _base_heading,
                #         pass_label="right",
                #         pitch_override=pitch,
                #     ))
                
                # if not self._has_any_confident_candidate(all_image_candidates):
                #     l_lat, l_lng = self._road_offset(latitude, longitude, _base_heading, -offset_m)
                #     logger.info(
                #         f"Pass 3: no confident result — shifting {offset_m}m LEFT "
                #         f"to ({l_lat:.6f}, {l_lng:.6f})"
                #     )
                #     all_image_candidates.extend(self._fetch_candidates(
                #         l_lat, l_lng, _base_heading,
                #         pass_label="left",
                #         pitch_override=pitch,
                #     ))

                # ── Perpendicular fallback (disabled) ──────────────────────────
                # When pass 1 finds nothing (road-snapped coordinate) it is
                # preferable to return no result rather than risk reading the
                # wrong building on the other side of the road.
                # To re-enable, uncomment the block below. It uses road_heading
                # from the GSV metadata to pick the correct perpendicular side
                # deterministically (no guessing when road_heading is available).
                #
                # if not self._has_any_confident_candidate(all_image_candidates):
                #     if road_heading is not None:
                #         angle_diff = (_base_heading - road_heading + 540) % 360 - 180
                #         perp = (road_heading + 90) % 360 if angle_diff >= 0 else (road_heading - 90) % 360
                #         side = "right" if angle_diff >= 0 else "left"
                #         logger.info(
                #             f"Perp fallback: road_heading={road_heading:.1f}°, "
                #             f"using {perp:.1f}° ({side} side of road)"
                #         )
                #         all_image_candidates.extend(self._fetch_candidates(
                #             latitude, longitude, perp,
                #             pass_label="perp",
                #             pitch_override=pitch,
                #         ))
                #     else:
                #         logger.warning(
                #             "Pass 1 found nothing and road_heading unavailable — "
                #             "skipping perpendicular fallback to avoid false positives."
                #         )

            if not all_image_candidates:
                logger.warning(f"Unable to retrieve any image for {latitude}, {longitude}")
                return {
                    "success":     False,
                    "latitude":    latitude,
                    "longitude":   longitude,
                    "door_number": None,
                    "confidence":  0,
                    "error":       "Image unavailable",
                }

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

            success = bool(door_number and confidence >= self.config.confidence_threshold)
            result = {
                "success":     success,
                "latitude":    latitude,
                "longitude":   longitude,
                "door_number": door_number if success else None,
                "confidence":  confidence,
                "heading":     heading,
                "pitch":       pitch,
                "timestamp":   datetime.now().isoformat(),
            }

            if not success:
                result["error"] = (
                    f"Low confidence ({confidence}%). "
                    f"Requires at least {self.config.confidence_threshold}% to pass."
                )
                logger.warning(
                    f"Low confidence result: '{door_number}' ({confidence}%). Marked as failed."
                )
            else:
                logger.info(f"Result: {door_number} (confidence: {confidence}%)")

            self.db.save_result(result)
            return result

        except Exception as exc:
            logger.error(f"Error processing coordinate: {exc}", exc_info=True)
            return {
                "success":   False,
                "latitude":  latitude,
                "longitude": longitude,
                "error":     str(exc),
            }

    def process_coordinates_batch(self, coordinates_list: list) -> list:
        results = []
        total   = len(coordinates_list)
        for index, coordinate in enumerate(coordinates_list, 1):
            logger.info(f"Processing {index}/{total}")
            result = self.detect_door_number(
                coordinate["latitude"],
                coordinate["longitude"],
                coordinate.get("heading"),  # None → auto-compute fires correctly
                coordinate.get("pitch"),    # None → full pitch sweep used
            )
            results.append(result)
        return results

    def close(self):
        self.db.close()
        logger.info("Door Number Detector finished")
