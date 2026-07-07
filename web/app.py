import csv
import json
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── Add project root to path so core.* and entrypoint are importable ──────────
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from core.detector import DoorNumberDetector          # noqa: E402
from entrypoint import run_batch_predictions           # noqa: E402

WEB_DIR   = Path(__file__).resolve().parent
HTML_PATH = WEB_DIR / "templates" / "index.html"

detector = DoorNumberDetector()

_state = {"running": False, "csv_path": None, "total": 0, "error": None}


def _count_excel_rows(path: Path) -> int:
    try:
        import pandas as pd
        return len(pd.read_excel(path))
    except Exception:
        return 0


def _run_batch(excel_path: Path, csv_path: Path) -> None:
    _state.update({"running": True, "error": None})
    try:
        run_batch_predictions(excel_path, csv_path)
    except Exception as exc:
        _state["error"] = str(exc)
    finally:
        _state["running"] = False


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
            self._send_json(200, {
                "running":   _state["running"],
                "total":     _state["total"],
                "processed": len(rows),
                "error":     _state["error"],
                "rows":      rows,
            })

        elif parsed.path == "/api/results":
            self._send_json(200, detector.db.get_results(limit=20))

        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/api/upload":
            if _state["running"]:
                self._send_json(409, {"error": "Pipeline already running."})
                return

            fname      = Path(params.get("filename", ["upload.xlsx"])[0]).name
            length     = int(self.headers.get("Content-Length", "0"))
            data       = self.rfile.read(length)
            excel_path = ROOT_DIR / fname
            excel_path.write_bytes(data)

            ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
            csv_path = ROOT_DIR / f"predictions_{ts}.csv"

            _state.update({
                "total":    _count_excel_rows(excel_path),
                "csv_path": str(csv_path),
            })

            threading.Thread(
                target=_run_batch, args=(excel_path, csv_path), daemon=True
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


def create_server(host="127.0.0.1", port=8000):
    return ThreadingHTTPServer((host, port), DoorNumberRequestHandler)


def main():
    server = create_server()
    print("Open http://127.0.0.1:8000 to use the web interface")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
