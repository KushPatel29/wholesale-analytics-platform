import types

import pandas as pd
import pytest

from app.services.filters import FilterParams
from app.services import overview_forecast
from app.services import overview_v2 as ov2


def _fake_ctx(df):
    return ov2.FrameContext(
        df=df,
        colmap={
            "date": "Date",
            "revenue": "Revenue",
            "cost": "Cost",
            "qty": "QuantityShipped",
            "order_id": "OrderId",
            "customer_id": "CustomerId",
            "weight": None,
        },
        flags={},
        missing=[],
        window={},
        last_refresh=None,
        version="test",
        cache_hit=False,
    )


def test_monthly_series_computes_profit_and_asp(monkeypatch, app):
    df = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2024-01-05", "2024-01-20", "2024-02-02"]),
            "Revenue": [100, 200, 300],
            "Cost": [40, 60, 90],
            "QuantityShipped": [10, 20, 30],
            "OrderId": [1, 2, 3],
            "CustomerId": ["c1", "c2", "c1"],
        }
    )
    ctx = _fake_ctx(df)
    monkeypatch.setattr(ov2, "get_filtered_frame", lambda user, filters: ctx)

    with app.test_request_context():
        monthly, _ = overview_forecast.monthly_series(FilterParams())

    jan = monthly.loc[pd.Period("2024-01", freq="M")]
    feb = monthly.loc[pd.Period("2024-02", freq="M")]
    assert pytest.approx(jan["revenue"]) == 300
    assert pytest.approx(jan["profit"]) == 200
    assert pytest.approx(jan["asp"]) == 10
    assert -100 <= jan["margin_pct"] <= 100
    assert pytest.approx(feb["profit"]) == 210


def test_forecast_clips_negative_values(monkeypatch, app):
    monthly = pd.DataFrame(
        {
            "revenue": [-100, -80, -60, -50, -40, -30, -25, -20, -15, -10, -5, -1],
            "cost": [0.0] * 12,
            "qty": [10.0] * 12,
            "profit": [-100, -80, -60, -50, -40, -30, -25, -20, -15, -10, -5, -1],
            "margin_pct": [-100.0] * 12,
        },
        index=pd.period_range("2023-01", periods=12, freq="M"),
    )
    ctx = ov2.FrameContext(
        df=pd.DataFrame({"Date": pd.date_range("2023-01-01", periods=12, freq="MS")}),
        colmap={},
        flags={},
        missing=[],
        window={},
        last_refresh=None,
        version="test",
        cache_hit=False,
    )
    monkeypatch.setattr(overview_forecast, "monthly_series", lambda filters, include_partial_current=False: (monthly, ctx))

    with app.test_request_context():
        result = overview_forecast.forecast_metric(FilterParams(), metric="revenue", horizon_months=3)

    yhat_values = [pt.get("yhat") for pt in result.get("series", []) if pt.get("yhat") is not None]
    assert yhat_values, "forecast should include predictions"
    assert all(v >= 0 for v in yhat_values)


def test_forecast_cache_hit(monkeypatch, app):
    fake_cache = types.SimpleNamespace(store={})

    def _get(key):
        return fake_cache.store.get(key)

    def _set(key, value, timeout=None):
        fake_cache.store[key] = value

    fake_cache.get = _get
    fake_cache.set = _set
    monkeypatch.setattr(overview_forecast, "cache", fake_cache)

    calls = {"count": 0}

    monthly = pd.DataFrame(
        {
            "revenue": [100.0] * 12,
            "cost": [40.0] * 12,
            "qty": [10.0] * 12,
            "profit": [60.0] * 12,
            "margin_pct": [60.0] * 12,
        },
        index=pd.period_range("2023-01", periods=12, freq="M"),
    )
    ctx = ov2.FrameContext(
        df=pd.DataFrame({"Date": pd.date_range("2023-01-01", periods=12, freq="MS")}),
        colmap={},
        flags={},
        missing=[],
        window={},
        last_refresh=None,
        version="test",
        cache_hit=False,
    )

    def _monthly(filters, include_partial_current=False):
        calls["count"] += 1
        return monthly, ctx

    monkeypatch.setattr(overview_forecast, "monthly_series", _monthly)

    with app.test_request_context():
        first = overview_forecast.forecast_metric(FilterParams(), metric="revenue", horizon_months=3)
        second = overview_forecast.forecast_metric(FilterParams(), metric="revenue", horizon_months=3)

    assert calls["count"] == 1
    assert second.get("cache_hit") is True


