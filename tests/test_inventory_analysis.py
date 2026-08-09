from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.page_guides import guide_for_request
from app.services import inventory_bundle


def test_inventory_page_is_registered_and_lightweight(client, app):
    app.config.update(LOGIN_DISABLED=True, AUTHZ_DISABLED=True)
    response = client.get("/inventory/")

    assert response.status_code == 200
    assert b'id="InventoryApp"' in response.data
    assert b"inventory.js" in response.data
    assert b"plotly" not in response.data.lower()


def test_inventory_formulas_match_source_app_definitions():
    rows = [
        {
            "product_id": "A",
            "product_name": "Core",
            "category": "Shelf stable",
            "protein": "Other",
            "supplier": "Supplier 1",
            "inventory_value": 80,
            "usage_units": 20,
            "observed_days": 14,
            "on_hand_qty": 20,
            "days_supply": 14,
            "safety_stock_qty": 1,
            "reorder_point_qty": 2,
            "last_movement_date": "2026-07-31",
        },
        {
            "product_id": "B",
            "product_name": "Middle",
            "inventory_value": 15,
            "usage_units": 5,
            "observed_days": 14,
            "on_hand_qty": 5,
            "days_supply": 14,
            "last_movement_date": "2026-07-15",
        },
        {
            "product_id": "C",
            "product_name": "Tail",
            "inventory_value": 5,
            "usage_units": 1,
            "observed_days": 14,
            "on_hand_qty": 1,
            "days_supply": 14,
            "last_movement_date": "2025-01-01",
        },
    ]

    inventory_bundle._classify_rows(rows, "2026-08-01")

    assert [row["abc_class"] for row in rows] == ["A", "B", "C"]
    assert rows[0]["avg_weekly_usage"] == pytest.approx(10)
    assert rows[0]["weeks_on_hand"] == pytest.approx(2)
    assert rows[0]["annual_turns"] == pytest.approx(26)
    assert rows[0]["holding_cost_annual"] == pytest.approx(8.8)
    assert rows[2]["aging_bucket"] == "365+"


def test_inventory_holding_cost_rate_is_explicit_and_auditable():
    assert inventory_bundle.ANNUAL_HOLDING_RATE == pytest.approx(0.11)
    assert inventory_bundle.ANNUAL_HOLDING_RATE == pytest.approx(
        inventory_bundle.ANNUAL_CAPITAL_RATE
        + inventory_bundle.ANNUAL_SERVICE_RATE
        + inventory_bundle.ANNUAL_STORAGE_RATE
        + inventory_bundle.ANNUAL_RISK_RATE
    )


def test_inventory_has_page_specific_visitor_guide():
    guide = guide_for_request(
        SimpleNamespace(endpoint="inventory.index", blueprint="inventory", path="/inventory/")
    )

    assert guide is not None
    assert guide["key"] == "inventory"
    assert len(guide["steps"]) == 3
