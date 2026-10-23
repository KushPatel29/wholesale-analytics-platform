"""
Hold the marts Snowflake built to a second, independent implementation.

The dbt models are Snowflake SQL; this re-derives every mart in pandas from the
same landed Parquet (warehouse/landing), with Snowflake's semantics where they
matter: NUMBER(18,2) casts round, weeks start on Monday, DATEDIFF counts
boundaries crossed, LAG follows the previous row, not the previous calendar
month. For each mart it compares the row count and the sum of every measure.

    python -m warehouse.snowflake.reconcile sql        # the totals query to run in Snowsight
    python -m warehouse.snowflake.reconcile compare    # the recorded result against pandas

`compare` reads warehouse/snowflake/snowflake_totals.json (what the query
returned, with its query ID) and writes warehouse/snowflake/reconciliation.csv.
Any difference of half a cent or more exits 1.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

from decimal import ROUND_HALF_UP, Decimal

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
LANDING = HERE.parents[1] / "warehouse" / "landing"
TOTALS = HERE / "snowflake_totals.json"
OUT = HERE / "reconciliation.csv"
MART_SCHEMA = "NORTHGATE_DEMO.DBT_ANALYTICS"

PREVENTABLE_REASONS = {"quality_issue", "damaged", "wrong_item", "short_issue", "vendor_return"}
PREVENTABLE_CATEGORIES = {"Production", "Warehouse"}


def _read(table: str) -> pd.DataFrame:
    return pd.read_parquet(LANDING / table / f"{table}.parquet")


def _num(series: pd.Series, places: int) -> pd.Series:
    """Snowflake's FLOAT -> NUMBER(p, s) cast: the double's exact binary value,
    rounded half away from zero. Rounding with float arithmetic instead
    (x * 100 + 0.5) left the first comparison $4.38 apart on $30.8M of sales,
    and rounding the printed decimal left it $4.75 apart; this matches to the cent."""
    q = Decimal(1).scaleb(-places)

    def one(x: float) -> float:
        if pd.isna(x):
            return np.nan
        return float(Decimal(float(x)).quantize(q, rounding=ROUND_HALF_UP))

    return series.astype("float64").map(one)


def _monday(dates: pd.Series) -> pd.Series:
    d = pd.to_datetime(dates)
    return (d - pd.to_timedelta(d.dt.day_of_week, unit="D")).dt.normalize()


def stg_sales() -> pd.DataFrame:
    s = _read("sales_fact")
    return pd.DataFrame({
        "order_date": pd.to_datetime(s["Date"]),
        "order_id": s["OrderId"].astype(str),
        "customer_id": s["CustomerId"].astype(str),
        "product_id": s["ProductId"].astype(str),
        "gross_sales": _num(s["Revenue"], 2),
        "discount_amount": _num(s["DiscountAmount"].fillna(0), 2),
        "cost_amount": _num(s["Cost"], 2),
        "units_shipped": _num(s["QuantityShipped"], 3),
        "weight_lb": _num(s["WeightLb"], 3),
    })


def stg_rma() -> pd.DataFrame:
    r = _read("return_rmas")
    r["SUBMITTED_AT"] = r["DATE_SUBMITTED"].fillna(r["CREATED_AT"])
    closed_status = r["STATUS"].str.lower().isin(["completed", "approved", "rejected"])
    r["CLOSED_AT"] = (r["FIN_CLEARED_AT"].fillna(r["REJECTED_AT"]).fillna(r["MGR_APPROVED_AT"])
                      .fillna(r["UPDATED_AT"].where(closed_status)))
    return r


def marts() -> dict[str, dict[str, float]]:
    sales = stg_sales()
    out: dict[str, dict[str, float]] = {}

    # fct_commercial_performance
    rma = stg_rma()
    approved = rma[rma["STATUS"].str.lower().isin(["approved", "completed"])]
    approved_credit = approved.groupby("ORDER_ID")["TOTAL_CREDIT_AMOUNT"].sum()
    sales["metric_month"] = sales["order_date"].dt.to_period("M").dt.start_time
    orders = sales.groupby(["metric_month", "order_id", "customer_id"], as_index=False).agg(
        gross_sales=("gross_sales", "sum"), discounts=("discount_amount", "sum"), cogs=("cost_amount", "sum"),
        units_shipped=("units_shipped", "sum"), weight_lb=("weight_lb", "sum"))
    orders["approved_returns"] = orders["order_id"].map(approved_credit).fillna(0.0)
    orders["net_sales"] = orders["gross_sales"] - orders["discounts"] - orders["approved_returns"]
    orders["gross_profit"] = orders["net_sales"] - orders["cogs"]
    cp = orders.groupby("metric_month").agg(
        order_count=("order_id", "nunique"), active_customers=("customer_id", "nunique"),
        gross_sales=("gross_sales", "sum"), discounts=("discounts", "sum"),
        approved_returns=("approved_returns", "sum"), net_sales=("net_sales", "sum"), cogs=("cogs", "sum"),
        gross_profit=("gross_profit", "sum"), units_shipped=("units_shipped", "sum"), weight_lb=("weight_lb", "sum"))
    out["fct_commercial_performance"] = {"row_count": len(cp), **cp.sum().to_dict()}

    # fct_customer_movement
    sales["net"] = sales["gross_sales"] - sales["discount_amount"]
    cm = sales.groupby(["customer_id", "metric_month"], as_index=False)["net"].sum().rename(columns={"net": "revenue"})
    cm = cm.sort_values(["customer_id", "metric_month"])
    cm["prior_revenue"] = cm.groupby("customer_id")["revenue"].shift(1)
    cm["expansion"] = (cm["revenue"] - cm["prior_revenue"].fillna(0)).clip(lower=0)
    cm["contraction"] = (cm["prior_revenue"].fillna(0) - cm["revenue"]).clip(lower=0)
    out["fct_customer_movement"] = {"row_count": len(cm), **cm[["revenue", "prior_revenue", "expansion",
                                                                "contraction"]].sum().to_dict()}

    # fct_inventory_performance
    sales["metric_week"] = _monday(sales["order_date"])
    ip = sales.groupby(["metric_week", "product_id"]).agg(
        units_sold=("units_shipped", "sum"), net_sales=("net", "sum"), cogs=("cost_amount", "sum"))
    ip["gross_margin"] = ip["net_sales"] - ip["cogs"]
    out["fct_inventory_performance"] = {"row_count": len(ip), **ip.sum().to_dict()}

    # fct_labor_performance
    lab = _read("labor_fact")
    lab["LABOR_DATE"] = pd.to_datetime(lab["LABOR_DATE"])
    lab["PAID"] = _num(lab["PAID_HOURS"], 3)
    sep = lab["SEPARATION_DATE"].notna() & (pd.to_datetime(lab["SEPARATION_DATE"]) == lab["LABOR_DATE"])
    daily = lab.groupby("LABOR_DATE").agg(active_headcount=("EMPLOYEE_KEY", "nunique"), paid_hours=("PAID", "sum"))
    daily["separations"] = lab[sep].groupby("LABOR_DATE")["EMPLOYEE_KEY"].nunique().reindex(daily.index).fillna(0)
    daily = daily.reset_index().sort_values("LABOR_DATE")
    daily["metric_month"] = daily["LABOR_DATE"].dt.to_period("M")
    lp = daily.groupby("metric_month").agg(
        opening_headcount=("active_headcount", "first"), closing_headcount=("active_headcount", "last"),
        paid_hours=("paid_hours", "sum"), separations=("separations", "sum"))
    out["fct_labor_performance"] = {"row_count": len(lp), **lp.sum().to_dict()}

    # fct_forecast_performance
    fb = _read("forecast_backtest")
    fb_actual, fb_forecast = _num(fb["ACTUAL"], 2), _num(fb["FORECAST"], 2)
    weeks = (pd.to_datetime(fb["TARGET_DATE"]) - pd.to_datetime(fb["FORECAST_DATE"])).dt.days // 7
    out["fct_forecast_performance"] = {
        "row_count": len(fb), "horizon_weeks": float(weeks.sum()), "actual": float(fb_actual.sum()),
        "forecast": float(fb_forecast.sum()), "signed_error": float((fb["FORECAST"] - fb["ACTUAL"]).sum()),
        "absolute_error": float((fb["FORECAST"] - fb["ACTUAL"]).abs().sum())}

    # fct_returns_performance
    items = _read("return_rma_items")
    prev = (items["REASON_CODE"].fillna("").str.lower().isin(PREVENTABLE_REASONS)
            | items["CATEGORY"].isin(PREVENTABLE_CATEGORIES))
    items["PREV_CREDIT"] = items["CREDIT_AMOUNT"].where(prev, 0.0)
    items["PREV"] = prev.astype(int)
    lines = items.groupby("RMA_ID").agg(return_lines=("ID", "size"), returned_units=("QTY", "sum"),
                                        preventable_credit_amount=("PREV_CREDIT", "sum"),
                                        preventable_lines=("PREV", "sum"))
    so = sales.groupby("order_id").agg(order_date=("order_date", "min"), order_units=("units_shipped", "sum"),
                                       order_revenue=("gross_sales", "sum"), order_cost=("cost_amount", "sum"))
    rp = rma.join(lines, on="ID").join(so, on="ORDER_ID")
    for col in ("return_lines", "returned_units", "preventable_lines", "preventable_credit_amount"):
        rp[col] = rp[col].fillna(0)
    rp["return_adjusted_margin"] = (rp["order_revenue"].fillna(0) - rp["order_cost"].fillna(0)
                                    - rp["TOTAL_CREDIT_AMOUNT"].fillna(0))
    rp["unit_return_rate_pct"] = rp["returned_units"] / rp["order_units"].replace(0, np.nan) * 100
    rp["revenue_return_rate_pct"] = rp["TOTAL_CREDIT_AMOUNT"].fillna(0) / rp["order_revenue"].replace(0, np.nan) * 100
    rp["days_to_return"] = (rp["SUBMITTED_AT"].dt.normalize() - rp["order_date"]).dt.days
    rp["resolution_hours"] = ((rp["CLOSED_AT"].dt.floor("h") - rp["SUBMITTED_AT"].dt.floor("h"))
                              / pd.Timedelta(hours=1))
    cols = ["total_credit_amount", "return_lines", "returned_units", "preventable_lines",
            "preventable_credit_amount", "order_units", "order_revenue", "order_cost", "return_adjusted_margin",
            "unit_return_rate_pct", "revenue_return_rate_pct", "days_to_return", "resolution_hours"]
    rp["total_credit_amount"] = rp["TOTAL_CREDIT_AMOUNT"]
    out["fct_returns_performance"] = {"row_count": len(rp), **rp[cols].sum().to_dict()}

    # pass-through marts: every numeric column
    for mart, table in (("fct_finance_monthly", "finance_month"), ("fct_marketing_performance",
                                                                    "marketing_campaign")):
        frame = _read(table)
        nums = frame.select_dtypes("number")
        out[mart] = {"row_count": len(frame), **{c.lower(): float(nums[c].sum()) for c in nums.columns}}
    out["fct_return_corrective_actions"] = {"row_count": len(_read("return_corrective_actions"))}
    return {m: {k: float(v) for k, v in d.items()} for m, d in out.items()}


def snowflake_sql(reference: dict[str, dict[str, float]]) -> str:
    parts = []
    for mart, measures in reference.items():
        for m in measures:
            expr = "count(*)" if m == "row_count" else f"sum({m})"
            parts.append(f"select '{mart}' as mart, '{m}' as measure, to_varchar({expr}) as value "
                         f"from {MART_SCHEMA}.{mart.upper()}")
    union = "\n  union all ".join(parts)
    return ("select to_json(array_agg(object_construct('mart', mart, 'measure', measure, 'value', value))\n"
            "         within group (order by mart, measure)) as totals\n"
            f"from (\n  {union}\n);\n")


def compare(reference: dict[str, dict[str, float]], recorded: list[dict]) -> tuple[list[dict], int]:
    got = {(r["mart"], r["measure"]): (float(r["value"]) if r["value"] is not None else 0.0) for r in recorded}
    rows, bad = [], 0
    for mart, measures in reference.items():
        for measure, ref in measures.items():
            sf = got.get((mart, measure))
            diff = abs(ref - sf) if sf is not None else float("inf")
            ok = diff <= 0.005  # half a cent: every measure is money, a count or a rate
            bad += not ok
            rows.append({"mart": mart, "measure": measure, "pandas": f"{ref:.6f}",
                         "snowflake": "missing" if sf is None else f"{sf:.6f}",
                         "abs_diff": f"{diff:.3g}", "match": "yes" if ok else "NO"})
    return rows, bad


def main(argv: list[str]) -> int:
    reference = marts()
    if argv[:1] == ["sql"]:
        sys.stdout.write(snowflake_sql(reference))
        return 0
    if argv[:1] == ["compare"]:
        record = json.loads(TOTALS.read_text(encoding="utf-8"))
        rows, bad = compare(reference, record["totals"])
        with OUT.open("w", newline="\n", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        print(f"{len(rows)} measures across {len(reference)} marts; {bad} differ")
        return 1 if bad else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
