#!/usr/bin/env python3
"""
Fact dataset smoke checks (DuckDB).

Checks:
- max(Date) is in the current year (>= Jan 1)
- no duplicate business keys (OrderLineId)
- basic totals can be computed

This does not mutate data and is safe to run on production hosts.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
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


def run_checks(*, dataset_path: Optional[Path] = None) -> Dict[str, Any]:
    ds_path = (dataset_path or watermark_store.resolve_dataset_path()).expanduser().resolve()
    pattern = _dataset_pattern(ds_path)

    con = duckdb.connect()
    con.execute(f"CREATE OR REPLACE VIEW fact_raw AS SELECT * FROM read_parquet('{pattern}', union_by_name=true);")

    cols = [r[1] for r in con.execute("PRAGMA table_info('fact_raw')").fetchall()]
    pk_col = "OrderLineId" if "OrderLineId" in cols else None
    date_col = "Date" if "Date" in cols else ("DateExpected" if "DateExpected" in cols else None)

    out: Dict[str, Any] = {
        "dataset_path": ds_path.as_posix(),
        "pattern": pattern,
        "pk_col": pk_col,
        "date_col": date_col,
        "row_count": int(con.execute("SELECT COUNT(*) FROM fact_raw").fetchone()[0]),
    }

    if date_col:
        mn, mx = con.execute(f"SELECT MIN(CAST({date_col} AS DATE)), MAX(CAST({date_col} AS DATE)) FROM fact_raw").fetchone()
        out["min_date"] = mn.isoformat() if mn else None
        out["max_date"] = mx.isoformat() if mx else None

        year_start = date(_now_utc_date().year, 1, 1).isoformat()
        out["rows_current_year"] = int(
            con.execute(f"SELECT COUNT(*) FROM fact_raw WHERE CAST({date_col} AS DATE) >= DATE '{year_start}'").fetchone()[0]
        )
        out["current_year_start"] = year_start

    if "Revenue" in cols:
        out["revenue_sum"] = float(con.execute("SELECT COALESCE(SUM(CAST(Revenue AS DOUBLE)), 0) FROM fact_raw").fetchone()[0])

    if pk_col:
        out["duplicate_pk_groups"] = int(
            con.execute(
                f"SELECT COUNT(*) FROM (SELECT {pk_col}, COUNT(*) c FROM fact_raw GROUP BY 1 HAVING COUNT(*) > 1)"
            ).fetchone()[0]
        )

    ok = True
    reasons: list[str] = []
    if pk_col and out.get("duplicate_pk_groups", 0) != 0:
        ok = False
        reasons.append("duplicates_present")
    if date_col and out.get("max_date"):
        mx = date.fromisoformat(str(out["max_date"]))
        if mx < date(_now_utc_date().year, 1, 1):
            ok = False
            reasons.append("max_date_not_in_current_year")
    out["ok"] = ok
    out["reasons"] = reasons
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Smoke checks for the partitioned fact dataset.")
    ap.add_argument("--dataset-path", default=None, help="Dataset dir (defaults to resolved FACT_DATASET_PATH/PARQUET_PATH)")
    args = ap.parse_args()

    payload = run_checks(dataset_path=Path(args.dataset_path).expanduser().resolve() if args.dataset_path else None)
    print(json.dumps(payload, indent=2, default=str))
    raise SystemExit(0 if payload.get("ok") else 2)


if __name__ == "__main__":
    main()
