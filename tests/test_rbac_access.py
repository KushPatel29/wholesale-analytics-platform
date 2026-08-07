import os
import uuid

import pandas as pd
import pytest
from werkzeug.exceptions import HTTPException

from app import create_app
from app.auth.models import SessionLocal, User
from app.core.access_policy import AccessScope, enforce_entity_access
from app.services import fact_store


@pytest.fixture()
def secured_app():
    os.environ.setdefault("FLASK_ENV", "testing")
    os.environ.setdefault("WTF_CSRF_ENABLED", "false")
    os.environ.setdefault("WA_FAST_PWHASH", "1")
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test", LOGIN_DISABLED=False, AUTHZ_DISABLED=False)
    return app


@pytest.fixture()
def secured_client(secured_app):
    with secured_app.test_client() as client:
        yield client


def _create_user(role: str = "sales", erp_user_id: str | None = None, password: str = "pw"):
    username = f"test.{uuid.uuid4().hex[:8]}"
    email = f"{username}@example.com"
    if not erp_user_id:
        erp_user_id = f"rep-{uuid.uuid4().hex[:8]}"
    with SessionLocal() as s:
        user = User(
            username=username,
            email=email,
            role=role,
            is_active=True,
            is_approved=True,
            erp_user_id=erp_user_id,
            sales_rep_id=erp_user_id,
        )
        user.set_password(password)
        s.add(user)
        s.commit()
        s.refresh(user)
        return user, password


def _set_fact_path(tmp_path, df: pd.DataFrame):
    path = tmp_path / "fact.parquet"
    df.to_parquet(path, index=False)
    orig_path = fact_store.FACT_PATH
    orig_env = os.environ.get("PARQUET_PATH")
    os.environ["PARQUET_PATH"] = str(path)
    fact_store.FACT_PATH = path
    fact_store.reset_duckdb_state()
    return orig_path, orig_env


def _restore_fact_path(orig_path, orig_env):
    fact_store.FACT_PATH = orig_path
    if orig_env is None:
        os.environ.pop("PARQUET_PATH", None)
    else:
        os.environ["PARQUET_PATH"] = orig_env
    fact_store.reset_duckdb_state()


def test_anonymous_requires_login(secured_client):
    resp = secured_client.get("/")
    assert resp.status_code in (302, 401)
    if resp.status_code == 302:
        assert "/login" in (resp.headers.get("Location") or "")

    resp = secured_client.get("/customers")
    assert resp.status_code in (302, 401)

    resp = secured_client.get("/api/overview/summary")
    assert resp.status_code == 401


def test_non_admin_forbidden_admin_routes(secured_client):
    user, password = _create_user(role="sales")
    secured_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = secured_client.get("/admin/users")
    assert resp.status_code == 403

    resp = secured_client.get("/api/_admin/users")
    assert resp.status_code == 403


def test_scope_list_restricts_query(tmp_path):
    df = pd.DataFrame(
        {
            "PrimarySalesRepUserId": ["rep-1", "rep-2", "rep-1"],
            "Revenue": [100, 200, 300],
            "OrderId": [1, 2, 3],
        }
    )
    orig_path, orig_env = _set_fact_path(tmp_path, df)
    try:
        scope_admin = {"scope_mode": "all", "allowed_erp_user_ids": []}
        scope_rep1 = {"scope_mode": "list", "allowed_erp_user_ids": ["rep-1"]}
        admin_df = fact_store.query_fact(filters=None, scope=scope_admin, apply_default_window=False, use_cache=False)
        rep_df = fact_store.query_fact(filters=None, scope=scope_rep1, apply_default_window=False, use_cache=False)
        assert len(admin_df) == 3
        assert len(rep_df) == 2
    finally:
        _restore_fact_path(orig_path, orig_env)


def test_scope_list_restricts_customer_region_supplier(tmp_path):
    df = pd.DataFrame(
        {
            "PrimarySalesRepUserId": ["rep-1", "rep-1", "rep-1"],
            "CustomerId": ["C-1", "C-2", "C-1"],
            "RegionName": ["West", "West", "East"],
            "SupplierId": ["S-1", "S-1", "S-2"],
            "Revenue": [100, 200, 300],
            "OrderId": [1, 2, 3],
        }
    )
    orig_path, orig_env = _set_fact_path(tmp_path, df)
    try:
        scope = {
            "scope_mode": "list",
            "allowed_erp_user_ids": ["rep-1"],
            "allowed_customer_ids": ["C-1"],
            "allowed_region_ids": ["West"],
            "allowed_supplier_ids": ["S-1"],
        }
        scoped_df = fact_store.query_fact(filters=None, scope=scope, apply_default_window=False, use_cache=False)
        assert len(scoped_df) == 1
        assert str(scoped_df.iloc[0]["CustomerId"]) == "C-1"
        assert str(scoped_df.iloc[0]["RegionName"]) == "West"
        assert str(scoped_df.iloc[0]["SupplierId"]) == "S-1"
    finally:
        _restore_fact_path(orig_path, orig_env)


def test_drilldown_denies_out_of_scope(tmp_path, secured_app):
    df = pd.DataFrame(
        {
            "CustomerId": ["C-1", "C-2"],
            "PrimarySalesRepUserId": ["rep-1", "rep-2"],
            "Revenue": [10, 20],
        }
    )
    orig_path, orig_env = _set_fact_path(tmp_path, df)
    scope = AccessScope(
        is_admin=False,
        user_id=1,
        erp_user_id="rep-1",
        allowed_erp_user_ids=["rep-1"],
        scope_mode="list",
        permissions_version="1",
        scope_hash="test",
    )
    try:
        with secured_app.app_context():
            with pytest.raises(HTTPException) as exc:
                enforce_entity_access("customer", "C-2", scope)
            assert exc.value.code in (403, 404)
    finally:
        _restore_fact_path(orig_path, orig_env)
