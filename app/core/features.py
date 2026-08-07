from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict


FEATURES_PATH = Path("cache/features.json")
FEATURES_PATH.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_FLAGS: Dict[str, bool] = {
    "enable_churn": True,
    "enable_prophet": True,
    "enable_2fa": True,
    "enable_legacy_pandas_endpoints": False,
}

_flags: Dict[str, bool] = DEFAULT_FLAGS.copy()
_mtime: float | None = None


def _validate(flags: Dict[str, object]) -> Dict[str, bool]:
    out: Dict[str, bool] = DEFAULT_FLAGS.copy()
    for k, v in flags.items():
        if k in DEFAULT_FLAGS and isinstance(v, bool):
            out[k] = bool(v)
    return out


def _read() -> Dict[str, bool]:
    if FEATURES_PATH.exists():
        try:
            data = json.loads(FEATURES_PATH.read_text(encoding="utf-8"))
            return _validate(data if isinstance(data, dict) else {})
        except Exception:
            return DEFAULT_FLAGS.copy()
    return DEFAULT_FLAGS.copy()


def load_flags() -> Dict[str, bool]:
    global _flags, _mtime
    _flags = _read()
    try:
        _mtime = FEATURES_PATH.stat().st_mtime
    except Exception:
        _mtime = None
    return _flags.copy()


def get_flags() -> Dict[str, bool]:
    global _flags, _mtime
    try:
        mt = FEATURES_PATH.stat().st_mtime
    except Exception:
        mt = None
    if mt != _mtime:
        load_flags()
    return _flags.copy()


def save_flags(flags: Dict[str, object]) -> Dict[str, bool]:
    global _flags, _mtime
    val = _validate(flags)
    try:
        FEATURES_PATH.parent.mkdir(parents=True, exist_ok=True)
        FEATURES_PATH.write_text(json.dumps(val, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        pass
    _flags = val
    try:
        _mtime = FEATURES_PATH.stat().st_mtime
    except Exception:
        _mtime = None
    return _flags.copy()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return default


def legacy_pandas_enabled() -> bool:
    """
    Feature flag to allow legacy pandas-heavy endpoints to remain reachable.
    Defaults to False; can be overridden via ENABLE_LEGACY_PANDAS_ENDPOINTS env var
    or the persisted features file.
    """
    env_override = _env_bool("ENABLE_LEGACY_PANDAS_ENDPOINTS", False)
    if env_override:
        return True
    return bool(get_flags().get("enable_legacy_pandas_endpoints", False))
