"""
Land the demo's source tables as Parquet, for Snowflake to load.

    python -m warehouse.snowflake.export_landing [--out warehouse/landing]

The dbt project in models/ reads nine source tables. The app keeps them in
three places: the order-line fact and the labour fact as DuckDB views over
Parquet, the finance and marketing months as Parquet, and the Returns workflow
as SQLite tables. This writes each one, as the app itself reads it, to one
Parquet file per table:

    sales_fact, labor_fact, finance_month, marketing_campaign,
    forecast_backtest, return_rmas, return_rma_items, return_corrective_actions

Column names are upper-cased except sales_fact's, so Snowflake resolves the
staging models' unquoted names; sales_fact keeps the source system's
PascalCase, which the staging model quotes on purpose.

forecast_backtest is not a table in the app: the Planner computes a
rolling-origin backtest when the page loads. It is landed here from the app's
own function, chain level, at the horizons the Planner reports (1, 4 and 8
weeks), so the mart scores what the page scores.

Run it after building the demo dataset (the Dockerfile's seed steps).
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "warehouse" / "landing"
AUTH_DB = ROOT / "app" / "auth" / "auth.db"

SALES_COLUMNS = ["OrderLineId", "Date", "OrderId", "CustomerId", "ProductId", "SupplierName", "RegionName",
                 "Revenue", "DiscountAmount", "Cost", "QuantityShipped", "WeightLb"]
# Only the columns the dbt sources read are landed: the Returns tables also
# carry (synthetic) contact details and free-text notes a warehouse has no use for.
RETURN_COLUMNS = {
    "return_rmas": ["id", "rma_number", "order_id", "order_date", "customer_id", "date_submitted",
                    "created_at", "updated_at", "mgr_approved_at", "fin_cleared_at", "rejected_at",
                    "status", "primary_reason", "primary_category", "total_credit_amount"],
    "return_rma_items": ["id", "rma_id", "sku", "qty", "credit_amount", "reason_code", "category"],
    "return_corrective_actions": None,  # every column: the mart reads nearly all of them
}
LABOR_COLUMNS = ["labor_date", "employee_key", "department_name", "paid_hours", "paid_hours_allocated",
                 "separation_date", "separation_type"]
FORECAST_HORIZONS = (1, 4, 8)


def _upper(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.rename(columns={c: c.upper() for c in frame.columns})


def _parquet(frame: pd.DataFrame, out: Path, table: str) -> Path:
    path = out / table / f"{table}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Microsecond timestamps: Snowflake reads nanosecond Parquet timestamps as
    # NUMBER unless told otherwise.
    for col in frame.columns:
        if pd.api.types.is_datetime64_any_dtype(frame[col]):
            frame[col] = frame[col].astype("datetime64[us]")
    frame.to_parquet(path, index=False, coerce_timestamps="us", allow_truncated_timestamps=True)
    return path


def sales_fact() -> pd.DataFrame:
    from app.services import fact_store

    con = fact_store.get_conn()
    fact_store.init_views(con)
    cols = ", ".join(f'"{c}"' for c in SALES_COLUMNS)
    frame = con.execute(f'select {cols} from fact order by "OrderLineId"').df()
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.date
    return frame


def labor_fact() -> pd.DataFrame:
    from app.services import labor_store

    con = labor_store.get_conn()
    labor_store._ensure_views(con)
    cols = ", ".join(LABOR_COLUMNS)
    frame = con.execute(f"select {cols} from labor_fact order by labor_date, employee_key, transaction_index").df()
    return _upper(frame)


def _dataset(name: str) -> pd.DataFrame:
    files = sorted((ROOT / "cache" / name / "fact_dataset").rglob("*.parquet"))
    if not files:
        raise SystemExit(f"no {name} dataset under cache/{name}: build the demo dataset first")
    return _upper(pd.concat([pd.read_parquet(f) for f in files], ignore_index=True))


def forecast_backtest(sales: pd.DataFrame) -> pd.DataFrame:
    from app.services import planning

    weekly = planning._weekly_revenue(sales[["Date", "Revenue"]])
    rows = []
    for horizon in FORECAST_HORIZONS:
        for row in planning._weekly_rolling_origin_rows(weekly, horizon=horizon):
            target = pd.Period(row["period"], freq="W-SUN").start_time.date()
            rows.append({
                "forecast_date": target - pd.Timedelta(weeks=horizon),
                "target_date": target,
                "horizon_weeks": horizon,
                "product_id": "ALL",
                "region_name": "ALL",
                "method": row["method"],
                "actual": round(float(row["actual"]), 2),
                "forecast": round(float(row["forecast"]), 2),
            })
    return _upper(pd.DataFrame(rows))


def returns(table: str) -> pd.DataFrame:
    cols = RETURN_COLUMNS[table]
    with sqlite3.connect(AUTH_DB) as con:
        frame = pd.read_sql_query(f"select {', '.join(cols) if cols else '*'} from {table} order by id", con)
        declared = {row[1]: str(row[2]).upper() for row in con.execute(f"pragma table_info({table})")}
    # Type every column from its declared SQLite type, so an empty table (the
    # demo has no corrective actions yet) still lands with a usable schema.
    for col in frame.columns:
        kind = declared.get(col, "")
        if "DATE" in kind or "TIME" in kind:
            frame[col] = pd.to_datetime(frame[col], errors="coerce")
        elif "INT" in kind or "BOOL" in kind:
            frame[col] = pd.to_numeric(frame[col], errors="coerce").astype("Int64")
        elif any(k in kind for k in ("FLOAT", "REAL", "NUMERIC", "DECIMAL", "DOUBLE")):
            frame[col] = pd.to_numeric(frame[col], errors="coerce").astype("float64")
        else:
            frame[col] = frame[col].astype("string")
    return _upper(frame)


def export(out: Path) -> dict[str, int]:
    sales = sales_fact()
    tables = {
        "sales_fact": sales,
        "labor_fact": labor_fact(),
        "finance_month": _dataset("finance"),
        "marketing_campaign": _dataset("marketing"),
        "forecast_backtest": forecast_backtest(sales),
        **{t: returns(t) for t in RETURN_COLUMNS},
    }
    counts = {}
    for name, frame in tables.items():
        _parquet(frame, out, name)
        counts[name] = len(frame)
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    for name, n in export(args.out).items():
        print(f"{name:28s} {n:>8,} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
