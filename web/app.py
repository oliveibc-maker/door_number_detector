import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core.detector import DoorNumberDetector


BASE_DIR = Path(__file__).resolve().parent
HTML_PATH = BASE_DIR / "web" / "templates" / "index.html"

detector = DoorNumberDetector()


class DoorNumberRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urlparse(self.path)

        if parsed_path.path == "/":
            self._serve_html(HTML_PATH)
            return

        if parsed_path.path == "/api/results":
            results = detector.db.get_results(limit=20)
            self._send_json(200, results)
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        parsed_path = urlparse(self.path)

        if parsed_path.path != "/api/analyze":
            self._send_json(404, {"error": "Not found"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        data = parse_qs(body, keep_blank_values=True)

        latitude_raw = data.get("latitude", [""])[0].strip()
        longitude_raw = data.get("longitude", [""])[0].strip()
        heading_raw = data.get("heading", ["0"])[0].strip()
        pitch_raw = data.get("pitch", ["0"])[0].strip()

        try:
            latitude = float(latitude_raw)
            longitude = float(longitude_raw)
            heading = int(heading_raw) if heading_raw else 0
            pitch = int(pitch_raw) if pitch_raw else 0
        except ValueError:
            self._send_json(400, {"error": "Latitude and longitude must be valid numbers."})
            return

        result = detector.detect_door_number(latitude, longitude, heading, pitch)
        self._send_json(200, result)

    def _serve_html(self, file_path: Path):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(file_path.read_bytes())

    def _send_json(self, status_code: int, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def create_server(host="127.0.0.1", port=8000):
    return ThreadingHTTPServer((host, port), DoorNumberRequestHandler)


def main():
    server = create_server()
    print("Open http://127.0.0.1:8000 to use the web interface")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
