"""Authenticated readiness check for Docker."""

import base64
import json
import os
from urllib.request import Request, urlopen


def main() -> None:
    username = os.environ["WEB_USERNAME"]
    password = os.environ["WEB_PASSWORD"]
    port = os.getenv("WEB_PORT", "8080")
    credentials = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    request = Request(
        f"http://127.0.0.1:{port}/api/csrf",
        headers={"Authorization": f"Basic {credentials}"},
    )
    with urlopen(request, timeout=5) as response:
        payload = json.load(response)
    if response.status != 200 or not payload.get("csrf_token"):
        raise RuntimeError("Application is not ready.")


if __name__ == "__main__":
    main()