def test_forecast_returns_metadata(monkeypatch, app):
    monthly = pd.DataFrame(
        {
            "revenue": [100.0] * 18,
            "cost": [40.0] * 18,
            "qty": [10.0] * 18,
            "profit": [60.0] * 18,
            "margin_pct": [60.0] * 18,
        },
        index=pd.period_range("2023-01", periods=18, freq="M"),
    )
    ctx = ov2.FrameContext(
        df=pd.DataFrame({"Date": pd.date_range("2023-01-01", periods=18, freq="MS")}),
        colmap={},
        flags={},
        missing=[],
        window={},
        last_refresh=None,
        version="test",
        cache_hit=False,
    )
    monkeypatch.setattr(overview_forecast, "monthly_series", lambda filters, include_partial_current=False: (monthly, ctx))

    with app.test_request_context():
        result = overview_forecast.forecast_metric(FilterParams(), metric="revenue", horizon_months=3)

    assert "backtest" in result
    assert "model_version" in result
    assert "last_train_date" in result
    assert "model_info" in result


def test_forecast_history_serialization(monkeypatch, app):
    monthly = pd.DataFrame(
        {
            "revenue": [100.0] * 12,
            "cost": [40.0] * 12,
            "qty": [10.0] * 12,
            "profit": [60.0] * 12,
            "margin_pct": [60.0] * 12,
        },
        index=pd.period_range("2023-01", periods=12, freq="M"),
    )
    ctx = ov2.FrameContext(
        df=pd.DataFrame({"Date": pd.date_range("2023-01-01", periods=12, freq="MS")}),
        colmap={},
        flags={},
        missing=[],
        window={},
        last_refresh=None,
        version="test",
        cache_hit=False,
    )
    monkeypatch.setattr(overview_forecast, "monthly_series", lambda filters, include_partial_current=False: (monthly, ctx))

    with app.test_request_context():
        result = overview_forecast.forecast_metric(FilterParams(), metric="revenue", horizon_months=3)

    history = result.get("history") or []
    assert history, "history series should be present"
    assert all(pt.get("ds") for pt in history)
    assert all(isinstance(pt.get("y"), (int, float)) or pt.get("y") is None for pt in history)


def test_margin_series_handles_zero_revenue(monkeypatch, app):
    monthly = pd.DataFrame(
        {
            "revenue": [0.0, 100.0, 0.0, 200.0, 150.0, 0.0, 120.0, 130.0, 0.0, 180.0, 160.0, 0.0],
            "cost": [0.0, 60.0, 0.0, 120.0, 90.0, 0.0, 70.0, 80.0, 0.0, 110.0, 100.0, 0.0],
            "qty": [0.0] * 12,
            "profit": [0.0, 40.0, 0.0, 80.0, 60.0, 0.0, 50.0, 50.0, 0.0, 70.0, 60.0, 0.0],
            "margin_pct": [0.0, 40.0, float("nan"), 40.0, 40.0, float("nan"), 41.7, 38.5, float("nan"), 38.9, 37.5, float("nan")],
        },
        index=pd.period_range("2023-01", periods=12, freq="M"),
    )
    ctx = ov2.FrameContext(
        df=pd.DataFrame({"Date": pd.date_range("2023-01-01", periods=12, freq="MS")}),
        colmap={},
        flags={},
        missing=[],
        window={},
        last_refresh=None,
        version="test",
        cache_hit=False,
    )
    monkeypatch.setattr(overview_forecast, "monthly_series", lambda filters, include_partial_current=False: (monthly, ctx))

    with app.test_request_context():
        result = overview_forecast.forecast_metric(FilterParams(), metric="margin", horizon_months=3)

    history = result.get("history") or []
    assert history, "margin history should be present even with zero revenue months"
    assert all(pt.get("ds") for pt in history)
    for pt in history:
        val = pt.get("y")
        if val is None:
            continue
        assert not pd.isna(val)


