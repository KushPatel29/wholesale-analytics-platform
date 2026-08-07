from __future__ import annotations

import io

import pandas as pd
import pytest

from app.core.access_policy import AccessScope
from app.services import fact_store, filters_service, salesreps_bundle


@pytest.fixture
def seed_salesreps_exports(tmp_path, monkeypatch):
    rows = []
    # Target rep with >25 customers and >25 products across six months.
    for idx in range(1, 61):
        month = ((idx - 1) % 6) + 1
        customer = f"C-{idx:03d}"
        product = f"P-{((idx - 1) % 35) + 1:03d}"
        revenue = float(1000 + idx * 3)
        cost = revenue * 0.7
        rows.append(
            {
                "Date": f"2025-{month:02d}-15",
                "DateExpected": f"2025-{month:02d}-15",
                "SalesRepId": "R1",
                "SalesRepName": "Alex",
                "OrderId": f"O-{idx:04d}",
                "CustomerId": customer,
                "CustomerName": f"Customer {customer}",
                "ProductId": product,
                "ProductName": f"Product {product}",
                "OrderStatus": "packed",
                "Revenue": revenue,
                "Cost": cost,
                "QuantityOrdered": 10 + (idx % 5),
                "WeightLb": 20.0 + (idx % 7),
                "UnitOfBillingId": 1,
                "pack_item_count_sum": float(10 + (idx % 5)),
                "pack_weight_lb_sum": 20.0 + (idx % 7),
                "pack_count": 1,
                "Price": revenue,
                "CostPrice": cost,
            }
        )

    # Out-of-scope rep for RBAC deny checks.
    for idx in range(1, 8):
        revenue = float(700 + idx * 5)
        cost = revenue * 0.75
        rows.append(
            {
                "Date": f"2025-04-{idx:02d}",
                "DateExpected": f"2025-04-{idx:02d}",
                "SalesRepId": "R2",
                "SalesRepName": "Bea",
                "OrderId": f"R2-{idx:03d}",
                "CustomerId": f"R2C-{idx:03d}",
                "CustomerName": f"R2 Customer {idx}",
                "ProductId": f"R2P-{idx:03d}",
                "ProductName": f"R2 Product {idx}",
                "OrderStatus": "packed",
                "Revenue": revenue,
                "Cost": cost,
                "QuantityOrdered": 4 + idx,
                "WeightLb": 8.0 + idx,
                "UnitOfBillingId": 1,
                "pack_item_count_sum": float(4 + idx),
                "pack_weight_lb_sum": 8.0 + idx,
                "pack_count": 1,
                "Price": revenue,
                "CostPrice": cost,
            }
        )

    df = pd.DataFrame(rows)
    parquet_path = tmp_path / "fact_salesreps_exports.parquet"
    df.to_parquet(parquet_path)

    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    monkeypatch.delenv("CUSTOMER_REP_HISTORY_PATH", raising=False)
    monkeypatch.delenv("TERRITORY_REP_HISTORY_PATH", raising=False)
    monkeypatch.delenv("CUSTOMER_TERRITORY_HISTORY_PATH", raising=False)
    monkeypatch.delenv("SALESREP_SUCCESSION_PATH", raising=False)
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def _csv_frame(resp) -> pd.DataFrame:
    return pd.read_csv(io.StringIO(resp.get_data(as_text=True)))


def test_salesrep_export_customers_and_products_not_truncated(app_client, seed_salesreps_exports):
    qs = {"start": "2025-01-01", "end": "2025-12-31", "dataset": "customers", "format": "csv"}
    resp_customers = app_client.get("/salesreps/R1/export", query_string=qs)
    assert resp_customers.status_code == 200
    customers_df = _csv_frame(resp_customers)
    assert len(customers_df.index) > 25
    assert len(customers_df["customer_id"].dropna().unique()) == len(customers_df.index)

    resp_products = app_client.get(
        "/salesreps/R1/export",
        query_string={"start": "2025-01-01", "end": "2025-12-31", "dataset": "products", "format": "csv"},
    )
    assert resp_products.status_code == 200
    products_df = _csv_frame(resp_products)
    assert len(products_df.index) > 25
    assert len(products_df["product_id"].dropna().unique()) == len(products_df.index)


