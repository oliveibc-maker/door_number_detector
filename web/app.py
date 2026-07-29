import csv
import json
import logging
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
from entrypoint import run_batch_predictions, _query_sqlserver, _run_detection_on_df

WEB_DIR       = Path(__file__).resolve().parent
HTML_PATH     = WEB_DIR / "templates" / "index.html"
TEMPLATE_PATH = WEB_DIR / "template_moradas.xlsx"

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


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _count_excel_rows(path: Path) -> int:
    try:
        import pandas as pd
        return len(pd.read_excel(path))
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

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self._serve_html(HTML_PATH)

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
            self.end_headers()
            self.wfile.write(data)

        elif parsed.path == "/api/results":
            self._send_json(200, detector.db.get_results(limit=20))

        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
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

            fname  = Path(params.get("filename", ["upload.xlsx"])[0]).name
            length = int(self.headers.get("Content-Length", "0"))
            data   = self.rfile.read(length)

            if not data:
                self._send_json(400, {"error": "No file data received (Content-Length=0)."})
                return

            excel_path = ROOT_DIR / fname
            try:
                excel_path.write_bytes(data)
                _logger.info(f"[upload] Saved {len(data):,} bytes → {excel_path}")
            except Exception as exc:
                _logger.error(f"[upload] Cannot save uploaded file: {exc}")
                self._send_json(500, {"error": f"Cannot save file: {exc}"})
                return

            ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
            csv_path = ROOT_DIR / f"predictions_{ts}.csv"

            metrics = RunMetrics()
            _state.update({
                "running":  True,
                "error":    None,
                "total":    _count_excel_rows(excel_path),
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

            length = int(self.headers.get("Content-Length", "0"))
            body   = self.rfile.read(length).decode("utf-8")
            data   = parse_qs(body, keep_blank_values=True)

            filter_by    = data.get("filter_by",    [""])[0].strip()
            filter_value = data.get("filter_value", [""])[0].strip()

            valid_filters = ["localidade", "freguesia", "concelho", "rua"]
            if filter_by not in valid_filters or not filter_value:
                self._send_json(400, {"error": "filter_by e filter_value são obrigatórios."})
                return

            ts        = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = filter_value.replace(" ", "_")
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
            length = int(self.headers.get("Content-Length", "0"))
            body   = self.rfile.read(length).decode("utf-8")
            data   = parse_qs(body, keep_blank_values=True)
            try:
                lat = float(data.get("latitude",  [""])[0])
                lon = float(data.get("longitude", [""])[0])
                hdg = int(data.get("heading", ["0"])[0] or 0)
                pit = int(data.get("pitch",   ["0"])[0] or 0)
            except ValueError:
                self._send_json(400, {"error": "Latitude e longitude invalidos."})
                return
            self._send_json(200, detector.detect_door_number(lat, lon, hdg, pit))

        else:
            self._send_json(404, {"error": "Not found"})

    def _serve_html(self, file_path: Path):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(file_path.read_bytes())

    def _send_json(self, code: int, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        return


def create_server(host="0.0.0.0", port=8080):
    return ThreadingHTTPServer((host, port), DoorNumberRequestHandler)


def main():
    server   = create_server()
    local_ip = _get_local_ip()
    print("=" * 50)
    print("Door Number Detector — Web Server")
    print("=" * 50)
    print(f"Local:    http://127.0.0.1:8080")
    print(f"Network:  http://{local_ip}:8080")
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
