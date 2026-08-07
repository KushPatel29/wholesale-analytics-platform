from __future__ import annotations

import io
import shutil
import uuid
from pathlib import Path

import pandas as pd
import pytest
from werkzeug.exceptions import Forbidden

import app.blueprints.products as products_bp
from app.services import fact_store
from app.services import product_drilldown_service
from app.services import presentation


@pytest.fixture
def product_drilldown_v2_client(app, monkeypatch, tmp_path):
    products_bp._STORE_SINGLETON = None
    tmp_dir = tmp_path / f"product_drilldown_v2_{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    end_month = pd.Timestamp.utcnow().normalize().replace(day=1)
    month_starts = pd.date_range(end=end_month, periods=22, freq="MS")
    rows = []
    target_sku = "SKU-001"
    customer_ids = [f"CUST-{idx:02d}" for idx in range(1, 13)]
    for idx, month in enumerate(month_starts):
        for customer in customer_ids:
            order_id = f"ORD-{idx:02d}-{customer}"
            revenue = 220.0 + idx * 12 + (hash(customer) % 30)
            units = 8.0 + (idx % 5)
            weight = units * 2.4
            cost = revenue * 0.68 if idx % 4 != 0 else None
            rows.append(
                {
                    "DateExpected": month,
                    "Date": month,
                    "SKU": target_sku,
                    "ProductID": target_sku,
                    "ProductName": "Prime Ribeye",
                    "Protein": "Beef",
                    "ProteinType": None,
                    "Category": None,
                    "ProductCategory": "Steak",
                    "CustomerID": customer,
                    "CustomerName": f"Customer {customer}",
                    "OrderID": order_id,
                    "Region": "West" if idx % 2 == 0 else "East",
                    "SupplierName": "North Foods" if idx % 3 == 0 else "Valley Supply",
                    "ShippingMethodName": "Delivery" if idx % 2 == 0 else "Pickup",
                    "Revenue": revenue,
                    "Cost": cost,
                    "ShippedItems": units,
                    "WeightLb": weight,
                }
            )
            rows.append(
                {
                    "DateExpected": month,
                    "Date": month,
                    "SKU": "SKU-CO1",
                    "ProductID": "SKU-CO1",
                    "ProductName": "Linked Side",
                    "Protein": "Prepared",
                    "ProteinType": None,
                    "Category": None,
                    "ProductCategory": "Sides",
                    "CustomerID": customer,
                    "CustomerName": f"Customer {customer}",
                    "OrderID": order_id,
                    "Region": "West" if idx % 2 == 0 else "East",
                    "SupplierName": "North Foods",
                    "ShippingMethodName": "Delivery" if idx % 2 == 0 else "Pickup",
                    "Revenue": 60.0 + idx,
                    "Cost": 35.0 + (idx * 0.5),
                    "ShippedItems": 2.0,
                    "WeightLb": 3.0,
                }
            )

    df = pd.DataFrame(rows)
    parquet_path = tmp_dir / "products_drilldown_v2.parquet"
    df.to_parquet(parquet_path)

    monkeypatch.setenv("PRODUCTS_SALES_PARQUET", str(parquet_path))
    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    app.config["PRODUCTS_SALES_PARQUET"] = str(parquet_path)
    app.config["PARQUET_PATH"] = str(parquet_path)
    app.config["DATA_DIR"] = str(parquet_path.parent)
    app.config["LOGIN_DISABLED"] = True
    app.config["AUTHZ_DISABLED"] = True
    app.config["PRODUCT_DRILLDOWN_V2"] = True
    app.config["PRODUCT_FORECAST_V1"] = True
    app.config["FEATURE_FORECAST_ENABLED"] = False
    fact_store.reset_duckdb_state()
    fact_store.init_views()

    expected_customers = int(df[df["SKU"] == target_sku]["CustomerID"].nunique())

    with app.test_client() as client:
        yield client, expected_customers

    products_bp._STORE_SINGLETON = None
    fact_store.reset_duckdb_state()
    shutil.rmtree(tmp_dir, ignore_errors=True)


def test_product_drilldown_v2_renders(product_drilldown_v2_client):
    client, _expected_customers = product_drilldown_v2_client
    response = client.get("/products/SKU-001/drilldown")
    assert response.status_code == 200
    assert b"Product Intelligence Workspace" in response.data
    assert b"SKU-001 \xe2\x80\x94 Prime Ribeye" in response.data
    assert b"Weight Analytics" in response.data
    assert b"ASP/lb Distribution" in response.data


