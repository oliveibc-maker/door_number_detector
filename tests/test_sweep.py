import threading
import unittest

from door_number_detector.core.candidate_selection import Candidate, CandidateSelector
from door_number_detector.core.google_street_view import CameraPosition
from door_number_detector.core.sweep import StreetViewSweep


class FakeStreetView:
    def __init__(self, images):
        self.images = iter(images)
        self.calls = []

    def get_image(self, camera, **kwargs):
        self.calls.append((camera, kwargs))
        return next(self.images)


class FakeOcr:
    def __init__(self, results):
        self.results = iter(results)

    def extract_door_number(self, image):
        return next(self.results)


class SweepTests(unittest.TestCase):
    def test_sweep_preserves_offset_pitch_order_and_confident_early_stop(self):
        street_view = FakeStreetView(["first", "second", "third"])
        ocr = FakeOcr([[Candidate("9", 95)], [Candidate("10", 97)]])
        sweep = StreetViewSweep(street_view, ocr, CandidateSelector(97))

        candidates = sweep.fetch_candidates(
            CameraPosition(41.0, -8.0, None, True),
            90,
            sweep=[(10, [0, 5], [0, -5])],
        )

        self.assertEqual([candidate.number for candidate in candidates], ["9", "10"])
        self.assertEqual(
            [call[1]["heading"] for call in street_view.calls],
            [90, 95],
        )
        self.assertEqual(
            [call[1]["pitch"] for call in street_view.calls],
            [0, 0],
        )

    def test_empty_frames_continue_until_sweep_is_exhausted(self):
        street_view = FakeStreetView([None, None])
        sweep = StreetViewSweep(street_view, FakeOcr([]), CandidateSelector(70))

        self.assertEqual(
            sweep.fetch_candidates(
                CameraPosition(41.0, -8.0, None, True),
                90,
                sweep=[(10, [0, 5], [0])],
            ),
            [],
        )
        self.assertEqual(len(street_view.calls), 2)

    def test_cancellation_before_sweep_makes_no_fetches(self):
        street_view = FakeStreetView([])
        sweep = StreetViewSweep(street_view, FakeOcr([]), CandidateSelector(70))
        cancel_event = threading.Event()
        cancel_event.set()

        self.assertEqual(
            sweep.fetch_candidates(
                CameraPosition(41.0, -8.0, None, True),
                90,
                sweep=[(10, [0], [0])],
                cancel_event=cancel_event,
            ),
            [],
        )
        self.assertEqual(street_view.calls, [])

    def test_headings_wrap_across_north(self):
        street_view = FakeStreetView([None, None, None])
        sweep = StreetViewSweep(street_view, FakeOcr([]), CandidateSelector(70))

        sweep.fetch_candidates(
            CameraPosition(41.0, -8.0, None, False),
            355,
            sweep=[(10, [0, 10, -360], [0])],
        )

        self.assertEqual([call[1]["heading"] for call in street_view.calls], [355, 5, 355])

    def test_sweep_continues_past_suspicious_candidate_then_stops_on_clean_one(self):
        street_view = FakeStreetView(["year-fragment", "door-number"])
        ocr = FakeOcr([[Candidate("23", 99)], [Candidate("17", 90)]])
        sweep = StreetViewSweep(street_view, ocr, CandidateSelector(70))

        candidates = sweep.fetch_candidates(
            CameraPosition(41.0, -8.0, None, True),
            90,
            sweep=[(10, [0, 5], [0])],
        )

        self.assertEqual([candidate.number for candidate in candidates], ["23", "17"])
        self.assertEqual(len(street_view.calls), 2)


if __name__ == "__main__":
    unittest.main()
