from __future__ import annotations

import copy
import importlib.util
import io
import json
import re
import zipfile

import pandas as pd
import pytest

from app.services import fact_store


@pytest.fixture
def seed_customers_drilldown_v2(tmp_path, monkeypatch):
    rows: list[dict[str, object]] = []

    # In-scope customer with many products to validate full export parity (>25 rows).
    for idx in range(1, 36):
        day = (idx % 28) + 1
        product = f"Product {idx:02d}"
        qty = float(4 + (idx % 4))
        weight = float(8 + (idx % 5))
        protein = "Beef" if idx % 2 else "Pork"
        product_category = "Steak" if idx % 2 else "Roast"

        prior_revenue = float(90 + idx)
        current_revenue = float(120 + idx)
        prior_cost = round(prior_revenue * 0.62, 2)
        current_cost = round(current_revenue * 0.63, 2)

        rows.append(
            {
                "Date": f"2025-02-{day:02d}",
                "DateExpected": f"2025-02-{day:02d}",
                "OrderId": f"O-PRIOR-{idx:03d}",
                "CustomerId": "C_MAIN",
                "CustomerName": "Main Customer",
                "SalesRepId": "R1",
                "SalesRepName": "Rep One",
                "OrderStatus": "packed",
                "ProductName": product,
                "SKU": f"SKU-{idx:03d}",
                "Protein": protein,
                "ProteinType": None,
                "Category": None,
                "ProductCategory": product_category,
                "Revenue": prior_revenue,
                "Cost": prior_cost,
                "QuantityOrdered": qty,
                "WeightLb": weight,
                "UnitOfBillingId": 1,
                "pack_item_count_sum": qty,
                "pack_weight_lb_sum": weight,
                "pack_count": 1,
                "Price": prior_revenue,
                "CostPrice": prior_cost,
            }
        )
        rows.append(
            {
                "Date": f"2025-03-{day:02d}",
                "DateExpected": f"2025-03-{day:02d}",
                "OrderId": f"O-CUR-{idx:03d}",
                "CustomerId": "C_MAIN",
                "CustomerName": "Main Customer",
                "SalesRepId": "R1",
                "SalesRepName": "Rep One",
                "OrderStatus": "packed",
                "ProductName": product,
                "SKU": f"SKU-{idx:03d}",
                "Protein": protein,
                "ProteinType": None,
                "Category": None,
                "ProductCategory": product_category,
                "Revenue": current_revenue,
                "Cost": current_cost,
                "QuantityOrdered": qty + 1.0,
                "WeightLb": weight + 1.0,
                "UnitOfBillingId": 1,
                "pack_item_count_sum": qty + 1.0,
                "pack_weight_lb_sum": weight + 1.0,
                "pack_count": 1,
                "Price": current_revenue,
                "CostPrice": current_cost,
            }
        )

    # Out-of-scope customer for RBAC checks.
    for idx in range(1, 4):
        rows.append(
            {
                "Date": f"2025-03-{10 + idx:02d}",
                "DateExpected": f"2025-03-{10 + idx:02d}",
                "OrderId": f"O-OTHER-{idx:03d}",
                "CustomerId": "C_OTHER",
                "CustomerName": "Other Scope Customer",
                "SalesRepId": "R2",
                "SalesRepName": "Rep Two",
                "OrderStatus": "packed",
                "ProductName": f"Other Product {idx}",
                "SKU": f"OSKU-{idx:03d}",
                "Protein": "Chicken",
                "ProteinType": None,
                "Category": None,
                "ProductCategory": "Poultry",
                "Revenue": float(100 + (idx * 10)),
                "Cost": float(65 + (idx * 6)),
                "QuantityOrdered": float(5 + idx),
                "WeightLb": float(11 + idx),
                "UnitOfBillingId": 1,
                "pack_item_count_sum": float(5 + idx),
                "pack_weight_lb_sum": float(11 + idx),
                "pack_count": 1,
                "Price": float(100 + (idx * 10)),
                "CostPrice": float(65 + (idx * 6)),
            }
        )

    rows.append(
        {
            "Date": "2025-04-01",
            "DateExpected": "2025-04-01",
            "OrderId": "O-LAST-SELL-001",
            "CustomerId": "C_MAIN",
            "CustomerName": "Main Customer",
            "SalesRepId": "R2",
            "SalesRepName": "Rep Two",
            "OrderStatus": "packed",
            "ProductName": "Product 01",
            "SKU": "SKU-001",
            "Protein": "Beef",
            "ProteinType": None,
            "Category": None,
            "ProductCategory": "Steak",
            "Revenue": 130.0,
            "Cost": 81.9,
            "QuantityOrdered": 6.0,
            "WeightLb": 10.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 6.0,
            "pack_weight_lb_sum": 10.0,
            "pack_count": 1,
            "Price": 130.0,
            "CostPrice": 81.9,
        }
    )

    rows.append(
        {
            "Date": "2025-03-05",
            "DateExpected": "2025-03-05",
            "OrderId": "O-SPARSE-001",
            "CustomerId": "C_SPARSE",
            "CustomerName": "Sparse Customer",
            "SalesRepId": "R1",
            "SalesRepName": "Rep One",
            "OrderStatus": "packed",
            "ProductName": "Sparse Product",
            "SKU": "SKU-SPARSE-001",
            "Protein": "Beef",
            "ProteinType": None,
            "Category": None,
            "ProductCategory": "Trim",
            "Revenue": 75.0,
            "Cost": 48.0,
            "QuantityOrdered": 1.0,
            "WeightLb": 0.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 1.0,
            "pack_weight_lb_sum": 0.0,
            "pack_count": 1,
            "Price": 75.0,
            "CostPrice": 48.0,
        }
    )

    frame = pd.DataFrame(rows)
    parquet_path = tmp_path / "fact_customers_drilldown_v2.parquet"
    frame.to_parquet(parquet_path)
    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def _scope_sales(rep_ids: list[str]):
    return {
        "is_admin": False,
        "scope_mode": "list",
        "allowed_erp_user_ids": rep_ids,
        "sales_rep_ids": rep_ids,
        "allowed_count": len(rep_ids),
        "scope_hash": "scope-sales-drilldown-v2",
        "permissions_version": "1",
        "user_id": 901,
        "role": "sales",
    }


