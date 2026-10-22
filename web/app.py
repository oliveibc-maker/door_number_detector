import csv
import base64
import hmac
import json
import logging
import math
import re
import secrets
import signal
import socket
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from core.config import Config
from core.metrics import RunMetrics
from core.batch import run_detection_batch, run_excel_batch
from core.sources import query_sqlserver, read_excel_any
from web.security import (
    UploadTooLarge,
    is_loopback_host,
    new_upload_path,
    parse_content_length,
    remove_orphaned_uploads,
    valid_csrf_token,
)

WEB_DIR       = Path(__file__).resolve().parent
HTML_PATH     = WEB_DIR / "templates" / "index.html"
DATA_DIR      = Config().data_dir
UPLOAD_DIR    = DATA_DIR / "uploads"
OUTPUT_DIR    = DATA_DIR / "outputs"

# ── Lazy single detector ───────────────────────────────────────────────────────
_detector = None
_detector_lock = threading.Lock()
_logger  = logging.getLogger("core.detector")


def get_detector():
    global _detector
    if _detector is None:
        with _detector_lock:
            if _detector is None:
                from core.detector import DoorNumberDetector

                _detector = DoorNumberDetector()
    return _detector

_state = {
    "running":      False,
    "csv_path":     None,
    "total":        0,
    "error":        None,
    "cancel_event": None,
    "metrics":      None,
    "worker":       None,
}
_state_lock = threading.Lock()


def _claim_job(csv_path=None, total=None, metrics=None):
    with _state_lock:
        if _state["running"]:
            return None
        cancel_event = threading.Event()
        _state.update({
            "running": True,
            "error": None,
            "cancel_event": cancel_event,
            "worker": None,
        })
        if csv_path is not None:
            _state["csv_path"] = str(csv_path)
        if total is not None:
            _state["total"] = total
        if metrics is not None:
            _state["metrics"] = metrics
        return cancel_event


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


def _run_batch(excel_path: Path, csv_path: Path, cancel_event: threading.Event, metrics: RunMetrics) -> None:
    _logger.info(f"[batch] Excel batch starting — {excel_path.name}")
    error = None
    try:
        run_excel_batch(
            excel_path, csv_path,
            detector_instance=get_detector(),
            cancel_event=cancel_event,
            metrics=metrics,
        )
    except Exception as exc:
        _logger.exception(f"[batch] Excel batch failed: {exc}")
        if not cancel_event.is_set():
            error = str(exc)
    finally:
        try:
            excel_path.unlink(missing_ok=True)
        except OSError as exc:
            _logger.warning("[batch] Could not remove uploaded workbook: %s", exc)
        _save_metrics(metrics, csv_path)
        _finish_job(cancel_event, error)
        _logger.info("[batch] Excel batch finished.")


