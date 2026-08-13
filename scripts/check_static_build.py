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

import html as html_stdlib
import json
import gzip
import re
import sys
import urllib.parse
from pathlib import Path

# These moved when the section tabs came off and every page began shipping
# whole. The old limits (100 KB raw, 1,499 nodes, 3,999px) were what forced the
# tabs, and for a public demo that trade was backwards - analysis behind an
# unlabelled tab is analysis nobody sees.
#
# The size limit is now measured **gzipped**, because that is what a visitor
# actually downloads and the raw number was misleading by roughly 6x. Measured
# across the full build: 8.8-32.8 KB gzipped per page, against 42-264 KB raw.
# 60 KB leaves real headroom while still catching a page that runs away.
#
# Nodes and height are generous because a full page is legitimately long, but
# they are not unlimited: they still catch a table that forgets to cap itself.
# Worst observed at the time of writing: 3,287 nodes (products) and 10,185px
# (labor).
MAX_PAGE_GZIP_KB = 60
MAX_FRAGMENT_KB = 400
# Charts now carry a hover label per data point, as an empty <b> positioned over
# the frozen image. That is up to 44 nodes per chart, and the pages that draw a
# dozen charts pay for it - Labor and Sales Reps most. The nodes are leaf
# elements with no text and no layout of their own, so the cost is bytes rather
# than paint, and the budget moves to match rather than the hover coming back
# off. Worst observed after the change: 3,652 nodes (products).
MAX_PAGE_NODES = 4_400
MAX_PAGE_HEIGHT = 11_500

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
LOCAL_ATTR_RE = re.compile(r'\s(?:href|src|action)="([^"]+)"')
CSS_URL_RE = re.compile(r"url\(([^)]+)\)")
FROZEN_MARKER = "data-static-page"


def _local_target(dist: Path, base: Path, raw: str) -> Path | None:
    value = html_stdlib.unescape(raw).strip().strip("'\"")
    if not value or value.startswith(("#", "//")):
        return None
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme or not parsed.path:
        return None
    path = urllib.parse.unquote(parsed.path)
    target = dist / path.lstrip("/") if path.startswith("/") else base / path
    target = target.resolve()
    if path.endswith("/") or target.is_dir():
        target /= "index.html"
    return target


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
    includes_drilldowns = bool(manifest.get("drilldowns"))

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

        if is_fragment:
            if kb > MAX_FRAGMENT_KB:
                failures.append(f"{rel}: {kb:.0f} KB exceeds the {MAX_FRAGMENT_KB} KB budget")
        else:
            # Gzipped, because that is the number a visitor pays. Raw HTML on
            # this site compresses about 6x, so measuring raw was rejecting
            # pages that cost 15 KB to download.
            gzip_kb = len(gzip.compress(html.encode("utf-8"), 9)) / 1024
            if gzip_kb > MAX_PAGE_GZIP_KB:
                failures.append(
                    f"{rel}: {gzip_kb:.0f} KB gzipped ({kb:.0f} KB raw) "
                    f"exceeds the {MAX_PAGE_GZIP_KB} KB budget"
                )

        if is_fragment:
            for marker in LOADING_MARKERS:
                if marker in html:
                    failures.append(f"{rel}: fragment still shows a loading state ({marker!r})")
            for match in API_ATTR_RE.finditer(html):
                failures.append(f"{rel}: fragment links to a live API path {match.group(1)[:60]}")
            if includes_drilldowns:
                # Section fragments are inserted into pages at different
                # directory depths, so their ordinary relative URLs cannot be
                # resolved against the fragment file itself. Drilldown links
                # have a stable root marker; validate the referenced entity
                # file directly so links inside closed tabs are crawled too.
                for match in LOCAL_ATTR_RE.finditer(html):
                    raw = html_stdlib.unescape(match.group(1))
                    path = urllib.parse.urlsplit(raw).path
                    if "drilldowns/" not in path:
                        continue
                    suffix = path.split("drilldowns/", 1)[1]
                    target = (dist / "drilldowns" / urllib.parse.unquote(suffix)).resolve()
                    if not target.exists():
                        failures.append(f"{rel}: missing fragment drilldown link {path[:60]}")
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

        for match in LOCAL_ATTR_RE.finditer(html):
            target = _local_target(dist, page.parent, match.group(1))
            if target is None:
                continue
            try:
                target.relative_to(dist)
            except ValueError:
                failures.append(f"{rel}: local link escapes the build root ({match.group(1)[:60]})")
                continue
            if not target.exists():
                target_rel = target.relative_to(dist).as_posix()
                if not includes_drilldowns and target_rel.startswith("drilldowns/"):
                    # CI's fast smoke build deliberately uses --no-drilldowns;
                    # the full publish build includes them and checks the links.
                    continue
                failures.append(f"{rel}: missing local asset or page {match.group(1)[:60]}")

        # The options the filter control needs, embedded rather than fetched.
        if '"filter-options"' not in html and "id=\"filter-options\"" not in html:
            warnings.append(f"{rel}: no embedded filter options block")

    # A stylesheet can exist while its font or background asset does not. This
    # is especially easy to miss after content hashing because CSS URLs are
    # relative to the stylesheet, not the HTML document that loaded it.
    for stylesheet in sorted((dist / "static").rglob("*.css")):
        rel = stylesheet.relative_to(dist).as_posix()
        css = stylesheet.read_text(encoding="utf-8")
        for match in CSS_URL_RE.finditer(css):
            target = _local_target(dist, stylesheet.parent, match.group(1))
            if target is None:
                continue
            try:
                target.relative_to(dist)
            except ValueError:
                failures.append(f"{rel}: CSS URL escapes the build root ({match.group(1)[:60]})")
                continue
            if not target.exists():
                failures.append(f"{rel}: missing CSS asset {match.group(1)[:60]}")

    budget_entries = manifest.get("rendered_pages") or manifest.get("pages", [])
    for entry in budget_entries:
        rel = str(entry.get("path") or entry.get("key") or "page")
        nodes = int(entry.get("nodes") or 0)
        height = int(entry.get("height") or 0)
        if nodes > MAX_PAGE_NODES:
            failures.append(f"{rel}: {nodes} first-paint nodes exceed the {MAX_PAGE_NODES} budget")
        if height > MAX_PAGE_HEIGHT:
            failures.append(f"{rel}: {height}px first-paint height exceeds the {MAX_PAGE_HEIGHT}px budget")

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
