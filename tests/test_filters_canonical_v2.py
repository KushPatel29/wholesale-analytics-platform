from __future__ import annotations

import io
import os

import pandas as pd
import pytest
from flask import request

from app.services import fact_store
from app.services.filters import STICKY_FILTERS_SESSION_KEY, filters_to_store, resolve_filters


@pytest.fixture
def seed_filters_fact(tmp_path, monkeypatch):
    df = pd.DataFrame(
        [
            {
                "Date": "2025-01-05",
                "DateExpected": "2025-01-05",
                "ProductId": "SKU-1",
                "ProductName": "Ribeye",
                "CustomerId": "C-1",
                "CustomerName": "Chef A",
                "RegionName": "West",
                "SupplierName": "Supplier A",
                "OrderId": "O-1",
                "OrderStatus": "packed",
                "Revenue": 100.0,
                "Cost": 60.0,
                "QuantityShipped": 10,
                "WeightLb": 30,
                "UnitOfBillingId": 1,
                "pack_item_count_sum": 10.0,
                "pack_weight_lb_sum": 30.0,
                "pack_count": 1,
                "Price": 10.0,
                "CostPrice": 6.0,
            },
            {
                "Date": "2025-01-10",
                "DateExpected": "2025-01-10",
                "ProductId": "SKU-2",
                "ProductName": "Tenderloin",
                "CustomerId": "C-2",
                "CustomerName": "Chef B",
                "RegionName": "West",
                "SupplierName": "Supplier B",
                "OrderId": "O-2",
                "OrderStatus": "packed",
                "Revenue": 200.0,
                "Cost": 120.0,
                "QuantityShipped": 20,
                "WeightLb": 60,
                "UnitOfBillingId": 1,
                "pack_item_count_sum": 20.0,
                "pack_weight_lb_sum": 60.0,
                "pack_count": 1,
                "Price": 10.0,
                "CostPrice": 6.0,
            },
            {
                "Date": "2025-01-12",
                "DateExpected": "2025-01-12",
                "ProductId": "SKU-1",
                "ProductName": "Ribeye",
                "CustomerId": "C-3",
                "CustomerName": "Chef C",
                "RegionName": "East",
                "SupplierName": "Supplier A",
                "OrderId": "O-3",
                "OrderStatus": "packed",
                "Revenue": 300.0,
                "Cost": 180.0,
                "QuantityShipped": 30,
                "WeightLb": 90,
                "UnitOfBillingId": 1,
                "pack_item_count_sum": 30.0,
                "pack_weight_lb_sum": 90.0,
                "pack_count": 1,
                "Price": 10.0,
                "CostPrice": 6.0,
            },
            {
                "Date": "2025-02-02",
                "DateExpected": "2025-02-02",
                "ProductId": "SKU-1",
                "ProductName": "Ribeye",
                "CustomerId": "C-4",
                "CustomerName": "Chef D",
                "RegionName": "West",
                "SupplierName": "Supplier A",
                "OrderId": "O-4",
                "OrderStatus": "packed",
                "Revenue": 400.0,
                "Cost": 250.0,
                "QuantityShipped": 40,
                "WeightLb": 120,
                "UnitOfBillingId": 1,
                "pack_item_count_sum": 40.0,
                "pack_weight_lb_sum": 120.0,
                "pack_count": 1,
                "Price": 10.0,
                "CostPrice": 6.25,
            },
        ]
    )
    parquet_path = tmp_path / "filters_v2_fact.parquet"
    df.to_parquet(parquet_path)
    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield df
    fact_store.reset_duckdb_state()


@pytest.fixture
def seed_filters_fact_with_status(seed_filters_fact):
    parquet_path = os.getenv("PARQUET_PATH")
    assert parquet_path
    df = pd.read_parquet(parquet_path)
    extra = pd.DataFrame(
        [
            {
                "Date": "2025-01-20",
                "DateExpected": "2025-01-20",
                "ProductId": "SKU-1",
                "ProductName": "Ribeye",
                "CustomerId": "C-9",
                "CustomerName": "Chef Z",
                "RegionName": "West",
                "SupplierName": "Supplier A",
                "OrderId": "O-9",
                "OrderStatus": "cancelled",
                "Revenue": 50.0,
                "Cost": 30.0,
                "QuantityShipped": 5,
                "WeightLb": 15,
                "UnitOfBillingId": 1,
                "pack_item_count_sum": 5.0,
                "pack_weight_lb_sum": 15.0,
                "pack_count": 1,
                "Price": 10.0,
                "CostPrice": 6.0,
            }
        ]
    )
    df = pd.concat([df, extra], ignore_index=True)
    df.to_parquet(parquet_path)
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield df
    fact_store.reset_duckdb_state()