def test_forecast_v2_short_history_uses_limited_confidence(monkeypatch, app):
    series = pd.Series(
        [100.0] * 10,
        index=pd.date_range("2024-01-01", periods=10, freq="MS"),
        dtype="float64",
    )
    monkeypatch.setattr(overview_forecast, "_build_monthly_history", lambda *a, **k: (series, series, {}))
    monkeypatch.setattr(overview_forecast, "cache", types.SimpleNamespace(get=lambda _k: None, set=lambda *_a, **_k: None))

    with app.test_request_context():
        payload = overview_forecast.forecast_metric_v2(
            FilterParams(),
            metric="revenue",
            horizon=6,
            granularity="monthly",
        )

    assert payload["eligible"] is True
    assert payload["model"]["train_points"] == 10
    assert payload["model"]["confidence"] in {"low", "medium"}
    assert payload["confidence_badge"] in {"Limited", "Watch", "Medium"}
    assert payload["diagnostics"]["history_points"] == 10
    assert payload["notes"]


def test_forecast_v2_prefers_seasonal_or_decomposition_model_when_pattern_is_strong(monkeypatch, app):
    pattern = [120.0, 135.0, 150.0, 165.0, 180.0, 195.0, 210.0, 200.0, 185.0, 170.0, 155.0, 140.0]
    vals = pattern * 3
    series = pd.Series(vals, index=pd.date_range("2022-01-01", periods=len(vals), freq="MS"), dtype="float64")
    monkeypatch.setattr(overview_forecast, "_build_monthly_history", lambda *a, **k: (series, series, {}))
    monkeypatch.setattr(overview_forecast, "cache", types.SimpleNamespace(get=lambda _k: None, set=lambda *_a, **_k: None))

    with app.test_request_context():
        payload = overview_forecast.forecast_metric_v2(
            FilterParams(),
            metric="revenue",
            horizon=6,
            granularity="monthly",
        )

    assert payload["eligible"] is True
    assert payload["model"]["name"] in {"seasonal_naive", "stl_trend_recent36", "stl_trend_full", "holt_winters_mul", "holt_winters_recent36"}
    assert payload["model"]["smape"] is not None
    assert payload["diagnostics"]["seasonality_strength_score"] >= 40


def test_forecast_v2_clips_negative_forecasts(monkeypatch, app):
    series = pd.Series(
        [180.0, 175.0, 190.0, 200.0, 210.0, 195.0, 205.0, 215.0, 220.0, 225.0, 230.0, 235.0] * 2,
        index=pd.date_range("2023-01-01", periods=24, freq="MS"),
        dtype="float64",
    )
    monkeypatch.setattr(overview_forecast, "_build_monthly_history", lambda *a, **k: (series, series, {}))
    monkeypatch.setattr(
        overview_forecast,
        "_forecast_level_baseline_generic",
        lambda history, horizon, granularity, window=6: pd.Series(
            [-50.0] * horizon,
            index=pd.date_range(history.index.max() + pd.offsets.MonthBegin(1), periods=horizon, freq="MS"),
            dtype="float64",
        ),
    )
    monkeypatch.setattr(
        overview_forecast,
        "_forecast_theta_generic",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("theta unavailable in test")),
    )
    monkeypatch.setattr(
        overview_forecast,
        "_forecast_stl_trend_generic",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stl unavailable in test")),
    )
    monkeypatch.setattr(overview_forecast, "cache", types.SimpleNamespace(get=lambda _k: None, set=lambda *_a, **_k: None))

    with app.test_request_context():
        payload = overview_forecast.forecast_metric_v2(
            FilterParams(),
            metric="revenue",
            horizon=6,
            granularity="monthly",
        )

    forecasts = [row.get("forecast") for row in payload.get("series", []) if row.get("forecast") is not None]
    assert forecasts
    assert all(float(v) >= 0 for v in forecasts)


