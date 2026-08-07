#!/usr/bin/env python3
"""
One-time fact dataset dedupe / rebuild.

Goal: remove duplicates by business key (default: OrderLineId) across the full
partitioned dataset while keeping the newest row (UpdatedAt desc, then Date desc).

This is intended to be run once in production after deploying the upsert fixes.

Safety:
- Writes to a new dataset directory and then swaps atomically.
- Optionally keeps a *_prev copy for rollback.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import timezone, datetime
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services import watermark_store
from etl import partition_writer


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _schema_fingerprint(df: pd.DataFrame) -> str:
    cols = [{"name": c, "dtype": str(dtype)} for c, dtype in df.dtypes.items()]
    raw = json.dumps(cols, sort_keys=True)
    import hashlib

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def dedupe_and_rebuild(
    *,
    dataset_path: Path,
    pk_col: str,
    date_col: str,
    updated_col: Optional[str],
    keep_prev: bool,
) -> Dict[str, Any]:
    if not dataset_path.exists():
        raise SystemExit(f"Dataset path does not exist: {dataset_path}")

    dataset = ds.dataset(dataset_path.as_posix(), format="parquet", partitioning="hive")
    table = dataset.to_table()  # one-time job; read everything
    df = table.to_pandas()

    # Drop partition columns if present; they'll be re-derived.
    for c in ("year", "month"):
        if c in df.columns:
            df.drop(columns=[c], inplace=True, errors="ignore")

    if pk_col not in df.columns:
        raise SystemExit(f"Primary key column missing: {pk_col}")
    if date_col not in df.columns:
        raise SystemExit(f"Date column missing: {date_col}")

    work = df.copy()
    sort_cols = []
    asc = []
    if updated_col and updated_col in work.columns:
        work[updated_col] = pd.to_datetime(work[updated_col], errors="coerce")
        sort_cols.append(updated_col)
        asc.append(True)
    work[date_col] = pd.to_datetime(work[date_col], errors="coerce")
    sort_cols.append(date_col)
    asc.append(True)
    sort_cols.append(pk_col)
    asc.append(True)

    work = work.sort_values(by=sort_cols, ascending=asc, kind="mergesort", ignore_index=True)
    deduped = work.drop_duplicates(subset=[pk_col], keep="last")

    # Rebuild as a fresh partitioned dataset and swap atomically.
    tmp_dir = dataset_path.parent / f".{dataset_path.name}.dedupe-{os.getpid()}-{int(time.time() * 1000)}"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Partition and write via pyarrow.dataset
    ts = pd.to_datetime(deduped[date_col], errors="coerce")
    deduped = deduped.copy()
    deduped["year"] = ts.dt.year.astype("Int64")
    deduped["month"] = ts.dt.month.astype("Int64")
    table_out = pa.Table.from_pandas(deduped, preserve_index=False)
    ds.write_dataset(
        data=table_out,
        base_dir=tmp_dir.as_posix(),
        format="parquet",
        partitioning=["year", "month"],
        partitioning_flavor="hive",
        existing_data_behavior="overwrite_or_ignore",
    )

    min_iso = pd.to_datetime(ts.min(), errors="coerce").tz_localize(None).isoformat() if ts.notna().any() else None
    max_iso = pd.to_datetime(ts.max(), errors="coerce").tz_localize(None).isoformat() if ts.notna().any() else None

    schema_hash = _schema_fingerprint(deduped.drop(columns=["year", "month"], errors="ignore"))
    manifest = watermark_store.read_manifest(dataset_path)
    manifest = dict(manifest or {})
    manifest.update(
        {
            "dataset_version": str(int(time.time() * 1000)),
            "rows": int(len(deduped)),
            "row_count": int(len(deduped)),
            "min_date": min_iso,
            "max_date": max_iso,
            "min_dateexpected": min_iso,
            "max_dateexpected": max_iso,
            "date_column": manifest.get("date_column") or date_col,
            "schema_hash": schema_hash,
            "schema_fingerprint": schema_hash,
            "built_at_utc": _now_utc_iso(),
            "last_refresh_utc": _now_utc_iso(),
            "status": "deduped",
        }
    )
    watermark_store.write_manifest_atomic(manifest, dataset_path=tmp_dir)
    partition_writer._swap_dataset_dirs(tmp_dir, dataset_path, keep_prev=keep_prev)
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description="One-time dedupe + rebuild for the partitioned fact dataset.")
    ap.add_argument("--dataset-path", default=None, help="Dataset dir (defaults to resolved FACT_DATASET_PATH/PARQUET_PATH)")
    ap.add_argument("--pk-col", default="OrderLineId", help="Business key / primary key column")
    ap.add_argument("--date-col", default=None, help="Date column for partitioning (defaults to manifest date_column)")
    ap.add_argument("--updated-col", default=None, help="Updated timestamp column (defaults to manifest watermark_column)")
    ap.add_argument("--keep-prev", action="store_true", help="Keep *_prev dataset directory for rollback")
    args = ap.parse_args()

    dataset_path = Path(args.dataset_path).expanduser().resolve() if args.dataset_path else watermark_store.resolve_dataset_path()
    manifest = watermark_store.read_manifest(dataset_path)
    date_col = args.date_col or manifest.get("date_column") or "Date"
    updated_col = args.updated_col or manifest.get("watermark_column") or "UpdatedAt"

    out = dedupe_and_rebuild(
        dataset_path=dataset_path,
        pk_col=args.pk_col,
        date_col=str(date_col),
        updated_col=str(updated_col) if updated_col else None,
        keep_prev=bool(args.keep_prev),
    )
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
