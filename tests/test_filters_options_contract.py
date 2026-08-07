import copy

import pytest


@pytest.fixture
def authed_client(client, monkeypatch):
    class _DummyUser:
        is_authenticated = True
        is_active = True
        is_anonymous = False
        role = "admin"

        def get_id(self):
            return "admin"

    monkeypatch.setattr("flask_login.utils._get_user", lambda *a, **k: _DummyUser())
    return client


def _stub_options_payload():
    return {
        "options": {
            "regions": [{"id": "west", "label": "West", "bucket": "regions"}],
            "methods": [{"id": "ground", "label": "Ground", "bucket": "methods"}],
            "ship_methods": [{"id": "ground", "label": "Ground", "bucket": "ship_methods"}],
            "customers": [{"id": "2914", "label": "truLOCAL", "bucket": "customers"}],
            "suppliers": [{"id": "1899", "label": "1899", "bucket": "suppliers"}],
            "products": [],
            "sales_reps": [],
            "statuses": [],
        },
        "dataset_version": "v1",
        "date_min": "2023-01-01",
        "date_max": "2023-12-31",
        "filters": {},
        "scope": {},
        "cached": False,
    }


def test_filters_options_contract(monkeypatch, authed_client):
    monkeypatch.setattr(
        "app.services.filters_service.get_filter_options",
        lambda *a, **k: copy.deepcopy(_stub_options_payload()),
    )
    resp = authed_client.get("/api/filters/options")
    assert resp.status_code == 200
    data = resp.get_json()
    opts = data["options"]

    for key, items in opts.items():
        assert isinstance(items, list)
        assert all(item is not None for item in items)
        for item in items:
            assert isinstance(item, dict)
            assert "id" in item
            assert "label" in item
            assert "bucket" in item