def test_build_monthly_history_excludes_partial_terminal_month_by_default(monkeypatch, app):
    monthly = pd.DataFrame(
        {
            "revenue": [100.0, 120.0, 60.0],
            "cost": [50.0, 60.0, 30.0],
            "qty": [10.0, 12.0, 6.0],
        },
        index=pd.period_range("2026-01", periods=3, freq="M"),
    )
    monkeypatch.setattr(overview_forecast, "monthly_series", lambda filters, include_partial_current=True: (monthly, {}))

    with app.test_request_context():
        display_series, fit_series, meta = overview_forecast._build_monthly_history(
            FilterParams(start=pd.Timestamp("2026-01-01"), end=pd.Timestamp("2026-03-19")),
            metric="revenue",
            include_current=False,
        )

    assert list(display_series.index.strftime("%Y-%m")) == ["2026-01", "2026-02"]
    assert list(fit_series.index.strftime("%Y-%m")) == ["2026-01", "2026-02"]
    assert meta["partial_period"]["detected"] is True
    assert meta["partial_period"]["excluded"] is True


def test_monthly_series_requests_current_month_when_partial_requested(monkeypatch, app):
    captured = {}
    monthly = pd.DataFrame(
        {"revenue": [100.0], "cost": [50.0], "qty": [10.0]},
        index=pd.period_range("2026-03", periods=1, freq="M"),
    )

    def _bundle_ctx(filters, include_current_month=False, defaulted_window=False):
        captured["include_current_month"] = include_current_month
        captured["defaulted_window"] = defaulted_window
        return {"monthly": monthly, "cache_hit": False}

    monkeypatch.setattr(overview_forecast.ov2, "get_bundle_context", _bundle_ctx)

    with app.test_request_context():
        out, _ctx = overview_forecast.monthly_series(
            FilterParams(start=pd.Timestamp("2026-03-01"), end=pd.Timestamp("2026-03-28")),
            include_partial_current=True,
        )

    assert captured["include_current_month"] is True
    assert captured["defaulted_window"] is False
    assert not out.empty
    assert str(out.index.max()) == "2026-03"


def test_build_monthly_history_can_include_partial_terminal_month(monkeypatch, app):
    monthly = pd.DataFrame(
        {
            "revenue": [100.0, 120.0, 60.0],
            "cost": [50.0, 60.0, 30.0],
            "qty": [10.0, 12.0, 6.0],
        },
        index=pd.period_range("2026-01", periods=3, freq="M"),
    )
    monkeypatch.setattr(overview_forecast, "monthly_series", lambda filters, include_partial_current=True: (monthly, {}))
    monkeypatch.setattr(
        overview_forecast,
        "_current_month_nowcast",
        lambda *a, **k: {
            "applied": True,
            "period": "2026-03",
            "effective_end": "2026-03-19",
            "estimated_month_end_revenue": 145.0,
            "estimated_month_end_cost": 75.0,
            "estimated_month_end_profit": 70.0,
            "estimated_month_end_margin_pct": 48.3,
            "raw_mtd_revenue": 60.0,
            "raw_mtd_cost": 30.0,
            "raw_mtd_profit": 30.0,
            "raw_mtd_margin_pct": 50.0,
            "revenue_interval": {"lower": 132.0, "upper": 158.0},
            "cost_interval": {"lower": 71.0, "upper": 79.0},
            "profit_interval": {"lower": 53.0, "upper": 87.0},
            "pace_vs_prior_month_same_day": 1.12,
            "pace_vs_prior_year_same_day": 1.08,
            "blend_weights": [{"name": "blended_recent_curve", "weight": 1.0}],
            "growth_regime": "accelerating",
            "stability_score": 72.0,
            "bias_risk": "low",
            "uncertainty_level": "medium",
            "uncertainty_pct": 11.0,
            "current_month_basis": "validation_weighted_pacing_ensemble",
        },
    )

    with app.test_request_context():
        display_series, fit_series, meta = overview_forecast._build_monthly_history(
            FilterParams(start=pd.Timestamp("2026-01-01"), end=pd.Timestamp("2026-03-19")),
            metric="revenue",
            include_current=True,
        )

    assert list(display_series.index.strftime("%Y-%m")) == ["2026-01", "2026-02"]
    assert list(fit_series.index.strftime("%Y-%m")) == ["2026-01", "2026-02", "2026-03"]
    assert meta["partial_period"]["detected"] is True
    assert meta["partial_period"]["included"] is True
    assert meta["partial_period"]["nowcast_applied"] is True
    assert meta["nowcast"]["estimated_month_end"] == 145.0


