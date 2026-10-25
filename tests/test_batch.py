import csv
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from door_number_detector.core.batch import (
    InvalidExcelWorkbookError,
    load_excel_dataframe,
    run_detection_batch,
    run_excel_batch,
    run_sqlserver_batch,
)
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
    def test_excel_loader_rejects_empty_and_missing_columns(self):
        dataframes = (
            pd.DataFrame(columns=["NOME_COMPLETO_PORTA", "LATITUDE", "LONGITUDE"]),
            pd.DataFrame([{"NOME_COMPLETO_PORTA": "1", "LATITUDE": 41.2}]),
        )
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.xlsx"
            input_path.touch()
            for dataframe in dataframes:
                with (
                    self.subTest(dataframe=dataframe),
                    patch(
                        "door_number_detector.core.batch.read_excel_any",
                        return_value=dataframe,
                    ),
                ):
                    with self.assertRaises(InvalidExcelWorkbookError):
                        load_excel_dataframe(input_path)

    def test_excel_batch_reuses_a_preloaded_workbook(self):
        dataframe = pd.DataFrame(
            [{"NOME_COMPLETO_PORTA": "12A", "LATITUDE": 41.2, "LONGITUDE": -8.6}]
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

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.xlsx"
            input_path.touch()
            with patch("door_number_detector.core.batch.read_excel_any") as read_excel:
                result_path = run_excel_batch(
                    input_path,
                    root / "run.csv",
                    detector_instance=detector,
                    metrics=FakeMetrics(),
                    dataframe=dataframe,
                )
            read_excel.assert_not_called()

        self.assertEqual(result_path.suffix, ".csv")
        self.assertEqual(detector.calls, [(41.2, -8.6)])

    def test_sqlserver_batch_reports_loaded_row_count(self):
        dataframe = pd.DataFrame(
            [{"NOME_COMPLETO_PORTA": "12A", "LATITUDE": 41.2, "LONGITUDE": -8.6}]
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
        events = []

        def provide_detector():
            events.append("detector")
            return detector

        def record_count(count):
            events.append(("rows", count))

        def query_rows(*args, **kwargs):
            events.append("query")
            return dataframe

        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "door_number_detector.core.batch.query_sqlserver", side_effect=query_rows
            ) as query_sqlserver:
                run_sqlserver_batch(
                    "rua",
                    "123",
                    Path(directory) / "run.csv",
                    metrics=FakeMetrics(),
                    on_rows_loaded=record_count,
                    detector_factory=provide_detector,
                )

        query_sqlserver.assert_called_once()
        self.assertEqual(events, ["query", ("rows", 1), "detector"])
        self.assertEqual(detector.calls, [(41.2, -8.6)])

    def test_cancelled_batch_keeps_header_and_does_not_detect_rows(self):
        dataframe = pd.DataFrame(
            [{"NOME_COMPLETO_PORTA": "12A", "LATITUDE": 41.2, "LONGITUDE": -8.6}]
        )
        detector = FakeDetector([])
        cancel_event = threading.Event()
        cancel_event.set()

        with tempfile.TemporaryDirectory() as directory:
            result_path = run_detection_batch(
                dataframe,
                Path(directory) / "cancelled.csv",
                set(),
                detector_instance=detector,
                cancel_event=cancel_event,
                metrics=FakeMetrics(),
            )
            with result_path.open(newline="", encoding="utf-8-sig") as output_file:
                rows = list(csv.DictReader(output_file, delimiter=";"))

        self.assertEqual(rows, [])
        self.assertEqual(detector.calls, [])

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
