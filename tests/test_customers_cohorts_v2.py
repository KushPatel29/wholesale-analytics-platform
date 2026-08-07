import pandas as pd
import pytest

from app.blueprints import customers as customers_blueprint
from app.core.access_policy import AccessScope
from app.services import customers_cohorts_v2, fact_store
from app.services.filters import normalize_filters


@pytest.fixture
def seed_customers_cohorts_v2(tmp_path, monkeypatch):
    rows = []

    def add_row(
        *,
        order_id: str,
        customer_id: str,
        customer_name: str,
        date_value: str,
        revenue: float,
        rep_id: str,
        rep_name: str,
        region: str | None,
    ) -> None:
        rows.append(
            {
                "Date": date_value,
                "DateExpected": date_value,
                "OrderId": order_id,
                "CustomerId": customer_id,
                "CustomerName": customer_name,
                "SalesRepId": rep_id,
                "SalesRepName": rep_name,
                "RegionName": region,
                "OrderStatus": "packed",
                "Revenue": revenue,
                "Cost": revenue * 0.6,
                "QuantityOrdered": 2,
                "WeightLb": 10.0,
                "UnitOfBillingId": 1,
                "pack_item_count_sum": 2.0,
                "pack_weight_lb_sum": 10.0,
                "pack_count": 1,
                "ProductId": "P-1",
                "ProductName": "Product 1",
                "SupplierId": "S-1",
                "SupplierName": "Supplier 1",
            }
        )

    # Jan cohort: C1 active/at-risk, C2 churned (R1/West)
    add_row(order_id="O-001", customer_id="C1", customer_name="Customer 1", date_value="2025-01-05", revenue=1200, rep_id="R1", rep_name="Alex", region="West")
    add_row(order_id="O-002", customer_id="C1", customer_name="Customer 1", date_value="2025-02-10", revenue=900, rep_id="R1", rep_name="Alex", region="West")
    add_row(order_id="O-003", customer_id="C1", customer_name="Customer 1", date_value="2025-04-15", revenue=800, rep_id="R1", rep_name="Alex", region="West")

    add_row(order_id="O-004", customer_id="C2", customer_name="Customer 2", date_value="2025-01-20", revenue=1100, rep_id="R1", rep_name="Alex", region="West")

    # Feb cohort: C3 churned (R2/East)
    add_row(order_id="O-005", customer_id="C3", customer_name="Customer 3", date_value="2025-02-01", revenue=700, rep_id="R2", rep_name="Bea", region="East")
    add_row(order_id="O-006", customer_id="C3", customer_name="Customer 3", date_value="2025-03-05", revenue=500, rep_id="R2", rep_name="Bea", region="East")

    # Mar cohort: C4 reactivated (R1/Unknown region)
    add_row(order_id="O-007", customer_id="C4", customer_name="Customer 4", date_value="2025-03-10", revenue=600, rep_id="R1", rep_name="Alex", region=None)
    add_row(order_id="O-008", customer_id="C4", customer_name="Customer 4", date_value="2025-06-20", revenue=1500, rep_id="R1", rep_name="Alex", region=None)

    df = pd.DataFrame(rows)
    parquet_path = tmp_path / "fact_customers_cohorts_v2.parquet"
    df.to_parquet(parquet_path)

    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    fact_store.reset_duckdb_state()
    fact_store.init_views()
    yield parquet_path
    fact_store.reset_duckdb_state()


def _scope_all() -> dict:
    return {
        "is_admin": True,
        "user_id": "admin",
        "role": "admin",
        "scope_mode": "all",
        "scope_hash": "scope-all",
        "allowed_erp_user_ids": [],
        "allowed_customer_ids": [],
        "allowed_region_ids": [],
        "allowed_supplier_ids": [],
        "permissions_version": "1",
    }


