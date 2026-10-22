"""Input adapters for spreadsheets and SQL Server."""

from door_number_detector.core.sources.excel import read_excel_any, read_spreadsheetml
from door_number_detector.core.sources.sqlserver import query_sqlserver

__all__ = ["query_sqlserver", "read_excel_any", "read_spreadsheetml"]