"""Project entry point for running the detector from the repository root."""

import csv
from pathlib import Path

import pandas as pd

from core.detector import DoorNumberDetector

from datetime import datetime

_OUTPUT_COLUMNS = [
    "LATITUDE",
    "LONGITUDE",
    "NOME_COMPLETO_PORTA",
    "PREDICTION",
    "CONFIDENCE",
    "PREDICTION_FOUND",   # prediction cleared the confidence threshold
    "MATCH",       # prediction == NOME_COMPLETO_PORTA
]


def _init_output_csv(output_path: Path) -> None:
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f, delimiter=";").writerow(_OUTPUT_COLUMNS)


def _append_row_to_csv(output_path: Path, row_data: list) -> None:
    with open(output_path, "a", newline="", encoding="utf-8-sig") as f:
        csv.writer(f, delimiter=";").writerow(row_data)


def run_batch_predictions(input_path: str | Path, output_path: str | Path) -> Path:
    input_path  = Path(input_path)
    output_path = Path(output_path).with_suffix(".csv")

    if not input_path.exists():
        raise FileNotFoundError(f"Input workbook not found: {input_path}")

    df = pd.read_excel(input_path)
    required_columns = {"NOME_COMPLETO_PORTA", "LATITUDE", "LONGITUDE"}
    missing = required_columns.difference(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _init_output_csv(output_path)

    detector = DoorNumberDetector()
    total   = len(df)
    n_match = 0

    try:
        for index, row in df.iterrows():
            nome      = str(row.get("NOME_COMPLETO_PORTA", "") or "").strip()
            latitude  = row.get("LATITUDE")
            longitude = row.get("LONGITUDE")

            if pd.isna(latitude) or pd.isna(longitude):
                prediction = None
                confidence = 0
                prediction_found = False
            else:
                result     = detector.detect_door_number(float(latitude), float(longitude))
                prediction_found  = result.get("success", False)
                prediction = result.get("door_number") if prediction_found else None
                confidence = result.get("confidence", 0)

            match = prediction is not None and nome != "" and prediction == nome
            if match:
                n_match += 1

            _append_row_to_csv(
                output_path,
                [latitude, longitude, nome, prediction, confidence, prediction_found, match],
            )

            status = "✓ MATCH" if match else ("✗ MISMATCHED" if prediction_found else "? PREDICTION NOT FOUND")
            print(f"[{index + 1}/{total}] expected={nome!r} predicted={prediction!r} ({confidence}%) {status}")

    finally:
        detector.close()

    print(f"\nDone: {n_match}/{total} matched ({100 * n_match / total:.1f}%)")
    print(f"Saved to {output_path}")
    return output_path


def main():
    # ── Batch mode ─────────────────────────────────────────────────────────────
    project_root   = Path(__file__).resolve().parent
    input_workbook = project_root / "Portas_Teste.xlsx"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_csv = project_root / f"Portas_Teste_predictions_{ts}.csv"
    if input_workbook.exists():
        run_batch_predictions(input_workbook, output_csv)
        return

    # ── Single coordinate mode ─────────────────────────────────────────────────
    detector = DoorNumberDetector()

    #latitude, longitude = 41.256331, -8.645468        # 36 simple
    #latitude, longitude = 38.024463, -7.712959        # 17
    #latitude, longitude = 39.701068, -8.910996        # 14 very difficult
    #latitude, longitude = 41.088368507582, -6.81537789305259  # 36
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
    #latitude, longitude = 41.0883085036578, -6.81482510571329 #its giving 2024, ver se é por causa da cena das occurrencias (ideia se estivermos entre dois numeros com a mesma confidence, se for um numero normal ve s entre as ocorrencias dps, se for tp um numero e o outro é 202, 2024, 2026... dar prioridade ao outro)
    #latitude, longitude = 41.0894836100001, -6.81600447899996 #its giving casa ao lado 78, mas a rua em si no maps ta estranha (ele tentou mas nem no maps dá para visualizar, é aceitar)
    #latitude, longitude = 41.0885644410001, -6.81447433499994 #its giving casa ao lado, 22, mas ambos estão igualmente perto, ver o que se pode fazer!
    #latitude, longitude = 41.0886594190001, -6.81427094499998 #its giving 20 (casa ao lado?) ver o que se passou com esta

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