def test_current_month_nowcast_falls_back_to_monthly_progress_when_daily_path_is_unavailable(monkeypatch, app):
    monthly = pd.DataFrame(
        {
            "revenue": [140.0, 150.0, 155.0],
            "cost": [85.0, 90.0, 94.0],
            "qty": [10.0, 11.0, 12.0],
            "profit": [55.0, 60.0, 61.0],
            "margin_pct": [39.3, 40.0, 39.4],
        },
        index=pd.period_range("2026-01", periods=3, freq="M"),
    )
    monkeypatch.setattr(
        overview_forecast,
        "_daily_fact_frame",
        lambda _filters: (pd.DataFrame(), {"source": "fact_store_failed"}),
    )

    with app.test_request_context():
        payload = overview_forecast._current_month_nowcast(
            FilterParams(start=pd.Timestamp("2026-01-01"), end=pd.Timestamp("2026-03-28")),
            monthly,
        )

    assert payload["applied"] is True
    assert payload["current_month_basis"] == "monthly_progress_fallback"
    assert payload["estimated_month_end_revenue"] >= payload["raw_mtd_revenue"]
    assert payload["estimated_month_end_cost"] >= payload["raw_mtd_cost"]
    assert payload["source"] == "fact_store_failed"


def test_build_monthly_history_uses_protected_note_for_fallback_nowcast(monkeypatch, app):
    monthly = pd.DataFrame(
        {
            "revenue": [100.0, 120.0, 60.0],
            "cost": [50.0, 60.0, 30.0],
            "qty": [10.0, 12.0, 6.0],
        },
        index=pd.period_range("2026-01", periods=3, freq="M"),
    )
    monkeypatch.setattr(overview_forecast, "monthly_series", lambda filters, include_partial_current=True: (monthly, {}))
    monkeypatch.setattr(
        overview_forecast,
        "_current_month_nowcast",
        lambda *a, **k: {
            "applied": True,
            "period": "2026-03",
            "effective_end": "2026-03-19",
            "estimated_month_end_revenue": 145.0,
            "estimated_month_end_cost": 75.0,
            "estimated_month_end_profit": 70.0,
            "estimated_month_end_margin_pct": 48.3,
            "raw_mtd_revenue": 60.0,
            "raw_mtd_cost": 30.0,
            "raw_mtd_profit": 30.0,
            "raw_mtd_margin_pct": 50.0,
            "revenue_interval": {"lower": 132.0, "upper": 158.0},
            "cost_interval": {"lower": 71.0, "upper": 79.0},
            "profit_interval": {"lower": 53.0, "upper": 87.0},
            "growth_regime": "stable",
            "stability_score": 61.0,
            "bias_risk": "medium",
            "uncertainty_level": "medium",
            "uncertainty_pct": 12.0,
            "current_month_basis": "monthly_progress_fallback",
        },
    )

    with app.test_request_context():
        _display_series, _fit_series, meta = overview_forecast._build_monthly_history(
            FilterParams(start=pd.Timestamp("2026-01-01"), end=pd.Timestamp("2026-03-19")),
            metric="revenue",
            include_current=True,
        )

    assert meta["partial_period"]["nowcast_applied"] is True
    assert "protected month-end pace nowcast" in meta["partial_period"]["note"]


