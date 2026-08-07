import json
import math
from datetime import datetime, timedelta

import pandas as pd
import pytest

from flask import request

from app.blueprints import products
from app.services import products as services_products


def _sample_df():
    today = pd.Timestamp.utcnow().normalize()
    start = (today - pd.DateOffset(months=5)).replace(day=1)
    dates = pd.date_range(start, periods=6, freq="MS")
    rows = []
    for i, dt in enumerate(dates):
        rows.append(
            {
                "Date": dt,
                "ProductId": 1,
                "revenue_ordered": 100 + i * 10,
                "QuantityOrdered": 10,
                "OrderId": f"A{i}",
                "CustomerId": f"C{i%2}",
            }
        )
        rows.append(
            {
                "Date": dt,
                "ProductId": 2,
                "revenue_ordered": 10,
                "QuantityOrdered": 2,
                "OrderId": f"B{i}",
                "CustomerId": f"Z{i%2}",
            }
        )
    return pd.DataFrame(rows)


def test_default_filter_last_six_months(app):
    app.config["FILTERS_CANONICAL_V2"] = False
    with app.test_request_context("/products/api/overview"):
        parsed = products.parse_filters(request)
    assert parsed["start"] is not None and parsed["end"] is not None
    start_dt = pd.to_datetime(parsed["start"]).to_pydatetime()
    if start_dt.tzinfo:
        start_dt = start_dt.replace(tzinfo=None)
    now = datetime.utcnow()
    delta_days = (now - start_dt).days
    assert 150 <= delta_days <= 220  # ~6 months window


def test_nan_sanitization_overview(client, monkeypatch):
    client.application.config["LOGIN_DISABLED"] = True
    from flask_login import utils as fl_utils
    dummy_user = type(
        "U",
        (),
        {
            "is_authenticated": True,
            "roles": ["admin"],
            "is_active": True,
            "is_anonymous": False,
            "get_id": lambda self: "test-user",
        },
    )()
    monkeypatch.setattr(fl_utils, "_get_user", lambda: dummy_user)
    monkeypatch.setattr(
        products,
        "_get_service_payload",
        lambda *a, **k: {
            "kpis": {"total_revenue": math.nan},
            "trend": [{"period": "2024-01", "revenue": float("inf")}],
            "price_dist": {"p50": float("-inf")},
        },
    )
    resp = client.get("/products/api/overview")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "NaN" not in body and "Infinity" not in body
    data = json.loads(body)
    assert data["kpis"]["total_revenue"] is None
    assert data["trend"]["revenue"][0] == 0 or data["trend"]["revenue"][0] is None


def test_segments_computation_from_fixture(monkeypatch, app):
    df = _sample_df()
    monkeypatch.setattr(products, "get_fact_df", lambda *a, **k: df.copy())
    monkeypatch.setattr(products, "_build_overview_from_service", lambda *a, **k: {})
    products._cached_df_overview.cache_clear()
    products._cached_sales_segments.cache_clear()
    with app.test_request_context("/products/api/segments"):
        segs = products._build_sales_segments(filters=products.parse_filters(request, fallback_months=6))
    prod_segments = {p["product_id"]: p["segment"] for p in segs.get("products", [])}
    assert prod_segments.get("1") in {"Stars", "Concentrated", "Steady Sellers", "At Risk"}
    assert prod_segments.get("2") in {"Long Tail", "No Signal", "Dormant", "At Risk"}
    assert segs.get("summary") is not None


def test_overview_and_drilldown_schema(monkeypatch, client):
    df = _sample_df()
    monkeypatch.setattr(products, "get_fact_df", lambda *a, **k: df.copy())
    client.application.config["LOGIN_DISABLED"] = True
    from flask_login import utils as fl_utils
    dummy_user = type(
        "U",
        (),
        {
            "is_authenticated": True,
            "roles": ["admin"],
            "is_active": True,
            "is_anonymous": False,
            "get_id": lambda self: "test-user",
        },
    )()
    monkeypatch.setattr(fl_utils, "_get_user", lambda: dummy_user)
    mock_payload_with_product = {
        "kpis": {"total_revenue": 1000, "unique_products": 2},
        "trend": [{"period": "2024-01", "revenue": 100}],
        "price_dist": {},
        "top_products": [{"product_id": "1", "desc": "Product 1", "sku": "1", "revenue": 500, "qty": 50, "avg_price": 10, "margin_pct": 20, "category": "Test", "supplier": "Test Supplier"}],
        "breakdowns": {},
        "top_movers": [],
    }
    monkeypatch.setattr(products, "_build_overview_from_service", lambda *a, **k: mock_payload_with_product)
    # We don't need to mock services_products.get_products_overview directly if _build_overview_from_service is mocked
    # Also mock _build_product_recommendations as it's called by product_detail
    monkeypatch.setattr(products, "_build_product_recommendations", lambda *a, **k: {"recommendations": []})
    products._cached_df_overview.cache_clear()
    products._cached_sales_segments.cache_clear()

    resp_overview = client.get("/products/api/overview")
    assert resp_overview.status_code == 200
    data = json.loads(resp_overview.data)
    assert "kpis" in data and "trend" in data

    resp_drill = client.get("/products/1")
    assert resp_drill.status_code == 200
    assert resp_drill.data
