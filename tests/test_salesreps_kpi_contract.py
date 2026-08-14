"""Per-order KPIs on the Sales Reps scorecard.

The bug these guard was not a wrong number - it was a key that did not exist.
`profit_per_order` was absent from the payload, so the page read `undefined`
and printed an em dash beside a Profit of $1.66m and an Orders of 4,184 on the
same card, while Overview divided those two numbers and showed $396.89. The two
per-order deltas were absent for the same reason: the rollup counted orders for
the current window only, so neither had a prior denominator.

These assert the contract rather than the figures. Asserting figures needs the
real fact dataset, and conftest.py deliberately points PARQUET_PATH at an empty
one; the fact store is a module singleton bound at import, so a fixture cannot
swap it back afterwards. A test that skips on every run is not a guard, and a
missing key is exactly the failure mode worth pinning.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "app" / "services" / "salesreps_bundle.py"

# Keys the scorecard reads off the `kpis` block. Each is a card on screen, and
# a missing one renders as an em dash rather than as an error.
REQUIRED_KPI_KEYS = (
    "revenue",
    "profit",
    "margin_pct",
    "orders",
    "customers",
    "avg_order_value",
    "profit_per_order",
    "ppo_mom_pct",
    "aov_mom_pct",
    "revenue_per_customer",
)


def _scorecard_kpi_keys() -> set[str]:
    """The literal keys of the scorecard's `kpis = {...}` assignment."""
    tree = ast.parse(BUNDLE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        if "kpis" not in [t.id for t in node.targets if isinstance(t, ast.Name)]:
            continue
        keys = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
        # Other `kpis = {}` assignments in this module are empty-state fallbacks.
        if "revenue" in keys and "orders" in keys:
            return keys
    raise AssertionError("could not find the scorecard `kpis` dict in salesreps_bundle.py")


@pytest.mark.parametrize("key", REQUIRED_KPI_KEYS)
def test_the_scorecard_payload_declares_every_kpi_the_page_reads(key: str):
    keys = _scorecard_kpi_keys()
    assert key in keys, (
        f"`{key}` is not in the Sales Reps kpis payload, so its card renders an em dash"
    )


def test_profit_per_order_divides_profit_by_the_orders_on_the_same_card():
    """Same definition as Overview's, so the two pages cannot disagree.

    Read from source rather than from a payload because the identity is the
    point: the per-rep version divides by `invoice_count`, a different
    denominator from the `orders` the scorecard displays.
    """
    source = BUNDLE.read_text(encoding="utf-8")
    assert '"profit_per_order": profit_per_order,' in source, (
        "the scorecard no longer assigns profit_per_order from the computed local"
    )
    assert (
        "profit_per_order = (profit / orders) if profit is not None and orders else None"
        in source
    ), "profit per order is no longer profit over the order count shown beside it"


def test_the_totals_query_counts_prior_orders():
    """Without it, both per-order deltas have nothing to compare against."""
    source = BUNDLE.read_text(encoding="utf-8")
    assert "is_prior_window = 1 THEN ab.order_id END) AS prior_orders" in source, (
        "the totals query no longer counts prior-window orders, so AOV and "
        "Profit / Order lose their comparison"
    )
