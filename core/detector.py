"""Main door number detection workflow."""

import os
os.environ["FLAGS_use_mkldnn"] = "0"
os.environ["PADDLE_DISABLE_MKLDNN"] = "1"
os.environ["FLAGS_enable_pir_in_executor"] = "0"

import io
from collections import defaultdict
import logging
import math
import re
import sys
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from core.config import Config
from core.database import DatabaseManager
from core.google_street_view import StreetViewFetcher, NoImageryAvailable


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

# ── Door number patterns ───────────────────────────────────────────────────────

_KEYWORD_RE = re.compile(
    r"\b(LOTE|LOT|SECTOR|SEC|BLOCO|BL|APARTAMENTO|APT|FRACCAO|FRACAO)\s*(\d{1,4})\b",
    re.IGNORECASE,
)

_DOOR_NUMBER_RE = re.compile(r"[A-Za-z]?\d{1,4}[A-Za-z]?")

_BLOCKLIST: set[str] = {
    "2024G", "2023G", "2022G", "2025G", "2026G",
    "G000", "G001", "G002", "G003", "G004",
    "2024", "2025",
}


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
            self.config.google_api_key,
            size=self.config.street_view_size,
            https_proxy=self.config.https_proxy,
        )

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
        h, w = image.shape[:2]
        return cv2.resize(image, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)

    @staticmethod
    def _is_leading_zero(num: str) -> bool:
        return bool(re.match(r"0\d", num))

    @staticmethod
    def _is_year_like(num: str) -> bool:
        return bool(
            re.fullmatch(r"202", num)      or
            re.fullmatch(r"20\d{2}", num)  or
            re.fullmatch(r"19\d{2}", num)
        )

    @staticmethod
    def _is_suspicious_short(num: str) -> bool:
        if re.fullmatch(r"2[0-6]", num):
            return True
        return False

    def _is_bad_candidate(self, num: str) -> bool:
        return self._is_year_like(num) or self._is_suspicious_short(num)

    def _has_non_year_alternative(self, candidates: list[dict]) -> bool:
        return any(
            c["number"]
            and not self._is_bad_candidate(c["number"])
            and c["number"] not in _BLOCKLIST
            and c["confidence"] >= self.config.confidence_threshold
            for c in candidates
        )

    def _needs_more_search(self, candidates: list[dict]) -> bool:
        if not self._has_any_confident_candidate(candidates):
            return True
        _tmp_best = max(
            (c for c in candidates if c["number"]),
            key=lambda c: (c["confidence"], len(c["number"]) if c["number"] else 0),
            default=None,
        )
        if _tmp_best is None:
            return False
        return (
            self._is_bad_candidate(_tmp_best["number"])
            and not self._has_non_year_alternative(candidates)
        )

    def _parse_ocr_text(self, text: str) -> list[str]:
        upper = text.strip().upper()

        keyword_hits = _KEYWORD_RE.findall(upper)
        if keyword_hits:
            combined = "-".join(f"{kw}{num}" for kw, num in keyword_hits)
            if combined in _BLOCKLIST:
                logger.info(f"OCR: blocked keyword fragment '{combined}'")
                return []
            return [combined]

        results = []
        for num in _DOOR_NUMBER_RE.findall(upper):
            if self._is_leading_zero(num):
                logger.info(f"OCR: skipped leading-zero fragment '{num}'")
                continue
            if num in _BLOCKLIST:
                logger.info(f"OCR: blocked fragment '{num}' (blocklist)")
                continue
            results.append(num)
        return results

    def _extract_door_number(self, image: Image.Image) -> tuple[str | None, int]:
        cv_img = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)

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
                for num in self._parse_ocr_text(text):
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
                for num in self._parse_ocr_text(text):
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

    @staticmethod
    def _road_offset(
        lat: float, lng: float, base_heading: float, distance_m: float
    ) -> tuple[float, float]:
        bearing = math.radians((base_heading + 90) % 360)
        dlat = distance_m * math.cos(bearing) / 111_000
        dlng = distance_m * math.sin(bearing) / (111_000 * math.cos(math.radians(lat)))
        return lat + dlat, lng + dlng

    _FOV_SWEEP: list[tuple[int, list[int], list[int]]] = [
        (10, [0, -5, 5, -10, 10, -15, 15, -20, 20, -25, 25, -30, 30], [0, -5, 5, -10, 10, -15, 15]),
    ]

    _EARLY_EXIT_MIN_DIGITS: int = 4
    _EARLY_EXIT_MIN_AGREE:  int = 1

    def _is_suspicious_number(self, num: str) -> bool:
        return True

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

        if not self._is_suspicious_number(best_number):
            return True

        agreeing = sum(
            1 for c in candidates
            if c["number"] == best_number
            and c["confidence"] >= self.config.confidence_threshold
        )
        return agreeing >= self._EARLY_EXIT_MIN_AGREE

    def _has_any_confident_candidate(self, candidates: list[dict]) -> bool:
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
        sweep: list | None = None,
        cancel_event: threading.Event | None = None,
    ) -> list[dict]:
        """Run the FOV sweep for a given position.

        Raises NoImageryAvailable if Street View confirms no coverage for this
        coordinate (detected via metadata ZERO_RESULTS or the grey placeholder).
        Once detected, the entire sweep is aborted immediately — all subsequent
        images for the same coordinate would be identical placeholders.
        """
        fov_sweep = sweep if sweep is not None else self._FOV_SWEEP
        all_image_candidates: list[dict] = []
        done        = False
        _no_imagery = False  # set True the moment a no-imagery response is detected

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

                    # ── Fetch — catch no-imagery placeholder ──────────────────
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

                    if self._has_confident_result(all_image_candidates):
                        logger.info(
                            f"[{pass_label}] Confident result at FOV={fov_try} "
                            f"offset={offset} pitch={pitch_try} — stopping sweep."
                        )
                        done = True
                        break

        # Propagate no-imagery to the caller so it can record the observation.
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
    ):
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
                if cancel_event is not None and cancel_event.is_set():
                    logger.info(f"Cancelled before fetching ({latitude}, {longitude}) — skipping.")
                    return {"success": False, "latitude": latitude, "longitude": longitude,
                            "door_number": None, "confidence": 0, "error": "Cancelled"}

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
                        # Metadata already confirmed no pano — skip immediately.
                        raise
                    except Exception as exc:
                        logger.warning(f"Could not compute heading: {exc}. Using 0°.")
                        _base_heading = 0

                effective_road_heading = road_heading if road_heading is not None else (_base_heading + 90) % 360
                if road_heading is None:
                    logger.warning(
                        f"road_heading unavailable — approximating as {effective_road_heading:.1f}° "
                        f"(perpendicular to camera)"
                    )

                angle_diff = (_base_heading - effective_road_heading + 360) % 360
                side_sign  = -1 if angle_diff < 180 else +1

                wide_offsets_fwd  = [side_sign * o for o in [45, 60, 90]]
                wide_offsets_back = [-side_sign * o for o in [45, 60, 90]]

                fov_sweep_wide_direct = [
                    (10, wide_offsets_fwd,  [0, -5, 5, -10, 10, -15, 15]),
                ]
                fov_sweep_wide_right = [
                    (10, wide_offsets_back, [0, -5, 5, -10, 10, -15, 15]),
                ]
                fov_sweep_wide_left = [
                    (20, wide_offsets_fwd,  [0, -5, 5, -10, 10, -15, 15]),
                    (10, wide_offsets_fwd,  [0, -5, 5, -10, 10, -15, 15]),
                ]

                offset_m = self.config.road_offset_meters
                along_road_heading = (effective_road_heading - 90 + 360) % 360
                r_lat, r_lng = self._road_offset(latitude, longitude, along_road_heading, +offset_m)
                l_lat, l_lng = self._road_offset(latitude, longitude, along_road_heading, -offset_m)

                # Pass 1 — direct heading, normal offsets (±30°)
                logger.info(f"Pass 1: direct heading {_base_heading:.1f}° (normal sweep ±30°)")
                all_image_candidates = self._fetch_candidates(
                    latitude, longitude, _base_heading,
                    pass_label="direct",
                    pitch_override=pitch,
                    sweep=self._FOV_SWEEP,
                    cancel_event=cancel_event,
                )

                # Pass 1b — wide sweep toward building facade
                # if self._needs_more_search(all_image_candidates):
                #     if cancel_event is not None and cancel_event.is_set():
                #         logger.info("Cancelled before Pass 1b.")
                #     else:
                #         logger.info(
                #             f"Pass 1b: direct wide sweep "
                #             f"({'RIGHT' if side_sign == 1 else 'LEFT'}, angle_diff={angle_diff:.1f}°)"
                #         )
                #         all_image_candidates.extend(self._fetch_candidates(
                #             latitude, longitude, _base_heading,
                #             pass_label="direct_wide",
                #             pitch_override=pitch,
                #             sweep=fov_sweep_wide_direct,
                #             cancel_event=cancel_event,
                #         ))

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
                        and c["number"] not in _BLOCKLIST
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
                            and c["number"] not in _BLOCKLIST
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
            result = {
                "success":     success,
                "latitude":    latitude,
                "longitude":   longitude,
                "door_number": door_number if success else None,
                "confidence":  confidence,
                "heading":     heading,
                "pitch":       pitch,
                "observation": "",
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

        except NoImageryAvailable:
            # ── No Street View coverage — record and return cleanly ────────────
            logger.warning(
                f"No Street View imagery available for ({latitude}, {longitude}) — "
                f"skipping OCR and recording observation."
            )
            result = {
                "success":     False,
                "latitude":    latitude,
                "longitude":   longitude,
                "door_number": None,
                "confidence":  0,
                "observation": "No Street View imagery available",
                "timestamp":   datetime.now().isoformat(),
            }
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
                coordinate.get("heading"),
                coordinate.get("pitch"),
            )
            results.append(result)
        return results

    def close(self):
        self.db.close()
        logger.info("Door Number Detector finished")
