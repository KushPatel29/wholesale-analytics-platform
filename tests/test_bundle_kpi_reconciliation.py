"""
Every page bundle must answer the same question with the same number.

Revenue, cost, profit, margin and order count are one company fact each. Five
bundles compute them independently, and the whole reason they are computed
independently is speed - each page's query is shaped for the grain that page
needs. That is a reasonable design and it has exactly one failure mode, which
this file exists to catch: the shapes drift apart and nobody notices, because
the only way to see it is to hold two pages side by side under one filter and
no test did that.

Two defects were live when this was written, and both had survived every
existing suite:

* **Suppliers** reported 22.31% margin where the other four reported 22.96%.
  `NULLIF(Cost, 0)` in the cost resolver turned zero-revenue stockout lines
  into "no cost recorded", so the COALESCE fell through to a per-unit rate and
  charged $333,573 for goods that never shipped. Tracked as an xfail in
  `test_cross_page_consistency.py` for three sessions.
* **Products** reported $32.5M of revenue where the other four reported $51.0M,
  because its whole KPI block was summed from the top 200 SKUs of 880 - and its
  cost came off the raw ledger column while every other page adds the flat
  overhead charge, so its profit was one charge per line too high on top.

`test_cross_page_consistency.py` scrapes the rendered pages, which is the
stronger test and only runs when pointed at a built dataset. This one runs on a
fixture, so it runs everywhere: CI, a clean clone, and a machine mid-demo.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.services import fact_store

# bundle endpoint -> the KPI key that bundle uses for each shared idea.
# Divergent key names are half of why this went unnoticed: `avg_margin_pct`,
# `margin_pct` and `total_profit` all sort into different places in a payload
# diff, so nothing lined them up.
SHARED_KPIS: dict[str, dict[str, str]] = {
    "/api/customers/bundle": {
        "revenue": "revenue", "cost": "cost", "profit": "profit",
        "margin_pct": "margin_pct", "orders": "orders",
    },
    "/api/suppliers/bundle": {
        "revenue": "total_revenue", "cost": "total_cost", "profit": "total_profit",
        "margin_pct": "avg_margin_pct", "orders": "total_orders",
    },
    "/api/regions/bundle": {
        "revenue": "total_revenue", "profit": "profit", "margin_pct": "margin_pct",
        "orders": "orders",
    },
    "/api/salesreps/bundle": {
        "revenue": "revenue", "cost": "cost", "profit": "profit",
        "margin_pct": "margin_pct", "orders": "orders",
    },
    "/api/products/bundle": {
        "revenue": "revenue", "profit": "profit", "margin_pct": "margin_pct",
        "orders": "orders",
    },
}

WINDOW = {"start": "2025-01-01", "end": "2025-12-31"}

# Past the 200-row slice the Products summary was built from. 240 keeps the
# fixture small enough to stay fast while putting the cap comfortably inside it.
TAIL_SKU_LIMIT = 240


def _fact_rows() -> list[dict]:
    """
    Four suppliers, 240 SKUs, three regions, two reps, twelve months.

    Deliberately awkward in the two ways that caused real divergence:

    * a **long tail** - SKU-08 is a hundredth the size of SKU-01, so a bundle
      that truncates its SKU list drops revenue a full-population sum keeps;
    * **zero-revenue stockout lines** carrying an ordered quantity and a unit
      cost, which is the shape that made a zero cost look like a missing one.

    Revenue and cost are *not* columns here, deliberately. The DuckDB fact view
    derives both from pack counts and per-unit prices and overrides whatever the
    parquet carried, so a fixture that writes `Revenue` directly gets `NULL`
    back and silently exercises a different app. Supplying the pack columns is
    what makes this fixture a small version of the real thing rather than a
    lookalike - and finding that out cost a test run that reported the five
    bundles disagreeing by the entire company.
    """
    rows: list[dict] = []
    skus = [
        # sku, supplier, region, rep, unit price, unit cost, units/month
        ("SKU-01", "SUP_A", "West",    "R1", 40.0, 30.0, 260),
        ("SKU-02", "SUP_A", "West",    "R1", 22.0, 16.0, 180),
        ("SKU-03", "SUP_B", "Central", "R1", 15.0, 11.5, 140),
        ("SKU-04", "SUP_B", "Central", "R2",  9.0,  6.2,  95),
        ("SKU-05", "SUP_C", "East",    "R2", 55.0, 44.0,  40),
        ("SKU-06", "SUP_C", "East",    "R2",  6.0,  4.1,  30),
        ("SKU-07", "SUP_D", "West",    "R1",  3.0,  2.2,  12),
        ("SKU-08", "SUP_D", "Central", "R2",  2.0,  1.4,   4),
    ]
    # A tail past 200 SKUs. The Products bundle summed the top 200 by revenue,
    # so a catalogue that fits inside the cap cannot show the defect: eight SKUs
    # reconciled perfectly against the broken code. These are small on purpose -
    # together they are a few percent of revenue, which is what a truncated sum
    # loses and what makes the loss easy to mistake for rounding.
    suppliers = ("SUP_A", "SUP_B", "SUP_C", "SUP_D")
    regions = ("West", "Central", "East")
    for tail_index in range(9, TAIL_SKU_LIMIT + 1):
        skus.append(
            (
                f"SKU-{tail_index:03d}",
                suppliers[tail_index % len(suppliers)],
                regions[tail_index % len(regions)],
                "R1" if tail_index % 2 else "R2",
                round(2.0 + (tail_index % 7) * 0.5, 2),
                round(1.4 + (tail_index % 7) * 0.35, 2),
                1 + tail_index % 5,
            )
        )
    for month in range(1, 13):
        day = f"2025-{month:02d}-15"
        for index, (sku, supplier, region, rep, price, unit_cost, units) in enumerate(skus):
            rows.append(
                {
                    "Date": day, "DateExpected": day,
                    "OrderId": f"O-{month:02d}-{index}",
                    "OrderLineId": f"L-{month:02d}-{index}",
                    "SupplierId": supplier, "SupplierName": f"Supplier {supplier[-1]}",
                    "ProductId": sku, "SKU": sku, "ProductName": f"Product {sku[-2:]}",
                    "CustomerId": f"C{index % 3}", "CustomerName": f"Customer {index % 3}",
                    "RegionId": region[:2].upper(), "RegionName": region,
                    "SalesRepId": rep, "SalesRepName": f"Rep {rep[-1]}",
                    "PrimarySalesRepId": rep, "PrimarySalesRepName": f"Rep {rep[-1]}",
                    "Protein": "Grocery", "Category": "Shelf stable",
                    "ShippingMethodName": "DC",
                    # The view reads these; Revenue and Cost fall out of them.
                    "UnitOfBillingId": 1,  # priced per each, not per pound
                    "pack_count": 1.0,
                    "pack_item_count_sum": float(units),
                    "pack_weight_lb_sum": float(units) * 1.5,
                    "Price": price,
                    "CostPrice": unit_cost,
                    "QuantityShipped": float(units), "QuantityOrdered": float(units),
                    "WeightLb": float(units) * 1.5,
                    "OrderStatus": "packed",
                }
            )
        # One stockout line a month: ordered, priced, never shipped, never sold.
        rows.append(
            {
                "Date": day, "DateExpected": day,
                "OrderId": f"O-{month:02d}-STOCKOUT",
                "OrderLineId": f"L-{month:02d}-STOCKOUT",
                "SupplierId": "SUP_C", "SupplierName": "Supplier C",
                "ProductId": "SKU-05", "SKU": "SKU-05", "ProductName": "Product 05",
                "CustomerId": "C1", "CustomerName": "Customer 1",
                "RegionId": "EA", "RegionName": "East",
                "SalesRepId": "R2", "SalesRepName": "Rep 2",
                "PrimarySalesRepId": "R2", "PrimarySalesRepName": "Rep 2",
                "Protein": "Grocery", "Category": "Shelf stable",
                "ShippingMethodName": "DC",
                # Nothing shipped, so nothing was billed: zero packs at $55.
                # The unit cost and the ordered quantity survive, because the
                # order was placed and the goods were priced - and that pair is
                # exactly what a cost resolver reaches for when it mistakes a
                # recorded zero for a missing value.
                "UnitOfBillingId": 1,
                "pack_count": 1.0,
                "pack_item_count_sum": 0.0,
                "pack_weight_lb_sum": 0.0,
                "Price": 55.0, "CostPrice": 44.0,
                "QuantityShipped": 0.0, "QuantityOrdered": 25.0,
                "WeightLb": 0.0,
                "OrderStatus": "packed",
            }
        )
    return rows


@pytest.fixture(scope="module")
def seed_reconciliation_dataset(tmp_path_factory):
    df = pd.DataFrame(_fact_rows())
    parquet_path = tmp_path_factory.mktemp("recon") / "fact_reconciliation.parquet"
    df.to_parquet(parquet_path)

    import os

    previous = os.environ.get("PARQUET_PATH")
    os.environ["PARQUET_PATH"] = str(parquet_path)
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    if previous is None:
        os.environ.pop("PARQUET_PATH", None)
    else:
        os.environ["PARQUET_PATH"] = previous
    fact_store.reset_duckdb_state()


def _scope_admin() -> dict:
    return {
        "is_admin": True,
        "scope_mode": "all",
        "allowed_erp_user_ids": [],
        "sales_rep_ids": [],
        "allowed_count": 0,
        "scope_hash": "scope-admin-kpi-reconciliation",
        "permissions_version": "1",
        "user_id": 1,
        "role": "admin",
    }


@pytest.fixture
def observed(app_client, seed_reconciliation_dataset, monkeypatch):
    """{idea: {page: value}} for every shared KPI each bundle actually returns."""
    monkeypatch.setattr(
        "app.services.filters_service.scope_from_user", lambda _u: _scope_admin()
    )
    readings: dict[str, dict[str, float]] = {}
    reachable = 0
    for url, mapping in SHARED_KPIS.items():
        response = app_client.get(url, query_string=WINDOW)
        if response.status_code != 200:
            continue
        kpis = (response.get_json() or {}).get("kpis") or {}
        if not kpis:
            continue
        reachable += 1
        page = url.split("/")[2]
        for idea, key in mapping.items():
            value = kpis.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                readings.setdefault(idea, {})[page] = float(value)
    if reachable < 2:
        pytest.skip(f"only {reachable} bundle(s) answered; nothing to reconcile")
    return readings


@pytest.mark.parametrize("idea", ["revenue", "cost", "profit", "orders"])
def test_bundles_agree_on(observed, idea):
    """
    Exact agreement, to floating-point noise.

    A tolerance here would be a place for the next divergence to hide: the
    Products gap was 36% and the Suppliers gap 0.85%, and a tolerance loose
    enough to admit the second admits a great deal that is genuinely wrong.
    These are sums over one row set - they should agree to the cent.
    """
    values = observed.get(idea) or {}
    if len(values) < 2:
        pytest.skip(f"fewer than two bundles report {idea}: {sorted(values)}")
    largest = max(abs(v) for v in values.values()) or 1.0
    spread = max(values.values()) - min(values.values())
    assert spread / largest < 1e-9, (
        f"bundles disagree on {idea} by {spread:,.2f}:\n"
        + "\n".join(f"    {page:12s} {value:>18,.4f}" for page, value in sorted(values.items()))
    )


def test_bundles_agree_on_margin_pct(observed):
    """Margin is a ratio, so it gets its own tolerance: a hundredth of a point."""
    values = observed.get("margin_pct") or {}
    if len(values) < 2:
        pytest.skip(f"fewer than two bundles report margin: {sorted(values)}")
    spread = max(values.values()) - min(values.values())
    assert spread < 0.01, (
        f"bundles disagree on margin % by {spread:.4f} points:\n"
        + "\n".join(f"    {page:12s} {value:>10.4f}%" for page, value in sorted(values.items()))
    )


def test_margin_is_derived_from_the_revenue_and_profit_on_the_same_page(observed):
    """
    Internal consistency, which is a different failure from cross-page drift.

    A page can agree with its neighbours on revenue and profit and still print a
    margin that is neither - that is what an unweighted mean of per-row margins
    does, and it is what the Suppliers card labelled "Avg Margin %" used to be.
    """
    revenue, profit, margins = (
        observed.get("revenue") or {},
        observed.get("profit") or {},
        observed.get("margin_pct") or {},
    )
    offenders = {}
    for page, margin in margins.items():
        if page not in revenue or page not in profit or not revenue[page]:
            continue
        implied = profit[page] / revenue[page] * 100.0
        if abs(implied - margin) >= 0.01:
            offenders[page] = (margin, implied)
    assert not offenders, "margin does not follow from the page's own revenue and profit: " + ", ".join(
        f"{page} prints {shown:.2f}% but its numbers imply {implied:.2f}%"
        for page, (shown, implied) in sorted(offenders.items())
    )


def test_products_counts_every_sku_not_just_the_head(
    app_client, seed_reconciliation_dataset, monkeypatch
):
    """
    The Products KPI block is built from a revenue-ranked slice of the catalogue.

    That is the right basis for a distribution and the wrong one for a count, and
    the two shared a code path: the header printed the slice size next to a table
    reporting the real one. The fixture's tail SKU is a hundredth the size of its
    head, so it is exactly what a truncating sum drops.
    """
    monkeypatch.setattr(
        "app.services.filters_service.scope_from_user", lambda _u: _scope_admin()
    )
    payload = app_client.get("/api/products/bundle", query_string=WINDOW).get_json() or {}
    kpis = payload.get("kpis") or {}
    table_total = (payload.get("table") or {}).get("total")
    if not table_total:
        pytest.skip("products bundle reported no table total to reconcile against")
    assert kpis.get("products") == table_total, (
        f"KPI SKU count {kpis.get('products')} differs from the {table_total} the "
        "table reports for the same filter"
    )
