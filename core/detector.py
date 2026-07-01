"""Main door number detection workflow."""

import os
os.environ["FLAGS_use_mkldnn"] = "0"   # disables oneDNN — fixes Windows crash
os.environ["PADDLE_DISABLE_MKLDNN"] = "1"
os.environ["FLAGS_enable_pir_in_executor"] = "0"  # disables PIR execution path on Windows

import io
import logging
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
            # gpu=False is safe default; set gpu=True if you have CUDA
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

    def _save_debug_image(self, image, name):
        path = self.debug_dir / name
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
        """
        MSER em raw gray E em imagem CLAHE-melhorada — apanha texto de baixo contraste.
        Retorna bounding boxes ao nível de palavra.
        """
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


    def _extract_door_number(self, image: Image.Image) -> tuple[str | None, int]:
        """
        Pipeline:
        1. OCR na imagem completa 2x
        2. OCR em CLAHE 2x
        3. OCR em 3 crops de entrada
        4. Tesseract fallback (PSMs 6/7/8/11)
        5. Votação por soma de confiança
        """
        w_orig, h_orig = image.size

        cv_img = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        gray   = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
        orig_h, orig_w = cv_img.shape[:2]

        all_candidates: list[tuple[str, float]] = []
        clahe_obj = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))

        # ── Helpers ───────────────────────────────────────────────────────────
        def _is_google_watermark(text: str, bbox: list[tuple[float, float]]) -> bool:
            normalized = re.sub(r"[^a-z0-9]", "", text.lower())
            if "google" not in normalized:
                return False
            y_max = max(int(p[1]) for p in bbox)
            return y_max >= orig_h - 32

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
                if _is_google_watermark(text, bbox):
                    logger.info(f"Skipped Google watermark [{tag}] '{text}'")
                    continue
                for num in re.findall(r"\d{1,4}", text.strip()):
                    all_candidates.append((num, conf))
                    logger.info(f"EasyOCR [{tag}] '{num}' ← '{text}' ({conf * 100:.0f}%)")
                filtered.append((bbox, text, conf))
            return filtered

        def _run_paddleocr(scan_img: np.ndarray, tag: str) -> list:
            ocr_result = self._paddle_reader.ocr(scan_img)
            if not ocr_result:
                return []

            # PaddleOCR v2 returns a flat list of lines: [ [bbox, (text, score)], ... ]
            # PaddleOCR v3 returns a list of pages where the first page is the image result.
            if isinstance(ocr_result[0], list) and len(ocr_result[0]) >= 2 and isinstance(ocr_result[0][1], tuple):
                page = ocr_result
            else:
                page = ocr_result[0]

            if not page:
                return []

            normalized = []
            for line in page:
                bbox, (text, conf) = line
                if _is_google_watermark(text, bbox):
                    logger.info(f"Skipped Google watermark [{tag}] '{text}'")
                    continue
                for num in re.findall(r"\d{1,4}", text.strip()):
                    all_candidates.append((num, conf))
                    logger.info(f"PaddleOCR [{tag}] '{num}' ← '{text}' ({conf * 100:.0f}%)")
                normalized.append((bbox, text, conf))
            return normalized

        # Dispatch to the right engine
        _run_ocr = _run_paddleocr if self._ocr_engine == "paddleocr" else _run_easyocr

        def _annotate(canvas: np.ndarray, results: list, label: str = "") -> np.ndarray:
            for bbox, text, conf in results:
                has_num = bool(re.findall(r"\d{1,4}", text.strip()))
                color   = (0, 200, 0) if has_num else (0, 0, 220)
                pts     = np.array([[int(p[0]), int(p[1])] for p in bbox], np.int32)
                cv2.polylines(canvas, [pts], True, color, 2)
                x0 = int(min(p[0] for p in bbox))
                y0 = int(min(p[1] for p in bbox))
                cv2.putText(canvas, f"{text}({conf * 100:.0f}%)",
                            (x0, max(y0 - 6, 12)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
            if label:
                cv2.putText(canvas, label, (8, canvas.shape[0] - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
            return canvas

        def _to_bgr(img: np.ndarray) -> np.ndarray:
            return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR) if img.ndim == 2 else img.copy()

        # ── Step 1: Imagem completa 2x ────────────────────────────────────────
        logger.info(f"OCR [{self._ocr_engine}]: full image 2x upscale...")
        full_2x  = self._upscale(cv_img, scale=2)
        full_res = _run_ocr(full_2x, "full_2x")
        if self.config.debug:
            vis = _annotate(full_2x.copy(), full_res,
                            f"full_2x | {len(full_res)} detections")
            self._save_debug_image(vis, "scan_01_full_2x.png")

        # ── Step 2: CLAHE 2x ──────────────────────────────────────────────────
        logger.info(f"OCR [{self._ocr_engine}]: clahe...")
        gray_clahe = clahe_obj.apply(gray)
        clahe_2x   = self._upscale(gray_clahe, scale=2)
        clahe_res  = _run_ocr(clahe_2x, "clahe")
        if self.config.debug:
            vis = _annotate(_to_bgr(clahe_2x).copy(), clahe_res,
                            f"clahe | {len(clahe_res)} detections")
            self._save_debug_image(vis, "scan_02_clahe.png")

        # ── Step 3: Crops de entrada ──────────────────────────────────────────
        entrance_crops = {
            "enter_lower_center": (orig_h // 3,        orig_h, orig_w // 4, 3 * orig_w // 4),
            "enter_bottom_strip": (int(orig_h * 0.55), orig_h, 0,           orig_w),
            "enter_center_col":   (0,                  orig_h, orig_w // 3, 2 * orig_w // 3),
        }

        # for crop_name, (y1, y2, x1, x2) in entrance_crops.items():
        #     crop = cv_img[y1:y2, x1:x2]
        #     if crop.size == 0:
        #         continue

        #     crop_up         = self._upscale(crop, scale=3)
        #     crop_gray_clahe = clahe_obj.apply(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY))
        #     crop_clahe_up   = self._upscale(crop_gray_clahe, scale=3)

        #     logger.info(f"OCR: entrance crop '{crop_name}'...")
        #     res_color = _run_ocr(crop_up,       crop_name)
        #     res_clahe = _run_ocr(crop_clahe_up, f"{crop_name}_clahe")

        #     if self.config.debug:
        #         vis = _annotate(crop_up.copy(), res_color,
        #                         f"{crop_name} | {len(res_color)} det.")
        #         self._save_debug_image(vis, f"scan_03_{crop_name}.png")
        #         vis2 = _annotate(_to_bgr(crop_clahe_up).copy(), res_clahe,
        #                         f"{crop_name}_clahe | {len(res_clahe)} det.")
        #         self._save_debug_image(vis2, f"scan_03_{crop_name}_clahe.png")

        # # ── Step 4: Tesseract fallback ────────────────────────────────────────
        # logger.info("Tesseract fallback (PSMs 6/7/8/11)...")
        # for psm in [6, 7, 8, 11]:
        #     try:
        #         data = pytesseract.image_to_data(
        #             image,
        #             config=f"--oem 3 --psm {psm}",
        #             lang=self.config.ocr_language,
        #             output_type=pytesseract.Output.DICT,
        #         )
        #         for word, conf in zip(data["text"], data["conf"]):
        #             word = str(word).strip()
        #             try:
        #                 conf_int = int(float(conf))
        #             except (ValueError, TypeError):
        #                 continue
        #             if conf_int < 0:
        #                 continue
        #             for num in re.findall(r"\d{1,4}", word):
        #                 all_candidates.append((num, conf_int / 100.0))
        #                 logger.info(f"Tesseract psm={psm} '{num}' ({conf_int}%)")
        #     except Exception as exc:
        #         logger.warning(f"Tesseract psm={psm} failed: {exc}")

        if not all_candidates:
            logger.info("No candidates found")
            return None, 0

        # ── Step 5: Scoring por soma de confiança ─────────────────────────────
        best_conf: dict[str, float] = {}
        conf_sum:  dict[str, float] = {}

        for num, conf in all_candidates:
            if num not in best_conf or conf > best_conf[num]:
                best_conf[num] = conf
            conf_sum[num] = conf_sum.get(num, 0.0) + conf

        def score(num: str) -> tuple:
            return (conf_sum[num], len(num), best_conf[num])

        best_number         = max(best_conf.keys(), key=score)
        best_confidence_pct = int(best_conf[best_number] * 100)

        vote_counts = Counter(num for num, _ in all_candidates)
        logger.info(
            f"Best: '{best_number}' ({best_confidence_pct}%) "
            f"conf_sum={conf_sum[best_number]:.2f} "
            f"votes={vote_counts[best_number]}/{len(all_candidates)}"
        )
        return best_number, best_confidence_pct



    # ── Everything below is UNCHANGED from your existing class ────────────────

    def detect_door_number(self, latitude, longitude, heading=None, pitch=None, image=None):
        """Detect the door number for a specific coordinate."""
        logger.info(f"Processing coordinate: {latitude}, {longitude}")

        try:
            heading_offsets = [0, -20, 20, -40, 40, -60, 60]
            pitch_values    = [5, 10, 15] if pitch == None else [pitch]

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
                for fov_try in [90, 60, 40, 30, 20]:
                    fov_candidates = []

                    for pitch_try in pitch_values:
                        for offset in heading_offsets:
                            logger.info(f"Trying FOV={fov_try} offset={offset} pitch={pitch_try}")
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
                                    f"No image returned for offset={offset} pitch={pitch_try}"
                                )
                                continue

                            if self.config.debug:
                                debug_name = f"candidate_fov{fov_try}_offset{offset}_pitch{pitch_try}.png"
                                self._save_debug_image(candidate_image, debug_name)

                            number, confidence = self._extract_door_number(candidate_image)
                            logger.info(
                                f"FOV={fov_try} offset={offset} pitch={pitch_try} -> "
                                f"'{number}' (confidence: {confidence}%)"
                            )

                            fov_candidates.append({
                                "number":     number,
                                "confidence": confidence,
                                "heading":    offset,
                                "pitch":      pitch_try,
                                "fov":        fov_try,
                            })

                    if not fov_candidates:
                        continue

                    all_image_candidates.extend(fov_candidates)

                    best_this_fov = max(fov_candidates, key=lambda c: c["confidence"])
                    if best_this_fov["number"] and best_this_fov["confidence"] >= self.config.confidence_threshold:
                        logger.info(f"Found result at FOV={fov_try}, skipping smaller FOVs")
                        break

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

            best_candidate = max(
                all_image_candidates,
                key=lambda c: (
                    c["confidence"],
                    len(c["number"]) if c["number"] else 0,
                ),
            )

            door_number = best_candidate["number"]
            confidence  = best_candidate["confidence"]
            heading     = best_candidate["heading"]
            pitch       = best_candidate["pitch"]

            logger.info(
                f"Best across all offsets: '{door_number}' ({confidence}%) "
                f"at heading_offset={heading} pitch={pitch}"
            )

            success = bool(
                door_number and confidence >= self.config.confidence_threshold
            )
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
