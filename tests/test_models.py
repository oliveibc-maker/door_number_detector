import unittest
from datetime import datetime

from door_number_detector.core.models import DetectionOutcome, DetectionResult


class DetectionResultTests(unittest.TestCase):
    def test_serializes_a_stable_complete_schema(self):
        result = DetectionResult(
            success=True,
            latitude=41.2,
            longitude=-8.6,
            door_number="12A",
            confidence=92,
            heading=45,
            pitch=0,
        )

        serialized = result.to_dict()
        self.assertEqual(
            set(serialized),
            {
                "success",
                "outcome",
                "latitude",
                "longitude",
                "door_number",
                "confidence",
                "heading",
                "pitch",
                "observation",
                "timestamp",
                "error",
                "image_calls",
                "metadata_calls",
            },
        )
        self.assertEqual(serialized["door_number"], "12A")
        self.assertEqual(serialized["outcome"], DetectionOutcome.FOUND)
        self.assertEqual(serialized["image_calls"], 0)
        self.assertEqual(serialized["metadata_calls"], 0)
        self.assertIsNone(serialized["error"])
        self.assertIsInstance(datetime.fromisoformat(serialized["timestamp"]), datetime)

    def test_failure_and_no_imagery_can_be_distinguished(self):
        no_imagery = DetectionResult(
            success=False,
            outcome=DetectionOutcome.NO_IMAGERY,
            latitude=41.2,
            longitude=-8.6,
            observation="No Street View imagery available",
        )
        failed = DetectionResult(
            success=False,
            outcome=DetectionOutcome.IMAGE_UNAVAILABLE,
            latitude=41.2,
            longitude=-8.6,
            error="Image unavailable",
        )

        self.assertEqual(no_imagery.to_dict()["observation"], "No Street View imagery available")
        self.assertEqual(failed.to_dict()["error"], "Image unavailable")
        self.assertEqual(no_imagery.outcome, DetectionOutcome.NO_IMAGERY)
        self.assertEqual(failed.outcome, DetectionOutcome.IMAGE_UNAVAILABLE)

    def test_success_must_agree_with_explicit_outcome(self):
        with self.assertRaises(ValueError):
            DetectionResult(
                success=False,
                outcome=DetectionOutcome.FOUND,
                latitude=41.2,
                longitude=-8.6,
            )

        with self.assertRaises(ValueError):
            DetectionResult(
                success=True,
                outcome=DetectionOutcome.LOW_CONFIDENCE,
                latitude=41.2,
                longitude=-8.6,
            )


if __name__ == "__main__":
    unittest.main()
