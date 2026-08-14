"""The price/volume/mix identity.

A decomposition whose parts do not sum to the whole is worse than no
decomposition: it invites the reader to reason about components that cannot
explain the movement they are attached to. This was live - the page reported a
mix effect of exactly $0 on every window, because the split it used was already
complete in two terms and "mix" was the leftover of an identity that had none.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.services.overview_v2 import _driver_metric_block


def _frame(rows):
    """One row per SKU: (key, qty_prev, price_prev, qty_cur, price_cur)."""
    records = []
    for key, q0, p0, q1, p1 in rows:
        records.append(
            {
                "sku_key": key,
                "sku_label": key,
                "qty_prev": float(q0),
                "qty_cur": float(q1),
                "price_prev": float(p0),
                "price_cur": float(p1),
                "revenue_prev": float(q0) * float(p0),
                "revenue_cur": float(q1) * float(p1),
            }
        )
    return pd.DataFrame.from_records(records)


def _block(frame):
    return _driver_metric_block(
        frame,
        metric="revenue",
        period_label="Primary",
        tolerance=0.01,
        top_n=5,
        metric_available=True,
    )


BASKETS = {
    "price up, volume flat": [("A", 100, 10.0, 100, 12.0), ("B", 50, 4.0, 50, 4.5)],
    "volume up, price flat": [("A", 100, 10.0, 140, 10.0), ("B", 50, 4.0, 60, 4.0)],
    "mix shifts to dearer": [("A", 100, 10.0, 40, 10.0), ("B", 50, 2.0, 110, 2.0)],
    "all three move": [("A", 120, 9.5, 90, 11.25), ("B", 40, 3.0, 75, 2.6), ("C", 10, 50.0, 12, 47.5)],
    "a line disappears": [("A", 100, 10.0, 0, 0.0), ("B", 50, 4.0, 80, 4.2)],
    "a line appears": [("A", 100, 10.0, 100, 10.0), ("B", 0, 0.0, 30, 6.0)],
}


@pytest.mark.parametrize("name", sorted(BASKETS))
def test_price_volume_mix_sums_to_the_total_delta(name):
    block = _block(_frame(BASKETS[name]))
    price = block["price_effect"]
    volume = block["volume_effect"]
    mix = block["mix_effect"]
    total = block["delta"]

    assert None not in (price, volume, mix, total), f"{name}: a component is missing"
    # A few cents of tolerance, as the audit asked for - not a free pass.
    assert price + volume + mix == pytest.approx(total, abs=0.01), (
        f"{name}: price {price:,.2f} + volume {volume:,.2f} + mix {mix:,.2f} "
        f"= {price + volume + mix:,.2f}, but the total delta is {total:,.2f}"
    )


@pytest.mark.parametrize("name", sorted(BASKETS))
def test_reconciliation_block_reports_the_truth(name):
    block = _block(_frame(BASKETS[name]))
    rec = block["reconciliation"]
    assert rec["within_tolerance"] is True, f"{name}: {rec}"
    assert rec["residual"] == pytest.approx(0.0, abs=0.01)


def test_mix_is_not_structurally_zero():
    """The regression that shipped: a mix term that could only ever be zero.

    Volume moves between two lines at different price points with every unit
    price held constant. Nothing about price changed, and the total is nonzero,
    so any decomposition that cannot attribute this to mix is not measuring it.
    """
    block = _block(_frame(BASKETS["mix shifts to dearer"]))
    assert abs(block["mix_effect"]) > 1.0, (
        "mix is ~0 on a basket that shifted between price points; "
        "the term is a residual, not a measurement"
    )
    assert abs(block["price_effect"]) < 0.01, "no unit price moved, so price must be flat"


def test_price_effect_is_isolated_when_only_price_moves():
    block = _block(_frame(BASKETS["price up, volume flat"]))
    assert abs(block["volume_effect"]) < 0.01
    assert abs(block["mix_effect"]) < 0.01
    assert block["price_effect"] == pytest.approx(block["delta"], abs=0.01)
