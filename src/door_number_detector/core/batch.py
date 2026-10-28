"""Batch orchestration shared by the CLI and web application."""

from __future__ import annotations

import csv
import logging
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from door_number_detector.core.config import Config
from door_number_detector.core.metrics import RunMetrics
from door_number_detector.core.models import DetectionOutcome
from door_number_detector.core.outcomes import OUTCOME_DETAILS
from door_number_detector.core.sources import SourceFilter, query_sqlserver, read_excel_any

if TYPE_CHECKING:
    from door_number_detector.core.detector import DoorNumberDetector


logger = logging.getLogger(__name__)

OUTPUT_COLUMNS = [
    "LATITUDE",
    "LONGITUDE",
    "NOME_COMPLETO_PORTA",
    "PREDICTION",
    "CONFIDENCE",
    "PREDICTION_FOUND",
    "MATCH",
    "OBSERVATION",
]

_NO_IMAGERY_TOKEN = OUTCOME_DETAILS[DetectionOutcome.NO_IMAGERY].csv_prediction_found


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().upper())


def _init_output_csv(output_path: Path) -> None:
    with output_path.open("w", newline="", encoding="utf-8-sig") as output_file:
        csv.writer(output_file, delimiter=";").writerow(OUTPUT_COLUMNS)


def _append_row_to_csv(output_path: Path, row_data: list) -> None:
    with output_path.open("a", newline="", encoding="utf-8-sig") as output_file:
        csv.writer(output_file, delimiter=";").writerow(row_data)


def load_already_processed(existing_csv: Path) -> set[tuple[float, float]]:
    processed = set()
    with Path(existing_csv).open(newline="", encoding="utf-8-sig") as input_file:
        reader = csv.DictReader(input_file, delimiter=";")
        for row in reader:
            try:
                processed.add((float(row["LATITUDE"]), float(row["LONGITUDE"])))
            except (KeyError, ValueError):
                continue
    return processed


def _get_resume_state(
    existing_csv: str | Path | None,
) -> tuple[set[tuple[float, float]], Path | None]:
    if existing_csv is None:
        return set(), None
    existing_path = Path(existing_csv)
    if not existing_path.exists():
        raise FileNotFoundError(f"Existing CSV not found: {existing_path}")
    already_processed = load_already_processed(existing_path)
    logger.info(
        "Resuming from %s — %s rows already processed.",
        existing_path.name,
        len(already_processed),
    )
    return already_processed, existing_path


class InvalidExcelWorkbookError(ValueError):
    """Raised when a workbook has no rows or lacks required source columns."""


_REQUIRED_EXCEL_COLUMNS = {"NOME_COMPLETO_PORTA", "LATITUDE", "LONGITUDE"}


def _read_excel_workbook(input_path: Path) -> pd.DataFrame:
    if not input_path.exists():
        raise FileNotFoundError(f"Input workbook not found: {input_path}")
    return read_excel_any(input_path)


def load_excel_dataframe(input_path: str | Path) -> pd.DataFrame:
    """Read a workbook for the web upload, rejecting empty or incomplete ones."""
    dataframe = _read_excel_workbook(Path(input_path))
    if dataframe.empty or not _REQUIRED_EXCEL_COLUMNS.issubset(dataframe.columns):
        raise InvalidExcelWorkbookError("The spreadsheet is empty or missing required columns.")
    return dataframe


