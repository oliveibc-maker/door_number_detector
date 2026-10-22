import csv
import base64
import hmac
import json
import logging
import math
import re
import secrets
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
from core.detector import DoorNumberDetector
from core.metrics import RunMetrics
from entrypoint import run_batch_predictions, _query_sqlserver, _run_detection_on_df, _read_excel_any
from web.security import (
    UploadTooLarge,
    is_loopback_host,
    new_upload_path,
    parse_content_length,
    valid_csrf_token,
)

WEB_DIR       = Path(__file__).resolve().parent
HTML_PATH     = WEB_DIR / "templates" / "index.html"
TEMPLATE_PATH = WEB_DIR / "template_moradas.xlsx"
UPLOAD_DIR    = ROOT_DIR / "data" / "uploads"

# ── Single global detector ─────────────────────────────────────────────────────
detector = DoorNumberDetector()
_logger  = logging.getLogger("core.detector")

_state = {
    "running":      False,
    "csv_path":     None,
    "total":        0,
    "error":        None,
    "cancel_event": None,
    "metrics":      None,
}


def _count_excel_rows(path: Path) -> int:
    try:
        return len(_read_excel_any(path))
    except Exception:
        return 0


def _new_cancel_event() -> threading.Event:
    ev = threading.Event()
    _state["cancel_event"] = ev
    return ev


def _save_metrics(metrics: RunMetrics, csv_path: Path) -> None:
    try:
        metrics_path = csv_path.with_suffix(".metrics.json")
        metrics.save_json(metrics_path)
        _logger.info(f"[batch] Metrics saved to {metrics_path.name}")
    except Exception as exc:
        _logger.warning(f"[batch] Could not save metrics: {exc}")


def _run_batch(excel_path: Path, csv_path: Path, cancel_event: threading.Event, metrics: RunMetrics) -> None:
    _logger.info(f"[batch] Excel batch starting — {excel_path.name}")
    _state.update({"running": True, "error": None})
    try:
        run_batch_predictions(
            excel_path, csv_path,
            detector_instance=detector,
            cancel_event=cancel_event,
            metrics=metrics,
        )
    except Exception as exc:
        _logger.exception(f"[batch] Excel batch failed: {exc}")
        if not cancel_event.is_set():
            _state["error"] = str(exc)
    finally:
        _state["running"] = False
        _save_metrics(metrics, csv_path)
        _logger.info("[batch] Excel batch finished.")


def _run_sqlserver_batch(
    filter_by: str, filter_value: str, csv_path: Path,
    cancel_event: threading.Event, metrics: RunMetrics,
) -> None:
    _logger.info(f"[batch] SQL batch starting — {filter_by}={filter_value!r}")
    _state.update({"running": True, "error": None})
    try:
        config = Config()
        df = _query_sqlserver(filter_by, filter_value, config)
        _state["total"] = len(df)
        _run_detection_on_df(
            df, csv_path, set(),
            detector_instance=detector,
            cancel_event=cancel_event,
            metrics=metrics,
        )
    except Exception as exc:
        _logger.exception(f"[batch] SQL batch failed: {exc}")
        if not cancel_event.is_set():
            _state["error"] = str(exc)
    finally:
        _state["running"] = False
        _save_metrics(metrics, csv_path)
        _logger.info("[batch] SQL batch finished.")


def _read_csv() -> list:
    p = _state.get("csv_path")
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
            rows = _read_csv()
            m = _state.get("metrics")
            metrics_summary = m.summary() if m else None
            self._send_json(200, {
                "running":   _state["running"],
                "total":     _state["total"],
                "processed": len(rows),
                "error":     _state["error"],
                "rows":      rows,
                "metrics":   metrics_summary,
            })

        elif parsed.path == "/api/template":
            if not TEMPLATE_PATH.exists():
                self._send_json(404, {"error": "Template not found."})
                return
            data = TEMPLATE_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", 'attachment; filename="template_moradas.xlsx"')
            self.send_header("Content-Length", str(len(data)))
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(data)

        elif parsed.path == "/api/results":
            self._send_json(200, detector.db.get_results(limit=20))

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
            ev = _state.get("cancel_event")
            if ev is not None:
                ev.set()
            _state.update({
                "running":      False,
                "csv_path":     None,
                "total":        0,
                "error":        None,
                "cancel_event": None,
                "metrics":      None,
            })
            self._send_json(200, {"status": "reset"})

        elif parsed.path == "/api/upload":
            if _state["running"]:
                self._send_json(409, {"error": "Pipeline already running."})
                return

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
                workbook = _read_excel_any(excel_path)
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
            csv_path = ROOT_DIR / f"predictions_{ts}.csv"

            metrics = RunMetrics()
            _state.update({
                "running":  True,
                "error":    None,
                "total":    len(workbook),
                "csv_path": str(csv_path),
                "metrics":  metrics,
            })

            cancel_event = _new_cancel_event()
            threading.Thread(
                target=_run_batch,
                args=(excel_path, csv_path, cancel_event, metrics),
                daemon=True,
            ).start()

            self._send_json(200, {"status": "started", "csv": csv_path.name})

        elif parsed.path == "/api/run_sqlserver":
            if _state["running"]:
                self._send_json(409, {"error": "Pipeline already running."})
                return

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
            csv_path  = ROOT_DIR / f"predictions_{filter_by}_{safe_name}_{ts}.csv"

            metrics = RunMetrics()
            _state.update({
                "running":  True,
                "error":    None,
                "total":    0,
                "csv_path": str(csv_path),
                "metrics":  metrics,
            })

            cancel_event = _new_cancel_event()
            threading.Thread(
                target=_run_sqlserver_batch,
                args=(filter_by, filter_value, csv_path, cancel_event, metrics),
                daemon=True,
            ).start()

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
            self._send_json(200, detector.detect_door_number(lat, lon, hdg, pit))

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
    server_class = ThreadingHTTPServer
    if ":" in host:
        class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
            address_family = socket.AF_INET6

        server_class = IPv6ThreadingHTTPServer
    return server_class((host, port), handler)


def main():
    server   = create_server()
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
        server.server_close()


if __name__ == "__main__":
    main()