def test_salesrep_export_trend_and_history_respect_filters(app_client, seed_salesreps_exports):
    trend_resp = app_client.get(
        "/salesreps/R1/export",
        query_string={"start": "2025-01-01", "end": "2025-06-30", "dataset": "trend", "format": "csv"},
    )
    assert trend_resp.status_code == 200
    trend_df = _csv_frame(trend_resp)
    assert len(trend_df.index) == 6

    scope = {"is_admin": True}
    narrow_filters = filters_service.resolve_effective_filters(
        {"start": "2025-01-01", "end": "2025-03-31"},
        session_obj={},
        user_id=None,
        sticky_enabled=False,
    )
    wide_filters = filters_service.resolve_effective_filters(
        {"start": "2025-01-01", "end": "2025-06-30"},
        session_obj={},
        user_id=None,
        sticky_enabled=False,
    )
    narrow_df = salesreps_bundle.build_salesrep_export_dataset("R1", narrow_filters, scope, {}, dataset="history")
    wide_df = salesreps_bundle.build_salesrep_export_dataset("R1", wide_filters, scope, {}, dataset="history")
    assert len(wide_df.index) > len(narrow_df.index)

    # "all_time" should not override explicit filter window anymore.
    narrow_all_time_df = salesreps_bundle.build_salesrep_export_dataset(
        "R1",
        narrow_filters,
        scope,
        {"all_time": "1"},
        dataset="history",
    )
    assert len(narrow_all_time_df.index) == len(narrow_df.index)


def test_salesrep_export_rbac_scope_enforced(app_client, seed_salesreps_exports, monkeypatch):
    class _DummyUser:
        is_authenticated = True
        is_active = True
        is_anonymous = False
        role = "sales"
        id = 42

        def get_id(self):
            return "42"

    app_client.application.config["LOGIN_DISABLED"] = False
    app_client.application.config["AUTHZ_DISABLED"] = False

    monkeypatch.setattr("flask_login.utils._get_user", lambda *a, **k: _DummyUser())
    monkeypatch.setattr(
        "app.core.access_policy.get_current_scope",
        lambda use_cache=True: AccessScope(
            is_admin=False,
            user_id=42,
            erp_user_id="r1",
            allowed_erp_user_ids=["r1"],
            scope_mode="list",
            permissions_version="1",
            scope_hash="scope-r1",
        ),
    )

    allowed = app_client.get(
        "/salesreps/R1/export",
        query_string={"start": "2025-01-01", "end": "2025-12-31", "dataset": "customers", "format": "csv"},
    )
    denied = app_client.get(
        "/salesreps/R2/export",
        query_string={"start": "2025-01-01", "end": "2025-12-31", "dataset": "customers", "format": "csv"},
    )
    assert allowed.status_code == 200
    assert denied.status_code == 403


@pytest.fixture
def seed_salesreps_snapshot_exports(tmp_path, monkeypatch):
    rows = [
        {
            "Date": "2025-01-15",
            "DateExpected": "2025-01-15",
            "SalesRepId": "R1",
            "SalesRepName": "Alex",
            "PrimarySalesRepId": "R2",
            "OrderId": "EXP-SNAP-1",
            "CustomerId": "C-SNAP-01",
            "CustomerName": "Moved Customer",
            "ProductId": "P-SNAP-01",
            "ProductName": "Snapshot Product",
            "OrderStatus": "packed",
            "Revenue": 900.0,
            "Cost": 540.0,
            "QuantityOrdered": 9,
            "WeightLb": 18.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 9.0,
            "pack_weight_lb_sum": 18.0,
            "pack_count": 1,
            "Price": 100.0,
            "CostPrice": 60.0,
        },
        {
            "Date": "2025-02-20",
            "DateExpected": "2025-02-20",
            "SalesRepId": "R2",
            "SalesRepName": "Bea",
            "PrimarySalesRepId": "R2",
            "OrderId": "EXP-SNAP-2",
            "CustomerId": "C-SNAP-02",
            "CustomerName": "Current Customer",
            "ProductId": "P-SNAP-02",
            "ProductName": "Snapshot Product 2",
            "OrderStatus": "packed",
            "Revenue": 600.0,
            "Cost": 360.0,
            "QuantityOrdered": 6,
            "WeightLb": 12.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 6.0,
            "pack_weight_lb_sum": 12.0,
            "pack_count": 1,
            "Price": 100.0,
            "CostPrice": 60.0,
        },
    ]

    parquet_path = tmp_path / "fact_salesreps_snapshot_exports.parquet"
    pd.DataFrame(rows).to_parquet(parquet_path)

    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    monkeypatch.delenv("CUSTOMER_REP_HISTORY_PATH", raising=False)
    monkeypatch.delenv("TERRITORY_REP_HISTORY_PATH", raising=False)
    monkeypatch.delenv("CUSTOMER_TERRITORY_HISTORY_PATH", raising=False)
    monkeypatch.delenv("SALESREP_SUCCESSION_PATH", raising=False)
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def test_salesreps_exports_resolve_snapshot_owner_names(seed_salesreps_snapshot_exports):
    scope = {"is_admin": True}
    filters = filters_service.resolve_effective_filters(
        {"start": "2025-01-01", "end": "2025-12-31"},
        session_obj={},
        user_id=None,
        sticky_enabled=False,
    )
    summary_df = salesreps_bundle.build_salesreps_export_frame(
        filters,
        scope,
        {"attribution_mode": "current_owner", "roster_mode": "include_former"},
    )
    assert "Rep Name" in summary_df.columns
    assert "Rep ID" not in summary_df.columns
    assert "Bea" in summary_df["Rep Name"].tolist()

    customers_df = salesreps_bundle.build_salesrep_export_dataset(
        "R2",
        filters,
        scope,
        {"attribution_mode": "current_owner", "roster_mode": "include_former"},
        dataset="customers",
    )
    owner_names = set(customers_df["account_owner_name"].dropna().astype(str).tolist())
    assert "Bea" in owner_names


