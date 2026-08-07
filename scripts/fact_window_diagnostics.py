#!/usr/bin/env python3
"""
Current-month diagnostics for incremental fact refresh.

Outputs comparable stats for:
- raw extract (full window, updated_after disabled)
- raw extract (incremental-style updated_after applied)
- parquet dataset
- app query layer (full-window and default-window)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import data_loader
from app.services import etl_state, fact_store, watermark_store


def _resolve_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def _dataset_pattern(path: Path) -> str:
    if path.is_file():
        return path.as_posix()
    return (path / "**" / "*.parquet").as_posix()


def _choose_date_col(df: pd.DataFrame) -> Optional[str]:
    for col in ("Date", "DateExpected", "EffectiveDate"):
        if col in df.columns:
            return col
    return None


def _stats_from_df(df: pd.DataFrame, *, month_start: date, end_exclusive: date) -> Dict[str, Any]:
    if df is None or df.empty:
        return {"rows": 0, "max_date": None, "current_month_rows": 0, "date_col": None}
    date_col = _choose_date_col(df)
    if not date_col:
        return {"rows": int(len(df)), "max_date": None, "current_month_rows": 0, "date_col": None}
    ts = pd.to_datetime(df[date_col], errors="coerce")
    max_date = ts.max().date().isoformat() if ts.notna().any() else None
    cur_mask = ts.notna() & (ts.dt.date >= month_start) & (ts.dt.date < end_exclusive)
    return {
        "rows": int(len(df)),
        "max_date": max_date,
        "current_month_rows": int(cur_mask.sum()),
        "date_col": date_col,
    }


def _parquet_stats(dataset_path: Path, *, month_start: date, end_exclusive: date) -> Dict[str, Any]:
    pattern = _dataset_pattern(dataset_path)
    con = duckdb.connect()
    con.execute(f"CREATE OR REPLACE VIEW fact_raw AS SELECT * FROM read_parquet('{pattern}', union_by_name=true);")
    cols = [r[1] for r in con.execute("PRAGMA table_info('fact_raw')").fetchall()]
    date_col = "Date" if "Date" in cols else ("DateExpected" if "DateExpected" in cols else None)
    payload: Dict[str, Any] = {"rows": int(con.execute("SELECT COUNT(*) FROM fact_raw").fetchone()[0]), "date_col": date_col}
    if not date_col:
        payload["max_date"] = None
        payload["current_month_rows"] = 0
        return payload

    mn, mx = con.execute(f"SELECT MIN(CAST({date_col} AS DATE)), MAX(CAST({date_col} AS DATE)) FROM fact_raw").fetchone()
    payload["min_date"] = mn.isoformat() if mn else None
    payload["max_date"] = mx.isoformat() if mx else None
    payload["current_month_rows"] = int(
        con.execute(
            f"SELECT COUNT(*) FROM fact_raw WHERE CAST({date_col} AS DATE) >= DATE '{month_start.isoformat()}' "
            f"AND CAST({date_col} AS DATE) < DATE '{end_exclusive.isoformat()}'"
        ).fetchone()[0]
    )
    return payload


def _updated_after_from_state(dataset_path: Path, lag_days: int) -> Optional[datetime]:
    state = etl_state.load_state(dataset_path=dataset_path)
    _, updated_at_max = etl_state.get_watermark(state)
    if not updated_at_max:
        return None
    ts = pd.to_datetime(updated_at_max, errors="coerce", utc=True)
    if pd.isna(ts):
        return None
    out = ts.to_pydatetime() - timedelta(days=max(0, int(lag_days)))
    return out.replace(tzinfo=None)


def main() -> None:
    ap = argparse.ArgumentParser(description="Diagnose current-month gaps across raw/parquet/app layers.")
    ap.add_argument("--dataset-path", default=None, help="Dataset dir (defaults to FACT_DATASET_PATH/PARQUET_PATH)")
    ap.add_argument("--timezone", default=(os.getenv("FACT_REFRESH_TZ") or "America/Vancouver"), help="Window timezone")
    ap.add_argument("--lag-days", type=int, default=int(os.getenv("FACT_REFRESH_LAG_DAYS") or 7), help="Incremental lag days")
    ap.add_argument("--month", default=None, help="Month to inspect (YYYY-MM). Defaults to current month in timezone.")
    args = ap.parse_args()

    tz = _resolve_tz(args.timezone)
    today_local = datetime.now(timezone.utc).astimezone(tz).date()
    if args.month:
        month_start = datetime.strptime(f"{args.month}-01", "%Y-%m-%d").date()
        month_label = args.month
    else:
        month_start = date(today_local.year, today_local.month, 1)
        month_label = f"{today_local.year:04d}-{today_local.month:02d}"
    end_exclusive = today_local + timedelta(days=1)
    dataset_path = Path(args.dataset_path).expanduser().resolve() if args.dataset_path else watermark_store.resolve_dataset_path()

    raw_full_df = data_loader.get_dataframe(start=month_start, end=today_local, updated_after=None)
    raw_full = _stats_from_df(raw_full_df, month_start=month_start, end_exclusive=end_exclusive)

    updated_after = _updated_after_from_state(dataset_path, lag_days=args.lag_days)
    raw_incr_df = data_loader.get_dataframe(start=month_start, end=today_local, updated_after=updated_after)
    raw_incremental = _stats_from_df(raw_incr_df, month_start=month_start, end_exclusive=end_exclusive)
    raw_incremental["updated_after"] = updated_after.isoformat() if updated_after else None

    parquet = _parquet_stats(dataset_path, month_start=month_start, end_exclusive=end_exclusive)

    app_full_df = fact_store.query_fact(
        filters={"start_date": month_start.isoformat(), "end_date": today_local.isoformat()},
        apply_default_window=False,
        use_cache=False,
    )
    app_full = _stats_from_df(app_full_df, month_start=month_start, end_exclusive=end_exclusive)

    app_default_df = fact_store.query_fact(filters={}, apply_default_window=True, use_cache=False)
    app_default = _stats_from_df(app_default_df, month_start=month_start, end_exclusive=end_exclusive)

    diagnosis: list[str] = []
    if int(raw_full.get("current_month_rows") or 0) > int(raw_incremental.get("current_month_rows") or 0):
        diagnosis.append("incremental_updated_after_filter_reduces_current_month_rows")
    if int(raw_full.get("current_month_rows") or 0) > int(parquet.get("current_month_rows") or 0):
        diagnosis.append("drop_between_extraction_and_parquet_write")
    if int(parquet.get("current_month_rows") or 0) > int(app_full.get("current_month_rows") or 0):
        diagnosis.append("drop_between_parquet_and_app_query")
    if (app_default.get("max_date") or "1900-01-01") < month_start.isoformat():
        diagnosis.append("app_default_window_excludes_current_month")

    payload = {
        "timezone": str(tz),
        "today_local": today_local.isoformat(),
        "month": month_label,
        "month_start": month_start.isoformat(),
        "month_end_exclusive": end_exclusive.isoformat(),
        "date_field_for_month_logic": raw_full.get("date_col") or parquet.get("date_col") or app_full.get("date_col"),
        "raw_extract_full_window": raw_full,
        "raw_extract_incremental_style": raw_incremental,
        "parquet": parquet,
        "app_query_full_window": app_full,
        "app_query_default_window": app_default,
        "diagnosis": diagnosis,
    }
    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
