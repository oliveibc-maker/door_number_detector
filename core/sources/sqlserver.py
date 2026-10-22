"""SQL Server input adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import pandas as pd
    from core.config import Config


FILTER_COLUMNS = {
    "localidade": "NOME_LOCALIDADE",
    "freguesia": "NOME_FREGUESIA",
    "concelho": "NOME_CONCELHO",
    "rua": "ID_RUA",
}


def _odbc_value(value: str) -> str:
    return "{" + str(value).replace("}", "}}") + "}"


def query_sqlserver(
    filter_by: Literal["localidade", "freguesia", "concelho", "rua"],
    filter_value: str,
    config: Config,
) -> pd.DataFrame:
    import pandas as pd

    if filter_by not in FILTER_COLUMNS:
        raise ValueError(
            f"filter_by must be one of {list(FILTER_COLUMNS)}, got '{filter_by}'"
        )

    column = FILTER_COLUMNS[filter_by]
    import pyodbc

    if config.src_db_trusted:
        connection_string = (
            f"DRIVER={_odbc_value(config.src_db_driver)};"
            f"SERVER={_odbc_value(config.src_db_server)};"
            f"DATABASE={_odbc_value(config.src_db_database)};"
            "Trusted_Connection=yes;"
        )
    else:
        connection_string = (
            f"DRIVER={_odbc_value(config.src_db_driver)};"
            f"SERVER={_odbc_value(config.src_db_server)};"
            f"DATABASE={_odbc_value(config.src_db_database)};"
            f"UID={_odbc_value(config.src_db_user)};"
            f"PWD={_odbc_value(config.src_db_password)};"
        )

    query = f"""
        SELECT
            NOME_COMPLETO_PORTA,
            LAT  AS LATITUDE,
            LON  AS LONGITUDE
        FROM [GEO_DB].[SGM].[SGM_PORTA]
        WHERE {column} = ?
          AND LAT IS NOT NULL
          AND LON IS NOT NULL
          AND ESTADO_PORTA NOT IN (N'Sinónimo', N'Apagado')
    """

    print(f"Connecting to {config.src_db_server} / {config.src_db_database} ...")
    print(f"Filter: {column} = '{filter_value}'")
    connection = pyodbc.connect(connection_string, timeout=30)
    try:
        dataframe = pd.read_sql(query, connection, params=[filter_value])
    finally:
        connection.close()

    if dataframe.empty:
        raise ValueError(f"No rows found for {column} = '{filter_value}'")

    dataframe["LATITUDE"] = pd.to_numeric(dataframe["LATITUDE"], errors="coerce")
    dataframe["LONGITUDE"] = pd.to_numeric(dataframe["LONGITUDE"], errors="coerce")
    dataframe = dataframe.dropna(subset=["LATITUDE", "LONGITUDE"]).reset_index(drop=True)
    print(f"Loaded {len(dataframe)} rows for {column} = '{filter_value}'")
    return dataframe