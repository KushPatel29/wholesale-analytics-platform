
import types

import duckdb
import pandas as pd

from app.services import overview_insights
from app.services.filters import FilterParams


def _fake_cache():
    store = {}

    def _get(key):
        return store.get(key)

    def _set(key, value, timeout=None):
        store[key] = value

    return types.SimpleNamespace(get=_get, set=_set)


def _setup_conn(df):
    conn = duckdb.connect(database=":memory:")
    conn.register("df", df)
    conn.execute("CREATE VIEW fact AS SELECT * FROM df")
    return conn

def _patch_insights(monkeypatch, df):
    conn = _setup_conn(df)
    monkeypatch.setattr(overview_insights, "get_duck_conn", lambda: conn)
    monkeypatch.setattr(overview_insights, "init_duck_views", lambda conn=None: None)
    monkeypatch.setattr(overview_insights, "duck_columns", lambda conn=None: set(df.columns))
    monkeypatch.setattr(overview_insights, "cache", _fake_cache())
    return conn

def test_insights_new_customer_detection(monkeypatch, app):
    df = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2024-01-10", "2024-03-05"]),
            "Revenue": [200.0, 150.0],
            "Cost": [120.0, 60.0],
            "QuantityShipped": [10.0, 5.0],
            "CustomerId": ["c1", "c2"],
            "CustomerName": ["Alpha", "Beta"],
            "ProductId": ["p1", "p2"],
            "ProductName": ["Prod1", "Prod2"],
        }
    )
    _patch_insights(monkeypatch, df)
    params = FilterParams(start=pd.Timestamp("2024-03-01"), end=pd.Timestamp("2024-03-31"))

    with app.test_request_context():
        payload = overview_insights.build_insights_payload(params)

    stats = payload.get("insights", {}).get("callouts", [])
    new_callout = next((c for c in stats if c.get("title") == "New Customer Share"), {})
    assert "new" in (new_callout.get("detail") or "").lower()

def test_insights_revenue_mom_no_prev(monkeypatch, app):
    df = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2024-02-10"]),
            "Revenue": [300.0],
            "Cost": [150.0],
            "QuantityShipped": [10.0],
            "CustomerId": ["c1"],
            "CustomerName": ["Alpha"],
            "ProductId": ["p1"],
            "ProductName": ["Prod1"],
        }
    )
    _patch_insights(monkeypatch, df)
    params = FilterParams(start=pd.Timestamp("2024-02-01"), end=pd.Timestamp("2024-02-28"))

    with app.test_request_context():
        payload = overview_insights.build_insights_payload(params)

    callouts = payload.get("insights", {}).get("callouts", [])
    mom = next((c for c in callouts if c.get("title") == "Revenue MoM"), {})
    assert mom.get("value") is None
    assert "history" in (mom.get("detail") or "").lower()

def test_insights_cost_coverage_message(monkeypatch, app):
    df = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2024-03-01", "2024-03-02"]),
            "Revenue": [100.0, 120.0],
            "Cost": [None, None],
            "QuantityShipped": [5.0, 6.0],
            "CustomerId": ["c1", "c1"],
            "CustomerName": ["Alpha", "Alpha"],
            "ProductId": ["p1", "p1"],
            "ProductName": ["Prod1", "Prod1"],
        }
    )
    _patch_insights(monkeypatch, df)
    params = FilterParams(start=pd.Timestamp("2024-03-01"), end=pd.Timestamp("2024-03-31"))

    with app.test_request_context():
        payload = overview_insights.build_insights_payload(params)

    profitability = payload.get("profitability", {})
    assert "message" in profitability
