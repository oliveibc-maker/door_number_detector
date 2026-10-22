"""Street View image sweep orchestration."""

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from core.google_street_view import NoImageryAvailable


DEFAULT_FOV_SWEEP: list[tuple[int, list[int], list[int]]] = [
    (10, [0, -5, 5, -10, 10, -15, 15, -20, 20, -25, 25, -30, 30], [0, -5, 5, -10, 10, -15, 15]),
]


class StreetViewSweep:
    def __init__(
        self,
        street_view,
        ocr_service,
        confidence_check: Callable[[list[dict]], bool],
        *,
        debug: bool = False,
        logger: logging.Logger | None = None,
    ):
        self.street_view = street_view
        self.ocr_service = ocr_service
        self.confidence_check = confidence_check
        self.debug = debug
        self.logger = logger or logging.getLogger(__name__)

    def fetch_candidates(
        self,
        latitude: float,
        longitude: float,
        heading,
        pass_label: str = "center",
        pitch_override: int | None = None,
        sweep: list | None = None,
        cancel_event: threading.Event | None = None,
        debug_directory: Path | None = None,
        save_debug_image: Callable | None = None,
    ) -> list[dict]:
        fov_sweep = sweep if sweep is not None else DEFAULT_FOV_SWEEP
        candidates: list[dict] = []
        done = False
        no_imagery = False

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
                    try:
                        image = self.street_view.get_image(
                            latitude,
                            longitude,
                            heading=heading,
                            heading_offset=offset,
                            pitch=pitch,
                            fov=fov,
                        )
                    except NoImageryAvailable:
                        self.logger.warning(
                            "[%s] No Street View imagery for (%s, %s) — aborting sweep.",
                            pass_label,
                            latitude,
                            longitude,
                        )
                        no_imagery = True
                        done = True
                        break

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

                    number, confidence = self.ocr_service.extract_door_number(image)
                    self.logger.info(
                        "[%s] FOV=%s offset=%s pitch=%s -> '%s' (confidence: %s%%)",
                        pass_label,
                        fov,
                        offset,
                        pitch,
                        number,
                        confidence,
                    )
                    candidates.append({
                        "number": number,
                        "confidence": confidence,
                        "heading": offset,
                        "pitch": pitch,
                        "fov": fov,
                    })

                    if self.confidence_check(candidates):
                        self.logger.info(
                            "[%s] Confident result at FOV=%s offset=%s pitch=%s — stopping sweep.",
                            pass_label,
                            fov,
                            offset,
                            pitch,
                        )
                        done = True
                        break

        if no_imagery:
            raise NoImageryAvailable(
                f"No Street View imagery available for ({latitude}, {longitude})"
            )
        return candidates