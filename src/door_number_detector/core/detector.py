"""Main door number detection workflow."""

import logging
import threading
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from door_number_detector.core.candidate_selection import Candidate, CandidateSelector
from door_number_detector.core.config import Config
from door_number_detector.core.database import DatabaseManager
from door_number_detector.core.google_street_view import NoImageryAvailable, StreetViewFetcher
from door_number_detector.core.guided import GuidedSearch, LocalHTTPPlanner, SearchLimits
from door_number_detector.core.models import DetectionOutcome, DetectionResult
from door_number_detector.core.ocr import DoorNumberOCR
from door_number_detector.core.outcomes import OUTCOME_DETAILS
from door_number_detector.core.redaction import redact_api_key
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
        guided_planner=None,
        debug_root: Path | None = None,
    ):
        self.config = config if config is not None else Config(env_path)
        self.detection_strategy = getattr(self.config, "detection_strategy", "sweep")
        if self.detection_strategy not in {"sweep", "guided"}:
            raise ValueError("DETECTION_STRATEGY must be 'sweep' or 'guided'.")
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
        self._local_vlm_planner: LocalHTTPPlanner | None = None
        if guided_planner is None and self.detection_strategy == "guided":
            chat_url = getattr(self.config, "local_vlm_chat_url", "")
            model = getattr(self.config, "local_vlm_model", "")
            if bool(chat_url) != bool(model):
                raise ValueError("Set both LOCAL_VLM_CHAT_URL and LOCAL_VLM_MODEL for guided mode.")
            if not chat_url or not model:
                raise ValueError("Guided strategy requires a configured local planner.")
            self._local_vlm_planner = LocalHTTPPlanner(
                chat_url,
                model,
                timeout=getattr(self.config, "local_vlm_timeout_seconds", 300),
            )
            guided_planner = self._local_vlm_planner

        self.ocr = DoorNumberOCR(reader=ocr_engine, logger=logger)
        self.candidate_selector = CandidateSelector(self.config.confidence_threshold, logger)
        self.sweep = StreetViewSweep(
            self.street_view,
            self.ocr,
            self.candidate_selector,
            debug=self.config.debug,
            logger=logger,
        )
        self.guided_search = (
            GuidedSearch(
                self.street_view,
                self.ocr,
                guided_planner,
                self.candidate_selector,
                limits=SearchLimits(
                    max_images=getattr(self.config, "guided_max_images", 12),
                    max_model_calls=getattr(self.config, "guided_max_model_calls", 16),
                    max_panorama_moves=getattr(self.config, "guided_max_panorama_moves", 2),
                    max_metadata_discoveries=getattr(
                        self.config, "guided_max_metadata_discoveries", 8
                    ),
                ),
            )
            if guided_planner is not None
            else None
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

    def _usage_since(self, before: tuple[int, int], enabled: bool) -> tuple[int, int]:
        if not enabled:
            return 0, 0
        after = self.street_view.get_call_counts()
        return after[0] - before[0], after[1] - before[1]

    @staticmethod
    def _unsuccessful_result(
        outcome: DetectionOutcome, latitude, longitude, usage: tuple[int, int]
    ) -> DetectionResult:
        details = OUTCOME_DETAILS[outcome]
        return DetectionResult(
            success=False,
            outcome=outcome,
            latitude=latitude,
            longitude=longitude,
            observation=details.observation,
            error=details.database_message,
            image_calls=usage[0],
            metadata_calls=usage[1],
        )

    def _finish_result(self, result: DetectionResult) -> DetectionResult:
        """Save the result when its outcome is persisted; a failed save becomes FAILED."""
        assert result.outcome is not None
        if not OUTCOME_DETAILS[result.outcome].persist:
            return result
        try:
            self.db.save_result(result.to_dict())
        except Exception:
            logger.error("Could not save detection result", exc_info=True)
            if result.outcome is DetectionOutcome.FAILED:
                return result
            return self._unsuccessful_result(
                DetectionOutcome.FAILED,
                result.latitude,
                result.longitude,
                (result.image_calls, result.metadata_calls),
            )
        return result

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
        track_usage = image is None
        usage_before = self.street_view.get_call_counts() if track_usage else (0, 0)

        try:
            if self.config.debug:
                house_name = f"{latitude:.6f}_{longitude:.6f}"
                self.current_debug_house_dir = self.debug_dir / house_name
                self.current_debug_house_dir.mkdir(parents=True, exist_ok=True)

            all_image_candidates: list[Candidate] = []

            if image is not None:
                logger.info("Using supplied image instead of fetching from Street View")
                all_image_candidates = self.ocr.extract_door_number(image)
                if all_image_candidates:
                    all_image_candidates = [
                        Candidate(candidate.number, candidate.confidence, heading, pitch)
                        for candidate in all_image_candidates
                    ]
                else:
                    all_image_candidates = [Candidate("", 0, heading, pitch)]
            else:
                if cancel_event is not None and cancel_event.is_set():
                    logger.info(f"Cancelled before fetching ({latitude}, {longitude}) — skipping.")
                    return self._finish_result(
                        self._unsuccessful_result(
                            DetectionOutcome.CANCELLED, latitude, longitude, (0, 0)
                        )
                    )

                camera = self.street_view.resolve_camera(latitude, longitude)
                _base_heading = heading
                road_heading = camera.road_heading
                if _base_heading is None:
                    _base_heading = StreetViewFetcher.heading_to_target(camera, latitude, longitude)
                    logger.info(
                        "Heading camera->target: %.1f° (road heading from API: %s)",
                        _base_heading,
                        road_heading,
                    )

                if road_heading is None:
                    logger.warning("road_heading unavailable — using camera-to-target heading.")

                if self.detection_strategy == "guided":
                    if self.guided_search is None:
                        raise RuntimeError("Guided strategy requires a configured local planner")
                    initial_alternative_heading = None
                    target = type(camera)(latitude, longitude, None, True)
                    camera_distance = StreetViewFetcher.distance_m(camera, target)
                    if camera_distance <= getattr(self.config, "guided_near_camera_meters", 2.0):
                        road_axis = camera.road_heading
                        if road_axis is not None and heading is None:
                            _base_heading = road_axis
                        initial_alternative_heading = (_base_heading + 180) % 360
                    guided_result = self.guided_search.run(
                        camera,
                        initial_heading=_base_heading,
                        initial_pitch=pitch if pitch is not None else 0,
                        initial_alternative_heading=initial_alternative_heading,
                        cancel_event=cancel_event,
                    )
                    logger.info(
                        "Guided search stopped: reason=%s images=%s model_calls=%s panorama_moves=%s",
                        guided_result.stop_reason,
                        guided_result.image_calls,
                        guided_result.model_calls,
                        guided_result.panorama_moves,
                    )
                    if guided_result.stop_reason in {
                        "image_error",
                        "ocr_error",
                        "planner_error",
                        "invalid_action",
                        "no_building_not_authorized",
                    }:
                        raise RuntimeError(f"Guided search failed: {guided_result.stop_reason}")
                    all_image_candidates = list(guided_result.candidates)
                    if (
                        guided_result.stop_reason == "image_unavailable"
                        and not all_image_candidates
                    ):
                        all_image_candidates = []
                    elif not all_image_candidates:
                        all_image_candidates = [Candidate("", 0)]
                else:
                    # Pass 1 — direct heading, normal offsets (±30°)
                    logger.info(f"Pass 1: direct heading {_base_heading:.1f}° (normal sweep ±30°)")
                    all_image_candidates = self.sweep.fetch_candidates(
                        camera,
                        _base_heading,
                        pass_label="direct",
                        pitch_override=pitch,
                        cancel_event=cancel_event,
                        debug_directory=self.current_debug_house_dir or self.debug_dir,
                        save_debug_image=self._save_debug_image,
                    )

            if cancel_event is not None and cancel_event.is_set():
                return self._finish_result(
                    self._unsuccessful_result(
                        DetectionOutcome.CANCELLED,
                        latitude,
                        longitude,
                        self._usage_since(usage_before, track_usage),
                    )
                )

            if not all_image_candidates:
                logger.warning(f"Unable to retrieve any image for {latitude}, {longitude}")
                return self._finish_result(
                    self._unsuccessful_result(
                        DetectionOutcome.IMAGE_UNAVAILABLE,
                        latitude,
                        longitude,
                        self._usage_since(usage_before, track_usage),
                    )
                )

            decision = self.candidate_selector.select(all_image_candidates)
            if decision is None:
                raise RuntimeError("Candidate selection returned no result for fetched images")

            door_number = decision.candidate.number
            confidence = decision.candidate.confidence
            heading = decision.candidate.heading_offset
            pitch = decision.candidate.pitch

            logger.info(
                f"Best across all passes: '{door_number}' ({confidence}%) "
                f"at heading_offset={heading} pitch={pitch}"
            )

            success = decision.accepted
            error = None
            if not success:
                error = (
                    f"Low confidence ({confidence}%). "
                    f"Requires at least {self.config.confidence_threshold}% to pass."
                )
            outcome = DetectionOutcome.FOUND if success else DetectionOutcome.LOW_CONFIDENCE
            details = OUTCOME_DETAILS[outcome]
            image_calls, metadata_calls = self._usage_since(usage_before, track_usage)
            result = DetectionResult(
                success=success,
                outcome=outcome,
                latitude=latitude,
                longitude=longitude,
                door_number=door_number if success else None,
                confidence=confidence,
                heading=heading,
                pitch=pitch,
                observation=details.observation,
                error=error,
                image_calls=image_calls,
                metadata_calls=metadata_calls,
            )

            if not success:
                logger.warning(
                    f"Low confidence result: '{door_number}' ({confidence}%). Marked as failed."
                )
            else:
                logger.info(f"Result: {door_number} (confidence: {confidence}%)")

            return self._finish_result(result)

        except NoImageryAvailable:
            if cancel_event is not None and cancel_event.is_set():
                logger.info(f"Cancelled while resolving imagery for ({latitude}, {longitude}).")
                return self._finish_result(
                    self._unsuccessful_result(
                        DetectionOutcome.CANCELLED,
                        latitude,
                        longitude,
                        self._usage_since(usage_before, track_usage),
                    )
                )
            logger.warning(
                f"No Street View imagery available for ({latitude}, {longitude}) — "
                f"skipping OCR and recording observation."
            )
            return self._finish_result(
                self._unsuccessful_result(
                    DetectionOutcome.NO_IMAGERY,
                    latitude,
                    longitude,
                    self._usage_since(usage_before, track_usage),
                )
            )

        except Exception as exc:
            if cancel_event is not None and cancel_event.is_set():
                logger.info(f"Cancelled while processing ({latitude}, {longitude}).")
                return self._finish_result(
                    self._unsuccessful_result(
                        DetectionOutcome.CANCELLED,
                        latitude,
                        longitude,
                        self._usage_since(usage_before, track_usage),
                    )
                )
            message = redact_api_key(str(exc))
            logger.error(f"Error processing coordinate: {message}", exc_info=True)
            return self._finish_result(
                self._unsuccessful_result(
                    DetectionOutcome.FAILED,
                    latitude,
                    longitude,
                    self._usage_since(usage_before, track_usage),
                )
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
        try:
            if self._local_vlm_planner is not None:
                self._local_vlm_planner.close()
        finally:
            self.db.close()
        logger.info("Door Number Detector finished")
