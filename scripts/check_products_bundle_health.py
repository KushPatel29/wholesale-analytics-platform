#!/usr/bin/env python3
"""Products bundle health/perf check for production smoke monitoring.

Usage:
  python scripts/check_products_bundle_health.py --iterations 20 --workers 4
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Allow direct execution without requiring PYTHONPATH setup.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from app.services.products_bundle import build_products_bundle


def _run_once(filters: Dict[str, Any], scope: Dict[str, Any], args: Dict[str, Any]) -> Tuple[float, bool]:
    started = time.perf_counter()
    payload = build_products_bundle(filters, scope, args)
    elapsed = time.perf_counter() - started
    has_error = bool((payload or {}).get("error"))
    return elapsed, has_error


def _percentile(values: List[float], pct: float) -> float:
    ordered = sorted(values)
    idx = int(round((pct / 100.0) * (len(ordered) - 1)))
    idx = max(0, min(len(ordered) - 1, idx))
    return ordered[idx]


def main() -> int:
    parser = argparse.ArgumentParser(description="Products bundle health/perf check")
    parser.add_argument("--start", default="2025-12-01")
    parser.add_argument("--end", default="2026-03-06")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-p95-sec", type=float, default=5.0)
    parser.add_argument("--max-error-rate", type=float, default=0.0)
    parser.add_argument("--bubble-top-n", type=int, default=250)
    args = parser.parse_args()

    filters = {"start": args.start, "end": args.end, "date_preset": "custom"}
    scope: Dict[str, Any] = {}
    query = {
        "page": "1",
        "page_size": "25",
        "sort_by": "revenue",
        "sort_dir": "desc",
        "bubble_top_n": str(args.bubble_top_n),
        "bubble_color": "uplift_pct",
        "bubble_y": "velocity",
    }

    latencies: List[float] = []
    errors = 0
    exceptions = 0

    # Warm-up.
    for _ in range(min(3, args.iterations)):
        _run_once(filters, scope, query)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [executor.submit(_run_once, filters, scope, query) for _ in range(max(1, args.iterations))]
        for future in as_completed(futures):
            try:
                elapsed, has_error = future.result()
                latencies.append(elapsed)
                errors += int(has_error)
            except Exception:
                exceptions += 1

    total = len(latencies) + exceptions
    error_rate = (errors + exceptions) / total if total else 1.0
    if not latencies:
        print("FAIL: no successful runs")
        return 2

    p95 = _percentile(latencies, 95.0)
    p50 = _percentile(latencies, 50.0)
    mean = statistics.mean(latencies)

    print(
        "products_bundle_health total=%d ok=%d payload_errors=%d exceptions=%d "
        "latency_sec p50=%.4f p95=%.4f mean=%.4f max=%.4f"
        % (total, len(latencies), errors, exceptions, p50, p95, mean, max(latencies))
    )

    failed = False
    if p95 > args.max_p95_sec:
        print("FAIL: p95 %.4f exceeded threshold %.4f" % (p95, args.max_p95_sec))
        failed = True
    if error_rate > args.max_error_rate:
        print("FAIL: error_rate %.4f exceeded threshold %.4f" % (error_rate, args.max_error_rate))
        failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
