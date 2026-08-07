import pandas as pd

from app.services import overview_v2 as ov2
from app.services.filters import FilterParams, parse_filters
from app.services import analytics_utils as au


def _ctx(df: pd.DataFrame) -> ov2.FrameContext:
    colmap = au.column_map(df)
    flags = au.column_flags(colmap)
    missing = au.missing_from_map(colmap)
    window = {"start": None, "end": None, "days": 0, "rows": int(len(df))}
    return ov2.FrameContext(
        df=df,
        colmap=colmap,
        flags=flags,
        missing=missing,
        window=window,
        last_refresh="2024-01-01",
        version="test",
        cache_hit=False,
    )


def _disable_cache(monkeypatch):
    monkeypatch.setattr(ov2, "_from_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(ov2, "_store_cache", lambda *args, **kwargs: None)


def test_default_filter_window_uses_current_fy(app):
    with app.test_request_context():
        params = parse_filters({})
    assert params.start is not None
    assert params.end is not None
    assert params.start.year >= 2019
    assert params.preset == "current_fy"
    assert params.date_type == "fiscal"
    assert str(params.start.month) == "10"
    assert str(params.start.day) == "1"


def test_summary_handles_cost(app, monkeypatch):
    _disable_cache(monkeypatch)
    df = pd.DataFrame(
        {
            "Date": pd.date_range("2024-01-01", periods=3, freq="M"),
            "Revenue": [1000, 1100, 1200],
            "Cost": [500, 550, 600],
            "QuantityShipped": [10, 11, 12],
            "OrderId": [1, 2, 3],
            "CustomerId": ["c1", "c2", "c3"],
        }
    )
    monkeypatch.setattr(ov2, "get_filtered_frame", lambda user, filters: _ctx(df))
    with app.test_request_context():
        payload = ov2.build_summary(FilterParams())
    assert payload["kpis"]["profit"] > 0
    assert payload["kpis"]["margin_pct"] is not None


def test_summary_without_cost_is_graceful(app, monkeypatch):
    _disable_cache(monkeypatch)
    df = pd.DataFrame(
        {
            "Date": pd.date_range("2024-01-01", periods=2, freq="M"),
            "Revenue": [1000, 800],
            "QuantityShipped": [5, 4],
            "OrderId": [1, 2],
            "CustomerId": ["c1", "c2"],
        }
    )
    monkeypatch.setattr(ov2, "get_filtered_frame", lambda user, filters: _ctx(df))
    with app.test_request_context():
        payload = ov2.build_summary(FilterParams())
    assert payload["kpis"]["profit"] is None
    assert "Cost" in payload["meta"]["missing_columns"]


def test_summary_empty_frame(app, monkeypatch):
    _disable_cache(monkeypatch)
    df = pd.DataFrame(columns=["Date", "Revenue"])
    monkeypatch.setattr(ov2, "get_filtered_frame", lambda user, filters: _ctx(df))
    with app.test_request_context():
        payload = ov2.build_summary(FilterParams())
    assert payload["meta"]["has_data"] is False


def test_build_trend_uses_fiscal_month_labels(monkeypatch):
    monthly = pd.DataFrame(
        {
            "revenue": [1000.0, 1200.0],
            "qty": [10.0, 12.0],
            "asp": [100.0, 100.0],
        },
        index=pd.period_range("2025-10", periods=2, freq="M"),
    )
    monkeypatch.setattr(
        ov2,
        "get_bundle_context",
        lambda _filters: {
            "payload": {"meta": {"window": {"date_type": "fiscal", "trend_bucket_label": "Fiscal Month"}}},
            "monthly": monthly,
            "cache_hit": False,
        },
    )
    payload = ov2.build_trend(FilterParams(preset="current_fy", date_type="fiscal"), exclude_partial=False)
    assert payload["labels"] == ["FM1", "FM2"]
    assert payload["meta"]["date_type"] == "fiscal"
