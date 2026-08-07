from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.services.filters import FilterParams
from app.services import overview_metrics as om
from app.services import overview_v2 as ov2


def test_delta_percent_prior_zero_is_na() -> None:
    assert om.delta_percent(120.0, 0.0) is None
    payload = om.delta_payload(120.0, 0.0)
    assert payload["delta"] == pytest.approx(120.0)
    assert payload["delta_pct"] is None
    assert payload["delta_pct_na_reason"] == "no prior-period value"


def test_window_contract_includes_prior_windows() -> None:
    params = FilterParams(start=date(2025, 4, 1), end=date(2025, 6, 30))
    window = om.resolve_window_contract(params)
    data = window.as_dict()
    assert data["start"] == "2025-04-01"
    assert data["end"] == "2025-06-30"
    assert data["prior_month_start"] == "2025-01-01"
    assert data["prior_month_end"] == "2025-03-31"
    assert data["prior_year_start"] == "2024-04-01"
    assert data["prior_year_end"] == "2024-06-30"
    assert data["method"] == "completed_months_vs_prior_completed_months"
    assert data["comparison_label"] == "Completed months vs prior completed months"


def test_window_contract_partial_month_aligns_to_prior_month_same_day() -> None:
    params = FilterParams(start=date(2025, 3, 1), end=date(2025, 3, 19))
    window = om.resolve_window_contract(params, include_current_month=True)
    data = window.as_dict()
    assert data["prior_month_start"] == "2025-02-01"
    assert data["prior_month_end"] == "2025-02-19"
    assert data["method"] == "month_to_date_vs_prior_month_same_day"
    assert data["terminal_period_incomplete"] is True
    assert data["is_partial_period"] is True
    assert "avoid misleading partial-month" in data["note"]


def test_window_contract_arbitrary_range_uses_matched_days() -> None:
    params = FilterParams(start=date(2025, 3, 10), end=date(2025, 3, 24))
    window = om.resolve_window_contract(params, include_current_month=True)
    data = window.as_dict()
    assert data["prior_month_start"] == "2025-02-23"
    assert data["prior_month_end"] == "2025-03-09"
    assert data["method"] == "selected_window_vs_prior_matched_days"
    assert data["comparison_label"] == "Selected window vs prior matched days"


def test_window_contract_current_fy_uses_prior_fytd() -> None:
    params = FilterParams(
        start=date(2025, 10, 1),
        end=date(2026, 4, 8),
        preset="current_fy",
        date_type="fiscal",
    )
    window = om.resolve_window_contract(params, include_current_month=True)
    data = window.as_dict()
    assert data["prior_month_start"] == "2024-10-01"
    assert data["prior_month_end"] == "2025-04-08"
    assert data["prior_year_start"] == "2024-10-01"
    assert data["prior_year_end"] == "2025-09-30"
    assert data["method"] == "fiscal_year_to_date_vs_prior_fiscal_year_to_date"
    assert data["date_type"] == "fiscal"
    assert data["trend_bucket_label"] == "Fiscal Month"
    assert data["comparison_label"] == "Current FYTD vs prior FYTD"


def test_window_contract_current_fq_uses_prior_fiscal_quarter_days() -> None:
    params = FilterParams(
        start=date(2026, 4, 1),
        end=date(2026, 4, 8),
        preset="current_fq",
        date_type="fiscal",
    )
    window = om.resolve_window_contract(params, include_current_month=True)
    data = window.as_dict()
    assert data["prior_month_start"] == "2026-01-01"
    assert data["prior_month_end"] == "2026-01-08"
    assert data["method"] == "fiscal_quarter_to_date_vs_prior_fiscal_quarter_same_day"
    assert data["comparison_label"] == "Current FQTD vs prior FQTD"


def test_window_contract_previous_fq_uses_prior_full_fiscal_quarter() -> None:
    params = FilterParams(
        start=date(2026, 1, 1),
        end=date(2026, 3, 31),
        preset="previous_fq",
        date_type="fiscal",
    )
    window = om.resolve_window_contract(params, include_current_month=True)
    data = window.as_dict()
    assert data["start"] == "2026-01-01"
    assert data["end"] == "2026-03-31"
    assert data["prior_month_start"] == "2025-10-01"
    assert data["prior_month_end"] == "2025-12-31"
    assert data["method"] == "fiscal_quarter_vs_prior_fiscal_quarter"
    assert data["comparison_label"] == "Previous FQ vs prior FQ"
    assert data["is_partial_period"] is False


def test_window_contract_current_fm_uses_prior_fiscal_month_days() -> None:
    params = FilterParams(
        start=date(2026, 4, 1),
        end=date(2026, 4, 8),
        preset="current_fm",
        date_type="fiscal",
    )
    window = om.resolve_window_contract(params, include_current_month=True)
    data = window.as_dict()
    assert data["prior_month_start"] == "2026-03-01"
    assert data["prior_month_end"] == "2026-03-08"
    assert data["method"] == "fiscal_month_to_date_vs_prior_fiscal_month_same_day"
    assert data["comparison_label"] == "Current MoM (FMTD) vs prior MoM (FMTD)"
    assert data["trend_bucket_label"] == "Fiscal Month"


def test_decomposition_effects_sum_to_total() -> None:
    out = om.decompose_price_volume_mix(
        current_total=1200.0,
        prior_total=1000.0,
        current_qty=60.0,
        prior_qty=50.0,
    )
    assert out["total"] == pytest.approx(200.0, abs=1e-6)
    assert (out["price_effect"] or 0.0) + (out["volume_effect"] or 0.0) + (out["mix_effect"] or 0.0) == pytest.approx(
        out["total"] or 0.0,
        abs=1e-6,
    )