def test_product_drilldown_v2_interactive_workspace_markers(product_drilldown_v2_client):
    client, _expected_customers = product_drilldown_v2_client
    response = client.get("/products/SKU-001/drilldown")
    assert response.status_code == 200
    assert b"v2IntelPanelWrap" in response.data
    assert b"js-v2-action-trigger" in response.data
    assert b"js-v2-customer-row" in response.data
    assert b"js-v2-basket-row" in response.data
    assert b"js-v2-mover-trigger" in response.data
    assert b"Rolling 28-day diagnostics compare" in response.data


def test_product_drilldown_v2_exit_button_preserves_request_state(product_drilldown_v2_client):
    client, _expected_customers = product_drilldown_v2_client
    response = client.get("/products/SKU-001/drilldown?page=3&search=ribeye&quick_filters=below_target_margin")
    assert response.status_code == 200
    assert b"productDrilldownBackButton" in response.data
    assert b"data-back-fallback" in response.data
    assert b"page=3&amp;search=ribeye&amp;quick_filters=below_target_margin" in response.data


def test_product_drilldown_v2_long_name_hero_markup(product_drilldown_v2_client, monkeypatch):
    client, _expected_customers = product_drilldown_v2_client
    original = product_drilldown_service.build_product_drilldown_context
    long_name = "Prime Ribeye Boneless Export Program Reserve Cut With Extended Seasonal Planning Description"

    def _patched(product_id, filters, current_user_obj):
        context = original(product_id, filters, current_user_obj)
        context["meta"]["product_name"] = long_name
        context["meta"]["product_display_label"] = f"SKU-001 — {long_name}"
        return context

    monkeypatch.setattr(product_drilldown_service, "build_product_drilldown_context", _patched)
    response = client.get("/products/SKU-001/drilldown")
    assert response.status_code == 200
    assert long_name.encode() in response.data
    assert b"product-v2-hero-title" in response.data
    assert b"product-v2-hero-display-label" in response.data


def test_product_drilldown_v2_export_customers_full_rows(product_drilldown_v2_client):
    client, expected_customers = product_drilldown_v2_client
    response = client.get("/products/SKU-001/drilldown/export?kind=customers&format=csv")
    assert response.status_code == 200
    exported = pd.read_csv(io.BytesIO(response.data))
    assert len(exported.index) == expected_customers


def test_product_drilldown_v2_export_month_labels_clean(product_drilldown_v2_client):
    client, _expected_customers = product_drilldown_v2_client
    response = client.get("/products/SKU-001/drilldown/export?kind=monthly_series&format=csv")
    assert response.status_code == 200
    exported = pd.read_csv(io.BytesIO(response.data))
    assert "month" in exported.columns
    assert "weight_lb" in exported.columns
    assert "asp_lb" in exported.columns
    assert "profit_per_lb" in exported.columns
    month_values = exported["month"].astype(str).tolist()
    assert month_values == sorted(month_values)
    assert all(":" not in value for value in month_values)
    assert all(len(value) == 7 and value[4] == "-" for value in month_values)


def test_product_drilldown_v2_context_weight_and_pricing_metrics(product_drilldown_v2_client):
    client, _expected_customers = product_drilldown_v2_client
    with client.application.app_context():
        context = product_drilldown_service.build_product_drilldown_context("SKU-001", filters={}, current_user_obj=object())
    assert context["meta"]["product_display_label"] == "SKU-001 — Prime Ribeye"
    assert context["distributions"]["price"]["label"] == "ASP/lb"
    assert context["price_volume"]["price_label"] == "ASP/lb"
    assert context["kpis"]["revenue_per_lb"] == context["kpis"]["asp_lb"]
    assert context["weight_analytics"]["summary"]["avg_weight_per_order"] is not None
    assert context["lifecycle_insights"]["stage"] in {"New", "Growth", "Mature", "Declining", "Reactivated", "Unstable"}


