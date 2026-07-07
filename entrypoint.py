"""Project entry point for running the detector from the repository root."""

import csv
from datetime import datetime
from pathlib import Path

import pandas as pd

from core.detector import DoorNumberDetector


_OUTPUT_COLUMNS = [
    "LATITUDE",
    "LONGITUDE",
    "NOME_COMPLETO_PORTA",
    "PREDICTION",
    "CONFIDENCE",
    "PREDICTION_FOUND",   # YES / NO
    "MATCH",              # YES / NO
]


def _init_output_csv(output_path: Path) -> None:
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f, delimiter=";").writerow(_OUTPUT_COLUMNS)


def _append_row_to_csv(output_path: Path, row_data: list) -> None:
    with open(output_path, "a", newline="", encoding="utf-8-sig") as f:
        csv.writer(f, delimiter=";").writerow(row_data)


def _load_already_processed(existing_csv: Path) -> set[tuple[float, float]]:
    """Return a set of (latitude, longitude) already present in an existing predictions CSV."""
    processed = set()
    with open(existing_csv, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        for row in reader:
            try:
                lat = float(row["LATITUDE"])
                lng = float(row["LONGITUDE"])
                processed.add((lat, lng))
            except (KeyError, ValueError):
                continue
    return processed


def run_batch_predictions(
    input_path: str | Path,
    output_path: str | Path,
    existing_csv: str | Path | None = None,
) -> Path:
    input_path  = Path(input_path)
    output_path = Path(output_path).with_suffix(".csv")

    if not input_path.exists():
        raise FileNotFoundError(f"Input workbook not found: {input_path}")

    df = pd.read_excel(input_path)
    required_columns = {"NOME_COMPLETO_PORTA", "LATITUDE", "LONGITUDE"}
    missing = required_columns.difference(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    # ── Filter out already-processed rows ─────────────────────────────────────
    already_processed: set[tuple[float, float]] = set()
    if existing_csv is not None:
        existing_csv = Path(existing_csv)
        if not existing_csv.exists():
            raise FileNotFoundError(f"Existing CSV not found: {existing_csv}")
        already_processed = _load_already_processed(existing_csv)
        print(f"Resuming from {existing_csv.name} — {len(already_processed)} rows already processed.")

    df_todo = df[
        ~df.apply(
            lambda r: (
                not pd.isna(r["LATITUDE"])
                and not pd.isna(r["LONGITUDE"])
                and (float(r["LATITUDE"]), float(r["LONGITUDE"])) in already_processed
            ),
            axis=1,
        )
    ]

    skipped = len(df) - len(df_todo)
    if skipped:
        print(f"Skipping {skipped} already-processed row(s). {len(df_todo)} remaining.")

    if df_todo.empty:
        print("Nothing to process — all rows already in the existing CSV.")
        return existing_csv

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _init_output_csv(output_path)

    # ── Copy already-processed rows into the new file first ───────────────────
    if existing_csv is not None:
        with open(existing_csv, newline="", encoding="utf-8-sig") as src:
            reader = csv.DictReader(src, delimiter=";")
            for row in reader:
                _append_row_to_csv(output_path, [row.get(col, "") for col in _OUTPUT_COLUMNS])
        print(f"Copied {len(already_processed)} existing row(s) into {output_path.name}.")

    detector = DoorNumberDetector()
    total   = len(df_todo)
    n_match = 0

    try:
        for i, (_, row) in enumerate(df_todo.iterrows(), 1):
            nome      = str(row.get("NOME_COMPLETO_PORTA", "") or "").strip()
            latitude  = row.get("LATITUDE")
            longitude = row.get("LONGITUDE")

            if pd.isna(latitude) or pd.isna(longitude):
                prediction       = None
                confidence       = 0
                prediction_found = "NO"
            else:
                result           = detector.detect_door_number(float(latitude), float(longitude))
                prediction_found = "YES" if result.get("success", False) else "NO"
                prediction       = result.get("door_number") if prediction_found == "YES" else None
                confidence       = result.get("confidence", 0)

            match = "YES" if (prediction is not None and nome != "" and prediction == nome) else "NO"
            if match == "YES":
                n_match += 1

            _append_row_to_csv(
                output_path,
                [latitude, longitude, nome, prediction, confidence, prediction_found, match],
            )

            status = "✓ MATCH" if match == "YES" else ("✗ WRONG" if prediction_found == "YES" else "? NOT FOUND")
            print(f"[{i}/{total}] expected={nome!r} predicted={prediction!r} ({confidence}%) {status}")

    finally:
        detector.close()

    print(f"\nDone: {n_match}/{total} matched ({100 * n_match / total:.1f}%)")
    print(f"Saved to {output_path}")
    return output_path


def main():
    # ── Batch mode ─────────────────────────────────────────────────────────────
    project_root   = Path(__file__).resolve().parent
    input_workbook = project_root / "Portas_Teste_2.xlsx"
    ts             = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_csv     = project_root / f"Portas_Teste2_predictions_{ts}.csv"

    # To resume from an existing CSV, set this to the path of that file.
    # Rows already present (matched by lat/lng) will be skipped.
    # Results for missing rows will be written to output_csv (a new file).
    existing_csv = None
    # existing_csv = project_root / f"Portas_Teste2_predictions_20260707_152914.csv"

    if input_workbook.exists():
        run_batch_predictions(input_workbook, output_csv, existing_csv=existing_csv)
        return
    else:
        print("No excel file with that name found. Proceeding with latitude/longitude sample...")

    # ── Single coordinate mode ─────────────────────────────────────────────────
    detector = DoorNumberDetector()

    #latitude, longitude = 41.256331, -8.645468        # 36 simple
    #latitude, longitude = 38.024463, -7.712959        # 17
    #latitude, longitude = 39.701068, -8.910996        # 14 very difficult
    # latitude, longitude = 41.088368507582, -6.81537789305259  # 36
    #latitude, longitude = 41.0893759030001, -6.81537262699993 # 131
    #latitude, longitude = 41.088695435, -6.81420591799997     # 17/19?
    #latitude, longitude = 41.088503121, -6.81459559899997     # 31
    #latitude, longitude = 41.0883351100001, -6.81493620399993 # 43/56?
    #latitude, longitude = 41.0881165805859, -6.8151718389904  # strange
    #latitude, longitude = 41.0883242360001, -6.81522462599997 # 36
    #latitude, longitude = 41.088532667, -6.81453213099996     # 22/29?
    #latitude, longitude = 41.088607396, -6.81463781499997     # 20
    #latitude, longitude = 41.089318, -6.813988                # 79
    #latitude, longitude = 41.0883129010001, -6.81497951799997  # 45
    #latitude, longitude = 41.0894836100001, -6.81600447899996
    # latitude, longitude = 41.089591501;-6.80906635299993 #da 202 em vez de 7/5
    #latitude, longitude = 41.090633, -6.810231 # shoudl be 55, it is giving 82 (virado para o lado oposto)
    # latitude, longitude = 41.090763108, -6.81034417499995 #same problem, virado para a rua, devia dar 59, dá 92
    # latitude, longitude = 41.0899111780001, -6.80824305099998
    latitude, longitude = 41.0897176190001, -6.80928270699997

    preview_image = detector.street_view.get_image(latitude, longitude)
    if preview_image is not None:
        print("Displaying fetched Street View image...")
        preview_image.show()

    result = detector.detect_door_number(latitude, longitude)

    print("\n" + "=" * 50)
    print("DETECTION RESULT")
    print("=" * 50)
    print(f"Latitude:    {result['latitude']}")
    print(f"Longitude:   {result['longitude']}")
    print(f"Door number: {result.get('door_number', 'N/A')}")
    print(f"Confidence:  {result.get('confidence', 0)}%")
    print(f"Status:      {'✓ Success' if result['success'] else '✗ Error'}")
    if not result["success"]:
        print(f"Error:       {result.get('error', 'Unknown')}")
    print("=" * 50)

    detector.close()


if __name__ == "__main__":
    main()
