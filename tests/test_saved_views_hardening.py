from __future__ import annotations

import copy
import json

import pytest

from app.auth.models import SavedView, get_saved_view, get_session, save_view
from app.services.filters import ACTIVE_SAVED_VIEW_SESSION_KEY, filters_to_store


@pytest.fixture
def authed_views_client(client, monkeypatch):
    class _DummyUser:
        id = 1
        role = "admin"
        is_authenticated = True
        is_active = True
        is_anonymous = False

        def get_id(self):
            return "1"

    monkeypatch.setattr("flask_login.utils._get_user", lambda *args, **kwargs: _DummyUser())
    client.application.config["FILTERS_CANONICAL_V2"] = False
    client.application.config["STICKY_FILTERS"] = True
    with get_session() as s:
        s.query(SavedView).filter(SavedView.user_id.in_([1, 2])).delete(synchronize_session=False)
        s.commit()
    yield client
    with get_session() as s:
        s.query(SavedView).filter(SavedView.user_id.in_([1, 2])).delete(synchronize_session=False)
        s.commit()


def _scoped_options_payload(params):
    return {
        "options": {
            "regions": [{"id": "West", "label": "West", "bucket": "regions"}],
            "methods": [{"id": "Ground", "label": "Ground", "bucket": "methods"}],
            "ship_methods": [{"id": "Ground", "label": "Ground", "bucket": "ship_methods"}],
            "customers": [{"id": "Customer 1", "label": "Customer 1", "bucket": "customers"}],
            "suppliers": [{"id": "Supplier 1", "label": "Supplier 1", "bucket": "suppliers"}],
            "products": [{"id": "Product 1", "label": "Product 1", "bucket": "products"}],
            "sales_reps": [{"id": "Rep 1", "label": "Rep 1", "bucket": "sales_reps"}],
            "statuses": [{"id": "packed", "label": "packed", "bucket": "statuses"}],
        },
        "dataset_version": "saved-views-hardening",
        "date_min": "2024-01-01",
        "date_max": "2026-12-31",
        "filters": filters_to_store(params) if hasattr(params, "start") else dict(params or {}),
        "scope": {},
        "cached": False,
    }


def test_save_view_uses_pending_posted_filters(monkeypatch, authed_views_client):
    monkeypatch.setattr(
        "app.services.filters_service.get_filter_options",
        lambda params, *_args, **_kwargs: copy.deepcopy(_scoped_options_payload(params)),
    )
    with authed_views_client.session_transaction() as sess:
        sess["filters"] = {"regions": ["East"], "customers": ["Customer 9"]}

    resp = authed_views_client.post(
        "/views/save",
        data={
            "name": "Pending Snapshot",
            "next": "/",
            "regions": ["West"],
            "customers": ["Customer 1"],
            "products": ["Product 1"],
        },
        follow_redirects=False,
    )

    assert resp.status_code in {302, 303}
    with get_session() as s:
        view = (
            s.query(SavedView)
            .filter(SavedView.user_id == 1, SavedView.name == "Pending Snapshot")
            .order_by(SavedView.id.desc())
            .first()
        )
        assert view is not None
        payload = json.loads(view.filters_json or "{}")
        assert payload.get("regions") == ["West"]
        assert payload.get("customers") == ["Customer 1"]
        assert payload.get("products") == ["Product 1"]

    with authed_views_client.session_transaction() as sess:
        assert isinstance(sess.get(ACTIVE_SAVED_VIEW_SESSION_KEY), int)


def test_load_saved_view_sanitizes_stale_scope_values(monkeypatch, authed_views_client):
    monkeypatch.setattr(
        "app.services.filters_service.get_filter_options",
        lambda params, *_args, **_kwargs: copy.deepcopy(_scoped_options_payload(params)),
    )
    view_id = save_view(
        1,
        "Restricted Snapshot",
        json.dumps(
            {
                "regions": ["East"],
                "customers": ["Customer 9"],
                "products": ["Product 9"],
                "statuses": ["forbidden"],
            }
        ),
    )

    resp = authed_views_client.post(f"/views/load/{view_id}", data={"next": "/"}, follow_redirects=False)
    assert resp.status_code in {302, 303}

    with authed_views_client.session_transaction() as sess:
        assert sess.get(ACTIVE_SAVED_VIEW_SESSION_KEY) == view_id
        stored = sess.get("filters") or {}
        assert stored.get("regions") == []
        assert stored.get("customers") == []
        assert stored.get("products") == []
        assert stored.get("statuses") == []
        flashes = sess.get("_flashes") or []
        assert any("removed because they are no longer available" in str(message) for _category, message in flashes)


def test_saved_view_routes_are_owner_only(authed_views_client):
    view_id = save_view(2, "Other User View", json.dumps({"regions": ["West"]}))

    load_resp = authed_views_client.post(f"/views/load/{view_id}", data={"next": "/"}, follow_redirects=False)
    update_resp = authed_views_client.post(
        f"/views/update/{view_id}",
        data={"next": "/", "name": "Hijacked", "regions": ["East"]},
        follow_redirects=False,
    )
    delete_resp = authed_views_client.post(f"/views/delete/{view_id}", data={"next": "/"}, follow_redirects=False)

    assert load_resp.status_code in {302, 303}
    assert update_resp.status_code in {302, 303}
    assert delete_resp.status_code in {302, 303}

    existing = get_saved_view(view_id)
    assert existing is not None
    assert existing.name == "Other User View"
    assert json.loads(existing.filters_json or "{}").get("regions") == ["West"]

    with authed_views_client.session_transaction() as sess:
        assert sess.get(ACTIVE_SAVED_VIEW_SESSION_KEY) is None
        flashes = sess.get("_flashes") or []
        assert sum(1 for _category, message in flashes if str(message) == "View not found.") == 3