def test_cohorts_v2_retention_and_churn_logic(seed_customers_cohorts_v2):
    filters = normalize_filters({"start": "2025-01-01", "end": "2025-12-31"})
    args = {
        "threshold": "90",
        "lookback_months": "24",
        "cohort_granularity": "month",
        "cohort_horizon": "6",
        "reactivation_window_days": "30",
    }

    payload = customers_cohorts_v2.build_cohorts_payload(filters, _scope_all(), args)
    kpis = payload.get("kpis") or {}

    assert int(kpis.get("customers_total") or 0) == 4
    assert int(kpis.get("churned_customers") or 0) == 2
    assert int(kpis.get("at_risk_customers") or 0) == 1
    assert int(kpis.get("active_customers") or 0) == 2
    assert bool(kpis.get("reconciled")) is True
    assert int(kpis.get("reactivated_customers") or 0) == 1

    retention_rows = (payload.get("retention") or {}).get("rows") or []
    assert retention_rows
    jan_m0 = next(
        r for r in retention_rows
        if str(r.get("cohort_label")) == "2025-01" and int(r.get("month_index") or 0) == 0
    )
    jan_m1 = next(
        r for r in retention_rows
        if str(r.get("cohort_label")) == "2025-01" and int(r.get("month_index") or 0) == 1
    )
    assert float(jan_m0.get("retention_pct") or 0.0) == pytest.approx(100.0, rel=1e-6)
    assert float(jan_m1.get("retention_pct") or 0.0) == pytest.approx(50.0, rel=1e-6)

    churned = customers_cohorts_v2.fetch_churn_status_list(
        filters,
        _scope_all(),
        args,
        status="churned",
        export_all=True,
    )
    churned_ids = {str(r.get("customer_id")) for r in (churned.get("rows") or [])}
    assert churned_ids == {"C2", "C3"}


def test_cohorts_v2_effective_end_date_drives_active_kpi(seed_customers_cohorts_v2):
    filters = normalize_filters({"start": "2025-01-01", "end": "2025-05-31"})
    args = {
        "threshold": "90",
        "lookback_months": "24",
        "cohort_granularity": "month",
        "cohort_horizon": "6",
    }

    payload = customers_cohorts_v2.build_cohorts_payload(filters, _scope_all(), args)
    kpis = payload.get("kpis") or {}
    meta = payload.get("meta") or {}

    assert meta.get("analysis_window_end") == "2025-05-31"
    assert meta.get("churn_cutoff_date") == "2025-03-02"
    assert int(kpis.get("customers_total") or 0) == 4
    assert int(kpis.get("active_customers") or 0) == 3
    assert int(kpis.get("churned_customers") or 0) == 1
    assert int(kpis.get("at_risk_customers") or 0) == 2
    assert bool(kpis.get("reconciled")) is True


def test_cohorts_v2_ignores_hidden_default_window(seed_customers_cohorts_v2):
    filters = normalize_filters({})
    args = {
        "threshold": "90",
        "lookback_months": "24",
        "cohort_granularity": "month",
        "cohort_horizon": "6",
    }

    payload = customers_cohorts_v2.build_cohorts_payload(
        filters,
        _scope_all(),
        args,
        filters_meta={"source": "defaults", "filters_source": "default"},
    )
    kpis = payload.get("kpis") or {}

    assert int(kpis.get("customers_total") or 0) == 4
    assert payload.get("meta", {}).get("resolved_filters_source") == "defaults"


def test_cohorts_v2_low_sample_region_flag_and_export_suffix(seed_customers_cohorts_v2):
    filters = normalize_filters({"start": "2025-01-01", "end": "2025-12-31"})
    args = {
        "threshold": "90",
        "lookback_months": "24",
        "cohort_granularity": "month",
        "cohort_horizon": "6",
    }

    payload = customers_cohorts_v2.build_cohorts_payload(filters, _scope_all(), args)
    region_rows = (payload.get("segmentation") or {}).get("region") or []
    east_row = next(r for r in region_rows if str(r.get("segment")) == "East")
    assert east_row.get("low_sample") is True
    assert float(east_row.get("churn_rate_pct") or 0.0) == pytest.approx(100.0, rel=1e-6)

    export_df, stem = customers_cohorts_v2.build_export_dataset(filters, _scope_all(), args, "churn_region")
    assert stem == "churn_by_region_t90_lb24_month_h6"
    assert "Low Sample" in export_df.columns
    east_export = next(rec for rec in export_df.to_dict(orient="records") if str(rec.get("Segment")) == "East")
    assert bool(east_export.get("Low Sample")) is True