def test_salesrep_drilldown_rbac_scope_enforced(app_client, seed_salesreps_exports, monkeypatch):
    class _DummySalesUser:
        is_authenticated = True
        is_active = True
        is_anonymous = False
        role = "sales"
        id = 42

        def get_id(self):
            return "42"

    app_client.application.config["LOGIN_DISABLED"] = False
    app_client.application.config["AUTHZ_DISABLED"] = False
    monkeypatch.setattr("flask_login.utils._get_user", lambda *a, **k: _DummySalesUser())
    monkeypatch.setattr(
        "app.core.access_policy.get_current_scope",
        lambda use_cache=True: AccessScope(
            is_admin=False,
            user_id=42,
            erp_user_id="r1",
            allowed_erp_user_ids=["r1"],
            scope_mode="list",
            permissions_version="1",
            scope_hash="scope-r1",
        ),
    )

    allowed = app_client.get("/salesreps/R1")
    denied = app_client.get("/salesreps/R2")
    assert allowed.status_code == 200
    assert denied.status_code == 403


def test_salesrep_drilldown_admin_can_open_all(app_client, seed_salesreps_exports, monkeypatch):
    class _DummyAdminUser:
        is_authenticated = True
        is_active = True
        is_anonymous = False
        role = "admin"
        id = 1

        def get_id(self):
            return "1"

    app_client.application.config["LOGIN_DISABLED"] = False
    app_client.application.config["AUTHZ_DISABLED"] = False
    monkeypatch.setattr("flask_login.utils._get_user", lambda *a, **k: _DummyAdminUser())
    monkeypatch.setattr(
        "app.core.access_policy.get_current_scope",
        lambda use_cache=True: AccessScope(
            is_admin=True,
            user_id=1,
            erp_user_id="admin",
            allowed_erp_user_ids=[],
            scope_mode="all",
            permissions_version="1",
            scope_hash="scope-admin",
        ),
    )

    resp = app_client.get("/salesreps/R2")
    assert resp.status_code == 200


def test_salesrep_drilldown_v2_renders_export_buttons(app_client, seed_salesreps_exports, monkeypatch):
    monkeypatch.setitem(app_client.application.config, "SALESREP_DRILLDOWN_V2", True)
    resp = app_client.get("/salesreps/R1")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    for dataset in ("trend", "mix", "customers", "products", "movers_customers", "movers_products", "margin_risk", "at_risk"):
        assert f'data-export-dataset="{dataset}"' in body
    assert 'data-v2-enabled="1"' in body
    assert 'id="drAttributionMode"' in body
    assert 'id="drOwnershipCompare"' in body
    assert 'id="drWarnings"' in body
    assert "drMoversCustomersTable" in body
    assert "drMarginRiskTable" in body
    assert "Current owner portfolio context" in body
    assert 'id="SalesRepDrilldownBoot"' in body


def test_salesrep_drilldown_v2_falls_back_to_legacy_when_prefetch_fails(app_client, monkeypatch):
    monkeypatch.setitem(app_client.application.config, "SALESREP_DRILLDOWN_V2", True)
    monkeypatch.setattr("app.blueprints.salesreps.bundle_service.drilldown", lambda *_args, **_kwargs: {"error": {"message": "synthetic salesrep bundle failure"}})

    resp = app_client.get("/salesreps/R1")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'data-v2-enabled="0"' in body
    assert "Revenue and Profit Trend (Monthly)" in body
    assert 'id="drAttributionMode"' not in body


