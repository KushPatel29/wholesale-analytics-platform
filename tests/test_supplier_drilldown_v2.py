from __future__ import annotations

import io
import re

import pandas as pd
import pytest
from werkzeug.exceptions import Forbidden

from app.services import fact_store
from app.services import presentation
from app.services import suppliers_bundle


@pytest.fixture
def seed_supplier_drilldown_v2(tmp_path, monkeypatch):
    rows = [
        {
            "Date": "2025-01-15",
            "DateExpected": "2025-01-15",
            "OrderId": "A-01",
            "SupplierId": "SUP_A",
            "SupplierName": "Supplier A",
            "ProductId": "P1",
            "ProductName": "Prime Rib",
            "Protein": "Grocery",
            "Category": "Steaks",
            "CustomerId": "C1",
            "CustomerName": "Customer One",
            "RegionName": "West",
            "ShippingMethodName": "Truck",
            "Revenue": 1200.0,
            "Cost": 780.0,
            "QuantityShipped": 100.0,
            "WeightLb": 200.0,
            "SalesRepId": "R1",
            "SalesRepName": "Rep One",
            "OrderStatus": "packed",
        },
        {
            "Date": "2025-02-15",
            "DateExpected": "2025-02-15",
            "OrderId": "A-02",
            "SupplierId": "SUP_A",
            "SupplierName": "Supplier A",
            "ProductId": "P2",
            "ProductName": "Striploin",
            "Protein": "Grocery",
            "Category": "Steaks",
            "CustomerId": "C2",
            "CustomerName": "Customer Two",
            "RegionName": "West",
            "ShippingMethodName": "Air",
            "Revenue": 1000.0,
            "Cost": 700.0,
            "QuantityShipped": 90.0,
            "WeightLb": 180.0,
            "SalesRepId": "R1",
            "SalesRepName": "Rep One",
            "OrderStatus": "packed",
        },
        {
            "Date": "2025-03-10",
            "DateExpected": "2025-03-10",
            "OrderId": "A-03",
            "SupplierId": "SUP_A",
            "SupplierName": "Supplier A",
            "ProductId": "P1",
            "ProductName": "Prime Rib",
            "Protein": "Grocery",
            "Category": "Steaks",
            "CustomerId": "C1",
            "CustomerName": "Customer One",
            "RegionName": "West",
            "ShippingMethodName": "Truck",
            "Revenue": 1700.0,
            "Cost": 1050.0,
            "QuantityShipped": 130.0,
            "WeightLb": 250.0,
            "SalesRepId": "R1",
            "SalesRepName": "Rep One",
            "OrderStatus": "packed",
        },
        {
            "Date": "2025-04-10",
            "DateExpected": "2025-04-10",
            "OrderId": "A-04",
            "SupplierId": "SUP_A",
            "SupplierName": "Supplier A",
            "ProductId": "P3",
            "ProductName": "Top Sirloin",
            "Protein": "Grocery",
            "Category": "Steaks",
            "CustomerId": "C3",
            "CustomerName": "Customer Three",
            "RegionName": "South",
            "ShippingMethodName": "Courier",
            "Revenue": 900.0,
            "Cost": None,
            "QuantityShipped": 70.0,
            "WeightLb": 140.0,
            "SalesRepId": "R1",
            "SalesRepName": "Rep One",
            "OrderStatus": "packed",
        },
        {
            "Date": "2025-03-12",
            "DateExpected": "2025-03-12",
            "OrderId": "B-01",
            "SupplierId": "SUP_B",
            "SupplierName": "Supplier B",
            "ProductId": "P9",
            "ProductName": "Outside Scope SKU",
            "Protein": "Apparel",
            "Category": "Bacon",
            "CustomerId": "C9",
            "CustomerName": "Customer Nine",
            "RegionName": "North",
            "ShippingMethodName": "Truck",
            "Revenue": 1400.0,
            "Cost": 900.0,
            "QuantityShipped": 110.0,
            "WeightLb": 230.0,
            "SalesRepId": "R2",
            "SalesRepName": "Rep Two",
            "OrderStatus": "packed",
        },
    ]

    df = pd.DataFrame(rows)
    df["revenue_ordered"] = df["Revenue"]
    df["cost_ordered"] = df["Cost"]
    parquet_path = tmp_path / "fact_supplier_drilldown_v2.parquet"
    df.to_parquet(parquet_path)
    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def _scope_admin():
    return {
        "is_admin": True,
        "scope_mode": "all",
        "allowed_erp_user_ids": [],
        "sales_rep_ids": [],
        "allowed_count": 0,
        "scope_hash": "scope-admin-sup-drill-v2",
        "permissions_version": "1",
        "user_id": 1,
        "role": "admin",
    }


