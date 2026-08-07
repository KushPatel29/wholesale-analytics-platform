from __future__ import annotations

import json
import re
import sys
from types import SimpleNamespace

import pandas as pd
import pytest
from flask import render_template

from app.auth.models import list_role_permissions, sync_permissions
from app.returns import blueprints as returns_blueprints
from app.returns import orders as returns_orders
from app.returns import service
from app.returns import suggestions
from app.returns.models import ReturnApproval, ReturnEvent, ReturnRMA, ReturnRMAItem, ReturnWebhookEvent, get_session


@pytest.fixture(autouse=True)
def _seed_returns_fact_parquet(app, tmp_path, monkeypatch):
    frame = pd.DataFrame(
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
                "OrderId": "ORD-1",
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
            }
        ]
    )
    parquet_path = tmp_path / "returns_fact.parquet"
    frame.to_parquet(parquet_path)
    monkeypatch.setenv("PARQUET_PATH", str(parquet_path))
    app.config["PARQUET_PATH"] = str(parquet_path)

    from app.services import fact_store

    fact_store.reset_duckdb_state()
    fact_store.init_views()
    app.config["FACT_SCHEMA_STATUS"] = fact_store.validate_fact_schema(strict=False)
    yield
    fact_store.reset_duckdb_state()


class _Scope:
    def __init__(self, payload):
        self._payload = payload

    def as_dict(self):
        return dict(self._payload)


class _Upload:
    def __init__(self, filename: str, payload: bytes, mimetype: str = "application/octet-stream"):
        self.filename = filename
        self._payload = payload
        self.mimetype = mimetype

    def read(self) -> bytes:
        return self._payload


def _make_order(order_id: str, customer_id: str, email: str = "buyer@example.com") -> dict[str, object]:
    return {
        "order_id": order_id,
        "customer_id": customer_id,
        "customer_name": f"Customer {customer_id}",
        "customer_email": email,
        "customer_phone": "555-1212",
    }


def _make_item(reason_code: str = "wrong_item") -> dict[str, object]:
    return {
        "order_line_id": "OL-1",
        "sku": "SKU-1",
        "product_name": "Test Product",
        "qty": 1,
        "price": 25.0,
        "reason_code": reason_code,
        "condition": "sealed",
        "notes": "test",
    }


def _make_user(role: str = "sales", user_id: int = 42) -> SimpleNamespace:
    return SimpleNamespace(
        is_authenticated=True,
        role=role,
        id=user_id,
        username=f"{role}.{user_id}",
        get_id=lambda: str(user_id),
    )


def _patch_permission_user(monkeypatch, user: SimpleNamespace, granted: set[str]) -> None:
    monkeypatch.setattr("app.core.rbac._get_user", lambda: user, raising=False)
    monkeypatch.setattr("flask_login.utils._get_user", lambda: user, raising=False)
    monkeypatch.setattr(
        "app.core.rbac.has_any_permission",
        lambda *perms, **kwargs: bool(set(perms).intersection(granted)),
        raising=False,
    )
    monkeypatch.setattr(
        "app.core.rbac.has_permission",
        lambda *perms, **kwargs: set(perms).issubset(granted),
        raising=False,
    )
    monkeypatch.setattr(
        "app.core.rbac.effective_permissions",
        lambda _user=None: set(granted),
        raising=False,
    )


def _extract_csrf_token(body: bytes) -> str:
    match = re.search(rb'name="csrf_token"\s+value="([^"]+)"', body)
    assert match is not None
    return match.group(1).decode("utf-8")


def test_returns_flag_blocks_routes(app, client):
    app.config.update(RETURNS_ENABLED=False, RETURNS_CUSTOMER_PORTAL_ENABLED=True)
    resp = client.get("/returns/lookup")
    assert resp.status_code == 404


def test_returns_routes_redirect_to_primary_login(app):
    app.config.update(RETURNS_ENABLED=True, RETURNS_CUSTOMER_PORTAL_ENABLED=True, LOGIN_DISABLED=False)
    with app.test_client() as client:
        resp = client.get("/returns/lookup")
    assert resp.status_code == 302
    assert "/login" in (resp.headers.get("Location") or "")


def test_returns_index_redirects_to_login_when_unauthenticated(app):
    app.config.update(RETURNS_ENABLED=True, LOGIN_DISABLED=False)
    with app.test_client() as client:
        resp = client.get("/returns")
    assert resp.status_code == 302
    assert "/login" in (resp.headers.get("Location") or "")


def test_returns_index_supports_both_slash_variants(app, monkeypatch):
    app.config.update(RETURNS_ENABLED=True, LOGIN_DISABLED=False, AUTHZ_DISABLED=False)
    user = _make_user(role="sales")
    monkeypatch.setattr(service, "scope_for_user", lambda _user, use_cache=True: _Scope({"is_admin": True, "scope_mode": "all"}))
    monkeypatch.setattr("app.core.rbac._get_user", lambda: user, raising=False)
    monkeypatch.setattr("flask_login.utils._get_user", lambda: user, raising=False)
    monkeypatch.setattr(
        "app.core.rbac.has_any_permission",
        lambda *args, **kwargs: "page.returns.view" in set(args),
        raising=False,
    )
    monkeypatch.setattr(
        "app.core.rbac.effective_permissions",
        lambda _user=None: {"page.returns.view"},
        raising=False,
    )
    with app.test_client() as client:
        resp = client.get("/returns")
        resp_slash = client.get("/returns/")
    assert resp.status_code == 200
    assert resp_slash.status_code == 200
    assert b"All Returns" in resp.data
    assert b"Returns" in resp.data


def test_returns_index_forbidden_without_permission(app, monkeypatch):
    app.config.update(RETURNS_ENABLED=True, LOGIN_DISABLED=False, AUTHZ_DISABLED=False)
    user = _make_user(role="viewer")
    monkeypatch.setattr("app.core.rbac._get_user", lambda: user, raising=False)
    monkeypatch.setattr("flask_login.utils._get_user", lambda: user, raising=False)
    monkeypatch.setattr("app.core.rbac.has_any_permission", lambda *args, **kwargs: False, raising=False)
    with app.test_client() as client:
        resp = client.get("/returns")
    assert resp.status_code == 403


def test_returns_new_excel_form_renders(app):
    app.config.update(
        RETURNS_ENABLED=True,
        RETURNS_CUSTOMER_PORTAL_ENABLED=True,
        RETURNS_UI_EXCEL_FORM=True,
        RETURNS_AUTOFILL_ORDER=True,
        LOGIN_DISABLED=True,
    )
    with app.test_client() as client:
        resp = client.get("/returns/new")
    assert resp.status_code == 200
    assert b"Submit New Return" in resp.data
    assert b'id="itemTemplate"' in resp.data
    assert b'name="csrf_token"' in resp.data
    assert b'data-filters-handler="disabled"' in resp.data
    assert b"event.preventDefault();" in resp.data


def test_returns_new_legacy_form_uses_lookup_button_and_enter_guard(app):
    app.config.update(
        RETURNS_ENABLED=True,
        RETURNS_CUSTOMER_PORTAL_ENABLED=True,
        RETURNS_UI_EXCEL_FORM=False,
        LOGIN_DISABLED=True,
    )
    with app.test_client() as client:
        resp = client.get("/returns/new")
    assert resp.status_code == 200
    assert b'name="csrf_token"' in resp.data
    assert b'id="legacyLoadOrderButton"' in resp.data
    assert b"lookupButton.click()" in resp.data


