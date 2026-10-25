import unittest

from door_number_detector.core.database import DatabaseManager
from door_number_detector.core.models import DetectionOutcome, DetectionResult
from door_number_detector.core.outcomes import OUTCOME_DETAILS


class FakeSession:
    def __init__(self):
        self.added = []
        self.committed = False
        self.closed = False

    def add(self, value):
        self.added.append(value)

    def commit(self):
        self.committed = True

    def rollback(self):
        return None

    def close(self):
        self.closed = True


class DatabaseOutcomeTests(unittest.TestCase):
    def test_outcomes_map_to_existing_status_and_reason_columns(self):
        for outcome, details in OUTCOME_DETAILS.items():
            with self.subTest(outcome=outcome):
                session = FakeSession()
                manager = object.__new__(DatabaseManager)
                manager.Session = lambda: session
                result = DetectionResult(
                    success=outcome is DetectionOutcome.FOUND,
                    outcome=outcome,
                    latitude=41.2,
                    longitude=-8.6,
                )

                manager.save_result(result.to_dict())

                if not details.persist:
                    self.assertEqual(session.added, [])
                    self.assertFalse(session.committed)
                    self.assertFalse(session.closed)
                    continue
                self.assertEqual(len(session.added), 1)
                self.assertEqual(session.added[0].status, details.database_status)
                self.assertEqual(session.added[0].error_message, details.database_message)
                self.assertTrue(session.committed)
                self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()
