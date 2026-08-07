#!/usr/bin/env python3
"""
Fact freshness guardrail for production refresh.

Checks:
- max(fact_date) is within SLA (default: today-1 in refresh timezone)
- current-month rows are present
- no duplicate OrderLineId groups
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import duckdb

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services import watermark_store


def _dataset_pattern(path: Path) -> str:
    if path.is_file():
        return path.as_posix()
    return (path / "**" / "*.parquet").as_posix()


def _resolve_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


@dataclass(frozen=True)
class Snapshot:
    row_count: int
    min_date: Optional[str]
    max_date: Optional[str]
    current_month_rows: int
    last45_rows: int
    duplicate_pk_groups: Optional[int]
    date_col: Optional[str]
    pk_col: Optional[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "row_count": self.row_count,
            "min_date": self.min_date,
            "max_date": self.max_date,
            "current_month_rows": self.current_month_rows,
            "last45_rows": self.last45_rows,
            "duplicate_pk_groups": self.duplicate_pk_groups,
            "date_col": self.date_col,
            "pk_col": self.pk_col,
        }


def _snapshot(dataset_path: Path, *, tz_name: str) -> Snapshot:
    pattern = _dataset_pattern(dataset_path)
    con = duckdb.connect()
    con.execute(f"CREATE OR REPLACE VIEW fact_raw AS SELECT * FROM read_parquet('{pattern}', union_by_name=true);")
    cols = [r[1] for r in con.execute("PRAGMA table_info('fact_raw')").fetchall()]

    date_col = "Date" if "Date" in cols else ("DateExpected" if "DateExpected" in cols else None)
    pk_col = "OrderLineId" if "OrderLineId" in cols else None
    row_count = int(con.execute("SELECT COUNT(*) FROM fact_raw").fetchone()[0])

    tz = _resolve_tz(tz_name)
    today_local = datetime.now(timezone.utc).astimezone(tz).date()
    month_start = date(today_local.year, today_local.month, 1).isoformat()
    next_month = (date(today_local.year, today_local.month, 1) + timedelta(days=32)).replace(day=1).isoformat()
    last45_start = (today_local - timedelta(days=45)).isoformat()
    tomorrow_local = (today_local + timedelta(days=1)).isoformat()

    min_date = None
    max_date = None
    current_month_rows = 0
    last45_rows = 0
    if date_col:
        mn, mx = con.execute(
            f"SELECT MIN(CAST({date_col} AS DATE)), MAX(CAST({date_col} AS DATE)) FROM fact_raw"
        ).fetchone()
        min_date = mn.isoformat() if mn else None
        max_date = mx.isoformat() if mx else None
        current_month_rows = int(
            con.execute(
                f"SELECT COUNT(*) FROM fact_raw WHERE CAST({date_col} AS DATE) >= DATE '{month_start}' "
                f"AND CAST({date_col} AS DATE) < DATE '{next_month}'"
            ).fetchone()[0]
        )
        last45_rows = int(
            con.execute(
                f"SELECT COUNT(*) FROM fact_raw WHERE CAST({date_col} AS DATE) >= DATE '{last45_start}' "
                f"AND CAST({date_col} AS DATE) < DATE '{tomorrow_local}'"
            ).fetchone()[0]
        )

    duplicate_pk_groups = None
    if pk_col:
        duplicate_pk_groups = int(
            con.execute(
                f"SELECT COUNT(*) FROM (SELECT {pk_col}, COUNT(*) c FROM fact_raw GROUP BY 1 HAVING COUNT(*) > 1)"
            ).fetchone()[0]
        )

    return Snapshot(
        row_count=row_count,
        min_date=min_date,
        max_date=max_date,
        current_month_rows=current_month_rows,
        last45_rows=last45_rows,
        duplicate_pk_groups=duplicate_pk_groups,
        date_col=date_col,
        pk_col=pk_col,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Freshness checks for the partitioned fact dataset.")
    ap.add_argument("--dataset-path", default=None, help="Dataset dir (defaults to FACT_DATASET_PATH/PARQUET_PATH)")
    ap.add_argument(
        "--timezone",
        default=(os.getenv("FACT_REFRESH_TZ") or "America/Vancouver"),
        help="Timezone used for SLA boundaries",
    )
    ap.add_argument("--sla-days", type=int, default=1, help="Max acceptable staleness in days")
    ap.add_argument("--skip-current-month", action="store_true", help="Do not fail when current-month rows are zero")
    args = ap.parse_args()

    dataset_path = Path(args.dataset_path).expanduser().resolve() if args.dataset_path else watermark_store.resolve_dataset_path()
    tz = _resolve_tz(args.timezone)
    today_local = datetime.now(timezone.utc).astimezone(tz).date()
    freshness_cutoff = today_local - timedelta(days=max(0, int(args.sla_days)))

    snap = _snapshot(dataset_path, tz_name=args.timezone)
    ok = True
    reasons: list[str] = []

    if snap.max_date:
        if date.fromisoformat(snap.max_date) < freshness_cutoff:
            ok = False
            reasons.append("stale_max_date")
    else:
        ok = False
        reasons.append("missing_date_column")

    if (not args.skip_current_month) and snap.current_month_rows <= 0:
        ok = False
        reasons.append("current_month_rows_zero")

    if snap.duplicate_pk_groups is not None and snap.duplicate_pk_groups != 0:
        ok = False
        reasons.append("duplicates_present")

    payload = {
        "ok": ok,
        "reasons": reasons,
        "dataset_path": dataset_path.as_posix(),
        "timezone": str(tz),
        "today_local": today_local.isoformat(),
        "freshness_cutoff": freshness_cutoff.isoformat(),
        "snapshot": snap.as_dict(),
    }
    print(json.dumps(payload, indent=2, default=str))
    raise SystemExit(0 if ok else 2)


if __name__ == "__main__":
    main()
