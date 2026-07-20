"""Run-level performance and cost metrics for the door number detection pipeline."""

import json
import threading
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Callable, List

# ── Google Street View Static API pricing ──────────────────────────────────────
# https://developers.google.com/maps/documentation/streetview/usage-and-billing
# Image Static API: $7.00 / 1 000 requests  →  ~€6.40 / 1 000 (adjust to current rate)
# Metadata API:     free
_DEFAULT_COST_PER_IMAGE = 0.0064   # € — change to 0.007 and currency="$" for USD
_DEFAULT_COST_PER_META  = 0.000
_DEFAULT_CURRENCY       = "€"


@dataclass
class _Record:
    elapsed_s:      float
    image_calls:    int
    metadata_calls: int
    success:        bool
    no_imagery:     bool


class RunMetrics:
    """Thread-safe metrics collector for a single detection run.

    Typical usage
    -------------
    metrics = RunMetrics()                        # defaults to € pricing
    metrics = RunMetrics(cost_per_image=0.007, currency="$")   # USD instead

    # inside the detection loop:
    detector.street_view.reset_call_counts()
    t0 = time.perf_counter()
    result = detector.detect_door_number(lat, lon, ...)
    elapsed = time.perf_counter() - t0
    img, meta = detector.street_view.get_call_counts()
    metrics.record(elapsed, img, meta,
                   success=result.get("success", False),
                   no_imagery=result.get("observation") == _NO_IMAGERY_OBS)

    # at the end of the run:
    metrics.print_summary()
    metrics.save_json(output_path.with_suffix(".metrics.json"))
    """

    def __init__(
        self,
        cost_per_image: float = _DEFAULT_COST_PER_IMAGE,
        cost_per_meta:  float = _DEFAULT_COST_PER_META,
        currency:       str   = _DEFAULT_CURRENCY,
    ):
        self.cost_per_image = cost_per_image
        self.cost_per_meta  = cost_per_meta
        self.currency       = currency          # symbol used in display & JSON output
        self._lock    = threading.Lock()
        self._records: List[_Record] = []

    # ── Recording ──────────────────────────────────────────────────────────────

    def record(
        self,
        elapsed_s:      float,
        image_calls:    int,
        metadata_calls: int,
        success:        bool,
        no_imagery:     bool,
    ) -> None:
        """Record the outcome of one detect_door_number() call."""
        with self._lock:
            self._records.append(_Record(
                elapsed_s=elapsed_s,
                image_calls=image_calls,
                metadata_calls=metadata_calls,
                success=success,
                no_imagery=no_imagery,
            ))

    # ── Aggregates ─────────────────────────────────────────────────────────────

    def summary(self) -> dict:
        """Return aggregate statistics as a plain dict; {} if nothing recorded."""
        with self._lock:
            records = list(self._records)

        n = len(records)
        if n == 0:
            return {}

        total_s      = sum(r.elapsed_s      for r in records)
        total_img    = sum(r.image_calls     for r in records)
        total_meta   = sum(r.metadata_calls  for r in records)
        n_success    = sum(1 for r in records if r.success)
        n_no_imagery = sum(1 for r in records if r.no_imagery)
        total_cost   = (
            total_img  * self.cost_per_image
            + total_meta * self.cost_per_meta
        )
        times = [r.elapsed_s for r in records]

        return {
            # Throughput
            "n_total":                       n,
            "n_success":                     n_success,
            "n_no_imagery":                  n_no_imagery,
            "success_rate_pct":              round(100 * n_success / n, 1),
            # Time
            "total_time_s":                  round(total_s, 2),
            "avg_time_per_detection_s":      round(total_s / n, 2),
            "min_time_s":                    round(min(times), 2),
            "max_time_s":                    round(max(times), 2),
            # API calls
            "total_image_api_calls":         total_img,
            "total_metadata_api_calls":      total_meta,
            "total_api_calls":               total_img + total_meta,
            "avg_image_calls_per_detection": round(total_img  / n, 2),
            "avg_meta_calls_per_detection":  round(total_meta / n, 2),
            # Cost
            "currency":                      self.currency,
            "cost_per_image_call":           self.cost_per_image,
            "total_cost":                    round(total_cost, 4),
            "avg_cost_per_detection":        round(total_cost / n, 4),
        }

    # ── Display ────────────────────────────────────────────────────────────────

    def print_summary(self, print_fn: Callable = print) -> None:
        """Print a formatted summary box.

        Pass ``logger.info`` as print_fn to route output to the log file instead.
        """
        s = self.summary()
        if not s:
            print_fn("  No metrics recorded.")
            return

        cur = s["currency"]
        SEP = "=" * 62
        DIV = "-" * 62
        td  = str(timedelta(seconds=int(s["total_time_s"])))

        def row(label: str, value) -> None:
            print_fn(f"    {label:<36} {value}")

        print_fn("")
        print_fn(SEP)
        print_fn("  DETECTION RUN — METRICS")
        print_fn(SEP)
        print_fn("  THROUGHPUT")
        row("Total processed",            s["n_total"])
        row("  Successful",               f"{s['n_success']}  ({s['success_rate_pct']} %)")
        row("  No Street View imagery",   s["n_no_imagery"])
        print_fn(DIV)
        print_fn("  TIME")
        row("Total",                      f"{s['total_time_s']:.1f} s  ({td})")
        row("Avg per door number",        f"{s['avg_time_per_detection_s']:.2f} s")
        row("Min / Max",                  f"{s['min_time_s']:.2f} s  /  {s['max_time_s']:.2f} s")
        print_fn(DIV)
        print_fn("  API CALLS  (Google Street View)")
        row("Image calls  [billed]",      f"{s['total_image_api_calls']}   avg {s['avg_image_calls_per_detection']:.2f} / door")
        row("Metadata calls  [free]",     f"{s['total_metadata_api_calls']}   avg {s['avg_meta_calls_per_detection']:.2f} / door")
        print_fn(DIV)
        print_fn(f"  ESTIMATED COST  ({cur}{s['cost_per_image_call']:.4f} / image call)")
        row("Total run cost",             f"{cur}{s['total_cost']:.4f}")
        row("Avg cost per door number",   f"{cur}{s['avg_cost_per_detection']:.4f}")
        print_fn(SEP)
        print_fn("")

    # ── Persistence ────────────────────────────────────────────────────────────

    def save_json(self, path: Path) -> None:
        """Write summary + individual records to a JSON file for later analysis."""
        with self._lock:
            raw = [asdict(r) for r in self._records]

        payload = {"summary": self.summary(), "records": raw}
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
