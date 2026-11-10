#!/usr/bin/env python3
"""Feasibility check for a local llama.cpp vision endpoint used by the guided planner.

Sends one planner-shaped request (same system prompt shape, JSON-object response
format, base64 image) to the loopback OpenAI-compatible endpoint and verifies that
the response parses through the app's real action parser. Optionally verifies that
disconnecting the client mid-stream stops server compute.

Usage:
    python scripts/check_local_vlm.py [--url http://127.0.0.1:8080/v1/chat/completions]
                                      [--model qwen2.5-vl-3b] [--image path.jpg]
                                      [--repeat 5] [--check-cancel]

Run with the project venv: uv run --locked --no-sync python scripts/check_local_vlm.py
"""

import argparse
import base64
import io
import json
import sys
import threading
import time
from urllib import request as urlrequest
from urllib.error import URLError

from PIL import Image, ImageDraw

sys.path.insert(0, "src")

from door_number_detector.core.guided import parse_action  # noqa: E402

SYSTEM_PROMPT = (
    "Inspect the current Street View image for a door-number search. "
    "Return exactly one JSON action. Use OCR observation IDs for candidates; "
    "never invent a number. Allowed action shapes: inspect_region with box "
    "and fov; try_location with box and fov; adjust_view with heading, pitch, "
    "and fov; candidate with "
    "observation_ids; move_panorama with an available panorama_id; stop "
    "with reason search_complete. Do not output coordinates, tools, or prose."
)

ALLOWED_IDS = {
    "allowed_location_ids": {"L1", "L2"},
    "allowed_panorama_ids": set(),
    "allowed_observation_ids": {"o1", "o2"},
}


def _synthetic_image() -> bytes:
    """Build a synthetic facade-like image with a large printed door number."""
    image = Image.new("RGB", (640, 640), (210, 200, 190))
    draw = ImageDraw.Draw(image)
    draw.rectangle([250, 260, 390, 560], fill=(90, 60, 40))
    draw.rectangle([285, 300, 355, 340], fill=(245, 245, 235))
    draw.text((298, 305), "62", fill=(20, 20, 20))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def _load_image(path: str | None) -> bytes:
    if path is None:
        return _synthetic_image()
    with Image.open(path) as img:
        buffer = io.BytesIO()
        img.convert("RGB").save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


def _build_payload(model: str, image_b64: str, stream: bool) -> bytes:
    context = {
        "view": {"heading": 0.0, "pitch": 0.0, "fov": 90},
        "ocr_observations": [
            {"id": "o1", "text": "62", "candidate": "62", "confidence": 0.97, "box": None},
            {"id": "o2", "text": "RUA", "candidate": None, "confidence": 0.88, "box": None},
        ],
        "history": [],
        "available_panoramas": [],
    }
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 256,
        "stream": stream,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps(context, separators=(",", ":"))},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                ],
            },
        ],
    }
    return json.dumps(payload).encode("utf-8")


def _post(url: str, payload: bytes, timeout: float) -> tuple[dict, float]:
    req = urlrequest.Request(url, data=payload, headers={"Content-Type": "application/json"})
    start = time.monotonic()
    with urlrequest.urlopen(req, timeout=timeout) as response:
        body = response.read()
    elapsed = time.monotonic() - start
    return json.loads(body), elapsed


def check_contract(url: str, model: str, image_b64: str, repeat: int, timeout: float) -> int:
    failures = 0
    for attempt in range(1, repeat + 1):
        try:
            body, elapsed = _post(url, _build_payload(model, image_b64, stream=False), timeout)
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            print(f"[{attempt}/{repeat}] request failed: {exc}")
            failures += 1
            continue
        try:
            content = body["choices"][0]["message"]["content"]
            action = json.loads(content)
            parsed = parse_action(action, **ALLOWED_IDS)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError) as exc:
            print(f"[{attempt}/{repeat}] invalid action: {exc}; raw={content!r:.200}")
            failures += 1
            continue
        print(f"[{attempt}/{repeat}] ok in {elapsed:.1f}s -> type={parsed.kind}")
    print(f"contract: {repeat - failures}/{repeat} valid planner actions")
    return 1 if failures else 0


def check_cancel(url: str, model: str, image_b64: str) -> int:
    """Abort a streaming request mid-flight; the operator watches server CPU/log."""
    payload = _build_payload(model, image_b64, stream=True)
    req = urlrequest.Request(url, data=payload, headers={"Content-Type": "application/json"})

    def _reader(response) -> None:
        try:
            while response.read(4096):
                pass
        except OSError:
            pass

    print("starting streaming request; aborting after first chunk...")
    try:
        response = urlrequest.urlopen(req, timeout=30)
    except (URLError, TimeoutError, OSError) as exc:
        print(f"cancel check: request failed to start: {exc}")
        return 1
    thread = threading.Thread(target=_reader, args=(response,), daemon=True)
    thread.start()
    time.sleep(2.0)
    response.close()
    print(
        "client disconnected. Now confirm on the server: a 'cancel task' log line and "
        "llama-server CPU returning to idle within a few seconds."
    )
    result = input("Did the server stop compute after disconnect? [y/N] ").strip().lower()
    return 0 if result == "y" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080/v1/chat/completions")
    parser.add_argument("--model", default="qwen2.5-vl-3b")
    parser.add_argument("--image", default=None, help="optional local JPEG/PNG; default synthetic")
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--check-cancel", action="store_true")
    args = parser.parse_args()

    image_b64 = base64.b64encode(_load_image(args.image)).decode("ascii")
    status = check_contract(args.url, args.model, image_b64, args.repeat, args.timeout)
    if args.check_cancel:
        status |= check_cancel(args.url, args.model, image_b64)
    return status


if __name__ == "__main__":
    sys.exit(main())
