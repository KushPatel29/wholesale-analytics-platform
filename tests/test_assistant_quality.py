from __future__ import annotations

import uuid

import pytest

from app import create_app
from app.auth.models import SessionLocal, User


def _make_user(*, role: str = "sales", password: str = "test") -> tuple[User, str]:
    username = f"assistant_{uuid.uuid4().hex[:10]}"
    email = f"{username}@example.com"
    erp = f"REP-{uuid.uuid4().hex[:6]}"
    with SessionLocal() as s:
        user = User(
            username=username,
            email=email,
            role=role,
            erp_user_id=erp,
            sales_rep_id=erp,
            is_active=True,
            is_approved=True,
            must_reset_password=False,
        )
        user.set_password(password)
        s.add(user)
        s.commit()
        s.refresh(user)
        s.expunge(user)
        return user, password


@pytest.fixture()
def assistant_app(monkeypatch):
    monkeypatch.setenv("AI_ENABLED", "1")
    monkeypatch.setenv("WA_FAST_PWHASH", "1")
    app = create_app()
    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SECRET_KEY="test",
        LOGIN_DISABLED=False,
        AUTHZ_DISABLED=False,
        AUTHZ_ENFORCEMENT=True,
        AUTHZ_ENFORCEMENT_MODE="enforce",
        ADMIN_PERMISSIONS_V2=True,
        AI_ENABLED=True,
        AI_ENABLE_AUDIT=False,
    )
    return app


@pytest.fixture()
def assistant_client(assistant_app):
    with assistant_app.test_client() as client:
        yield client


def _login(client, *, role: str = "sales") -> None:
    user, password = _make_user(role=role)
    resp = client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=False)
    assert resp.status_code in {302, 303}


def _base_tool_result(title: str, *, data=None, status: str = "ok", module: str = "overview") -> dict:
    return {
        "status": status,
        "title": title,
        "module": module,
        "scope_used": {},
        "window_used": {},
        "notes": [],
        "next_actions": [],
        "citations": [],
        "data": data or {},
    }


def test_ranking_and_definition_answers_expose_compatibility_shape(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)

    def _fake_execute_tool(name, ctx, args=None):
        if name == "get_top_regions":
            return _base_tool_result(
                "Top Regions",
                module="overview",
                data={
                    "dimension": "regions",
                    "metric": "revenue",
                    "direction": "top",
                    "rows": [
                        {"label": "West", "revenue": 120000.0, "margin_pct": 18.4},
                        {"label": "South", "revenue": 98000.0, "margin_pct": 15.2},
                    ],
                },
            )
        if name == "get_metric_definition":
            return _base_tool_result(
                "Metric Definition",
                module="overview",
                data=[
                    {
                        "title": "AOV",
                        "definition": "Average order value in the active window.",
                    }
                ],
            )
        if name == "get_confidence_or_trust_summary":
            return _base_tool_result("Confidence And Trust Summary", module="overview", data={"confidence_score": 0.91})
        if name == "get_next_best_questions":
            return _base_tool_result("Next Best Questions", module="overview", data={"questions": ["Export this ranking."]})
        return _base_tool_result(name.replace("_", " ").title(), module=str(getattr(ctx, "page", "overview") or "overview"))

    monkeypatch.setattr(assistant_service, "execute_tool", _fake_execute_tool, raising=True)

    _login(assistant_client)

    rank_resp = assistant_client.post(
        "/ai/chat",
        json={"message": "top 5 regions by revenue", "context": {"page": "overview", "ref_path": "/overview"}},
    )
    def_resp = assistant_client.post(
        "/ai/chat",
        json={"message": "what is AOV?", "context": {"page": "overview", "ref_path": "/overview"}},
    )

    rank_payload = rank_resp.get_json()
    def_payload = def_resp.get_json()

    assert rank_payload["answer"]["answer_type"] == "ranking"
    assert def_payload["answer"]["answer_type"] == "definition"
    assert rank_payload["answer"]["sections"][0]["title"] == "Ranked Results"
    assert def_payload["answer"]["sections"][0]["title"] == "Definition"
    assert any(item["tool"] == "get_top_regions" for item in rank_payload["tool_trace"])
    assert any(item["tool"] == "get_metric_definition" for item in def_payload["tool_trace"])


def test_export_answer_exposes_download_action_alias():
    from app.assistant import service as assistant_service

    followup = assistant_service.FollowupResolution(resolved_message="export this", is_followup=False)
    slots = assistant_service.SemanticSlots(intent_type="ranking", export_requested=True, export_intent_type="export_ranked_list")
    results = [
        {
            "status": "ok",
            "title": "Assistant File Export",
            "module": "overview",
            "scope_used": {},
            "window_used": {},
            "notes": [],
            "next_actions": [],
            "citations": [],
            "data": {
                "status": "completed",
                "format": "xlsx",
                "export_id": "ax_test_completed",
                "filename": "test_export.xlsx",
                "download_url": "/ai/exports/ax_test_completed/download",
                "sheets": ["Summary"],
            },
        }
    ]

    synthesized = assistant_service._synthesize_answer(
        "export this",
        results,
        module="overview",
        question_type="export_request",
        slots=slots,
        permission_limited=False,
        scope={},
        trust_flags={"cost": False, "profit": False, "margin": False},
        followup=followup,
    )

    assert synthesized["answer_type"] == "export"
    assert synthesized["actions"][0]["kind"] == "download"
    assert synthesized["actions"][0]["url"] == "/ai/exports/ax_test_completed/download"


def test_page_help_and_returns_workflow_use_compatibility_titles(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)

    def _fake_execute_tool(name, ctx, args=None):
        module = str(getattr(ctx, "page", "overview") or "overview")
        if name == "get_page_help":
            return _base_tool_result(
                "Page Help",
                module=module,
                data={
                    "matches": [
                        {
                            "title": "Products Page",
                            "body": "Use Products to review SKU performance, pricing, dependency, velocity, and margin risk.",
                        }
                    ]
                },
            )
        if name == "get_returns_workflow_help":
            return _base_tool_result(
                "Returns Workflow Help",
                module="returns",
                data=[
                    {
                        "title": "Returns Workflow",
                        "definition": "Returns move from request to review to approval with role-based controls.",
                    }
                ],
            )
        if name == "get_pending_returns":
            return _base_tool_result("Pending Returns And Approvals", module="returns", data={"pending_count": 4})
        if name == "get_returns_status_overview":
            return _base_tool_result("Returns Status Overview", module="returns", data={"open": 12})
        return _base_tool_result(name.replace("_", " ").title(), module=module)

    monkeypatch.setattr(assistant_service, "execute_tool", _fake_execute_tool, raising=True)

    _login(assistant_client, role="warehouse")

    help_resp = assistant_client.post(
        "/ai/chat",
        json={"message": "how should i use this page?", "context": {"page": "products", "ref_path": "/products/SKU-1"}},
    )
    returns_resp = assistant_client.post(
        "/ai/chat",
        json={"message": "explain the returns workflow", "context": {"page": "returns", "ref_path": "/returns"}},
    )

    help_payload = help_resp.get_json()
    returns_payload = returns_resp.get_json()

    assert help_payload["answer"]["answer_type"] == "help"
    assert help_payload["answer"]["sections"][0]["title"] == "How To Use This Page"
    assert help_payload["answer"]["sections"][1]["title"] == "Good Next Questions"
    assert returns_payload["answer"]["answer_type"] == "returns"
    assert returns_payload["answer"]["sections"][0]["title"] == "Returns Summary"
    assert returns_payload["answer"]["sections"][1]["title"] == "Workflow Help"