def test_product_drilldown_v2_includes_family_context(product_drilldown_v2_client):
    client, _expected_customers = product_drilldown_v2_client
    with client.application.app_context():
        context = product_drilldown_service.build_product_drilldown_context("SKU-001", filters={}, current_user_obj=object())

    family_context = context.get("family_context") or {}
    assert family_context.get("protein_family") == "Beef"
    assert family_context.get("product_category") == "Steak"
    assert family_context.get("target_margin_pct") == pytest.approx(26.0, abs=0.01)
    assert family_context.get("minimum_margin_pct") == pytest.approx(17.0, abs=0.01)
    assert family_context.get("target_price_lb") is not None
    assert family_context.get("minimum_price_lb") is not None
    assert (context.get("margin_profile") or {}).get("target_status")


def test_product_drilldown_v2_kpi_profit_matches_monthly_profit_rollup(product_drilldown_v2_client):
    client, _expected_customers = product_drilldown_v2_client
    with client.application.app_context():
        context = product_drilldown_service.build_product_drilldown_context("SKU-001", filters={}, current_user_obj=object())
    monthly_profit = sum(float(row.get("profit") or 0.0) for row in (context["time_series"].get("monthly") or []))
    assert context["kpis"]["gross_margin_value"] == pytest.approx(monthly_profit)


def test_product_drilldown_v2_basket_labels_and_confidence(product_drilldown_v2_client):
    client, _expected_customers = product_drilldown_v2_client
    with client.application.app_context():
        context = product_drilldown_service.build_product_drilldown_context("SKU-001", filters={}, current_user_obj=object())
    basket_rows = context["basket"]["rows"]
    assert basket_rows
    assert basket_rows[0]["display_name"].startswith("SKU-CO1 — Linked Side")
    assert basket_rows[0]["display_name_axis"].startswith("SKU-CO1")
    assert basket_rows[0]["confidence"] is not None


def test_product_drilldown_v2_forecast_includes_weight_series(product_drilldown_v2_client):
    client, _expected_customers = product_drilldown_v2_client
    response = client.get("/api/products/SKU-001/forecast?freq=month")
    assert response.status_code == 200
    payload = response.get_json() or {}
    actual = payload.get("actual_series") or []
    forecast_rows = payload.get("forecast_series") or []
    assert actual and "weight_lb" in actual[0]
    assert forecast_rows and "weight_yhat" in forecast_rows[0]
    assert (payload.get("meta") or {}).get("forecastability_score") is not None


def test_product_drilldown_v2_product_label_helper():
    assert presentation.format_product_label("SKU-001", "Prime Ribeye") == "SKU-001 — Prime Ribeye"


def test_product_drilldown_v2_redaction_keeps_asp_lb_but_hides_margin_signals():
    context = {
        "kpis": {"asp_lb": 5.0, "profit_per_lb": 1.2, "margin_pct": 24.0, "data_confidence_score": 82.0, "delta_28d": {"margin_delta_pp": 2.0}},
        "quality": {"cost_coverage_pct": 97.0},
        "trend": {"months": ["2025-01"], "profit": [10.0], "margin_pct": [20.0], "profit_per_lb": [0.5]},
        "distributions": {"margin": {"samples": [10.0]}, "profit_per_lb": {"samples": [0.5]}},
        "margin_risk": {"summary": {"uplift_to_target": 100.0, "revenue_exposure_pct": 22.0}},
        "risk_opportunity": {
            "primary_risk": {"title": "Margin recovery needed", "detail": "20% of revenue sits below target margin."},
            "primary_opportunity": {"title": "Pricing recovery", "detail": "Recover margin on one customer."},
            "risks": [{"title": "Margin recovery needed", "detail": "Below target margin."}, {"title": "Seasonality risk", "detail": "Demand swings."}],
            "opportunities": [{"title": "Pricing recovery", "detail": "Recover margin."}, {"title": "Basket expansion", "detail": "Lift 2.0."}],
        },
        "header_summary": {"primary_risk": "Margin recovery needed", "primary_opportunity": "Pricing recovery"},
        "decision_panel": {"actions": [{"title": "Address the primary risk", "detail": "Margin recovery needed."}, {"title": "Use basket data", "detail": "Bundle with SKU-CO1."}]},
    }
    redacted = products_bp._redact_product_drilldown_cost_fields(context)
    assert redacted["kpis"]["asp_lb"] == 5.0
    assert redacted["kpis"]["profit_per_lb"] is None
    assert redacted["quality"]["cost_coverage_pct"] is None
    assert all("margin" not in (row.get("title") or "").lower() for row in redacted["risk_opportunity"]["risks"])
    assert all("pricing recovery" not in (row.get("title") or "").lower() for row in redacted["risk_opportunity"]["opportunities"])


