#!/usr/bin/env python3
"""Cold-browser performance and correctness audit for every static page."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import statistics
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, Page, async_playwright


AUDIT_BOOTSTRAP = r"""
(() => {
  window.__northgateAudit = { longTasks: [], lcp: 0, spinnerSeen: false, waitingTextSeen: false };
  try {
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        window.__northgateAudit.longTasks.push({ start: entry.startTime, duration: entry.duration });
      }
    }).observe({ type: 'longtask', buffered: true });
  } catch (_) {}
  try {
    new PerformanceObserver((list) => {
      const entries = list.getEntries();
      if (entries.length) window.__northgateAudit.lcp = entries[entries.length - 1].startTime;
    }).observe({ type: 'largest-contentful-paint', buffered: true });
  } catch (_) {}
})();
"""


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return round(ordered[lower], 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower), 1)


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "median": round(statistics.median(values), 1) if values else 0.0,
        "p95": _percentile(values, 0.95),
        "max": round(max(values), 1) if values else 0.0,
    }


async def _measure(browser: Browser, base_url: str, item: dict[str, Any], settle_ms: int) -> dict[str, Any]:
    context = await browser.new_context(viewport={"width": 1440, "height": 900})
    page: Page = await context.new_page()
    await page.add_init_script(AUDIT_BOOTSTRAP)
    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []
    bad_responses: list[str] = []
    api_requests: list[str] = []
    option_requests: list[str] = []
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("requestfailed", lambda request: failed_requests.append(request.url))
    page.on("response", lambda response: bad_responses.append(f"{response.status} {response.url}") if response.status >= 400 else None)

    def record_request(request: Any) -> None:
        if "/api/" in request.url:
            api_requests.append(request.url)
        if "/api/filters/options" in request.url:
            option_requests.append(request.url)

    page.on("request", record_request)
    path = str(item["path"]).lstrip("/")
    url = f"{base_url.rstrip('/')}/{path}"
    started = asyncio.get_running_loop().time()
    response = None
    error = ""
    try:
        response = await page.goto(url, wait_until="load", timeout=30_000)
        await page.wait_for_timeout(settle_ms)
        details = await page.evaluate(
            """() => {
              const nav = performance.getEntriesByType('navigation')[0] || {};
              const resources = performance.getEntriesByType('resource');
              const lcpEntries = performance.getEntriesByType('largest-contentful-paint');
              const audit = window.__northgateAudit || { longTasks: [] };
              const longTasks = audit.longTasks || [];
              const longestEntry = longTasks.reduce(
                (best, entry) => Number(entry.duration) > Number(best.duration || 0) ? entry : best, {}
              );
              const longest = Number(longestEntry.duration) || 0;
              const lastLongTaskEnd = longTasks.reduce(
                (best, entry) => Math.max(best, (Number(entry.start) || 0) + (Number(entry.duration) || 0)), 0
              );
              let options = null;
              try { options = JSON.parse(document.getElementById('filter-options')?.textContent || 'null'); } catch (_) {}
              const loadMs = Number(nav.loadEventEnd) || 0;
              const visible = (el) => {
                const style = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
              };
              return {
                ttfb_ms: Number(nav.responseStart) || 0,
                dom_interactive_ms: Number(nav.domInteractive) || 0,
                load_ms: loadMs,
                tti_ms: Math.max(loadMs, lastLongTaskEnd),
                lcp_ms: Number(audit.lcp) || (lcpEntries.length ? Number(lcpEntries[lcpEntries.length - 1].startTime) || 0 : 0),
                longest_task_ms: longest,
                longest_task_start_ms: Number(longestEntry.start) || 0,
                long_task_count: longTasks.filter((entry) => Number(entry.duration) >= 50).length,
                nav_transfer_bytes: Number(nav.transferSize) || 0,
                nav_encoded_bytes: Number(nav.encodedBodySize) || 0,
                total_transfer_bytes: (Number(nav.transferSize) || 0) + resources.reduce((sum, entry) => sum + (Number(entry.transferSize) || 0), 0),
                resource_count: resources.length,
                node_count: document.getElementsByTagName('*').length,
                height_px: document.documentElement.scrollHeight,
                frozen: document.body.hasAttribute('data-static-page'),
                filter_options: !!(options?.options && Object.keys(options.options).length),
                spinner_seen: !!audit.spinnerSeen || [...document.querySelectorAll('.filters-loading-overlay,.spinner-border')].some(visible),
                waiting_text_seen: !!audit.waitingTextSeen || /Loading filters|Retry filters|Options request timed out/.test(document.body?.innerText || ''),
                main_chars: (document.querySelector('main')?.innerText || '').trim().length,
                broken_images: [...document.images].filter((image) => image.complete && image.naturalWidth === 0).length,
                title: document.title,
              };
            }"""
        )
    except Exception as exc:  # keep the full audit running and report the page
        details = {}
        error = f"{type(exc).__name__}: {exc}"
    wall_ms = round((asyncio.get_running_loop().time() - started) * 1000, 1)
    await context.close()
    page_type = "drilldown" if path.startswith("drilldowns/") else "preset" if "/presets/" in path else "workspace"
    return {
        "path": path,
        "url": url,
        "page": item.get("page") or "unknown",
        "preset": item.get("preset") or "",
        "page_type": page_type,
        "status": response.status if response is not None else 0,
        **details,
        "wall_ms": wall_ms,
        "console_error_count": len(console_errors),
        "page_error_count": len(page_errors),
        "failed_request_count": len(failed_requests),
        "bad_response_count": len(bad_responses),
        "api_request_count": len(api_requests),
        "options_request_count": len(option_requests),
        "error": error,
        "details": " | ".join((console_errors + page_errors + failed_requests + bad_responses)[:5]),
    }


async def run(args: argparse.Namespace) -> list[dict[str, Any]]:
    manifest_url = f"{args.base_url.rstrip('/')}/manifest.json"
    with urllib.request.urlopen(manifest_url, timeout=30) as response:
        manifest = json.load(response)
    items = manifest.get("rendered_pages") or []
    if not items:
        raise RuntimeError(f"No rendered_pages in {manifest_url}")

    semaphore = asyncio.Semaphore(args.concurrency)
    completed = 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        async def bounded(item: dict[str, Any]) -> dict[str, Any]:
            nonlocal completed
            async with semaphore:
                result = await _measure(browser, args.base_url, item, args.settle_ms)
                completed += 1
                if completed % 50 == 0 or completed == len(items):
                    print(f"measured {completed}/{len(items)}", flush=True)
                return result

        rows = await asyncio.gather(*(bounded(item) for item in items))
        await browser.close()
    return sorted(rows, key=lambda row: row["path"])


def write_reports(rows: list[dict[str, Any]], output: Path, base_url: str, concurrency: int, settle_ms: int) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0])
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    failures = [
        row
        for row in rows
        if row["status"] != 200
        or row.get("error")
        or not row.get("frozen")
        or not row.get("filter_options")
        or row.get("spinner_seen")
        or row.get("waiting_text_seen")
        or row.get("options_request_count")
        or row.get("console_error_count")
        or row.get("page_error_count")
        or row.get("failed_request_count")
        or row.get("bad_response_count")
        or row.get("broken_images")
    ]
    summary = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "method": {
            "browser": "Chromium via Playwright",
            "viewport": "1440x900",
            "cache": "fresh browser context per URL",
            "concurrency": concurrency,
            "settle_ms": settle_ms,
        },
        "count": len(rows),
        "status_200": sum(row["status"] == 200 for row in rows),
        "issue_count": len(failures),
        "options_requests": sum(row.get("options_request_count", 0) for row in rows),
        "api_requests": sum(row.get("api_request_count", 0) for row in rows),
        "spinner_pages": sum(bool(row.get("spinner_seen")) for row in rows),
        "console_error_pages": sum(bool(row.get("console_error_count")) for row in rows),
        "renderer_freezes": sum(float(row.get("longest_task_ms", 0)) >= 1000 for row in rows),
        "budget_violations": {
            "ttfb_over_300ms": sum(float(row.get("ttfb_ms", 0)) >= 300 for row in rows),
            "tti_over_1000ms": sum(float(row.get("tti_ms", 0)) >= 1000 for row in rows),
            "task_at_least_50ms": sum(float(row.get("longest_task_ms", 0)) >= 50 for row in rows),
            "html_over_100kb": sum(float(row.get("nav_encoded_bytes", 0)) > 100 * 1024 for row in rows),
            "dom_over_1500_nodes": sum(int(row.get("node_count", 0)) > 1500 for row in rows),
            "height_over_4000px": sum(int(row.get("height_px", 0)) > 4000 for row in rows),
        },
        "metrics_ms": {
            key: _summary([float(row.get(key, 0)) for row in rows])
            for key in ("ttfb_ms", "tti_ms", "load_ms", "lcp_ms", "longest_task_ms")
        },
        "maximums": {
            "html_transfer_kb": round(max(float(row.get("nav_transfer_bytes", 0)) for row in rows) / 1024, 1),
            "html_encoded_kb": round(max(float(row.get("nav_encoded_bytes", 0)) for row in rows) / 1024, 1),
            "total_transfer_kb": round(max(float(row.get("total_transfer_bytes", 0)) for row in rows) / 1024, 1),
            "node_count": max(int(row.get("node_count", 0)) for row in rows),
            "height_px": max(int(row.get("height_px", 0)) for row in rows),
        },
        "page_type_counts": dict(Counter(str(row["page_type"]) for row in rows)),
        "issues": [{"path": row["path"], "details": row.get("details") or row.get("error")} for row in failures[:50]],
    }
    summary_path = output.with_name(output.stem + "-summary.json")
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://kushpatel29.github.io/wholesale-analytics-platform/")
    parser.add_argument("--output", type=Path, default=Path("reports/production-filter-verification.csv"))
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--settle-ms", type=int, default=350)
    args = parser.parse_args()
    rows = asyncio.run(run(args))
    write_reports(rows, args.output, args.base_url, args.concurrency, args.settle_ms)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
