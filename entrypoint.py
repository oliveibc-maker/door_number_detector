"""CLI entry point for processing door-number batches."""

import os
import re
import sys
from datetime import datetime
from pathlib import Path

from core.batch import run_excel_batch, run_sqlserver_batch
from core.config import Config

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


run_batch_predictions = run_excel_batch
run_from_sqlserver = run_sqlserver_batch


def main() -> None:
    project_root = Path(__file__).resolve().parent
    output_dir = Config().data_dir / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    source = os.getenv("SOURCE", "sqlserver").strip().lower()
    filter_by = os.getenv("FILTER_BY", "rua").strip().lower()
    filter_value = os.getenv("FILTER_VALUE", "").strip()

    if source == "sqlserver":
        if not filter_value:
            raise ValueError("Set FILTER_VALUE to select SQL Server source rows.")
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", filter_value).strip("_")[:80] or "filter"
        output_csv = output_dir / f"predictions_{filter_by}_{safe_name}_{timestamp}.csv"
        run_sqlserver_batch(filter_by, filter_value, output_csv)
        return

    if source == "excel":
        input_workbook = Path(os.getenv("INPUT_EXCEL", str(project_root / "input.xlsx")))
        output_csv = output_dir / f"predictions_{timestamp}.csv"
        run_excel_batch(input_workbook, output_csv)
        return

    raise ValueError("SOURCE must be either 'sqlserver' or 'excel'.")


if __name__ == "__main__":
    main()