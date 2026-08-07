from __future__ import annotations

from typing import Any

import pytest
from flask import request

from app.services.filters import STICKY_FILTERS_SESSION_KEY, filters_to_store


class _DummyUser:
    is_authenticated = True
    is_active = True
    is_anonymous = False
    role = "admin"

    def __init__(self, user_id: str):
        self._user_id = user_id

    def get_id(self) -> str:
        return self._user_id


def _stub_options_payload(params: Any) -> dict[str, Any]:
    return {
        "options": {
            "regions": [{"id": "West", "label": "West", "bucket": "regions"}],
            "methods": [{"id": "Ground", "label": "Ground", "bucket": "methods"}],
            "ship_methods": [{"id": "Ground", "label": "Ground", "bucket": "ship_methods"}],
            "customers": [],
            "suppliers": [],
            "products": [],
            "sales_reps": [],
            "statuses": [],
        },
        "dataset_version": "sticky-test-v1",
        "date_min": "2023-01-01",
        "date_max": "2026-01-31",
        "filters": filters_to_store(params),
        "scope": {},
        "cached": False,
    }


@pytest.fixture
def sticky_client(app, client, monkeypatch):
    app.config["STICKY_FILTERS"] = True
    app.config["FILTERS_CANONICAL_V2"] = False

    def _fake_get_user(*_args, **_kwargs):
        uid = request.headers.get("X-User", "u1")
        return _DummyUser(uid)

    monkeypatch.setattr("flask_login.utils._get_user", _fake_get_user)
    monkeypatch.setattr(
        "app.services.filters_service.get_filter_options",
        lambda params, scope: _stub_options_payload(params),
    )
    return client


def test_sticky_filters_persist_across_requests_and_schema(sticky_client):
    first = sticky_client.get("/api/filters/options?regions=West&date_preset=last_30_days&_gf=1", headers={"X-User": "u1"})
    assert first.status_code == 200
    first_payload = first.get_json()
    assert first_payload["filters"]["regions"] == ["West"]
    assert first_payload["filters"]["date_preset"] == "last_30_days"

    second = sticky_client.get("/api/filters/options", headers={"X-User": "u1"})
    assert second.status_code == 200
    second_payload = second.get_json()
    assert second_payload["filters"]["regions"] == ["West"]
    assert second_payload["filters"]["date_preset"] == "last_30_days"

    schema = sticky_client.get("/api/filters/schema", headers={"X-User": "u1"})
    assert schema.status_code == 200
    schema_payload = schema.get_json()
    assert schema_payload["defaults"]["regions"] == ["West"]
    assert schema_payload["defaults"]["date_preset"] == "last_30_days"


def test_sticky_filters_url_override_updates_session(sticky_client):
    sticky_client.get("/api/filters/options?regions=West&_gf=1", headers={"X-User": "u1"})
    override = sticky_client.get("/api/filters/options?regions=East&_gf=1", headers={"X-User": "u1"})
    assert override.status_code == 200
    assert override.get_json()["filters"]["regions"] == ["East"]

    follow_up = sticky_client.get("/api/filters/options", headers={"X-User": "u1"})
    assert follow_up.status_code == 200
    assert follow_up.get_json()["filters"]["regions"] == ["East"]

    with sticky_client.session_transaction() as sess:
        stored = sess.get(STICKY_FILTERS_SESSION_KEY) or {}
        assert (stored.get("filters") or {}).get("regions") == ["East"]


def test_reset_clears_sticky_filters(sticky_client):
    sticky_client.get("/api/filters/options?regions=West&_gf=1", headers={"X-User": "u1"})
    with sticky_client.session_transaction() as sess:
        assert STICKY_FILTERS_SESSION_KEY in sess

    reset = sticky_client.get("/api/filters/options?_gf=1&_gf_reset=1", headers={"X-User": "u1"})
    assert reset.status_code == 200

    with sticky_client.session_transaction() as sess:
        assert STICKY_FILTERS_SESSION_KEY not in sess
        assert "filters" not in sess
        assert "global_filters" not in sess

    after_reset = sticky_client.get("/api/filters/options", headers={"X-User": "u1"})
    assert after_reset.status_code == 200
    assert after_reset.get_json()["filters"]["regions"] == []


def test_sticky_filters_isolated_by_session_and_user(app, monkeypatch):
    app.config["STICKY_FILTERS"] = True
    app.config["FILTERS_CANONICAL_V2"] = False

    def _fake_get_user(*_args, **_kwargs):
        uid = request.headers.get("X-User", "u1")
        return _DummyUser(uid)

    monkeypatch.setattr("flask_login.utils._get_user", _fake_get_user)
    monkeypatch.setattr(
        "app.services.filters_service.get_filter_options",
        lambda params, scope: _stub_options_payload(params),
    )

    c1 = app.test_client()
    c2 = app.test_client()
    r1 = c1.get("/api/filters/options?regions=West&_gf=1", headers={"X-User": "u1"})
    r2 = c2.get("/api/filters/options?regions=East&_gf=1", headers={"X-User": "u2"})
    assert r1.status_code == 200
    assert r2.status_code == 200

    c1_follow = c1.get("/api/filters/options", headers={"X-User": "u1"})
    c2_follow = c2.get("/api/filters/options", headers={"X-User": "u2"})
    assert c1_follow.status_code == 200
    assert c2_follow.status_code == 200
    assert c1_follow.get_json()["filters"]["regions"] == ["West"]
    assert c2_follow.get_json()["filters"]["regions"] == ["East"]


def test_sticky_filters_ignore_drilldown_salesrep_id(sticky_client):
    seeded = sticky_client.get("/api/filters/options?regions=West&_gf=1", headers={"X-User": "u1"})
    assert seeded.status_code == 200
    assert seeded.get_json()["filters"]["regions"] == ["West"]

    drilldown_like = sticky_client.get(
        "/api/filters/options?salesrep_id=R1&start=2025-01-01&end=2025-01-31&_gf=1",
        headers={"X-User": "u1"},
    )
    assert drilldown_like.status_code == 200
    payload = drilldown_like.get_json()
    assert payload["filters"].get("sales_reps") == []

    with sticky_client.session_transaction() as sess:
        stored = (sess.get(STICKY_FILTERS_SESSION_KEY) or {}).get("filters") or {}
        assert stored.get("sales_reps") == []