def test_salesrep_drilldown_v1_fallback_renders_without_v2_blocks(app_client, monkeypatch):
    monkeypatch.setitem(app_client.application.config, "SALESREP_DRILLDOWN_V2", False)
    resp = app_client.get("/salesreps/R1")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'data-v2-enabled="0"' in body
    assert "drMoversCustomersTable" not in body
    assert "drMarginRiskTable" not in body


def test_salesrep_export_xlsx_endpoints_return_full_data(app_client, seed_salesreps_exports):
    try:
        import openpyxl  # noqa: F401
    except Exception:
        pytest.skip("openpyxl not installed")

    legacy = app_client.get(
        "/salesreps/R1/export.xlsx",
        query_string={"start": "2025-01-01", "end": "2025-12-31"},
    )
    assert legacy.status_code == 200, legacy.get_data(as_text=True)[:300]
    if "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in (legacy.content_type or ""):
        with pd.ExcelFile(io.BytesIO(legacy.get_data())) as xls:
            assert "Customers" in xls.sheet_names
            customers_df = pd.read_excel(xls, sheet_name="Customers")
    else:
        assert "text/csv" in (legacy.content_type or "")
        customers_df = _csv_frame(legacy)
    assert len(customers_df.index) > 25

    dataset_xlsx = app_client.get(
        "/salesreps/R1/export",
        query_string={"start": "2025-01-01", "end": "2025-12-31", "dataset": "products", "format": "xlsx"},
    )
    assert dataset_xlsx.status_code == 200
    if "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" in (dataset_xlsx.content_type or ""):
        products_df = pd.read_excel(io.BytesIO(dataset_xlsx.get_data()))
    else:
        assert "text/csv" in (dataset_xlsx.content_type or "")
        products_df = _csv_frame(dataset_xlsx)
    assert len(products_df.index) > 25


def test_salesrep_new_export_types_and_export_type_param(app_client, seed_salesreps_exports):
    for dataset in ("movers_customers", "movers_products"):
        resp = app_client.get(
            "/salesreps/R1/export",
            query_string={"start": "2025-01-01", "end": "2025-12-31", "export_type": dataset, "format": "csv"},
        )
        assert resp.status_code == 200
        frame = _csv_frame(resp)
        assert len(frame.index) > 0

    margin_risk = app_client.get(
        "/salesreps/R1/export",
        query_string={"start": "2025-01-01", "end": "2025-12-31", "export_type": "margin_risk", "format": "csv"},
    )
    assert margin_risk.status_code == 200
    margin_risk_df = _csv_frame(margin_risk)
    assert {
        "product_id",
        "product_name",
        "margin_pct",
        "leakage_to_target",
        "negative_margin_flag",
    }.issubset(set(margin_risk_df.columns))

    at_risk = app_client.get(
        "/salesreps/R1/export",
        query_string={"start": "2025-01-01", "end": "2025-12-31", "export_type": "at_risk", "format": "csv", "at_risk_days": "30"},
    )
    assert at_risk.status_code == 200
    at_risk_df = _csv_frame(at_risk)
    assert list(at_risk_df.columns)