def test_hhi_computation() -> None:
    # Shares: 50%, 30%, 20% => HHI = (0.5^2 + 0.3^2 + 0.2^2) * 10000 = 3800
    hhi = om.compute_hhi([50.0, 30.0, 20.0])
    assert hhi == pytest.approx(3800.0, abs=1e-6)
    assert om.hhi_risk_label(hhi) == "high"


def test_driver_metric_block_reconciles() -> None:
    pair = pd.DataFrame(
        {
            "sku_key": ["SKU1", "SKU2"],
            "sku_label": ["SKU 1", "SKU 2"],
            "revenue_cur": [220.0, 80.0],
            "revenue_prev": [180.0, 140.0],
            "qty_cur": [20.0, 8.0],
            "qty_prev": [15.0, 14.0],
            "revenue_with_cost_cur": [220.0, 80.0],
            "revenue_with_cost_prev": [180.0, 140.0],
            "cost_cur": [140.0, 60.0],
            "cost_prev": [120.0, 95.0],
            "qty_with_cost_cur": [20.0, 8.0],
            "qty_with_cost_prev": [15.0, 14.0],
            "price_cur": [11.0, 10.0],
            "price_prev": [12.0, 10.0],
            "profit_cur": [80.0, 20.0],
            "profit_prev": [60.0, 45.0],
            "margin_cur": [4.0, 2.5],
            "margin_prev": [4.0, 3.214285714],
        }
    )

    out = ov2._driver_metric_block(
        pair,
        metric="revenue",
        period_label="MOM",
        tolerance=0.01,
        top_n=5,
        metric_available=True,
    )

    assert (out["price_effect"] or 0.0) + (out["volume_effect"] or 0.0) + (out["mix_effect"] or 0.0) == pytest.approx(
        out["delta"] or 0.0,
        abs=1e-6,
    )
    assert out.get("reconciliation", {}).get("within_tolerance") is True


def test_driver_insight_mentions_direction_and_component() -> None:
    pair = pd.DataFrame(
        {
            "sku_key": ["SKU1", "SKU2"],
            "sku_label": ["SKU 1", "SKU 2"],
            "revenue_cur": [120.0, 50.0],
            "revenue_prev": [180.0, 140.0],
            "qty_cur": [10.0, 5.0],
            "qty_prev": [15.0, 14.0],
            "revenue_with_cost_cur": [120.0, 50.0],
            "revenue_with_cost_prev": [180.0, 140.0],
            "cost_cur": [75.0, 40.0],
            "cost_prev": [120.0, 90.0],
            "qty_with_cost_cur": [10.0, 5.0],
            "qty_with_cost_prev": [15.0, 14.0],
            "price_cur": [12.0, 10.0],
            "price_prev": [12.0, 10.0],
            "profit_cur": [45.0, 10.0],
            "profit_prev": [60.0, 50.0],
            "margin_cur": [4.5, 2.0],
            "margin_prev": [4.0, 3.571428571],
        }
    )
    out = ov2._driver_metric_block(
        pair,
        metric="revenue",
        period_label="MOM",
        tolerance=0.01,
        top_n=5,
        metric_available=True,
    )

    insight = (out.get("insight") or "").lower()
    assert "mom revenue down" in insight
    assert ("volume" in insight) or ("price" in insight) or ("mix" in insight)


def test_driver_mix_not_forced_zero_with_bad_qty_case() -> None:
    pair = pd.DataFrame(
        {
            "sku_key": ["SKU1", "SKU2"],
            "sku_label": ["SKU 1", "SKU 2"],
            "revenue_cur": [100.0, 0.0],
            "revenue_prev": [90.0, 50.0],
            "qty_cur": [10.0, 0.0],
            "qty_prev": [9.0, 0.0],
            "revenue_with_cost_cur": [100.0, 0.0],
            "revenue_with_cost_prev": [90.0, 50.0],
            "cost_cur": [60.0, 0.0],
            "cost_prev": [55.0, 20.0],
            "qty_with_cost_cur": [10.0, 0.0],
            "qty_with_cost_prev": [9.0, 0.0],
            "price_cur": [10.0, 0.0],
            "price_prev": [10.0, 0.0],
            "profit_cur": [40.0, 0.0],
            "profit_prev": [35.0, 30.0],
            "margin_cur": [4.0, 0.0],
            "margin_prev": [3.888888889, 0.0],
        }
    )
    out = ov2._driver_metric_block(
        pair,
        metric="revenue",
        period_label="MOM",
        tolerance=0.01,
        top_n=5,
        metric_available=True,
    )
    assert out["mix_effect"] == pytest.approx(-50.0, abs=1e-6)


def test_driver_decomp_flag_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("DRIVER_DECOMP_V2", raising=False)
    assert ov2._driver_decomp_v2_enabled() is False


def test_bundle_cache_key_changes_with_window_and_flag(monkeypatch) -> None:
    f_jan = FilterParams(start=date(2025, 1, 1), end=date(2025, 1, 31))
    f_feb = FilterParams(start=date(2025, 2, 1), end=date(2025, 2, 28))

    monkeypatch.setenv("DRIVER_DECOMP_V2", "0")
    jan_flag_off = ov2._bundle_cache_key(f_jan, include_current_month=False, defaulted_window=False)

    monkeypatch.setenv("DRIVER_DECOMP_V2", "1")
    jan_flag_on = ov2._bundle_cache_key(f_jan, include_current_month=False, defaulted_window=False)
    feb_flag_on = ov2._bundle_cache_key(f_feb, include_current_month=False, defaulted_window=False)

    assert jan_flag_off != jan_flag_on
    assert jan_flag_on != feb_flag_on