def test_returns_full_workflow_posts_succeed_with_csrf_tokens(app_client, monkeypatch):
    app = app_client.application
    app.config.update(
        RETURNS_ENABLED=True,
        RETURNS_CUSTOMER_PORTAL_ENABLED=True,
        RETURNS_UI_EXCEL_FORM=True,
        RETURNS_AUTOFILL_ORDER=True,
        RETURNS_ANALYTICS=True,
        LOGIN_DISABLED=True,
        WTF_CSRF_ENABLED=True,
    )
    monkeypatch.setattr(
        service,
        "lookup_order_for_return",
        lambda order_id, actor_user=None: {
            "order": {
                "order_id": "ORD-CSRF-1",
                "customer_id": "C-CSRF-1",
                "customer_name": "CSRF Customer",
                "date_shipped": "2026-03-03",
                "order_date": "2026-03-03",
            },
            "items": [],
            "suggestions": [],
            "meta": {},
        },
    )
    monkeypatch.setattr(service, "_send_rma_bulk_email", lambda *args, **kwargs: True)

    client = app_client
    new_resp = client.get("/returns/new")
    assert new_resp.status_code == 200
    csrf_token = _extract_csrf_token(new_resp.data)

    create_resp = client.post(
        "/returns/new",
        data={
            "csrf_token": csrf_token,
            "manual_order_id": "ORD-CSRF-1",
            "customer_id": "C-CSRF-1",
            "customer_name": "CSRF Customer",
            "date_shipped": "2026-03-03",
            "return_type": "Sales Return",
            "product_code[]": ["SKU-CSRF-1"],
            "product_desc[]": ["CSRF Product"],
            "price_per_lb[]": ["9.00"],
            "weight_lb[]": ["2.000"],
            "credit_amount[]": ["0.00"],
            "product_returning[]": ["1"],
            "reason_for_return[]": ["customer_return"],
            "follow_up_action[]": ["Credit"],
            "supplier_credit[]": ["0"],
        },
        follow_redirects=False,
    )
    assert create_resp.status_code == 302
    location = create_resp.headers.get("Location") or ""
    assert "/returns/" in location
    rma_id = int(location.rstrip("/").rsplit("/", 1)[-1])

    detail_resp = client.get(f"/returns/{rma_id}")
    assert detail_resp.status_code == 200
    assert detail_resp.data.count(b'name="csrf_token"') >= 1
    approve_wh_token = _extract_csrf_token(detail_resp.data)

    wh_resp = client.post(
        f"/returns/{rma_id}/approve_wh",
        data={"csrf_token": approve_wh_token, "return_to": "detail"},
        follow_redirects=False,
    )
    assert wh_resp.status_code == 302

    detail_resp = client.get(f"/returns/{rma_id}")
    assert detail_resp.status_code == 200
    approve_mgr_token = _extract_csrf_token(detail_resp.data)

    mgr_resp = client.post(
        f"/returns/{rma_id}/approve_mgr",
        data={"csrf_token": approve_mgr_token},
        follow_redirects=False,
    )
    assert mgr_resp.status_code == 200
    assert mgr_resp.mimetype == "application/pdf"
    assert mgr_resp.get_data().startswith(b"%PDF-")

    pdf_resp = client.get(f"/returns/{rma_id}/pdf")

    assert pdf_resp.status_code == 200
    assert pdf_resp.mimetype == "application/pdf"

    with app.app_context():
        detail = service.get_rma_detail(rma_id)
        snapshot = service.returns_analytics_snapshot()
        assert detail is not None
        assert detail["status"] == service.STATUS_APPROVED
        assert snapshot["summary"]["total_returns"] >= 1


def test_returns_new_route_requires_returns_create_permission(app, monkeypatch):
    app.config.update(RETURNS_ENABLED=True, RETURNS_CUSTOMER_PORTAL_ENABLED=True, LOGIN_DISABLED=False, AUTHZ_DISABLED=False)
    user = _make_user(role="sales", user_id=77)
    monkeypatch.setattr("app.core.rbac._get_user", lambda: user, raising=False)
    monkeypatch.setattr("flask_login.utils._get_user", lambda: user, raising=False)

    monkeypatch.setattr(
        "app.core.rbac.has_any_permission",
        lambda *args, **kwargs: "returns.create" in set(args),
        raising=False,
    )
    with app.test_client() as client:
        allowed_resp = client.get("/returns/new")
    assert allowed_resp.status_code == 200

    monkeypatch.setattr(
        "app.core.rbac.has_any_permission",
        lambda *args, **kwargs: "page.returns.customer_portal" in set(args),
        raising=False,
    )
    with app.test_client() as client:
        denied_resp = client.get("/returns/new")
    assert denied_resp.status_code == 403


def test_returns_new_route_allows_admin_manage_permission(app, monkeypatch):
    app.config.update(RETURNS_ENABLED=True, RETURNS_CUSTOMER_PORTAL_ENABLED=True, LOGIN_DISABLED=False, AUTHZ_DISABLED=False)
    user = _make_user(role="admin", user_id=91)
    monkeypatch.setattr("app.core.rbac._get_user", lambda: user, raising=False)
    monkeypatch.setattr("flask_login.utils._get_user", lambda: user, raising=False)

    monkeypatch.setattr(
        "app.core.rbac.has_any_permission",
        lambda *args, **kwargs: "admin.returns.manage" in set(args),
        raising=False,
    )
    with app.test_client() as client:
        resp = client.get("/returns/new")

    assert resp.status_code == 200


def test_returns_role_permission_supersets_are_seeded(app):
    app.config.update(RETURNS_ENABLED=True, RETURNS_FINAL_V1=True, LOGIN_DISABLED=True)
    with app.app_context():
        sync_permissions()
        sales = set(list_role_permissions("sales"))
        manager = set(list_role_permissions("sales_manager"))
        admin = set(list_role_permissions("admin"))
        warehouse = set(list_role_permissions("warehouse"))

    assert {"page.returns.view", "returns.create", "export.returns", "returns.pdf.export"}.issubset(sales)
    assert sales.issubset(manager)
    assert warehouse.issubset(manager)
    assert "returns.approve.mgr" in manager
    assert manager.issubset(admin)
    assert "admin.returns.manage" in admin
    app.config.update(RETURNS_FINAL_V1=False)


def test_manager_and_admin_can_access_sales_returns_routes(app, monkeypatch):
    app.config.update(
        RETURNS_ENABLED=True,
        RETURNS_CUSTOMER_PORTAL_ENABLED=True,
        RETURNS_FINAL_V1=True,
        RETURNS_ANALYTICS=True,
        LOGIN_DISABLED=False,
        AUTHZ_DISABLED=False,
    )

    manager_perms = set(list_role_permissions("sales_manager"))
    admin_perms = set(list_role_permissions("admin"))

    for role_name, granted in (("sales_manager", manager_perms), ("admin", admin_perms)):
        user = _make_user(role=role_name, user_id=100 if role_name == "sales_manager" else 101)
        _patch_permission_user(monkeypatch, user, granted)
        with app.test_client() as client:
            assert client.get("/returns").status_code == 200
            assert client.get("/returns/new").status_code == 200
            assert client.get("/returns/approvals").status_code == 200
            assert client.get("/returns/analytics").status_code == 200
    app.config.update(RETURNS_FINAL_V1=False, RETURNS_ANALYTICS=False)


def test_sales_permissions_do_not_include_approvals_or_warehouse_actions(app, monkeypatch):
    app.config.update(
        RETURNS_ENABLED=True,
        RETURNS_CUSTOMER_PORTAL_ENABLED=True,
        RETURNS_FINAL_V1=True,
        RETURNS_ANALYTICS=True,
        LOGIN_DISABLED=False,
        AUTHZ_DISABLED=False,
    )
    granted = set(list_role_permissions("sales"))
    user = _make_user(role="sales", user_id=102)
    _patch_permission_user(monkeypatch, user, granted)
    with app.test_client() as client:
        assert client.get("/returns").status_code == 200
        assert client.get("/returns/new").status_code == 200
        assert client.get("/returns/analytics").status_code == 200
        assert client.get("/returns/approvals").status_code == 403
    app.config.update(RETURNS_FINAL_V1=False, RETURNS_ANALYTICS=False)


def test_returns_new_lookup_submit_stays_inline_on_scope_error(app, monkeypatch):
    app.config.update(
        RETURNS_ENABLED=True,
        RETURNS_CUSTOMER_PORTAL_ENABLED=True,
        RETURNS_AUTOFILL_ORDER=True,
        RETURNS_UI_EXCEL_FORM=False,
        LOGIN_DISABLED=True,
    )

    def _deny_lookup(order_id, actor_user=None):
        raise service.ScopeViolationError("Order not in your scope.")

    monkeypatch.setattr(service, "lookup_order_for_return", _deny_lookup)

    with app.test_client() as client:
        resp = client.post(
            "/returns/new",
            data={
                "manual_order_id": "ORD-OUT-403",
                "lookup_order": "1",
            },
        )

    assert resp.status_code == 200
    assert b"Order not in your scope." in resp.data
    assert b"You do not have permission to access this page." not in resp.data