def _run_sqlserver_batch(
    filter_by: str, filter_value: str, csv_path: Path,
    cancel_event: threading.Event, metrics: RunMetrics,
) -> None:
    _logger.info(f"[batch] SQL batch starting — {filter_by}={filter_value!r}")
    error = None
    try:
        config = Config()
        df = query_sqlserver(filter_by, filter_value, config)
        with _state_lock:
            if _state.get("cancel_event") is cancel_event:
                _state["total"] = len(df)
        run_detection_batch(
            df, csv_path, set(),
            detector_instance=get_detector(),
            cancel_event=cancel_event,
            metrics=metrics,
        )
    except Exception as exc:
        _logger.exception(f"[batch] SQL batch failed: {exc}")
        if not cancel_event.is_set():
            error = str(exc)
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
            valid = hmac.compare_digest(username.encode("utf-8"), self.auth_username.encode("utf-8"))
            valid &= hmac.compare_digest(password.encode("utf-8"), self.auth_password.encode("utf-8"))
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
            self._send_json(200, {
                "running":   state["running"],
                "total":     state["total"],
                "processed": len(rows),
                "error":     state["error"],
                "rows":      rows,
                "metrics":   metrics_summary,
            })

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
                    _state.update({
                        "running":      False,
                        "csv_path":     None,
                        "total":        0,
                        "error":        None,
                        "cancel_event": None,
                        "metrics":      None,
                        "worker":       None,
                    })
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
            data = self._read_body(self.max_upload_bytes)
            if data is None:
                return
            try:
                UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
                with excel_path.open("xb") as uploaded_file:
                    uploaded_file.write(data)
                workbook = read_excel_any(excel_path)
                required_columns = {"NOME_COMPLETO_PORTA", "LATITUDE", "LONGITUDE"}
                if workbook.empty or not required_columns.issubset(workbook.columns):
                    excel_path.unlink(missing_ok=True)
                    self._send_json(400, {"error": "The spreadsheet is empty or missing required columns."})
                    return
            except Exception as exc:
                excel_path.unlink(missing_ok=True)
                _logger.warning("[upload] Rejected spreadsheet (%s).", type(exc).__name__)
                self._send_json(400, {"error": "The uploaded file could not be read as a supported spreadsheet."})
                return
            _logger.info("[upload] Accepted %s bytes as %s", len(data), excel_path.name)

            ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            csv_path = OUTPUT_DIR / f"predictions_{ts}.csv"

            metrics = RunMetrics()
            cancel_event = _claim_job(csv_path, len(workbook), metrics)
            if cancel_event is None:
                excel_path.unlink(missing_ok=True)
                self._send_json(409, {"error": "Pipeline already running."})
                return
            try:
                _start_worker(
                    _run_batch,
                    (excel_path, csv_path, cancel_event, metrics),
                    cancel_event,
                )
            except RuntimeError as exc:
                excel_path.unlink(missing_ok=True)
                _finish_job(cancel_event, str(exc))
                self._send_json(500, {"error": "Could not start the batch worker."})
                return

            self._send_json(200, {"status": "started", "csv": csv_path.name})

        elif parsed.path == "/api/run_sqlserver":
            body = self._read_body(self.max_request_bytes)
            if body is None:
                return
            try:
                body = body.decode("utf-8")
            except UnicodeDecodeError:
                self._send_json(400, {"error": "Request body must be UTF-8."})
                return
            data   = parse_qs(body, keep_blank_values=True)

            filter_by    = data.get("filter_by",    [""])[0].strip()
            filter_value = data.get("filter_value", [""])[0].strip()

            valid_filters = ["localidade", "freguesia", "concelho", "rua"]
            if filter_by not in valid_filters or not filter_value:
                self._send_json(400, {"error": "filter_by e filter_value são obrigatórios."})
                return

            ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", filter_value).strip("_")[:80] or "filter"
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            csv_path  = OUTPUT_DIR / f"predictions_{filter_by}_{safe_name}_{ts}.csv"

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
            except RuntimeError as exc:
                _finish_job(cancel_event, str(exc))
                self._send_json(500, {"error": "Could not start the batch worker."})
                return

            self._send_json(200, {"status": "started", "csv": csv_path.name})

        elif parsed.path == "/api/analyze":
            body = self._read_body(self.max_request_bytes)
            if body is None:
                return
            try:
                body = body.decode("utf-8")
            except UnicodeDecodeError:
                self._send_json(400, {"error": "Request body must be UTF-8."})
                return
            data   = parse_qs(body, keep_blank_values=True)
            try:
                lat = float(data.get("latitude",  [""])[0])
                lon = float(data.get("longitude", [""])[0])
                hdg = int(data.get("heading", ["0"])[0] or 0)
                pit = int(data.get("pitch",   ["0"])[0] or 0)
            except ValueError:
                self._send_json(400, {"error": "Coordinates and camera angles must be numeric."})
                return
            if (
                not math.isfinite(lat)
                or not math.isfinite(lon)
                or not -90 <= lat <= 90
                or not -180 <= lon <= 180
                or not 0 <= hdg <= 359
                or not -90 <= pit <= 90
            ):
                self._send_json(400, {"error": "Coordinates or camera angles are outside valid ranges."})
                return
            cancel_event = _claim_job(total=1)
            if cancel_event is None:
                self._send_json(409, {"error": "Pipeline already running."})
                return
            try:
                result = get_detector().detect_door_number(
                    lat, lon, hdg, pit, cancel_event=cancel_event
                )
            except Exception:
                _logger.exception("Single-coordinate analysis failed.")
                self._send_json(500, {"error": "Detection failed."})
                return
            finally:
                _finish_job(cancel_event)
            self._send_json(200, result)

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

    def log_message(self, *_):
        return


class DoorNumberHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True


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


def _handle_sigterm(_signum, _frame):
    raise KeyboardInterrupt


def main():
    signal.signal(signal.SIGTERM, _handle_sigterm)
    server   = create_server()
    removed_uploads = remove_orphaned_uploads(UPLOAD_DIR)
    if removed_uploads:
        _logger.info("Removed %s abandoned uploaded workbook(s).", removed_uploads)
    print("=" * 50)
    print("Door Number Detector — Web Server")
    print("=" * 50)
    print(f"Local:    http://127.0.0.1:{server.server_address[1]}")
    print("=" * 50)
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        with _state_lock:
            cancel_event = _state.get("cancel_event")
            if cancel_event is not None:
                cancel_event.set()
        server.server_close()
        with _state_lock:
            cancel_event = _state.get("cancel_event")
            worker = _state.get("worker")
            if cancel_event is not None:
                cancel_event.set()
        if worker is not None and worker is not threading.current_thread():
            worker.join()
        if _detector is not None:
            _detector.close()


if __name__ == "__main__":
    main()