def test_cohorts_controls_resolver_precedence_and_hash(seed_customers_cohorts_v2):
    session_state = {
        "threshold": 120,
        "lookback_months": 36,
        "cohort_granularity": "month",
        "cohort_horizon": 8,
        "status": "churned",
        "segmentation": "segment",
        "table_search": "legacy",
        "table_page": 3,
        "page_size": 50,
    }

    state = customers_cohorts_v2.resolve_cohorts_controls(
        {"status": "at_risk", "lookback_months": "24", "table_search": "fresh"},
        session_state,
    )
    assert state.status == "at_risk"
    assert state.segmentation == "segment"
    assert state.controls.lookback_months == 24
    assert state.table_search == "fresh"
    assert state.source == "request"

    changed = customers_cohorts_v2.resolve_cohorts_controls({"status": "churned"}, session_state)
    assert state.controls_hash != changed.controls_hash

    invalid = customers_cohorts_v2.resolve_cohorts_controls(
        {"status": "bogus", "segmentation": "bogus", "lookback_months": "999"},
        {},
        allow_sales_rep=False,
    )
    assert invalid.status == "at_risk"
    assert invalid.segmentation == "region"
    assert any("Invalid status" in warning for warning in invalid.warnings)
    assert any("Invalid segmentation" in warning for warning in invalid.warnings)


def test_cohorts_v2_status_changes_dataset_and_cache_key(seed_customers_cohorts_v2, app):
    filters = normalize_filters({"start": "2025-01-01", "end": "2025-12-31"})
    at_risk_state = customers_cohorts_v2.resolve_cohorts_controls({"status": "at_risk"}, {})
    churned_state = customers_cohorts_v2.resolve_cohorts_controls({"status": "churned"}, {})

    at_risk_rows = customers_cohorts_v2.fetch_churn_status_list(
        filters,
        _scope_all(),
        {},
        state=at_risk_state,
        export_all=True,
    )
    churned_rows = customers_cohorts_v2.fetch_churn_status_list(
        filters,
        _scope_all(),
        {},
        state=churned_state,
        export_all=True,
    )

    assert {str(r.get("customer_id")) for r in (at_risk_rows.get("rows") or [])} == {"C1"}
    assert {str(r.get("customer_id")) for r in (churned_rows.get("rows") or [])} == {"C2", "C3"}

    with app.app_context():
        at_risk_payload = customers_blueprint._cohorts_v2_bundle({}, filters, _scope_all(), at_risk_state, {"source": "test"})
        churned_payload = customers_blueprint._cohorts_v2_bundle({}, filters, _scope_all(), churned_state, {"source": "test"})
    assert at_risk_payload.get("meta", {}).get("cache_key") != churned_payload.get("meta", {}).get("cache_key")
    assert at_risk_payload.get("meta", {}).get("state_hash") != churned_payload.get("meta", {}).get("state_hash")


def test_cohorts_v2_active_status_list_and_export_match(seed_customers_cohorts_v2):
    filters = normalize_filters({"start": "2025-01-01", "end": "2025-12-31"})
    state = customers_cohorts_v2.resolve_cohorts_controls({"status": "active"}, {})

    list_payload = customers_cohorts_v2.fetch_churn_status_list(
        filters,
        _scope_all(),
        {},
        state=state,
        export_all=True,
    )
    assert {str(r.get("customer_id")) for r in (list_payload.get("rows") or [])} == {"C4"}

    export_df, _ = customers_cohorts_v2.build_export_dataset(
        filters,
        _scope_all(),
        {},
        "status_list",
        state=state,
    )
    assert set(export_df["Customer ID"].dropna().astype(str).tolist()) == {"C4"}