def test_product_drilldown_v2_basket_insufficient_sample_guard(monkeypatch):
    monkeypatch.setattr(product_drilldown_service.filters_service, "scope_from_user", lambda _user: {})
    monkeypatch.setattr(
        product_drilldown_service.fact_store,
        "list_columns",
        lambda: {
            "DateExpected",
            "Date",
            "OrderID",
            "Revenue",
            "SKU",
            "ProductID",
            "ProductName",
        },
    )
    monkeypatch.setattr(
        product_drilldown_service.fact_store,
        "build_where_clause",
        lambda *_args, **_kwargs: ("1=1", [], "2024-01-01", "2024-12-31"),
    )
    monkeypatch.setattr(
        product_drilldown_service.fact_store,
        "execute_sql_df",
        lambda *_args, **_kwargs: pd.DataFrame(
            [
                {
                    "product_id": "SKU-CO1",
                    "sku": "SKU-CO1",
                    "product_name": "Linked Side",
                    "display_name": "SKU-CO1 Linked Side",
                    "co_orders": 3,
                    "paired_revenue": 90.0,
                    "orders_with_other": 40,
                    "base_orders": 5,
                    "total_orders": 100,
                }
            ]
        ),
    )
    payload = product_drilldown_service.build_basket_affinity("SKU-001", filters={}, current_user_obj=object())
    assert payload.get("insufficient_sample") is True
    assert payload.get("base_orders") == 5


def test_product_drilldown_v2_basket_metrics_use_standard_support_and_confidence(monkeypatch):
    monkeypatch.setattr(product_drilldown_service.filters_service, "scope_from_user", lambda _user: {})
    monkeypatch.setattr(
        product_drilldown_service.fact_store,
        "list_columns",
        lambda: {
            "DateExpected",
            "Date",
            "OrderID",
            "Revenue",
            "SKU",
            "ProductID",
            "ProductName",
        },
    )
    monkeypatch.setattr(
        product_drilldown_service.fact_store,
        "build_where_clause",
        lambda *_args, **_kwargs: ("1=1", [], "2024-01-01", "2024-12-31"),
    )
    monkeypatch.setattr(
        product_drilldown_service.fact_store,
        "execute_sql_df",
        lambda *_args, **_kwargs: pd.DataFrame(
            [
                {
                    "product_id": "SKU-CO1",
                    "sku": "SKU-CO1",
                    "product_name": "Linked Side",
                    "display_name": "SKU-CO1 Linked Side",
                    "co_orders": 10,
                    "paired_revenue": 250.0,
                    "orders_with_other": 40,
                    "base_orders": 20,
                    "total_orders": 100,
                }
            ]
        ),
    )
    payload = product_drilldown_service.build_basket_affinity("SKU-001", filters={}, current_user_obj=object())
    row = payload["rows"][0]
    assert row["support"] == pytest.approx(0.10)
    assert row["confidence"] == pytest.approx(0.50)
    assert row["lift"] == pytest.approx(1.25)


def test_product_drilldown_v2_quality_flags_count_zero_cost_as_covered():
    payload = product_drilldown_service.build_quality_flags(
        pd.DataFrame(
            {
                "cost": [0.0, None, 7.5],
                "revenue": [12.0, 10.0, 18.0],
            }
        )
    )
    assert payload["cost_coverage_pct"] == pytest.approx((2 / 3) * 100.0)
    assert payload["missing_cost_rows"] == 1


def test_product_drilldown_v2_weekly_series_uses_monday_starts():
    payload = product_drilldown_service._series_by_freq(
        pd.DataFrame(
            {
                "order_date": pd.to_datetime(["2025-01-06", "2025-01-07", "2025-01-12", "2025-01-13"]),
                "revenue": [10.0, 20.0, 30.0, 40.0],
                "units": [1.0, 1.0, 1.0, 1.0],
                "weight_lb": [2.0, 2.0, 2.0, 2.0],
                "profit": [3.0, 4.0, 5.0, 6.0],
            }
        ),
        "week",
    )
    assert payload["period"].tolist() == ["2025-01-06", "2025-01-13"]
    assert payload["revenue"].tolist() == [60.0, 40.0]


