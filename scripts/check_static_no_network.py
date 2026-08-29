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
                            // A page can legitimately have no filter bar. Finance
                            // reports entity-level statements, and a balance sheet
                            // has no region dimension to filter by, so it sets
                            // `hide_global_filters` and base.html stamps the body
                            // `disabled`. Demanding an options payload there would
                            // force the page to publish filters it does not honour.
                            filtersDisabled: document.body.dataset.filtersHandler === 'disabled',
                            retired: [
                              'filtersLoadingOverlay', 'filtersRetryBtn',
                              'filtersRetryWrap', 'filtersErrorBanner'
                            ].filter(id => document.getElementById(id)),
                            waitingText: /Loading filters|Retry filters|Options request timed out/.test(document.body.innerText || ''),
                            h1Count: document.querySelectorAll('main h1').length,
                            duplicateIds: [...document.querySelectorAll('[id]')]
                              .map(el => el.id).filter((id, index, all) => id && all.indexOf(id) !== index)
                              .filter((id, index, all) => all.indexOf(id) === index),
                            missingButtonTypes: document.querySelectorAll('button:not([type])').length,
                            unsafeBlankLinks: [...document.querySelectorAll('a[target="_blank"]')]
                              .filter(link => !String(link.rel || '').split(/\\s+/).includes('noopener')).length,
                            headingSkips: (() => {
                              const levels = [...document.querySelectorAll('main h1,main h2,main h3,main h4,main h5,main h6')]
                                .filter(el => !el.closest('[hidden]'))
                                .map(el => Number(el.tagName.slice(1)));
                              return levels.filter((level, index) => index > 0 && level > levels[index - 1] + 1).length;
                            })(),
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
                    if not result["options"] and not result["filtersDisabled"]:
                        issues.append("inline filter options are empty")
                    if result["options"] and result["filtersDisabled"]:
                        issues.append("filters are disabled but an options payload was published")
                    if result["retired"]:
                        issues.append(f"retired UI exists: {result['retired']}")
                    if result["waitingText"]:
                        issues.append("waiting/error text is visible")
                    if result["h1Count"] != 1:
                        issues.append(f"expected one main H1, found {result['h1Count']}")
                    if result["duplicateIds"]:
                        issues.append(f"duplicate IDs: {result['duplicateIds'][:3]}")
                    if result["missingButtonTypes"]:
                        issues.append(f"{result['missingButtonTypes']} button(s) lack an explicit type")
                    if result["unsafeBlankLinks"]:
                        issues.append(f"{result['unsafeBlankLinks']} target=_blank link(s) lack rel=noopener")
                    if result["headingSkips"]:
                        issues.append(f"{result['headingSkips']} heading-level skip(s)")
                    if requested_api:
                        issues.append(f"API requested: {requested_api[:2]}")

                    # The first pass deliberately blocks every subresource to
                    # prove that the meaningful DOM is prerendered. Reload with
                    # the local runtime available before checking interaction;
                    # event listeners cannot exist when their script is
                    # intentionally blocked.
                    page.unroute("**/*")
                    page.goto(f"{origin}/{path}", wait_until="domcontentloaded", timeout=15_000)
                    dropdown = page.locator('[data-bs-toggle="dropdown"]').first
                    if dropdown.count():
                        dropdown.focus()
                        dropdown.press("ArrowDown")
                        keyboard_state = page.evaluate(
                            """() => ({
                              expanded: document.activeElement?.closest('.dropdown-menu.show') != null,
                              menuitem: document.activeElement?.matches('a[href],button,[role="menuitem"]') || false,
                            })"""
                        )
                        if not keyboard_state["expanded"] or not keyboard_state["menuitem"]:
                            issues.append("header dropdown does not open and focus an item with ArrowDown")
                        page.keyboard.press("Escape")
                        if dropdown.get_attribute("aria-expanded") != "false" or not dropdown.evaluate("el => el === document.activeElement"):
                            issues.append("header dropdown Escape does not close and restore focus")
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
