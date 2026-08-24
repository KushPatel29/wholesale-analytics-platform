#!/usr/bin/env python3
"""Fail when an active page's first-party JavaScript exceeds 200 KB gzipped."""

from __future__ import annotations

import gzip
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = ROOT / "app" / "templates"
STATIC_ROOT = ROOT / "app" / "static"
MAX_JS_GZIP_KB = 200.0
ACTIVE_TEMPLATES = (
    "overview/index_v3.html",
    "customers/kpis_v3.html",
    "customers/rfm_v2.html",
    "customers/cohorts_v2.html",
    "customers/clv_v2.html",
    "products/index_v4.html",
    "inventory/index.html",
    "finance/index.html",
    "marketing/index.html",
    "regions/index_v2.html",
    "suppliers/index_v2.html",
    "labor/index.html",
    "salesreps/index.html",
    "returns/index.html",
    "returns/analytics.html",
    "stakeholder_report/index.html",
    "metrics/index.html",
)
JS_PATTERNS = (
    re.compile(r"filename\s*=\s*['\"](js/[^'\"]+\.js)['\"]"),
    re.compile(r"(?:src|href)=['\"]/static/(js/[^?'\"]+\.js)"),
)


def _scripts(text: str) -> set[str]:
    return {match.group(1) for pattern in JS_PATTERNS for match in pattern.finditer(text)}


def check() -> int:
    base_scripts = _scripts((TEMPLATE_ROOT / "base.html").read_text(encoding="utf-8"))
    failures: list[str] = []
    for relative in ACTIVE_TEMPLATES:
        template = TEMPLATE_ROOT / relative
        if not template.exists():
            failures.append(f"{relative}: active template is missing")
            continue
        scripts = base_scripts | _scripts(template.read_text(encoding="utf-8"))
        total = 0
        missing: list[str] = []
        for script in sorted(scripts):
            source = STATIC_ROOT / script
            if not source.exists():
                missing.append(script)
                continue
            total += len(gzip.compress(source.read_bytes(), compresslevel=9))
        total_kb = total / 1024
        if missing:
            failures.append(f"{relative}: missing first-party scripts: {', '.join(missing)}")
        if total_kb > MAX_JS_GZIP_KB:
            failures.append(
                f"{relative}: {total_kb:.1f} KB first-party JS gzipped exceeds {MAX_JS_GZIP_KB:.0f} KB"
            )
    if failures:
        print("Frontend budget failures:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(f"Frontend JS budgets OK across {len(ACTIVE_TEMPLATES)} active templates (<={MAX_JS_GZIP_KB:.0f} KB gzipped).")
    return 0


if __name__ == "__main__":
    raise SystemExit(check())
