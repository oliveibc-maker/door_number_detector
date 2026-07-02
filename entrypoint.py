"""Project entry point for running the detector from the repository root."""

from pathlib import Path

import pandas as pd

from core.detector import DoorNumberDetector


def build_prediction_dataframe(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(
        rows,
        columns=["NOME_COMPLETO_PORTA", "LATITUDE", "LONGITUDE", "PREDICTION"],
    )


def run_batch_predictions(input_path: str | Path, output_path: str | Path) -> Path:
    input_path = Path(input_path)
    output_path = Path(output_path)

    if not input_path.exists():
        raise FileNotFoundError(f"Input workbook not found: {input_path}")

    df = pd.read_excel(input_path)
    required_columns = {"NOME_COMPLETO_PORTA", "LATITUDE", "LONGITUDE"}
    missing_columns = required_columns.difference(df.columns)
    if missing_columns:
        raise ValueError(f"Missing required columns: {sorted(missing_columns)}")

    detector = DoorNumberDetector()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_rows: list[dict] = []

    try:
        for index, row in df.iterrows():
            latitude = row.get("LATITUDE")
            longitude = row.get("LONGITUDE")
            if pd.isna(latitude) or pd.isna(longitude):
                prediction = None
            else:
                result = detector.detect_door_number(float(latitude), float(longitude))
                prediction = result.get("door_number") if result.get("success") else None

            output_row = {
                "NOME_COMPLETO_PORTA": row.get("NOME_COMPLETO_PORTA"),
                "LATITUDE": latitude,
                "LONGITUDE": longitude,
                "PREDICTION": prediction,
            }
            output_rows.append(output_row)
            pd.DataFrame([output_row], columns=["NOME_COMPLETO_PORTA", "LATITUDE", "LONGITUDE", "PREDICTION"]).to_excel(
                output_path,
                index=False,
                header=not output_path.exists(),
                mode="a" if output_path.exists() else "w",
            )
            print(f"[{index + 1}/{len(df)}] {row.get('NOME_COMPLETO_PORTA')} -> {prediction}")
    finally:
        detector.close()

    print(f"Saved predictions to {output_path}")
    return output_path


def main():
    # project_root = Path(__file__).resolve().parent
    # input_workbook = project_root / "Portas_Teste.xlsx"
    # output_workbook = project_root / "Portas_Teste_predictions.xlsx"

    # if input_workbook.exists():
    #     run_batch_predictions(input_workbook, output_workbook)
    #     return

    detector = DoorNumberDetector()
    #latitude, longitude = 41.256331, -8.645468  # 36 simple
    #latitude, longitude = 38.024463, -7.712959  # 17 
    #latitude, longitude = 39.701068, -8.910996 #14 very difficult
    #latitude, longitude = 41.088368507582, -6.81537789305259 # 36
    #latitude, longitude = 41.0893759030001, -6.81537262699993 #131
    #latitude, longitude = 41.088695435, -6.81420591799997 #17/19?
    latitude, longitude = 41.088503121, -6.81459559899997
    #latitude, longitude = 41.0883351100001, -6.81493620399993


    preview_image = detector.street_view.get_image(latitude, longitude)
    if preview_image is not None:
        print("Displaying fetched Street View image...")
        preview_image.show()

    result = detector.detect_door_number(latitude, longitude)

    print("\n" + "=" * 50)
    print("DETECTION RESULT")
    print("=" * 50)
    print(f"Latitude: {result['latitude']}")
    print(f"Longitude: {result['longitude']}")
    print(f"Door number: {result.get('door_number', 'N/A')}")
    print(f"Confidence: {result.get('confidence', 0)}%")
    print(f"Status: {'✓ Success' if result['success'] else '✗ Error'}")
    if not result["success"]:
        print(f"Error: {result.get('error', 'Unknown')}")
    print("=" * 50)

    detector.close()


if __name__ == "__main__":
    main()
