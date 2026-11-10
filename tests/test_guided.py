import asyncio
import json
import os
import threading
import unittest
from collections import deque
from contextlib import contextmanager
from unittest.mock import patch

from PIL import Image

from door_number_detector.core.candidate_selection import Candidate, CandidateSelector
from door_number_detector.core.config import Config
from door_number_detector.core.google_street_view import CameraPosition, PanoramaNeighbor
from door_number_detector.core.guided import (
    GuidedSearch,
    GuidedView,
    LocalHTTPPlanner,
    OCRObservation,
    SearchLimits,
    parse_action,
)
from door_number_detector.core.ocr import OCRReading


class FakeStreetView:
    def __init__(self, images=None):
        self.images = deque(images or [Image.new("RGB", (100, 100))])
        self.requests = []
        self.neighbors = []
        self.metadata_discoveries = 0

    def get_image(self, camera, *, heading, pitch, fov):
        self.requests.append((camera, heading, pitch, fov))
        return self.images.popleft() if self.images else Image.new("RGB", (100, 100))

    def find_neighbor_cameras(self, camera, *, heading, max_probes):
        if camera.pano_id is None:
            return []
        probes = min(max_probes, 4)
        self.metadata_discoveries += probes
        return self.neighbors[:probes]

    def get_call_counts(self):
        return len(self.requests), self.metadata_discoveries


class FakeOcr:
    def __init__(self, candidates=None):
        self.candidates = candidates or [Candidate("17", 95)]
        self.images = []

    def extract_door_number(self, image):
        self.images.append(image)
        return list(self.candidates)


class ScriptedPlanner:
    def __init__(self, actions, cancel_event=None):
        self.actions = deque(actions)
        self.cancel_event = cancel_event
        self.calls = []

    def plan(
        self,
        *,
        image,
        view,
        observations,
        history,
        available_panoramas=(),
        cancel_event=None,
    ):
        self.calls.append((image, view, observations, history))
        action = self.actions.popleft()
        if self.cancel_event is not None:
            self.cancel_event.set()
        return action


class FakeHttpResponse:
    def __init__(self, content, status_code=200):
        self.content = content
        self.status_code = status_code
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("request failed")

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        self.closed = True

    async def aiter_bytes(self, chunk_size):
        payload = json.dumps(self.json()).encode("utf-8")
        for offset in range(0, len(payload), chunk_size):
            yield payload[offset : offset + chunk_size]


class FakeHttpSession:
    def __init__(self, response, **options):
        self.response = response
        self.options = options
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return None

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


