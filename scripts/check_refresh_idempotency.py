#!/usr/bin/env python3
"""
Refresh idempotency check.

Runs the production refresh command twice and validates:
- row counts/totals do not change between run #1 and run #2
- last hot-window (default 45 days) counts/totals do not change between run #1 and run #2
- no duplicate OrderLineId keys exist
- max(Date) is in the current year (>= Jan 1)

Intended to be run on the ETL host in production, after deploying fixes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import duckdb

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services import watermark_store


def _dataset_pattern(path: Path) -> str:
    if path.is_file():
        return path.as_posix()
    return (path / "**" / "*.parquet").as_posix()


def _now_utc_date() -> date:
    return datetime.now(timezone.utc).date()


@dataclass(frozen=True)
class Stats:
    rows: int
    revenue: Optional[float]
    max_date: Optional[str]
    dup_groups: Optional[int]
    last45_rows: Optional[int]
    last45_revenue: Optional[float]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "rows": self.rows,
            "revenue": self.revenue,
            "max_date": self.max_date,
            "duplicate_pk_groups": self.dup_groups,
            "last45_rows": self.last45_rows,
            "last45_revenue": self.last45_revenue,
        }


def _stats(dataset_path: Path, *, window_days: int) -> Stats:
    pattern = _dataset_pattern(dataset_path)
    con = duckdb.connect()
    con.execute(f"CREATE OR REPLACE VIEW fact_raw AS SELECT * FROM read_parquet('{pattern}', union_by_name=true);")
    cols = [r[1] for r in con.execute("PRAGMA table_info('fact_raw')").fetchall()]

    rows = int(con.execute("SELECT COUNT(*) FROM fact_raw").fetchone()[0])
    revenue = float(con.execute("SELECT COALESCE(SUM(CAST(Revenue AS DOUBLE)), 0) FROM fact_raw").fetchone()[0]) if "Revenue" in cols else None
    date_col = "Date" if "Date" in cols else ("DateExpected" if "DateExpected" in cols else None)
    max_date = None
    last45_rows: Optional[int] = None
    last45_revenue: Optional[float] = None
    if date_col:
        mx = con.execute(f"SELECT MAX(CAST({date_col} AS DATE)) FROM fact_raw").fetchone()[0]
        max_date = mx.isoformat() if mx else None
        start = (_now_utc_date() - timedelta(days=max(0, int(window_days)))).isoformat()
        end = (_now_utc_date() + timedelta(days=1)).isoformat()
        last45_rows = int(
            con.execute(
                f"SELECT COUNT(*) FROM fact_raw WHERE CAST({date_col} AS DATE) >= DATE '{start}' "
                f"AND CAST({date_col} AS DATE) < DATE '{end}'"
            ).fetchone()[0]
        )
        if "Revenue" in cols:
            last45_revenue = float(
                con.execute(
                    f"SELECT COALESCE(SUM(CAST(Revenue AS DOUBLE)), 0) FROM fact_raw "
                    f"WHERE CAST({date_col} AS DATE) >= DATE '{start}' AND CAST({date_col} AS DATE) < DATE '{end}'"
                ).fetchone()[0]
            )
    dup_groups = None
    if "OrderLineId" in cols:
        dup_groups = int(
            con.execute(
                "SELECT COUNT(*) FROM (SELECT OrderLineId, COUNT(*) c FROM fact_raw GROUP BY 1 HAVING COUNT(*) > 1)"
            ).fetchone()[0]
        )
    return Stats(
        rows=rows,
        revenue=revenue,
        max_date=max_date,
        dup_groups=dup_groups,
        last45_rows=last45_rows,
        last45_revenue=last45_revenue,
    )


def _run(cmd: str) -> int:
    proc = subprocess.run(cmd, shell=True, check=False)
    return int(proc.returncode)


def main() -> None:
    ap = argparse.ArgumentParser(description="Run refresh twice and validate idempotency + 2026 coverage.")
    ap.add_argument(
        "--cmd",
        default="python run.py refresh-fact --once",
        help="Refresh command to run twice",
    )
    ap.add_argument("--dataset-path", default=None, help="Dataset dir (defaults to resolved FACT_DATASET_PATH/PARQUET_PATH)")
    ap.add_argument("--revenue-eps", type=float, default=0.01, help="Allowed delta in revenue totals between runs")
    ap.add_argument("--window-days", type=int, default=45, help="Hot-window days for idempotency parity checks")
    args = ap.parse_args()

    dataset_path = Path(args.dataset_path).expanduser().resolve() if args.dataset_path else watermark_store.resolve_dataset_path()

    before = _stats(dataset_path, window_days=args.window_days)
    rc1 = _run(args.cmd)
    after1 = _stats(dataset_path, window_days=args.window_days)
    rc2 = _run(args.cmd)
    after2 = _stats(dataset_path, window_days=args.window_days)

    ok = True
    reasons: list[str] = []

    if rc1 != 0 or rc2 != 0:
        ok = False
        reasons.append("refresh_command_failed")

    if after1.rows != after2.rows:
        ok = False
        reasons.append("row_count_changed_between_runs")

    if after1.revenue is not None and after2.revenue is not None:
        if abs(after1.revenue - after2.revenue) > float(args.revenue_eps):
            ok = False
            reasons.append("revenue_changed_between_runs")

    if after1.last45_rows is not None and after2.last45_rows is not None:
        if int(after1.last45_rows) != int(after2.last45_rows):
            ok = False
            reasons.append("window_rows_changed_between_runs")

    if after1.last45_revenue is not None and after2.last45_revenue is not None:
        if abs(float(after1.last45_revenue) - float(after2.last45_revenue)) > float(args.revenue_eps):
            ok = False
            reasons.append("window_revenue_changed_between_runs")

    if after2.dup_groups is not None and after2.dup_groups != 0:
        ok = False
        reasons.append("duplicates_present")

    if after2.max_date:
        mx = date.fromisoformat(after2.max_date)
        if mx < date(_now_utc_date().year, 1, 1):
            ok = False
            reasons.append("max_date_not_in_current_year")

    payload = {
        "ok": ok,
        "reasons": reasons,
        "dataset_path": dataset_path.as_posix(),
        "cmd": args.cmd,
        "window_days": int(args.window_days),
        "before": before.as_dict(),
        "after1": after1.as_dict(),
        "after2": after2.as_dict(),
        "returncodes": {"run1": rc1, "run2": rc2},
    }
    print(json.dumps(payload, indent=2, default=str))
    raise SystemExit(0 if ok else 2)


if __name__ == "__main__":
    main()
