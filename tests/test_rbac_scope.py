import os
import uuid

import pytest

from app import create_app
from app.auth.models import SessionLocal, User, UserVisibilitySalesRep, UserScopeRule
from app.core import access_policy


@pytest.fixture(scope="session")
def app():
    os.environ.setdefault("FLASK_ENV", "testing")
    os.environ.setdefault("WTF_CSRF_ENABLED", "false")
    os.environ.setdefault("WA_FAST_PWHASH", "1")
    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, SECRET_KEY="test", LOGIN_DISABLED=False, AUTHZ_DISABLED=False)
    return app


def _create_user(role: str, erp_user_id: str):
    username = f"scope.{uuid.uuid4().hex[:8]}"
    email = f"{username}@example.com"
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
        user.set_password("pw")
        s.add(user)
        s.commit()
        s.refresh(user)
        return user


def test_admin_scope_is_all(app):
    user = type("U", (), {"role": "admin", "id": 1, "erp_user_id": None, "is_authenticated": True})
    with app.app_context():
        scope = access_policy.scope_for_user(user, use_cache=False)
    assert scope.is_admin is True
    assert scope.scope_mode == "all"


def test_non_admin_without_visibility_has_none(app):
    user = type("U", (), {"role": "sales", "id": None, "erp_user_id": None, "is_authenticated": True})
    with app.app_context():
        scope = access_policy.scope_for_user(user, use_cache=False)
    assert scope.is_admin is False
    assert scope.scope_mode == "none"


def test_non_admin_visibility_list_loaded(app):
    erp_id = f"rep-{uuid.uuid4().hex[:6]}"
    user = _create_user("sales", erp_id)
    with SessionLocal() as s:
        s.add(UserVisibilitySalesRep(app_user_id=user.id, visible_erp_user_id=erp_id))
        s.commit()
    with app.app_context():
        scope = access_policy.scope_for_user(user, use_cache=False)
    assert scope.scope_mode == "list"
    assert erp_id in scope.allowed_erp_user_ids


def test_non_admin_scope_rules_include_customer_region_supplier(app):
    erp_id = f"rep-{uuid.uuid4().hex[:6]}"
    user = _create_user("sales", erp_id)
    with SessionLocal() as s:
        s.add(UserVisibilitySalesRep(app_user_id=user.id, visible_erp_user_id=erp_id))
        s.add(UserScopeRule(user_id=user.id, scope_type="customer", scope_value="CUST-1", scope_mode="allow"))
        s.add(UserScopeRule(user_id=user.id, scope_type="region", scope_value="West", scope_mode="allow"))
        s.add(UserScopeRule(user_id=user.id, scope_type="supplier", scope_value="SUP-1", scope_mode="allow"))
        s.commit()
    with app.app_context():
        scope = access_policy.scope_for_user(user, use_cache=False)
    assert scope.scope_mode == "list"
    assert "cust-1" in scope.allowed_customer_ids
    assert "west" in scope.allowed_region_ids
    assert "sup-1" in scope.allowed_supplier_ids


def test_non_rep_scope_does_not_force_self_rep_fallback(app):
    erp_id = f"rep-{uuid.uuid4().hex[:6]}"
    user = _create_user("sales_manager", erp_id)
    with SessionLocal() as s:
        s.add(UserScopeRule(user_id=user.id, scope_type="region", scope_value="West", scope_mode="allow"))
        s.commit()
    with app.app_context():
        scope = access_policy.scope_for_user(user, use_cache=False)
    assert scope.scope_mode == "list"
    assert scope.allowed_erp_user_ids == []
    assert "west" in scope.allowed_region_ids