class GuidedActionTests(unittest.TestCase):
    def test_parses_a_bounded_region_inspection(self):
        action = parse_action(
            {
                "type": "inspect_region",
                "box": [0.1, 0.2, 0.4, 0.3],
                "fov": 35,
            },
            allowed_location_ids=set(),
            allowed_panorama_ids=set(),
            allowed_observation_ids=set(),
        )

        self.assertEqual(action.kind, "inspect_region")
        self.assertEqual(action.box, (0.1, 0.2, 0.4, 0.3))
        self.assertEqual(action.fov, 35)

    def test_rejects_region_outside_image_geometry(self):
        with self.assertRaises(ValueError):
            parse_action(
                {"type": "inspect_region", "box": [0.8, 0.2, 0.4, 0.3], "fov": 35},
                allowed_location_ids=set(),
                allowed_panorama_ids=set(),
                allowed_observation_ids=set(),
            )

    def test_rejects_unknown_fields(self):
        with self.assertRaises(ValueError):
            parse_action(
                {
                    "type": "stop",
                    "reason": "uncertain",
                    "coordinates": [41.0, -8.0],
                },
                allowed_location_ids=set(),
                allowed_panorama_ids=set(),
                allowed_observation_ids=set(),
            )

    def test_panorama_move_must_reference_an_allowed_panorama(self):
        with self.assertRaises(ValueError):
            parse_action(
                {"type": "move_panorama", "panorama_id": "model-invented-id"},
                allowed_location_ids=set(),
                allowed_panorama_ids={"observed-pano"},
                allowed_observation_ids=set(),
            )

    def test_candidate_must_reference_observed_ocr(self):
        action = parse_action(
            {"type": "candidate", "observation_ids": ["number", "letter"]},
            allowed_location_ids=set(),
            allowed_panorama_ids=set(),
            allowed_observation_ids={"number", "letter"},
        )

        self.assertEqual(action.observation_ids, ("number", "letter"))
        with self.assertRaises(ValueError):
            parse_action(
                {"type": "candidate", "number": "20A"},
                allowed_location_ids=set(),
                allowed_panorama_ids=set(),
                allowed_observation_ids={"number", "letter"},
            )

    def test_location_change_must_reference_an_observed_location(self):
        action = parse_action(
            {"type": "try_location", "box": [0.7, 0.2, 0.2, 0.3], "fov": 40},
            allowed_location_ids={"door-1"},
            allowed_panorama_ids=set(),
            allowed_observation_ids=set(),
        )

        self.assertEqual(action.kind, "try_location")
        self.assertEqual(action.box, (0.7, 0.2, 0.2, 0.3))
        with self.assertRaises(ValueError):
            parse_action(
                {"type": "try_location", "box": [0.9, 0.2, 0.2, 0.3], "fov": 40},
                allowed_location_ids={"door-1"},
                allowed_panorama_ids=set(),
                allowed_observation_ids=set(),
            )

    def test_stop_confidence_is_validated_and_reason_is_closed(self):
        action = parse_action(
            {"type": "stop", "reason": "no_building", "confidence": 0.99},
            allowed_location_ids=set(),
            allowed_panorama_ids=set(),
            allowed_observation_ids=set(),
        )

        self.assertEqual(action.reason, "no_building")
        self.assertEqual(action.confidence, 0.99)
        with self.assertRaises(ValueError):
            parse_action(
                {"type": "stop", "reason": "no_building", "confidence": 1.1},
                allowed_location_ids=set(),
                allowed_panorama_ids=set(),
                allowed_observation_ids=set(),
            )
        with self.assertRaises(ValueError):
            parse_action(
                {"type": "stop", "reason": "guess"},
                allowed_location_ids=set(),
                allowed_panorama_ids=set(),
                allowed_observation_ids=set(),
            )


