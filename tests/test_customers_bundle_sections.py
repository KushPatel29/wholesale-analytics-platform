from __future__ import annotations

import pandas as pd
import pytest

from app.services import customers_bundle
from app.services import fact_store
from app.services.bundle_cache import _CACHE


@pytest.fixture
def seed_customers_bundle_sections(tmp_path, monkeypatch):
    rows = [
        {
            "Date": "2025-02-15",
            "DateExpected": "2025-02-15",
            "OrderId": "O-RET-PRIOR",
            "CustomerId": "C_RET",
            "CustomerName": "Returning Co",
            "ProductId": "P_BEEF",
            "ProductName": "Prime Beef",
            "ProteinType": "Beef",
            "RegionName": "West",
            "ShippingMethodName": "Truck",
            "City": "Los Angeles",
            "Province": "CA",
            "SalesRepId": "R1",
            "SalesRepName": "Rep One",
            "OrderStatus": "packed",
            "Revenue": 100.0,
            "Cost": 60.0,
            "QuantityOrdered": 10.0,
            "WeightLb": 20.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 10.0,
            "pack_weight_lb_sum": 20.0,
            "pack_count": 1,
            "Price": 100.0,
            "CostPrice": 60.0,
        },
        {
            "Date": "2025-03-10",
            "DateExpected": "2025-03-10",
            "OrderId": "O-RET-CUR",
            "CustomerId": "C_RET",
            "CustomerName": "Returning Co",
            "ProductId": "P_SALMON",
            "ProductName": "Atlantic Salmon",
            "ProteinType": "Seafood",
            "RegionName": "West",
            "ShippingMethodName": "Truck",
            "City": "Los Angeles",
            "Province": "CA",
            "SalesRepId": "R1",
            "SalesRepName": "Rep One",
            "OrderStatus": "packed",
            "Revenue": 140.0,
            "Cost": 84.0,
            "QuantityOrdered": 14.0,
            "WeightLb": 28.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 14.0,
            "pack_weight_lb_sum": 28.0,
            "pack_count": 1,
            "Price": 140.0,
            "CostPrice": 84.0,
        },
        {
            "Date": "2025-03-18",
            "DateExpected": "2025-03-18",
            "OrderId": "O-NEW-CUR",
            "CustomerId": "C_NEW",
            "CustomerName": "New Co",
            "ProductId": "P_BEEF",
            "ProductName": "Prime Beef",
            "ProteinType": "Beef",
            "RegionName": "West",
            "ShippingMethodName": "Courier",
            "City": "San Diego",
            "Province": "CA",
            "SalesRepId": "R1",
            "SalesRepName": "Rep One",
            "OrderStatus": "packed",
            "Revenue": 75.0,
            "Cost": 45.0,
            "QuantityOrdered": 6.0,
            "WeightLb": 12.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 6.0,
            "pack_weight_lb_sum": 12.0,
            "pack_count": 1,
            "Price": 75.0,
            "CostPrice": 45.0,
        },
        {
            "Date": "2025-03-20",
            "DateExpected": "2025-03-20",
            "OrderId": "O-OTHER-CUR",
            "CustomerId": "C_OTHER",
            "CustomerName": "Other Scope Co",
            "ProductId": "P_CHICKEN",
            "ProductName": "Organic Chicken",
            "ProteinType": "Poultry",
            "RegionName": "West",
            "ShippingMethodName": "Courier",
            "City": "San Diego",
            "Province": "CA",
            "SalesRepId": "R2",
            "SalesRepName": "Rep Two",
            "OrderStatus": "packed",
            "Revenue": 220.0,
            "Cost": 132.0,
            "QuantityOrdered": 22.0,
            "WeightLb": 44.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 22.0,
            "pack_weight_lb_sum": 44.0,
            "pack_count": 1,
            "Price": 220.0,
            "CostPrice": 132.0,
        },
    ]

    frame = pd.DataFrame(rows)
    parquet_path = tmp_path / "fact_customers_bundle_sections.parquet"
    frame.to_parquet(parquet_path)
    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    _CACHE.clear()
    customers_bundle.CROSS_SELL_CACHE.clear()
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    _CACHE.clear()
    customers_bundle.CROSS_SELL_CACHE.clear()
    fact_store.reset_duckdb_state()


def _scope_admin():
    return {
        "is_admin": True,
        "scope_mode": "all",
        "allowed_erp_user_ids": [],
        "sales_rep_ids": [],
        "allowed_count": 0,
        "scope_hash": "scope-admin-customers-sections",
        "permissions_version": "1",
        "user_id": 1,
        "role": "admin",
    }