def _scope_admin():
    return {
        "is_admin": True,
        "scope_mode": "all",
        "allowed_erp_user_ids": [],
        "sales_rep_ids": [],
        "allowed_count": 0,
        "scope_hash": "scope-admin-drilldown-v2",
        "permissions_version": "1",
        "user_id": 1,
        "role": "admin",
    }


def _csv_frame(resp) -> pd.DataFrame:
    return pd.read_csv(io.StringIO(resp.get_data(as_text=True)))


def _xlsx_sheet_names(resp) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(resp.get_data())) as workbook:
        workbook_xml = workbook.read("xl/workbook.xml").decode("utf-8", errors="ignore")
    return set(re.findall(r'<sheet[^>]+name="([^"]+)"', workbook_xml))


def _base_query():
    return {
        "customer_id": "C_MAIN",
        "start": "2025-03-01",
        "end": "2025-03-31",
    }


def test_customers_drilldown_v2_monthly_bins_are_yyyy_mm_and_sorted(app_client, seed_customers_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMER_DRILLDOWN_V2", True)

    query = {"customer_id": "C_MAIN", "start": "2025-02-01", "end": "2025-03-31", "drilldown_v2": "1"}
    resp = app_client.get("/api/customers/drilldown/bundle", query_string=query)
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    labels = (payload.get("trend") or {}).get("labels") or []

    assert labels
    assert labels == sorted(labels)
    assert all(re.match(r"^\d{4}-\d{2}$", str(label)) for label in labels)
    assert labels[:2] == ["2025-02", "2025-03"]


def test_customers_drilldown_v2_keeps_protein_and_category_dimensions_distinct(app_client, seed_customers_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMER_DRILLDOWN_V2", True)

    resp = app_client.get("/api/customers/drilldown/bundle", query_string={"customer_id": "C_MAIN", "start": "2025-03-01", "end": "2025-03-31"})
    assert resp.status_code == 200
    payload = resp.get_json() or {}

    protein_mix = ((payload.get("protein_intelligence") or {}).get("mix") or [])
    category_rows = payload.get("categories") or []

    assert protein_mix
    assert {row.get("family") for row in protein_mix[:4]} <= {"Beef", "Pork"}
    assert category_rows
    assert {row.get("category") for row in category_rows[:4]} <= {"Steak", "Roast"}
    assert ((payload.get("protein_intelligence") or {}).get("summary") or {}).get("top_family") in {"Beef", "Pork"}


def test_customers_drilldown_v2_snapshot_export_contains_expected_sheets(app_client, seed_customers_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMER_DRILLDOWN_V2", True)

    resp = app_client.get(
        "/customers/export",
        query_string={**_base_query(), "page": "drilldown", "dataset": "snapshot", "format": "xlsx"},
    )
    assert resp.status_code == 200

    content_type = str(resp.headers.get("Content-Type") or "").lower()
    if resp.headers.get("X-Export-Fallback") == "csv" or "text/csv" in content_type:
        fallback_df = _csv_frame(resp)
        assert {"metric", "value"}.issubset(set(fallback_df.columns))
        assert len(fallback_df.index) > 0
    else:
        expected_sheets = {
            "Hero",
            "HeroBadges",
            "SummaryKPIs",
            "ExecutiveScorecard",
            "PriorityEngine",
            "LifecycleRetention",
            "MonthlyTrends",
            "WeightOperational",
            "WeightTopProducts",
            "ProductProfitability",
            "CategoryMix",
            "TopProductsSpend",
            "TopProductsWeight",
            "Orders",
            "CrossSell",
            "PriceIntelligence",
            "CRMActionWorkspace",
            "TrustCoverage",
            "Seasonality",
            "Cadence",
            "Metadata",
        }
        if importlib.util.find_spec("openpyxl") is None:
            assert expected_sheets.issubset(_xlsx_sheet_names(resp))
        else:
            book = pd.ExcelFile(io.BytesIO(resp.get_data()))
            assert expected_sheets.issubset(set(book.sheet_names))

            product_df = pd.read_excel(book, sheet_name="ProductProfitability")
            assert len(product_df.index) == 35


def test_customers_drilldown_v2_tracks_latest_visible_seller_and_historical_owner(app_client, seed_customers_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMER_DRILLDOWN_V2", True)

    admin_resp = app_client.get("/api/customers/drilldown/bundle", query_string=_base_query())
    assert admin_resp.status_code == 200
    admin_payload = admin_resp.get_json() or {}

    admin_hero = admin_payload.get("hero") or {}
    admin_kpis = admin_payload.get("kpis") or {}
    admin_trust = admin_payload.get("trust_coverage") or {}

    assert admin_hero.get("owner") == "Rep One"
    assert admin_hero.get("last_sales_rep") == "Rep Two"
    assert admin_hero.get("last_sales_rep_date") == "2025-04-01"
    assert admin_hero.get("historical_owner") is None
    assert admin_kpis.get("historical_owner_sales_rep") is None
    assert admin_kpis.get("last_sales_rep") == "Rep Two"
    assert admin_trust.get("owner_source") == "Dominant visible seller"
    assert "Rep One" in str(admin_hero.get("owner_detail") or "")

    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_sales(["r1"]))
    scoped_resp = app_client.get("/api/customers/drilldown/bundle", query_string=_base_query())
    assert scoped_resp.status_code == 200
    scoped_payload = scoped_resp.get_json() or {}
    scoped_hero = scoped_payload.get("hero") or {}

    assert scoped_hero.get("owner") == "Rep One"
    assert scoped_hero.get("last_sales_rep") == "Rep One"
    assert scoped_hero.get("historical_owner") is None


def test_customers_drilldown_v2_rbac_scope_bundle_and_export(app_client, seed_customers_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_sales(["r1"]))
    monkeypatch.setitem(app_client.application.config, "CUSTOMER_DRILLDOWN_V2", True)

    allowed_resp = app_client.get("/api/customers/drilldown/bundle", query_string={"customer_id": "C_MAIN", "start": "2025-03-01", "end": "2025-03-31"})
    assert allowed_resp.status_code == 200
    allowed_payload = allowed_resp.get_json() or {}
    assert int(((allowed_payload.get("table") or {}).get("total_rows") or 0) > 0)

    blocked_resp = app_client.get("/api/customers/drilldown/bundle", query_string={"customer_id": "C_OTHER", "start": "2025-03-01", "end": "2025-03-31"})
    assert blocked_resp.status_code == 200
    blocked_payload = blocked_resp.get_json() or {}
    assert int(((blocked_payload.get("table") or {}).get("total_rows") or 0)) == 0
    blocked_kpis = blocked_payload.get("kpis") or {}
    assert float(blocked_kpis.get("total_revenue") or 0.0) == 0.0
    assert int(blocked_kpis.get("total_orders") or 0) == 0

    export_resp = app_client.get(
        "/customers/export",
        query_string={
            "page": "drilldown",
            "customer_id": "C_MAIN",
            "dataset": "product_profitability",
            "format": "csv",
            "start": "2025-03-01",
            "end": "2025-03-31",
        },
    )
    assert export_resp.status_code == 200
    export_df = _csv_frame(export_resp)
    assert len(export_df.index) == 35


def test_customers_drilldown_v2_product_export_not_truncated_and_matches_table(app_client, seed_customers_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMER_DRILLDOWN_V2", True)

    bundle_resp = app_client.get("/api/customers/drilldown/bundle", query_string={**_base_query(), "drilldown_v2": "1"})
    assert bundle_resp.status_code == 200
    payload = bundle_resp.get_json() or {}
    table_total = int(((payload.get("table") or {}).get("total_rows") or 0))
    assert table_total >= 35

    export_resp = app_client.get(
        "/customers/export",
        query_string={**_base_query(), "page": "drilldown", "dataset": "product_profitability", "format": "csv"},
    )
    assert export_resp.status_code == 200
    export_df = _csv_frame(export_resp)

    assert len(export_df.index) == 35
    assert len(export_df.index) == table_total
    assert len(export_df.index) > 25


def test_customers_drilldown_v2_orders_monthly_and_crm_action_exports_respect_customer_scope(app_client, seed_customers_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMER_DRILLDOWN_V2", True)

    orders_resp = app_client.get(
        "/customers/export",
        query_string={**_base_query(), "page": "drilldown", "dataset": "orders", "format": "csv"},
    )
    assert orders_resp.status_code == 200
    orders_df = _csv_frame(orders_resp)
    assert len(orders_df.index) == 35
    order_dates = pd.to_datetime(orders_df["order_date"])
    assert order_dates.min() >= pd.Timestamp("2025-03-01")
    assert order_dates.max() < pd.Timestamp("2025-04-01")

    monthly_resp = app_client.get(
        "/customers/export",
        query_string={**_base_query(), "page": "drilldown", "dataset": "monthly", "format": "xlsx"},
    )
    assert monthly_resp.status_code == 200
    assert {"MonthlyTrends", "Metadata"}.issubset(_xlsx_sheet_names(monthly_resp))

    actions_resp = app_client.get(
        "/customers/export",
        query_string={**_base_query(), "page": "drilldown", "dataset": "crm_actions", "format": "xlsx"},
    )
    assert actions_resp.status_code == 200
    assert {"CRMActionWorkspace", "Metadata"}.issubset(_xlsx_sheet_names(actions_resp))


def test_customers_drilldown_v2_exports_preserve_local_drill_state(app_client, seed_customers_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMER_DRILLDOWN_V2", True)

    bundle_resp = app_client.get("/api/customers/drilldown/bundle", query_string=_base_query())
    assert bundle_resp.status_code == 200
    crm_workspace = (bundle_resp.get_json() or {}).get("crm_workspace") or {}
    selected_lane = next((lane for lane, rows in crm_workspace.items() if rows), "protect_now")

    products_resp = app_client.get(
        "/customers/export",
        query_string={
            **_base_query(),
            "page": "drilldown",
            "dataset": "product_profitability",
            "format": "csv",
            "protein_focus": "Beef",
        },
    )
    assert products_resp.status_code == 200
    products_df = _csv_frame(products_resp)
    assert len(products_df.index) == 18
    assert set(products_df["protein_family"].astype(str).str.lower()) == {"beef"}

    actions_resp = app_client.get(
        "/customers/export",
        query_string={
            **_base_query(),
            "page": "drilldown",
            "dataset": "crm_actions",
            "format": "csv",
            "action_lane": selected_lane,
        },
    )
    assert actions_resp.status_code == 200
    actions_df = _csv_frame(actions_resp)
    assert {"lane", "title", "owner"}.issubset(set(actions_df.columns))
    if len(actions_df.index) > 0:
        assert set(actions_df["lane"].astype(str).str.lower()) == {selected_lane}


def test_customers_drilldown_v2_basket_denominator_matches_window_orders(app_client, seed_customers_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMER_DRILLDOWN_V2", True)

    resp = app_client.get("/api/customers/drilldown/bundle", query_string={"customer_id": "C_MAIN", "start": "2025-03-01", "end": "2025-03-31"})
    assert resp.status_code == 200
    payload = resp.get_json() or {}
    basket = payload.get("basket") or {}
    assert int(basket.get("orders") or 0) == 35
    assert int(basket.get("orders_lifetime") or 0) == 71


def test_customers_drilldown_v2_exposes_crm_workspace_and_weight_metrics(app_client, seed_customers_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMER_DRILLDOWN_V2", True)

    resp = app_client.get("/api/customers/drilldown/bundle", query_string={**_base_query(), "drilldown_v2": "1"})
    assert resp.status_code == 200
    payload = resp.get_json() or {}

    hero = payload.get("hero") or {}
    assert hero.get("customer_name") == "Main Customer"
    assert hero.get("customer_id") == "C_MAIN"
    assert len(hero.get("badges") or []) > 0

    scorecard = payload.get("executive_scorecard") or []
    assert any((group or {}).get("title") == "Weight & Operational Value" for group in scorecard)
    assert any(
        (metric or {}).get("scope") == "Current filter window"
        for group in scorecard
        for metric in ((group or {}).get("metrics") or [])
    )

    weight_summary = ((payload.get("weight_analytics") or {}).get("summary") or {})
    assert float(weight_summary.get("total_weight_lb_window") or 0.0) == pytest.approx(385.0)
    assert float(weight_summary.get("avg_lb_per_order") or 0.0) == pytest.approx(11.0)

    crm_workspace = payload.get("crm_workspace") or {}
    assert {"protect_now", "grow_now", "recover_now", "monitor"}.issubset(set(crm_workspace.keys()))


def test_customers_drilldown_v2_window_revenue_respects_active_filter_window(app_client, seed_customers_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMER_DRILLDOWN_V2", True)

    full_resp = app_client.get("/api/customers/drilldown/bundle", query_string={"customer_id": "C_MAIN", "start": "2025-03-01", "end": "2025-03-31"})
    assert full_resp.status_code == 200
    full_payload = full_resp.get_json() or {}

    narrow_resp = app_client.get("/api/customers/drilldown/bundle", query_string={"customer_id": "C_MAIN", "start": "2025-03-01", "end": "2025-03-10"})
    assert narrow_resp.status_code == 200
    narrow_payload = narrow_resp.get_json() or {}

    full_revenue = float((full_payload.get("kpis") or {}).get("revenue_window") or 0.0)
    narrow_revenue = float((narrow_payload.get("kpis") or {}).get("revenue_window") or 0.0)

    assert full_revenue > 0.0
    assert narrow_revenue > 0.0
    assert narrow_revenue < full_revenue
    assert float((narrow_payload.get("kpis") or {}).get("orders_window") or 0.0) < float((full_payload.get("kpis") or {}).get("orders_window") or 0.0)


def test_customers_drilldown_v2_rendered_page_scopes_window_metrics_and_exports(app_client, seed_customers_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMER_DRILLDOWN_V2", True)

    bundle_resp = app_client.get("/api/customers/drilldown/bundle", query_string={"customer_id": "C_MAIN", "start": "2025-03-01", "end": "2025-03-31"})
    assert bundle_resp.status_code == 200
    bundle_payload = bundle_resp.get_json() or {}
    expected_window_revenue = float((bundle_payload.get("kpis") or {}).get("revenue_window") or 0.0)

    resp = app_client.get("/customers/drilldown/C_MAIN", query_string={"start": "2025-03-01", "end": "2025-03-31"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    assert "Visible lifetime" in body
    assert "Current filter window" in body
    assert "Coverage, Trust, &amp; Governance" in body or "Coverage, Trust, & Governance" in body
    assert "Inferred from dominant visible seller across visible history: Rep One." in body
    assert "Latest visible sale 2025-04-01" in body
    assert f"${expected_window_revenue:,.0f}" in body or f"${expected_window_revenue:,.2f}" in body
    assert "dataset=snapshot" in body
    assert "dataset=crm_actions" in body
    assert "start=2025-03-01" in body
    assert "end=2025-03-31" in body
    assert "data-export-link" in body


def test_customers_drilldown_v2_moves_shared_scripts_below_hero_and_defers_page_js(app_client, seed_customers_drilldown_v2, monkeypatch):
    class _DummyUser:
        is_authenticated = True
        is_active = True
        is_anonymous = False
        role = "admin"

        def get_id(self):
            return "admin"

    monkeypatch.setattr("flask_login.utils._get_user", lambda *a, **k: _DummyUser())
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMER_DRILLDOWN_V2", True)

    resp = app_client.get("/customers/drilldown/C_MAIN", query_string={"start": "2025-03-01", "end": "2025-03-31"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)

    hero_idx = body.index('class="ciw-title"')
    assert body.index("js/auth-fetch.js") > hero_idx
    assert body.index("https://cdn.plot.ly/plotly-2.30.0.min.js") > hero_idx
    assert re.search(r'customer_drilldown_v2\.js(?:\?[^"]+)?"\s+defer', body)


def test_customers_drilldown_v2_sparse_customer_exposes_chart_fallback_states(app_client, seed_customers_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMER_DRILLDOWN_V2", True)

    bundle_resp = app_client.get("/api/customers/drilldown/bundle", query_string={"customer_id": "C_SPARSE", "start": "2025-03-01", "end": "2025-03-31"})
    assert bundle_resp.status_code == 200
    bundle_payload = bundle_resp.get_json() or {}
    chart_states = bundle_payload.get("chart_states") or {}

    assert (chart_states.get("trend") or {}).get("status") == "limited"
    assert (chart_states.get("weight_value") or {}).get("status") == "empty"
    assert (chart_states.get("seasonality") or {}).get("status") == "empty"
    assert "No weight-bearing rows" in str((chart_states.get("weight_value") or {}).get("reason") or "")
    assert "Not enough history" in str((chart_states.get("seasonality") or {}).get("reason") or "")

    resp = app_client.get("/customers/drilldown/C_SPARSE", query_string={"start": "2025-03-01", "end": "2025-03-31"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Sparse Customer" in body
    assert "No weight-bearing rows are available in the current filter window." in body
    assert "Not enough history to render seasonality reliably." in body


def test_customers_drilldown_v2_chart_payload_is_json_safe(app_client, seed_customers_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())
    monkeypatch.setitem(app_client.application.config, "CUSTOMER_DRILLDOWN_V2", True)

    import app.blueprints.customers as customers_bp

    original_drilldown = customers_bp.bundle_service.drilldown

    def _patched_drilldown(page, args):
        payload = copy.deepcopy(original_drilldown(page, args))
        trend = payload.setdefault("trend", {})
        trend["margin_pct"] = [float("nan")]
        table = payload.setdefault("table", {})
        rows = list(table.get("rows") or [])
        if rows:
            rows[0]["weight_lb"] = float("nan")
        table["rows"] = rows
        payload["chart_states"] = {
            **(payload.get("chart_states") or {}),
            "trend": {"status": "ready"},
            "top_mix": {"status": "ready"},
        }
        return payload

    monkeypatch.setattr(customers_bp.bundle_service, "drilldown", _patched_drilldown)

    resp = app_client.get("/customers/drilldown/C_MAIN", query_string={"start": "2025-03-01", "end": "2025-03-31"})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "NaN" not in body
    assert "Infinity" not in body

    match = re.search(
        r'<script id="customerWorkspaceData" type="application/json">\s*(\{.*?\})\s*</script>',
        body,
        re.S,
    )
    assert match is not None
    payload = json.loads(match.group(1))
    assert payload["trend"]["margin_pct"][0] is None
    assert payload["topMixRows"][0]["weight_lb"] is None


def test_customers_drilldown_v2_template_flag_on_and_off(app_client, seed_customers_drilldown_v2, monkeypatch):
    monkeypatch.setattr("app.services.filters_service.scope_from_user", lambda _u: _scope_admin())

    monkeypatch.setitem(app_client.application.config, "CUSTOMER_DRILLDOWN_V2", True)
    v2_resp = app_client.get("/customers/drilldown/C_MAIN", query_string={"start": "2025-03-01", "end": "2025-03-31"})
    assert v2_resp.status_code == 200
    v2_body = v2_resp.get_data(as_text=True)
    assert "Customer Intelligence Workspace" in v2_body
    assert "Next Best Actions" in v2_body
    assert "CRM Action Workspace" in v2_body

    monkeypatch.setitem(app_client.application.config, "CUSTOMER_DRILLDOWN_V2", False)
    v1_resp = app_client.get("/customers/drilldown/C_MAIN", query_string={"start": "2025-03-01", "end": "2025-03-31"})
    assert v1_resp.status_code == 200
    v1_body = v1_resp.get_data(as_text=True)
    assert "Customer Intelligence Workspace" not in v1_body
    assert "Opportunity highlights" in v1_body
