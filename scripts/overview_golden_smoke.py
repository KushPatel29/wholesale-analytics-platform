from __future__ import annotations

import argparse
import math
import sys
from dataclasses import replace
from datetime import datetime

import pandas as pd

from app.services.filters import FilterParams
from app.services import overview_metrics as om
from app.services import overview_v2 as ov2
from data.store import get_conn as get_duck_conn, init_views as init_duck_views, list_columns as duck_columns


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Overview golden smoke check")
    parser.add_argument("--start", required=True, help="Window start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="Window end date (YYYY-MM-DD)")
    parser.add_argument("--include-current-month", action="store_true", help="Include current month in window")
    return parser.parse_args()


def _as_timestamp(val: str) -> pd.Timestamp:
    return pd.Timestamp(datetime.fromisoformat(val))


def main() -> int:
    args = _parse_args()
    filters = FilterParams(start=_as_timestamp(args.start), end=_as_timestamp(args.end))

    ctx = ov2._compute_bundle_context(  # pylint: disable=protected-access
        filters,
        include_current_month=args.include_current_month,
        defaulted_window=False,
        cache_key=None,
    )
    payload = ctx.get("payload", {})
    kpis = payload.get("kpis", {})
    movers = payload.get("top_movers", {})

    contract = om.resolve_window_contract(filters, include_current_month=args.include_current_month)
    expanded_filters = replace(
        filters,
        start=pd.Timestamp(contract.history_start),
        end=pd.Timestamp(contract.current_end),
    )

    conn = get_duck_conn()
    init_duck_views(conn)
    cols = duck_columns(conn)
    where_sql, params, _, _, _ = ov2._where_clause(expanded_filters, cols, True)  # pylint: disable=protected-access

    date_col = ov2._safe_col(cols, "Date", "ShipDate", "OrderDate")  # pylint: disable=protected-access
    revenue_col = ov2._safe_col(cols, "Revenue", "TotalRevenue", "Sales")  # pylint: disable=protected-access
    order_col = ov2._safe_col(cols, "OrderId", "OrderID")  # pylint: disable=protected-access
    customer_col = ov2._safe_col(cols, "CustomerId", "CustomerID")  # pylint: disable=protected-access
    if not (date_col and revenue_col):
        print("Required columns missing in fact view; cannot validate.")
        return 2

    date_expr = ov2._col_expr(date_col, "DATE", "NULL")  # pylint: disable=protected-access
    revenue_expr = ov2._col_expr(revenue_col, "DOUBLE", "0")  # pylint: disable=protected-access
    order_expr = ov2._col_expr(order_col, "VARCHAR", "NULL")  # pylint: disable=protected-access
    customer_expr = ov2._col_expr(customer_col, "VARCHAR", "NULL")  # pylint: disable=protected-access

    sql = f"""
        WITH base AS (
            SELECT
                {date_expr} AS order_date,
                {revenue_expr} AS revenue,
                {order_expr} AS order_id,
                {customer_expr} AS customer_id
            FROM fact
            WHERE {where_sql}
        )
        SELECT
            SUM(CASE WHEN order_date >= CAST(? AS DATE) AND order_date < CAST(? AS DATE) THEN revenue ELSE 0 END) AS revenue,
            COUNT(DISTINCT CASE WHEN order_date >= CAST(? AS DATE) AND order_date < CAST(? AS DATE) THEN order_id END) AS orders,
            COUNT(DISTINCT CASE WHEN order_date >= CAST(? AS DATE) AND order_date < CAST(? AS DATE) THEN customer_id END) AS customers
        FROM base
    """
    params = list(params)
    params.extend(
        [
            contract.current_start.isoformat(),
            contract.current_end_exclusive.isoformat(),
            contract.current_start.isoformat(),
            contract.current_end_exclusive.isoformat(),
            contract.current_start.isoformat(),
            contract.current_end_exclusive.isoformat(),
        ]
    )
    direct = conn.execute(sql, params).fetchone()
    direct_revenue = float(direct[0] or 0.0)
    direct_orders = int(direct[1] or 0)
    direct_customers = int(direct[2] or 0)

    print("Overview headline KPI check")
    print(f"  Revenue  bundle={kpis.get('revenue')} direct={direct_revenue}")
    print(f"  Orders   bundle={kpis.get('orders')} direct={direct_orders}")
    print(f"  Customers bundle={kpis.get('customers')} direct={direct_customers}")

    tol = 0.01
    ok = True
    if not math.isclose(float(kpis.get("revenue") or 0.0), direct_revenue, rel_tol=tol, abs_tol=1.0):
        print("  [FAIL] Revenue mismatch")
        ok = False
    if int(kpis.get("orders") or 0) != direct_orders:
        print("  [FAIL] Orders mismatch")
        ok = False
    if int(kpis.get("customers") or 0) != direct_customers:
        print("  [FAIL] Customers mismatch")
        ok = False

    print("Top mover duplicate check")
    for dim in ("customer", "product", "region"):
        bucket = movers.get(dim, {})
        for side in ("gainers", "decliners"):
            labels = [str(r.get("label") or "") for r in bucket.get(side, [])]
            labels = [l for l in labels if l]
            dupes = len(labels) - len(set(labels))
            print(f"  {dim}/{side}: rows={len(labels)} dupes={dupes}")
            if dupes > 0:
                ok = False
                print(f"  [FAIL] Duplicate labels found in {dim}/{side}")

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
