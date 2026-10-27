"""Input adapters for spreadsheets and SQL Server."""

from door_number_detector.core.sources.excel import read_excel_any, read_spreadsheetml
from door_number_detector.core.sources.sqlserver import (
    FILTER_COLUMNS,
    SourceFilter,
    query_sqlserver,
)

__all__ = [
    "FILTER_COLUMNS",
    "SourceFilter",
    "query_sqlserver",
    "read_excel_any",
    "read_spreadsheetml",
]
