"""
A recorded cost of zero is a cost, not a missing cost.

This is the whole of a defect that survived three review passes as an xfail in
`test_cross_page_consistency.py`. The Suppliers page reported 22.3% margin
where Customers, Regions and Sales Reps all reported 22.96% on revenue that
matched to the cent, and 22.96% is what the fact table says.

The formula was never wrong. `suppliers_bundle` resolves cost through

    COALESCE(cost_total, cost_per_lb * weight_lb, cost_per_unit * units)

and built `cost_total` with `NULLIF(Cost, 0)`, which exists so a zero-filled
column cannot shadow a real one - a question about which *column* to read. It
was answering a different question: whether a cost was recorded at all. So on
a stockout line (nothing shipped, no revenue, cost zero) the COALESCE fell
through to the per-unit rate, `units` fell back to QuantityOrdered because
nothing shipped, and the page charged cost for goods that never moved against
revenue that was never earned. In the demo dataset that was 3,056 lines and
$333,573 of invented cost.

The invariant these tests hold is narrow and worth stating plainly:

    a derived cost may never outrank a recorded one, and a line that earned
    no revenue may not be assigned a positive cost.

`tests/test_cross_page_consistency.py` is the end-to-end guard, but it only has
anything to compare when pointed at a built dataset. These run everywhere.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.services import fact_store

# One shipped line and one stockout line for the same supplier. The stockout
# line is the shape that caused the defect: QuantityShipped 0, Revenue 0,
# Cost 0, but QuantityOrdered and CostPrice both populated, because the order
# was placed and the goods were priced - they just never arrived.
SHIPPED_REVENUE = 1000.0
SHIPPED_COST = 700.0
STOCKOUT_ORDERED_QTY = 40.0
STOCKOUT_COST_PRICE = 9.0  # 40 x 9.00 = $360 of cost that must not be counted


def _rows() -> list[dict]:
    common = {
        "SupplierId": "SUP_A",
        "SupplierName": "Supplier A",
        "ProductId": "P1",
        "ProductName": "Prod 1",
        "CustomerId": "C1",
        "CustomerName": "Cust 1",
        "Protein": "Grocery",
        "Category": "Shelf stable",
        "SalesRepId": "R1",
        "SalesRepName": "Rep One",
        "OrderStatus": "packed",
    }
    return [
        {
            **common,
            "Date": "2025-03-10",
            "DateExpected": "2025-03-10",
            "OrderId": "O-SHIPPED",
            "Revenue": SHIPPED_REVENUE,
            "Cost": SHIPPED_COST,
            "CostPrice": 7.0,
            "QuantityShipped": 100.0,
            "QuantityOrdered": 100.0,
            "WeightLb": 200.0,
        },
        {
            **common,
            "Date": "2025-03-11",
            "DateExpected": "2025-03-11",
            "OrderId": "O-STOCKOUT",
            "Revenue": 0.0,
            "Cost": 0.0,
            "CostPrice": STOCKOUT_COST_PRICE,
            "QuantityShipped": 0.0,
            "QuantityOrdered": STOCKOUT_ORDERED_QTY,
            "WeightLb": 0.0,
        },
    ]


@pytest.fixture
def seed_stockout_line(tmp_path, monkeypatch):
    df = pd.DataFrame(_rows())
    # The DuckDB fact view resolves cost through these candidates too; seeding
    # them keeps the fixture honest about which column the resolver picked.
    df["revenue_ordered"] = df["Revenue"]
    df["cost_ordered"] = df["Cost"]
    parquet_path = tmp_path / "fact_stockout.parquet"
    df.to_parquet(parquet_path)
    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def _scope_admin(scope_hash: str) -> dict:
    return {
        "is_admin": True,
        "scope_mode": "all",
        "allowed_erp_user_ids": [],
        "sales_rep_ids": [],
        "allowed_count": 0,
        "scope_hash": scope_hash,
        "permissions_version": "1",
        "user_id": 1,
        "role": "admin",
    }


def _kpis(app_client, monkeypatch, scope_hash: str) -> dict:
    monkeypatch.setattr(
        "app.services.filters_service.scope_from_user",
        lambda _u: _scope_admin(scope_hash),
    )
    response = app_client.get(
        "/api/suppliers/bundle",
        query_string={"start": "2025-03-01", "end": "2025-03-31"},
    )
    assert response.status_code == 200, response.get_data(as_text=True)[:400]
    return (response.get_json() or {}).get("kpis") or {}


def test_zero_cost_line_does_not_fall_through_to_the_unit_rate(
    app_client, seed_stockout_line, monkeypatch
):
    """
    The regression itself: $360 of cost for 40 units that were never shipped.

    Asserted as a bound rather than an equality on purpose - the flat overhead
    charge in `margin_rules` is a tuning constant and pinning it here would
    turn a deliberate retune into a failure in an unrelated file. What must not
    move is the order of magnitude: the stockout line's rate-derived cost is
    half again the real cost of the line that actually shipped.
    """
    kpis = _kpis(app_client, monkeypatch, "scope-cost-basis-fallthrough")
    total_cost = kpis.get("total_cost")
    assert total_cost is not None, "suppliers bundle reported no cost at all"

    invented = STOCKOUT_ORDERED_QTY * STOCKOUT_COST_PRICE
    assert total_cost < SHIPPED_COST + invented / 2, (
        f"total cost {total_cost:,.2f} carries the ${invented:,.2f} rate-derived "
        f"cost of a line that shipped nothing; recorded cost for the window is "
        f"${SHIPPED_COST:,.2f}"
    )


def test_margin_matches_the_recorded_ledger(app_client, seed_stockout_line, monkeypatch):
    """
    Revenue-weighted margin off the recorded columns, within the overhead charge.

    The stockout line contributes nothing to either side, so the answer is the
    shipped line's own margin. Before the fix this read 6 points lower.
    """
    kpis = _kpis(app_client, monkeypatch, "scope-cost-basis-margin")
    margin = kpis.get("avg_margin_pct")
    assert margin is not None, "suppliers bundle reported no margin"

    ledger_margin = (SHIPPED_REVENUE - SHIPPED_COST) / SHIPPED_REVENUE * 100.0
    assert margin == pytest.approx(ledger_margin, abs=1.0), (
        f"margin {margin:.2f}% is not the ledger's {ledger_margin:.2f}% on the "
        "one line that shipped"
    )


def test_the_stockout_line_is_costed_at_zero_not_dropped(
    app_client, seed_stockout_line, monkeypatch
):
    """
    The other way this could have been "fixed", and why it would be wrong.

    Filtering zero-shipped lines out of the supplier book would also close the
    margin gap, and it would be wrong for a different reason: a stockout is not
    a non-event. It is the thing the OTIF, fill-rate and service panels are
    reporting on, and a supplier whose lines keep arriving empty is exactly the
    supplier a buyer opens this page to find. Cost it at zero; keep the row.
    """
    kpis = _kpis(app_client, monkeypatch, "scope-cost-basis-rows-kept")
    assert kpis.get("total_orders") == 2, (
        "the stockout order left the supplier book; a line that shipped nothing "
        "still happened, and the service panels are built on those lines"
    )
    assert kpis.get("total_revenue") == pytest.approx(SHIPPED_REVENUE), (
        "revenue moved while fixing a cost bug"
    )
