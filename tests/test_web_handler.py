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

from core.models import DetectionResult


def _load_app_with_stubs():
    metrics_module = types.ModuleType("core.metrics")
    batch_module = types.ModuleType("core.batch")
    sources_module = types.ModuleType("core.sources")

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
    sources_module.query_sqlserver = lambda *args, **kwargs: None
    sources_module.read_excel_any = lambda path: Workbook()

    with patch.dict(sys.modules, {
        "core.metrics": metrics_module,
        "core.batch": batch_module,
        "core.sources": sources_module,
    }):
        return importlib.import_module("web.app")


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

        status, _ = self._request(
            "POST", "/api/reset", headers={"X-CSRF-Token": "test-csrf-token"}
        )
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
        self.app._detector.detect_door_number = lambda *args, **kwargs: typed_result
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
        self.assertIn("observation", payload)
        self.assertIn("timestamp", payload)
        self.assertIsNone(payload["error"])

    def test_detector_factory_is_lazy_and_constructs_once(self):
        self.assertIsNone(self.original_detector)
        self.app._detector = None
        detector_module = types.ModuleType("core.detector")
        created = []

        class LazyDetector:
            pass

        def build_detector():
            instance = LazyDetector()
            created.append(instance)
            return instance

        detector_module.DoorNumberDetector = build_detector
        try:
            with patch.dict(sys.modules, {"core.detector": detector_module}):
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
        from web.healthcheck import main

        with patch.dict("os.environ", {
            "WEB_USERNAME": "tester",
            "WEB_PASSWORD": "secret",
            "WEB_PORT": str(self.server.server_port),
        }):
            main()

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
        worker_started = threading.Event()
        release_worker = threading.Event()
        worker_finished = threading.Event()
        uploaded_paths = []
        original_finish = self.app._finish_job

        def hold_worker(path, *args, **kwargs):
            uploaded_paths.append(Path(path))
            worker_started.set()
            release_worker.wait(10)

        def signal_finish(cancel_event, error=None):
            original_finish(cancel_event, error)
            worker_finished.set()

        try:
            with patch.object(self.app, "run_excel_batch", side_effect=hold_worker), patch.object(
                self.app, "_finish_job", side_effect=signal_finish
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


if __name__ == "__main__":
    unittest.main()