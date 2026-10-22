"""Excel and SpreadsheetML input readers."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


def read_spreadsheetml(path: Path) -> pd.DataFrame:
    """Parse the XML spreadsheet format emitted by the browser template."""
    from defusedxml import ElementTree as ET
    import pandas as pd

    namespace = "urn:schemas-microsoft-com:office:spreadsheet"
    rows_data = []
    for row_element in ET.parse(str(path)).getroot().iter(f"{{{namespace}}}Row"):
        row_values = []
        for cell_element in row_element.findall(f"{{{namespace}}}Cell"):
            data_element = cell_element.find(f"{{{namespace}}}Data")
            row_values.append(
                data_element.text if data_element is not None and data_element.text else ""
            )
        rows_data.append(row_values)
    if not rows_data:
        raise ValueError("No data found in SpreadsheetML file")
    headers = rows_data[0]
    data = [row + [""] * (len(headers) - len(row)) for row in rows_data[1:]]
    return pd.DataFrame(data, columns=headers)


def read_excel_any(path: Path) -> pd.DataFrame:
    """Read modern Excel, binary XLS, or browser-generated SpreadsheetML."""
    import pandas as pd

    path = Path(path)
    with path.open("rb") as workbook:
        preview = workbook.read(200).lstrip()
    if preview.startswith(b"<?xml") or preview.startswith(b"<Workbook"):
        return read_spreadsheetml(path)
    engine = "openpyxl" if path.suffix.lower() == ".xlsx" else "xlrd"
    return pd.read_excel(path, engine=engine)