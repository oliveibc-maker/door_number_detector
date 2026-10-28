import base64
import csv
import hmac
import json
import logging
import math
import re
import secrets
import socket
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from door_number_detector.core.batch import (
    InvalidExcelWorkbookError,
    load_excel_dataframe,
    run_excel_batch,
    run_sqlserver_batch,
)
from door_number_detector.core.config import Config
from door_number_detector.core.metrics import RunMetrics
from door_number_detector.core.redaction import redact_api_key
from door_number_detector.core.sources import (
    FILTER_COLUMNS,
    SourceFilter,
)
from door_number_detector.runtime import shutdown_process, start_process
from door_number_detector.web.security import (
    UploadTooLarge,
    is_loopback_host,
    new_upload_path,
    parse_content_length,
    remove_orphaned_uploads,
    valid_csrf_token,
)

WEB_DIR = Path(__file__).resolve().parent
HTML_PATH = WEB_DIR / "templates" / "index.html"
DATA_DIR = Config().data_dir
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"

# ── Lazy single detector ───────────────────────────────────────────────────────
_detector = None
_detector_lock = threading.Lock()
_logger = logging.getLogger(__name__)


def get_detector():
    global _detector
    if _detector is None:
        with _detector_lock:
            if _detector is None:
                from door_number_detector.core.detector import DoorNumberDetector

                _detector = DoorNumberDetector()
    return _detector


_state: dict[str, Any] = {
    "running": False,
    "csv_path": None,
    "total": 0,
    "error": None,
    "cancel_event": None,
    "metrics": None,
    "worker": None,
}
_state_lock = threading.Lock()


def _claim_job(csv_path=None, total=None, metrics=None) -> threading.Event | None:
    with _state_lock:
        if _state["running"]:
            return None
        cancel_event = threading.Event()
        _state.update(
            {
                "running": True,
                "error": None,
                "cancel_event": cancel_event,
                "worker": None,
                "csv_path": str(csv_path) if csv_path is not None else None,
                "total": total if total is not None else 0,
                "metrics": metrics,
            }
        )
        return cancel_event


def _update_job(cancel_event: threading.Event, csv_path, total, metrics) -> bool:
    with _state_lock:
        if _state.get("cancel_event") is not cancel_event or cancel_event.is_set():
            return False
        _state.update({"csv_path": str(csv_path), "total": total, "metrics": metrics})
        return True


def _finish_job(cancel_event: threading.Event, error=None) -> None:
    with _state_lock:
        if _state.get("cancel_event") is cancel_event:
            if error and not cancel_event.is_set():
                _state["error"] = error
            _state["running"] = False
            _state["cancel_event"] = None
            _state["worker"] = None


def _start_worker(target, args, cancel_event: threading.Event) -> None:
    worker = threading.Thread(target=target, args=args, daemon=False)
    with _state_lock:
        if _state.get("cancel_event") is not cancel_event:
            raise RuntimeError("Job reservation expired before worker startup.")
        _state["worker"] = worker
    try:
        worker.start()
    except RuntimeError:
        with _state_lock:
            if _state.get("worker") is worker:
                _state["worker"] = None
        raise


def _save_metrics(metrics: RunMetrics, csv_path: Path) -> None:
    try:
        metrics_path = csv_path.with_suffix(".metrics.json")
        metrics.save_json(metrics_path)
        _logger.info(f"[batch] Metrics saved to {metrics_path.name}")
    except Exception as exc:
        _logger.warning(f"[batch] Could not save metrics: {exc}")


def _remove_uploaded_workbook(excel_path: Path) -> None:
    try:
        excel_path.unlink(missing_ok=True)
    except OSError as exc:
        _logger.warning("Could not remove uploaded workbook: %s", exc)


def _run_batch(
    excel_path: Path,
    csv_path: Path,
    cancel_event: threading.Event,
    metrics: RunMetrics,
    dataframe: Any,
) -> None:
    _logger.info(f"[batch] Excel batch starting — {excel_path.name}")
    error = None
    try:
        run_excel_batch(
            excel_path,
            csv_path,
            detector_instance=get_detector(),
            cancel_event=cancel_event,
            metrics=metrics,
            dataframe=dataframe,
        )
    except Exception as exc:
        _logger.exception(f"[batch] Excel batch failed: {exc}")
        if not cancel_event.is_set():
            error = redact_api_key(str(exc))
    finally:
        _remove_uploaded_workbook(excel_path)
        _save_metrics(metrics, csv_path)
        _finish_job(cancel_event, error)
        _logger.info("[batch] Excel batch finished.")