class GuidedSearchTests(unittest.TestCase):
    def _make_search(self, planner, *, limits=None, cancel_event=None):
        street_view = FakeStreetView()
        ocr = FakeOcr()
        search = GuidedSearch(
            street_view,
            ocr,
            planner,
            CandidateSelector(70),
            limits=limits or SearchLimits(),
        )
        result = search.run(
            CameraPosition(41.0, -8.0, None, True),
            initial_heading=90,
            cancel_event=cancel_event,
        )
        return result, street_view, ocr

    def test_adjusted_view_leads_to_ocr_supported_candidate(self):
        planner = ScriptedPlanner(
            [
                {"type": "adjust_view", "heading": 120, "pitch": -5, "fov": 40},
                {"type": "candidate", "observation_ids": ["image-2-ocr-1"]},
            ]
        )

        result, street_view, ocr = self._make_search(planner)

        self.assertEqual(result.stop_reason, "candidate_found")
        self.assertEqual(result.candidates, (Candidate("17", 95, 30, -5, 40),))
        self.assertEqual((result.image_calls, result.model_calls), (2, 2))
        self.assertEqual(
            [request[1:] for request in street_view.requests], [(90, 0, 90), (120, -5, 40)]
        )
        self.assertEqual(len(ocr.images), 2)

    def test_try_location_aims_at_a_validated_other_region(self):
        planner = ScriptedPlanner(
            [
                {"type": "try_location", "box": [0.7, 0.2, 0.2, 0.3], "fov": 40},
                {"type": "candidate", "observation_ids": ["image-2-ocr-1"]},
            ]
        )

        result, street_view, _ = self._make_search(planner)

        self.assertEqual(result.stop_reason, "candidate_found")
        self.assertEqual(len(street_view.requests), 2)
        self.assertEqual(street_view.requests[1][3], 40)
        self.assertNotEqual(street_view.requests[1][2], 0)
        self.assertGreater(street_view.requests[1][1], 90)

    def test_image_budget_stops_without_a_fallback_request(self):
        planner = ScriptedPlanner([{"type": "adjust_view", "heading": 120, "pitch": 0, "fov": 40}])

        result, street_view, _ = self._make_search(
            planner,
            limits=SearchLimits(max_images=1, max_model_calls=4, max_panorama_moves=1),
        )

        self.assertEqual(result.stop_reason, "image_budget_exhausted")
        self.assertEqual((result.image_calls, result.model_calls), (1, 1))
        self.assertEqual(len(street_view.requests), 1)

    def test_cancellation_after_planning_prevents_another_image(self):
        cancel_event = threading.Event()
        planner = ScriptedPlanner(
            [{"type": "adjust_view", "heading": 120, "pitch": 0, "fov": 40}],
            cancel_event=cancel_event,
        )

        result, street_view, _ = self._make_search(planner, cancel_event=cancel_event)

        self.assertEqual(result.stop_reason, "cancelled")
        self.assertEqual((result.image_calls, result.model_calls), (1, 1))
        self.assertEqual(len(street_view.requests), 1)

    def test_invalid_planner_action_stops_without_an_extra_request(self):
        planner = ScriptedPlanner([{"type": "adjust_view", "heading": 720, "pitch": 0, "fov": 40}])

        result, street_view, _ = self._make_search(planner)

        self.assertEqual(result.stop_reason, "invalid_action")
        self.assertEqual((result.image_calls, result.model_calls), (1, 1))
        self.assertEqual(len(street_view.requests), 1)

    def test_duplicate_view_stops_before_requesting_the_same_image(self):
        planner = ScriptedPlanner([{"type": "adjust_view", "heading": 90, "pitch": 0, "fov": 90}])

        result, street_view, _ = self._make_search(planner)

        self.assertEqual(result.stop_reason, "duplicate_view")
        self.assertEqual((result.image_calls, result.model_calls), (1, 1))
        self.assertEqual(len(street_view.requests), 1)

    def test_composes_selected_adjacent_number_and_letter_ocr(self):
        class ReadingOcr:
            def extract_observations(self, image):
                return [
                    OCRReading("20", 0.9, (0.2, 0.2, 0.1, 0.05), ("20",)),
                    OCRReading("A", 0.8, (0.325, 0.2, 0.04, 0.05), ()),
                ]

        street_view = FakeStreetView()
        planner = ScriptedPlanner(
            [
                {
                    "type": "candidate",
                    "observation_ids": ["image-1-ocr-1", "image-1-ocr-2"],
                }
            ]
        )
        search = GuidedSearch(
            street_view, ReadingOcr(), planner, CandidateSelector(70), limits=SearchLimits()
        )

        result = search.run(CameraPosition(41.0, -8.0, None, True), initial_heading=90)

        self.assertEqual(result.stop_reason, "candidate_found")
        self.assertEqual(
            result.candidates,
            (Candidate("20", 90, 0, 0, 90), Candidate("20A", 80, 0, 0, 90)),
        )

    def test_does_not_compose_non_adjacent_letter_evidence(self):
        class ReadingOcr:
            def extract_observations(self, image):
                return [
                    OCRReading("20", 0.9, (0.2, 0.2, 0.1, 0.05), ("20",)),
                    OCRReading("A", 0.8, (0.8, 0.2, 0.04, 0.05), ()),
                ]

        planner = ScriptedPlanner(
            [
                {
                    "type": "candidate",
                    "observation_ids": ["image-1-ocr-1", "image-1-ocr-2"],
                },
                {"type": "stop", "reason": "search_complete"},
            ]
        )
        search = GuidedSearch(
            FakeStreetView(), ReadingOcr(), planner, CandidateSelector(70), limits=SearchLimits()
        )

        result = search.run(CameraPosition(41.0, -8.0, None, True), initial_heading=90)

        self.assertEqual(result.stop_reason, "search_complete")
        self.assertEqual(result.candidates[0].number, "20")
        self.assertEqual(len(result.candidates), 1)

    def test_moves_only_to_an_application_discovered_panorama(self):
        initial_camera = CameraPosition(41.0, -8.0, None, False, "pano-0")
        nearby_camera = CameraPosition(41.0001, -8.0, None, False, "pano-1")
        street_view = FakeStreetView()
        street_view.neighbors = [PanoramaNeighbor(nearby_camera, 11.1, 0)]
        planner = ScriptedPlanner(
            [
                {"type": "move_panorama", "panorama_id": "pano-1"},
                {"type": "candidate", "observation_ids": ["image-2-ocr-1"]},
            ]
        )
        search = GuidedSearch(
            street_view, FakeOcr(), planner, CandidateSelector(70), limits=SearchLimits()
        )

        result = search.run(initial_camera, initial_heading=90)

        self.assertEqual(result.stop_reason, "candidate_found")
        self.assertEqual(result.panorama_moves, 1)
        self.assertEqual(street_view.requests[1][0].pano_id, "pano-1")
        self.assertLessEqual(result.metadata_discoveries, 8)

    def test_uses_one_bounded_alternative_heading_after_an_empty_wide_view(self):
        planner = ScriptedPlanner(
            [
                {"type": "stop", "reason": "search_complete"},
                {"type": "candidate", "observation_ids": ["image-2-ocr-1"]},
            ]
        )
        street_view = FakeStreetView()
        search = GuidedSearch(
            street_view, FakeOcr(), planner, CandidateSelector(70), limits=SearchLimits()
        )

        result = search.run(
            CameraPosition(41.0, -8.0, None, False),
            initial_heading=45,
            initial_alternative_heading=225,
        )

        self.assertEqual(result.stop_reason, "candidate_found")
        self.assertEqual([request[1] for request in street_view.requests], [45, 225])


