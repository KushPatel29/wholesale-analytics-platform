import os
import uuid
from datetime import datetime, timezone

import pytest

from app import create_app
from app.auth.models import SessionLocal, User
from app.core.access_policy import scope_for_user
from app.core.exceptions import DatasetNotBuiltError
from app.services.filters import read_sticky_filters_from_session, write_sticky_filters_to_session


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
        ADMIN_USER_SELECT=True,
    )
    return app


@pytest.fixture()
def client(app):
    with app.test_client() as c:
        yield c


def _login(client, username: str, password: str):
    resp = client.post("/auth/login", data={"username": username, "password": password}, follow_redirects=True)
    assert resp.status_code == 200


def _login_admin(client):
    _login(client, "admin", "admin")


def _create_user(
    *,
    role: str = "sales",
    password: str = "pw",
    email: str | None = None,
    erp_user_id: str | None = None,
    is_active: bool = True,
    is_approved: bool = True,
):
    username = f"user_{uuid.uuid4().hex[:8]}"
    rep_id = erp_user_id or f"ERP-{uuid.uuid4().hex[:8]}"
    with SessionLocal() as s:
        user = User(
            username=username,
            email=email or f"{username}@example.com",
            role=role,
            is_active=is_active,
            is_approved=is_approved,
            erp_user_id=rep_id,
            sales_rep_id=rep_id,
            updated_at=datetime.now(timezone.utc),
        )
        user.set_password(password)
        s.add(user)
        s.commit()
        s.refresh(user)
        return user, password


def test_admin_users_search_returns_matches(client):
    _login_admin(client)
    suffix = uuid.uuid4().hex[:6]
    target_email = f"select_{suffix}@example.com"
    _create_user(email=target_email)

    resp = client.get(f"/admin/api/users/search?q={suffix}&limit=20")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "items" in data
    assert any((item.get("email") or "").lower() == target_email for item in data["items"])


def test_admin_search_endpoints_require_admin(client, monkeypatch):
    user, password = _create_user(role="sales")
    _login(client, user.username, password)

    # Stub dimension search so endpoint internals stay deterministic.
    monkeypatch.setattr(
        "app.blueprints.admin_api._search_dimension",
        lambda scope_type, query, limit, offset: [{"id": "X1", "label": f"{scope_type}:{query}"}],
    )

    users_resp = client.get("/admin/api/users/search?q=test")
    reps_resp = client.get("/admin/api/reps/search?q=test")
    customers_resp = client.get("/admin/api/customers/search?q=test")

    assert users_resp.status_code == 403
    assert reps_resp.status_code == 403
    assert customers_resp.status_code == 403


def test_reps_and_customers_search_return_results_for_admin(client, monkeypatch):
    _login_admin(client)

    def _fake_search(scope_type, query, limit, offset):
        _ = query
        start = int(offset)
        stop = int(offset) + int(limit)
        return [{"id": f"{scope_type}-{idx}", "label": f"{scope_type.upper()} {idx}"} for idx in range(start, stop)]

    monkeypatch.setattr("app.blueprints.admin_api._search_dimension", _fake_search)

    reps_resp = client.get("/admin/api/reps/search?q=rep&limit=20")
    customers_resp = client.get("/admin/api/customers/search?q=cust&limit=20")
    assert reps_resp.status_code == 200
    assert customers_resp.status_code == 200

    reps_data = reps_resp.get_json()
    cust_data = customers_resp.get_json()
    assert len(reps_data["items"]) == 20
    assert len(cust_data["items"]) == 20
    assert reps_data["pagination"]["has_more"] is True
    assert cust_data["pagination"]["has_more"] is True

    reps_all_resp = client.get("/admin/api/reps/search?q=al&limit=20")
    assert reps_all_resp.status_code == 200
    reps_all_data = reps_all_resp.get_json()
    assert reps_all_data["items"][0]["id"] == "__all__"


def test_customers_suggest_endpoint_uses_rep_ids(client, monkeypatch):
    _login_admin(client)
    captured = {"rep_ids": None, "limit": None}

    def _fake_customer_suggest(rep_ids, limit):
        captured["rep_ids"] = list(rep_ids)
        captured["limit"] = int(limit)
        return [{"id": "C-1", "label": "Customer 1"}]

    monkeypatch.setattr("app.blueprints.admin_api._customer_suggestions_for_reps", _fake_customer_suggest)
    resp = client.get("/api/_admin/customers/suggest?rep_ids=REP-1&rep_ids=REP-2&limit=10")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["items"][0]["id"] == "C-1"
    assert captured["rep_ids"] == ["REP-1", "REP-2"]
    assert captured["limit"] == 10


