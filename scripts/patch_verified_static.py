#!/usr/bin/env python3
"""Overlay runtime-only fixes onto the checksum-verified static release."""

from __future__ import annotations

import argparse
import ast
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

RUNTIME_ASSETS = (
    "static/css/theme.css",
    "static/js/filters-enhanced.js",
    "static/js/overview.js",
    "static/js/overview_legacy.js",
    "static/js/overview_v2.js",
    "static/js/regions.js",
    "static/js/regions_drilldown.js",
    "static/js/regions_v2.js",
    "static/js/salesrep_drilldown.js",
    "static/js/salesreps.js",
    "static/js/salesreps_legacy.js",
    "static/js/suppliers.js",
    "static/js/suppliers_drilldown.js",
    "static/js/suppliers_v2.js",
)

CONTAINMENT_CSS = (
    "/* northgate-instant-layout */"
    "body[data-static-page] main section{content-visibility:auto;"
    "contain-intrinsic-size:auto 320px}"
    "body[data-static-page] #productsAvailability{content-visibility:auto;"
    "contain-intrinsic-size:auto 560px}"
)


def _constant_from_build(name: str) -> str:
    tree = ast.parse((ROOT / "build_static.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise RuntimeError(f"{name} string constant not found in build_static.py")


def patch(dist: Path, version: str) -> dict[str, object]:
    dist = dist.resolve()
    manifest_path = dist / "manifest.json"
    asset_manifest_path = dist / "asset-manifest.json"
    if not manifest_path.is_file() or not asset_manifest_path.is_file():
        raise RuntimeError(f"{dist} is not a complete verified static artifact")

    asset_manifest = json.loads(asset_manifest_path.read_text(encoding="utf-8"))
    replacements: list[str] = []
    for rel in RUNTIME_ASSETS:
        source = ROOT / "app" / rel
        if not source.is_file():
            raise RuntimeError(f"missing source asset: {source}")
        fingerprinted = str(asset_manifest.get(rel) or "")
        if not fingerprinted:
            raise RuntimeError(f"verified artifact has no mapping for {rel}")
        raw = source.read_bytes()
        (dist / rel).write_bytes(raw)
        (dist / fingerprinted).write_bytes(raw)
        replacements.append(fingerprinted)

    runtime_rel = "static/js/static-runtime.js"
    runtime_fingerprinted = str(asset_manifest.get(runtime_rel) or "")
    if not runtime_fingerprinted:
        raise RuntimeError(f"verified artifact has no mapping for {runtime_rel}")
    runtime = _constant_from_build("STATIC_RUNTIME").strip() + "\n"
    (dist / runtime_rel).write_text(runtime, encoding="utf-8")
    (dist / runtime_fingerprinted).write_text(runtime, encoding="utf-8")
    replacements.append(runtime_fingerprinted)

    pages = sorted(dist.rglob("*.html"))
    if not pages:
        raise RuntimeError("verified artifact contains no HTML pages")
    for page in pages:
        html = page.read_text(encoding="utf-8")
        if "northgate-instant-layout" not in html:
            html = html.replace("</head>", f"<style>{CONTAINMENT_CSS}</style></head>", 1)
        for rel in replacements:
            html = html.replace(rel, f"{rel}?v={version}")
        page.write_text(html, encoding="utf-8")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["patched_at"] = datetime.now(timezone.utc).isoformat()
    manifest["patched_version"] = version
    manifest["patch_contract"] = "inline-filter-options-and-instant-layout"
    by_path = {str(item.get("path")): item for item in manifest.get("rendered_pages", [])}
    for page in pages:
        rel = page.relative_to(dist).as_posix()
        if rel in by_path:
            by_path[rel]["kb"] = round(page.stat().st_size / 1024, 1)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    result = {
        "html_files": len(pages),
        "frozen_pages": sum(
            "data-static-page" in page.read_text(encoding="utf-8") for page in pages
        ),
        "assets": len(replacements),
        "version": version,
        "old_options_endpoint_present": "/api/filters/options"
        in (dist / "static/js/filters-enhanced.js").read_text(encoding="utf-8"),
        "containment_pages": sum(
            "northgate-instant-layout" in page.read_text(encoding="utf-8") for page in pages
        ),
    }
    if result["old_options_endpoint_present"]:
        raise RuntimeError("patched filter runtime still contains the retired options endpoint")
    print(json.dumps(result, indent=2))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    patch(args.dist, args.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