def test_product_drilldown_v2_forecast_flag_visibility(product_drilldown_v2_client):
    client, _expected_customers = product_drilldown_v2_client
    response = client.get("/products/SKU-001/drilldown")
    assert response.status_code == 200
    assert b"Forecast & Planning" in response.data


def test_product_drilldown_v2_forecast_hidden_when_flag_off(product_drilldown_v2_client):
    client, _expected_customers = product_drilldown_v2_client
    app_obj = client.application
    previous = app_obj.config.get("PRODUCT_FORECAST_V1")
    app_obj.config["PRODUCT_FORECAST_V1"] = False
    try:
        response = client.get("/products/SKU-001/drilldown")
    finally:
        app_obj.config["PRODUCT_FORECAST_V1"] = previous
    assert response.status_code == 200
    assert b"Forecast & Planning" not in response.data


def test_product_drilldown_v2_forecast_api_disabled_when_flag_off(product_drilldown_v2_client):
    client, _expected_customers = product_drilldown_v2_client
    app_obj = client.application
    previous = app_obj.config.get("PRODUCT_FORECAST_V1")
    app_obj.config["PRODUCT_FORECAST_V1"] = False
    try:
        response = client.get("/api/products/SKU-001/forecast?freq=month")
    finally:
        app_obj.config["PRODUCT_FORECAST_V1"] = previous
    assert response.status_code == 404


def test_product_drilldown_v2_forecast_api_non_negative_and_metadata(product_drilldown_v2_client):
    client, _expected_customers = product_drilldown_v2_client
    response = client.get("/api/products/SKU-001/forecast?freq=month")
    assert response.status_code == 200
    payload = response.get_json() or {}
    assert "actual_series" in payload
    assert "forecast_series" in payload
    meta = payload.get("meta") or {}
    assert meta.get("method") in {"ets", "damped_ma", "moving_average_fallback"}
    assert "history_points" in meta
    for row in payload.get("forecast_series") or []:
        assert (row.get("yhat") or 0) >= 0
        assert (row.get("yhat_lower") or 0) >= 0
        assert (row.get("yhat_upper") or 0) >= 0


def test_product_drilldown_v2_forecast_api_short_history_fallback(monkeypatch, app):
    short_df = pd.DataFrame(
        {
            "order_date": pd.date_range("2025-01-01", periods=4, freq="MS"),
            "revenue": [120.0, 125.0, 118.0, 130.0],
            "units": [9.0, 10.0, 8.5, 9.5],
            "weight_lb": [20.0, 21.0, 19.0, 20.0],
            "order_id": [f"O-{idx}" for idx in range(4)],
            "customer_id": [f"C-{idx}" for idx in range(4)],
        }
    )

    monkeypatch.setattr(product_drilldown_service.access_policy, "enforce_entity_access", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(product_drilldown_service.filters_service, "scope_from_user", lambda _user: {})
    monkeypatch.setattr(product_drilldown_service.fact_store, "cache_buster", lambda: "test-ds-v1")
    monkeypatch.setattr(
        product_drilldown_service,
        "_product_row_query",
        lambda *_args, **_kwargs: (short_df.copy(), "2025-01-01", "2025-04-30"),
    )

    with app.app_context():
        app.config["PRODUCT_FORECAST_V1"] = True
        payload = product_drilldown_service.build_product_forecast_payload(
            "SKU-001",
            filters={},
            current_user_obj=object(),
            freq="month",
            horizon=6,
        )
    meta = payload.get("meta") or {}
    assert meta.get("method") == "moving_average_fallback"
    assert bool(meta.get("insufficient_history")) is True
    assert len(payload.get("forecast_series") or []) == 6


def test_product_drilldown_v2_forecast_api_scope_denied_returns_403(product_drilldown_v2_client, monkeypatch):
    client, _expected_customers = product_drilldown_v2_client
    monkeypatch.setattr(
        product_drilldown_service,
        "cached_bundle",
        lambda **kwargs: kwargs["builder"](),
    )
    monkeypatch.setattr(
        product_drilldown_service.access_policy,
        "enforce_entity_access",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(Forbidden()),
    )
    response = client.get("/api/products/SKU-001/forecast?freq=month")
    assert response.status_code == 403
