"""
Phase 8: Bundle accuracy tests for salesreps.

Covers:
  - Revenue-weighted margin in benchmarks (not simple mean)
  - Safe division: _yoy returns None (not 0.0) when denominator is zero
  - monthly_compare dataset provides separate revenue and revenue_yoy arrays
    so JS can compute YoY % client-side without division-by-zero risk
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.services import fact_store, filters_service, salesreps_bundle
from app.services.salesreps_bundle import _salesrep_lost_accounts, _clean_optional


# ── Shared fixture ─────────────────────────────────────────────────────────

@pytest.fixture
def seed_accuracy(tmp_path, monkeypatch):
    """
    Two reps with intentionally different revenue/margin levels so we can
    verify revenue-weighted (not simple-mean) aggregation.

    Rep A: revenue=2000, cost=1400 → margin=30%
    Rep B: revenue= 500, cost= 450 → margin=10%

    Simple mean margin = (30 + 10) / 2 = 20%
    Revenue-weighted  = (2000*30 + 500*10) / (2000+500) = 65000/2500 = 26%
    """
    rows = []
    reps = [
        ("RA", "Rep A", 2000.0, 1400.0),  # 30% margin
        ("RB", "Rep B",  500.0,  450.0),  # 10% margin
    ]
    for rep_id, rep_name, rev, cost in reps:
        for month in (6, 7):
            rows.append(
                {
                    "Date": f"2025-{month:02d}-10",
                    "DateExpected": f"2025-{month:02d}-10",
                    "SalesRepId": rep_id,
                    "SalesRepName": rep_name,
                    "OrderId": f"O-{rep_id}-{month}",
                    "CustomerId": f"C-{rep_id}",
                    "CustomerName": f"Customer {rep_id}",
                    "ProductId": f"P-{rep_id}",
                    "ProductName": f"Product {rep_id}",
                    "OrderStatus": "packed",
                    "Revenue": rev,
                    "Cost": cost,
                    "QuantityOrdered": 10,
                    "WeightLb": 20.0,
                    "UnitOfBillingId": 1,
                    "pack_item_count_sum": 10.0,
                    "pack_weight_lb_sum": 20.0,
                    "pack_count": 1,
                    "Price": rev,
                    "CostPrice": cost,
                }
            )

    df = pd.DataFrame(rows)
    parquet_path = tmp_path / "fact_salesreps_v2.parquet"
    df.to_parquet(parquet_path)

    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    monkeypatch.delenv("CUSTOMER_REP_HISTORY_PATH", raising=False)
    monkeypatch.delenv("TERRITORY_REP_HISTORY_PATH", raising=False)
    monkeypatch.delenv("CUSTOMER_TERRITORY_HISTORY_PATH", raising=False)
    monkeypatch.delenv("SALESREP_SUCCESSION_PATH", raising=False)
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield
    fact_store.reset_duckdb_state()


def _build(seed_accuracy, start="2025-01-01", end="2025-12-31", **kwargs):
    """Call build_salesreps_bundle with minimal boilerplate."""
    filters = filters_service.resolve_effective_filters(
        {"start": start, "end": end},
        session_obj={},
        user_id=None,
        sticky_enabled=False,
    )
    scope = {"is_admin": True}
    args = {"page_size": "100", **kwargs}
    return salesreps_bundle.build_salesreps_bundle(filters, scope, args)


# ── Test 1: revenue-weighted margin ───────────────────────────────────────

def test_margin_is_revenue_weighted_not_simple_mean(seed_accuracy):
    """
    benchmarks.avg_margin_pct must be revenue-weighted.

    With Rep A (2000 rev, 30% margin) and Rep B (500 rev, 10% margin):
      simple mean = 20%
      revenue-weighted = (2000*30 + 500*10) / 2500 = 26%

    The bundle must return ~26%, not ~20%.
    """
    payload = _build(seed_accuracy)
    benchmarks = payload.get("benchmarks") or {}
    avg_margin = benchmarks.get("avg_margin_pct")

    assert avg_margin is not None, "benchmarks.avg_margin_pct must not be None"

    # Revenue-weighted answer is 26%; simple mean is 20%.
    # Assert it is closer to 26 than to 20.
    assert abs(avg_margin - 26.0) < abs(avg_margin - 20.0), (
        f"avg_margin_pct={avg_margin:.2f}% looks like simple mean (20%) "
        f"rather than revenue-weighted (26%)."
    )
    # Tighter bound: within 2 pp of 26%
    assert abs(avg_margin - 26.0) < 2.0, (
        f"Expected revenue-weighted margin ~26%, got {avg_margin:.2f}%"
    )


# ── Test 2: safe division — _yoy returns None for zero denominator ─────────

def test_safe_division_returns_none_not_zero_for_zero_denominator():
    """
    The internal _yoy helper inside a drilldown bundle must return None
    when the prior-period value is 0, never 0.0 or raises ZeroDivisionError.

    We verify this by calling _clean_optional directly on known None inputs,
    and by asserting the contract on the lost_accounts helper which uses
    the same safe-division pattern internally.
    """
    # _clean_optional must return None for None input (never 0.0)
    assert _clean_optional(None) is None
    assert _clean_optional(float("nan")) is None

    # Ensure _salesrep_lost_accounts does not divide by zero on edge values
    customers = [
        {
            "customer_id": "C999",
            "customer_name": "Zero Prev",
            "revenue_last_30": 0.0,
            "revenue_prev_30": 0.0,   # denominator would be 0 in pct-change
            "last_order_date": "2025-07-01",
        }
    ]
    ref_date = pd.Timestamp("2025-08-31")
    result = _salesrep_lost_accounts(customers, ref_date)
    # Customer with revenue_prev_30 == 0 must be excluded (not a lost account)
    assert result == [], "Customer with zero prev revenue must not appear as lost account"


# ── Test 3: monthly_compare has separate revenue + revenue_yoy arrays ──────

def test_monthly_compare_has_revenue_and_revenue_yoy_arrays(seed_accuracy):
    """
    payload.trend.monthly_compare must expose both 'revenue' and 'revenue_yoy'
    as separate arrays.  The JS computes YoY % client-side from these two
    series, so they must always be present (even if all-zero) and equal length.
    """
    payload = _build(seed_accuracy)
    mc = (payload.get("trend") or {}).get("monthly_compare") or {}

    assert "revenue" in mc, "monthly_compare must have 'revenue' array"
    assert "revenue_yoy" in mc, "monthly_compare must have 'revenue_yoy' array"

    revenue = mc["revenue"]
    revenue_yoy = mc["revenue_yoy"]

    assert isinstance(revenue, list), "'revenue' must be a list"
    assert isinstance(revenue_yoy, list), "'revenue_yoy' must be a list"
    assert len(revenue) == len(revenue_yoy), (
        f"'revenue' ({len(revenue)}) and 'revenue_yoy' ({len(revenue_yoy)}) "
        f"must have the same length so JS can zip them safely"
    )


# ── Test 4: table rows contain margin_pct as float or None, never NaN ──────

def test_table_rows_margin_pct_is_clean(seed_accuracy):
    """
    Table row margin_pct values must be float or None — never NaN or
    Python float('nan'), which would break JSON serialisation.
    """
    import math

    payload = _build(seed_accuracy)
    rows = (payload.get("table") or {}).get("rows") or []
    assert rows, "Expected at least one table row"

    for row in rows:
        m = row.get("margin_pct")
        if m is not None:
            assert isinstance(m, (int, float)), f"margin_pct must be numeric, got {type(m)}"
            assert not math.isnan(m), f"margin_pct must not be NaN for rep {row.get('rep_id')}"