class GuidedConfigTests(unittest.TestCase):
    @contextmanager
    def _config(self, **environment):
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("door_number_detector.core.config.load_dotenv"),
        ):
            yield Config()

    def test_sweep_is_the_default_strategy(self):
        with self._config() as config:
            self.assertEqual(config.detection_strategy, "sweep")

    def test_guided_strategy_and_budgets_are_configurable(self):
        with self._config(
            DETECTION_STRATEGY="guided",
            GUIDED_MAX_IMAGE_CALLS="8",
            GUIDED_MAX_MODEL_CALLS="10",
            GUIDED_MAX_PANORAMA_MOVES="1",
            GUIDED_MAX_METADATA_DISCOVERIES="6",
            GUIDED_NEAR_CAMERA_METERS="3.5",
            LOCAL_VLM_CHAT_URL="http://127.0.0.1:11434/v1/chat/completions",
            LOCAL_VLM_MODEL="vision-model",
            LOCAL_VLM_TIMEOUT_SECONDS="90",
        ) as config:
            self.assertEqual(config.detection_strategy, "guided")
            self.assertEqual(config.guided_max_images, 8)
            self.assertEqual(config.guided_max_model_calls, 10)
            self.assertEqual(config.guided_max_panorama_moves, 1)
            self.assertEqual(config.guided_max_metadata_discoveries, 6)
            self.assertEqual(config.guided_near_camera_meters, 3.5)
            self.assertEqual(
                config.local_vlm_chat_url,
                "http://127.0.0.1:11434/v1/chat/completions",
            )
            self.assertEqual(config.local_vlm_model, "vision-model")
            self.assertEqual(config.local_vlm_timeout_seconds, 90)

    def test_rejects_unknown_strategy_and_invalid_budgets(self):
        with self._config(DETECTION_STRATEGY="automatic") as config:
            with self.assertRaises(ValueError):
                _ = config.detection_strategy
        for value in ("0", "-1", "not-an-integer"):
            with self.subTest(value=value), self._config(GUIDED_MAX_IMAGE_CALLS=value) as config:
                with self.assertRaises(ValueError):
                    _ = config.guided_max_images