@pytest.fixture
def seed_salesreps_ownership_exports(tmp_path, monkeypatch):
    rows = [
        {
            "Date": "2025-01-15",
            "DateExpected": "2025-01-15",
            "SalesRepId": "R1",
            "SalesRepName": "Alex",
            "OrderId": "EXP-OWN-1",
            "CustomerId": "C-OWN-01",
            "CustomerName": "Moved Customer",
            "ProductId": "P-OWN-01",
            "ProductName": "Ownership Product",
            "OrderStatus": "packed",
            "Revenue": 900.0,
            "Cost": 540.0,
            "QuantityOrdered": 9,
            "WeightLb": 18.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 9.0,
            "pack_weight_lb_sum": 18.0,
            "pack_count": 1,
            "Price": 100.0,
            "CostPrice": 60.0,
        },
        {
            "Date": "2025-03-20",
            "DateExpected": "2025-03-20",
            "SalesRepId": "R1",
            "SalesRepName": "Alex",
            "OrderId": "EXP-OWN-2",
            "CustomerId": "C-OWN-01",
            "CustomerName": "Moved Customer",
            "ProductId": "P-OWN-02",
            "ProductName": "Ownership Product 2",
            "OrderStatus": "packed",
            "Revenue": 600.0,
            "Cost": 360.0,
            "QuantityOrdered": 6,
            "WeightLb": 11.0,
            "UnitOfBillingId": 1,
            "pack_item_count_sum": 6.0,
            "pack_weight_lb_sum": 11.0,
            "pack_count": 1,
            "Price": 100.0,
            "CostPrice": 60.0,
        },
    ]
    bridge = pd.DataFrame(
        [
            {
                "customer_id": "C-OWN-01",
                "rep_id": "R1",
                "rep_name": "Alex",
                "assignment_start_date": "2025-01-01",
                "assignment_end_date": "2025-03-31",
                "is_current": False,
                "rep_is_active": False,
            },
            {
                "customer_id": "C-OWN-01",
                "rep_id": "R2",
                "rep_name": "Bea",
                "assignment_start_date": "2025-04-01",
                "assignment_end_date": None,
                "is_current": True,
                "rep_is_active": True,
            },
        ]
    )

    parquet_path = tmp_path / "fact_salesreps_ownership_exports.parquet"
    bridge_path = tmp_path / "customer_rep_history_exports.csv"
    pd.DataFrame(rows).to_parquet(parquet_path)
    bridge.to_csv(bridge_path, index=False)

    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    monkeypatch.setenv("CUSTOMER_REP_HISTORY_PATH", str(bridge_path))
    monkeypatch.delenv("TERRITORY_REP_HISTORY_PATH", raising=False)
    monkeypatch.delenv("CUSTOMER_TERRITORY_HISTORY_PATH", raising=False)
    monkeypatch.delenv("SALESREP_SUCCESSION_PATH", raising=False)
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def test_salesreps_export_respects_current_owner_mode(app_client, seed_salesreps_ownership_exports):
    resp = app_client.get(
        "/salesreps/export.csv",
        query_string={
            "start": "2025-01-01",
            "end": "2025-12-31",
            "attribution_mode": "current_owner",
            "roster_mode": "include_former",
        },
    )
    assert resp.status_code == 200
    frame = _csv_frame(resp)
    assert "Rep ID" not in frame.columns
    owner_row = frame.loc[frame["Rep Name"] == "Bea"]
    assert not owner_row.empty
    row = owner_row.iloc[0]
    assert float(row["Revenue"]) == pytest.approx(1500.0, abs=0.01)
    assert float(row["Transferred In Revenue"]) == pytest.approx(1500.0, abs=0.01)
    assert float(row["Current Owner Revenue"]) == pytest.approx(1500.0, abs=0.01)


def test_salesrep_dataset_xlsx_includes_metadata_sheet(app_client, seed_salesreps_exports):
    try:
        import openpyxl  # noqa: F401
    except Exception:
        pytest.skip("openpyxl not installed")

    resp = app_client.get(
        "/salesreps/R1/export",
        query_string={"start": "2025-01-01", "end": "2025-12-31", "dataset": "customers", "format": "xlsx"},
    )
    assert resp.status_code == 200
    if "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" not in (resp.content_type or ""):
        pytest.skip("XLSX engine unavailable in test environment")

    with pd.ExcelFile(io.BytesIO(resp.get_data())) as xls:
        assert "Metadata" in xls.sheet_names
        assert "Customers" in xls.sheet_names
        metadata = pd.read_excel(xls, sheet_name="Metadata")
        assert {"key", "value"}.issubset(set(metadata.columns))


def test_salesreps_page_xlsx_export_builds_portfolio_and_rep_tabs(app_client, seed_salesreps_exports):
    try:
        import openpyxl  # noqa: F401
    except Exception:
        pytest.skip("openpyxl not installed")

    resp = app_client.get(
        "/salesreps/export.xlsx",
        query_string={"start": "2025-01-01", "end": "2025-12-31"},
    )
    assert resp.status_code == 200
    if "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" not in (resp.content_type or ""):
        pytest.skip("XLSX engine unavailable in test environment")

    with pd.ExcelFile(io.BytesIO(resp.get_data())) as xls:
        assert "Portfolio Summary" in xls.sheet_names
        assert "Alex" in xls.sheet_names
        assert "Bea" in xls.sheet_names
        alex_df = pd.read_excel(xls, sheet_name="Alex")
        assert {"Customer", "Risk Signal", "Silent Days"}.issubset(set(alex_df.columns))
