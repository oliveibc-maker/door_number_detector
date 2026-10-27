"""SQL Server input adapter."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    import pandas as pd

    from door_number_detector.core.config import Config


logger = logging.getLogger(__name__)

SourceFilter = Literal["localidade", "freguesia", "concelho", "rua"]

FILTER_COLUMNS = {
    "localidade": "NOME_LOCALIDADE",
    "freguesia": "NOME_FREGUESIA",
    "concelho": "NOME_CONCELHO",
    "rua": "ID_RUA",
}


def _odbc_value(value: str) -> str:
    return "{" + str(value).replace("}", "}}") + "}"


def query_sqlserver(
    filter_by: SourceFilter,
    filter_value: str,
    config: Config,
) -> pd.DataFrame:
    import pandas as pd

    if filter_by not in FILTER_COLUMNS:
        raise ValueError(f"filter_by must be one of {list(FILTER_COLUMNS)}, got '{filter_by}'")

    column = FILTER_COLUMNS[filter_by]
    import pyodbc

    connection_string = (
        f"DRIVER={_odbc_value(config.src_db_driver)};"
        f"SERVER={_odbc_value(config.src_db_server)};"
        f"DATABASE={_odbc_value(config.src_db_database)};"
    )
    if config.src_db_trusted:
        connection_string += "Trusted_Connection=yes;"
    else:
        connection_string += (
            f"UID={_odbc_value(config.src_db_user)};PWD={_odbc_value(config.src_db_password)};"
        )
    if config.src_db_encrypt:
        connection_string += f"Encrypt={config.src_db_encrypt};"
    if config.src_db_trust_server_certificate:
        connection_string += f"TrustServerCertificate={config.src_db_trust_server_certificate};"

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

    logger.info(
        "Connecting to source database %s / %s", config.src_db_server, config.src_db_database
    )
    logger.info("Querying source rows by %s", column)
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
    logger.info("Loaded %s rows for %s", len(dataframe), column)
    return dataframe