class LocalHTTPPlannerTests(unittest.TestCase):
    def test_posts_only_to_loopback_and_returns_structured_action(self):
        response = FakeHttpResponse('{"type":"stop","reason":"search_complete"}')
        clients = []

        def client_factory(**options):
            client = FakeHttpSession(response, **options)
            clients.append(client)
            return client

        planner = LocalHTTPPlanner(
            "http://127.0.0.1:11434/v1/chat/completions",
            "vision-model",
            client_factory=client_factory,
        )

        action = planner.plan(
            image=Image.new("RGB", (8, 8)),
            view=GuidedView(90, 0, 90),
            observations=(OCRObservation("image-1-ocr-1", Candidate("17", 91)),),
            history=(),
        )

        self.assertEqual(action, {"type": "stop", "reason": "search_complete"})
        session = clients[0]
        self.assertEqual(
            session.options,
            {"trust_env": False, "follow_redirects": False, "timeout": 300.0},
        )
        _, url, request = session.calls[0]
        self.assertEqual(url, "http://127.0.0.1:11434/v1/chat/completions")
        self.assertNotIn("timeout", request)
        self.assertTrue(session.response.closed)
        self.assertEqual(request["json"]["model"], "vision-model")
        user_content = request["json"]["messages"][1]["content"]
        self.assertIn("image-1-ocr-1", user_content[0]["text"])
        self.assertEqual(user_content[1]["type"], "image_url")
        self.assertIn("try_location", request["json"]["messages"][0]["content"])

    def test_rejects_non_loopback_endpoint(self):
        with self.assertRaises(ValueError):
            LocalHTTPPlanner("https://example.com/v1/chat/completions", "model")

    def test_rejects_redirect_responses(self):
        planner = LocalHTTPPlanner(
            "http://localhost:11434/v1/chat/completions",
            "vision-model",
            client_factory=lambda **options: FakeHttpSession(
                FakeHttpResponse("", status_code=302), **options
            ),
        )

        with self.assertRaises(ValueError):
            planner.plan(
                image=Image.new("RGB", (8, 8)),
                view=GuidedView(90, 0, 90),
                observations=(),
                history=(),
            )

    def test_rejects_non_json_model_output(self):
        planner = LocalHTTPPlanner(
            "http://localhost:11434/v1/chat/completions",
            "vision-model",
            client_factory=lambda **options: FakeHttpSession(
                FakeHttpResponse("not json"), **options
            ),
        )

        with self.assertRaises(ValueError):
            planner.plan(
                image=Image.new("RGB", (8, 8)),
                view=GuidedView(90, 0, 90),
                observations=(),
                history=(),
            )

    def test_rejects_oversized_response_body(self):
        planner = LocalHTTPPlanner(
            "http://localhost:11434/v1/chat/completions",
            "vision-model",
            client_factory=lambda **options: FakeHttpSession(
                FakeHttpResponse("x" * (70 * 1024)), **options
            ),
        )

        with self.assertRaises(ValueError):
            planner.plan(
                image=Image.new("RGB", (8, 8)),
                view=GuidedView(90, 0, 90),
                observations=(),
                history=(),
            )

    def test_cancellation_interrupts_the_local_http_wait_and_closes_response(self):
        entered_stream = threading.Event()
        response_closed = threading.Event()

        class BlockingResponse(FakeHttpResponse):
            async def __aexit__(self, _exc_type, _exc, _traceback):
                self.closed = True
                response_closed.set()

            async def aiter_bytes(self, chunk_size):
                entered_stream.set()
                await asyncio.Event().wait()
                yield b""

        response = BlockingResponse("")
        client = FakeHttpSession(response)
        planner = LocalHTTPPlanner(
            "http://localhost:11434/v1/chat/completions",
            "vision-model",
            client_factory=lambda **options: client,
        )
        cancel_event = threading.Event()
        errors = []

        def call_planner():
            try:
                planner.plan(
                    image=Image.new("RGB", (8, 8)),
                    view=GuidedView(90, 0, 90),
                    observations=(),
                    history=(),
                    cancel_event=cancel_event,
                )
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=call_planner)
        worker.start()
        entered = entered_stream.wait(timeout=2)
        cancel_event.set()
        worker.join(timeout=2)

        self.assertTrue(entered, errors)
        self.assertFalse(worker.is_alive())
        self.assertTrue(response_closed.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)


if __name__ == "__main__":
    unittest.main()
