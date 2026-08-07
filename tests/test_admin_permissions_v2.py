import io
import os
import uuid

import pandas as pd
from flask import jsonify, render_template_string
from sqlalchemy import func, text

from app import create_app
from app.blueprints import products as products_blueprint
from app.auth.models import AuditLog, Permission, Role, RolePermission, SessionLocal, User, UserRole, list_role_permissions, sync_permissions
from app.auth.models import replace_user_permission_rules
from app.core.payload_permissions import apply_payload_permissions
from app.core.exports import dataframe_to_csv_response
from app.core.rbac import route_permission_override_allows_request


def _build_app():
    os.environ.setdefault("FLASK_ENV", "testing")
    os.environ.setdefault("WTF_CSRF_ENABLED", "false")
    os.environ.setdefault("WA_FAST_PWHASH", "1")
    app = create_app()
    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SECRET_KEY="test",
        LOGIN_DISABLED=False,
        AUTHZ_DISABLED=False,
        ADMIN_PERMISSIONS_V2=True,
        AUTHZ_ENFORCEMENT=True,
        AUTHZ_ENFORCEMENT_MODE="enforce",
    )
    return app


def _create_user(*, role: str, username_prefix: str) -> User:
    username = f"{username_prefix}.{uuid.uuid4().hex[:8]}"
    email = f"{username}@example.com"
    with SessionLocal() as s:
        user = User(
            username=username,
            email=email,
            role=role,
            is_active=True,
            is_approved=True,
            erp_user_id=f"ERP-{uuid.uuid4().hex[:6]}",
            sales_rep_id=f"ERP-{uuid.uuid4().hex[:6]}",
        )
        user.set_password("pw12345")
        s.add(user)
        s.commit()
        s.refresh(user)
        return user