def test_supplier_drilldown_v2_template_flag_on_off(app_client, seed_supplier_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "SUPPLIERS_V2", True)
    monkeypatch.setitem(app_client.application.config, "SUPPLIER_DRILLDOWN_V2", True)
    resp_v2 = app_client.get("/suppliers/SUP_A", query_string={"start": "2025-03-01", "end": "2025-04-30"})
    assert resp_v2.status_code == 200
    body_v2 = resp_v2.get_data(as_text=True)
    assert "Supplier Command Center" in body_v2
    assert "Health & Trust" in body_v2
    assert "Decision-Ready Product Table" in body_v2
    assert "ASP/lb" in body_v2
    assert "Products vs Customers (XLSX)" in body_v2

    monkeypatch.setitem(app_client.application.config, "SUPPLIER_DRILLDOWN_V2", False)
    resp_v1 = app_client.get("/suppliers/SUP_A", query_string={"start": "2025-03-01", "end": "2025-04-30"})
    assert resp_v1.status_code == 200
    body_v1 = resp_v1.get_data(as_text=True)
    assert "Monthly Revenue" in body_v1
    assert "Supplier Performance & Diagnostics" not in body_v1


def test_supplier_drilldown_v2_requires_both_flags(app_client, seed_supplier_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "SUPPLIERS_V2", False)
    monkeypatch.setitem(app_client.application.config, "SUPPLIER_DRILLDOWN_V2", True)

    resp = app_client.get("/suppliers/SUP_A", query_string={"start": "2025-03-01", "end": "2025-04-30"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Supplier Performance & Diagnostics" not in body
    assert "Monthly Revenue" in body


def test_supplier_drilldown_v2_month_labels_clean(app_client, seed_supplier_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    resp = app_client.get(
        "/api/suppliers/drilldown/bundle",
        query_string={
            "supplier_id": "SUP_A",
            "supplier_drilldown_v2": "1",
            "start": "2025-03-01",
            "end": "2025-04-30",
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    labels = (((payload.get("supplier_v2") or {}).get("trend") or {}).get("labels") or [])
    assert labels, "expected monthly trend labels"
    assert labels == sorted(labels)
    assert all(re.match(r"^\d{4}-\d{2}$", str(label)) for label in labels)
    assert all(":" not in str(label) for label in labels)


def test_supplier_drilldown_v2_export_customers_has_full_rows(app_client, seed_supplier_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    resp = app_client.get(
        "/api/suppliers/SUP_A/drilldown/export.csv",
        query_string={
            "dataset": "customers",
            "supplier_drilldown_v2": "1",
            "start": "2025-03-01",
            "end": "2025-04-30",
        },
    )
    assert resp.status_code == 200
    frame = pd.read_csv(io.StringIO(resp.get_data(as_text=True)))
    assert len(frame.index) >= 2
    assert {"CustomerId", "CustomerName", "Revenue"}.issubset(frame.columns)


def test_supplier_drilldown_v2_export_products_vs_customers_columns(app_client, seed_supplier_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "SUPPLIERS_V2", True)
    monkeypatch.setitem(app_client.application.config, "SUPPLIER_DRILLDOWN_V2", True)

    resp = app_client.get(
        "/api/suppliers/SUP_A/drilldown/export.csv",
        query_string={
            "dataset": "products_vs_customers",
            "supplier_drilldown_v2": "1",
            "start": "2025-03-01",
            "end": "2025-04-30",
        },
    )
    assert resp.status_code == 200
    frame = pd.read_csv(io.StringIO(resp.get_data(as_text=True)))
    expected_cols = {
        "SupplierId",
        "SupplierName",
        "SKU",
        "Product",
        "ProductName",
        "ProteinFamily",
        "ProductCategory",
        "CustomerId",
        "CustomerName",
        "Region",
        "Orders",
        "Units",
        "WeightLb",
        "Revenue",
        "Cost",
        "Profit",
        "MarginPct",
        "ASP/lb",
        "FirstOrderDate",
        "LastOrderDate",
        "RevenueShareWithinSupplier",
        "RevenueShareWithinProduct",
        "CustomerRankForProduct",
        "ProductRankWithinSupplier",
        "ActiveFilterStart",
        "ActiveFilterEnd",
    }
    assert expected_cols.issubset(frame.columns)
    assert len(frame.index) >= 2
    assert set(frame["SupplierId"].astype(str)) == {"SUP_A"}
    assert set(frame["Region"].astype(str)) <= {"West", "South"}
    assert set(frame["ProteinFamily"].dropna().astype(str)) == {"Grocery"}
    assert set(frame["ProductCategory"].dropna().astype(str)) == {"Steaks"}
    assert frame["Product"].astype(str).str.contains(" — ").any()
    assert "ASP" not in frame.columns


def test_supplier_drilldown_v2_scope_denied_returns_403(app_client, seed_supplier_drilldown_v2, monkeypatch):
    monkeypatch.setitem(app_client.application.config, "SUPPLIERS_V2", True)
    monkeypatch.setitem(app_client.application.config, "SUPPLIER_DRILLDOWN_V2", True)

    def _deny(*_args, **_kwargs):
        raise Forbidden("Entity not in scope")

    monkeypatch.setattr("app.core.access_policy.enforce_entity_access", _deny)

    page_resp = app_client.get("/suppliers/SUP_B", query_string={"start": "2025-03-01", "end": "2025-04-30"})
    assert page_resp.status_code == 403

    export_resp = app_client.get(
        "/api/suppliers/SUP_B/drilldown/export.csv",
        query_string={"dataset": "customers", "supplier_drilldown_v2": "1", "start": "2025-03-01", "end": "2025-04-30"},
    )
    assert export_resp.status_code == 403


def test_supplier_product_label_formatter_standardizes_sku_and_name():
    assert presentation.format_product_label("13667", "Deli Bacon No Nitrate Added") == "13667 — Deli Bacon No Nitrate Added"
    assert presentation.compact_product_label("13667", "Deli Bacon No Nitrate Added Fresh Sliced (Retail Case)", max_length=28).startswith("13667 — ")


def test_supplier_drilldown_product_display_uses_sku_and_unnamed_product_fallback():
    display = suppliers_bundle._product_display_fields("13667", None)
    assert display["display_name"] == "13667 — Unnamed Product"
    assert display["display_name_short"].startswith("13667 — ")


def test_supplier_drilldown_v2_payload_uses_asp_per_lb_and_combined_product_labels(app_client, seed_supplier_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    resp = app_client.get(
        "/api/suppliers/drilldown/bundle",
        query_string={
            "supplier_id": "SUP_A",
            "supplier_drilldown_v2": "1",
            "start": "2025-03-01",
            "end": "2025-04-30",
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    supplier_v2 = payload.get("supplier_v2") or {}
    score = supplier_v2.get("scorecard") or {}
    assert score.get("asp_lb") == pytest.approx(2600.0 / 390.0, rel=1e-4)
    assert score.get("asp_lb_delta_pct") == pytest.approx((((2600.0 / 390.0) - (2200.0 / 380.0)) / (2200.0 / 380.0)) * 100.0, rel=1e-4)

    product_rows = ((supplier_v2.get("products_table") or {}).get("rows") or [])
    assert product_rows, "expected product rows"
    first = product_rows[0]
    assert first.get("display_name") == "P1 — Prime Rib"
    assert "display_name_axis" in first
    assert "display_name_short" in first
    assert first.get("protein_family") == "Grocery"
    assert first.get("product_category") == "Steaks"

    protein_rows = ((supplier_v2.get("protein") or {}).get("rows") or [])
    assert protein_rows
    assert protein_rows[0].get("protein_family") == "Grocery"
    assert protein_rows[0].get("lead_category") == "Steaks"


def test_supplier_drilldown_v2_summary_export_uses_asp_lb_header(app_client, seed_supplier_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    resp = app_client.get(
        "/api/suppliers/SUP_A/drilldown/export.csv",
        query_string={
            "dataset": "summary",
            "supplier_drilldown_v2": "1",
            "start": "2025-03-01",
            "end": "2025-04-30",
        },
    )
    assert resp.status_code == 200
    frame = pd.read_csv(io.StringIO(resp.get_data(as_text=True)))
    assert "ASP/lb" in frame.columns


def test_supplier_drilldown_v2_route_gracefully_renders_notice_when_bundle_errors(app_client, seed_supplier_drilldown_v2, monkeypatch):
    monkeypatch.setitem(app_client.application.config, "SUPPLIERS_V2", True)
    monkeypatch.setitem(app_client.application.config, "SUPPLIER_DRILLDOWN_V2", True)
    monkeypatch.setattr("app.services.bundle_service.drilldown", lambda *_args, **_kwargs: {"error": {"message": "synthetic supplier bundle failure"}, "meta": {}})

    resp = app_client.get("/suppliers/SUP_A", query_string={"start": "2025-03-01", "end": "2025-04-30"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "synthetic supplier bundle failure" in body
    assert "Supplier Command Center" in body
    assert "Department & Category Exposure" in body
