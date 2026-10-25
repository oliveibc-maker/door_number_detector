"""Street View image sweep orchestration."""

import logging
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from door_number_detector.core.candidate_selection import Candidate, CandidateSelector
from door_number_detector.core.google_street_view import CameraPosition

DEFAULT_FOV_SWEEP: list[tuple[int, list[int], list[int]]] = [
    (10, [0, -5, 5, -10, 10, -15, 15, -20, 20, -25, 25, -30, 30], [0, -5, 5, -10, 10, -15, 15]),
]


class StreetViewSweep:
    def __init__(
        self,
        street_view,
        ocr_service,
        candidate_selector: CandidateSelector,
        *,
        debug: bool = False,
        logger: logging.Logger | None = None,
    ):
        self.street_view = street_view
        self.ocr_service = ocr_service
        self.candidate_selector = candidate_selector
        self.debug = debug
        self.logger = logger or logging.getLogger(__name__)

    def fetch_candidates(
        self,
        latitude: float,
        longitude: float,
        camera: CameraPosition,
        heading,
        pass_label: str = "center",
        pitch_override: int | None = None,
        sweep: list | None = None,
        cancel_event: threading.Event | None = None,
        debug_directory: Path | None = None,
        save_debug_image: Callable | None = None,
    ) -> list[Candidate]:
        fov_sweep = sweep if sweep is not None else DEFAULT_FOV_SWEEP
        candidates: list[Candidate] = []
        done = False

        for fov, heading_offsets, pitch_values in fov_sweep:
            if done:
                break
            if cancel_event is not None and cancel_event.is_set():
                self.logger.info("[%s] Cancelled — aborting FOV sweep.", pass_label)
                break

            pitches = [pitch_override] if pitch_override is not None else pitch_values
            for pitch in pitches:
                if done:
                    break
                if cancel_event is not None and cancel_event.is_set():
                    self.logger.info("[%s] Cancelled — aborting pitch loop.", pass_label)
                    done = True
                    break

                for offset in heading_offsets:
                    if cancel_event is not None and cancel_event.is_set():
                        self.logger.info(
                            "[%s] Cancelled at offset=%s — aborting heading sweep.",
                            pass_label,
                            offset,
                        )
                        done = True
                        break

                    self.logger.info(
                        "[%s] Trying FOV=%s offset=%s pitch=%s",
                        pass_label,
                        fov,
                        offset,
                        pitch,
                    )
                    image = self.street_view.get_image(
                        camera,
                        heading=(heading + offset) % 360,
                        pitch=pitch,
                        fov=fov,
                    )
                    if image is None:
                        self.logger.warning(
                            "[%s] No image returned for offset=%s pitch=%s",
                            pass_label,
                            offset,
                            pitch,
                        )
                        continue

                    if self.debug and debug_directory is not None and save_debug_image is not None:
                        pass_directory = Path(debug_directory) / pass_label
                        pass_directory.mkdir(parents=True, exist_ok=True)
                        image_name = f"fov{fov}_offset{offset}_pitch{pitch}.png"
                        save_debug_image(image, image_name, directory=pass_directory)

                    image_candidates = self.ocr_service.extract_door_number(image)
                    if image_candidates:
                        candidates.extend(
                            replace(
                                candidate,
                                heading_offset=offset,
                                pitch=pitch,
                                fov=fov,
                            )
                            for candidate in image_candidates
                        )
                    else:
                        candidates.append(Candidate("", 0, offset, pitch, fov))
                    for candidate in image_candidates:
                        self.logger.info(
                            "[%s] FOV=%s offset=%s pitch=%s -> '%s' (confidence: %s%%)",
                            pass_label,
                            fov,
                            offset,
                            pitch,
                            candidate.number,
                            candidate.confidence,
                        )

                    if self.candidate_selector.should_stop(candidates):
                        self.logger.info(
                            "[%s] Confident result at FOV=%s offset=%s pitch=%s — stopping sweep.",
                            pass_label,
                            fov,
                            offset,
                            pitch,
                        )
                        done = True
                        break

        return candidates