def test_returns_analytics_page_renders_and_exports(app):
    app.config.update(
        RETURNS_ENABLED=True,
        RETURNS_ANALYTICS=True,
        LOGIN_DISABLED=True,
    )
    analytics_payload = {
        "summary": {
            "total_returns": 2,
            "total_credit_amount": 123.45,
            "total_weight_lb": 5.25,
            "total_packs": 3,
            "supplier_credit_pct": 50.0,
        },
        "frames": {
            "volume_by_week": pd.DataFrame([{"week": "2026-03-02/2026-03-08", "return_count": 2}]),
            "credit_by_week": pd.DataFrame([{"week": "2026-03-02/2026-03-08", "total_credit_amount": 123.45}]),
            "top_customers": pd.DataFrame([{"customer_name": "Customer A", "customer_id": "C-1", "total_credit_amount": 123.45, "return_count": 2}]),
            "top_skus": pd.DataFrame([{"product_code": "SKU-1", "total_credit_amount": 123.45, "return_count": 2}]),
            "reason_breakdown": pd.DataFrame([{"reason_for_return": "Damaged", "reason_code": "damaged", "total_credit_amount": 123.45}]),
            "category_breakdown": pd.DataFrame([{"category": "Warehouse", "total_credit_amount": 123.45}]),
            "approval_sla": pd.DataFrame([{"metric": "Pending -> WH Approved", "median_hours": 1.2, "p90_hours": 3.4}]),
            "follow_up_breakdown": pd.DataFrame([{"follow_up_action": "Restock", "line_count": 2}]),
            "approval_detail": pd.DataFrame([{"rma_id": 1}]),
            "headers": pd.DataFrame([{"rma_id": 1}]),
            "volume_by_month": pd.DataFrame([{"month": "2026-03", "return_count": 2}]),
            "credit_by_month": pd.DataFrame([{"month": "2026-03", "total_credit_amount": 123.45}]),
            "supplier_credit": pd.DataFrame([{"supplier_credit_pct": 0.5}]),
        },
    }
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(service, "returns_analytics_snapshot", lambda actor_user=None: analytics_payload)
    monkeypatch.setattr(service, "returns_analytics_export_response", lambda **kwargs: app.response_class("xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
    try:
        with app.test_client() as client:
            resp = client.get("/returns/analytics")
            export_resp = client.get("/returns/analytics?export=xlsx")
        assert resp.status_code == 200
        assert b"Returns Analytics" in resp.data
        assert export_resp.status_code == 200
    finally:
        monkeypatch.undo()


def test_returns_state_machine_valid_and_invalid(app):
    app.config.update(
        RETURNS_ENABLED=True,
        RETURNS_CUSTOMER_PORTAL_ENABLED=True,
        RETURNS_LABELS_ENABLED=True,
        LOGIN_DISABLED=True,
    )
    with app.app_context():
        created = service.create_rma(order_payload=_make_order("ORD-100", "C-100"), item_payloads=[_make_item("wrong_item")])
        assert created["status"] == service.STATUS_AUTO_APPROVED

        progressed = service.transition_rma(created["id"], service.STATUS_AWAITING_RETURN)
        assert progressed["status"] == service.STATUS_AWAITING_RETURN

        with pytest.raises(service.InvalidTransitionError):
            service.transition_rma(created["id"], service.STATUS_REQUESTED)


def test_returns_tracker_rollups_populate_header_fields(app):
    app.config.update(RETURNS_ENABLED=True, RETURNS_CUSTOMER_PORTAL_ENABLED=True, LOGIN_DISABLED=True)
    with app.app_context():
        created = service.create_rma(
            order_payload={
                **_make_order("ORD-ROLLUP-1", "C-ROLLUP-1"),
                "workflow_mode": "legacy",
                "order_date": "2026-03-01",
            },
            item_payloads=[
                {
                    **_make_item("damaged"),
                    "weight_lb": 2.5,
                    "price_per_lb": 12.0,
                    "packs_count": 3,
                    "follow_up_action": "Restock",
                }
            ],
            workflow_mode="legacy",
        )

    assert created["rma_number"].startswith("RMA-")
    assert created["total_credit_amount"] == 30.0
    assert created["total_weight_lb"] == 2.5
    assert created["total_packs"] == 3
    assert created["primary_reason"] == "damaged"
    assert created["primary_category"] == "Warehouse"
    assert created["primary_follow_up"] == "Restock"


def test_returns_tracker_export_route_includes_all_matching_rows(app):
    app.config.update(RETURNS_ENABLED=True, LOGIN_DISABLED=True)
    with app.app_context():
        service.create_rma(
            order_payload={**_make_order("ORD-EXP-1", "C-EXP-1"), "workflow_mode": "legacy"},
            item_payloads=[_make_item("damaged")],
            workflow_mode="legacy",
        )
        service.create_rma(
            order_payload={**_make_order("ORD-EXP-2", "C-EXP-2"), "workflow_mode": "legacy"},
            item_payloads=[_make_item("wrong_item")],
            workflow_mode="legacy",
        )

    with app.test_client() as client:
        resp = client.get("/returns?export=csv&status=all")

    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    payload = resp.data.decode("utf-8")
    assert "RMA #,Customer,Order #,Rep,Order Date,Date Submitted,Status" in payload
    assert "ORD-EXP-1" in payload
    assert "ORD-EXP-2" in payload


def test_returns_settings_round_trip(app):
    app.config.update(RETURNS_ENABLED=True, LOGIN_DISABLED=True)
    with app.app_context():
        saved = service.save_returns_settings(
            {
                "defaults": {"return_type": "Vendor Return", "follow_up_action": "Restock"},
                "email_templates": {"new_return_subject": "Return {{ order_id }}"},
            }
        )
        loaded = service.get_returns_settings()

    assert saved["defaults"]["return_type"] == "Vendor Return"
    assert loaded["defaults"]["follow_up_action"] == "Restock"
    assert loaded["email_templates"]["new_return_subject"] == "Return {{ order_id }}"


def test_returns_rbac_route_enforced(app, monkeypatch):
    app.config.update(RETURNS_ENABLED=True, LOGIN_DISABLED=False, AUTHZ_DISABLED=False)
    user = SimpleNamespace(is_authenticated=True, role="sales", id=42)
    monkeypatch.setattr("app.core.rbac._get_user", lambda: user, raising=False)
    monkeypatch.setattr("flask_login.utils._get_user", lambda: user, raising=False)
    monkeypatch.setattr("app.core.rbac.has_any_permission", lambda *args, **kwargs: False, raising=False)
    with app.test_client() as client:
        resp = client.get("/returns/ops/queue")
    assert resp.status_code == 403


def test_returns_only_user_can_access_returns_but_not_other_pages(app, monkeypatch):
    app.config.update(RETURNS_ENABLED=True, RETURNS_CUSTOMER_PORTAL_ENABLED=True, LOGIN_DISABLED=False, AUTHZ_DISABLED=False)
    user = SimpleNamespace(
        is_authenticated=True,
        role="returns_only",
        returns_only=True,
        id=4242,
        username="returns.only",
        get_id=lambda: "4242",
    )
    monkeypatch.setattr("app.core.rbac._get_user", lambda: user, raising=False)
    monkeypatch.setattr("flask_login.utils._get_user", lambda: user, raising=False)
    monkeypatch.setattr("app.core.rbac.has_any_permission", lambda *args, **kwargs: True, raising=False)
    monkeypatch.setattr("app.core.rbac.effective_permissions", lambda _user=None: {"*"}, raising=False)

    with app.test_client() as client:
        returns_resp = client.get("/returns")
        overview_resp = client.get("/overview")
        customers_resp = client.get("/customers/")
        products_resp = client.get("/products/")
        regions_resp = client.get("/regions/")
        suppliers_resp = client.get("/suppliers/")
        salesreps_resp = client.get("/salesreps/")

    assert returns_resp.status_code == 200
    assert overview_resp.status_code == 403
    assert customers_resp.status_code == 403
    assert products_resp.status_code == 403
    assert regions_resp.status_code == 403
    assert suppliers_resp.status_code == 403
    assert salesreps_resp.status_code == 403


def test_warehouse_scan_route_requires_permission(app, monkeypatch):
    app.config.update(RETURNS_ENABLED=True, LOGIN_DISABLED=False, AUTHZ_DISABLED=False)
    user = _make_user(role="sales", user_id=77)
    _patch_permission_user(monkeypatch, user, set())
    with app.test_client() as client:
        resp = client.get("/returns/wh/scan")
    assert resp.status_code == 403


def test_warehouse_scan_parses_rma_prefixed_values(app, monkeypatch):
    app.config.update(RETURNS_ENABLED=True, LOGIN_DISABLED=False, AUTHZ_DISABLED=False)
    user = _make_user(role="warehouse", user_id=88)
    _patch_permission_user(monkeypatch, user, set(list_role_permissions("warehouse")))
    monkeypatch.setattr(service, "get_rma_detail", lambda rma_id, actor_user=None: {"id": int(rma_id)})

    with app.test_client() as client:
        resp = client.post("/returns/wh/scan", data={"rma_id": "RMA-123"}, follow_redirects=False)

    assert resp.status_code == 302
    assert (resp.headers.get("Location") or "").endswith("/returns/wh/123")
    assert returns_blueprints._parse_scanned_rma_id("RMA-123") == 123
    assert returns_blueprints._parse_scanned_rma_id("RETURN-123") == 123


def test_sales_scope_filters_return_visibility(app, monkeypatch):
    app.config.update(RETURNS_ENABLED=True, LOGIN_DISABLED=True)
    with app.app_context():
        visible = service.create_rma(order_payload=_make_order("ORD-201", "C-1"), item_payloads=[_make_item("wrong_item")])
        hidden = service.create_rma(order_payload=_make_order("ORD-202", "C-2"), item_payloads=[_make_item("wrong_item")])

        scope_payload = {
            "is_admin": False,
            "scope_mode": "list",
            "allowed_customer_ids": ["C-1"],
            "allowed_erp_user_ids": [],
            "allowed_region_ids": [],
            "allowed_supplier_ids": [],
        }
        monkeypatch.setattr(service, "get_current_scope", lambda use_cache=True: _Scope(scope_payload))

        rows = service.list_rmas()
        row_ids = {row["id"] for row in rows}
        assert visible["id"] in row_ids
        assert hidden["id"] not in row_ids

        with pytest.raises(service.ScopeViolationError):
            service.get_rma_detail(hidden["id"])


def test_attachment_upload_stays_inside_returns_dir(app, tmp_path):
    app.config.update(RETURNS_ENABLED=True, RETURNS_UPLOAD_DIR=str(tmp_path / "returns_uploads"), LOGIN_DISABLED=True)
    with app.app_context():
        created = service.create_rma(order_payload=_make_order("ORD-301", "C-301"), item_payloads=[_make_item("wrong_item")])
        saved = service.save_attachment(
            rma_id=created["id"],
            upload=_Upload("../../evil.txt", b"hello"),
        )

        assert ".." not in saved["file_path"]
        assert saved["file_path"].endswith("evil.txt")
        assert (tmp_path / "returns_uploads" / saved["file_path"]).exists()


def test_list_rmas_batches_scope_customer_lookup_when_scope_is_not_preexpanded(app, monkeypatch):
    app.config.update(RETURNS_ENABLED=True, LOGIN_DISABLED=True)
    with app.app_context():
        visible = service.create_rma(order_payload=_make_order("ORD-211", "C-211"), item_payloads=[_make_item("wrong_item")])
        hidden = service.create_rma(order_payload=_make_order("ORD-212", "C-212"), item_payloads=[_make_item("wrong_item")])

        scope_payload = {
            "is_admin": False,
            "scope_mode": "derived",
            "allowed_customer_ids": [],
            "allowed_erp_user_ids": ["REP-1"],
            "allowed_region_ids": [],
            "allowed_supplier_ids": [],
        }
        batch_calls: list[list[str]] = []

        monkeypatch.setattr(service, "get_current_scope", lambda use_cache=True: _Scope(scope_payload))

        def _fake_lookup(customer_ids, payload):
            assert payload["scope_mode"] == "derived"
            batch_calls.append(sorted({str(item).strip().lower() for item in customer_ids if str(item).strip()}))
            return {"c-211"}

        monkeypatch.setattr(service, "_lookup_customers_in_scope", _fake_lookup)
        monkeypatch.setattr(
            service,
            "_lookup_customer_exists_in_scope",
            lambda *_args, **_kwargs: pytest.fail("list_rmas should not fall back to per-row scope lookups"),
        )

        rows = service.list_rmas()
        row_ids = {row["id"] for row in rows}

    assert visible["id"] in row_ids
    assert hidden["id"] not in row_ids
    assert len(batch_calls) == 1
    assert {"c-211", "c-212"}.issubset(set(batch_calls[0]))


def test_attachment_upload_enforces_customer_scope(app, tmp_path, monkeypatch):
    app.config.update(RETURNS_ENABLED=True, RETURNS_UPLOAD_DIR=str(tmp_path / "returns_uploads"), LOGIN_DISABLED=True)
    with app.app_context():
        created = service.create_rma(order_payload=_make_order("ORD-302", "C-302"), item_payloads=[_make_item("wrong_item")])
        scope_payload = {
            "is_admin": False,
            "scope_mode": "list",
            "allowed_customer_ids": ["C-999"],
            "allowed_erp_user_ids": [],
            "allowed_region_ids": [],
            "allowed_supplier_ids": [],
        }
        monkeypatch.setattr(service, "get_current_scope", lambda use_cache=True: _Scope(scope_payload))
        with pytest.raises(service.ScopeViolationError):
            service.save_attachment(
                rma_id=created["id"],
                upload=_Upload("blocked.txt", b"hello"),
            )


def test_email_notifications_sent_on_create_and_transition(app, monkeypatch):
    app.config.update(RETURNS_ENABLED=True, RETURNS_CUSTOMER_PORTAL_ENABLED=True, LOGIN_DISABLED=True)
    deliveries = []

    def _fake_send(*args, **kwargs):
        deliveries.append({"args": args, "kwargs": kwargs})
        return True

    monkeypatch.setattr(service, "send_email", _fake_send)
    with app.app_context():
        created = service.create_rma(order_payload=_make_order("ORD-401", "C-401"), item_payloads=[_make_item("wrong_item")])
        service.transition_rma(created["id"], service.STATUS_AWAITING_RETURN)

    assert len(deliveries) >= 2
    subjects = [item["args"][1] for item in deliveries]
    assert any("Return requested" in subject for subject in subjects)
    assert any("approved" in subject.lower() for subject in subjects)


def test_webhook_idempotency_processed_once(app):
    app.config.update(RETURNS_ENABLED=True, LOGIN_DISABLED=True)
    with app.app_context():
        created = service.create_rma(order_payload=_make_order("ORD-501", "C-501"), item_payloads=[_make_item("wrong_item")])
        service.transition_rma(created["id"], service.STATUS_AWAITING_RETURN)
        key = f"event-{created['id']}"

        first, created_first = service.process_webhook(
            source="carrier",
            event_type="tracking.received",
            idempotency_key=key,
            payload={"rma_id": created["id"], "event_type": "tracking.received"},
        )
        second, created_second = service.process_webhook(
            source="carrier",
            event_type="tracking.received",
            idempotency_key=key,
            payload={"rma_id": created["id"], "event_type": "tracking.received"},
        )

        assert created_first is True
        assert created_second is False
        assert first["id"] == second["id"]

        with get_session() as session:
            rows = session.query(ReturnWebhookEvent).filter(ReturnWebhookEvent.idempotency_key == key).all()
            assert len(rows) == 1


def test_ops_action_requires_granular_permission(app, monkeypatch):
    app.config.update(RETURNS_ENABLED=True, LOGIN_DISABLED=True, AUTHZ_DISABLED=False)
    with app.app_context():
        created = service.create_rma(order_payload=_make_order("ORD-601", "C-601"), item_payloads=[_make_item("wrong_item")])
    app.config.update(LOGIN_DISABLED=False)

    user = SimpleNamespace(is_authenticated=True, role="sales_manager", id=42)

    def _fake_has_any_permission(*perms, **kwargs):
        wanted = set(perms)
        if "returns.ops.queue.view" in wanted or "page.returns.ops" in wanted:
            return True
        if "returns.ops.approve" in wanted or "returns.approve" in wanted:
            return False
        return False

    monkeypatch.setattr("app.core.rbac._get_user", lambda: user, raising=False)
    monkeypatch.setattr("flask_login.utils._get_user", lambda: user, raising=False)
    monkeypatch.setattr("app.core.rbac.has_any_permission", _fake_has_any_permission, raising=False)

    with app.test_client() as client:
        resp = client.post(f"/returns/ops/{created['id']}", data={"action": "approve", "notes": "approve"})
    assert resp.status_code == 403


def test_returns_webhook_route_skips_login_redirect(app):
    app.config.update(RETURNS_ENABLED=True, LOGIN_DISABLED=False)
    with app.test_client() as client:
        resp = client.post("/returns/webhooks/carrier", json={"event_type": "noop"})
    assert resp.status_code == 200


def test_returns_order_lookup_api_returns_stable_json(app, monkeypatch):
    app.config.update(RETURNS_ENABLED=True, RETURNS_CUSTOMER_PORTAL_ENABLED=True, RETURNS_V2=True, LOGIN_DISABLED=False, AUTHZ_DISABLED=False)
    user = _make_user(role="sales")
    monkeypatch.setattr("app.core.rbac._get_user", lambda: user, raising=False)
    monkeypatch.setattr("flask_login.utils._get_user", lambda: user, raising=False)
    monkeypatch.setattr("app.core.rbac.has_any_permission", lambda *args, **kwargs: True, raising=False)

    fixture_payload = {
        "order": {
            "order_id": "ORD-API-1",
            "customer_id": "C-API",
            "customer_name": "API Customer",
            "order_date": "2026-03-01",
            "ship_to": "Seattle",
            "status": "delivered",
            "total_revenue": 120.0,
        },
        "items": [
            {
                "order_line_id": "L-1",
                "sku": "SKU-1",
                "description": "Striploin",
                "qty_ordered": 2,
                "qty_shipped": 2,
                "unit_price": 60.0,
                "revenue": 120.0,
                "cost": 70.0,
                "margin": 50.0,
            }
        ],
        "suggestions": [
            {
                "order_line_id": "L-1",
                "selected": True,
                "suggested_return_qty": 2,
                "suggested_reason": "quality_issue",
                "suggested_condition": "opened",
                "rationale": "Within return window.",
            }
        ],
        "meta": {"lookup_ms": 12.3, "dataset_version": "v1", "scope_hash": "scope-1"},
    }
    monkeypatch.setattr(service, "lookup_order_for_return", lambda order_id, actor_user=None: fixture_payload)

    with app.test_client() as client:
        resp = client.get("/returns/api/order/ORD-API-1")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["order"]["order_id"] == "ORD-API-1"
    assert payload["items"][0]["order_line_id"] == "L-1"
    assert payload["suggestions"][0]["suggested_reason"] == "quality_issue"
    assert payload["meta"]["cached"] is False


def test_returns_order_lookup_api_respects_scope(app, monkeypatch):
    app.config.update(RETURNS_ENABLED=True, RETURNS_CUSTOMER_PORTAL_ENABLED=True, RETURNS_V2=True, LOGIN_DISABLED=False, AUTHZ_DISABLED=False)
    user = _make_user(role="sales")
    monkeypatch.setattr("app.core.rbac._get_user", lambda: user, raising=False)
    monkeypatch.setattr("flask_login.utils._get_user", lambda: user, raising=False)
    monkeypatch.setattr("app.core.rbac.has_any_permission", lambda *args, **kwargs: True, raising=False)

    def _deny(order_id, actor_user=None):
        raise service.ScopeViolationError("That order is outside your authorized customer scope.")

    monkeypatch.setattr(service, "lookup_order_for_return", _deny)
    with app.test_client() as client:
        resp = client.get("/returns/api/order/ORD-OUT-1")

    assert resp.status_code == 404
    payload = resp.get_json()
    assert payload["error"] == "not_in_scope"


def test_returns_order_lookup_api_empty_scope_message(app, monkeypatch):
    app.config.update(RETURNS_ENABLED=True, RETURNS_CUSTOMER_PORTAL_ENABLED=True, RETURNS_V2=True, LOGIN_DISABLED=False, AUTHZ_DISABLED=False)
    user = _make_user(role="sales")
    monkeypatch.setattr("app.core.rbac._get_user", lambda: user, raising=False)
    monkeypatch.setattr("flask_login.utils._get_user", lambda: user, raising=False)
    monkeypatch.setattr("app.core.rbac.has_any_permission", lambda *args, **kwargs: True, raising=False)

    def _deny(order_id, actor_user=None):
        raise service.ScopeViolationError("Your account has no customer access configured. Contact admin.")

    monkeypatch.setattr(service, "lookup_order_for_return", _deny)
    with app.test_client() as client:
        resp = client.get("/returns/api/order/ORD-EMPTY-1")

    assert resp.status_code == 403
    payload = resp.get_json()
    assert payload["error"] == "scope_not_configured"


def test_returns_order_search_handles_nullable_int_columns(monkeypatch):
    frame = pd.DataFrame(
        {
            "order_id": ["ORD-NULL-1"],
            "customer_id": ["C-NULL"],
            "customer_name": ["Null Customer"],
            "customer_email": [pd.NA],
            "customer_phone": [pd.NA],
            "order_date": [pd.NaT],
            "order_total": [12.5],
            "line_count": pd.Series([pd.NA], dtype="Int32"),
        }
    )

    monkeypatch.setattr(
        returns_orders.fact_store,
        "list_columns",
        lambda conn=None: {"OrderId", "CustomerId", "CustomerName", "Revenue"},
    )
    monkeypatch.setattr(returns_orders.fact_store, "execute_sql_df", lambda *args, **kwargs: frame)

    rows = returns_orders.search_orders(order_id="ORD-NULL-1")

    assert len(rows) == 1
    assert rows[0]["order_id"] == "ORD-NULL-1"
    assert rows[0]["line_count"] is None


def test_returns_order_detail_handles_nullable_int_columns(monkeypatch):
    frame = pd.DataFrame(
        {
            "order_id": ["ORD-DET-1"],
            "customer_id": ["C-DET"],
            "customer_name": ["Detail Customer"],
            "customer_email": [pd.NA],
            "customer_phone": [pd.NA],
            "ship_to": [pd.NA],
            "order_status": ["Complete"],
            "order_date": ["2026-03-01"],
            "order_line_id": [pd.NA],
            "sku": ["SKU-DET-1"],
            "product_name": ["Detail Product"],
            "pack": [pd.NA],
            "category": [pd.NA],
            "qty_ordered": pd.Series([pd.NA], dtype="Int32"),
            "qty_shipped": pd.Series([2], dtype="Int32"),
            "revenue": [40.0],
            "cost": [24.0],
            "margin": [16.0],
        }
    )

    monkeypatch.setattr(
        returns_orders.fact_store,
        "list_columns",
        lambda conn=None: {
            "OrderId",
            "CustomerId",
            "CustomerName",
            "OrderStatus",
            "Date",
            "SKU",
            "ProductName",
            "QtyOrdered",
            "QtyShipped",
            "Revenue",
            "Cost",
            "Margin",
        },
    )
    monkeypatch.setattr(returns_orders.fact_store, "execute_sql_df", lambda *args, **kwargs: frame)

    detail = returns_orders.get_order_detail("ORD-DET-1")

    assert detail is not None
    assert detail["order"]["order_id"] == "ORD-DET-1"
    assert detail["items"][0]["order_line_id"] == "ORD-DET-1:1"
    assert detail["items"][0]["qty_ordered"] == 2.0


def test_returns_order_detail_normalizes_weighted_line_price_per_lb_and_weight(monkeypatch):
    frame = pd.DataFrame(
        {
            "order_id": ["459977"],
            "customer_id": ["C-WEIGHT"],
            "customer_name": ["Weighted Customer"],
            "customer_email": ["weighted@example.com"],
            "customer_phone": ["555-0000"],
            "ship_to": ["Seattle"],
            "order_status": ["Complete"],
            "order_date": ["2026-03-01"],
            "order_line_id": ["1767950"],
            "sku": ["SKU-WEIGHT-1"],
            "product_name": ["Weighted Product"],
            "pack": [pd.NA],
            "category": [pd.NA],
            "billing_uom": ["3"],
            "qty_ordered": [5.0],
            "qty_shipped": [0.0],
            "weight_lb_source": [5.34],
            "pack_weight_lb_source": [0.0],
            "price_per_lb_source": [pd.NA],
            "price_source": [20.65],
            "revenue": [110.271],
            "cost": [70.0],
            "margin": [40.271],
        }
    )

    monkeypatch.setattr(
        returns_orders.fact_store,
        "list_columns",
        lambda conn=None: {
            "OrderId",
            "CustomerId",
            "CustomerName",
            "Email",
            "Phone",
            "ShipToName",
            "OrderStatus",
            "Date",
            "OrderLineId",
            "SKU",
            "ProductName",
            "QuantityOrdered",
            "QuantityShipped",
            "WeightLb",
            "Revenue",
            "Cost",
            "Margin",
            "Price",
            "UnitOfBillingId",
        },
    )
    monkeypatch.setattr(returns_orders.fact_store, "execute_sql_df", lambda *args, **kwargs: frame)

    detail = returns_orders.get_order_detail("459977")

    assert detail is not None
    item = detail["items"][0]
    assert item["qty_ordered"] == pytest.approx(5.0, rel=0, abs=1e-6)
    assert item["qty_shipped"] == pytest.approx(0.0, rel=0, abs=1e-6)
    assert item["weight_lb"] == pytest.approx(5.34, rel=0, abs=1e-6)
    assert item["price_per_lb"] == pytest.approx(20.65, rel=0, abs=1e-6)
    assert item["credit_amount"] == pytest.approx(110.27, rel=0, abs=1e-6)
    assert item["mapping_warning"] is None
    assert item["price_source"] == "price"
    assert item["weight_source"] == "weight_lb"


def test_lookup_payload_to_form_rows_keeps_weight_lb_separate_from_qty():
    rows = returns_blueprints._lookup_payload_to_form_rows(
        {
            "items": [
                {
                    "order_line_id": "LINE-1",
                    "sku": "SKU-1",
                    "product_name": "Weighted Product",
                    "price_per_lb": 62.41,
                    "weight_lb": 18.0,
                    "qty_ordered": 5.0,
                    "qty_shipped": 1.0,
                }
            ],
            "suggestions": [
                {
                    "order_line_id": "LINE-1",
                    "selected": True,
                    "suggested_return_qty": 1.0,
                    "rationale": "Suggested for review.",
                }
            ],
        }
    )

    assert len(rows) == 1
    assert rows[0]["qty"] == pytest.approx(1.0, rel=0, abs=1e-6)
    assert rows[0]["weight_lb"] == pytest.approx(18.0, rel=0, abs=1e-6)
    assert rows[0]["price_per_lb"] == pytest.approx(62.41, rel=0, abs=1e-6)


def test_lookup_payload_to_form_rows_does_not_invent_weight_from_qty():
    rows = returns_blueprints._lookup_payload_to_form_rows(
        {
            "items": [
                {
                    "order_line_id": "LINE-2",
                    "sku": "SKU-2",
                    "product_name": "Missing Weight Product",
                    "price_per_lb": 10.0,
                    "qty_ordered": 4.0,
                    "qty_shipped": 4.0,
                }
            ],
            "suggestions": [
                {
                    "order_line_id": "LINE-2",
                    "selected": True,
                    "suggested_return_qty": 2.0,
                }
            ],
        }
    )

    assert len(rows) == 1
    assert rows[0]["qty"] == pytest.approx(2.0, rel=0, abs=1e-6)
    assert rows[0]["weight_lb"] == pytest.approx(0.0, rel=0, abs=1e-6)


def test_returns_suggestion_engine_is_deterministic(app, monkeypatch):
    app.config.update(RETURNS_MARGIN_TARGET=0.27, RETURNS_FREQUENT_THRESHOLD=2, RETURNS_POLICY_DAYS=14)
    monkeypatch.setattr(suggestions, "_frequent_return_counts", lambda skus: {"sku-perishable": 3, "sku-low": 0})
    order = {"order_id": "ORD-SUG-1", "customer_id": "C-SUG", "order_date": "2026-03-01"}
    items = [
        {
            "order_line_id": "L-1",
            "sku": "SKU-PERISHABLE",
            "description": "Beef Ribeye",
            "qty_ordered": 2,
            "qty_shipped": 2,
            "revenue": 100,
            "cost": 90,
            "margin": 10,
        },
        {
            "order_line_id": "L-2",
            "sku": "SKU-LOW",
            "description": "Shelf Stable",
            "qty_ordered": 4,
            "qty_shipped": 2,
            "revenue": 20,
            "cost": 25,
            "margin": -5,
        },
    ]

    with app.app_context():
        rows = suggestions.suggest_returns(order, items, {"scope_hash": "scope-1"})

    assert rows[0]["selected"] is True
    assert rows[0]["suggested_reason"] == "quality_issue"
    assert "prior returns" in rows[0]["rationale"].lower()
    assert rows[1]["suggested_reason"] == "wrong_item"
    assert "negative" in rows[1]["rationale"].lower()


def test_returns_v2_submit_creates_rma_with_items(app, monkeypatch):
    app.config.update(
        RETURNS_ENABLED=True,
        RETURNS_CUSTOMER_PORTAL_ENABLED=True,
        RETURNS_V2=True,
        LOGIN_DISABLED=True,
        AUTHZ_DISABLED=False,
    )
    fixture_payload = {
        "order": {
            "order_id": "ORD-POST-1",
            "customer_id": "C-POST",
            "customer_name": "Post Customer",
            "customer_email": "post@example.com",
            "customer_phone": "555-0000",
            "ship_to": "Portland",
            "status": "delivered",
            "order_date": "2026-03-01",
            "items": [],
        },
        "items": [
            {
                "order_line_id": "L-POST-1",
                "sku": "SKU-POST-1",
                "product_name": "Posted Product",
                "description": "Posted Product",
                "qty_ordered": 2,
                "qty_shipped": 2,
                "unit_price": 20.0,
                "price": 20.0,
            }
        ],
        "suggestions": [
            {
                "order_line_id": "L-POST-1",
                "selected": True,
                "suggested_return_qty": 1,
                "suggested_reason": "customer_return",
                "suggested_condition": "unopened",
                "rationale": "Default suggestion.",
            }
        ],
        "meta": {"lookup_ms": 5.5, "dataset_version": "v1", "scope_hash": "scope-post"},
    }
    monkeypatch.setattr(service, "lookup_order_for_return", lambda order_id, actor_user=None: fixture_payload)

    item_payload = json.dumps(
        [
            {
                "selected": True,
                "order_line_id": "L-POST-1",
                "sku": "SKU-POST-1",
                "product_name": "Posted Product",
                "qty": 1,
                "price": 20.0,
                "reason_code": "customer_return",
                "condition": "unopened",
                "notes": "test submit",
                "qty_ordered": 2,
                "qty_shipped": 2,
                "max_qty": 2,
            }
        ]
    )

    with get_session() as session:
        before = session.query(ReturnRMA).filter(ReturnRMA.order_id == "ORD-POST-1").count()
        before_events = session.query(ReturnEvent).count()

    with app.test_client() as client:
        resp = client.post(
            "/returns/new",
            data={
                "order_id": "ORD-POST-1",
                "items_json": item_payload,
                "notes": "Route submit",
            },
            follow_redirects=False,
        )

    assert resp.status_code == 302

    with get_session() as session:
        after = session.query(ReturnRMA).filter(ReturnRMA.order_id == "ORD-POST-1").count()
        after_events = session.query(ReturnEvent).count()

    assert after == before + 1
    assert after_events >= before_events + 1


def test_returns_new_array_submit_creates_rma_with_server_credit(app, monkeypatch):
    app.config.update(
        RETURNS_ENABLED=True,
        RETURNS_CUSTOMER_PORTAL_ENABLED=True,
        RETURNS_UI_EXCEL_FORM=True,
        RETURNS_AUTOFILL_ORDER=True,
        LOGIN_DISABLED=True,
    )
    monkeypatch.setattr(
        service,
        "lookup_order_for_return",
        lambda order_id, actor_user=None: {
            "order": {
                "order_id": "ORD-ARRAY-1",
                "customer_id": "C-ARRAY",
                "customer_name": "Array Customer",
                "date_shipped": "2026-03-01",
                "order_date": "2026-03-01",
            },
            "items": [],
            "suggestions": [],
            "meta": {},
        },
    )

    with app.test_client() as client:
        resp = client.post(
            "/returns/new",
            data={
                "manual_order_id": "ORD-ARRAY-1",
                "customer_id": "C-ARRAY",
                "customer_name": "Array Customer",
                "date_shipped": "2026-03-01",
                "return_type": "Sales Return",
                "product_code[]": ["SKU-ARRAY-1"],
                "product_desc[]": ["Array Product"],
                "price_per_lb[]": ["12.50"],
                "weight_lb[]": ["2.000"],
                "credit_amount[]": ["0.00"],
                "product_returning[]": ["1"],
                "reason_for_return[]": ["customer_return"],
                "follow_up_action[]": ["Credit"],
                "supplier_credit[]": ["1"],
            },
            follow_redirects=False,
        )

    assert resp.status_code == 302

    with get_session() as session:
        row = session.query(ReturnRMA).filter(ReturnRMA.order_id == "ORD-ARRAY-1").order_by(ReturnRMA.id.desc()).first()
        assert row is not None
        item = session.query(ReturnRMAItem).filter(ReturnRMAItem.rma_id == row.id).order_by(ReturnRMAItem.id.asc()).first()
        assert item is not None
        assert float(row.total_credit_amount or 0) == 25.0
        assert float(item.credit_pct or 0) == 100.0
        assert float(item.credit_amount or 0) == 25.0


def test_returns_new_array_submit_applies_credit_pct_server_side(app):
    app.config.update(
        RETURNS_ENABLED=True,
        RETURNS_FINAL_V1=False,
        LOGIN_DISABLED=True,
    )

    with app.app_context():
        created = service.create_rma(
            order_payload={**_make_order("ORD-ARRAY-PCT-1", "C-ARRAY-PCT"), "workflow_mode": "legacy"},
            item_payloads=[
                {
                    "product_code": "SKU-ARRAY-PCT-1",
                    "product_desc": "Array Product Pct",
                    "price_per_lb": 12.5,
                    "weight_lb": 2.0,
                    "credit_pct": 25,
                    "credit_amount": 999.99,
                    "reason_code": "customer_return",
                    "reason_for_return": "customer_return",
                    "follow_up_action": "Credit",
                    "supplier_credit": False,
                    "product_returning": True,
                }
            ],
            workflow_mode="legacy",
        )
        with get_session() as session:
            row = session.query(ReturnRMA).filter(ReturnRMA.id == int(created["id"])).first()
            assert row is not None
            item = session.query(ReturnRMAItem).filter(ReturnRMAItem.rma_id == row.id).order_by(ReturnRMAItem.id.asc()).first()
            assert item is not None
            assert float(item.credit_pct or 0) == 25.0
            assert float(item.credit_amount or 0) == pytest.approx(6.25, rel=0, abs=1e-6)
            assert float(row.total_credit_amount or 0) == pytest.approx(6.25, rel=0, abs=1e-6)


@pytest.mark.parametrize(
    ("price_per_lb", "weight_lb", "credit_pct", "expected_credit"),
    [
        ("10", "5", "100", 50.00),
        ("10", "5", "25", 12.50),
        ("7.35", "12.4", "50", 45.57),
        ("", "5", "100", 0.00),
    ],
)
def test_credit_amount_for_item_uses_price_weight_and_credit_pct(price_per_lb, weight_lb, credit_pct, expected_credit):
    assert service._credit_amount_for_item(
        {
            "price_per_lb": price_per_lb,
            "weight_lb": weight_lb,
            "credit_pct": credit_pct,
        }
    ) == pytest.approx(expected_credit, rel=0, abs=1e-6)


def test_parse_order_items_from_form_defaults_blank_credit_pct_and_price(app):
    with app.test_request_context(
        "/returns/new",
        method="POST",
        data={
            "product_code[]": ["SKU-BLANK-1"],
            "product_desc[]": ["Blank Product"],
            "price_per_lb[]": [""],
            "weight_lb[]": ["5"],
            "credit_pct[]": [""],
            "credit_amount[]": [""],
            "product_returning[]": ["1"],
            "reason_for_return[]": ["customer_return"],
            "follow_up_action[]": ["Credit"],
            "supplier_credit[]": ["0"],
        },
    ):
        items = returns_blueprints._parse_order_items_from_form()

    assert len(items) == 1
    assert items[0]["price_per_lb"] == pytest.approx(0.0, rel=0, abs=1e-6)
    assert items[0]["credit_pct"] == pytest.approx(100.0, rel=0, abs=1e-6)
    assert items[0]["credit_amount"] == pytest.approx(0.0, rel=0, abs=1e-6)


def test_returns_new_array_submit_recomputes_multi_line_credit_totals(app, monkeypatch):
    app.config.update(
        RETURNS_ENABLED=True,
        RETURNS_CUSTOMER_PORTAL_ENABLED=True,
        RETURNS_UI_EXCEL_FORM=True,
        RETURNS_AUTOFILL_ORDER=True,
        LOGIN_DISABLED=True,
    )
    monkeypatch.setattr(
        service,
        "lookup_order_for_return",
        lambda order_id, actor_user=None: {
            "order": {
                "order_id": "ORD-ARRAY-MULTI-1",
                "customer_id": "C-ARRAY-MULTI",
                "customer_name": "Array Multi Customer",
                "date_shipped": "2026-03-01",
                "order_date": "2026-03-01",
            },
            "items": [],
            "suggestions": [],
            "meta": {},
        },
    )

    with app.test_client() as client:
        resp = client.post(
            "/returns/new",
            data={
                "manual_order_id": "ORD-ARRAY-MULTI-1",
                "customer_id": "C-ARRAY-MULTI",
                "customer_name": "Array Multi Customer",
                "date_shipped": "2026-03-01",
                "return_type": "Sales Return",
                "product_code[]": ["SKU-ARRAY-1", "SKU-ARRAY-2"],
                "product_desc[]": ["Array Product 1", "Array Product 2"],
                "price_per_lb[]": ["10", "7.35"],
                "weight_lb[]": ["5", "12.4"],
                "credit_pct[]": ["25", "50"],
                "credit_amount[]": ["999.99", "888.88"],
                "product_returning[]": ["1", "1"],
                "reason_for_return[]": ["customer_return", "customer_return"],
                "follow_up_action[]": ["Credit", "Credit"],
                "supplier_credit[]": ["0", "0"],
            },
            follow_redirects=False,
        )

    assert resp.status_code == 302

    with get_session() as session:
        row = session.query(ReturnRMA).filter(ReturnRMA.order_id == "ORD-ARRAY-MULTI-1").order_by(ReturnRMA.id.desc()).first()
        assert row is not None
        items = session.query(ReturnRMAItem).filter(ReturnRMAItem.rma_id == row.id).order_by(ReturnRMAItem.id.asc()).all()
        assert len(items) == 2
        assert float(items[0].credit_amount or 0) == pytest.approx(12.50, rel=0, abs=1e-6)
        assert float(items[1].credit_amount or 0) == pytest.approx(45.57, rel=0, abs=1e-6)
        assert float(row.total_credit_amount or 0) == pytest.approx(58.07, rel=0, abs=1e-6)
        assert float(row.total_weight_lb or 0) == pytest.approx(17.4, rel=0, abs=1e-6)


def test_returns_legacy_approval_flow_creates_approval_record_and_pdf(app):
    app.config.update(RETURNS_ENABLED=True, RETURNS_REFUNDS_ENABLED=True, LOGIN_DISABLED=True)
    with app.app_context():
        created = service.create_rma(
            order_payload={**_make_order("ORD-LEG-1", "C-LEG-1"), "workflow_mode": "legacy", "return_type": "credit"},
            item_payloads=[_make_item("damaged")],
            workflow_mode="legacy",
        )
        assert created["status"] == service.STATUS_PENDING

        wh = service.approve_warehouse(created["id"])
        assert wh["status"] == service.STATUS_WH_APPROVED

        mgr = service.approve_manager(created["id"])
        assert mgr["detail"]["status"] == service.STATUS_APPROVED
        assert mgr["pdf_bytes"].startswith(b"%PDF-")

        with get_session() as session:
            approval = session.query(ReturnApproval).filter(ReturnApproval.rma_id == created["id"]).first()
            assert approval is not None
            assert approval.wh_approved_at is not None
            assert approval.mgr_approved_at is not None


def test_pdf_generation_fallback_receives_credit_pct_and_totals(app, monkeypatch):
    app.config.update(RETURNS_ENABLED=True, LOGIN_DISABLED=True)
    with app.app_context():
        created = service.create_rma(
            order_payload={**_make_order("ORD-PDF-CREDIT-1", "C-PDF-CREDIT-1"), "workflow_mode": "legacy"},
            item_payloads=[
                {
                    "product_code": "SKU-PDF-1",
                    "product_desc": "PDF Product",
                    "price_per_lb": 12.5,
                    "weight_lb": 2.0,
                    "credit_pct": 25,
                    "reason_for_return": "Damaged",
                    "reason_code": "damaged",
                    "follow_up_action": "Credit",
                    "product_returning": True,
                    "supplier_credit": False,
                }
            ],
            workflow_mode="legacy",
        )

        captured: dict[str, object] = {}

        def _fake_structured_pdf(detail, *, generated_at, credit_po=False):
            captured["detail"] = detail
            captured["generated_at"] = generated_at
            captured["credit_po"] = credit_po
            return b"%PDF-fallback"

        monkeypatch.setattr(service, "_build_structured_return_pdf", _fake_structured_pdf)
        monkeypatch.setitem(sys.modules, "weasyprint", None)
        payload = service.build_return_pdf_bytes(created["id"], credit_po=True)

    assert payload == b"%PDF-fallback"
    assert captured.get("credit_po") is True
    detail = captured.get("detail")
    assert isinstance(detail, dict)
    item = (detail.get("items") or [])[0]
    assert float(item.get("credit_pct") or 0) == 25.0
    assert float(item.get("credit_amount") or 0) == pytest.approx(6.25, rel=0, abs=1e-6)
    assert float(detail.get("total_credit_amount") or 0) == pytest.approx(6.25, rel=0, abs=1e-6)


def test_returns_approvals_page_and_pdf_route_work(app, monkeypatch):
    app.config.update(RETURNS_ENABLED=True, LOGIN_DISABLED=False, AUTHZ_DISABLED=False)
    monkeypatch.setattr(service, "get_current_scope", lambda use_cache=True: _Scope({"is_admin": True, "scope_mode": "all"}))
    with app.app_context():
        created = service.create_rma(
            order_payload={**_make_order("ORD-LEG-2", "C-LEG-2"), "workflow_mode": "legacy"},
            item_payloads=[_make_item("damaged")],
            workflow_mode="legacy",
        )

    user = _make_user(role="admin", user_id=7)
    monkeypatch.setattr(service, "scope_for_user", lambda _user, use_cache=True: _Scope({"is_admin": True, "scope_mode": "all"}))
    monkeypatch.setattr("app.core.rbac._get_user", lambda: user, raising=False)
    monkeypatch.setattr("flask_login.utils._get_user", lambda: user, raising=False)
    monkeypatch.setattr("app.core.rbac.has_any_permission", lambda *args, **kwargs: True, raising=False)

    with app.test_client() as client:
        approvals_resp = client.get("/returns/approvals")
        pdf_resp = client.get(f"/returns/{created['id']}/pdf")

    assert approvals_resp.status_code == 200
    assert b"Returns Approvals" in approvals_resp.data
    assert pdf_resp.status_code == 200
    assert pdf_resp.mimetype == "application/pdf"


def test_returns_detail_receiving_update_and_completion_flow(app):
    app.config.update(
        RETURNS_ENABLED=True,
        RETURNS_CUSTOMER_PORTAL_ENABLED=True,
        LOGIN_DISABLED=True,
    )
    with app.app_context():
        created = service.create_rma(
            order_payload={**_make_order("ORD-DETAIL-1", "C-DETAIL-1"), "workflow_mode": "legacy"},
            item_payloads=[
                {
                    **_make_item("damaged"),
                    "packs_count": 1,
                    "follow_up_action": "Credit",
                }
            ],
            workflow_mode="legacy",
        )
        item_id = created["items"][0]["id"]

    with app.test_client() as client:
        update_resp = client.post(
            f"/returns/{created['id']}",
            data={
                "form_action": "update_receiving",
                "item_ids": [str(item_id)],
                f"item_pack_barcode_{item_id}": "PACK-001",
                f"item_packs_count_{item_id}": "4",
                f"item_follow_up_action_{item_id}": "Restock",
                f"item_supplier_credit_{item_id}": "1",
                f"item_receiving_notes_{item_id}": "Pallet checked",
                "rec_prod_signoff": "1",
                "receiving_notes": "Dock cleared",
            },
            follow_redirects=False,
        )
    assert update_resp.status_code == 302

    with app.app_context():
        updated = service.get_rma_detail(created["id"])
        assert updated is not None
        assert updated["rec_prod_signoff"] is True
        assert updated["total_packs"] == 4
        assert updated["primary_follow_up"] == "Restock"
        assert updated["metadata"]["receiving_notes"] == "Dock cleared"
        assert updated["items"][0]["pack_barcode"] == "PACK-001"
        assert updated["items"][0]["supplier_credit"] is True

        service.approve_warehouse(created["id"])
        service.approve_manager(created["id"])

    with app.test_client() as client:
        complete_resp = client.post(
            f"/returns/{created['id']}",
            data={"form_action": "complete"},
            follow_redirects=False,
        )
    assert complete_resp.status_code == 302

    with app.app_context():
        completed = service.get_rma_detail(created["id"])
        assert completed is not None
        assert completed["status"] == service.STATUS_COMPLETED
        assert any(event["event_type"] == "completed" for event in completed["events"])


def test_final_v1_submission_requires_strict_fields(app_client):
    app = app_client.application
    app.config.update(
        RETURNS_ENABLED=True,
        RETURNS_CUSTOMER_PORTAL_ENABLED=True,
        RETURNS_UI_EXCEL_FORM=True,
        RETURNS_FINAL_V1=True,
        LOGIN_DISABLED=True,
        WTF_CSRF_ENABLED=True,
    )

    client = app_client
    new_resp = client.get("/returns/new")
    csrf_token = _extract_csrf_token(new_resp.data)
    resp = client.post(
        "/returns/new",
        data={
            "csrf_token": csrf_token,
            "manual_order_id": "ORD-FINAL-VAL-1",
            "customer_id": "C-FINAL-VAL-1",
            "customer_name": "Strict Customer",
            "return_type": "Sales Return",
            "product_code[]": ["SKU-STRICT-1"],
            "product_desc[]": ["Strict Product"],
            "price_per_lb[]": ["9.00"],
            "weight_lb[]": ["2.000"],
            "product_returning[]": ["1"],
            "reason_for_return[]": ["customer_return"],
            "follow_up_action[]": ["Credit"],
            "supplier_credit[]": ["0"],
        },
        follow_redirects=False,
    )

    assert resp.status_code == 400
    assert b"Submission date is required." in resp.data


def test_final_v1_requires_warehouse_fields_and_blocks_warehouse_manager_approval(app, monkeypatch):
    app.config.update(RETURNS_ENABLED=True, RETURNS_FINAL_V1=True, LOGIN_DISABLED=True)
    with app.app_context():
        created = service.create_rma(
            order_payload={
                **_make_order("ORD-FINAL-WH-1", "C-FINAL-WH-1"),
                "customer_name": "Warehouse Final",
                "date_submitted": "2026-03-04",
                "return_type": "Sales Return",
                "advised_customer_provided": True,
                "advised_customer": True,
            },
            item_payloads=[
                {
                    "product_code": "SKU-WH-1",
                    "product_desc": "Warehouse Product",
                    "price_per_lb": 10.0,
                    "weight_lb": 1.5,
                    "product_returning": True,
                    "product_returning_provided": True,
                    "reason_for_return": "Damaged",
                    "reason_code": "damaged",
                    "follow_up_action": "Credit",
                    "supplier_credit": False,
                    "supplier_credit_provided": True,
                }
            ],
        )
        with pytest.raises(service.ReturnsError):
            service.approve_warehouse(created["id"])

        item_id = created["items"][0]["id"]
        service.update_receiving_review(
            created["id"],
            header_updates={"receiving_notes": "Dock cleared"},
            item_updates=[
                {
                    "item_id": item_id,
                    "pack_barcode": "PK-100",
                    "packs_count": 1,
                    "follow_up_action": "Credit",
                    "supplier_credit": 0,
                }
            ],
        )
        service.approve_warehouse(created["id"])

    app.config.update(LOGIN_DISABLED=False, AUTHZ_DISABLED=False)
    warehouse_user = _make_user(role="warehouse", user_id=103)
    _patch_permission_user(monkeypatch, warehouse_user, set(list_role_permissions("warehouse")))
    with app.test_client() as client:
        resp = client.post(f"/returns/{created['id']}/approve_mgr", data={})

    assert resp.status_code == 403
    app.config.update(RETURNS_FINAL_V1=False, AUTHZ_DISABLED=False)


def test_render_email_includes_deep_link_and_item_table(app):
    app.config.update(RETURNS_ENABLED=True, RETURNS_FINAL_V1=True, APP_PUBLIC_BASE_URL="https://analytics.example.com")
    payload = {
        "id": 123,
        "status": service.STATUS_PENDING,
        "status_label": "Pending",
        "order_id": "ORD-EMAIL-1",
        "customer_id": "C-EMAIL-1",
        "customer_name": "Email Customer",
        "rep_name": "Rep Example",
        "total_credit_amount": 25.0,
        "total_weight_lb": 2.0,
        "total_packs": 3,
        "primary_reason": "Damaged",
        "primary_category": "Warehouse",
        "primary_follow_up": "Credit",
        "items": [
            {
                "product_code": "SKU-EMAIL-1",
                "product_desc": "Email Product",
                "weight_lb": 2.0,
                "credit_amount": 25.0,
            }
        ],
    }
    with app.test_request_context("/"):
        html, text = service.render_email("event", service._return_email_context(payload, event_key="return_submitted"))

    assert html is not None
    assert "https://analytics.example.com/returns/123" in html
    assert "Open Return" in html
    assert "SKU-EMAIL-1" in html
    assert "https://analytics.example.com/returns/123" in text
    app.config.update(RETURNS_FINAL_V1=False, APP_PUBLIC_BASE_URL="")


def test_returns_nav_visibility_respects_flag_and_permission(app, monkeypatch):
    user = _make_user(role="sales")
    monkeypatch.setattr("flask_login.utils._get_user", lambda: user, raising=False)

    app.config.update(RETURNS_ENABLED=True)
    monkeypatch.setattr(
        "app.core.rbac.effective_permissions",
        lambda _user=None: {"page.returns.view"},
        raising=False,
    )
    with app.test_request_context("/"):
        html = render_template("base.html", disable_global_filters=True)
    assert 'href="/returns"' in html

    app.config.update(RETURNS_ENABLED=False)
    with app.test_request_context("/"):
        html_disabled = render_template("base.html", disable_global_filters=True)
    assert 'href="/returns"' not in html_disabled

    app.config.update(RETURNS_ENABLED=True)
    monkeypatch.setattr("app.core.rbac.effective_permissions", lambda _user=None: set(), raising=False)
    with app.test_request_context("/"):
        html_hidden = render_template("base.html", disable_global_filters=True)
    assert 'href="/returns"' not in html_hidden
