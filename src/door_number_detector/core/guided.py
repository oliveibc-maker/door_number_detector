"""Validated planner actions for guided Street View search."""

import asyncio
import base64
import json
import math
import re
from collections.abc import Mapping, Set
from dataclasses import dataclass
from io import BytesIO
from ipaddress import ip_address
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from PIL import Image

from door_number_detector.core.candidate_selection import Candidate, CandidateSelector
from door_number_detector.core.google_street_view import CameraPosition


@dataclass(frozen=True)
class GuidedAction:
    kind: str
    box: tuple[float, float, float, float] | None = None
    fov: int | None = None
    heading: float | None = None
    pitch: float | None = None
    location_id: str | None = None
    panorama_id: str | None = None
    observation_ids: tuple[str, ...] = ()
    reason: str | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class GuidedView:
    heading: float
    pitch: float
    fov: int


@dataclass(frozen=True)
class OCRObservation:
    identifier: str
    candidate: Candidate | None = None
    text: str = ""
    confidence: float | None = None
    box: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class SearchLimits:
    max_images: int = 12
    max_model_calls: int = 16
    max_panorama_moves: int = 2
    max_metadata_discoveries: int = 8

    def __post_init__(self) -> None:
        for name in (
            "max_images",
            "max_model_calls",
            "max_panorama_moves",
            "max_metadata_discoveries",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class GuidedSearchResult:
    candidates: tuple[Candidate, ...]
    stop_reason: str
    image_calls: int
    model_calls: int
    panorama_moves: int
    metadata_discoveries: int


class GuidedPlanner(Protocol):
    def plan(
        self,
        *,
        image: Image.Image,
        view: GuidedView,
        observations: tuple[OCRObservation, ...],
        history: tuple[dict[str, object], ...],
        available_panoramas: tuple[dict[str, object], ...] = (),
        cancel_event=None,
    ) -> object: ...


class LocalHTTPPlanner:
    """Call an OpenAI-compatible vision endpoint restricted to the local machine."""

    _MAX_IMAGE_BYTES = 4 * 1024 * 1024
    _MAX_RESPONSE_CHARS = 8192
    _MAX_RESPONSE_BYTES = 64 * 1024

    def __init__(
        self,
        chat_url: str,
        model: str,
        *,
        timeout: float = 300,
        client_factory=None,
    ):
        parsed = urlsplit(chat_url)
        try:
            hostname = parsed.hostname or ""
            is_loopback = hostname.lower() == "localhost" or ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = False
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("local model endpoint has an invalid port") from exc
        if (
            parsed.scheme != "http"
            or not is_loopback
            or not parsed.path
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("local model endpoint must be an HTTP loopback URL")
        if not isinstance(model, str) or not model.strip() or len(model) > 160:
            raise ValueError(
                "local model name must be a non-empty string of at most 160 characters"
            )
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("local model timeout must be a positive number")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("local model timeout must be a positive number")
        self.chat_url = chat_url
        self.model = model.strip()
        self.timeout = float(timeout)
        self.client_factory = client_factory or httpx.AsyncClient
        self.closed = False

    def plan(
        self,
        *,
        image: Image.Image,
        view: GuidedView,
        observations: tuple[OCRObservation, ...],
        history: tuple[dict[str, object], ...],
        available_panoramas: tuple[dict[str, object], ...] = (),
        cancel_event=None,
    ) -> object:
        if self.closed:
            raise RuntimeError("local model planner is closed")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self._plan_async(
                    image=image,
                    view=view,
                    observations=observations,
                    history=history,
                    available_panoramas=available_panoramas,
                    cancel_event=cancel_event,
                )
            )
        raise RuntimeError("synchronous planner cannot run inside an active asyncio event loop")

    async def _plan_async(
        self,
        *,
        image: Image.Image,
        view: GuidedView,
        observations: tuple[OCRObservation, ...],
        history: tuple[dict[str, object], ...],
        available_panoramas: tuple[dict[str, object], ...],
        cancel_event=None,
    ) -> dict[str, object]:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("guided search cancelled")
        image_buffer = BytesIO()
        image.convert("RGB").save(image_buffer, format="JPEG", quality=85)
        image_bytes = image_buffer.getvalue()
        if len(image_bytes) > self._MAX_IMAGE_BYTES:
            raise ValueError("planner image exceeds the configured size limit")
        image_data = base64.b64encode(image_bytes).decode("ascii")
        context = {
            "view": {"heading": view.heading, "pitch": view.pitch, "fov": view.fov},
            "ocr_observations": [
                {
                    "id": observation.identifier,
                    "text": observation.text,
                    "candidate": (
                        observation.candidate.number if observation.candidate is not None else None
                    ),
                    "confidence": observation.confidence,
                    "box": observation.box,
                }
                for observation in observations
            ],
            "history": history,
            "available_panoramas": available_panoramas,
        }
        request: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 256,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Inspect the current Street View image for a door-number search. "
                        "Return exactly one JSON action. Use OCR observation IDs for candidates; "
                        "never invent a number. Allowed action shapes: inspect_region with box "
                        "and fov; try_location with box and fov; adjust_view with heading, pitch, "
                        "and fov; candidate with "
                        "observation_ids; move_panorama with an available panorama_id; stop "
                        "with reason search_complete. Do not output coordinates, tools, or prose."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": json.dumps(context, separators=(",", ":"))},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_data}"},
                        },
                    ],
                },
            ],
        }
        request_task = asyncio.create_task(self._send_request(request))
        if cancel_event is None:
            return await request_task
        cancel_task = asyncio.create_task(self._wait_for_cancel(cancel_event))
        try:
            done, _ = await asyncio.wait(
                {request_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_task in done:
                request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)
                raise RuntimeError("guided search cancelled")
            return request_task.result()
        finally:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)

    async def _send_request(self, request: dict[str, Any]) -> dict[str, object]:
        async with self.client_factory(
            trust_env=False,
            follow_redirects=False,
            timeout=self.timeout,
        ) as client:
            async with client.stream("POST", self.chat_url, json=request) as response:
                if 300 <= response.status_code < 400:
                    raise ValueError("local model endpoint redirects are not allowed")
                response.raise_for_status()
                response_chunks = []
                response_bytes = 0
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    if not chunk:
                        continue
                    response_bytes += len(chunk)
                    if response_bytes > self._MAX_RESPONSE_BYTES:
                        raise ValueError("local model response exceeds the configured size limit")
                    response_chunks.append(chunk)
                try:
                    body = json.loads(b"".join(response_chunks))
                except json.JSONDecodeError as exc:
                    raise ValueError("local model response is not JSON") from exc
                try:
                    content = body["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as exc:
                    raise ValueError("local model response has an invalid shape") from exc
                if not isinstance(content, str) or len(content) > self._MAX_RESPONSE_CHARS:
                    raise ValueError("local model response is invalid or too large")
                try:
                    action = json.loads(content)
                except json.JSONDecodeError as exc:
                    raise ValueError("local model action is not JSON") from exc
                if not isinstance(action, dict):
                    raise ValueError("local model action must be an object")
                return action

    @staticmethod
    async def _wait_for_cancel(cancel_event) -> None:
        while not cancel_event.is_set():
            await asyncio.sleep(0.05)

    def close(self) -> None:
        self.closed = True


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _fov(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 10 <= value <= 120:
        raise ValueError("fov must be an integer from 10 through 120")
    return value


def _normalized_box(value: object) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("box must contain normalized x, y, width, and height")
    x, y, width, height = (_number(part, "box coordinate") for part in value)
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
        raise ValueError("box must be a non-empty region within the image")
    return x, y, width, height


def _fields(action: Mapping, expected: set[str]) -> None:
    if set(action) != expected:
        raise ValueError("action fields do not match its type")


def parse_action(
    value: object,
    *,
    allowed_location_ids: Set[str],
    allowed_panorama_ids: Set[str],
    allowed_observation_ids: Set[str],
) -> GuidedAction:
    """Parse one closed planner action and bind references to observed evidence."""
    if not isinstance(value, Mapping):
        raise ValueError("action must be an object")
    kind = value.get("type")

    if kind == "inspect_region":
        _fields(value, {"type", "box", "fov"})
        return GuidedAction(kind, box=_normalized_box(value["box"]), fov=_fov(value["fov"]))

    if kind == "adjust_view":
        _fields(value, {"type", "heading", "pitch", "fov"})
        heading = _number(value["heading"], "heading")
        pitch = _number(value["pitch"], "pitch")
        if not 0 <= heading < 360:
            raise ValueError("heading must be in [0, 360)")
        if not -90 <= pitch <= 90:
            raise ValueError("pitch must be in [-90, 90]")
        return GuidedAction(
            kind,
            fov=_fov(value["fov"]),
            heading=heading,
            pitch=pitch,
        )

    if kind == "try_location":
        _fields(value, {"type", "box", "fov"})
        return GuidedAction(kind, box=_normalized_box(value["box"]), fov=_fov(value["fov"]))

    if kind == "move_panorama":
        _fields(value, {"type", "panorama_id"})
        panorama_id = _identifier(value["panorama_id"], "panorama_id")
        if panorama_id not in allowed_panorama_ids:
            raise ValueError("panorama_id is not an allowed panorama")
        return GuidedAction(kind, panorama_id=panorama_id)

    if kind == "candidate":
        _fields(value, {"type", "observation_ids"})
        observation_ids = value["observation_ids"]
        if (
            not isinstance(observation_ids, (list, tuple))
            or not observation_ids
            or len(observation_ids) > 8
        ):
            raise ValueError("observation_ids must contain between 1 and 8 identifiers")
        identifiers = tuple(_identifier(item, "observation_id") for item in observation_ids)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("observation_ids must be unique")
        if not set(identifiers).issubset(allowed_observation_ids):
            raise ValueError("candidate must reference observed OCR evidence")
        return GuidedAction(kind, observation_ids=identifiers)

    if kind == "stop":
        reason = value.get("reason")
        if reason == "no_building":
            _fields(value, {"type", "reason", "confidence"})
            confidence = _number(value["confidence"], "confidence")
            if not 0 <= confidence <= 1:
                raise ValueError("confidence must be in [0, 1]")
            return GuidedAction(kind, reason=reason, confidence=confidence)
        if reason == "search_complete":
            _fields(value, {"type", "reason"})
            return GuidedAction(kind, reason=reason)
        raise ValueError("stop reason is not supported")

    raise ValueError("action type is not supported")


class GuidedSearch:
    """Run a bounded image-plan loop without exposing Street View tools to the planner."""

    _HISTORY_LIMIT = 8

    def __init__(
        self,
        street_view,
        ocr_service,
        planner: GuidedPlanner,
        candidate_selector: CandidateSelector,
        *,
        limits: SearchLimits | None = None,
        no_building_gate=None,
        no_building_confidence_threshold: float = 0.98,
    ):
        self.street_view = street_view
        self.ocr_service = ocr_service
        self.planner = planner
        self.candidate_selector = candidate_selector
        self.limits = limits or SearchLimits()
        if not math.isfinite(no_building_confidence_threshold) or not (
            0 <= no_building_confidence_threshold <= 1
        ):
            raise ValueError("no_building_confidence_threshold must be in [0, 1]")
        self.no_building_gate = no_building_gate
        self.no_building_confidence_threshold = no_building_confidence_threshold

    def run(
        self,
        camera: CameraPosition,
        *,
        initial_heading: float,
        initial_pitch: float = 0,
        initial_alternative_heading: float | None = None,
        cancel_event=None,
    ) -> GuidedSearchResult:
        heading = _number(initial_heading, "initial_heading") % 360
        pitch = _number(initial_pitch, "initial_pitch")
        if not -90 <= pitch <= 90:
            raise ValueError("initial_pitch must be in [-90, 90]")
        current_view = GuidedView(heading, pitch, 90)
        alternative_view = None
        if initial_alternative_heading is not None:
            alternative_heading = _number(
                initial_alternative_heading, "initial_alternative_heading"
            )
            if not 0 <= alternative_heading < 360:
                raise ValueError("initial_alternative_heading must be in [0, 360)")
            alternative_view = GuidedView(alternative_heading, pitch, 90)
        next_view: GuidedView | None = current_view
        tried_views: set[tuple[object, ...]] = set()
        current_camera = camera
        visited_panorama_ids = {camera.pano_id} if camera.pano_id is not None else set()
        discovered_from: set[str] = set()
        panorama_catalog = {}
        all_candidates: list[Candidate] = []
        candidate_keys: set[tuple[str, float, float, int]] = set()
        history: list[dict[str, object]] = []
        image_calls = 0
        model_calls = 0
        panorama_moves = 0
        metadata_discoveries = 0
        current_image: Image.Image | None = None
        current_observations: tuple[OCRObservation, ...] = ()

        def finish(reason: str) -> GuidedSearchResult:
            return GuidedSearchResult(
                tuple(all_candidates),
                reason,
                image_calls,
                model_calls,
                panorama_moves,
                metadata_discoveries,
            )

        while True:
            if cancel_event is not None and cancel_event.is_set():
                return finish("cancelled")

            if next_view is not None:
                if image_calls >= self.limits.max_images:
                    return finish("image_budget_exhausted")
                view_key = self._view_key(current_camera, next_view)
                if view_key in tried_views:
                    return finish("duplicate_view")
                tried_views.add(view_key)
                current_view = next_view
                next_view = None
                image_calls += 1
                try:
                    current_image = self.street_view.get_image(
                        current_camera,
                        heading=current_view.heading,
                        pitch=current_view.pitch,
                        fov=current_view.fov,
                    )
                except Exception:
                    return finish("image_error")
                if current_image is None:
                    return finish("image_unavailable")
                try:
                    if hasattr(self.ocr_service, "extract_observations"):
                        readings = self.ocr_service.extract_observations(current_image)
                        current_observations = self._observations_from_readings(
                            image_calls, readings
                        )
                    else:
                        ocr_candidates = self.ocr_service.extract_door_number(current_image)
                        current_observations = tuple(
                            OCRObservation(
                                f"image-{image_calls}-ocr-{index}",
                                candidate,
                                candidate.number,
                                candidate.confidence / 100,
                            )
                            for index, candidate in enumerate(ocr_candidates, start=1)
                        )
                except Exception:
                    return finish("ocr_error")

            if (
                current_camera.pano_id is not None
                and current_camera.pano_id not in discovered_from
                and metadata_discoveries < self.limits.max_metadata_discoveries
                and hasattr(self.street_view, "find_neighbor_cameras")
            ):
                discovered_from.add(current_camera.pano_id)
                remaining = self.limits.max_metadata_discoveries - metadata_discoveries
                before_metadata = self.street_view.get_call_counts()[1]
                try:
                    neighbors = self.street_view.find_neighbor_cameras(
                        current_camera,
                        heading=current_view.heading,
                        max_probes=min(4, remaining),
                    )
                except Exception:
                    neighbors = []
                after_metadata = self.street_view.get_call_counts()[1]
                metadata_discoveries += min(remaining, max(0, after_metadata - before_metadata))
                for neighbor in neighbors:
                    panorama_id = neighbor.camera.pano_id
                    if panorama_id is not None and panorama_id not in visited_panorama_ids:
                        panorama_catalog[panorama_id] = neighbor

            if current_image is None:
                return finish("image_unavailable")
            if cancel_event is not None and cancel_event.is_set():
                return finish("cancelled")
            if model_calls >= self.limits.max_model_calls:
                return finish("model_budget_exhausted")

            try:
                model_calls += 1
                raw_action = self.planner.plan(
                    image=current_image,
                    view=current_view,
                    observations=current_observations,
                    history=tuple(history[-self._HISTORY_LIMIT :]),
                    available_panoramas=tuple(
                        {
                            "id": panorama_id,
                            "distance_m": round(neighbor.distance_m, 1),
                            "relative_bearing": round(neighbor.relative_bearing, 1),
                        }
                        for panorama_id, neighbor in panorama_catalog.items()
                        if panorama_id not in visited_panorama_ids
                    ),
                    cancel_event=cancel_event,
                )
            except Exception:
                return finish("planner_error")
            try:
                action = parse_action(
                    raw_action,
                    allowed_location_ids=set(),
                    allowed_panorama_ids=set(panorama_catalog).difference(visited_panorama_ids),
                    allowed_observation_ids={
                        observation.identifier for observation in current_observations
                    },
                )
            except Exception:
                return finish("invalid_action")

            if cancel_event is not None and cancel_event.is_set():
                return finish("cancelled")

            if action.kind == "inspect_region":
                assert action.box is not None and action.fov is not None
                if action.fov >= current_view.fov:
                    return finish("invalid_action")
                alternative_view = None
                next_view = self._region_view(current_view, current_image, action)
            elif action.kind == "try_location":
                assert action.fov is not None
                if action.fov > current_view.fov:
                    return finish("invalid_action")
                alternative_view = None
                next_view = self._region_view(current_view, current_image, action)
            elif action.kind == "adjust_view":
                assert action.heading is not None and action.pitch is not None
                assert action.fov is not None
                alternative_view = None
                next_view = GuidedView(action.heading, action.pitch, action.fov)
            elif action.kind == "candidate":
                heading_offset = self._heading_offset(current_view.heading, heading)
                selected_candidates = self._selected_candidates(
                    current_observations, action.observation_ids
                )
                for candidate in selected_candidates:
                    key = (candidate.number, heading_offset, current_view.pitch, current_view.fov)
                    if key not in candidate_keys:
                        candidate_keys.add(key)
                        all_candidates.append(
                            Candidate(
                                candidate.number,
                                candidate.confidence,
                                heading_offset,
                                round(current_view.pitch),
                                current_view.fov,
                            )
                        )
                decision = self.candidate_selector.select(all_candidates)
                if (
                    decision is not None
                    and decision.accepted
                    and self.candidate_selector.should_stop([decision.candidate])
                ):
                    return finish("candidate_found")
                if alternative_view is not None:
                    next_view = alternative_view
                    alternative_view = None
                    history.append(self._history_entry(action, current_view))
                    continue
            elif action.kind == "move_panorama":
                if panorama_moves >= self.limits.max_panorama_moves:
                    return finish("panorama_budget_exhausted")
                assert action.panorama_id is not None
                neighbor = panorama_catalog.get(action.panorama_id)
                if neighbor is None or action.panorama_id in visited_panorama_ids:
                    return finish("invalid_action")
                current_camera = neighbor.camera
                visited_panorama_ids.add(action.panorama_id)
                panorama_moves += 1
                alternative_view = None
                next_view = GuidedView(current_view.heading, 0, 90)
            elif action.kind == "stop":
                if action.reason == "search_complete":
                    if alternative_view is not None:
                        next_view = alternative_view
                        alternative_view = None
                        history.append(self._history_entry(action, current_view))
                        continue
                    return finish("search_complete")
                if (
                    action.reason != "no_building"
                    or action.confidence is None
                    or action.confidence < self.no_building_confidence_threshold
                    or image_calls > 2
                    or self.no_building_gate is None
                    or not self.no_building_gate(action, current_observations, tuple(history))
                ):
                    return finish("no_building_not_authorized")
                return finish("no_building")
            else:
                return finish("invalid_action")

            history.append(self._history_entry(action, current_view))

    @staticmethod
    def _view_key(camera: CameraPosition, view: GuidedView) -> tuple[object, ...]:
        camera_key = (
            ("panorama", camera.pano_id)
            if camera.pano_id is not None
            else ("location", round(camera.latitude, 6), round(camera.longitude, 6))
        )
        return (*camera_key, round(view.heading, 1), round(view.pitch, 1), view.fov)

    @staticmethod
    def _heading_offset(heading: float, initial_heading: float) -> float:
        return (heading - initial_heading + 180) % 360 - 180

    @staticmethod
    def _region_view(view: GuidedView, image: Image.Image, action: GuidedAction) -> GuidedView:
        assert action.box is not None and action.fov is not None
        width, height = image.size
        if width <= 0 or height <= 0:
            raise ValueError("image dimensions must be positive")
        x, y, box_width, box_height = action.box
        center_x = x + box_width / 2
        center_y = y + box_height / 2
        horizontal_half_fov = math.radians(view.fov / 2)
        vertical_half_fov = math.atan(math.tan(horizontal_half_fov) * height / width)
        horizontal_offset = math.degrees(
            math.atan((2 * center_x - 1) * math.tan(horizontal_half_fov))
        )
        vertical_offset = math.degrees(math.atan((1 - 2 * center_y) * math.tan(vertical_half_fov)))
        return GuidedView(
            (view.heading + horizontal_offset) % 360,
            max(-90, min(90, view.pitch + vertical_offset)),
            action.fov,
        )

    @staticmethod
    def _history_entry(action: GuidedAction, view: GuidedView) -> dict[str, object]:
        entry: dict[str, object] = {
            "action": action.kind,
            "heading": round(view.heading, 2),
            "pitch": round(view.pitch, 2),
            "fov": view.fov,
        }
        if action.kind == "candidate":
            entry["observation_ids"] = list(action.observation_ids)
        elif action.kind == "move_panorama":
            entry["panorama_id"] = action.panorama_id or ""
        elif action.kind == "stop":
            entry["reason"] = action.reason or ""
        return entry

    @staticmethod
    def _observations_from_readings(image_number: int, readings) -> tuple[OCRObservation, ...]:
        observations = []
        for line_number, reading in enumerate(readings, start=1):
            if reading.number_candidates:
                for candidate_number, number in enumerate(reading.number_candidates, start=1):
                    candidate_suffix = (
                        f"-{candidate_number}" if len(reading.number_candidates) > 1 else ""
                    )
                    observations.append(
                        OCRObservation(
                            f"image-{image_number}-ocr-{line_number}{candidate_suffix}",
                            Candidate(number, round(reading.confidence * 100)),
                            reading.text,
                            reading.confidence,
                            reading.box,
                        )
                    )
            else:
                observations.append(
                    OCRObservation(
                        f"image-{image_number}-ocr-{line_number}",
                        text=reading.text,
                        confidence=reading.confidence,
                        box=reading.box,
                    )
                )
        return tuple(observations)

    @classmethod
    def _selected_candidates(
        cls, observations: tuple[OCRObservation, ...], identifiers: tuple[str, ...]
    ) -> tuple[Candidate, ...]:
        selected_by_id = {observation.identifier: observation for observation in observations}
        selected = [selected_by_id[identifier] for identifier in identifiers]
        candidates = [
            observation.candidate for observation in selected if observation.candidate is not None
        ]
        for number_observation in selected:
            number_candidate = number_observation.candidate
            if (
                number_candidate is None
                or not number_candidate.number.isdigit()
                or not 1 <= len(number_candidate.number) <= 4
                or number_observation.box is None
            ):
                continue
            for letter_observation in selected:
                if (
                    letter_observation.candidate is not None
                    or letter_observation.box is None
                    or re.fullmatch(r"[A-Za-z]", letter_observation.text.strip()) is None
                    or not cls._suffix_is_adjacent(number_observation.box, letter_observation.box)
                ):
                    continue
                letter_confidence = letter_observation.confidence
                if letter_confidence is None:
                    continue
                candidates.append(
                    Candidate(
                        number_candidate.number + letter_observation.text.strip().upper(),
                        min(number_candidate.confidence, round(letter_confidence * 100)),
                        number_candidate.heading_offset,
                        number_candidate.pitch,
                        number_candidate.fov,
                    )
                )
        unique = {}
        for candidate in candidates:
            unique[candidate.number] = candidate
        return tuple(unique.values())

    @staticmethod
    def _suffix_is_adjacent(
        number_box: tuple[float, float, float, float],
        letter_box: tuple[float, float, float, float],
    ) -> bool:
        number_x, number_y, number_width, number_height = number_box
        letter_x, letter_y, letter_width, letter_height = letter_box
        number_right = number_x + number_width
        letter_center_y = letter_y + letter_height / 2
        number_center_y = number_y + number_height / 2
        reference_height = max(number_height, letter_height)
        horizontal_gap = letter_x - number_right
        return (
            letter_x >= number_x
            and -0.5 * reference_height <= horizontal_gap <= max(0.03, 1.5 * reference_height)
            and abs(letter_center_y - number_center_y) <= max(0.02, 0.75 * reference_height)
        )
