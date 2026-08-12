#!/usr/bin/env python3
"""Prove every frozen page is complete when all subresource I/O is blocked."""

from __future__ import annotations

import argparse
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import Route, sync_playwright


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        return


def check(dist: Path) -> int:
    manifest = json.loads((dist / "manifest.json").read_text(encoding="utf-8"))
    paths = sorted({str(item["path"]) for item in manifest.get("rendered_pages", [])})
    if not paths:
        raise RuntimeError("manifest contains no rendered pages")

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(dist)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    failures: list[str] = []

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for index, path in enumerate(paths, start=1):
                    page = browser.new_page(viewport={"width": 1440, "height": 900})
                    requested_api: list[str] = []

                    def block_subresources(route: Route) -> None:
                        request = route.request
                        if "/api/" in request.url:
                            requested_api.append(request.url)
                        if request.is_navigation_request() and request.resource_type == "document":
                            route.continue_()
                        else:
                            route.abort()

                    page.route("**/*", block_subresources)
                    response = page.goto(f"{origin}/{path}", wait_until="domcontentloaded", timeout=15_000)
                    result = page.evaluate(
                        """() => {
                          let optionPayload = null;
                          try {
                            optionPayload = JSON.parse(document.getElementById('filter-options')?.textContent || 'null');
                          } catch (_) {}
                          return {
                            frozen: document.body.hasAttribute('data-static-page'),
                            // textContent proves the complete prerendered DOM is
                            // present even when content-visibility deliberately
                            // defers layout for below-the-fold sections.
                            mainText: (document.querySelector('main')?.textContent || '').trim().length,
                            options: optionPayload?.options || {},
                            retired: [
                              'filtersLoadingOverlay', 'filtersRetryBtn',
                              'filtersRetryWrap', 'filtersErrorBanner'
                            ].filter(id => document.getElementById(id)),
                            waitingText: /Loading filters|Retry filters|Options request timed out/.test(document.body.innerText || ''),
                          };
                        }"""
                    )
                    issues: list[str] = []
                    if response is None or response.status != 200:
                        issues.append(f"HTTP {getattr(response, 'status', None)}")
                    if not result["frozen"]:
                        issues.append("missing frozen marker")
                    if result["mainText"] < 150:
                        issues.append(f"main content only {result['mainText']} characters")
                    if not result["options"]:
                        issues.append("inline filter options are empty")
                    if result["retired"]:
                        issues.append(f"retired UI exists: {result['retired']}")
                    if result["waitingText"]:
                        issues.append("waiting/error text is visible")
                    if requested_api:
                        issues.append(f"API requested: {requested_api[:2]}")
                    if issues:
                        failures.append(f"{path}: {'; '.join(issues)}")
                    page.close()
                    if index % 100 == 0 or index == len(paths):
                        print(f"network-blocked pages: {index}/{len(paths)}")
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()

    if failures:
        print("\n".join(f"FAIL {item}" for item in failures[:30]))
        return 1
    print(f"all {len(paths)} pages render complete content with subresource I/O blocked")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", nargs="?", default="dist", type=Path)
    args = parser.parse_args()
    return check(args.dist.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
