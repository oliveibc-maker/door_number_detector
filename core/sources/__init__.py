"""Input adapters for spreadsheets and SQL Server."""

from core.sources.excel import read_excel_any, read_spreadsheetml
from core.sources.sqlserver import query_sqlserver

__all__ = ["query_sqlserver", "read_excel_any", "read_spreadsheetml"]