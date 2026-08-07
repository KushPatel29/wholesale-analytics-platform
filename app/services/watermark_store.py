from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pandas as pd

MANIFEST_NAME = "_manifest.json"
DEFAULT_DATASET_DIR = Path("cache") / "fact_dataset"

LOOKBACK_DAYS = int(os.getenv("FACT_REFRESH_LOOKBACK_DAYS", "14"))
REFRESH_INTERVAL_SECONDS = int(os.getenv("FACT_REFRESH_INTERVAL_SECONDS", "300"))

WATERMARK_CANDIDATES = (
    "UpdatedAt",
    "UpdatedAt_line",
    "UpdatedAt_order",
    "ModifiedAt",
    "ModifiedAt_line",
    "ModifiedAt_order",
    "LastModifiedAt",
    "LastModified",
    "RowVersion",
    "rowversion",
    "CreatedAt",
)

DATE_CANDIDATES = (
    "DateExpected",
    "Date",
    "EffectiveDate",
)


def resolve_dataset_path() -> Path:
    """
    Resolve the *partitioned* fact dataset directory.

    Production has historically used a mix of:
    - `PARQUET_PATH` pointing to a single snapshot file (e.g. cache/fact.parquet)
    - `PARQUET_PATH` pointing to a cache directory (e.g. cache/)
    - `FACT_DATASET_PATH` pointing to the partitioned dataset dir (e.g. cache/fact_dataset/)

    For the DuckDB-backed app + ETL, we want to consistently operate on the
    partitioned dataset directory so readers don't accidentally scan unrelated
    parquet files (or `_prev` / `.tmp-*` datasets) which can double totals.
    """
    raw = os.getenv("FACT_DATASET_PATH") or os.getenv("PARQUET_PATH")
    if not raw:
        return DEFAULT_DATASET_DIR.expanduser().resolve()

    path = Path(raw).expanduser().resolve()

    # If a snapshot file path was provided, treat the adjacent partitioned
    # dataset directory as canonical.
    if path.suffix == ".parquet":
        return path.parent / "fact_dataset"

    if path.is_dir():
        # Treat a directory value as a cache root unless it is explicitly the
        # dataset directory itself. This prevents readers from scanning every
        # parquet under the cache root (including `_prev` / `.tmp-*` datasets),
        # which can double totals.
        if path.name != "fact_dataset" and not (path / MANIFEST_NAME).exists():
            return (path / "fact_dataset").expanduser().resolve()
        # If the directory itself is the dataset dir (manifest present), use it.
        if (path / MANIFEST_NAME).exists():
            return path
        # If the directory is a cache root and contains a fact_dataset child,
        # prefer that to avoid scanning every parquet under the cache root.
        child = path / "fact_dataset"
        if (child / MANIFEST_NAME).exists():
            return child
        if child.exists():
            return child

    return path


def manifest_path(dataset_path: Optional[Path] = None) -> Path:
    base = dataset_path or resolve_dataset_path()
    return base / MANIFEST_NAME


def read_manifest(dataset_path: Optional[Path] = None) -> Dict[str, Any]:
    path = manifest_path(dataset_path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _atomic_write(path: Path, payload: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def write_manifest_atomic(payload: Dict[str, Any], dataset_path: Optional[Path] = None) -> Path:
    path = manifest_path(dataset_path)
    return _atomic_write(path, payload)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_dataset_version(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not payload.get("dataset_version"):
        payload["dataset_version"] = str(int(time.time() * 1000))
    return payload


def get_watermark(manifest: Optional[Dict[str, Any]] = None) -> Optional[str]:
    meta = manifest or read_manifest()
    for key in ("watermark", "last_sql_watermark", "watermark_dt"):
        val = meta.get(key)
        if val:
            return str(val)
    return None


def watermark_to_date(watermark: Optional[str]) -> Optional[date]:
    if not watermark:
        return None
    ts = pd.to_datetime(watermark, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.date()


def choose_watermark_column(df: pd.DataFrame) -> Optional[str]:
    if df is None or df.empty:
        return None
    for col in WATERMARK_CANDIDATES:
        if col in df.columns:
            return col
    return None


def choose_date_column(df: pd.DataFrame) -> Optional[str]:
    if df is None or df.empty:
        return None
    for col in DATE_CANDIDATES:
        if col in df.columns:
            return col
    return None


def compute_watermark(df: pd.DataFrame) -> Tuple[Optional[str], Optional[str]]:
    if df is None or df.empty:
        return None, None
    col = choose_watermark_column(df)
    if col and col in df.columns:
        ts = pd.to_datetime(df[col], errors="coerce")
        if ts.notna().any():
            return ts.max().tz_localize(None).isoformat(), col
    fallback = choose_date_column(df)
    if fallback and fallback in df.columns:
        ts = pd.to_datetime(df[fallback], errors="coerce")
        if ts.notna().any():
            return ts.max().tz_localize(None).isoformat(), fallback
    return None, None
