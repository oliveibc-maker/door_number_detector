import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

from door_number_detector.core.config import Config
from door_number_detector.core.sources import query_sqlserver, read_excel_any, read_spreadsheetml

SPREADSHEET_NAMESPACE = "urn:schemas-microsoft-com:office:spreadsheet"


class ExcelSourceTests(unittest.TestCase):
    def test_spreadsheetml_is_parsed_and_short_rows_are_padded(self):
        xml = f"""<?xml version="1.0"?>
        <Workbook xmlns="{SPREADSHEET_NAMESPACE}"><Worksheet><Table>
          <Row><Cell><Data>NOME_COMPLETO_PORTA</Data></Cell>
            <Cell><Data>LATITUDE</Data></Cell><Cell><Data>LONGITUDE</Data></Cell></Row>
          <Row><Cell><Data>12A</Data></Cell><Cell><Data>41.2</Data></Cell></Row>
        </Table></Worksheet></Workbook>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "addresses.xls"
            path.write_text(xml, encoding="utf-8")
            dataframe = read_spreadsheetml(path)

        self.assertEqual(dataframe.loc[0, "NOME_COMPLETO_PORTA"], "12A")
        self.assertEqual(dataframe.loc[0, "LATITUDE"], "41.2")
        self.assertEqual(dataframe.loc[0, "LONGITUDE"], "")

    def test_spreadsheetml_rejects_entity_declarations(self):
        xml = (
            f'<!DOCTYPE Workbook [<!ENTITY injected "unsafe">]>'
            f'<Workbook xmlns="{SPREADSHEET_NAMESPACE}">&injected;</Workbook>'
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "addresses.xls"
            path.write_text(xml, encoding="utf-8")
            with self.assertRaises(Exception):
                read_spreadsheetml(path)

    def test_binary_excel_engine_is_selected_by_extension(self):
        for suffix, expected_engine in ((".xlsx", "openpyxl"), (".xls", "xlrd")):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / f"addresses{suffix}"
                path.write_bytes(b"not parsed because read_excel is mocked")
                with patch("pandas.read_excel", return_value=pd.DataFrame()) as read_excel:
                    read_excel_any(path)
                read_excel.assert_called_once_with(path, engine=expected_engine)


class SqlServerSourceTests(unittest.TestCase):
    def test_filter_is_allowlisted_and_value_is_parameterized(self):
        connection = Mock()
        pyodbc_module = types.ModuleType("pyodbc")
        pyodbc_module.connect = Mock(return_value=connection)
        config = types.SimpleNamespace(
            src_db_trusted=False,
            src_db_driver="ODBC Driver 18 for SQL Server",
            src_db_server="db.example;Trusted_Connection=yes",
            src_db_database="GEO_DB",
            src_db_user="user",
            src_db_password="p;ass}word",
            src_db_encrypt=None,
            src_db_trust_server_certificate=None,
        )
        dataframe = pd.DataFrame(
            {
                "NOME_COMPLETO_PORTA": ["12"],
                "LATITUDE": ["41.2"],
                "LONGITUDE": ["-8.6"],
            }
        )

        with (
            patch.dict(sys.modules, {"pyodbc": pyodbc_module}),
            patch("pandas.read_sql", return_value=dataframe) as read_sql,
        ):
            result = query_sqlserver("rua", "1; DROP TABLE source", config)

        connection_string = pyodbc_module.connect.call_args.args[0]
        self.assertIn("SERVER={db.example;Trusted_Connection=yes};", connection_string)
        self.assertIn("PWD={p;ass}}word};", connection_string)
        self.assertIn("WHERE ID_RUA = ?", read_sql.call_args.args[0])
        self.assertEqual(read_sql.call_args.kwargs["params"], ["1; DROP TABLE source"])
        self.assertTrue(connection.close.called)
        self.assertEqual(result.loc[0, "LATITUDE"], 41.2)

    def test_tls_options_are_added_only_when_configured(self):
        pyodbc_module = types.ModuleType("pyodbc")
        pyodbc_module.connect = Mock(return_value=Mock())
        config = types.SimpleNamespace(
            src_db_trusted=True,
            src_db_driver="ODBC Driver 18 for SQL Server",
            src_db_server="db.example",
            src_db_database="GEO_DB",
            src_db_encrypt="yes",
            src_db_trust_server_certificate="yes",
        )
        dataframe = pd.DataFrame(
            {"NOME_COMPLETO_PORTA": ["1"], "LATITUDE": ["41.2"], "LONGITUDE": ["-8.6"]}
        )

        with (
            patch.dict(sys.modules, {"pyodbc": pyodbc_module}),
            patch("pandas.read_sql", return_value=dataframe),
        ):
            query_sqlserver("rua", "1", config)

        connection_string = pyodbc_module.connect.call_args.args[0]
        self.assertIn("Trusted_Connection=yes;", connection_string)
        self.assertIn("Encrypt=yes;", connection_string)
        self.assertIn("TrustServerCertificate=yes;", connection_string)

    @patch("door_number_detector.core.config.load_dotenv")
    def test_tls_settings_parse_environment_values(self, _load_dotenv):
        with patch.dict(
            "os.environ",
            {"SRC_DB_ENCRYPT": "False", "SRC_DB_TRUST_SERVER_CERTIFICATE": "true"},
            clear=True,
        ):
            config = Config(env_file=None)
            self.assertEqual(config.src_db_encrypt, "no")
            self.assertEqual(config.src_db_trust_server_certificate, "yes")
        with patch.dict("os.environ", {"SRC_DB_ENCRYPT": "maybe"}, clear=True):
            with self.assertRaises(ValueError):
                Config(env_file=None).src_db_encrypt
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(Config(env_file=None).src_db_encrypt)

    @patch("door_number_detector.core.config.load_dotenv")
    def test_database_path_defaults_under_app_data_dir(self, _load_dotenv):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"APP_DATA_DIR": directory}, clear=True):
                self.assertEqual(
                    Config(env_file=None).database_path,
                    Path(directory).resolve() / "door_detector.db",
                )
            custom_path = Path(directory) / "custom.db"
            with patch.dict(
                "os.environ",
                {"APP_DATA_DIR": directory, "DATABASE_PATH": str(custom_path)},
                clear=True,
            ):
                self.assertEqual(Config(env_file=None).database_path, custom_path.resolve())

    def test_unknown_filter_is_rejected_before_connecting(self):
        config = types.SimpleNamespace()
        with self.assertRaises(ValueError):
            query_sqlserver("unlisted", "value", config)


if __name__ == "__main__":
    unittest.main()
