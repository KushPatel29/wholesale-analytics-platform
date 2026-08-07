from datetime import date

import pandas as pd

from flask import request

from app.blueprints import products


class _DummyParams:
    _MISSING = object()

    def __init__(self, *, start=_MISSING, end=_MISSING, preset=None):
        self.start = date(2024, 1, 1) if start is self._MISSING else start
        self.end = date(2024, 12, 31) if end is self._MISSING else end
        self.regions = tuple()
        self.methods = tuple()
        self.customers = tuple()
        self.suppliers = tuple()
        self.products = tuple()
        self.sales_reps = tuple()
        self.statuses = tuple()
        self.preset = preset
        self.protein_min = 10.0
        self.protein_max = 20.0
        self.protein_name_like = "beef"
        self.complete_months_only = True


def test_products_parse_filters_uses_default_window(monkeypatch, app):
    captured = {}

    def fake_parse(params):
        captured["params"] = params
        return _DummyParams()

    # This test validates the legacy parse_filter_params fallback path.
    monkeypatch.setitem(app.config, "FILTERS_CANONICAL_V2", False)
    monkeypatch.setattr(products, "parse_filter_params", fake_parse)

    with app.test_request_context("/products/api/overview"):
        parsed = products.parse_filters(request)

    # Should not force an all-time preset; let shared defaults drive the window
    assert captured["params"].get("preset") is None
    assert parsed["start"] == "2024-01-01"
    assert parsed["end"] == "2024-12-31"
    assert parsed["complete_months_only"] is True


def test_products_overview_without_costs_does_not_invent_margins(monkeypatch, app):
    df = pd.DataFrame(
        {
            "Date": pd.date_range("2024-01-01", periods=3, freq="MS"),
            "ProductId": [1, 1, 2],
            "revenue_ordered": [100.0, 150.0, 50.0],
            "QuantityOrdered": [10, 15, 5],
        }
    )

    # Force service payload empty so fallback DF path is used
    monkeypatch.setattr(products, "_build_overview_from_service", lambda *a, **k: {})
    monkeypatch.setattr(products, "get_fact_df", lambda *a, **k: df.copy())

    with app.test_request_context("/products/api/overview"):
        payload = products.build_overview_payload(include_forecast=False, fallback_months=6)

    assert payload["kpis"]["avg_margin"] is None
    assert all(tp.get("Margin%") is None for tp in payload["top_products"])
    assert all(tp.get("Margin$") is None for tp in payload["top_products"])


def test_products_parse_filters_preserves_open_ended_dates_and_protein_fields(monkeypatch, app):
    monkeypatch.setitem(app.config, "FILTERS_CANONICAL_V2", False)
    monkeypatch.setattr(products, "parse_filter_params", lambda _params, fallback_months=products.DEFAULT_MONTHS: _DummyParams(end=None))

    with app.test_request_context("/products/api/overview?start=2024-01-01"):
        parsed = products.parse_filters(request)

    assert parsed["start"] == "2024-01-01"
    assert parsed["end"] is None
    assert parsed["protein_min"] == 10.0
    assert parsed["protein_max"] == 20.0
    assert parsed["protein_name_like"] == "beef"
    assert parsed["complete_months_only"] is True


def test_products_parse_filters_preserves_explicit_all_time(monkeypatch, app):
    monkeypatch.setitem(app.config, "FILTERS_CANONICAL_V2", False)
    monkeypatch.setattr(
        products,
        "parse_filter_params",
        lambda _params, fallback_months=products.DEFAULT_MONTHS: _DummyParams(start=None, end=None, preset="all"),
    )

    with app.test_request_context("/products/api/overview?date_preset=all"):
        parsed = products.parse_filters(request)

    assert parsed["start"] is None
    assert parsed["end"] is None
    assert parsed["date_preset"] == "all"


def test_products_apply_filters_honors_open_end_date_and_entity_ids():
    df = pd.DataFrame(
        {
            products.CAN.date: pd.to_datetime(["2024-01-31 17:15:00", "2024-02-01 09:00:00"]),
            products.CAN.region: ["West", "East"],
            products.CAN.region_id: ["R1", "R2"],
            products.CAN.supplier: ["Supplier A", "Supplier B"],
            products.CAN.supplier_id: ["S1", "S2"],
            products.CAN.customer_id: ["C1", "C2"],
            products.CAN.customer_name: ["Chef A", "Chef B"],
            products.CAN.product_id: ["SKU-1", "SKU-2"],
            products.CAN.product_name: ["Beef Ribeye", "Chicken Breast"],
            products.CAN.sku: ["SKU-1", "SKU-2"],
        }
    )

    filtered = products.apply_filters(
        df,
        {
            "start_date": "2024-01-31",
            "end_date": "2024-01-31",
            "regions": ["R1"],
            "suppliers": ["S1"],
            "customers": ["Chef A"],
            "products": ["SKU-1"],
            "protein_name_like": "beef",
        },
    )

    assert filtered[products.CAN.product_id].tolist() == ["SKU-1"]


def test_products_build_querystring_includes_extended_filters():
    querystring = products.build_querystring(
        {
            "start_date": "2024-01-01",
            "end_date": "2024-01-31",
            "statuses": ["packed"],
            "sales_reps": ["REP-1"],
            "protein_min": 10,
            "protein_max": 20,
            "protein_name_like": "beef",
            "complete_months_only": True,
        }
    )

    assert "statuses=packed" in querystring
    assert "sales_reps=REP-1" in querystring
    assert "protein_min=10" in querystring
    assert "protein_max=20" in querystring
    assert "protein_name_like=beef" in querystring
    assert "complete_months_only=1" in querystring
