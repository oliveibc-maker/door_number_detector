import unittest

from door_number_detector.core.candidate_selection import Candidate, CandidateSelector


def candidate(number, confidence, heading_offset=0):
    return Candidate(
        number=number,
        confidence=confidence,
        heading_offset=heading_offset,
        pitch=0,
        fov=10,
    )


class CandidateSelectorTests(unittest.TestCase):
    def setUp(self):
        self.selector = CandidateSelector(confidence_threshold=70)

    def test_clean_candidate_replaces_year_watermark(self):
        decision = self.selector.select([candidate("2024", 99), candidate("17", 90)])

        self.assertEqual(decision, candidate("17", 90))

    def test_year_is_kept_when_no_qualified_alternative_exists(self):
        self.assertEqual(self.selector.select([candidate("2024", 99)]), candidate("2024", 99))
        self.assertEqual(
            self.selector.select([candidate("2024", 99), candidate("17", 69)]),
            candidate("2024", 99),
        )

    def test_year_falls_back_to_tier_two_when_no_clean_candidate_exists(self):
        candidates = [candidate("2024", 99), candidate("24", 90)]

        self.assertEqual(self.selector.select(candidates), candidates[1])

    def test_tier_two_candidate_is_kept_without_clean_alternative(self):
        for number in ("2", "20", "21", "22", "23", "24", "25", "26"):
            with self.subTest(number=number):
                self.assertEqual(
                    self.selector.select([candidate(number, 90)]), candidate(number, 90)
                )

    def test_newly_suspicious_tiers_yield_to_clean_candidates(self):
        for number in ("2", "3001"):
            with self.subTest(number=number):
                self.assertEqual(
                    self.selector.select([candidate(number, 99), candidate("17", 90)]),
                    candidate("17", 90),
                )

    def test_repeated_confident_reads_break_confidence_ties(self):
        winner = self.selector.select(
            [
                candidate("8", 90, 0),
                candidate("8", 90, 5),
                candidate("12", 90, 10),
            ]
        )

        self.assertEqual(winner, candidate("8", 90, 0))

    def test_number_length_breaks_vote_ties(self):
        winner = self.selector.select([candidate("8", 90), candidate("12", 90)])

        self.assertEqual(winner, candidate("12", 90))

    def test_below_threshold_returns_best_candidate_but_does_not_stop(self):
        best = candidate("12", 69)

        self.assertEqual(self.selector.select([candidate("8", 60), best]), best)
        self.assertFalse(self.selector.should_stop([best]))

    def test_empty_candidates_have_no_decision(self):
        self.assertIsNone(self.selector.select([]))
        self.assertFalse(self.selector.should_stop([]))

    def test_suspicious_candidates_never_stop_the_sweep(self):
        for number in ("2", "20", "23", "202", "2024", "1999", "3001"):
            with self.subTest(number=number):
                self.assertFalse(self.selector.should_stop([candidate(number, 99)]))

    def test_clean_confident_candidate_stops_with_same_final_winner(self):
        candidates = [candidate("8", 88), candidate("17", 90)]

        self.assertTrue(self.selector.should_stop(candidates))
        self.assertEqual(self.selector.select(candidates), candidates[-1])

    def test_every_early_stop_candidate_is_the_final_selection(self):
        numbers = ("2", "17", "20", "23", "202", "2024", "1999", "3001")
        for first in numbers:
            for second in numbers:
                candidates = [candidate(first, 90), candidate(second, 90)]
                if self.selector.should_stop(candidates):
                    self.assertEqual(
                        self.selector.select(candidates), self.selector.best(candidates)
                    )


if __name__ == "__main__":
    unittest.main()