def test_customers_suggest_all_scope_returns_empty(client):
    _login_admin(client)
    resp = client.get("/api/_admin/customers/suggest?rep_ids=__all__&limit=10")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["items"] == []
    assert data["meta"]["auto_disabled"] == "all_scope"


def test_existing_user_assignment_does_not_create_duplicate(client):
    _login_admin(client)
    existing, _ = _create_user(role="sales")

    with SessionLocal() as s:
        before_count = s.query(User).count()

    resp = client.post(
        "/api/_admin/users",
        json={
            "existing_user_id": int(existing.id),
            "role": "sales",
            "approve": True,
            "visibility": [],
            "scope": {"sales_rep_ids": []},
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload.get("mode") == "existing"
    assert int(payload["user"]["id"]) == int(existing.id)

    with SessionLocal() as s:
        after_count = s.query(User).count()
    assert after_count == before_count


def test_existing_user_assignment_still_works_without_fact_dataset(client, monkeypatch):
    _login_admin(client)
    existing, _ = _create_user(role="sales")

    monkeypatch.setattr(
        "app.services.fact_store.list_columns",
        lambda: (_ for _ in ()).throw(DatasetNotBuiltError("dataset missing")),
    )

    resp = client.post(
        "/api/_admin/users",
        json={
            "existing_user_id": int(existing.id),
            "role": "sales",
            "approve": True,
            "visibility": [],
            "scope": {"sales_rep_ids": []},
        },
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload.get("mode") == "existing"
    assert int(payload["user"]["id"]) == int(existing.id)


def test_create_new_user_still_works(client):
    _login_admin(client)
    suffix = uuid.uuid4().hex[:8]
    email = f"new_{suffix}@example.com"
    erp_id = f"ERP-{suffix}"

    with SessionLocal() as s:
        before_count = s.query(User).count()

    resp = client.post(
        "/api/_admin/users",
        json={
            "email": email,
            "first_name": "New",
            "last_name": "User",
            "role": "sales",
            "approve": False,
            "erp_user_id": erp_id,
            "visibility": [],
            "scope": {"sales_rep_ids": []},
        },
    )
    assert resp.status_code == 201
    payload = resp.get_json()
    assert (payload["user"]["email"] or "").lower() == email

    with SessionLocal() as s:
        after_count = s.query(User).count()
    assert after_count == before_count + 1


def test_scope_update_still_works_without_fact_dataset(client, monkeypatch):
    _login_admin(client)
    target, _ = _create_user(role="sales")

    monkeypatch.setattr(
        "app.services.fact_store.list_columns",
        lambda: (_ for _ in ()).throw(DatasetNotBuiltError("dataset missing")),
    )

    resp = client.patch(
        f"/api/_admin/users/{int(target.id)}/scope",
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

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["user"]["scope"]["scope_mode"] == "all"


def test_all_scope_token_sets_user_scope_mode_all(client):
    _login_admin(client)
    suffix = uuid.uuid4().hex[:8]
    email = f"allscope_{suffix}@example.com"
    erp_id = f"ERP-{suffix}"

    resp = client.post(
        "/api/_admin/users",
        json={
            "email": email,
            "first_name": "All",
            "last_name": "Scope",
            "role": "sales",
            "approve": True,
            "erp_user_id": erp_id,
            "visibility": ["__all__"],
            "scope": {"sales_rep_ids": ["__all__"]},
        },
    )
    assert resp.status_code == 201
    payload = resp.get_json()
    assert payload["user"]["scope"]["scope_mode"] == "all"

    with SessionLocal() as s:
        user = s.query(User).filter(User.email == email).first()
        assert user is not None
        with client.application.app_context():
            scope = scope_for_user(user, use_cache=False)
        assert scope.scope_mode == "all"


def test_admin_users_default_view_shows_active_users_only(client):
    _login_admin(client)
    suffix = uuid.uuid4().hex[:6]
    active_email = f"active_{suffix}@example.com"
    disabled_email = f"disabled_{suffix}@example.com"
    _create_user(email=active_email, is_active=True, is_approved=True)
    _create_user(email=disabled_email, is_active=False, is_approved=True)

    resp = client.get(f"/api/_admin/users?query={suffix}")
    assert resp.status_code == 200
    payload = resp.get_json()

    emails = {(row.get("email") or "").lower() for row in payload["users"]}
    assert active_email in emails
    assert disabled_email not in emails
    assert payload["pagination"]["total"] == 1
    assert payload["stats"]["total"] == 2
    assert payload["stats"]["active"] == 1
    assert payload["stats"]["disabled"] == 1


def test_admin_users_list_batches_scope_and_access_hydration(client, monkeypatch):
    _login_admin(client)
    suffix = uuid.uuid4().hex[:6]
    email = f"batched_{suffix}@example.com"
    _create_user(email=email, is_active=True, is_approved=True)

    def _unexpected(*args, **kwargs):
        raise AssertionError("list_users should not do per-user role/scope/permission hydration")

    monkeypatch.setattr("app.blueprints.admin_api.list_user_role_names", _unexpected)
    monkeypatch.setattr("app.blueprints.admin_api.list_user_permission_rules", _unexpected)
    monkeypatch.setattr("app.blueprints.admin_api.list_effective_permission_keys_for_user", _unexpected)
    monkeypatch.setattr("app.blueprints.admin_api.list_user_scope_rules", _unexpected)

    resp = client.get(f"/api/_admin/users?query={suffix}&status=all")
    assert resp.status_code == 200
    payload = resp.get_json()

    assert payload["meta"]["list_mode"] == "batched"
    assert payload["meta"]["duration_ms"] >= 0
    assert payload["users"]
    assert (payload["users"][0].get("scope") or {}).get("scope_mode") in {"none", "list", "all"}


def test_admin_users_status_toggle_supports_active_disabled_and_all(client):
    _login_admin(client)
    suffix = uuid.uuid4().hex[:6]
    active_email = f"toggle_active_{suffix}@example.com"
    disabled_email = f"toggle_disabled_{suffix}@example.com"
    _create_user(email=active_email, is_active=True, is_approved=True)
    _create_user(email=disabled_email, is_active=False, is_approved=True)

    active_resp = client.get(f"/api/_admin/users?query={suffix}&status=active")
    disabled_resp = client.get(f"/api/_admin/users?query={suffix}&status=disabled")
    all_resp = client.get(f"/api/_admin/users?query={suffix}&status=all")

    assert active_resp.status_code == 200
    assert disabled_resp.status_code == 200
    assert all_resp.status_code == 200

    active_emails = {(row.get("email") or "").lower() for row in active_resp.get_json()["users"]}
    disabled_emails = {(row.get("email") or "").lower() for row in disabled_resp.get_json()["users"]}
    all_emails = {(row.get("email") or "").lower() for row in all_resp.get_json()["users"]}

    assert active_email in active_emails
    assert disabled_email not in active_emails
    assert disabled_email in disabled_emails
    assert active_email not in disabled_emails
    assert {active_email, disabled_email}.issubset(all_emails)


def test_admin_users_route_isolated_from_sticky_global_filters(client):
    _login_admin(client)
    suffix = uuid.uuid4().hex[:6]
    active_email = f"sticky_active_{suffix}@example.com"
    disabled_email = f"sticky_disabled_{suffix}@example.com"
    _create_user(email=active_email, is_active=True, is_approved=True)
    _create_user(email=disabled_email, is_active=False, is_approved=True)
    sticky_payload = {"statuses": ["Closed"], "regions": ["West"], "customers": ["C-1"]}

    with client.session_transaction() as sess:
        write_sticky_filters_to_session(sess, sticky_payload, user_id="admin")
        before = read_sticky_filters_from_session(sess, user_id="admin")

    default_resp = client.get(f"/api/_admin/users?query={suffix}")
    disabled_resp = client.get(f"/api/_admin/users?query={suffix}&status=disabled")

    assert default_resp.status_code == 200
    assert disabled_resp.status_code == 200

    default_emails = {(row.get("email") or "").lower() for row in default_resp.get_json()["users"]}
    disabled_emails = {(row.get("email") or "").lower() for row in disabled_resp.get_json()["users"]}
    assert active_email in default_emails
    assert disabled_email not in default_emails
    assert disabled_email in disabled_emails

    with client.session_transaction() as sess:
        after = read_sticky_filters_from_session(sess, user_id="admin")
    assert after == before


def test_admin_users_endpoints_disable_caching(client):
    _login_admin(client)

    page_resp = client.get("/admin/users")
    api_resp = client.get("/api/_admin/users")

    assert page_resp.status_code == 200
    assert api_resp.status_code == 200
    assert "no-store" in (page_resp.headers.get("Cache-Control") or "")
    assert "no-store" in (api_resp.headers.get("Cache-Control") or "")
    assert "Cookie" in (page_resp.headers.get("Vary") or "")
    assert "Cookie" in (api_resp.headers.get("Vary") or "")
    html = page_resp.get_data(as_text=True)
    assert "bootstrap.bundle.min.js" in html
    assert "function ensureModals()" in html
    assert "const scopeModal = new bootstrap.Modal" not in html
    assert "let usersLoadController = null;" in html
    assert "usersLoadController.abort();" in html
    assert "let accessLoadController = null;" in html
    assert "accessLoadController.abort();" in html
    assert "let auditLoadController = null;" in html
    assert "auditLoadController.abort();" in html
    assert "let previewLoadController = null;" in html
