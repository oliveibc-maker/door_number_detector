"""Startup and shutdown behavior of the web app entry point.

Paddle, the HTTP server, the worker, and the detector are replaced by fakes, so
no real Paddle package is imported and no model is downloaded.
"""

import importlib
import importlib.abc
import importlib.util
import io
import logging
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


def _fatal_paddle_sigterm(_signum, _frame):  # pragma: no cover - never invoked
    raise SystemExit("paddle fatal SIGTERM handler")


class _FakePaddleFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Serves a fake `paddle` that mimics the side effects of the real import."""

    def __init__(self, events: list[str]):
        self.events = events

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "paddle":
            return importlib.util.spec_from_loader(fullname, self)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        self.events.append("paddle_loaded")
        logging.getLogger().setLevel(logging.WARNING)  # real import resets this
        signal.signal(signal.SIGTERM, _fatal_paddle_sigterm)


class _FakeServer:
    server_address = ("127.0.0.1", 0)

    def __init__(self, events: list[str], on_serve):
        self.events = events
        self.on_serve = on_serve

    def serve_forever(self):
        self.events.append("serve")
        self.on_serve()

    def server_close(self):
        self.events.append("server_close")


class _FakeWorker:
    def __init__(self, events: list[str], cancel_event: threading.Event):
        self.events = events
        self.cancel_event = cancel_event

    def join(self):
        self.events.append(f"worker_join(cancelled={self.cancel_event.is_set()})")


class _FakeDetector:
    def __init__(self, events: list[str]):
        self.events = events

    def close(self):
        self.events.append("detector_close")


def _wait_for_sigterm_to_be_handled():
    # A SIGTERM handler that raises KeyboardInterrupt interrupts this loop.
    for _ in range(500):
        time.sleep(0.01)
    raise AssertionError("SIGTERM did not stop the server")


class RuntimeLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import inside a sys.modules snapshot so other test modules that load
        # the web app with their own stubs are not handed this instance.
        with patch.dict(sys.modules):
            cls.app = importlib.import_module("door_number_detector.web.app")

    def setUp(self):
        self.events: list[str] = []
        self.directory = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.directory.name)

        root_logger = logging.getLogger()
        self.original_handlers = list(root_logger.handlers)
        self.original_level = root_logger.level
        self.original_sigterm = signal.getsignal(signal.SIGTERM)
        self.original_state = dict(self.app._state)
        self.original_detector = self.app._detector
        self.original_upload_dir = self.app.UPLOAD_DIR
        self.app.UPLOAD_DIR = self.data_dir / "uploads"
        self.app.UPLOAD_DIR.mkdir()
        self.app._detector = None
        self.app._state.update({"cancel_event": None, "worker": None})

        self.patches = [
            patch.dict(os.environ, {"APP_DATA_DIR": str(self.data_dir)}),
            patch("sys.stderr", io.StringIO()),
            patch.object(sys, "meta_path", [_FakePaddleFinder(self.events), *sys.meta_path]),
        ]
        for active_patch in self.patches:
            active_patch.start()
        self.sys_modules_patch = patch.dict(sys.modules)
        self.sys_modules_patch.start()
        sys.modules.pop("paddle", None)

    def tearDown(self):
        self.sys_modules_patch.stop()
        for active_patch in reversed(self.patches):
            active_patch.stop()
        root_logger = logging.getLogger()
        for handler in list(root_logger.handlers):
            if handler not in self.original_handlers:
                root_logger.removeHandler(handler)
                handler.close()
        root_logger.setLevel(self.original_level)
        signal.signal(signal.SIGTERM, self.original_sigterm)
        self.app._state.clear()
        self.app._state.update(self.original_state)
        self.app._detector = self.original_detector
        self.app.UPLOAD_DIR = self.original_upload_dir
        self.directory.cleanup()

    def _run_main(self, on_serve):
        server = _FakeServer(self.events, on_serve)
        with patch.object(self.app, "create_server", return_value=server):
            self.app.main()
        return server

    def test_paddle_loads_before_our_sigterm_handler_replaces_it(self):
        seen = {}

        def on_serve():
            seen["handler"] = signal.getsignal(signal.SIGTERM)

        self._run_main(on_serve)

        self.assertEqual(self.events[0], "paddle_loaded")
        self.assertIsNot(seen["handler"], _fatal_paddle_sigterm)
        self.assertTrue(callable(seen["handler"]))

    def test_info_logging_is_active_after_paddle_resets_the_log_level(self):
        seen = {}

        def on_serve():
            seen["level"] = logging.getLogger().level
            seen["effective_info"] = logging.getLogger("door_number_detector.x").isEnabledFor(
                logging.INFO
            )

        self._run_main(on_serve)

        self.assertEqual(seen["level"], logging.INFO)
        self.assertTrue(seen["effective_info"])
        self.assertTrue((self.data_dir / "logs" / "door_detector.log").exists())

    def test_sigterm_stops_the_server_cleanly(self):
        def on_serve():
            os.kill(os.getpid(), signal.SIGTERM)
            _wait_for_sigterm_to_be_handled()

        self._run_main(on_serve)  # returns normally: no exception escapes main

        self.assertEqual(self.events, ["paddle_loaded", "serve", "server_close"])

    def test_shutdown_cancels_the_batch_waits_for_the_worker_and_closes_the_detector(self):
        cancel_event = threading.Event()
        self.app._state.update(
            {"cancel_event": cancel_event, "worker": _FakeWorker(self.events, cancel_event)}
        )
        self.app._detector = _FakeDetector(self.events)

        def on_serve():
            os.kill(os.getpid(), signal.SIGTERM)
            _wait_for_sigterm_to_be_handled()

        self._run_main(on_serve)

        self.assertTrue(cancel_event.is_set())
        self.assertEqual(
            self.events,
            [
                "paddle_loaded",
                "serve",
                "server_close",
                "worker_join(cancelled=True)",
                "detector_close",
            ],
        )

    def test_shutdown_runs_when_the_server_fails(self):
        self.app._detector = _FakeDetector(self.events)

        def on_serve():
            raise OSError("boom")

        with self.assertRaises(OSError):
            self._run_main(on_serve)

        self.assertEqual(self.events[-2:], ["server_close", "detector_close"])

    def test_shutdown_runs_when_startup_cleanup_fails_after_the_server_is_created(self):
        self.app._detector = _FakeDetector(self.events)
        server = _FakeServer(self.events, lambda: None)

        with (
            patch.object(self.app, "create_server", return_value=server),
            patch.object(self.app, "remove_orphaned_uploads", side_effect=OSError("boom")),
        ):
            with self.assertRaises(OSError):
                self.app.main()

        self.assertEqual(self.events, ["paddle_loaded", "server_close", "detector_close"])

    def test_startup_removes_abandoned_uploads_only(self):
        abandoned = self.app.UPLOAD_DIR / ("a" * 32 + ".xlsx")
        unrelated = self.app.UPLOAD_DIR / "notes.txt"
        abandoned.write_bytes(b"workbook")
        unrelated.write_bytes(b"keep")
        seen = {}

        def on_serve():
            seen["abandoned_exists"] = abandoned.exists()

        self._run_main(on_serve)

        self.assertFalse(seen["abandoned_exists"])
        self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
