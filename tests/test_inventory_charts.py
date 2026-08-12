"""
The three inventory chart series, and the arithmetic behind them.

The page already rendered ABC, movement and aging as ranked bar lists. Those
are right for a composition and wrong for the question each diagnostic exists
to answer: ABC is a concentration claim, cover is a distribution, and movement
is a relationship between two axes. `_chart_series` adds the series a bar list
cannot carry.

These assertions are about the numbers, not the pictures - a chart that draws
beautifully from a wrong series is worse than no chart.
"""
from __future__ import annotations

import pytest

from app.services import inventory_bundle as ib


def _sku(product_id: str, *, value: float, usage: float, days: float, category: str = "Ambient Grocery") -> dict:
    return {
        "product_id": product_id,
        "product_name": f"Product {product_id}",
        "category": category,
        "protein": "Unassigned",
        "supplier": "Vendor A",
        "inventory_value": value,
        "avg_weekly_usage": usage,
        "usage_units": usage * 10,
        "days_supply": days,
        "on_hand_qty": usage * days / 7.0,
        "movement_quadrant": "Core / protect",
        "abc_class": "A",
        "posture": "Healthy",
    }


def test_pareto_is_cumulative_and_ends_at_one_hundred():
    rows = [_sku(f"P{i}", value=100.0 * (10 - i), usage=5, days=30) for i in range(10)]
    series = ib._chart_series(rows, total_inventory=sum(r["inventory_value"] for r in rows))
    pareto = series["abc_pareto"]

    assert pareto["sku_count"] == 10
    assert len(pareto["cum_value_pct"]) == 10
    assert pareto["cum_value_pct"] == sorted(pareto["cum_value_pct"]), "cumulative curve must not fall"
    assert pareto["cum_value_pct"][-1] == pytest.approx(100.0, abs=0.01)
    assert pareto["sku_share_pct"][-1] == pytest.approx(100.0, abs=0.01)
    # Richest first: the first SKU alone must exceed an even share.
    assert pareto["cum_value_pct"][0] > 10.0


def test_pareto_detects_concentration():
    """One SKU holding nearly everything must bend the curve immediately."""
    rows = [_sku("BIG", value=9_000.0, usage=5, days=30)]
    rows += [_sku(f"S{i}", value=100.0, usage=5, days=30) for i in range(10)]
    series = ib._chart_series(rows, total_inventory=sum(r["inventory_value"] for r in rows))
    assert series["abc_pareto"]["cum_value_pct"][0] > 85.0


def test_cover_histogram_bins_every_sku_exactly_once():
    rows = [
        _sku("A", value=100, usage=5, days=3),      # 0-7
        _sku("B", value=100, usage=5, days=10),     # 7-14
        _sku("C", value=100, usage=5, days=44),     # 30-45
        _sku("D", value=100, usage=5, days=200),    # 120+ overflow
    ]
    series = ib._chart_series(rows, total_inventory=400.0)
    hist = series["cover_histogram"]

    assert sum(b["skus"] for b in hist["bins"]) == len(rows), "a SKU fell between bins or was counted twice"
    assert sum(b["value"] for b in hist["bins"]) == pytest.approx(400.0)
    assert hist["bins"][-1]["skus"] == 1, "the 200-day SKU belongs in the overflow bucket"
    assert hist["bins"][-1]["high"] is None, "the overflow bucket must be open-ended"
    # The guide bands must be the same numbers the posture rules use, or the
    # chart would draw a target the classification does not apply.
    assert hist["target_perishable_days"] == ib.TARGET_DAYS_PERISHABLE
    assert hist["target_ambient_days"] == ib.TARGET_DAYS_AMBIENT


def test_cover_targets_match_the_posture_classifier():
    perishable = {"category": "Fresh Meat", "protein": "Beef"}
    ambient = {"category": "Ambient Grocery", "protein": "Unassigned"}
    assert ib._target_days(perishable) == ib.TARGET_DAYS_PERISHABLE
    assert ib._target_days(ambient) == ib.TARGET_DAYS_AMBIENT


def test_movement_series_are_parallel_and_complete():
    rows = [_sku(f"P{i}", value=100.0 + i, usage=1.0 + i, days=20 + i) for i in range(6)]
    movement = ib._chart_series(rows, total_inventory=sum(r["inventory_value"] for r in rows))["movement"]

    lengths = {key: len(values) for key, values in movement.items()}
    assert len(set(lengths.values())) == 1, f"parallel arrays disagree in length: {lengths}"
    assert lengths["value"] == len(rows)
    assert movement["label"][0] == "Product P0"


def test_missing_values_stay_missing():
    """A SKU with no usage has no SVSI; it must not become zero."""
    row = _sku("X", value=100.0, usage=0.0, days=0.0)
    row["svsi"] = None
    row["days_supply"] = None
    movement = ib._chart_series([row], total_inventory=100.0)["movement"]
    assert movement["svsi"] == [None]
    assert movement["days_supply"] == [None]


def test_empty_scope_does_not_raise():
    series = ib._chart_series([], total_inventory=0.0)
    assert series["abc_pareto"]["sku_count"] == 0
    assert series["movement"]["value"] == []
    assert sum(b["skus"] for b in series["cover_histogram"]["bins"]) == 0