def test_cohorts_v2_rbac_scope_for_drilldown_and_export(seed_customers_cohorts_v2):
    sales_scope = AccessScope(
        is_admin=False,
        user_id=42,
        erp_user_id="r1",
        allowed_erp_user_ids=["r1"],
        allowed_customer_ids=[],
        allowed_region_ids=[],
        allowed_supplier_ids=[],
        scope_mode="list",
        permissions_version="1",
        scope_hash="scope-r1",
    )
    admin_scope = AccessScope(
        is_admin=True,
        user_id=1,
        erp_user_id=None,
        allowed_erp_user_ids=[],
        allowed_customer_ids=[],
        allowed_region_ids=[],
        allowed_supplier_ids=[],
        scope_mode="all",
        permissions_version="1",
        scope_hash="scope-all",
    )
    filters = normalize_filters({"start": "2025-01-01", "end": "2025-12-31"})
    args = {
        "threshold": "90",
        "lookback_months": "24",
        "cohort_granularity": "month",
        "cohort_horizon": "6",
        "status": "churned",
        "cohort": "2025-02",
        "month_index": "0",
    }

    sales_list = customers_cohorts_v2.fetch_churn_status_list(
        filters,
        sales_scope.as_dict(include_allowed=True),
        args,
        status="churned",
        export_all=True,
    )
    assert {str(r.get("customer_id")) for r in (sales_list.get("rows") or [])} == {"C2"}

    admin_list = customers_cohorts_v2.fetch_churn_status_list(
        filters,
        admin_scope.as_dict(include_allowed=True),
        args,
        status="churned",
        export_all=True,
    )
    assert {str(r.get("customer_id")) for r in (admin_list.get("rows") or [])} == {"C2", "C3"}

    sales_drill = customers_cohorts_v2.fetch_cohort_drilldown(
        filters,
        sales_scope.as_dict(include_allowed=True),
        args,
        cohort="2025-02",
        month_index=0,
        export_all=True,
    )
    assert int(sales_drill.get("total_rows") or 0) == 0

    admin_drill = customers_cohorts_v2.fetch_cohort_drilldown(
        filters,
        admin_scope.as_dict(include_allowed=True),
        args,
        cohort="2025-02",
        month_index=0,
        export_all=True,
    )
    assert {str(r.get("customer_id")) for r in (admin_drill.get("rows") or [])} == {"C3"}

    sales_export_df, _ = customers_cohorts_v2.build_export_dataset(
        filters,
        sales_scope.as_dict(include_allowed=True),
        args,
        "status_list",
    )
    admin_export_df, _ = customers_cohorts_v2.build_export_dataset(
        filters,
        admin_scope.as_dict(include_allowed=True),
        args,
        "status_list",
    )
    assert set(sales_export_df["Customer ID"].dropna().astype(str).tolist()) == {"C2"}
    assert set(admin_export_df["Customer ID"].dropna().astype(str).tolist()) == {"C2", "C3"}

    sales_at_risk = customers_cohorts_v2.fetch_churn_status_list(
        filters,
        sales_scope.as_dict(include_allowed=True),
        args,
        status="at_risk",
        export_all=True,
    )
    assert {str(r.get("customer_id")) for r in (sales_at_risk.get("rows") or [])} == {"C1"}


def test_cohorts_v2_page_and_drilldown_render(seed_customers_cohorts_v2, client, monkeypatch):
    client.application.config["COHORTS_V2"] = True

    filters = normalize_filters({"start": "2025-01-01", "end": "2025-12-31"})
    monkeypatch.setattr(
        customers_blueprint,
        "resolve_filters",
        lambda *_args, **_kwargs: (filters, {"source": "test"}),
    )
    monkeypatch.setattr(
        customers_blueprint.filters_service,
        "scope_from_user",
        lambda _user: _scope_all(),
    )

    resp = client.get("/customers/cohorts")
    assert resp.status_code == 200
    assert b"Cohort Retention Heatmap" in resp.data
    assert b"Data Incomplete" in resp.data
    assert b"cohorts/controls/apply" in resp.data

    drill = client.get(
        "/customers/cohorts/drilldown",
        query_string={
            "cohort": "2025-02",
            "month_index": "0",
            "threshold": "90",
            "lookback_months": "24",
        },
    )
    assert drill.status_code == 200
    payload = drill.get_json() or {}
    assert {str(r.get("customer_id")) for r in (payload.get("rows") or [])} == {"C3"}


