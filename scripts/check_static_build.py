#!/usr/bin/env python3
"""Verify a `dist/` tree is actually static: no data request, no loading state.

Run after `build_static.py`. This is a text-level check, deliberately: it needs
no browser, so it can gate every commit, and the things it looks for are the
ones that silently come back.

A frozen page is not the page the app serves. `build_static.py` loads each one
in a real browser, lets it render, then strips every script and the embedded
payloads, because by that point the numbers and the charts are in the DOM. So
the assertions here are about the *result* of that: no app JavaScript, nothing
left to fetch, and no text claiming something is still loading.

    python scripts/check_static_build.py dist
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# HTML is measured uncompressed; a CDN will gzip it, but parse cost is here.
MAX_PAGE_KB = 400
WARN_PAGE_KB = 150

# Text that means the page is waiting. On a prerendered page there is nothing
# to wait for, so any of these is a bug.
LOADING_MARKERS = [
    "Loading filters",
    "Retry filters",
    "Reading demand and delivery history",
    "Resolving active window",
    "Building current window and prior",
    "Reading selected filters and watchlists",
    "Summarizing the visible portfolio",
    "Options request timed out",
]

# Application scripts. Their presence means the page still boots the live data
# layer, which is the whole thing the freeze pass exists to remove.
APP_SCRIPTS = [
    "filters-enhanced.js",
    "bundle-adapter.js",
    "page-state-cache.js",
    "global_filters.js",
]

# A same-origin API path in an href/src would be a real request at runtime.
API_ATTR_RE = re.compile(r'(?:href|src)="(/?(?:[^"]*/)?api/[^"]*)"')
FROZEN_MARKER = "data-static-page"


def check(dist: Path) -> int:
    if not dist.is_dir():
        print(f"FAIL: {dist} does not exist - run build_static.py first")
        return 1

    pages = sorted(dist.rglob("*.html"))
    if not pages:
        print(f"FAIL: no HTML under {dist}")
        return 1

    manifest_path = dist / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

    failures: list[str] = []
    warnings: list[str] = []
    total_kb = 0.0
    frozen = 0

    for page in pages:
        rel = page.relative_to(dist).as_posix()
        html = page.read_text(encoding="utf-8")
        kb = len(html.encode("utf-8")) / 1024
        total_kb += kb

        # `data/` holds the fragments a closed tab pulls in when opened, and the
        # per-preset section bodies. They are pieces of an already-frozen page,
        # not pages, so they carry no page marker - but they must still be inert.
        is_fragment = rel.startswith("data/")

        if kb > MAX_PAGE_KB:
            failures.append(f"{rel}: {kb:.0f} KB exceeds the {MAX_PAGE_KB} KB budget")
        elif kb > WARN_PAGE_KB and not is_fragment:
            warnings.append(f"{rel}: {kb:.0f} KB (over the {WARN_PAGE_KB} KB target)")

        if is_fragment:
            for marker in LOADING_MARKERS:
                if marker in html:
                    failures.append(f"{rel}: fragment still shows a loading state ({marker!r})")
            for match in API_ATTR_RE.finditer(html):
                failures.append(f"{rel}: fragment links to a live API path {match.group(1)[:60]}")
            continue

        if FROZEN_MARKER not in html:
            failures.append(f"{rel}: never went through the freeze pass")
            continue
        frozen += 1

        for script in APP_SCRIPTS:
            if script in html:
                failures.append(f"{rel}: still loads {script}; it will fetch on load")

        if "static-api-payloads" in html:
            failures.append(
                f"{rel}: still carries its raw API payloads - the freeze pass "
                f"should have dropped them once the DOM was rendered"
            )

        for marker in LOADING_MARKERS:
            if marker in html:
                failures.append(f"{rel}: still shows a loading state ({marker!r})")

        for match in API_ATTR_RE.finditer(html):
            failures.append(f"{rel}: links to a live API path {match.group(1)[:60]}")

        # The options the filter control needs, embedded rather than fetched.
        if '"filter-options"' not in html and "id=\"filter-options\"" not in html:
            warnings.append(f"{rel}: no embedded filter options block")

    print(f"checked {len(pages)} pages ({frozen} frozen), {total_kb/1024:.1f} MB of HTML")
    if manifest:
        stats = manifest.get("stats", {})
        print(f"manifest: {stats.get('pages', '?')} pages, "
              f"{stats.get('drilldowns', '?')} drilldowns, "
              f"{stats.get('api_hits', '?')} payloads captured, "
              f"scopes={','.join(manifest.get('scopes', []))}")

    for warning in warnings[:20]:
        print(f"  warn: {warning}")
    if len(warnings) > 20:
        print(f"  warn: ... and {len(warnings) - 20} more")
    for failure in failures[:40]:
        print(f"  FAIL: {failure}")
    if len(failures) > 40:
        print(f"  FAIL: ... and {len(failures) - 40} more")

    if failures:
        print(f"\n{len(failures)} problem(s) - this build is not static")
        return 1
    print("\nstatic build is clean: every page carries its data and waits for nothing")
    return 0


if __name__ == "__main__":
    raise SystemExit(check(Path(sys.argv[1] if len(sys.argv) > 1 else "dist").resolve()))
