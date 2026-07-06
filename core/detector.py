"""Main door number detection workflow."""

import os
os.environ["FLAGS_use_mkldnn"] = "0"   # disables oneDNN — fixes Windows crash
os.environ["PADDLE_DISABLE_MKLDNN"] = "1"
os.environ["FLAGS_enable_pir_in_executor"] = "0"  # disables PIR execution path on Windows

import io
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
        self.current_debug_house_dir: Path | None = None  # set per detect_door_number call
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

    # ── ZONE FINDER: MSER — finds character-like blobs without any model ───────
    def _find_zones_mser(self, gray: np.ndarray) -> list[tuple[int, int, int, int]]:
        clahe      = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        gray_clahe = clahe.apply(gray)

        all_raw_boxes: list[tuple[int, int, int, int]] = []

        for variant in [gray, gray_clahe]:
            mser = cv2.MSER_create()
            mser.setDelta(5)
            mser.setMinArea(20)
            mser.setMaxArea(3000)
            regions, _ = mser.detectRegions(variant)
            if regions:
                all_raw_boxes += [
                    cv2.boundingRect(r.reshape(-1, 1, 2)) for r in regions
                ]

        char_boxes = [
            (x, y, w, h) for x, y, w, h in all_raw_boxes
            if h > 0 and 0.1 < (w / h) < 3.0 and h > 8
        ]

        if not char_boxes:
            return []

        deduped: list[tuple[int, int, int, int]] = []
        for box in char_boxes:
            x, y, w, h = box
            if not any(abs(x - sx) < 5 and abs(y - sy) < 5 for sx, sy, *_ in deduped):
                deduped.append(box)

        deduped.sort(key=lambda b: b[0])
        merged  = []
        current = list(deduped[0])

        for x, y, w, h in deduped[1:]:
            gap      = x - (current[0] + current[2])
            same_row = abs(y - current[1]) < current[3] * 0.8
            if gap < current[3] * 1.5 and same_row:
                x2 = max(current[0] + current[2], x + w)
                y1 = min(current[1], y)
                y2 = max(current[1] + current[3], y + h)
                current = [current[0], y1, x2 - current[0], y2 - y1]
            else:
                merged.append(tuple(current))
                current = [x, y, w, h]

        merged.append(tuple(current))
        word_boxes = [(x, y, w, h) for x, y, w, h in merged if w > 15]

        logger.info(f"MSER found {len(word_boxes)} candidate zone(s)")
        return word_boxes

    # ── Watermark helpers ──────────────────────────────────────────────────────
    # @staticmethod
    # def _bbox_center(bbox: list) -> tuple[float, float]:
    #     xs = [p[0] for p in bbox]
    #     ys = [p[1] for p in bbox]
    #     return sum(xs) / len(xs), sum(ys) / len(ys)

    # @staticmethod
    # def _is_watermark_text(text: str) -> bool:
    #     normalized = re.sub(r"[^a-z0-9]", "", text.lower())
    #     # Layer 1 — exact / near-exact
    #     if "google" in normalized or "©" in text:
    #         return True
    #     # Layer 2 — garbled Google: 'go' + 1-5 mixed chars ending in 'e' or '3'
    #     if re.search(r"go[0-9a-z]{1,5}[e3]", normalized):
    #         return True
    #     # Layer 3 — year-like run of 4+ digits ending in a vowel
    #     if re.search(r"\d{4,}[aeio]$", normalized):
    #         return True
    #     # Layer 4 — digits, then a vowel, then more digits/letters (mid-garble)
    #     if re.search(r"\d{3,}[aeio]\d+", normalized):
    #         return True
    #     # Layer 5 — 5+ digit string starting with 19xx/20xx, ≤1 alpha char
    #     digits_only = re.sub(r"[^0-9]", "", text)
    #     alpha_count = sum(1 for c in normalized if c.isalpha())
    #     if (len(digits_only) >= 5
    #             and re.match(r"(19|20)\d{2}", digits_only)
    #             and alpha_count <= 1):
    #         return True
    #     # Layer 6 — copyright year prefix followed by 'g' (partial Google read)
    #     if re.search(r"^(19|20)\d{0,2}g", normalized):
    #         return True
    #     # Layer 7 — decimal-format number: not a valid house number
    #     if re.match(r"^\d+\.\d+$", text.strip()):
    #         return True
    #     return False

    # def _near_any_watermark(
    #     self, bbox: list, watermark_bboxes: list, margin: int = 350
    # ) -> bool:
    #     if not watermark_bboxes:
    #         return False
    #     cx, cy = self._bbox_center(bbox)
    #     for wb in watermark_bboxes:
    #         wcx, wcy = self._bbox_center(wb)
    #         if abs(cx - wcx) < margin and abs(cy - wcy) < margin:
    #             return True
    #     return False

    # def _detect_watermark_regions(self, scan_img: np.ndarray) -> list:
    #     gray = cv2.cvtColor(scan_img, cv2.COLOR_BGR2GRAY)
    #     clahe_strong   = cv2.createCLAHE(clipLimit=6.0, tileGridSize=(4, 4))
    #     clahe_moderate = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    #     watermark_bboxes = []
    #     seen_centers: list[tuple[float, float]] = []
    #     def _add_anchor(bbox, text, source):
    #         cx, cy = self._bbox_center(bbox)
    #         if any(abs(cx - sx) < 100 and abs(cy - sy) < 100 for sx, sy in seen_centers):
    #             return
    #         seen_centers.append((cx, cy))
    #         watermark_bboxes.append(bbox)
    #         logger.info(f"Watermark pre-scan [{source}] found anchor: '{text}'")
    #     for variant_name, gray_variant in [
    #         ("clahe_strong",   clahe_strong.apply(gray)),
    #         ("clahe_moderate", clahe_moderate.apply(gray)),
    #     ]:
    #         enhanced = cv2.cvtColor(gray_variant, cv2.COLOR_GRAY2BGR)
    #         try:
    #             if self._ocr_engine == "paddleocr":
    #                 result = self._paddle_reader.ocr(enhanced)
    #                 if not result:
    #                     continue
    #                 page = (
    #                     result
    #                     if isinstance(result[0], list)
    #                     and len(result[0]) >= 2
    #                     and isinstance(result[0][1], tuple)
    #                     else result[0]
    #                 )
    #                 if page:
    #                     for bbox, (text, _) in page:
    #                         if self._is_watermark_text(text):
    #                             _add_anchor(bbox, text, variant_name)
    #             else:
    #                 results = self._ocr_reader.readtext(
    #                     enhanced,
    #                     paragraph=False,
    #                     detail=1,
    #                     min_size=3,
    #                     text_threshold=0.05,
    #                     low_text=0.05,
    #                     link_threshold=0.2,
    #                     canvas_size=2560,
    #                 )
    #                 for bbox, text, _ in results:
    #                     if self._is_watermark_text(text):
    #                         _add_anchor(bbox, text, variant_name)
    #         except Exception as exc:
    #             logger.warning(f"Watermark pre-scan [{variant_name}] failed: {exc}")
    #     logger.info(f"Watermark pre-scan: {len(watermark_bboxes)} anchor(s) found")
    #     return watermark_bboxes

    def _extract_door_number(self, image: Image.Image) -> tuple[str | None, int]:
        cv_img = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        orig_h, orig_w = cv_img.shape[:2]

        all_candidates: list[tuple[str, float]] = []

        logger.info(f"OCR [{self._ocr_engine}]: full image 2x upscale...")
        full_2x = self._upscale(cv_img, scale=2)

        # # Pre-scan: find ALL watermark anchors before main pass
        # pre_watermark_bboxes = self._detect_watermark_regions(full_2x)

        # ── Helpers ───────────────────────────────────────────────────────────
        # def _is_google_watermark(text: str, bbox: list) -> bool:
        #     normalized = re.sub(r"[^a-z0-9]", "", text.lower())
        #     if "google" not in normalized:
        #         return False
        #     y_max = max(int(p[1]) for p in bbox)
        #     return y_max >= orig_h - 32

        # def _apply_watermark_filters(bbox, text, tag, watermark_bboxes):
        #     if _is_google_watermark(text, bbox):
        #         logger.info(f"Skipped Google watermark [{tag}] '{text}'")
        #         return True
        #     if self._near_any_watermark(bbox, watermark_bboxes):
        #         logger.info(f"Skipped detection near watermark [{tag}] '{text}'")
        #         return True
        #     return False

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

            # # Seed with pre-scan anchors + any watermark text found in this pass
            # watermark_bboxes = list(pre_watermark_bboxes)
            # watermark_bboxes += [
            #     bbox for bbox, text, _ in results
            #     if self._is_watermark_text(text)
            # ]

            filtered = []
            for bbox, text, conf in results:
                # if _apply_watermark_filters(bbox, text, tag, watermark_bboxes):
                #     continue
                for num in re.findall(r"\d{1,4}", text.strip()):
                    # Door numbers never start with 0 (e.g. "050" is a cropped "5050")
                    if len(num) > 1 and num.startswith("0"):
                        logger.info(f"OCR: skipped leading-zero fragment '{num}'")
                        continue
                    all_candidates.append((num, conf))
                    logger.info(f"EasyOCR [{tag}] '{num}' ← '{text}' ({conf * 100:.0f}%)")
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

            # # Seed with pre-scan anchors + any watermark text found in this pass
            # watermark_bboxes = list(pre_watermark_bboxes)
            # watermark_bboxes += [
            #     bbox for bbox, (text, _) in page
            #     if self._is_watermark_text(text)
            # ]

            normalized_results = []
            for line in page:
                bbox, (text, conf) = line
                # if _apply_watermark_filters(bbox, text, tag, watermark_bboxes):
                #     continue
                for num in re.findall(r"\d{1,4}", text.strip()):
                    # Door numbers never start with 0 (e.g. "050" is a cropped "5050")
                    if len(num) > 1 and num.startswith("0"):
                        logger.info(f"OCR: skipped leading-zero fragment '{num}'")
                        continue
                    all_candidates.append((num, conf))
                    logger.info(f"PaddleOCR [{tag}] '{num}' ← '{text}' ({conf * 100:.0f}%)")
                normalized_results.append((bbox, text, conf))
            return normalized_results

        # ── Run main OCR pass ─────────────────────────────────────────────────
        if self._ocr_engine == "paddleocr":
            _run_paddleocr(full_2x, "full_2x")
        else:
            _run_easyocr(full_2x, "full_2x")

        if not all_candidates:
            logger.info("No candidates found")
            return None, 0

        # ── Scoring por soma de confiança ─────────────────────────────────────
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
    # Wide FOV captures more scene per shot → fewer steps needed.
    # Narrow FOV sees a small slice → dense heading & pitch grid required.
    _FOV_SWEEP: list[tuple[int, list[int], list[int]]] = [
        #  fov   heading_offsets                                      pitch_values
        (  60,   [0, -20, 20],                                       [-10, 0, 10]                     ),
        (  40,   [0, -15, 15, -30, 30],                              [-10, -5, 0, 5, 10]              ),
        (  30,   [0, -10, 10, -20, 20, -30, 30],                     [-10, -5, 0, 5, 10, 15]          ),
        (  20,   [0, -5, 5, -10, 10, -20, 20, -30, 30],              [-15, -10, -5, 0, 5, 10, 15]     ),
        (  10,   [0, -5, 5, -10, 10, -15, 15, -20, 20, -25, 25],    [-15, -10, -5, 0, 5, 10, 15, 20] ),
    ]


    # ── Early-exit thresholds ──────────────────────────────────────────────────
    # A number this short (digits) requires _EARLY_EXIT_MIN_AGREE agreeing shots
    # before we trust it and skip narrower FOV levels.
    # Rationale: a 2-digit read like "13" can be a cropped "131"; a 3-digit read
    # is far less likely to be a truncated longer number.
    _EARLY_EXIT_MIN_DIGITS: int = 4
    _EARLY_EXIT_MIN_AGREE:  int = 3

    def _is_suspicious_number(self, num: str) -> bool:
        """Numbers that need corroboration before early exit:
        - Short (< 3 digits) — could be a cropped longer number
        - Year-like (19xx/20xx) — likely Google watermark
        """
        return len(num) < self._EARLY_EXIT_MIN_DIGITS or bool(re.fullmatch(r"(19|20)\d{2}", num))

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

        # Long numbers are trusted on a single high-confidence shot — a 3-digit
        # read is unlikely to be a cropped fragment of a longer number.
        if not self._is_suspicious_number(best_number):
            return True

        # Short numbers (1-2 digits) must appear in at least N confident shots
        # before we early-exit.  One edge-cropped frame returning "13" at 99%
        # is not enough — we need corroboration, or we keep zooming in (FOV→10)
        # where the extra pixels often reveal the missing trailing digit(s).
        agreeing = sum(
            1 for c in candidates
            if c["number"] == best_number
            and c["confidence"] >= self.config.confidence_threshold
        )
        return agreeing >= self._EARLY_EXIT_MIN_AGREE
        
    def _has_any_confident_candidate(self, candidates: list[dict]) -> bool:
        """True if at least one candidate clears the confidence threshold.
        Used to gate pass 2/3 — only move to a different position when centre
        produced *nothing* plausible, not merely when the best read is short."""
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

        heading_offsets and pitch_values are driven by the per-FOV sweep table so
        that wide FOVs use a coarse grid (cheap) and narrow FOVs use a dense grid
        (thorough).  When the caller supplies a specific pitch (pitch_override),
        only that pitch is used at every FOV level.

        Exits early as soon as a confident result is found, avoiding unnecessary
        API calls at smaller FOV levels.
        """
        all_image_candidates: list[dict] = []

        for fov_try, fov_heading_offsets, fov_pitch_values in self._FOV_SWEEP:
            pitch_values = [pitch_override] if pitch_override is not None else fov_pitch_values
            fov_candidates: list[dict] = []

            for pitch_try in pitch_values:
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

                    fov_candidates.append({
                        "number":     number,
                        "confidence": confidence,
                        "heading":    offset,
                        "pitch":      pitch_try,
                        "fov":        fov_try,
                    })

            all_image_candidates.extend(fov_candidates)

            # ── Early exit ────────────────────────────────────────────────────
            if self._has_confident_result(all_image_candidates):
                logger.info(
                    f"[{pass_label}] Confident result at FOV={fov_try} — skipping narrower levels."
                )
                break

        return all_image_candidates


    def detect_door_number(self, latitude, longitude, heading=None, pitch=None, image=None):
        """Detect the door number for a specific coordinate."""
        logger.info(f"Processing coordinate: {latitude}, {longitude}")

        try:
            # ── Per-house debug directory ──────────────────────────────────────────
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
                if _base_heading is None:
                    try:
                        cam_lat, cam_lng = self.street_view.get_pano_location(latitude, longitude)
                        _base_heading = StreetViewFetcher.calculate_heading(
                            cam_lat, cam_lng, latitude, longitude
                        )
                        logger.info(f"Pre-computed heading for road offset: {_base_heading:.1f}°")
                    except Exception as exc:
                        logger.warning(f"Could not compute heading for road offset: {exc}. Using 0°.")
                        _base_heading = 0

                offset_m = self.config.road_offset_meters

                # ── Pass 1: original position ──────────────────────────────────
                logger.info("Pass 1/3: original position")
                all_image_candidates = self._fetch_candidates(
                    latitude, longitude, heading,
                    pass_label="center",
                    pitch_override=pitch,
                )

                # ── Pass 2: shift right along the road ────────────────────────
                if not self._has_any_confident_candidate(all_image_candidates):
                    r_lat, r_lng = self._road_offset(latitude, longitude, _base_heading, +offset_m)
                    logger.info(
                        f"Pass 2/3: no confident result — shifting {offset_m}m RIGHT "
                        f"to ({r_lat:.6f}, {r_lng:.6f})"
                    )
                    all_image_candidates.extend(self._fetch_candidates(
                        r_lat, r_lng, heading,
                        pass_label="right",
                        pitch_override=pitch,
                    ))

                # ── Pass 3: shift left along the road ─────────────────────────
                if not self._has_any_confident_candidate(all_image_candidates):
                    l_lat, l_lng = self._road_offset(latitude, longitude, _base_heading, -offset_m)
                    logger.info(
                        f"Pass 3/3: no confident result — shifting {offset_m}m LEFT "
                        f"to ({l_lat:.6f}, {l_lng:.6f})"
                    )
                    all_image_candidates.extend(self._fetch_candidates(
                        l_lat, l_lng, heading,
                        pass_label="left",
                        pitch_override=pitch,
                    ))

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
                coordinate.get("heading", 0),
                coordinate.get("pitch", 0),
            )
            results.append(result)
        return results

    def close(self):
        self.db.close()
        logger.info("Door Number Detector finished")
