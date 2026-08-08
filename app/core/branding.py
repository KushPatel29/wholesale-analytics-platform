from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Any


BRANDING_PATH = Path("cache/branding.json")
BRANDING_PATH.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_BRANDING: Dict[str, Any] = {
    "brand_name": "Northgate Retail Analytics",
    "primary_color": "#0d6efd",
    "logo_filename": None,
}

_branding: Dict[str, Any] = DEFAULT_BRANDING.copy()
_mtime: float | None = None


def _validate(b: Dict[str, Any]) -> Dict[str, Any]:
    out = DEFAULT_BRANDING.copy()
    if isinstance(b, dict):
        if isinstance(b.get("brand_name"), str):
            out["brand_name"] = b["brand_name"]
        if isinstance(b.get("primary_color"), str) and b["primary_color"].startswith("#"):
            out["primary_color"] = b["primary_color"]
        if b.get("logo_filename"):
            out["logo_filename"] = str(b["logo_filename"])
    return out


def _read() -> Dict[str, Any]:
    if BRANDING_PATH.exists():
        try:
            return _validate(json.loads(BRANDING_PATH.read_text(encoding="utf-8")))
        except Exception:
            return DEFAULT_BRANDING.copy()
    return DEFAULT_BRANDING.copy()


def load_branding() -> Dict[str, Any]:
    global _branding, _mtime
    _branding = _read()
    try:
        _mtime = BRANDING_PATH.stat().st_mtime
    except Exception:
        _mtime = None
    return _branding.copy()


def get_branding() -> Dict[str, Any]:
    global _branding, _mtime
    try:
        mt = BRANDING_PATH.stat().st_mtime
    except Exception:
        mt = None
    if mt != _mtime:
        load_branding()
    return _branding.copy()


def save_branding(data: Dict[str, Any]) -> Dict[str, Any]:
    global _branding, _mtime
    val = _validate(data)
    try:
        BRANDING_PATH.write_text(json.dumps(val, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        pass
    _branding = val
    try:
        _mtime = BRANDING_PATH.stat().st_mtime
    except Exception:
        _mtime = None
    return _branding.copy()