def test_forecast_v2_uses_nowcast_terminal_point_for_final_fit(monkeypatch, app):
    history = pd.Series(
        [120.0, 128.0, 134.0, 141.0, 149.0, 156.0, 164.0, 171.0, 179.0, 186.0],
        index=pd.date_range("2025-05-01", periods=10, freq="MS"),
        dtype="float64",
    )
    fit_history = history.copy()
    fit_history.loc[pd.Timestamp("2026-03-01")] = 245.0
    calls = {"last_fit_value": None}

    def _forecast_fn(train, horizon):
        return pd.Series(
            [float(train.iloc[-1])] * horizon,
            index=pd.date_range(pd.Timestamp(train.index[-1]) + pd.offsets.MonthBegin(1), periods=horizon, freq="MS"),
            dtype="float64",
        )

    def _runner(train, horizon):
        calls["last_fit_value"] = float(train.iloc[-1])
        return _forecast_fn(train, horizon), 8.0

    monkeypatch.setattr(
        overview_forecast,
        "_build_monthly_history",
        lambda *a, **k: (
            history,
            fit_history,
            {
                "partial_period": {
                    "detected": True,
                    "included": True,
                    "excluded": False,
                    "nowcast_applied": True,
                    "period": "2026-03",
                },
                "history_basis": {
                    "label": "Last 10 complete months",
                    "mode": "recent_10",
                    "reason": "Test history basis.",
                    "available_points": 10,
                    "selected_points": 10,
                    "available_start": "2025-05-01",
                    "available_end": "2026-02-01",
                    "selected_start": "2025-05-01",
                    "selected_end": "2026-02-01",
                    "excluded_points": 0,
                    "non_zero_share_pct": 100.0,
                },
                "nowcast": {
                    "applied": True,
                    "period": "2026-03",
                    "estimated_month_end": 245.0,
                    "yhat_lower": 228.0,
                    "yhat_upper": 262.0,
                    "growth_regime": "accelerating",
                    "stability_score": 74.0,
                    "bias_risk": "low",
                    "current_month_basis": "validation_weighted_pacing_ensemble",
                    "uncertainty_pct": 11.0,
                },
            },
        ),
    )
    monkeypatch.setattr(
        overview_forecast,
        "_build_candidate_specs",
        lambda *a, **k: [
            {
                "name": "level_baseline",
                "display_name": "Conservative Baseline",
                "family": "fallback",
                "history_mode": "recent_6",
                "forecast_fn": _forecast_fn,
                "runner": _runner,
                "max_windows": 3,
                "priority": 1,
            }
        ],
    )
    monkeypatch.setattr(overview_forecast, "cache", types.SimpleNamespace(get=lambda _k: None, set=lambda *_a, **_k: None))

    with app.test_request_context():
        payload = overview_forecast.forecast_metric_v2(
            FilterParams(start=pd.Timestamp("2025-05-01"), end=pd.Timestamp("2026-03-19")),
            metric="revenue",
            horizon=3,
            granularity="monthly",
            include_current=True,
        )

    assert payload["eligible"] is True
    assert calls["last_fit_value"] == 245.0
    assert payload["nowcast"]["estimated_month_end"] == 245.0
    assert payload["history"][-1]["ds"] == "2026-02-01"
    assert payload["forecast"][0]["ds"] == "2026-03-01"
    assert payload["forecast"][0]["yhat"] == 245.0


def test_forecast_v2_keeps_current_month_nowcast_visible_when_forward_model_is_ineligible(monkeypatch, app):
    raw_history = pd.Series(
        [120.0, 128.0],
        index=pd.date_range("2026-01-01", periods=2, freq="MS"),
        dtype="float64",
    )
    fit_history = raw_history.copy()
    fit_history.loc[pd.Timestamp("2026-03-01")] = 245.0
    monkeypatch.setattr(
        overview_forecast,
        "_build_monthly_history",
        lambda *a, **k: (
            raw_history,
            fit_history,
            {
                "partial_period": {
                    "detected": True,
                    "included": True,
                    "excluded": False,
                    "nowcast_applied": True,
                    "period": "2026-03",
                },
                "nowcast": {
                    "applied": True,
                    "period": "2026-03",
                    "estimated_month_end": 245.0,
                    "yhat_lower": 228.0,
                    "yhat_upper": 262.0,
                    "growth_regime": "accelerating",
                    "stability_score": 74.0,
                    "bias_risk": "low",
                    "current_month_basis": "validation_weighted_pacing_ensemble",
                    "uncertainty_pct": 11.0,
                },
            },
        ),
    )
    monkeypatch.setattr(overview_forecast, "cache", types.SimpleNamespace(get=lambda _k: None, set=lambda *_a, **_k: None))

    with app.test_request_context():
        payload = overview_forecast.forecast_metric_v2(
            FilterParams(start=pd.Timestamp("2026-01-01"), end=pd.Timestamp("2026-03-28")),
            metric="revenue",
            horizon=6,
            granularity="monthly",
            include_current=True,
        )

    assert payload["eligible"] is False
    assert payload["nowcast"]["applied"] is True
    assert payload["history"][-1]["ds"] == "2026-02-01"
    assert payload["forecast"][0]["ds"] == "2026-03-01"
    assert payload["forecast"][0]["yhat"] == 245.0
    assert payload["series"][-1]["t"] == "2026-03"
    assert payload["series"][-1]["forecast"] == 245.0
    assert "Current month is estimated to month-end" in payload["summary"]