def test_customers_overview_page_skips_non_overview_builders(app_client, seed_customers_bundle_sections, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMERS_KPIS_V3", True)
    monkeypatch.setattr(
        "app.services.customers_bundle._clv_payload",
        lambda *args, **kwargs: pytest.fail("CLV payload should not be built for the overview page"),
    )
    monkeypatch.setattr(
        "app.services.customers_bundle._parse_rfm_settings",
        lambda *args, **kwargs: pytest.fail("RFM settings should not be parsed for the overview page"),
    )

    original_execute = fact_store.execute_sql_df

    def wrapped_execute(sql, params=None, **kwargs):
        if kwargs.get("tag") == "customers.bundle.cohorts":
            pytest.fail("Cohort SQL should not run for the overview page")
        return original_execute(sql, params, **kwargs)

    monkeypatch.setattr(fact_store, "execute_sql_df", wrapped_execute)

    resp = app_client.get("/customers/", query_string={"start": "2025-03-01", "end": "2025-03-31"})
    assert resp.status_code == 200


def test_customers_bundle_sections_reduce_payload_and_report_section_metadata(app_client, seed_customers_bundle_sections, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    query = {"start": "2025-03-01", "end": "2025-03-31"}

    full_resp = app_client.get("/api/customers/bundle", query_string=query)
    assert full_resp.status_code == 200

    overview_resp = app_client.get("/api/customers/bundle", query_string={**query, "sections": "overview"})
    assert overview_resp.status_code == 200
    overview_payload = overview_resp.get_json() or {}
    overview_meta = overview_payload.get("meta") or {}

    assert overview_meta.get("sections") == ["overview"]
    assert overview_meta.get("rfm_params_hash") is None
    assert overview_meta.get("clv_params_hash") is None
    assert (((overview_payload.get("clv") or {}).get("customers_table") or {}).get("rows") or []) == []
    assert (((overview_payload.get("rfm") or {}).get("customers_table") or {}).get("rows") or []) == []
    assert len(overview_resp.get_data()) < len(full_resp.get_data())


def test_customers_bundle_cache_hit_matches_cached_metadata(app_client, seed_customers_bundle_sections, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    _CACHE.clear()
    query = {"start": "2025-03-01", "end": "2025-03-31", "sections": "overview"}

    first = app_client.get("/api/customers/bundle", query_string=query)
    second = app_client.get("/api/customers/bundle", query_string=query)

    first_meta = (first.get_json() or {}).get("meta") or {}
    second_meta = (second.get_json() or {}).get("meta") or {}

    assert first_meta.get("cached") is False
    assert first_meta.get("cache_hit") is False
    assert second_meta.get("cached") is True
    assert second_meta.get("cache_hit") is True


def test_customers_bundle_uses_effective_cost_margin_basis(app_client, seed_customers_bundle_sections, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())

    resp = app_client.get(
        "/api/customers/bundle",
        query_string={"start": "2025-03-01", "end": "2025-03-31", "sections": "overview"},
    )
    assert resp.status_code == 200

    payload = resp.get_json() or {}
    kpis = payload.get("kpis") or {}
    table = payload.get("table") or {}
    rows = table.get("rows") or []

    assert kpis.get("margin_pct") == pytest.approx(39.9648, abs=0.01)
    assert rows
    assert rows[0].get("cost") is not None
    assert rows[0].get("profit") is not None


def test_customers_drilldown_reports_actual_query_count(app_client, seed_customers_bundle_sections, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())

    resp = app_client.get(
        "/api/customers/drilldown/bundle",
        query_string={"customer_id": "C_RET", "start": "2025-03-01", "end": "2025-03-31"},
    )

    assert resp.status_code == 200
    meta = (resp.get_json() or {}).get("meta") or {}
    assert int(meta.get("duckdb_query_count") or 0) > 3
    assert int(meta.get("query_ms") or 0) >= 0


def test_customers_drilldown_bundle_includes_protein_intelligence(app_client, seed_customers_bundle_sections, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())

    resp = app_client.get(
        "/api/customers/drilldown/bundle",
        query_string={"customer_id": "C_RET", "start": "2025-03-01", "end": "2025-03-31"},
    )

    assert resp.status_code == 200
    payload = resp.get_json() or {}
    protein_intelligence = payload.get("protein_intelligence") or {}
    summary = protein_intelligence.get("summary") or {}
    assert summary.get("top_family") == "Seafood"
    assert summary.get("family_count") >= 1
    assert isinstance(protein_intelligence.get("mix"), list)


def test_validate_fact_schema_uses_column_metadata_instead_of_full_fact_read(monkeypatch):
    required_cols = {"Date", "Revenue", "Cost", "QuantityShipped", "WeightLb"}
    monkeypatch.setattr(fact_store, "list_columns", lambda conn=None: required_cols)
    monkeypatch.setattr(
        fact_store,
        "_read_fact",
        lambda *args, **kwargs: pytest.fail("validate_fact_schema should not read the full fact frame"),
    )

    status = fact_store.validate_fact_schema()

    assert status["ok"] is True
    assert status["missing"] == []
