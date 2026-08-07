import copy

import pytest
from flask import request

from app.blueprints import filters_api
from app.services import filters as canonical_filters
from app.services import filters_service
from app.services.filters import FilterParams, filters_to_store


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
    client.application.config["FILTERS_CANONICAL_V2"] = False
    client.application.config["STICKY_FILTERS"] = True
    return client


def _stub_options_payload():
    return {
        "options": {
            "regions": [{"id": "west", "label": "West", "bucket": "regions"}],
            "methods": [{"id": "ground", "label": "Ground", "bucket": "methods"}],
            "ship_methods": [{"id": "ground", "label": "Ground", "bucket": "ship_methods"}],
            "customers": [{"id": "Customer 1", "label": "Customer 1", "bucket": "customers"}],
            "suppliers": [],
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


def _safe_filters_payload(params):
    if hasattr(params, "start"):
        return filters_to_store(params)
    if isinstance(params, dict):
        return dict(params)
    return {}


def test_filters_options_etag_and_schema(monkeypatch, authed_client):
    monkeypatch.setattr(
        "app.services.filters_service.get_filter_options",
        lambda *a, **k: copy.deepcopy(_stub_options_payload()),
    )

    resp = authed_client.get("/api/filters/options")
    assert resp.status_code == 200
    data = resp.get_json()
    for key in ("options", "dataset_version", "date_min", "date_max"):
        assert key in data
    opts = data["options"]
    for key in ("regions", "methods", "ship_methods", "customers", "suppliers", "products", "sales_reps", "statuses"):
        assert key in opts

    etag = resp.headers.get("ETag")
    assert etag
    cache_header = resp.headers.get("Cache-Control", "")
    assert "no-store" in cache_header

    resp_304 = authed_client.get("/api/filters/options", headers={"If-None-Match": etag})
    assert resp_304.status_code == 304
    assert resp_304.get_data() in (b"",)


def test_filters_schema_uses_canonical_field_names(authed_client):
    resp = authed_client.get("/api/filters/schema")
    assert resp.status_code == 200
    data = resp.get_json()
    names = {field["name"] for field in data["fields"]}
    assert {"start_date", "end_date", "regions", "customers", "suppliers", "products", "sales_reps", "shipping_methods"} <= names


def test_filters_options_sanitizes_unauthorized_values_and_reports_notice(monkeypatch, authed_client):
    def _stub(params, *_args, **_kwargs):
        payload = copy.deepcopy(_stub_options_payload())
        payload["filters"] = _safe_filters_payload(params)
        return payload

    monkeypatch.setattr("app.services.filters_service.get_filter_options", _stub)

    resp = authed_client.get("/api/filters/options?regions=East&customers=Customer%209")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["filters"]["regions"] == []
    assert data["filters"]["customers"] == []
    assert data["meta"]["sanitized"] is True
    assert data["meta"]["dropped_filters"]["regions"] == ["East"]
    assert data["meta"]["dropped_filters"]["customers"] == ["Customer 9"]
    assert data["meta"]["filters_notice"]


def test_filters_options_sanitizes_status_only_tampering(monkeypatch, authed_client):
    def _stub(params, *_args, **_kwargs):
        payload = copy.deepcopy(_stub_options_payload())
        payload["filters"] = _safe_filters_payload(params)
        return payload

    monkeypatch.setattr("app.services.filters_service.get_filter_options", _stub)

    resp = authed_client.get("/api/filters/options?statuses=forbidden")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["filters"]["statuses"] == []
    assert data["meta"]["sanitized"] is True
    assert data["meta"]["dropped_filters"]["statuses"] == ["forbidden"]
    assert data["meta"]["filters_notice"]


def test_filters_options_only_requests_needed_dimensions(monkeypatch, authed_client):
    captured = {}

    def _stub(params, _scope, *, requested_keys=None, **_kwargs):
        captured["requested_keys"] = tuple(requested_keys or ())
        payload = copy.deepcopy(_stub_options_payload())
        payload["options"] = {
            "regions": payload["options"]["regions"],
            "methods": payload["options"]["methods"],
            "ship_methods": payload["options"]["ship_methods"],
        }
        payload["filters"] = _safe_filters_payload(params)
        return payload

    monkeypatch.setattr("app.services.filters_service.get_filter_options", _stub)

    resp = authed_client.get("/api/filters/options?dimensions=regions,methods")
    assert resp.status_code == 200
    data = resp.get_json()

    assert captured["requested_keys"] == ("regions", "methods", "ship_methods")
    assert data["meta"]["requested_dimensions"] == ["regions", "methods", "ship_methods"]
    assert set(data["options"].keys()) == {"regions", "methods", "ship_methods"}


def test_validate_filters_supports_legacy_loader_signature(monkeypatch):
    monkeypatch.setattr(
        "app.services.filters_service.get_filter_options",
        lambda _params, _scope: copy.deepcopy(_stub_options_payload()),
    )

    params, meta = filters_service.validate_filters(
        {"regions": ["west"]},
        scope={"scope_mode": "all", "is_admin": True, "scope_hash": "scope-all"},
    )

    assert tuple(params.regions) == ("west",)
    assert meta["validation_degraded"] is False


def test_filters_options_degraded_fallback_returns_200(monkeypatch, authed_client):
    monkeypatch.setattr(
        "app.services.filters_service.get_filter_options",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("options backend exploded")),
    )

    resp = authed_client.get("/api/filters/options?dimensions=regions")
    assert resp.status_code == 200
    data = resp.get_json()

    assert data["meta"]["degraded"] is True
    assert data["meta"]["requested_dimensions"] == ["regions"]
    assert data["meta"]["option_counts"]["regions"] == 0
    assert data["options"]["regions"] == []


def test_validate_filters_uses_options_contract_for_selected_dimensions(monkeypatch):
    def _stub(params, _scope, *, requested_keys=None, **_kwargs):
        payload = copy.deepcopy(_stub_options_payload())
        payload["filters"] = _safe_filters_payload(params)
        return payload

    monkeypatch.setattr("app.services.filters_service.get_filter_options", _stub)

    params, meta = filters_service.validate_filters(
        {"regions": ["East"], "customers": ["Customer 1"], "statuses": ["forbidden"]},
        scope={"scope_mode": "all", "is_admin": True, "scope_hash": "scope-all"},
    )

    assert tuple(params.regions) == ()
    assert tuple(params.customers) == ("Customer 1",)
    assert tuple(params.statuses) == ()
    assert meta["sanitized"] is True
    assert meta["dropped_filters"]["regions"] == ["East"]
    assert meta["dropped_filters"]["statuses"] == ["forbidden"]


def test_filters_apply_api_persists_server_validated_state(monkeypatch, authed_client):
    def _stub(params, _scope, *, requested_keys=None, **_kwargs):
        payload = copy.deepcopy(_stub_options_payload())
        payload["filters"] = _safe_filters_payload(params)
        return payload

    monkeypatch.setattr("app.services.filters_service.get_filter_options", _stub)

    resp = authed_client.post(
        "/api/filters/apply",
        data={"regions": ["East"], "customers": ["Customer 1"], "statuses": ["forbidden"]},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["filters"]["regions"] == []
    assert data["filters"]["customers"] == ["Customer 1"]
    assert data["filters"]["statuses"] == []
    assert data["meta"]["sanitized"] is True
    assert data["meta"]["dropped_filters"]["regions"] == ["East"]
    assert data["meta"]["dropped_filters"]["statuses"] == ["forbidden"]
    assert data["last_applied_at"]

    with authed_client.session_transaction() as sess:
        stored = (sess.get("global_filters_v1") or {}).get("filters") or {}
        assert stored.get("regions") == []
        assert stored.get("customers") == ["Customer 1"]
        assert stored.get("statuses") == []


def test_filters_reset_api_clears_sticky_state(monkeypatch, authed_client):
    def _stub(params, _scope, *, requested_keys=None, **_kwargs):
        payload = copy.deepcopy(_stub_options_payload())
        payload["filters"] = _safe_filters_payload(params)
        return payload

    monkeypatch.setattr("app.services.filters_service.get_filter_options", _stub)

    authed_client.get("/api/filters/options?regions=west&customers=Customer%201&_gf=1")
    resp = authed_client.post("/api/filters/reset", data={})

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["filters"]["regions"] == []
    assert data["filters"]["customers"] == []
    assert data["meta"]["action"] == "reset"
    assert data["last_applied_at"]


def test_sanitize_filters_does_not_fetch_all_time_options_by_default(monkeypatch):
    monkeypatch.setattr(canonical_filters, "_OPTIONS_ALLOWLIST_SANITIZE", False)
    monkeypatch.setattr(
        "app.services.filters_service.get_filter_options",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected options fetch")),
    )

    params, meta = canonical_filters.sanitize_filters(
        {"regions": ["West"]},
        scope={"is_admin": True, "scope_mode": "all"},
        include_meta=True,
    )

    assert tuple(params.regions) == ("West",)
    assert meta["sanitized"] is False


def test_get_filter_options_reuses_request_cache(app, monkeypatch):
    calls = {"count": 0}
    filters_service._OPTIONS_CACHE.clear()

    def _stub(filters, scope, *, requested_keys=None):
        calls["count"] += 1
        return {
            "options": {"regions": [{"id": "west", "label": "West", "bucket": "regions"}]},
            "dataset_version": "v-request-cache",
            "duration_ms": 12,
            "filters": filters_to_store(filters),
            "scope": scope,
            "meta": {"requested_dimensions": list(requested_keys or ("regions",))},
        }

    monkeypatch.setattr(filters_service, "_options_payload", _stub)
    monkeypatch.setattr(filters_service.fact_store, "cache_buster", lambda: "v-request-cache")

    with app.test_request_context("/api/filters/options?dimensions=regions"):
        scope = {"scope_mode": "all", "allowed_count": 0, "scope_hash": "scope-all", "permissions_version": "v1"}
        first = filters_service.get_filter_options({}, scope, requested_keys=("regions",))
        second = filters_service.get_filter_options({}, scope, requested_keys=("regions",))

    assert calls["count"] == 1
    assert second["meta"]["cached"] is True
    assert second["meta"]["cache_key"] == first["meta"]["cache_key"]


def test_options_payload_batches_uncached_dimensions_into_single_query(monkeypatch):
    filters_service._OPTION_GROUP_CACHE.clear()
    filters_service._OPTION_GROUP_STALE_CACHE.clear()

    monkeypatch.setattr(
        filters_service.fact_store,
        "list_columns",
        lambda: {"RegionName", "ShippingMethodName", "CustomerName", "Date"},
    )
    monkeypatch.setattr(
        filters_service.fact_store,
        "build_where_clause",
        lambda *_args, **_kwargs: ("1=1", [], "2025-01-01", "2025-01-31"),
    )
    monkeypatch.setattr(filters_service.fact_store, "get_conn", lambda: object())
    monkeypatch.setattr(filters_service.fact_store, "cache_buster", lambda: "v-batched-options")
    monkeypatch.setattr(filters_service.fact_store, "get_meta", lambda: {"date_min": "2025-01-01", "date_max": "2025-01-31"})

    calls = []

    def _stub_query(_conn, _cols, _where_sql, _params, *, group_keys):
        calls.append(tuple(group_keys))
        return {
            "options": {
                "regions": [{"id": "west", "label": "West", "bucket": "regions"}],
                "methods": [{"id": "ground", "label": "Ground", "bucket": "methods"}],
                "ship_methods": [{"id": "ground", "label": "Ground", "bucket": "ship_methods"}],
                "customers": [{"id": "Customer 1", "label": "Customer 1", "bucket": "customers"}],
            },
            "duration_ms": 37,
            "status": "ok",
        }

    monkeypatch.setattr(filters_service, "_query_option_group", _stub_query)

    scope = {"scope_mode": "all", "allowed_count": 0, "scope_hash": "scope-all", "permissions_version": "v1"}
    payload = filters_service._options_payload(FilterParams(), scope, requested_keys=("regions", "methods", "customers"))

    assert calls == [("regions", "methods", "ship_methods", "customers")]
    assert payload["options"]["regions"][0]["id"] == "west"
    assert payload["options"]["methods"][0]["id"] == "ground"
    assert payload["options"]["customers"][0]["id"] == "Customer 1"
    assert payload["meta"]["dimension_meta"]["regions"]["cached"] is False
    assert payload["meta"]["dimension_meta"]["methods"]["cached"] is False
    assert payload["meta"]["dimension_meta"]["customers"]["cached"] is False
    assert payload["meta"]["degraded"] is False


def test_get_filter_options_uses_stale_cache_when_builder_fails(app, monkeypatch):
    calls = {"count": 0}
    filters_service._OPTIONS_CACHE.clear()
    filters_service._OPTIONS_STALE_CACHE.clear()

    def _stub(filters, scope, *, requested_keys=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "options": {"regions": [{"id": "west", "label": "West", "bucket": "regions"}]},
                "dataset_version": "v-stale-cache",
                "duration_ms": 12,
                "filters": filters_to_store(filters),
                "scope": scope,
                "meta": {"requested_dimensions": list(requested_keys or ("regions",))},
            }
        raise RuntimeError("options backend exploded")

    monkeypatch.setattr(filters_service, "_options_payload", _stub)
    monkeypatch.setattr(filters_service.fact_store, "cache_buster", lambda: "v-stale-cache")

    scope = {"scope_mode": "all", "allowed_count": 0, "scope_hash": "scope-all", "permissions_version": "v1"}

    with app.test_request_context("/api/filters/options?dimensions=regions"):
        fresh = filters_service.get_filter_options({}, scope, requested_keys=("regions",))

    filters_service._OPTIONS_CACHE.clear()

    with app.test_request_context("/api/filters/options?dimensions=regions"):
        stale = filters_service.get_filter_options({}, scope, requested_keys=("regions",))

    assert calls["count"] == 2
    assert fresh["meta"]["degraded"] is False
    assert stale["options"]["regions"] == [{"id": "west", "label": "West", "bucket": "regions", "value": "west"}]
    assert stale["meta"]["stale"] is True
    assert stale["meta"]["degraded"] is True
    assert stale["meta"]["stale_error"] == "options backend exploded"

def test_resolve_filters_reuses_request_cache(app, monkeypatch):
    calls = {"count": 0}

    def _stub(*_args, **_kwargs):
        calls["count"] += 1
        return FilterParams(regions=("West",)), {"source": "explicit_request", "filters_source": "querystring"}

    monkeypatch.setattr(canonical_filters, "_resolve_filters_from_source", _stub)

    with app.test_request_context("/api/filters/options?regions=West"):
        first = canonical_filters.resolve_filters(request, None, session_obj={}, source=request.args, sticky_enabled=True, update_session=False)
        second = canonical_filters.resolve_filters(request, None, session_obj={}, source=request.args, sticky_enabled=True, update_session=False)

    assert calls["count"] == 1
    assert first == second


def test_options_log_method_quiets_fast_cached_success(monkeypatch):
    monkeypatch.delenv("FILTER_OPTIONS_LOG_ALL_SUCCESS", raising=False)
    monkeypatch.delenv("DEBUG_FILTERS", raising=False)
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.setenv("FILTER_OPTIONS_INFO_MS", "1000")

    assert filters_api._options_log_method({"duration_ms": 120, "cached": True, "meta": {"cached": True}}, None) == "debug"
    assert filters_api._options_log_method({"duration_ms": 1400, "cached": True, "meta": {"cached": True}}, None) == "info"
