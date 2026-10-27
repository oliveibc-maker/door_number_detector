import base64
import importlib
import io
import json
import socket
import sys
import tempfile
import threading
import types
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from door_number_detector.core.models import DetectionResult


def _load_app_with_stubs():
    metrics_module = types.ModuleType("door_number_detector.core.metrics")
    batch_module = types.ModuleType("door_number_detector.core.batch")
    sources_module = types.ModuleType("door_number_detector.core.sources")

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

    metrics_module.RunMetrics = Metrics
    batch_module.run_detection_batch = lambda *args, **kwargs: None
    batch_module.run_excel_batch = lambda *args, **kwargs: None
    batch_module.run_sqlserver_batch = lambda *args, **kwargs: None
    batch_module.InvalidExcelWorkbookError = type("InvalidExcelWorkbookError", (ValueError,), {})
    sources_module.FILTER_COLUMNS = {
        "localidade": "NOME_LOCALIDADE",
        "freguesia": "NOME_FREGUESIA",
        "concelho": "NOME_CONCELHO",
        "rua": "ID_RUA",
    }
    sources_module.SourceFilter = str
    sources_module.query_sqlserver = lambda *args, **kwargs: None
    sources_module.read_excel_any = lambda path: Workbook()
    batch_module.load_excel_dataframe = lambda path: sources_module.read_excel_any(path)

    with patch.dict(
        sys.modules,
        {
            "door_number_detector.core.metrics": metrics_module,
            "door_number_detector.core.batch": batch_module,
            "door_number_detector.core.sources": sources_module,
        },
    ):
        return importlib.import_module("door_number_detector.web.app")


class WebHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _load_app_with_stubs()
        cls.auth = "Basic " + base64.b64encode(b"tester:secret").decode("ascii")

    def setUp(self):
        self.upload_dir = tempfile.TemporaryDirectory()
        self.original_detector = self.app._detector
        self.app._detector = types.SimpleNamespace(
            db=types.SimpleNamespace(get_results=lambda limit: []),
            detect_door_number=lambda *args, **kwargs: {},
            close=lambda: None,
        )
        self.original_upload_dir = self.app.UPLOAD_DIR
        self.original_output_dir = self.app.OUTPUT_DIR
        self.app.UPLOAD_DIR = Path(self.upload_dir.name)
        self.app.OUTPUT_DIR = Path(self.upload_dir.name) / "outputs"
        self.app._state.update(
            {
                "running": False,
                "csv_path": None,
                "total": 0,
                "error": None,
                "cancel_event": None,
                "metrics": None,
            }
        )
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
        self.server = self.app.DoorNumberHTTPServer(("127.0.0.1", 0), handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        self.app._detector = self.original_detector
        self.app.UPLOAD_DIR = self.original_upload_dir
        self.app.OUTPUT_DIR = self.original_output_dir
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

        status, _ = self._request("POST", "/api/reset", headers={"X-CSRF-Token": "test-csrf-token"})
        self.assertEqual(status, 200)

    def test_analysis_errors_return_generic_json_and_release_the_job_slot(self):
        with patch.object(
            self.app._detector,
            "detect_door_number",
            side_effect=RuntimeError("internal detail"),
            create=True,
        ):
            status, body = self._request(
                "POST",
                "/api/analyze",
                b"latitude=41.0&longitude=-8.0",
                {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-CSRF-Token": "test-csrf-token",
                },
            )
        self.assertEqual(status, 500)
        self.assertIn(b"Detection failed", body)
        self.assertNotIn(b"internal detail", body)
        self.assertFalse(self.app._state["running"])

    def test_analysis_serializes_the_typed_result_contract(self):
        typed_result = DetectionResult(
            success=True,
            latitude=41.0,
            longitude=-8.0,
            door_number="12A",
            confidence=90,
            heading=10,
            pitch=0,
        )
        calls = []

        def analyze(*args, **kwargs):
            calls.append((args, kwargs))
            return typed_result

        self.app._detector.detect_door_number = analyze
        status, body = self._request(
            "POST",
            "/api/analyze",
            b"latitude=41.0&longitude=-8.0",
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "X-CSRF-Token": "test-csrf-token",
            },
        )

        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["door_number"], "12A")
        self.assertEqual(payload["outcome"], "found")
        self.assertIn("observation", payload)
        self.assertIn("timestamp", payload)
        self.assertIsNone(payload["error"])
        self.assertIsNone(calls[0][0][2])

    def test_detector_factory_is_lazy_and_constructs_once(self):
        self.assertIsNone(self.original_detector)
        self.app._detector = None
        detector_module = types.ModuleType("door_number_detector.core.detector")
        created = []

        class LazyDetector:
            pass

        def build_detector():
            instance = LazyDetector()
            created.append(instance)
            return instance

        detector_module.DoorNumberDetector = build_detector
        try:
            with patch.dict(sys.modules, {"door_number_detector.core.detector": detector_module}):
                first = self.app.get_detector()
                second = self.app.get_detector()
            self.assertIs(first, second)
            self.assertEqual(len(created), 1)
        finally:
            self.app._detector = self.original_detector

    def test_html_receives_runtime_csrf_token_and_upload_limit(self):
        status, body = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'content="test-csrf-token"', body)
        self.assertIn(b'content="8"', body)
        self.assertNotIn(b"__CSRF_TOKEN__", body)

        status, body = self._request("GET", "/api/csrf")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["csrf_token"], "test-csrf-token")

    def test_docker_healthcheck_accepts_the_running_authenticated_server(self):
        from door_number_detector.web.healthcheck import main

        with patch.dict(
            "os.environ",
            {
                "WEB_USERNAME": "tester",
                "WEB_PASSWORD": "secret",
                "WEB_PORT": str(self.server.server_port),
            },
        ):
            main()

    def test_docker_healthcheck_uses_and_formats_configured_ipv6_host(self):
        from door_number_detector.web import healthcheck

        response = types.SimpleNamespace(status=200, read=lambda: b'{"csrf_token":"token"}')
        with (
            patch.dict(
                "os.environ",
                {
                    "WEB_USERNAME": "tester",
                    "WEB_PASSWORD": "secret",
                    "WEB_HOST": "::1",
                    "WEB_PORT": "8080",
                },
            ),
            patch.object(healthcheck, "urlopen") as urlopen,
        ):
            urlopen.return_value.__enter__.return_value = response
            healthcheck.main()

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://[::1]:8080/api/csrf")

    def test_server_closes_incomplete_requests_after_socket_timeout(self):
        self.server.request_timeout = 0.1
        connection = socket.create_connection(("127.0.0.1", self.server.server_port))
        connection.settimeout(2)
        try:
            connection.sendall(b"GET / HTTP/1.1\r\n")
            self.assertEqual(connection.recv(1), b"")
        finally:
            connection.close()

    def test_upload_rejects_unsupported_extension_and_oversize_body(self):
        headers = {"X-CSRF-Token": "test-csrf-token"}
        status, _ = self._request(
            "POST", "/api/upload?filename=../../entrypoint.py", b"data", headers
        )
        self.assertEqual(status, 400)
        self.assertEqual(list(Path(self.upload_dir.name).iterdir()), [])

        status, _ = self._request("POST", "/api/upload?filename=doors.xlsx", b"123456789", headers)
        self.assertEqual(status, 413)
        self.assertEqual(list(Path(self.upload_dir.name).iterdir()), [])
        self.assertFalse(self.app._state["running"])

        with patch.object(
            self.app,
            "load_excel_dataframe",
            side_effect=self.app.InvalidExcelWorkbookError("invalid"),
        ):
            status, _ = self._request("POST", "/api/upload?filename=empty.xlsx", b"data", headers)
        self.assertEqual(status, 400)
        self.assertFalse(self.app._state["running"])
        self.assertEqual(list(Path(self.upload_dir.name).iterdir()), [])

    def test_upload_rejects_invalid_workbook_and_removes_it(self):
        with patch.object(
            self.app,
            "load_excel_dataframe",
            side_effect=self.app.InvalidExcelWorkbookError(
                "The spreadsheet is empty or missing required columns."
            ),
        ):
            status, body = self._request(
                "POST",
                "/api/upload?filename=doors.xlsx",
                b"data",
                {"X-CSRF-Token": "test-csrf-token"},
            )

        self.assertEqual(status, 400)
        self.assertIn(b"missing required columns", body)
        self.assertEqual(list(Path(self.upload_dir.name).iterdir()), [])

    def test_sql_worker_updates_total_from_shared_batch_runner(self):
        cancel_event = self.app._claim_job(total=0)
        self.assertIsNotNone(cancel_event)

        def run_shared_batch(*args, on_rows_loaded, **kwargs):
            on_rows_loaded(3)

        with patch.object(self.app, "run_sqlserver_batch", side_effect=run_shared_batch):
            self.app._run_sqlserver_batch(
                "rua",
                "123",
                Path(self.upload_dir.name) / "sql.csv",
                cancel_event,
                self.app.RunMetrics(),
            )

        self.assertEqual(self.app._state["total"], 3)
        self.assertFalse(self.app._state["running"])

    def test_valid_upload_uses_generated_path_outside_project_root(self):
        worker_started = threading.Event()
        release_worker = threading.Event()
        worker_finished = threading.Event()
        uploaded_paths = []
        parsed_workbooks = []
        original_finish = self.app._finish_job

        def hold_worker(path, *args, **kwargs):
            uploaded_paths.append(Path(path))
            parsed_workbooks.append(kwargs.get("dataframe"))
            worker_started.set()
            release_worker.wait(10)

        def signal_finish(cancel_event, error=None):
            original_finish(cancel_event, error)
            worker_finished.set()

        try:
            with (
                patch.object(
                    self.app, "load_excel_dataframe", wraps=self.app.load_excel_dataframe
                ) as load_workbook,
                patch.object(self.app, "run_excel_batch", side_effect=hold_worker),
                patch.object(self.app, "_finish_job", side_effect=signal_finish),
            ):
                status, _ = self._request(
                    "POST",
                    "/api/upload?filename=../../entrypoint.xlsx",
                    b"data",
                    {"X-CSRF-Token": "test-csrf-token"},
                )
                self.assertEqual(status, 200)
                self.assertTrue(worker_started.wait(2))
                uploaded_path = uploaded_paths[0]
                self.assertEqual(uploaded_path.parent, Path(self.upload_dir.name))
                self.assertRegex(uploaded_path.stem, r"^[0-9a-f]{32}$")
                self.assertTrue(uploaded_path.exists())
                load_workbook.assert_called_once_with(uploaded_path)
                self.assertIsNotNone(parsed_workbooks[0])
                self.assertFalse(self.app._state["worker"].daemon)

                status, _ = self._request(
                    "POST",
                    "/api/run_sqlserver",
                    b"filter_by=rua&filter_value=123",
                    {
                        "Content-Type": "application/x-www-form-urlencoded",
                        "X-CSRF-Token": "test-csrf-token",
                    },
                )
                self.assertEqual(status, 409)

                status, _ = self._request(
                    "POST", "/api/reset", headers={"X-CSRF-Token": "test-csrf-token"}
                )
                self.assertEqual(status, 202)
                self.assertTrue(self.app._state["running"])
                self.assertTrue(self.app._state["cancel_event"].is_set())
                self.assertTrue(uploaded_path.exists())
                release_worker.set()
                self.assertTrue(worker_finished.wait(2))
                self.assertFalse(uploaded_path.exists())
                self.assertFalse(self.app._state["running"])
        finally:
            release_worker.set()

    def test_upload_reserves_job_before_parsing_workbook(self):
        parse_started = threading.Event()
        release_parse = threading.Event()
        parse_count = 0
        results = []
        original_reader = self.app.load_excel_dataframe

        def blocking_reader(path):
            nonlocal parse_count
            parse_count += 1
            if parse_count == 1:
                parse_started.set()
                release_parse.wait(2)
            return original_reader(path)

        def upload():
            results.append(
                self._request(
                    "POST",
                    "/api/upload?filename=doors.xlsx",
                    b"data",
                    {"X-CSRF-Token": "test-csrf-token"},
                )
            )

        first_upload = threading.Thread(target=upload)
        try:
            with patch.object(self.app, "load_excel_dataframe", side_effect=blocking_reader):
                first_upload.start()
                self.assertTrue(parse_started.wait(2))
                status, _ = self._request(
                    "POST",
                    "/api/upload?filename=second.xlsx",
                    b"data",
                    {"X-CSRF-Token": "test-csrf-token"},
                )
                self.assertEqual(status, 409)
                self.assertEqual(parse_count, 1)
                release_parse.set()
                first_upload.join(2)
                self.assertFalse(first_upload.is_alive())
            self.assertEqual(results[0][0], 200)
        finally:
            release_parse.set()
            first_upload.join(2)

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
                self.assertFalse(server.daemon_threads)
            finally:
                server.server_close()

            with self.assertRaises(RuntimeError):
                self.app.create_server(host="0.0.0.0", port=8080)

    def test_only_one_concurrent_request_can_claim_the_detector(self):
        barrier = threading.Barrier(3)
        result_lock = threading.Lock()
        claims = []

        def compete():
            barrier.wait()
            claim = self.app._claim_job()
            with result_lock:
                claims.append(claim)

        threads = [threading.Thread(target=compete) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        winners = [claim for claim in claims if claim is not None]
        self.assertEqual(len(winners), 1)
        self.app._finish_job(winners[0])


def _load_real_app():
    with patch.dict(sys.modules):
        return importlib.import_module("door_number_detector.web.app")


def _workbook_bytes(rows, columns):
    buffer = io.BytesIO()
    pd.DataFrame(rows, columns=columns).to_excel(buffer, index=False, engine="openpyxl")
    return buffer.getvalue()


VALID_COLUMNS = ["NOME_COMPLETO_PORTA", "LATITUDE", "LONGITUDE"]
VALID_ROWS = [["12A", 41.2, -8.6]]


class _FakeDetector:
    def detect_door_number(self, latitude, longitude, cancel_event=None):
        return DetectionResult(
            success=True,
            latitude=latitude,
            longitude=longitude,
            door_number="12A",
            confidence=91,
        )


class WebUploadWorkflowTests(unittest.TestCase):
    """Upload endpoint with the real batch code, workbook reader, and a fake detector."""

    @classmethod
    def setUpClass(cls):
        cls.app = _load_real_app()
        cls.auth = "Basic " + base64.b64encode(b"tester:secret").decode("ascii")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.upload_dir = root / "uploads"
        self.output_dir = root / "outputs"
        self.originals = (self.app.UPLOAD_DIR, self.app.OUTPUT_DIR, dict(self.app._state))
        self.app.UPLOAD_DIR = self.upload_dir
        self.app.OUTPUT_DIR = self.output_dir
        self.app._state.update(
            {"running": False, "error": None, "cancel_event": None, "worker": None}
        )
        handler = type(
            "UploadWorkflowRequestHandler",
            (self.app.DoorNumberRequestHandler,),
            {
                "auth_username": "tester",
                "auth_password": "secret",
                "csrf_token": "test-csrf-token",
                "max_upload_bytes": 1024 * 1024,
            },
        )
        self.server = self.app.DoorNumberHTTPServer(("127.0.0.1", 0), handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.start()
        self.batch_finished = threading.Event()
        original_finish = self.app._finish_job

        def signal_finish(cancel_event, error=None):
            original_finish(cancel_event, error)
            self.batch_finished.set()

        self.finish_patch = patch.object(self.app, "_finish_job", side_effect=signal_finish)
        self.finish_patch.start()

    def tearDown(self):
        self.finish_patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        self.app.UPLOAD_DIR, self.app.OUTPUT_DIR, state = self.originals
        self.app._state.clear()
        self.app._state.update(state)
        self.directory.cleanup()

    def _upload(self, data):
        connection = HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(
            "POST",
            "/api/upload?filename=doors.xlsx",
            body=data,
            headers={"Authorization": self.auth, "X-CSRF-Token": "test-csrf-token"},
        )
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def _uploaded_files(self):
        return list(self.upload_dir.iterdir()) if self.upload_dir.exists() else []

    def test_real_invalid_workbooks_are_rejected_and_removed(self):
        workbooks = {
            "missing latitude": _workbook_bytes(
                [["12A", -8.6]], ["NOME_COMPLETO_PORTA", "LONGITUDE"]
            ),
            "empty": _workbook_bytes([], VALID_COLUMNS),
        }
        for name, data in workbooks.items():
            with self.subTest(workbook=name):
                status, body = self._upload(data)

                self.assertEqual(status, 400)
                self.assertIn(b"missing required columns", body)
                self.assertEqual(self._uploaded_files(), [])
                self.assertFalse(self.app._state["running"])

    def test_upload_is_deleted_after_a_successful_batch(self):
        with patch.object(self.app, "get_detector", return_value=_FakeDetector()):
            status, body = self._upload(_workbook_bytes(VALID_ROWS, VALID_COLUMNS))
            self.assertEqual(status, 200)
            self.assertTrue(self.batch_finished.wait(10))

        csv_path = self.output_dir / json.loads(body)["csv"]
        self.assertTrue(csv_path.exists())
        self.assertEqual(self._uploaded_files(), [])
        self.assertIsNone(self.app._state["error"])

    def test_upload_is_deleted_after_a_failed_batch(self):
        with patch.object(self.app, "get_detector", side_effect=RuntimeError("detector down")):
            status, _ = self._upload(_workbook_bytes(VALID_ROWS, VALID_COLUMNS))
            self.assertEqual(status, 200)
            self.assertTrue(self.batch_finished.wait(10))

        self.assertEqual(self._uploaded_files(), [])
        self.assertEqual(self.app._state["error"], "detector down")
        self.assertFalse(self.app._state["running"])

    def test_workbook_is_parsed_once_from_upload_to_batch_completion(self):
        with (
            patch("pandas.read_excel", wraps=pd.read_excel) as read_excel,
            patch.object(self.app, "get_detector", return_value=_FakeDetector()),
        ):
            status, _ = self._upload(_workbook_bytes(VALID_ROWS, VALID_COLUMNS))
            self.assertEqual(status, 200)
            self.assertTrue(self.batch_finished.wait(10))

        self.assertEqual(read_excel.call_count, 1)

    def test_unexpected_worker_start_failure_releases_the_job_and_upload(self):
        with patch.object(self.app, "_start_worker", side_effect=ValueError("boom")):
            status, body = self._upload(_workbook_bytes(VALID_ROWS, VALID_COLUMNS))

        self.assertEqual(status, 500)
        self.assertNotIn(b"boom", body)
        self.assertFalse(self.app._state["running"])
        self.assertEqual(self._uploaded_files(), [])

    def test_output_directory_failure_removes_the_upload_without_leaking_paths(self):
        def fail_mkdir(*args, **kwargs):
            raise OSError("/private/internal/outputs unavailable")

        self.app.OUTPUT_DIR = types.SimpleNamespace(mkdir=fail_mkdir)
        status, body = self._upload(_workbook_bytes(VALID_ROWS, VALID_COLUMNS))

        self.assertEqual(status, 500)
        self.assertNotIn(b"/private", body)
        self.assertEqual(self._uploaded_files(), [])
        self.assertFalse(self.app._state["running"])


if __name__ == "__main__":
    unittest.main()
