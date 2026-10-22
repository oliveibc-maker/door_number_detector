import unittest
from datetime import datetime

from core.models import DetectionResult


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
                "latitude",
                "longitude",
                "door_number",
                "confidence",
                "heading",
                "pitch",
                "observation",
                "timestamp",
                "error",
            },
        )
        self.assertEqual(serialized["door_number"], "12A")
        self.assertIsNone(serialized["error"])
        self.assertIsInstance(datetime.fromisoformat(serialized["timestamp"]), datetime)

    def test_failure_and_no_imagery_can_be_distinguished(self):
        no_imagery = DetectionResult(
            success=False,
            latitude=41.2,
            longitude=-8.6,
            observation="No Street View imagery available",
        )
        failed = DetectionResult(
            success=False,
            latitude=41.2,
            longitude=-8.6,
            error="Image unavailable",
        )

        self.assertEqual(no_imagery.to_dict()["observation"], "No Street View imagery available")
        self.assertEqual(failed.to_dict()["error"], "Image unavailable")


if __name__ == "__main__":
    unittest.main()