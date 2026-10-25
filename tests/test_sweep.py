import threading
import unittest

from door_number_detector.core.candidate_selection import Candidate, CandidateSelector
from door_number_detector.core.google_street_view import NoImageryAvailable
from door_number_detector.core.sweep import StreetViewSweep


class FakeStreetView:
    def __init__(self, images):
        self.images = iter(images)
        self.calls = []

    def get_image(self, latitude, longitude, **kwargs):
        self.calls.append((latitude, longitude, kwargs))
        image = next(self.images)
        if isinstance(image, Exception):
            raise image
        return image


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
            41.0,
            -8.0,
            90,
            sweep=[(10, [0, 5], [0, -5])],
        )

        self.assertEqual([candidate.number for candidate in candidates], ["9", "10"])
        self.assertEqual(
            [call[2]["heading_offset"] for call in street_view.calls],
            [0, 5],
        )
        self.assertEqual(
            [call[2]["pitch"] for call in street_view.calls],
            [0, 0],
        )

    def test_empty_frames_continue_until_sweep_is_exhausted(self):
        street_view = FakeStreetView([None, None])
        sweep = StreetViewSweep(street_view, FakeOcr([]), CandidateSelector(70))

        self.assertEqual(
            sweep.fetch_candidates(41.0, -8.0, 90, sweep=[(10, [0, 5], [0])]),
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
                41.0,
                -8.0,
                90,
                sweep=[(10, [0], [0])],
                cancel_event=cancel_event,
            ),
            [],
        )
        self.assertEqual(street_view.calls, [])

    def test_no_imagery_exception_propagates_after_sweep_abort(self):
        street_view = FakeStreetView([NoImageryAvailable("none")])
        sweep = StreetViewSweep(street_view, FakeOcr([]), CandidateSelector(70))

        with self.assertRaises(NoImageryAvailable):
            sweep.fetch_candidates(41.0, -8.0, 90, sweep=[(10, [0, 5], [0])])
        self.assertEqual(len(street_view.calls), 1)

    def test_sweep_continues_past_suspicious_candidate_then_stops_on_clean_one(self):
        street_view = FakeStreetView(["year-fragment", "door-number"])
        ocr = FakeOcr([[Candidate("23", 99)], [Candidate("17", 90)]])
        sweep = StreetViewSweep(street_view, ocr, CandidateSelector(70))

        candidates = sweep.fetch_candidates(41.0, -8.0, 90, sweep=[(10, [0, 5], [0])])

        self.assertEqual([candidate.number for candidate in candidates], ["23", "17"])
        self.assertEqual(len(street_view.calls), 2)


if __name__ == "__main__":
    unittest.main()