def _run_sqlserver_batch(
    filter_by: str,
    filter_value: str,
    csv_path: Path,
    cancel_event: threading.Event,
    metrics: RunMetrics,
) -> None:
    _logger.info(f"[batch] SQL batch starting — {filter_by}={filter_value!r}")
    error = None

    def set_total(total: int) -> None:
        with _state_lock:
            if _state.get("cancel_event") is cancel_event:
                _state["total"] = total

    try:
        run_sqlserver_batch(
            cast(SourceFilter, filter_by),
            filter_value,
            csv_path,
            cancel_event=cancel_event,
            metrics=metrics,
            on_rows_loaded=set_total,
            detector_factory=get_detector,
        )
    except Exception as exc:
        _logger.exception(f"[batch] SQL batch failed: {exc}")
        if not cancel_event.is_set():
            error = redact_api_key(str(exc))
    finally:
        _save_metrics(metrics, csv_path)
        _finish_job(cancel_event, error)
        _logger.info("[batch] SQL batch finished.")


def _read_csv(path=None) -> list:
    if path is None:
        with _state_lock:
            path = _state.get("csv_path")
    p = path
    if not p:
        return []
    try:
        with open(p, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f, delimiter=";"))
    except Exception:
        return []


class DoorNumberRequestHandler(BaseHTTPRequestHandler):
    auth_username = ""
    auth_password = ""
    csrf_token = ""
    max_upload_bytes = 25 * 1024 * 1024
    max_request_bytes = 16 * 1024

    def _send_security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def _is_authorized(self) -> bool:
        try:
            scheme, token = self.headers.get("Authorization", "").split(" ", 1)
            if scheme.lower() != "basic":
                raise ValueError("Unsupported authorization scheme")
            credentials = base64.b64decode(token, validate=True).decode("utf-8")
            username, password = credentials.split(":", 1)
            valid = hmac.compare_digest(
                username.encode("utf-8"), self.auth_username.encode("utf-8")
            )
            valid &= hmac.compare_digest(
                password.encode("utf-8"), self.auth_password.encode("utf-8")
            )
        except (ValueError, UnicodeDecodeError):
            valid = False
        if valid:
            return True
        body = b"Authentication required."
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Door Number Detector", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)
        return False

    def _is_csrf_valid(self) -> bool:
        if valid_csrf_token(self.headers.get("X-CSRF-Token", ""), self.csrf_token):
            return True
        self._send_json(403, {"error": "Invalid or missing CSRF token."})
        return False

    def _read_body(self, maximum: int) -> bytes | None:
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._send_json(400, {"error": "Transfer-Encoding is not supported."})
            return None
        try:
            length = parse_content_length(self.headers.get("Content-Length"), maximum)
        except UploadTooLarge:
            self.close_connection = True
            self._send_json(413, {"error": "Request body exceeds the configured size limit."})
            return None
        except ValueError as exc:
            self.close_connection = True
            self._send_json(400, {"error": str(exc)})
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self.close_connection = True
            self._send_json(400, {"error": "Request body was incomplete."})
            return None
        return body

    def do_GET(self):
        if not self._is_authorized():
            return
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self._serve_html(HTML_PATH)

        elif parsed.path == "/api/csrf":
            self._send_json(200, {"csrf_token": self.csrf_token})

        elif parsed.path == "/api/status":
            with _state_lock:
                state = _state.copy()
            rows = _read_csv(state.get("csv_path"))
            m = state.get("metrics")
            metrics_summary = m.summary() if m else None
            self._send_json(
                200,
                {
                    "running": state["running"],
                    "total": state["total"],
                    "processed": len(rows),
                    "error": state["error"],
                    "rows": rows,
                    "metrics": metrics_summary,
                },
            )

        elif parsed.path == "/api/results":
            self._send_json(200, get_detector().db.get_results(limit=20))

        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        if not self._is_authorized():
            return
        if not self._is_csrf_valid():
            return
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/api/reset":
            with _state_lock:
                ev = _state.get("cancel_event")
                running = _state["running"]
                if running and ev is not None:
                    ev.set()
                else:
                    _state.update(
                        {
                            "running": False,
                            "csv_path": None,
                            "total": 0,
                            "error": None,
                            "cancel_event": None,
                            "metrics": None,
                            "worker": None,
                        }
                    )
            if running:
                self._send_json(202, {"status": "cancelling"})
                return
            self._send_json(200, {"status": "reset"})

        elif parsed.path == "/api/upload":
            filename = params.get("filename", [""])[0]
            try:
                excel_path = new_upload_path(UPLOAD_DIR, filename)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            cancel_event = _claim_job()
            if cancel_event is None:
                self.close_connection = True
                self._send_json(409, {"error": "Pipeline already running."})
                return
            try:
                data = self._read_body(self.max_upload_bytes)
            except Exception as exc:
                self.close_connection = True
                _finish_job(cancel_event)
                _logger.warning("[upload] Could not read request body (%s).", type(exc).__name__)
                self._send_json(400, {"error": "The uploaded file could not be read."})
                return
            if data is None:
                _finish_job(cancel_event)
                return
            if cancel_event.is_set():
                _finish_job(cancel_event)
                self._send_json(409, {"error": "Upload was cancelled before validation."})
                return
            try:
                UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                with excel_path.open("xb") as uploaded_file:
                    uploaded_file.write(data)
                workbook = load_excel_dataframe(excel_path)
            except InvalidExcelWorkbookError as exc:
                _finish_job(cancel_event)
                _remove_uploaded_workbook(excel_path)
                self._send_json(400, {"error": str(exc)})
                return
            except Exception as exc:
                _finish_job(cancel_event)
                _remove_uploaded_workbook(excel_path)
                _logger.warning("[upload] Rejected spreadsheet (%s).", type(exc).__name__)
                self._send_json(
                    400,
                    {"error": "The uploaded file could not be read as a supported spreadsheet."},
                )
                return
            _logger.info("[upload] Accepted %s bytes as %s", len(data), excel_path.name)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            if cancel_event.is_set():
                _finish_job(cancel_event)
                _remove_uploaded_workbook(excel_path)
                self._send_json(409, {"error": "Upload was cancelled before the job started."})
                return
            try:
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
                csv_path = OUTPUT_DIR / f"predictions_{ts}.csv"
                metrics = RunMetrics()
            except Exception as exc:
                _finish_job(cancel_event)
                _remove_uploaded_workbook(excel_path)
                _logger.warning("[upload] Could not prepare batch (%s).", type(exc).__name__)
                self._send_json(500, {"error": "Could not prepare the batch."})
                return
            if not _update_job(cancel_event, csv_path, len(workbook), metrics):
                _finish_job(cancel_event)
                _remove_uploaded_workbook(excel_path)
                self._send_json(409, {"error": "Upload was cancelled before the job started."})
                return
            try:
                _start_worker(
                    _run_batch,
                    (excel_path, csv_path, cancel_event, metrics, workbook),
                    cancel_event,
                )
            except Exception as exc:
                _finish_job(cancel_event)
                _remove_uploaded_workbook(excel_path)
                _logger.warning("[upload] Could not start batch worker (%s).", type(exc).__name__)
                self._send_json(500, {"error": "Could not start the batch worker."})
                return

            self._send_json(200, {"status": "started", "csv": csv_path.name})

        elif parsed.path == "/api/run_sqlserver":
            raw_body = self._read_body(self.max_request_bytes)
            if raw_body is None:
                return
            try:
                decoded_body = raw_body.decode("utf-8")
            except UnicodeDecodeError:
                self._send_json(400, {"error": "Request body must be UTF-8."})
                return
            form_data = parse_qs(decoded_body, keep_blank_values=True)

            filter_by = form_data.get("filter_by", [""])[0].strip()
            filter_value = form_data.get("filter_value", [""])[0].strip()

            valid_filters = list(FILTER_COLUMNS)
            if filter_by not in valid_filters or not filter_value:
                self._send_json(400, {"error": "filter_by e filter_value são obrigatórios."})
                return
            filter_by = cast(SourceFilter, filter_by)

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", filter_value).strip("_")[:80] or "filter"
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            csv_path = OUTPUT_DIR / f"predictions_{filter_by}_{safe_name}_{ts}.csv"

            metrics = RunMetrics()
            cancel_event = _claim_job(csv_path, 0, metrics)
            if cancel_event is None:
                self._send_json(409, {"error": "Pipeline already running."})
                return
            try:
                _start_worker(
                    _run_sqlserver_batch,
                    (filter_by, filter_value, csv_path, cancel_event, metrics),
                    cancel_event,
                )
            except Exception as exc:
                _finish_job(cancel_event)
                _logger.warning("[sql] Could not start batch worker (%s).", type(exc).__name__)
                self._send_json(500, {"error": "Could not start the batch worker."})
                return

            self._send_json(200, {"status": "started", "csv": csv_path.name})

        elif parsed.path == "/api/analyze":
            raw_body = self._read_body(self.max_request_bytes)
            if raw_body is None:
                return
            try:
                decoded_body = raw_body.decode("utf-8")
            except UnicodeDecodeError:
                self._send_json(400, {"error": "Request body must be UTF-8."})
                return
            form_data = parse_qs(decoded_body, keep_blank_values=True)
            try:
                lat = float(form_data.get("latitude", [""])[0])
                lon = float(form_data.get("longitude", [""])[0])
                heading_value = form_data.get("heading", [None])[0]
                hdg = int(heading_value) if heading_value else None
                pit = int(form_data.get("pitch", ["0"])[0] or 0)
            except ValueError:
                self._send_json(400, {"error": "Coordinates and camera angles must be numeric."})
                return
            if (
                not math.isfinite(lat)
                or not math.isfinite(lon)
                or not -90 <= lat <= 90
                or not -180 <= lon <= 180
                or (hdg is not None and not 0 <= hdg <= 359)
                or not -90 <= pit <= 90
            ):
                self._send_json(
                    400, {"error": "Coordinates or camera angles are outside valid ranges."}
                )
                return
            cancel_event = _claim_job(total=1)
            if cancel_event is None:
                self._send_json(409, {"error": "Pipeline already running."})
                return
            # Release the job before responding, so a client that gets the response
            # can start the next job straight away.
            result = None
            try:
                result = get_detector().detect_door_number(
                    lat, lon, hdg, pit, cancel_event=cancel_event
                )
            except Exception:
                _logger.exception("Single-coordinate analysis failed.")
            finally:
                _finish_job(cancel_event)
            if result is None:
                self._send_json(500, {"error": "Detection failed."})
                return
            self._send_json(200, result.to_dict())

        else:
            self._send_json(404, {"error": "Not found"})

    def _serve_html(self, file_path: Path):
        body = file_path.read_bytes()
        body = body.replace(b"__CSRF_TOKEN__", self.csrf_token.encode("ascii"))
        body = body.replace(b"__MAX_UPLOAD_BYTES__", str(self.max_upload_bytes).encode("ascii"))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        status_code = args[-2] if len(args) >= 2 else "unknown"
        _logger.info("HTTP %s %s", self.command, status_code)


class DoorNumberHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    request_timeout = 30.0

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(self.request_timeout)
        return request, client_address


def create_server(host=None, port=None):
    config = Config()
    username, password = config.web_username, config.web_password
    if not username or not password:
        raise RuntimeError("Set WEB_USERNAME and WEB_PASSWORD before starting the web server.")
    host = config.web_host if host is None else host
    host = host or "127.0.0.1"
    port = config.web_port if port is None else port
    if not is_loopback_host(host):
        raise RuntimeError(
            "The application must bind to loopback. Terminate remote HTTPS at a same-host reverse proxy."
        )
    handler = type(
        "ConfiguredDoorNumberRequestHandler",
        (DoorNumberRequestHandler,),
        {
            "auth_username": username,
            "auth_password": password,
            "csrf_token": secrets.token_urlsafe(32),
            "max_upload_bytes": config.max_upload_bytes,
        },
    )
    server_class = DoorNumberHTTPServer
    if ":" in host:

        class IPv6ThreadingHTTPServer(DoorNumberHTTPServer):
            address_family = socket.AF_INET6

        server_class = IPv6ThreadingHTTPServer
    return server_class((host, port), handler)


def _cancel_active_job_and_get_worker():
    """Set the running batch's cancel event and return its worker, if any."""
    with _state_lock:
        cancel_event = _state.get("cancel_event")
        if cancel_event is not None:
            cancel_event.set()
        return _state.get("worker")


def _close_detector():
    if _detector is not None:
        _detector.close()


def main():
    start_process()
    server = create_server()
    try:
        removed_uploads = remove_orphaned_uploads(UPLOAD_DIR)
        if removed_uploads:
            _logger.info("Removed %s abandoned uploaded workbook(s).", removed_uploads)
        _logger.info("=" * 50)
        _logger.info("Door Number Detector — Web Server")
        _logger.info("=" * 50)
        _logger.info("Local:    http://127.0.0.1:%s", server.server_address[1])
        _logger.info("=" * 50)
        _logger.info("Press Ctrl+C to stop.")
        server.serve_forever()
    except KeyboardInterrupt:
        _logger.info("Web server stop requested.")
    finally:
        shutdown_process(server, _cancel_active_job_and_get_worker, _close_detector)


if __name__ == "__main__":
    main()