def _login(client, user: User):
    response = client.post(
        "/auth/login",
        data={"username": user.username, "password": "pw12345"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    return response


def _audit_actions_for_target(user_id: int) -> list[str]:
    with SessionLocal() as s:
        rows = (
            s.query(AuditLog.action)
            .filter(AuditLog.target_user_id == int(user_id))
            .order_by(AuditLog.id.asc())
            .all()
        )
    return [str(row[0]) for row in rows if row and row[0]]


def test_sync_permissions_is_idempotent_for_user_role_backfill():
    _build_app()

    sync_permissions()
    sync_permissions()

    with SessionLocal() as s:
        duplicates = (
            s.query(UserRole.user_id, UserRole.role_id)
            .group_by(UserRole.user_id, UserRole.role_id)
            .having(func.count(UserRole.id) > 1)
            .all()
        )

    assert duplicates == []


def test_sync_permissions_removes_stale_production_customer_access():
    _build_app()

    stale_keys = {
        "page.customers.view",
        "page.customers.drilldown.view",
        "export.customers",
        "feature.customers.dashboard.view",
    }

    with SessionLocal() as s:
        role = s.query(Role).filter(func.lower(Role.name) == "production").first()
        assert role is not None

        permission_rows = (
            s.query(Permission)
            .filter(func.lower(Permission.key).in_(sorted(stale_keys)))
            .all()
        )
        assert {str(row.key).strip().lower() for row in permission_rows} == stale_keys

        permission_ids = [int(row.id) for row in permission_rows]
        (
            s.query(RolePermission)
            .filter(
                RolePermission.role_id == int(role.id),
                RolePermission.permission_id.in_(permission_ids),
            )
            .delete(synchronize_session=False)
        )
        s.commit()

        for permission_id in permission_ids:
            s.add(RolePermission(role_id=int(role.id), permission_id=permission_id))
        s.commit()

    result = sync_permissions()

    assert isinstance(result["role_permissions_removed"], int)
    assert stale_keys.isdisjoint(set(list_role_permissions("production")))


def test_init_auth_db_ensures_login_attempts_table():
    _build_app()

    with SessionLocal() as s:
        tables = {
            str(row[0])
            for row in s.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            if row and row[0]
        }

    assert "login_attempts" in tables


def test_route_policy_honors_user_deny(monkeypatch):
    app = _build_app()
    user = _create_user(role="sales_manager", username_prefix="permdeny")
    replace_user_permission_rules(int(user.id), allow_keys=(), deny_keys=["page.customers.view"])

    monkeypatch.setattr("app.core.rbac.current_user", user, raising=False)
    with app.test_request_context("/customers"):
        assert route_permission_override_allows_request() is False


def test_api_bundle_policy_honors_feature_deny(monkeypatch):
    app = _build_app()
    user = _create_user(role="sales", username_prefix="bundledeny")
    replace_user_permission_rules(int(user.id), allow_keys=(), deny_keys=["feature.products.dashboard.view"])

    monkeypatch.setattr("app.core.rbac.current_user", user, raising=False)
    with app.test_request_context("/api/products/bundle"):
        assert route_permission_override_allows_request() is False


def test_production_role_is_blocked_from_customer_pages_without_customer_permissions():
    app = _build_app()
    user = _create_user(role="production", username_prefix="prodcustdeny")

    with app.test_client() as client:
        _login(client, user)

        html_resp = client.get("/customers/")
        assert html_resp.status_code == 403

        bundle_resp = client.get("/api/customers/bundle")
        assert bundle_resp.status_code == 403

        drill_resp = client.get("/customers/drilldown/C-1")
        assert drill_resp.status_code == 403


def test_csv_export_masks_sensitive_columns_without_export_permission(monkeypatch):
    app = _build_app()
    user = _create_user(role="sales", username_prefix="maskedexport")
    frame = pd.DataFrame(
        [
            {
                "product_id": "SKU-1",
                "revenue": 1200.0,
                "cost": 900.0,
                "profit": 300.0,
                "margin_pct": 25.0,
                "recommendation": "Raise price",
                "margin_risk": "High",
            }
        ]
    )

    monkeypatch.setattr("app.core.exports.current_user", user, raising=False)
    with app.test_request_context("/products/export/table.csv"):
        response = dataframe_to_csv_response(frame, filename="products.csv")
        response.direct_passthrough = False
        exported = pd.read_csv(io.BytesIO(response.get_data()))

    assert "cost" in exported.columns
    assert pd.isna(exported.loc[0, "cost"])
    assert pd.isna(exported.loc[0, "profit"])
    assert pd.isna(exported.loc[0, "margin_pct"])
    assert pd.isna(exported.loc[0, "recommendation"])
    assert pd.isna(exported.loc[0, "margin_risk"])


def test_json_api_masking_applies_to_sensitive_payloads():
    app = _build_app()
    user = _create_user(role="sales", username_prefix="maskedjson")

    @app.get("/api/__test_sensitive__")
    def test_sensitive_payload():
        return jsonify(
            {
                "cost": 100.0,
                "profit": 40.0,
                "margin_pct": 40.0,
                "recommendation": "Raise price",
                "margin_risk": {"rows": [{"sku": "SKU-1", "risk": "High"}], "summary": "flagged"},
            }
        )

    with app.test_client() as client:
        _login(client, user)
        response = client.get("/api/__test_sensitive__")
        assert response.status_code == 200
        payload = response.get_json()

    assert payload["cost"] is None
    assert payload["profit"] is None
    assert payload["margin_pct"] is None
    assert payload["recommendation"] is None
    assert payload["margin_risk"]["rows"] == []


def test_navbar_uses_permissions_instead_of_role_only():
    app = _build_app()
    user = _create_user(role="sales", username_prefix="navperm")
    replace_user_permission_rules(
        int(user.id),
        allow_keys=["page.admin.view", "admin.users.manage"],
        deny_keys=["page.products.view"],
    )

    @app.get("/__test_nav__")
    def test_nav_shell():
        return render_template_string("{% extends 'base.html' %}{% block content %}<div>ok</div>{% endblock %}")

    with app.test_client() as client:
        _login(client, user)
        response = client.get("/__test_nav__")
        assert response.status_code == 200
        html = response.get_data(as_text=True)

    assert 'href="/admin/users"' in html
    assert ">Customers</a>" in html
    assert ">Products</a>" not in html


def test_admin_permission_patch_writes_audit_entry():
    app = _build_app()
    admin = _create_user(role="admin", username_prefix="adminperm")
    target = _create_user(role="sales", username_prefix="targetperm")

    with app.test_client() as client:
        _login(client, admin)

        response = client.patch(
            f"/api/_admin/users/{target.id}/permissions",
            json={
                "allow": ["page.products.view", "page.products.drilldown.view"],
                "deny": ["page.customers.view"],
            },
        )
        assert response.status_code == 200
        payload = response.get_json()
        assert "page.products.view" in payload["user"]["user_permission_rules"]["allow"]
        assert "page.customers.view" in payload["user"]["user_permission_rules"]["deny"]

    with SessionLocal() as s:
        audit = (
            s.query(AuditLog)
            .filter(AuditLog.target_user_id == int(target.id), AuditLog.action == "admin_update_user_permissions")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert audit is not None


def test_user_permissions_api_returns_editor_schema():
    app = _build_app()
    admin = _create_user(role="admin", username_prefix="adminschema")
    target = _create_user(role="sales_manager", username_prefix="targetschema")

    with app.test_client() as client:
        _login(client, admin)
        response = client.get(f"/api/_admin/users/{target.id}/permissions")
        assert response.status_code == 200
        payload = response.get_json()

    assert payload["access_source"]["role_preset"] == "sales_manager"
    assert payload["access_source"]["use_role_defaults_only"] is True
    assert any(module["id"] == "products" for module in payload["editor"]["modules"])
    assert any(item["key"] == "data.cost.view" for item in payload["editor"]["sensitive_data"])
    assert any(preset["id"] == "returns_only_user" for preset in payload["editor"]["presets"])
    assert "selected_permissions" in payload["editor_state"]
    assert "summary" in payload["editor_state"]
    assert "validation" in payload["editor_state"]


def test_admin_users_page_renders_scrollable_access_modal_shell():
    app = _build_app()
    admin = _create_user(role="admin", username_prefix="adminpage")

    with app.test_client() as client:
        _login(client, admin)
        response = client.get("/admin/users")
        assert response.status_code == 200
        html = response.get_data(as_text=True)

    assert '<div class="modal-dialog modal-xl access-modal-dialog">' in html
    assert '<div class="modal-content access-modal-content">' in html
    assert '<form id="accessForm" class="access-modal-form">' in html
    assert '<div class="modal-body access-modal-body">' in html
    assert '<div class="modal-footer access-modal-footer">' in html
    assert 'class="access-top-grid"' in html


def test_permission_patch_can_reset_to_role_defaults():
    app = _build_app()
    admin = _create_user(role="admin", username_prefix="adminreset")
    target = _create_user(role="sales_manager", username_prefix="targetreset")
    replace_user_permission_rules(
        int(target.id),
        allow_keys=["page.notifications.view"],
        deny_keys=["page.products.view"],
    )

    with app.test_client() as client:
        _login(client, admin)
        response = client.patch(
            f"/api/_admin/users/{target.id}/permissions",
            json={"reset_to_role_defaults": True},
        )
        assert response.status_code == 200
        payload = response.get_json()

    assert payload["user"]["user_permission_rules"]["allow"] == []
    assert payload["user"]["user_permission_rules"]["deny"] == []


def test_permission_patch_selected_permissions_normalizes_parent_page():
    app = _build_app()
    admin = _create_user(role="admin", username_prefix="adminnorm")
    target = _create_user(role="returns_only", username_prefix="targetnorm")

    with app.test_client() as client:
        _login(client, admin)
        response = client.patch(
            f"/api/_admin/users/{target.id}/permissions",
            json={"selected_permissions": ["page.products.drilldown.view"]},
        )
        assert response.status_code == 200
        payload = response.get_json()

    selected = set(payload["editor_state"]["selected_permissions"])
    assert "page.products.drilldown.view" in selected
    assert "page.products.view" in selected


def test_permission_patch_selected_permissions_roundtrip_on_reopen():
    app = _build_app()
    admin = _create_user(role="admin", username_prefix="adminroundtrip")
    target = _create_user(role="sales", username_prefix="targetroundtrip")

    chosen = {
        "page.customers.view",
        "page.customers.drilldown.view",
        "export.customers",
        "data.cost.view",
    }

    with app.test_client() as client:
        _login(client, admin)
        patch_response = client.patch(
            f"/api/_admin/users/{target.id}/permissions",
            json={"selected_permissions": sorted(chosen)},
        )
        assert patch_response.status_code == 200
        reopen_response = client.get(f"/api/_admin/users/{target.id}/permissions")
        assert reopen_response.status_code == 200
        payload = reopen_response.get_json()

    selected = set(payload["editor_state"]["selected_permissions"])
    assert chosen.issubset(selected)
    assert payload["access_source"]["use_role_defaults_only"] is False


def test_permission_patch_selected_permissions_can_hide_inherited_role_access():
    app = _build_app()
    admin = _create_user(role="admin", username_prefix="adminhide")
    target = _create_user(role="sales", username_prefix="targethide")

    chosen = {"page.customers.view"}

    with app.test_client() as client:
        _login(client, admin)
        patch_response = client.patch(
            f"/api/_admin/users/{target.id}/permissions",
            json={"selected_permissions": sorted(chosen)},
        )
        assert patch_response.status_code == 200
        patch_payload = patch_response.get_json()
        reopen_response = client.get(f"/api/_admin/users/{target.id}/permissions")
        assert reopen_response.status_code == 200
        reopen_payload = reopen_response.get_json()

    patch_selected = set(patch_payload["editor_state"]["selected_permissions"])
    reopen_selected = set(reopen_payload["editor_state"]["selected_permissions"])
    assert patch_selected == reopen_selected
    assert patch_selected == chosen
    assert "page.products.view" not in reopen_selected
    deny_rules = set(reopen_payload["user"]["user_permission_rules"]["deny"])
    assert "page.products.view" in deny_rules
    assert reopen_payload["access_source"]["use_role_defaults_only"] is False


def test_permissions_api_exposes_validation_warning_for_cost_derived_visibility():
    app = _build_app()
    admin = _create_user(role="admin", username_prefix="adminwarn")
    target = _create_user(role="viewer", username_prefix="targetwarn")
    replace_user_permission_rules(
        int(target.id),
        allow_keys=["data.price_recommendation.view"],
        deny_keys=["data.cost.view"],
    )

    with app.test_client() as client:
        _login(client, admin)
        response = client.get(f"/api/_admin/users/{target.id}/permissions")
        assert response.status_code == 200
        payload = response.get_json()

    warnings = payload["editor_state"]["validation"]["warnings"]
    assert any("Cost visibility is hidden" in warning for warning in warnings)


def test_update_user_actions_write_specific_audit_entries():
    app = _build_app()
    admin = _create_user(role="admin", username_prefix="adminactions")
    target = _create_user(role="sales", username_prefix="targetactions")

    with SessionLocal() as s:
        row = s.get(User, int(target.id))
        row.is_active = False
        row.is_approved = False
        s.add(row)
        s.commit()

    with app.test_client() as client:
        _login(client, admin)

        approve = client.patch(
            f"/api/_admin/users/{target.id}/activation",
            json={"action": "approve"},
        )
        assert approve.status_code == 200

        disable = client.patch(
            f"/api/_admin/users/{target.id}/activation",
            json={"action": "disable"},
        )
        assert disable.status_code == 200

        revoke = client.patch(
            f"/api/_admin/users/{target.id}/activation",
            json={"action": "revoke"},
        )
        assert revoke.status_code == 200

    actions = _audit_actions_for_target(int(target.id))
    assert "admin_approve_user" in actions
    assert "admin_disable_user" in actions
    assert "admin_revoke_user" in actions


def test_scope_and_bulk_actions_write_audit_entries():
    app = _build_app()
    admin = _create_user(role="admin", username_prefix="adminscope")
    target = _create_user(role="sales", username_prefix="targetscope")

    with app.test_client() as client:
        _login(client, admin)

        scope_resp = client.patch(
            f"/api/_admin/users/{target.id}/scope",
            json={
                "visibility": ["__all__"],
                "scope": {
                    "sales_rep_ids": ["__all__"],
                    "customer_ids": [],
                    "region_ids": [],
                    "supplier_ids": [],
                },
            },
        )
        assert scope_resp.status_code == 200

        bulk_resp = client.post(
            "/api/_admin/users/bulk",
            json={"user_ids": [int(target.id)], "action": "disable"},
        )
        assert bulk_resp.status_code == 200

    actions = _audit_actions_for_target(int(target.id))
    assert "admin_update_visibility" in actions
    assert "admin_bulk_disable" in actions


def test_role_change_writes_audit_entry():
    app = _build_app()
    admin = _create_user(role="admin", username_prefix="adminroleaudit")
    target = _create_user(role="sales", username_prefix="targetroleaudit")

    with app.test_client() as client:
        _login(client, admin)
        response = client.patch(
            f"/api/_admin/users/{target.id}/role",
            json={"role": "viewer"},
        )
        assert response.status_code == 200

    actions = _audit_actions_for_target(int(target.id))
    assert "admin_update_role" in actions


def test_resend_invite_and_reset_password_write_audit_entries(monkeypatch):
    app = _build_app()
    app.config["INVITES_ENABLED"] = True
    admin = _create_user(role="admin", username_prefix="admininvite")
    target = _create_user(role="sales", username_prefix="targetinvite")

    monkeypatch.setattr("app.blueprints.admin_api._issue_password_link", lambda *args, **kwargs: "https://example.com/set-password")
    monkeypatch.setattr("app.blueprints.admin_api._send_password_email_for_user", lambda **kwargs: True)

    with app.test_client() as client:
        _login(client, admin)

        resend = client.post(f"/api/_admin/users/{target.id}/resend-invite")
        assert resend.status_code == 200

        reset = client.post(f"/api/_admin/users/{target.id}/reset-password")
        assert reset.status_code == 200

    actions = _audit_actions_for_target(int(target.id))
    assert "admin_resend_invite" in actions
    assert "admin_reset_password" in actions


def test_product_drilldown_policy_requires_drilldown_permission(monkeypatch):
    app = _build_app()
    user = _create_user(role="analyst", username_prefix="drilldeny")
    replace_user_permission_rules(int(user.id), allow_keys=(), deny_keys=["page.products.drilldown.view"])

    monkeypatch.setattr("app.core.rbac.current_user", user, raising=False)
    with app.test_request_context("/products/SKU-1/drilldown"):
        assert route_permission_override_allows_request() is False


def test_legacy_product_detail_policy_requires_drilldown_permission(monkeypatch):
    app = _build_app()
    user = _create_user(role="analyst", username_prefix="legacydrill")
    replace_user_permission_rules(int(user.id), allow_keys=(), deny_keys=["page.products.drilldown.view"])

    monkeypatch.setattr("app.core.rbac.current_user", user, raising=False)
    with app.test_request_context("/products/SKU-1"):
        assert route_permission_override_allows_request() is False


def test_payload_permissions_strip_disallowed_product_bundle_sections():
    app = _build_app()
    user = _create_user(role="analyst", username_prefix="bundleprune")
    replace_user_permission_rules(
        int(user.id),
        allow_keys=(),
        deny_keys=[
            "feature.products.pricing.view",
            "feature.products.recommendations.view",
            "feature.products.table.view",
        ],
    )

    payload = {
        "charts": {
            "unit_price_dist": [{"bucket": "10-20", "count": 3}],
            "segments": {"summary": [{"segment": "core"}], "movers": [], "mix_shift": []},
            "trajectory": {"labels": ["2026-01"], "revenue": [100.0]},
        },
        "price_vs_velocity": [{"sku": "SKU-1"}],
        "performance_bubble": {"target_margin": 20, "points": [{"sku": "SKU-1"}]},
        "pricing_guardrails": {"rows": [{"sku": "SKU-1"}], "outside_count": 1},
        "execution_lists": {"pricing_fixes": [{"sku": "SKU-1"}]},
        "recommendations": [{"sku": "SKU-1"}],
        "ai_signals": [{"sku": "SKU-1"}],
        "table": {"rows": [{"sku": "SKU-1", "quick_rec": "Act", "recommendation": "Raise price"}], "total_rows": 1},
    }

    with app.app_context():
        pruned = apply_payload_permissions(payload, user, path="/api/products/bundle")

    assert pruned["price_vs_velocity"] == []
    assert pruned["performance_bubble"]["points"] == []
    assert pruned["recommendations"] == []
    assert pruned["table"]["rows"] == []


def test_legacy_products_overview_csv_export_masks_sensitive_columns(monkeypatch):
    app = _build_app()
    user = _create_user(role="sales", username_prefix="legacyexport")
    replace_user_permission_rules(
        int(user.id),
        allow_keys=["page.products.view", "feature.products.table.view", "export.products"],
        deny_keys=["data.cost.view", "data.margin.view", "data.profit.view"],
    )

    monkeypatch.setattr(
        products_blueprint,
        "_collect_overview_export_rows",
        lambda: [
            {
                "product_id": "SKU-1",
                "revenue": 1200.0,
                "cost": 900.0,
                "profit": 300.0,
                "margin_pct": 25.0,
            }
        ],
        raising=False,
    )
    monkeypatch.setattr(products_blueprint, "_requested_products_export_fields", lambda: [], raising=False)
    monkeypatch.setattr(products_blueprint, "current_user", user, raising=False)
    export_fn = products_blueprint.export_overview_table_csv
    while hasattr(export_fn, "__wrapped__"):
        export_fn = export_fn.__wrapped__
    with app.test_request_context("/products/export/table.csv"):
        response = export_fn()
        exported = pd.read_csv(io.BytesIO(response.get_data()))

    assert pd.isna(exported.loc[0, "cost"])
    assert pd.isna(exported.loc[0, "profit"])
    assert pd.isna(exported.loc[0, "margin_pct"])


def test_returns_only_user_navbar_hides_other_modules():
    app = _build_app()
    app.config["RETURNS_ENABLED"] = True
    user = _create_user(role="returns_only", username_prefix="returnsnav")

    @app.get("/returns/__test_nav__")
    def test_returns_nav_shell():
        return render_template_string("{% extends 'base.html' %}{% block content %}<div>ok</div>{% endblock %}")

    with app.test_client() as client:
        response = client.post(
            "/auth/login",
            data={"username": user.username, "password": "pw12345"},
            follow_redirects=False,
        )
        assert response.status_code in {302, 303}
        response = client.get("/returns/__test_nav__")
        assert response.status_code == 200
        html = response.get_data(as_text=True)

    assert ">Returns</a>" in html
    assert ">Customers</a>" not in html
    assert ">Products</a>" not in html
