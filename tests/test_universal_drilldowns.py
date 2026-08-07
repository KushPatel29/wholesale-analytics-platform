from __future__ import annotations

import base64
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from flask import render_template_string

from app.services import drilldown_service


def _encode_context(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(__import__("json").dumps(payload).encode("utf-8")).decode("utf-8")
    return raw.rstrip("=")


def _dummy_user(user_id: str = "u-test"):
    return SimpleNamespace(
        id=user_id,
        role="admin",
        is_authenticated=True,
        get_id=lambda: user_id,
    )


def test_universal_drilldown_go_redirects_to_entity_with_inherited_filters(app_client):
    payload = {
        "source_page": "customer_drilldown",
        "source_section": "Product & Category Intelligence",
        "source_widget": "Product Profitability Table",
        "source_entity_type": "customer",
        "source_entity_id": "C_MAIN",
        "source_entity_label": "Main Customer",
        "requested_target": "product",
        "clicked_entity_type": "product",
        "clicked_entity_id": "SKU-001",
        "clicked_entity_label": "Chicken Breast",
        "clicked_metric": "Revenue",
        "clicked_metric_value": 1425.0,
        "active_filter_state": {
            "start": "2025-03-01",
            "end": "2025-03-31",
            "regions": ["West"],
        },
        "extra": {
            "target_filters": {"customer_ids": ["C_MAIN"]},
        },
    }

    response = app_client.get("/drilldowns/go", query_string={"context": _encode_context(payload)}, follow_redirects=False)
    assert response.status_code == 302

    parsed = urlparse(response.headers["Location"])
    assert parsed.path == "/products/SKU-001/drilldown"
    query = parse_qs(parsed.query)
    assert query["start"] == ["2025-03-01"]
    assert query["end"] == ["2025-03-31"]
    assert query["regions"] == ["West"]
    assert query["customers"] == ["C_MAIN"]
    assert "drill_context" in query


def test_universal_drilldown_workspace_renders_narrative_context(app_client):
    with app_client.application.app_context():
        token, _context = drilldown_service.issue_context_token(
            {
                "source_page": "customer_drilldown",
                "source_section": "CRM Action Workspace",
                "source_widget": "Protect Now",
                "source_entity_type": "customer",
                "source_entity_id": "C_MAIN",
                "source_entity_label": "Main Customer",
                "requested_target": "workspace",
                "clicked_metric": "Recover margin on poultry",
                "clicked_metric_value": 78,
                "extra": {
                    "workspace_kind": "narrative",
                    "detail": "Margin fell below target after the last pricing reset.",
                    "confidence": 84.0,
                    "revenue_upside": 1200.0,
                    "related_products": ["Chicken Breast", "Turkey Sausage"],
                    "target_filters": {"customer_ids": ["C_MAIN"]},
                },
            },
            user_obj=_dummy_user(),
        )

    response = app_client.get("/drilldowns/workspace", query_string={"token": token})
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Universal Drilldown Workspace" in body
    assert "Recover margin on poultry" in body
    assert "Margin fell below target after the last pricing reset." in body
    assert "Related context" in body


def test_active_drill_context_banner_renders_on_target_page(app_client, monkeypatch):
    app = app_client.application
    if "test_drill_banner" not in app.view_functions:
        app.add_url_rule(
            "/__test_drill_banner",
            "test_drill_banner",
            lambda: render_template_string('{% extends "base.html" %}{% block content %}<div>Banner target</div>{% endblock %}'),
        )

    user = _dummy_user("banner-user")
    monkeypatch.setattr("flask_login.utils._get_user", lambda: user)

    with app.app_context():
        token, _context = drilldown_service.issue_context_token(
            {
                "source_page": "overview",
                "source_section": "Executive Scorecard",
                "source_widget": "Revenue",
                "requested_target": "workspace",
                "clicked_metric": "Revenue",
                "clicked_metric_value": 52500.0,
                "active_filter_state": {"start": "2025-03-01", "end": "2025-03-31", "regions": ["West"]},
            },
            user_obj=user,
        )

    response = app_client.get("/__test_drill_banner", query_string={"drill_context": token})
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Drilled context active" in body
    assert "Overview &gt; Executive Scorecard &gt; Revenue" in body or "Overview > Executive Scorecard > Revenue" in body
    assert "Window 2025-03-01 to 2025-03-31" in body


def test_issue_context_token_normalizes_regions_v2_source_page(app_client):
    with app_client.application.app_context():
        token, context = drilldown_service.issue_context_token(
            {
                "source_page": "regions_v2",
                "source_section": "Ranking & Performance",
                "source_widget": "Revenue by Region",
                "requested_target": "region",
                "clicked_entity_type": "region",
                "clicked_entity_id": "West",
                "clicked_entity_label": "West",
                "clicked_metric": "Revenue",
                "clicked_metric_value": 125000,
                "active_filter_state": {"start": "2025-03-01", "end": "2025-03-31"},
            },
            user_obj=_dummy_user("regions-user"),
        )

    assert token
    assert context["source_page"] == "regions"
    parsed = urlparse(context["back_href"])
    assert parsed.path == "/regions/"
    query = parse_qs(parsed.query)
    assert query["start"] == ["2025-03-01"]
    assert query["end"] == ["2025-03-31"]
    assert "_gf" in query


def test_universal_drilldown_go_accepts_products_source_page(app_client):
    payload = {
        "source_page": "products",
        "source_section": "Portfolio Ranking",
        "source_widget": "Top Products",
        "requested_target": "product",
        "clicked_entity_type": "product",
        "clicked_entity_id": "SKU-123",
        "clicked_entity_label": "Chicken Breast",
        "clicked_metric": "Revenue",
        "clicked_metric_value": 7400,
        "active_filter_state": {"start": "2025-03-01", "end": "2025-03-31", "regions": ["West"]},
    }

    response = app_client.get("/drilldowns/go", query_string={"context": _encode_context(payload)}, follow_redirects=False)
    assert response.status_code == 302

    parsed = urlparse(response.headers["Location"])
    assert parsed.path == "/products/SKU-123/drilldown"
    query = parse_qs(parsed.query)
    assert query["start"] == ["2025-03-01"]
    assert query["end"] == ["2025-03-31"]
    assert query["regions"] == ["West"]
    assert "drill_context" in query


def test_universal_drilldown_salesrep_target_preserves_salesrep_local_query_state(app_client):
    payload = {
        "source_page": "salesreps",
        "source_section": "Trend Intelligence",
        "source_widget": "Revenue Trend by Rep",
        "requested_target": "salesrep",
        "clicked_entity_type": "salesrep",
        "clicked_entity_id": "R2",
        "clicked_entity_label": "Bea",
        "clicked_metric": "Revenue",
        "clicked_metric_value": 8400,
        "active_filter_state": {"start": "2025-03-01", "end": "2025-03-31", "regions": ["West"]},
        "target_query": {
            "attribution_mode": "historical_rep",
            "roster_mode": "include_former",
            "transfer_only": True,
            "trend_metric": "profit",
            "trend_grain": "quarterly",
            "trend_view": "yoy_delta",
            "top_n": 15,
        },
    }

    response = app_client.get("/drilldowns/go", query_string={"context": _encode_context(payload)}, follow_redirects=False)
    assert response.status_code == 302

    parsed = urlparse(response.headers["Location"])
    assert parsed.path == "/salesreps/R2"
    query = parse_qs(parsed.query)
    assert query["start"] == ["2025-03-01"]
    assert query["end"] == ["2025-03-31"]
    assert query["regions"] == ["West"]
    assert query["attribution_mode"] == ["historical_rep"]
    assert query["roster_mode"] == ["include_former"]
    assert query["transfer_only"] == ["1"]
    assert query["trend_metric"] == ["profit"]
    assert query["trend_grain"] == ["quarterly"]
    assert query["trend_view"] == ["yoy_delta"]
    assert query["top_n"] == ["15"]
    assert "drill_context" in query
