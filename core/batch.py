"""Batch orchestration shared by the CLI and web application."""

from __future__ import annotations

import csv
import logging
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pandas as pd

from core.config import Config
from core.metrics import RunMetrics
from core.sources import query_sqlserver, read_excel_any

if TYPE_CHECKING:
    from core.detector import DoorNumberDetector


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

_NO_IMAGERY_TOKEN = "NO_IMAGERY"
_NO_IMAGERY_OBSERVATION = "No Street View imagery available"


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


def _get_resume_state(existing_csv: str | Path | None) -> tuple[set[tuple[float, float]], Path | None]:
    if existing_csv is None:
        return set(), None
    existing_path = Path(existing_csv)
    if not existing_path.exists():
        raise FileNotFoundError(f"Existing CSV not found: {existing_path}")
    already_processed = load_already_processed(existing_path)
    print(f"Resuming from {existing_path.name} — {len(already_processed)} rows already processed.")
    return already_processed, existing_path


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
        print(f"Skipping {skipped} already-processed row(s). {len(rows_to_process)} remaining.")

    if rows_to_process.empty:
        print("Nothing to process — all rows already in the existing CSV.")
        return existing_path

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _init_output_csv(output_path)

    if existing_path is not None:
        with existing_path.open(newline="", encoding="utf-8-sig") as input_file:
            reader = csv.DictReader(input_file, delimiter=";")
            for row in reader:
                _append_row_to_csv(output_path, [row.get(column, "") for column in OUTPUT_COLUMNS])
        print(f"Copied {len(already_processed)} existing row(s) into {output_path.name}.")

    owns_detector = detector_instance is None
    if owns_detector:
        from core.detector import DoorNumberDetector

        detector = DoorNumberDetector()
    else:
        detector = detector_instance

    owns_metrics = metrics is None
    if owns_metrics:
        metrics = RunMetrics()

    total = len(rows_to_process)
    matches = 0
    errors = 0
    no_imagery_count = 0
    row_index = 0

    try:
        for row_index, (_, row) in enumerate(rows_to_process.iterrows(), 1):
            if cancel_event is not None and cancel_event.is_set():
                print(f"[{row_index}/{total}] Batch cancelled by user — stopping cleanly.")
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
                detector.street_view.reset_call_counts()
                started_at = time.perf_counter()
                result_success = False
                is_no_imagery = False

                try:
                    result = detector.detect_door_number(
                        float(latitude), float(longitude), cancel_event=cancel_event
                    )
                    if cancel_event is not None and cancel_event.is_set():
                        print(f"[{row_index}/{total}] Cancelled mid-detection — stopping.")
                        break

                    observation = result.get("observation", "")
                    is_no_imagery = observation == _NO_IMAGERY_OBSERVATION
                    if is_no_imagery:
                        prediction_found = _NO_IMAGERY_TOKEN
                        no_imagery_count += 1
                    else:
                        prediction_found = "YES" if result.get("success", False) else "NO"
                        prediction = result.get("door_number") if prediction_found == "YES" else None
                        confidence = result.get("confidence", 0)
                        result_success = result.get("success", False)
                except Exception as exc:
                    errors += 1
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
                        [latitude, longitude, expected, None, 0, "NO", "NO", ""],
                    )
                    image_calls, metadata_calls = detector.street_view.get_call_counts()
                    metrics.record(
                        elapsed_s=time.perf_counter() - started_at,
                        image_calls=image_calls,
                        metadata_calls=metadata_calls,
                        success=False,
                        no_imagery=False,
                    )
                    continue

                image_calls, metadata_calls = detector.street_view.get_call_counts()
                metrics.record(
                    elapsed_s=time.perf_counter() - started_at,
                    image_calls=image_calls,
                    metadata_calls=metadata_calls,
                    success=result_success,
                    no_imagery=is_no_imagery,
                )

            match = "YES" if (
                prediction is not None
                and expected != ""
                and _normalize(prediction) == _normalize(expected)
            ) else "NO"
            if match == "YES":
                matches += 1

            _append_row_to_csv(
                output_path,
                [latitude, longitude, expected, prediction, confidence, prediction_found, match, observation],
            )
            status = (
                "✓ MATCH" if match == "YES" else
                "📷 NO IMAGERY" if prediction_found == _NO_IMAGERY_TOKEN else
                "✗ WRONG" if prediction_found == "YES" else
                "? NOT FOUND"
            )
            print(f"[{row_index}/{total}] expected={expected!r} predicted={prediction!r} ({confidence}%) {status}")

    except Exception as fatal:
        logger.critical("Fatal error at row %s/%s: %s", row_index, total, fatal, exc_info=True)
        raise
    finally:
        if owns_detector:
            detector.close()

    cancelled = cancel_event is not None and cancel_event.is_set()
    if cancelled:
        print(f"\nCancelled after {row_index} row(s). Partial results saved to {output_path}")
    else:
        addressable = total - no_imagery_count
        print(
            f"\nDone: {matches}/{total} matched "
            f"({100 * matches / total:.1f}%) | "
            f"{no_imagery_count} sem cobertura GSV | "
            f"{errors} error(s)"
        )
        if addressable:
            print(f"Effective accuracy (excl. no imagery): {100 * matches / addressable:.1f}%")
        print(f"Saved to {output_path}")

    metrics.print_summary()
    if owns_metrics:
        metrics_path = output_path.with_suffix(".metrics.json")
        metrics.save_json(metrics_path)
        print(f"Metrics saved to {metrics_path.name}")
    return output_path


def run_sqlserver_batch(
    filter_by: Literal["localidade", "freguesia", "concelho", "rua"],
    filter_value: str,
    output_path: str | Path,
    existing_csv: str | Path | None = None,
    detector_instance: DoorNumberDetector | None = None,
    cancel_event: threading.Event | None = None,
    metrics: RunMetrics | None = None,
) -> Path | None:
    dataframe = query_sqlserver(filter_by, filter_value, Config())
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
) -> Path | None:
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input workbook not found: {input_path}")

    dataframe = read_excel_any(input_path)
    required_columns = {"NOME_COMPLETO_PORTA", "LATITUDE", "LONGITUDE"}
    missing = required_columns.difference(dataframe.columns)
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