def _stub_filter_options(params):
    return {
        "options": {
            "regions": [{"id": "West", "label": "West", "bucket": "regions"}],
            "methods": [],
            "ship_methods": [],
            "customers": [],
            "suppliers": [],
            "products": [],
            "sales_reps": [],
            "statuses": [],
        },
        "dataset_version": "filters-v2-test",
        "date_min": "2025-01-01",
        "date_max": "2025-02-28",
        "filters": filters_to_store(params),
        "scope": {},
        "cached": False,
    }


def _enable_v2(client):
    app = client.application
    app.config["FILTERS_CANONICAL_V2"] = True
    app.config["STICKY_FILTERS"] = True
    app.config["AUTHZ_DISABLED"] = True
    app.config["LOGIN_DISABLED"] = True


def test_filter_persistence_apply_redirect(client, monkeypatch):
    _enable_v2(client)
    monkeypatch.setattr(
        "app.services.filters_service.get_filter_options",
        lambda params, scope: _stub_filter_options(params),
    )

    resp = client.post(
        "/filters/apply",
        data={
            "next": "/api/filters/options",
            "start": "2025-01-01",
            "end": "2025-01-31",
            "regions": ["West"],
        },
        follow_redirects=False,
    )
    assert resp.status_code in {302, 303}
    assert resp.headers["Location"].endswith("/api/filters/options")

    follow = client.get("/api/filters/options")
    assert follow.status_code == 200
    payload = follow.get_json()
    assert payload["filters"]["start_date"] == "2025-01-01"
    assert payload["filters"]["end_date"] == "2025-01-31"
    assert payload["filters"]["regions"] == ["West"]

    with client.session_transaction() as sess:
        stored = sess.get(STICKY_FILTERS_SESSION_KEY) or {}
        assert (stored.get("filters") or {}).get("regions") == ["West"]


def test_consistency_bundle_drilldown_export_use_same_filters(client, seed_filters_fact):
    _enable_v2(client)
    client.post(
        "/filters/apply",
        data={
            "next": "/products",
            "start": "2025-01-01",
            "end": "2025-01-31",
            "regions": ["West"],
        },
    )

    bundle_resp = client.get("/api/products/bundle")
    drill_resp = client.get("/api/products/drilldown/bundle", query_string={"sku": "SKU-1"})
    export_resp = client.get("/products/export/overview.csv")

    assert bundle_resp.status_code == 200
    assert drill_resp.status_code == 200
    assert export_resp.status_code == 200

    h_bundle = bundle_resp.headers.get("X-Filters-Hash")
    h_drill = drill_resp.headers.get("X-Filters-Hash")
    h_export = export_resp.headers.get("X-Filters-Hash")
    assert h_bundle and h_bundle == h_drill == h_export

    bundle = bundle_resp.get_json()
    drill = drill_resp.get_json()
    exported = pd.read_csv(io.BytesIO(export_resp.data))

    # West + Jan rows are O-1 and O-2
    assert float(bundle.get("kpis", {}).get("revenue", 0.0)) == pytest.approx(300.0, rel=1e-6)
    assert float(drill.get("kpis", {}).get("revenue", 0.0)) == pytest.approx(100.0, rel=1e-6)
    if "revenue" in exported.columns and not exported.empty:
        exported_sum = 0.0
        for raw in exported["revenue"].tolist():
            try:
                if raw is None or pd.isna(raw):
                    continue
            except Exception:
                if raw is None:
                    continue
            try:
                exported_sum += float(raw)
            except Exception:
                continue
        assert exported_sum <= float(bundle.get("kpis", {}).get("revenue", 0.0)) + 1e-6


