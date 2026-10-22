import csv
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from door_number_detector.core.batch import run_detection_batch
from door_number_detector.core.models import DetectionResult


class FakeStreetView:
    def __init__(self):
        self.counts = (3, 1)

    def reset_call_counts(self):
        self.counts = (0, 0)

    def get_call_counts(self):
        return self.counts


class FakeDetector:
    def __init__(self, results):
        self.street_view = FakeStreetView()
        self.results = iter(results)
        self.calls = []

    def detect_door_number(self, latitude, longitude, cancel_event=None):
        self.calls.append((latitude, longitude))
        self.street_view.counts = (3, 1)
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


class FakeMetrics:
    def __init__(self):
        self.records = []

    def record(self, **values):
        self.records.append(values)

    def print_summary(self, print_fn=print):
        return None


class BatchTests(unittest.TestCase):
    def test_detection_writes_expected_csv_and_records_metrics(self):
        dataframe = pd.DataFrame(
            [
                {
                    "NOME_COMPLETO_PORTA": "12 A",
                    "LATITUDE": 41.2,
                    "LONGITUDE": -8.6,
                }
            ]
        )
        detector = FakeDetector(
            [
                DetectionResult(
                    success=True,
                    latitude=41.2,
                    longitude=-8.6,
                    door_number="12A",
                    confidence=91,
                )
            ]
        )
        metrics = FakeMetrics()

        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "run"
            result_path = run_detection_batch(
                dataframe,
                output_path,
                set(),
                detector_instance=detector,
                metrics=metrics,
            )
            with result_path.open(newline="", encoding="utf-8-sig") as output_file:
                rows = list(csv.DictReader(output_file, delimiter=";"))

        self.assertEqual(result_path.suffix, ".csv")
        self.assertEqual(rows[0]["PREDICTION"], "12A")
        self.assertEqual(rows[0]["MATCH"], "YES")
        self.assertEqual(rows[0]["PREDICTION_FOUND"], "YES")
        self.assertEqual(detector.calls, [(41.2, -8.6)])
        self.assertEqual(len(metrics.records), 1)
        self.assertEqual(metrics.records[0]["image_calls"], 3)
        self.assertTrue(metrics.records[0]["success"])

    def test_resume_copies_existing_rows_and_skips_matching_coordinates(self):
        dataframe = pd.DataFrame(
            [
                {"NOME_COMPLETO_PORTA": "1", "LATITUDE": 40.0, "LONGITUDE": -8.0},
                {"NOME_COMPLETO_PORTA": "2", "LATITUDE": 41.0, "LONGITUDE": -8.0},
            ]
        )
        detector = FakeDetector(
            [
                DetectionResult(
                    success=True,
                    latitude=41.0,
                    longitude=-8.0,
                    door_number="2",
                    confidence=88,
                )
            ]
        )
        header = (
            "LATITUDE;LONGITUDE;NOME_COMPLETO_PORTA;PREDICTION;CONFIDENCE;"
            "PREDICTION_FOUND;MATCH;OBSERVATION\n"
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing_csv = root / "existing.csv"
            existing_csv.write_text(header + "40.0;-8.0;1;1;90;YES;YES;\n", encoding="utf-8")
            result_path = run_detection_batch(
                dataframe,
                root / "resumed.csv",
                {(40.0, -8.0)},
                existing_csv=existing_csv,
                detector_instance=detector,
                metrics=FakeMetrics(),
            )
            with result_path.open(newline="", encoding="utf-8-sig") as output_file:
                rows = list(csv.DictReader(output_file, delimiter=";"))

        self.assertEqual(len(rows), 2)
        self.assertEqual(detector.calls, [(41.0, -8.0)])
        self.assertEqual([row["NOME_COMPLETO_PORTA"] for row in rows], ["1", "2"])

    def test_no_imagery_result_maps_to_existing_csv_fields_and_metrics(self):
        dataframe = pd.DataFrame(
            [
                {
                    "NOME_COMPLETO_PORTA": "4",
                    "LATITUDE": 41.2,
                    "LONGITUDE": -8.6,
                }
            ]
        )
        detector = FakeDetector(
            [
                DetectionResult(
                    success=False,
                    latitude=41.2,
                    longitude=-8.6,
                    observation="No Street View imagery available",
                )
            ]
        )
        metrics = FakeMetrics()

        with tempfile.TemporaryDirectory() as directory:
            result_path = run_detection_batch(
                dataframe,
                Path(directory) / "no-imagery.csv",
                set(),
                detector_instance=detector,
                metrics=metrics,
            )
            with result_path.open(newline="", encoding="utf-8-sig") as output_file:
                row = next(csv.DictReader(output_file, delimiter=";"))

        self.assertEqual(row["PREDICTION_FOUND"], "NO_IMAGERY")
        self.assertEqual(row["OBSERVATION"], "No Street View imagery available")
        self.assertTrue(metrics.records[0]["no_imagery"])


if __name__ == "__main__":
    unittest.main()
