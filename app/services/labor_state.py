from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from flask import current_app, has_app_context


STATE_FILENAME = "labor_etl_state.json"


def resolve_dataset_path(dataset_path: Optional[Path] = None) -> Path:
    if dataset_path is not None:
        return Path(dataset_path).expanduser().resolve()
    if has_app_context():
        try:
            return Path(current_app.config["LABOR_PARQUET_PATH"]).expanduser().resolve()
        except Exception:
            pass
    raw = os.getenv("LABOR_PARQUET_PATH") or "cache/labor/fact_dataset"
    return Path(raw).expanduser().resolve()


def resolve_state_path(dataset_path: Optional[Path] = None) -> Path:
    override = os.getenv("LABOR_ETL_STATE_PATH")
    if override:
        return Path(override).expanduser().resolve()
    dataset = resolve_dataset_path(dataset_path)
    base = dataset.parent if dataset.name == "fact_dataset" else dataset
    return base / STATE_FILENAME


def _default_state() -> Dict[str, Any]:
    return {
        "dataset_version": None,
        "initial_backfill_complete": False,
        "watermark": {
            "labor_date_max": None,
        },
        "last_success": None,
        "last_error": None,
        "rows_last_pull": 0,
        "windows_last_pull": 0,
        "updated_at": None,
    }


def _normalize_state(state: Dict[str, Any]) -> Dict[str, Any]:
    base = _default_state()
    merged = dict(base)
    merged.update(state or {})
    watermark = dict(base.get("watermark") or {})
    watermark.update(merged.get("watermark") or {})
    merged["watermark"] = watermark
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
