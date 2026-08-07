from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from flask import current_app, has_app_context

from app.services import watermark_store

STATE_FILENAME = "etl_state.json"


def resolve_cache_dir(dataset_path: Optional[Path] = None, *, cache_dir: Optional[Path] = None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir).expanduser().resolve()
    if has_app_context():
        try:
            return Path(current_app.config["CACHE_DIR"]).expanduser().resolve()
        except Exception:
            pass
    env_cache = os.getenv("CACHE_DIR") or os.getenv("DATA_DIR")
    if env_cache:
        return Path(env_cache).expanduser().resolve()
    if dataset_path is None:
        dataset_path = watermark_store.resolve_dataset_path()
    base = Path(dataset_path).expanduser().resolve()
    if base.suffix == ".parquet":
        base = base.parent
    if base.name == "fact_dataset":
        base = base.parent
    return base


def resolve_state_path(dataset_path: Optional[Path] = None, *, cache_dir: Optional[Path] = None) -> Path:
    raw = os.getenv("ETL_STATE_PATH")
    if raw:
        return Path(raw).expanduser().resolve()
    base = resolve_cache_dir(dataset_path, cache_dir=cache_dir)
    return base / STATE_FILENAME


def _default_state() -> Dict[str, Any]:
    return {
        "dataset_version": None,
        "initial_load_done": False,
        "watermark": {
            "dateexpected_max": None,
            "updated_at_max": None,
        },
        "last_success": None,
        "last_success_utc": None,
        "last_error": None,
        "rows_last_pull": 0,
        "updated_at": None,
    }


def _normalize_state(state: Dict[str, Any]) -> Dict[str, Any]:
    base = _default_state()
    merged = dict(base)
    merged.update(state or {})
    watermark = dict(base.get("watermark") or {})
    watermark.update(merged.get("watermark") or {})
    merged["watermark"] = watermark
    if merged.get("last_success_utc") is None and merged.get("last_success"):
        merged["last_success_utc"] = merged.get("last_success")
    if merged.get("last_success") is None and merged.get("last_success_utc"):
        merged["last_success"] = merged.get("last_success_utc")
    return merged


def load_state(*, dataset_path: Optional[Path] = None) -> Dict[str, Any]:
    path = resolve_state_path(dataset_path)
    if not path.exists():
        return _default_state()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _default_state()
    return _normalize_state(payload)


def save_state(state: Dict[str, Any], *, dataset_path: Optional[Path] = None) -> None:
    path = resolve_state_path(dataset_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _normalize_state(state)
    payload["updated_at"] = time.time()
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_watermark(state: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    watermark = (state or {}).get("watermark") or {}
    return watermark.get("dateexpected_max"), watermark.get("updated_at_max")


def set_watermark(state: Dict[str, Any], *, dateexpected_max: Optional[str], updated_at_max: Optional[str]) -> Dict[str, Any]:
    state = _normalize_state(state)
    watermark = state.get("watermark") or {}
    if dateexpected_max is not None:
        watermark["dateexpected_max"] = dateexpected_max
    if updated_at_max is not None:
        watermark["updated_at_max"] = updated_at_max
    state["watermark"] = watermark
    return state


def bootstrap_state_if_missing(
    state: Dict[str, Any],
    *,
    dataset_path: Optional[Path] = None,
) -> Dict[str, Any]:
    path = resolve_state_path(dataset_path)
    state_exists = path.exists()
    if state_exists and state.get("initial_load_done"):
        return state
    try:
        from app.services import fact_store
    except Exception:
        return state
    min_date, max_date = fact_store.get_dataset_min_max_dates(dataset_path=dataset_path)
    if not max_date:
        return state
    dateexpected_max, updated_at_max = get_watermark(state)
    if not dateexpected_max:
        state = set_watermark(state, dateexpected_max=max_date, updated_at_max=updated_at_max)
    state["initial_load_done"] = True
    if not state.get("last_success_utc"):
        state["last_success_utc"] = state.get("last_success") or _now_utc_iso()
    state["last_success"] = state.get("last_success_utc")
    save_state(state, dataset_path=dataset_path)
    return state
