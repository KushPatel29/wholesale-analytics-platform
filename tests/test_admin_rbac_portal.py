import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import create_app
from app.auth.models import SessionLocal, User, replace_user_permission_overrides
from app.services.filters import filters_cache_key, parse_filters


@pytest.fixture()
def app():
    os.environ.setdefault("FLASK_ENV", "development")
    os.environ.setdefault("WTF_CSRF_ENABLED", "false")
    os.environ.setdefault("WA_FAST_PWHASH", "1")
    app = create_app()
    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SECRET_KEY="test",
        LOGIN_DISABLED=False,
        AUTHZ_DISABLED=False,
        ADMIN_PORTAL_ENABLED=True,
    )
    return app


@pytest.fixture()
def client(app):
    with app.test_client() as c:
        yield c


def _login_admin(client):
    resp = client.post("/auth/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)
    assert resp.status_code == 200


def _login_user(client, username: str, password: str = "pw") -> None:
    resp = client.post("/auth/login", data={"username": username, "password": password}, follow_redirects=True)
    assert resp.status_code == 200


def test_roles_api_create_and_update(client):
    _login_admin(client)
    role_name = f"qa_role_{uuid.uuid4().hex[:8]}"
    create_resp = client.post(
        "/api/_admin/roles",
        json={"name": role_name, "description": "QA role", "permissions": ["page.overview.view"]},
    )
    assert create_resp.status_code == 201
    role_payload = create_resp.get_json()["role"]
    role_id = int(role_payload["id"])

    patch_resp = client.patch(
        f"/api/_admin/roles/{role_id}",
        json={"permissions": ["page.overview.view", "page.products.view"]},
    )
    assert patch_resp.status_code == 200
    updated = patch_resp.get_json()["role"]
    assert "page.products.view" in updated["permissions"]

    list_resp = client.get("/api/_admin/roles")
    assert list_resp.status_code == 200
    roles = list_resp.get_json()["roles"]
    assert any(r["name"] == role_name for r in roles)


def test_scope_update_supports_multi_dimension_allowlists(client, monkeypatch):
    _login_admin(client)
    monkeypatch.setattr("app.blueprints.admin_api._validate_scope_values", lambda _scope_map: None)
    with SessionLocal() as s:
        username = f"scope_user_{uuid.uuid4().hex[:6]}"
        rep_id = f"REP-{uuid.uuid4().hex[:6]}"
        user = User(
            username=username,
            email=f"{username}@example.com",
            role="sales",
            is_active=True,
            is_approved=True,
            sales_rep_id=rep_id,
            erp_user_id=rep_id,
            updated_at=datetime.now(timezone.utc),
        )
        user.set_password("pw")
        s.add(user)
        s.commit()
        s.refresh(user)
        user_id = int(user.id)

    resp = client.patch(
        f"/api/_admin/users/{user_id}/scope",
        json={
            "scope": {
                "sales_rep_ids": ["REP-9"],
                "customer_ids": ["CUST-1"],
                "region_ids": ["West"],
                "supplier_ids": ["SUP-1"],
            }
        },
    )
    assert resp.status_code == 200
    scope = resp.get_json()["user"]["scope"]
    assert "REP-9" in scope["allowed_erp_user_ids"]
    assert "CUST-1" in scope["allowed_customer_ids"]
    assert "West" in scope["allowed_region_ids"]
    assert "SUP-1" in scope["allowed_supplier_ids"]


def test_admin_portal_user_can_view_roles_read_only(client):
    username = f"owner_{uuid.uuid4().hex[:6]}"
    with SessionLocal() as s:
        user = User(
            username=username,
            email=f"{username}@example.com",
            role="owner",
            is_active=True,
            is_approved=True,
            updated_at=datetime.now(timezone.utc),
        )
        user.set_password("pw")
        s.add(user)
        s.commit()

    _login_user(client, username)

    page_resp = client.get("/admin/roles")
    assert page_resp.status_code == 200
    assert b"Role editing is read-only for your account." in page_resp.data

    list_resp = client.get("/api/_admin/roles")
    assert list_resp.status_code == 200

    create_resp = client.post(
        "/api/_admin/roles",
        json={"name": f"nope_{uuid.uuid4().hex[:6]}", "description": "Should fail", "permissions": []},
    )
    assert create_resp.status_code == 403


def test_cohorts_access_uses_page_permission_not_hard_coded_roles(client, app, monkeypatch):
    app.config["COHORTS_V2"] = True
    filters = parse_filters({"start": "2025-01-01", "end": "2025-12-31"})
    monkeypatch.setattr(
        "app.blueprints.customers.resolve_filters",
        lambda *_args, **_kwargs: (filters, {"source": "test"}),
    )
    monkeypatch.setattr(
        "app.blueprints.customers.filters_service.scope_from_user",
        lambda _user: {
            "is_admin": False,
            "user_id": 999,
            "scope_mode": "list",
            "scope_hash": "scope-999",
            "allowed_erp_user_ids": ["R1"],
            "allowed_customer_ids": ["C1"],
            "allowed_region_ids": [],
            "allowed_supplier_ids": [],
            "permissions_version": "1",
        },
    )
    monkeypatch.setattr(
        "app.blueprints.customers.customers_cohorts_v2.build_cohorts_payload",
        lambda *_args, **_kwargs: {"kpis": {}, "retention": {"rows": []}, "meta": {}},
    )
    monkeypatch.setattr(
        "app.blueprints.customers.customers_cohorts_v2.fetch_churn_status_list",
        lambda *_args, **_kwargs: {"rows": [{"customer_id": "C1"}], "pagination": {"total": 1}},
    )
    monkeypatch.setattr(
        "app.blueprints.customers.customers_cohorts_v2.fetch_cohort_drilldown",
        lambda *_args, **_kwargs: {"rows": [{"customer_id": "C1"}]},
    )

    allowed_username = f"viewer_{uuid.uuid4().hex[:6]}"
    denied_username = f"viewer_{uuid.uuid4().hex[:6]}"
    with SessionLocal() as s:
        allowed_user = User(
            username=allowed_username,
            email=f"{allowed_username}@example.com",
            role="viewer",
            is_active=True,
            is_approved=True,
            updated_at=datetime.now(timezone.utc),
        )
        allowed_user.set_password("pw")
        denied_user = User(
            username=denied_username,
            email=f"{denied_username}@example.com",
            role="production",
            is_active=True,
            is_approved=True,
            updated_at=datetime.now(timezone.utc),
        )
        denied_user.set_password("pw")
        s.add(allowed_user)
        s.add(denied_user)
        s.commit()
        allowed_user_id = int(allowed_user.id)

    replace_user_permission_overrides(allowed_user_id, ["page.customers.view"])

    _login_user(client, allowed_username)
    ok_page = client.get("/customers/cohorts")
    assert ok_page.status_code == 200
    ok_drill = client.get("/customers/cohorts/drilldown", query_string={"cohort": "2025-01", "month_index": "0"})
    assert ok_drill.status_code == 200
    assert ok_drill.get_json()["rows"][0]["customer_id"] == "C1"
    ok_list = client.get("/customers/churned/list", query_string={"status": "churned"})
    assert ok_list.status_code == 200

    client.get("/logout", follow_redirects=True)
    _login_user(client, denied_username)
    denied_page = client.get("/customers/cohorts")
    assert denied_page.status_code == 403


def test_filters_cache_key_varies_by_scope_hash(monkeypatch):
    filters = parse_filters({})
    user = SimpleNamespace(
        id=101,
        role="sales",
        is_authenticated=True,
        get_id=lambda: "101",
    )

    monkeypatch.setattr(
        "app.core.access_policy.scope_for_user",
        lambda _user, use_cache=True: SimpleNamespace(
            scope_mode="list",
            scope_hash="scope-a",
            permissions_version="1",
        ),
    )
    key_a = filters_cache_key(user, filters)

    monkeypatch.setattr(
        "app.core.access_policy.scope_for_user",
        lambda _user, use_cache=True: SimpleNamespace(
            scope_mode="list",
            scope_hash="scope-b",
            permissions_version="1",
        ),
    )
    key_b = filters_cache_key(user, filters)

    assert key_a != key_b