def test_cohorts_v2_apply_controls_redirects_with_status(seed_customers_cohorts_v2, client, monkeypatch):
    client.application.config["COHORTS_V2"] = True
    client.application.config["COHORTS_STATE_V2"] = True

    filters = normalize_filters({"start": "2025-01-01", "end": "2025-12-31"})
    monkeypatch.setattr(
        customers_blueprint,
        "resolve_filters",
        lambda *_args, **_kwargs: (filters, {"source": "test"}),
    )
    monkeypatch.setattr(
        customers_blueprint.filters_service,
        "scope_from_user",
        lambda _user: _scope_all(),
    )

    resp = client.post(
        "/customers/cohorts/controls/apply",
        data={
            "status": "churned",
            "segmentation": "segment",
            "threshold": "90",
            "lookback_months": "24",
            "cohort_granularity": "month",
            "cohort_horizon": "6",
            "start": "2025-01-01",
            "end": "2025-12-31",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers.get("Location") or ""
    assert "status=churned" in location
    assert "segmentation=segment" in location

    follow = client.get(location)
    assert follow.status_code == 200
    assert b'value="churned" selected' in follow.data


def test_cohorts_v2_sales_scope_list_and_export(seed_customers_cohorts_v2, client, monkeypatch):
    client.application.config["COHORTS_V2"] = True

    filters = normalize_filters({"start": "2025-01-01", "end": "2025-12-31"})
    sales_scope = AccessScope(
        is_admin=False,
        user_id=42,
        erp_user_id="r1",
        allowed_erp_user_ids=["r1"],
        allowed_customer_ids=[],
        allowed_region_ids=[],
        allowed_supplier_ids=[],
        scope_mode="list",
        permissions_version="1",
        scope_hash="scope-r1",
    )
    monkeypatch.setattr(
        customers_blueprint,
        "resolve_filters",
        lambda *_args, **_kwargs: (filters, {"source": "test"}),
    )
    monkeypatch.setattr(
        customers_blueprint.filters_service,
        "scope_from_user",
        lambda _user: sales_scope.as_dict(include_allowed=True),
    )

    list_resp = client.get(
        "/customers/churned/list",
        query_string={"status": "churned", "threshold": "90", "lookback_months": "24"},
    )
    assert list_resp.status_code == 200
    list_payload = list_resp.get_json() or {}
    assert {str(r.get("customer_id")) for r in (list_payload.get("rows") or [])} == {"C2"}

    export_resp = client.get(
        "/customers/cohorts/export",
        query_string={
            "dataset": "status_list",
            "status": "churned",
            "format": "csv",
            "threshold": "90",
            "lookback_months": "24",
        },
    )
    assert export_resp.status_code == 200
    csv_body = export_resp.get_data(as_text=True)
    assert "C2" in csv_body
    assert "C3" not in csv_body


def test_cohorts_v2_sticky_global_filters_apply_to_list_and_export(seed_customers_cohorts_v2, client, monkeypatch):
    client.application.config["COHORTS_V2"] = True
    client.application.config["COHORTS_HARDENED_V3"] = True

    seen_source = {}
    sticky_filters = normalize_filters({"customers": ["C2"], "start": "2025-01-01", "end": "2025-12-31"})

    def _fake_resolve_filters(_request, _user, *, source=None, **_kwargs):
        seen_source["keys"] = sorted(list(source.keys())) if hasattr(source, "keys") else []
        return sticky_filters, {"source": "session", "filters_source": "session", "filters_hash": "sticky-c2"}

    monkeypatch.setattr(customers_blueprint, "resolve_filters", _fake_resolve_filters)
    monkeypatch.setattr(
        customers_blueprint.filters_service,
        "scope_from_user",
        lambda _user: _scope_all(),
    )

    list_resp = client.get(
        "/customers/churned/list",
        query_string={"status": "churned", "threshold": "90", "lookback_months": "24"},
    )
    assert list_resp.status_code == 200
    list_payload = list_resp.get_json() or {}
    assert "status" not in seen_source.get("keys", [])
    assert {str(r.get("customer_id")) for r in (list_payload.get("rows") or [])} == {"C2"}

    export_resp = client.get(
        "/customers/cohorts/export",
        query_string={
            "dataset": "status_list",
            "status": "churned",
            "format": "csv",
            "threshold": "90",
            "lookback_months": "24",
        },
    )
    assert export_resp.status_code == 200
    csv_body = export_resp.get_data(as_text=True)
    assert "C2" in csv_body
    assert "C3" not in csv_body
