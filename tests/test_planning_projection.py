"""
The planner reads a projection, and the projection must be complete.

`stakeholder_report_bundle` used to read `SELECT *` - 81 columns, ~25 MB of
pandas for one filtered year - and then use about a quarter of them. Naming the
columns cut the read and the build by ~39% and the frame by 55%.

The risk that buys is specific: a column missing from `PLANNING_COLUMNS` is not
an error. `_department_column` falls back through four candidates, `_availability`
skips absent inventory fields, and `au.revenue_column` picks whatever it finds -
so a dropped column silently changes a number instead of raising. This pins the
projected payload against an unprojected one so that failure mode is loud.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.services import fact_store, planning
from app.services.stakeholder_report_bundle import PLANNING_COLUMNS


def _row(idx: int) -> dict:
    dept = ["Fresh & Meat", "Consumables", "General Merchandise"][idx % 3]
    return {
        "Date": f"2025-1{1 + idx % 2}-0{1 + idx % 8}",
        "DateExpected": f"2025-1{1 + idx % 2}-0{1 + idx % 8}",
        "OrderId": f"O-{idx}",
        "CustomerId": f"C{idx % 7}",
        "CustomerName": f"Customer {idx % 7}",
        "ProductId": f"P{idx % 11}",
        "ProductName": f"Product {idx % 11}",
        "SKU": f"SKU-{idx % 11}",
        "ProteinType": dept,
        "ProductCategory": dept,
        "SupplierName": f"Vendor {idx % 5}",
        "SupplierId": f"S{idx % 5}",
        "ShippingMethodName": ["DC", "DSD", "Cross-dock"][idx % 3],
        "RegionName": ["Pacific", "Midwest"][idx % 2],
        "RegionId": ["RG06", "RG03"][idx % 2],
        "OrderStatus": "packed",
        "Revenue": 100.0 + idx,
        "Cost": 60.0 + idx,
        "CostPrice": 60.0 + idx,
        "Price": 100.0 + idx,
        "QuantityShipped": 2.0 + (idx % 4),
        "QuantityOrdered": 3.0 + (idx % 4),
        "WeightLb": 8.0 + idx,
        "QtyShippedLb": 8.0 + idx,
        "pack_weight_lb_sum": 8.0 + idx,
        "IsLate": idx % 5 == 0,
        "IsShortShip": idx % 7 == 0,
        "IsStockout": idx % 13 == 0,
        "BackorderQty": float(idx % 3),
        "DaysOfSupply": 5.0 + (idx % 40),
        "OnHandQty": 50.0 + idx,
        "OnHandValue": 500.0 + idx,
        "ReorderPointQty": 20.0 + (idx % 10),
        "SafetyStockQty": 10.0 + (idx % 6),
        # A column no planner code reads: it must be excluded by the projection
        # without changing anything, which is the other half of the contract.
        "UnusedNoiseColumn": f"noise-{idx}",
    }


@pytest.fixture
def seed_planning(tmp_path, monkeypatch):
    frame = pd.DataFrame([_row(i) for i in range(240)])
    path = tmp_path / "fact_planning_projection.parquet"
    frame.to_parquet(path)
    monkeypatch.setenv("PARQUET_PATH", str(path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield path
    fact_store.reset_duckdb_state()


def _scope_admin() -> dict:
    return {
        "is_admin": True,
        "scope_mode": "all",
        "allowed_erp_user_ids": [],
        "sales_rep_ids": [],
        "allowed_count": 0,
        "scope_hash": "planning-projection",
        "permissions_version": "1",
        "user_id": 1,
        "role": "admin",
    }


def _filters():
    from werkzeug.datastructures import MultiDict

    from app.services.filters import parse_filters

    return parse_filters(MultiDict({"start": "2025-01-01", "end": "2025-12-31"}))


def test_projection_does_not_change_the_planning_payload(seed_planning):
    filters, scope = _filters(), _scope_admin()
    everything = fact_store.query_fact(filters=filters, scope=scope, use_cache=False)
    projected = fact_store.query_fact(
        filters=filters, scope=scope, columns=PLANNING_COLUMNS, use_cache=False
    )

    assert not everything.empty, "fixture produced no rows; the test would pass vacuously"
    assert projected.shape[1] < everything.shape[1], "projection selected nothing out"

    assert planning.build_planning(projected) == planning.build_planning(everything)


def test_projection_covers_every_column_the_planner_reads(seed_planning):
    """
    The department fallback is the sharp edge: `_department_column` tries
    ProteinType, ProteinName, ProductCategory, Category in order, so dropping
    the first one silently regroups the whole scorecard under the next.
    """
    filters, scope = _filters(), _scope_admin()
    projected = fact_store.query_fact(
        filters=filters, scope=scope, columns=PLANNING_COLUMNS, use_cache=False
    )
    assert planning._department_column(projected) == "ProteinType"

    payload = planning.build_planning(projected)
    assert payload["demand"], "the department demand breakdown is empty under the projection"
    assert payload["service_by_lane"], "lane service is empty; ShippingMethodName was dropped"
    assert payload["service_by_vendor"], "vendor service is empty; SupplierName was dropped"

    headline = payload["inventory"]["headline"]
    assert payload["inventory"]["by_department"], "inventory department scorecard is empty"
    # `_availability` supplies these, `_inventory_position` the rest; both go
    # silently empty when their columns are absent rather than raising.
    assert headline.get("otif_pct") is not None, "availability lost its columns"
    assert headline.get("on_hand_value") is not None, "stock position lost OnHandValue"
    assert headline.get("cover_days") is not None, "stock position lost DaysOfSupply"
    assert headline.get("turns") is not None, "stock position lost its turns basis"


def test_department_scorecard_is_computed_once(seed_planning, monkeypatch):
    """`actions` reuses the department scorecard rather than recomputing it."""
    filters, scope = _filters(), _scope_admin()
    frame = fact_store.query_fact(
        filters=filters, scope=scope, columns=PLANNING_COLUMNS, use_cache=False
    )

    calls: list[str] = []
    original = planning._inventory_by

    def counting(frame_arg, column, **kwargs):
        calls.append(column)
        return original(frame_arg, column, **kwargs)

    monkeypatch.setattr(planning, "_inventory_by", counting)
    planning.build_inventory(frame)

    dept = planning._department_column(frame)
    assert calls.count(dept) == 1, f"department scorecard computed {calls.count(dept)}x: {calls}"