def run_detection_batch(
    dataframe: pd.DataFrame,
    output_path: str | Path,
    already_processed: set[tuple[float, float]],
    existing_csv: str | Path | None = None,
    detector_instance: DoorNumberDetector | None = None,
    cancel_event: threading.Event | None = None,
    metrics: RunMetrics | None = None,
) -> Path | None:
    output_path = Path(output_path).with_suffix(".csv")
    existing_path = Path(existing_csv) if existing_csv is not None else None
    rows_to_process = dataframe[
        ~dataframe.apply(
            lambda row: (
                not pd.isna(row["LATITUDE"])
                and not pd.isna(row["LONGITUDE"])
                and (float(row["LATITUDE"]), float(row["LONGITUDE"])) in already_processed
            ),
            axis=1,
        )
    ]

    skipped = len(dataframe) - len(rows_to_process)
    if skipped:
        logger.info(
            "Skipping %s already-processed row(s). %s remaining.",
            skipped,
            len(rows_to_process),
        )

    if rows_to_process.empty:
        logger.info("Nothing to process — all rows already exist in the resume CSV.")
        return existing_path

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _init_output_csv(output_path)

    if existing_path is not None:
        with existing_path.open(newline="", encoding="utf-8-sig") as input_file:
            reader = csv.DictReader(input_file, delimiter=";")
            for row in reader:
                _append_row_to_csv(output_path, [row.get(column, "") for column in OUTPUT_COLUMNS])
        logger.info("Copied %s existing row(s) into %s.", len(already_processed), output_path.name)

    owns_detector = detector_instance is None
    detector: DoorNumberDetector
    if owns_detector:
        from door_number_detector.core.detector import DoorNumberDetector

        detector = DoorNumberDetector()
    else:
        assert detector_instance is not None
        detector = detector_instance

    owns_metrics = metrics is None
    if metrics is None:
        metrics = RunMetrics()
    assert metrics is not None

    total = len(rows_to_process)
    matches = 0
    errors = 0
    no_imagery_count = 0
    row_index = 0
    cancelled_detection = False

    try:
        for row_index, (_, row) in enumerate(rows_to_process.iterrows(), 1):
            if cancel_event is not None and cancel_event.is_set():
                logger.info("[%s/%s] Batch cancelled by user — stopping cleanly.", row_index, total)
                break

            expected = str(row.get("NOME_COMPLETO_PORTA", "") or "").strip()
            latitude = row.get("LATITUDE")
            longitude = row.get("LONGITUDE")
            prediction = None
            confidence = 0
            prediction_found = "NO"
            observation = ""

            if pd.isna(latitude) or pd.isna(longitude):
                logger.warning("[%s/%s] Skipping row with null coordinates.", row_index, total)
            else:
                started_at = time.perf_counter()
                try:
                    result = detector.detect_door_number(
                        float(latitude), float(longitude), cancel_event=cancel_event
                    )
                except Exception as exc:
                    errors += 1
                    failed_details = OUTCOME_DETAILS[DetectionOutcome.FAILED]
                    logger.error(
                        "[%s/%s] detect_door_number failed at (%s, %s): %s",
                        row_index,
                        total,
                        latitude,
                        longitude,
                        exc,
                        exc_info=True,
                    )
                    _append_row_to_csv(
                        output_path,
                        [
                            latitude,
                            longitude,
                            expected,
                            None,
                            0,
                            failed_details.csv_prediction_found,
                            "NO",
                            failed_details.observation,
                        ],
                    )
                    # Usage reaches the batch only through a returned result. The
                    # detector turns its own errors into FAILED results with usage,
                    # so only a defect in that handling lands here.
                    metrics.record(
                        elapsed_s=time.perf_counter() - started_at,
                        image_calls=0,
                        metadata_calls=0,
                        success=False,
                        no_imagery=False,
                    )
                    continue

                outcome = result.outcome
                assert outcome is not None
                details = OUTCOME_DETAILS[outcome]
                metrics.record(
                    elapsed_s=time.perf_counter() - started_at,
                    image_calls=result.image_calls,
                    metadata_calls=result.metadata_calls,
                    success=details.metrics_success,
                    no_imagery=details.metrics_no_imagery,
                )
                # Only a cancelled outcome skips the row. A detection that finished
                # while cancel was requested is already saved, so it gets its CSV row
                # too; the loop then stops before the next row.
                if outcome is DetectionOutcome.CANCELLED:
                    cancelled_detection = True
                    logger.info("[%s/%s] Cancelled mid-detection — stopping.", row_index, total)
                    break

                prediction_found = details.csv_prediction_found
                observation = details.observation
                if outcome is DetectionOutcome.FAILED:
                    errors += 1
                if details.metrics_no_imagery:
                    no_imagery_count += 1
                else:
                    confidence = result.confidence
                if details.metrics_success:
                    prediction = result.door_number

            match = (
                "YES"
                if (
                    prediction is not None
                    and expected != ""
                    and _normalize(prediction) == _normalize(expected)
                )
                else "NO"
            )
            if match == "YES":
                matches += 1

            _append_row_to_csv(
                output_path,
                [
                    latitude,
                    longitude,
                    expected,
                    prediction,
                    confidence,
                    prediction_found,
                    match,
                    observation,
                ],
            )
            status = (
                "✓ MATCH"
                if match == "YES"
                else "📷 NO IMAGERY"
                if prediction_found == _NO_IMAGERY_TOKEN
                else "✗ WRONG"
                if prediction_found == "YES"
                else "? NOT FOUND"
            )
            logger.info(
                "[%s/%s] expected=%r predicted=%r (%s%%) %s",
                row_index,
                total,
                expected,
                prediction,
                confidence,
                status,
            )

    except Exception as fatal:
        logger.critical("Fatal error at row %s/%s: %s", row_index, total, fatal, exc_info=True)
        raise
    finally:
        if owns_detector:
            detector.close()

    cancelled = cancelled_detection or (cancel_event is not None and cancel_event.is_set())
    if cancelled:
        logger.info(
            "Cancelled after %s row(s). Partial results saved to %s", row_index, output_path
        )
    else:
        addressable = total - no_imagery_count
        logger.info(
            "Done: %s/%s matched (%.1f%%) | %s sem cobertura GSV | %s error(s)",
            matches,
            total,
            100 * matches / total,
            no_imagery_count,
            errors,
        )
        if addressable:
            logger.info(
                "Effective accuracy (excl. no imagery): %.1f%%", 100 * matches / addressable
            )
        logger.info("Saved to %s", output_path)

    metrics.print_summary(print_fn=logger.info)
    if owns_metrics:
        metrics_path = output_path.with_suffix(".metrics.json")
        metrics.save_json(metrics_path)
        logger.info("Metrics saved to %s", metrics_path.name)
    return output_path