def test_select_monthly_training_history_prefers_recent_36_months():
    series = pd.Series(
        [float(idx) for idx in range(48)],
        index=pd.date_range("2022-01-01", periods=48, freq="MS"),
        dtype="float64",
    )

    selected, meta = overview_forecast._select_monthly_training_history(series)

    assert len(selected) == 36
    assert meta["mode"] == "recent_36_default"
    assert meta["selected_points"] == 36
    assert meta["excluded_points"] == 12
    assert meta["selected_start"] == "2023-01-01"


def test_select_monthly_training_history_tightens_sparse_long_history():
    values = [0.0 if idx % 2 else float(idx + 10) for idx in range(48)]
    series = pd.Series(
        values,
        index=pd.date_range("2022-01-01", periods=48, freq="MS"),
        dtype="float64",
    )

    selected, meta = overview_forecast._select_monthly_training_history(series)

    assert len(selected) == 24
    assert meta["mode"] == "recent_24_sparse"
    assert meta["selected_points"] == 24
    assert meta["excluded_points"] == 24
    assert meta["non_zero_share_pct"] < 55


def test_forecast_v2_emits_history_basis_metadata(monkeypatch, app):
    series = pd.Series(
        [150.0 + (idx % 12) * 6 for idx in range(36)],
        index=pd.date_range("2023-01-01", periods=36, freq="MS"),
        dtype="float64",
    )
    monkeypatch.setattr(
        overview_forecast,
        "_build_monthly_history",
        lambda *a, **k: (
            series,
            series,
            {
                "partial_period": {"detected": False},
                "history_basis": {
                    "label": "Last 36 complete months",
                    "mode": "recent_36_default",
                    "reason": "Preferred the most recent 36 complete months.",
                    "available_points": 48,
                    "selected_points": 36,
                    "available_start": "2021-01-01",
                    "available_end": "2024-12-01",
                    "selected_start": "2022-01-01",
                    "selected_end": "2024-12-01",
                    "excluded_points": 12,
                    "non_zero_share_pct": 100.0,
                },
            },
        ),
    )
    monkeypatch.setattr(overview_forecast, "cache", types.SimpleNamespace(get=lambda _k: None, set=lambda *_a, **_k: None))

    with app.test_request_context():
        payload = overview_forecast.forecast_metric_v2(FilterParams(), metric="revenue", horizon=6, granularity="monthly")

    diagnostics = payload["diagnostics"]
    assert diagnostics["history_basis_label"] == "Last 36 complete months"
    assert diagnostics["history_basis_mode"] == "recent_36_default"
    assert diagnostics["history_basis_points"] == 36
    assert diagnostics["available_history_points"] == 48
    assert diagnostics["history_excluded_points"] == 12


def test_forecast_v2_long_history_not_flat_under_trend_shift(monkeypatch, app):
    first = [95.0, 96.0, 98.0, 100.0, 102.0, 105.0, 108.0, 110.0, 112.0, 114.0, 116.0, 118.0] * 4
    second = [165.0, 170.0, 178.0, 185.0, 194.0, 202.0, 210.0, 218.0, 225.0, 232.0, 240.0, 248.0] * 2
    vals = first + second
    series = pd.Series(vals, index=pd.date_range("2018-01-01", periods=len(vals), freq="MS"), dtype="float64")
    monkeypatch.setattr(overview_forecast, "_build_monthly_history", lambda *a, **k: (series, series, {"partial_period": {"detected": False}}))
    monkeypatch.setattr(overview_forecast, "cache", types.SimpleNamespace(get=lambda _k: None, set=lambda *_a, **_k: None))

    with app.test_request_context():
        payload = overview_forecast.forecast_metric_v2(FilterParams(), metric="revenue", horizon=6, granularity="monthly")

    forecasts = [row["yhat"] for row in payload.get("forecast", []) if row.get("yhat") is not None]
    assert forecasts
    assert max(forecasts) - min(forecasts) > 1.0
    assert payload["diagnostics"]["level_shift_detected"] is True
    assert payload["model"]["runner_ups"]
