import base64
import importlib
import json
import sys
import tempfile
import threading
import types
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch
from http.server import ThreadingHTTPServer


def _load_app_with_stubs():
    detector_module = types.ModuleType("core.detector")
    metrics_module = types.ModuleType("core.metrics")
    entrypoint_module = types.ModuleType("entrypoint")

    class Detector:
        def __init__(self):
            self.db = types.SimpleNamespace(get_results=lambda limit: [])

    class Metrics:
        def summary(self):
            return {}

        def save_json(self, path):
            return None

    class Workbook:
        empty = False
        columns = {"NOME_COMPLETO_PORTA", "LATITUDE", "LONGITUDE"}

        def __len__(self):
            return 1

    detector_module.DoorNumberDetector = Detector
    metrics_module.RunMetrics = Metrics
    entrypoint_module.run_batch_predictions = lambda *args, **kwargs: None
    entrypoint_module._query_sqlserver = lambda *args, **kwargs: None
    entrypoint_module._run_detection_on_df = lambda *args, **kwargs: None
    entrypoint_module._read_excel_any = lambda path: Workbook()

    with patch.dict(sys.modules, {
        "core.detector": detector_module,
        "core.metrics": metrics_module,
        "entrypoint": entrypoint_module,
    }):
        return importlib.import_module("web.app")


class WebHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _load_app_with_stubs()
        cls.auth = "Basic " + base64.b64encode(b"tester:secret").decode("ascii")

    def setUp(self):
        self.upload_dir = tempfile.TemporaryDirectory()
        self.original_upload_dir = self.app.UPLOAD_DIR
        self.app.UPLOAD_DIR = Path(self.upload_dir.name)
        self.app._state.update({
            "running": False,
            "csv_path": None,
            "total": 0,
            "error": None,
            "cancel_event": None,
            "metrics": None,
        })
        handler = type(
            "TestDoorNumberRequestHandler",
            (self.app.DoorNumberRequestHandler,),
            {
                "auth_username": "tester",
                "auth_password": "secret",
                "csrf_token": "test-csrf-token",
                "max_upload_bytes": 8,
            },
        )
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        self.app.UPLOAD_DIR = self.original_upload_dir
        self.upload_dir.cleanup()

    def _request(self, method, path, body=None, headers=None):
        request_headers = {"Authorization": self.auth}
        if headers:
            request_headers.update(headers)
        connection = HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def test_authentication_and_csrf_are_required(self):
        connection = HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request("GET", "/")
        response = connection.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        connection.close()

        status, _ = self._request("POST", "/api/reset")
        self.assertEqual(status, 403)

        status, _ = self._request(
            "POST", "/api/reset", headers={"X-CSRF-Token": "test-csrf-token"}
        )
        self.assertEqual(status, 200)

    def test_html_receives_runtime_csrf_token_and_upload_limit(self):
        status, body = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'content="test-csrf-token"', body)
        self.assertIn(b'content="8"', body)
        self.assertNotIn(b"__CSRF_TOKEN__", body)

        status, body = self._request("GET", "/api/csrf")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["csrf_token"], "test-csrf-token")

    def test_upload_rejects_unsupported_extension_and_oversize_body(self):
        headers = {"X-CSRF-Token": "test-csrf-token"}
        status, _ = self._request(
            "POST", "/api/upload?filename=../../entrypoint.py", b"data", headers
        )
        self.assertEqual(status, 400)
        self.assertEqual(list(Path(self.upload_dir.name).iterdir()), [])

        status, _ = self._request(
            "POST", "/api/upload?filename=doors.xlsx", b"123456789", headers
        )
        self.assertEqual(status, 413)
        self.assertEqual(list(Path(self.upload_dir.name).iterdir()), [])

    def test_valid_upload_uses_generated_path_outside_project_root(self):
        status, _ = self._request(
            "POST",
            "/api/upload?filename=../../entrypoint.xlsx",
            b"data",
            {"X-CSRF-Token": "test-csrf-token"},
        )
        self.assertEqual(status, 200)
        uploaded = list(Path(self.upload_dir.name).iterdir())
        self.assertEqual(len(uploaded), 1)
        self.assertEqual(uploaded[0].parent, Path(self.upload_dir.name))
        self.assertNotEqual(uploaded[0].name, "entrypoint.xlsx")

    def test_server_factory_binds_loopback_and_rejects_remote_hosts(self):
        config = types.SimpleNamespace(
            web_username="tester",
            web_password="secret",
            web_host="127.0.0.1",
            web_port=8080,
            max_upload_bytes=1024,
        )
        with patch.object(self.app, "Config", return_value=config):
            server = self.app.create_server(port=0)
            try:
                self.assertIn(server.server_address[0], {"127.0.0.1", "::1"})
                self.assertNotEqual(server.server_port, 8080)
            finally:
                server.server_close()

            with self.assertRaises(RuntimeError):
                self.app.create_server(host="0.0.0.0", port=8080)


if __name__ == "__main__":
    unittest.main()