def run_sqlserver_batch(
    filter_by: SourceFilter,
    filter_value: str,
    output_path: str | Path,
    existing_csv: str | Path | None = None,
    detector_instance: DoorNumberDetector | None = None,
    cancel_event: threading.Event | None = None,
    metrics: RunMetrics | None = None,
    on_rows_loaded: Callable[[int], None] | None = None,
    detector_factory: Callable[[], DoorNumberDetector] | None = None,
) -> Path | None:
    if detector_factory is not None and detector_instance is not None:
        raise ValueError("Pass either detector_instance or detector_factory, not both.")
    dataframe = query_sqlserver(filter_by, filter_value, Config())
    if on_rows_loaded is not None:
        on_rows_loaded(len(dataframe))
    if detector_factory is not None:
        detector_instance = detector_factory()
    already_processed, existing_path = _get_resume_state(existing_csv)
    return run_detection_batch(
        dataframe,
        output_path,
        already_processed,
        existing_path,
        detector_instance=detector_instance,
        cancel_event=cancel_event,
        metrics=metrics,
    )


def run_excel_batch(
    input_path: str | Path,
    output_path: str | Path,
    existing_csv: str | Path | None = None,
    detector_instance: DoorNumberDetector | None = None,
    cancel_event: threading.Event | None = None,
    metrics: RunMetrics | None = None,
    dataframe: pd.DataFrame | None = None,
) -> Path | None:
    input_path = Path(input_path)
    if dataframe is None:
        dataframe = _read_excel_workbook(input_path)
        missing = _REQUIRED_EXCEL_COLUMNS.difference(dataframe.columns)
        if missing:
            raise ValueError(f"Missing required columns: {sorted(missing)}")

    already_processed, existing_path = _get_resume_state(existing_csv)
    return run_detection_batch(
        dataframe,
        output_path,
        already_processed,
        existing_path,
        detector_instance=detector_instance,
        cancel_event=cancel_event,
        metrics=metrics,
    )