def test_cache_key_changes_for_different_filters(client, seed_filters_fact):
    _enable_v2(client)

    west = client.get("/api/products/bundle", query_string={"start": "2025-01-01", "end": "2025-01-31", "regions": "West"})
    east = client.get("/api/products/bundle", query_string={"start": "2025-01-01", "end": "2025-01-31", "regions": "East"})

    assert west.status_code == 200
    assert east.status_code == 200
    west_payload = west.get_json()
    east_payload = east.get_json()

    west_key = (west_payload.get("meta") or {}).get("cache_key")
    east_key = (east_payload.get("meta") or {}).get("cache_key")
    assert west_key and east_key and west_key != east_key
    assert int(west_payload.get("kpis", {}).get("customers", 0)) != int(east_payload.get("kpis", {}).get("customers", 0))


def test_get_requests_do_not_mutate_session_filters_in_v2(client, monkeypatch):
    _enable_v2(client)
    monkeypatch.setattr(
        "app.services.filters_service.get_filter_options",
        lambda params, scope: _stub_filter_options(params),
    )

    with client.session_transaction() as sess:
        sess.pop(STICKY_FILTERS_SESSION_KEY, None)

    resp = client.get("/api/filters/options", query_string={"regions": "West"})
    assert resp.status_code == 200
    assert resp.headers.get("X-Filters-Source") == "explicit_request"

    with client.session_transaction() as sess:
        assert STICKY_FILTERS_SESSION_KEY not in sess


def test_normalization_order_does_not_change_filters_hash(app):
    app.config["FILTERS_CANONICAL_V2"] = True
    with app.test_request_context("/_test?regions=West&regions=East&customers=C-2&customers=C-1"):
        _, meta_a = resolve_filters(request, None, session_obj={}, source=request.args, sticky_enabled=True)
    with app.test_request_context("/_test?regions=East&regions=West&customers=C-1&customers=C-2"):
        _, meta_b = resolve_filters(request, None, session_obj={}, source=request.args, sticky_enabled=True)

    assert meta_a["filters_hash"] == meta_b["filters_hash"]
    assert meta_a["effective_filters"]["regions"] == ["East", "West"]
    assert meta_b["effective_filters"]["customers"] == ["C-1", "C-2"]


def test_products_overview_export_uses_sticky_filters(client, seed_filters_fact_with_status):
    _enable_v2(client)
    client.post(
        "/filters/apply",
        data={
            "next": "/products",
            "start": "2025-01-01",
            "end": "2025-01-31",
            "regions": ["West"],
            "statuses": ["packed"],
        },
    )

    bundle_resp = client.get("/api/products/bundle")
    export_resp = client.get("/products/export/overview.csv")
    assert bundle_resp.status_code == 200
    assert export_resp.status_code == 200

    h_bundle = bundle_resp.headers.get("X-Filters-Hash")
    h_export = export_resp.headers.get("X-Filters-Hash")
    assert h_bundle and h_bundle == h_export

    bundle_revenue = float(bundle_resp.get_json().get("kpis", {}).get("revenue", 0.0))
    exported = pd.read_csv(io.BytesIO(export_resp.data))
    revenue_col = None
    for col in list(exported.columns):
        if str(col).strip().lower() == "revenue":
            revenue_col = col
            break
    exported_revenue = 0.0
    if revenue_col is not None:
        for raw in exported[revenue_col].tolist():
            try:
                if raw is None or pd.isna(raw):
                    continue
            except Exception:
                if raw is None:
                    continue
            try:
                exported_revenue += float(raw)
            except Exception:
                continue
    assert exported_revenue == pytest.approx(bundle_revenue, rel=1e-6)


def test_customers_drilldown_page_uses_sticky_filters_without_reapply(client, seed_filters_fact):
    _enable_v2(client)
    client.post(
        "/filters/apply",
        data={
            "next": "/customers",
            "start": "2025-01-01",
            "end": "2025-01-31",
            "regions": ["West"],
        },
    )

    bundle_resp = client.get("/api/products/bundle")
    drill_resp = client.get("/customers/drilldown/C-1")
    assert bundle_resp.status_code == 200
    assert drill_resp.status_code == 200

    h_bundle = bundle_resp.headers.get("X-Filters-Hash")
    h_drill = drill_resp.headers.get("X-Filters-Hash")
    assert h_bundle and h_drill == h_bundle
    assert drill_resp.headers.get("X-Filters-Source") == "session"
