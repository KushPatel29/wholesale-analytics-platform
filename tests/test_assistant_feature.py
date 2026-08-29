from __future__ import annotations

import time
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


def _assistant_bundle_factory(config):
    def _rows(token, filters):
        for filter_key in ("sales_reps", "customers", "suppliers", "regions", "products"):
            values = filters.get(filter_key) or ()
            if values:
                scoped = config.get((token, filter_key, values[0]))
                if scoped is not None:
                    return [dict(row) for row in scoped]
        return [dict(row) for row in config.get(token, [])]

    def _fake_module_bundle(ctx, module, args=None):
        token = str(module)
        filters = {
            "customers": tuple(getattr(ctx.filters, "customers", ()) or ()),
            "suppliers": tuple(getattr(ctx.filters, "suppliers", ()) or ()),
            "sales_reps": tuple(getattr(ctx.filters, "sales_reps", ()) or ()),
            "regions": tuple(getattr(ctx.filters, "regions", ()) or ()),
            "products": tuple(getattr(ctx.filters, "products", ()) or ()),
        }
        rows = _rows(token, filters)
        revenue = sum(float(row.get("revenue") or 0.0) for row in rows)
        profit = sum(float(row.get("profit") or 0.0) for row in rows)
        margin = (profit / revenue * 100.0) if revenue else None
        return {
            "table": {"rows": rows},
            "meta": {"entity_label": next((values[0] for values in filters.values() if values), token)},
            "kpis": {"revenue": revenue, "profit": profit, "margin_pct": margin, "orders": len(rows)},
            "trend": {"labels": [], "revenue": []},
            "charts": {"trend": {"labels": [], "revenue": []}},
        }

    return _fake_module_bundle


def _assistant_followup_execute_tool(name, ctx, args=None):
    module = str(((args or {}).get("module")) or ctx.page or "overview").strip().lower()
    base = {
        "status": "ok",
        "module": module,
        "scope_used": {},
        "window_used": {},
        "notes": [],
        "next_actions": [],
        "citations": [],
        "data": {},
    }
    if name == "get_page_bundle":
        return {**base, "title": "Page Bundle", "data": {"module": module, "visible_sections": ["scorecard", "trend"]}}
    if name == "get_proactive_insights":
        cards_by_module = {
            "overview": [
                {
                    "title": "West revenue softness",
                    "narrative": "Revenue softness is concentrated in the West right now.",
                }
            ],
            "suppliers": [
                {
                    "title": "Prairie Meats margin pressure",
                    "narrative": "Supplier risk is concentrated in Prairie Meats margin pressure.",
                }
            ],
            "salesreps": [
                {
                    "title": "Fraser portfolio softness",
                    "narrative": "Sales rep risk is concentrated in Fraser's portfolio.",
                }
            ],
            "returns": [
                {
                    "title": "Pending returns queue elevated",
                    "narrative": "Returns risk is concentrated in the pending approval queue.",
                }
            ],
        }
        return {**base, "title": "Proactive Insights", "data": {"cards": cards_by_module.get(module, cards_by_module["overview"])}}
    if name == "get_priority_risks":
        risk_titles = {
            "overview": "West revenue softness",
            "suppliers": "Prairie Meats margin pressure",
            "salesreps": "Fraser portfolio softness",
            "returns": "Pending returns queue elevated",
        }
        return {
            **base,
            "title": "Priority Risks",
            "data": {"risks": [{"title": risk_titles.get(module, "Priority risk"), "detail": "This is the clearest risk in the current scope."}]},
        }
    if name == "get_risk_trend_baseline":
        return {**base, "title": "Risk Trend Baseline", "status": "empty", "data": {"message": "Not enough points for baseline."}}
    if name == "get_confidence_or_trust_summary":
        return {**base, "title": "Confidence And Trust Summary", "data": {"freshness": "current", "coverage": "good"}}
    if name == "get_guided_investigation_paths":
        return {**base, "title": "Guided Investigation Paths", "data": {"paths": [{"title": f"Open {module} drilldown"}]}}
    if name == "get_next_best_questions":
        return {**base, "title": "Next Best Questions", "data": {"questions": ["What should I do next?"]}}
    if name == "compare_entities":
        return {
            **base,
            "title": "Entity Comparison",
            "data": {
                "dimension": module,
                "metric": "revenue",
                "top": [{"label": "Maple Foods", "revenue": 12500.0}],
                "bottom": [{"label": "Beacon Markets", "revenue": -4200.0}],
            },
        }
    if name == "compare_periods_for_entity":
        return {
            **base,
            "title": "Entity Period Comparison",
            "data": {"comparison": {"revenue_change_pct": -12.0, "margin_change_pct": -2.1}},
        }
    if name == "get_entity_change_explanation":
        return {
            **base,
            "title": "Entity Change Explanation",
            "data": {
                "summary": "Revenue is down mainly because mix shifted into lower-margin SKUs.",
                "comparison": {"revenue_change_pct": -12.0, "margin_change_pct": -2.1},
                "entity_detail": {
                    "top_rows": [
                        {"product_name": "Short Rib", "revenue": 7200.0},
                        {"product_name": "Brisket", "revenue": 6500.0},
                    ]
                },
            },
        }
    return {**base, "title": name.replace("_", " ").title()}


def test_assistant_route_disabled_returns_404(app, client):
    app.config["AI_ENABLED"] = False
    resp = client.get("/assistant")
    assert resp.status_code == 404


def test_assistant_page_loads_when_enabled(assistant_client):
    user, password = _make_user(role="sales")
    login = assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    assert login.status_code == 200
    resp = assistant_client.get("/assistant")
    assert resp.status_code == 200
    assert b"Northgate Retail Analytics Enterprise Assistant" in resp.data


def test_assistant_page_hides_provider_scaffolding_and_limits_debug_toggle_to_admin(assistant_client):
    admin_user, admin_password = _make_user(role="admin")
    assistant_client.post("/auth/login", data={"username": admin_user.username, "password": admin_password}, follow_redirects=True)
    admin_resp = assistant_client.get("/assistant")
    assert admin_resp.status_code == 200
    assert b"assistantDebugToggle" in admin_resp.data
    assert b"assistantProviderChip" not in admin_resp.data

    assistant_client.get("/auth/logout", follow_redirects=True)

    sales_user, sales_password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": sales_user.username, "password": sales_password}, follow_redirects=True)
    sales_resp = assistant_client.get("/assistant")
    assert sales_resp.status_code == 200
    assert b"assistantDebugToggle" not in sales_resp.data
    assert b"assistantProviderChip" not in sales_resp.data


def test_assistant_chat_masks_sensitive_kpis_for_user_without_cost_access(assistant_client, monkeypatch):
    from app.assistant import tools as assistant_tools

    def _fake_overview_context(_ctx):
        return {
            "scorecard_kpis": {
                "revenue": 1000.0,
                "profit": 220.0,
                "margin_pct": 22.0,
                "orders": 11,
                "profit_per_order": 20.0,
            },
            "bundle": {
                "meta": {"window": {"start": "2025-01-01", "end": "2025-03-31", "rows": 100}},
                "executive_briefing": {
                    "biggest_win": {"title": "Revenue", "value": 20},
                    "biggest_decline": {"title": "None", "value": 0},
                    "key_risk": {"title": "Risk", "value": 1},
                    "top_action": {"title": "Do action"},
                    "watchouts": [],
                    "recommended_actions": [],
                },
            },
            "trend_series": {"monthly": {"labels": ["2025-01"], "revenue": [1000]}},
            "drivers": {"mom": {"price": 10, "volume": 20, "mix": -5}},
            "movers": {"customer": {"gainers": [], "decliners": []}},
            "risk": {"concentration": {}, "profitability": {"margin_risk": []}},
            "data_health": {"cost_coverage_pct": 100},
            "forecast": {"enabled": False},
        }

    monkeypatch.setattr(assistant_tools, "_overview_context", _fake_overview_context, raising=True)

    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=False)
    resp = assistant_client.post("/api/assistant/chat", json={"message": "Show me KPI summary with profit and margin"})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] in {"ok", "empty"}
    evidence = payload["answer"]["evidence"]
    kpi_tool = next((item for item in evidence if item.get("title") == "Overview KPI Command Center"), None)
    assert kpi_tool is not None
    assert kpi_tool["data"].get("profit") is None
    assert kpi_tool["data"].get("margin_pct") is None
    assert kpi_tool["data"].get("profit_per_order") is None


def test_assistant_chat_permission_limited_for_non_overview_role(assistant_client):
    user, password = _make_user(role="warehouse")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    resp = assistant_client.post("/api/assistant/chat", json={"message": "How is business performance and margin today?"})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["answer"]["permission_limited"] is True
    forbidden_titles = {item.get("title") for item in payload["answer"]["evidence"] if item.get("status") == "forbidden"}
    assert "Overview KPIs" in forbidden_titles or "Overview Summary" in forbidden_titles


def test_assistant_v2_health_context_and_suggestions_endpoints(assistant_client):
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    health = assistant_client.get("/ai/health")
    assert health.status_code == 200
    health_payload = health.get_json()
    assert health_payload["status"] in {"ok", "degraded", "disabled"}
    assert "provider_health" in health_payload

    ctx = assistant_client.get("/ai/context")
    assert ctx.status_code == 200
    ctx_payload = ctx.get_json()
    assert ctx_payload["status"] == "ok"
    assert "context" in ctx_payload
    assert "module_access" in ctx_payload["context"]

    suggestions = assistant_client.get("/ai/suggestions")
    assert suggestions.status_code == 200
    suggestions_payload = suggestions.get_json()
    assert suggestions_payload["status"] == "ok"
    assert isinstance(suggestions_payload["suggestions"], list)


def test_assistant_suggestions_respect_feature_flag(assistant_client):
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    with assistant_client.application.app_context():
        assistant_client.application.config["AI_ENABLE_SUGGESTED_PROMPTS"] = False
    resp = assistant_client.get("/ai/suggestions")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "ok"
    assert payload["suggestions"] == []


def test_assistant_thread_endpoint_returns_history_after_chat(assistant_client):
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    chat = assistant_client.post("/ai/chat", json={"message": "Summarize this page for leadership"})
    assert chat.status_code == 200
    chat_payload = chat.get_json()
    thread_id = chat_payload.get("thread_id")
    assert thread_id

    thread_resp = assistant_client.get(f"/ai/thread/{thread_id}")
    assert thread_resp.status_code == 200
    thread_payload = thread_resp.get_json()
    assert thread_payload["status"] == "ok"
    assert thread_payload["thread"]["thread_id"] == thread_id
    assert isinstance(thread_payload["thread"]["messages"], list)
    assert len(thread_payload["thread"]["messages"]) >= 2


def test_metric_answers_require_tool_backing_when_enabled(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service
    from app.assistant import tools as assistant_tools

    def _fake_overview_forbidden(_ctx):
        return {"scorecard_kpis": {}, "bundle": {}, "trend_series": {}, "drivers": {}, "movers": {}, "risk": {}, "data_health": {}, "forecast": {}}

    monkeypatch.setattr(assistant_tools, "_overview_context", _fake_overview_forbidden, raising=True)
    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: "Provider fabricated response", raising=True)

    user, password = _make_user(role="warehouse")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    with assistant_client.application.app_context():
        assistant_client.application.config["AI_REQUIRE_TOOL_BACKING_FOR_METRICS"] = True
    resp = assistant_client.post("/ai/chat", json={"message": "How is revenue and margin performing?"})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] in {"ok", "forbidden", "empty"}
    assert payload["answer"]["direct_answer"] != "Provider fabricated response"
    assert "Provider fabricated response" not in str(payload["answer"].get("explanation") or "")


def test_assistant_context_includes_page_state_and_entity(assistant_client):
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    resp = assistant_client.get("/ai/context?page=customers&ref=/customers/CUST-1001")
    assert resp.status_code == 200
    payload = resp.get_json()
    context = payload["context"]
    assert context["page"] == "customers"
    assert "page_state" in context
    assert context["page_state"]["module"] == "customers"
    assert isinstance(context["page_state"]["allowed_metrics"], list)
    assert isinstance(context["page_state"]["visible_sections"], list)


def test_assistant_suggestions_are_module_aware(assistant_client):
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    overview = assistant_client.get("/ai/suggestions?page=overview")
    products = assistant_client.get("/ai/suggestions?page=products")
    assert overview.status_code == 200
    assert products.status_code == 200
    overview_prompts = overview.get_json()["suggestions"]
    product_prompts = products.get_json()["suggestions"]
    assert any("revenue" in prompt.lower() or "risk" in prompt.lower() for prompt in overview_prompts)
    assert any("product" in prompt.lower() or "sku" in prompt.lower() for prompt in product_prompts)
    assert overview_prompts != product_prompts


def test_assistant_followup_why_rewrites_using_thread_state(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    captured: list[str] = []

    def _capture_choose_tools(message, **kwargs):
        captured.append(str(message))
        return [("get_current_page_context", {}), ("get_recommended_followups", {})]

    monkeypatch.setattr(assistant_service, "_choose_tools", _capture_choose_tools, raising=True)
    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)

    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    first = assistant_client.post(
        "/ai/chat",
        json={
            "message": "Summarize this customer",
            "context": {"page": "customers", "entity": {"type": "customer", "id": "CUST-9", "label": "Customer 9"}},
        },
    )
    assert first.status_code == 200
    thread_id = first.get_json().get("thread_id")
    assert thread_id

    second = assistant_client.post("/ai/chat", json={"thread_id": thread_id, "message": "Why?"})
    assert second.status_code == 200
    assert len(captured) >= 2
    assert captured[-1].lower().startswith("why did")


def test_assistant_thread_state_survives_inprocess_cache_clear(tmp_path):
    from flask import current_app

    from app.assistant import memory as assistant_memory

    store_path = tmp_path / "assistant_threads.sqlite3"
    user_id = "worker-hop-user"
    thread_id = assistant_memory.new_thread_id()

    with create_app().app_context():
        current_app.config["ASSISTANT_THREAD_STORE_PATH"] = store_path.as_posix()
        assistant_memory.clear_thread(user_id, thread_id)
        with assistant_memory._LOCK:
            assistant_memory._CACHE.clear()
        assistant_memory.append_turn(
            user_id,
            thread_id,
            user_message="What stands out most right now?",
            assistant_answer="Revenue softness is concentrated in the West.",
            state_update={"last_question_type": "proactive_insights", "last_focus": "regional softness"},
        )
        with assistant_memory._LOCK:
            assistant_memory._CACHE.clear()
        restored = assistant_memory.thread_state(user_id, thread_id)

    assert restored["last_question_type"] == "proactive_insights"
    assert restored["last_focus"] == "regional softness"


def test_assistant_answer_contains_rich_phase3_fields(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)

    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    resp = assistant_client.post("/ai/chat", json={"message": "Summarize this page for leadership"})
    assert resp.status_code == 200
    payload = resp.get_json()
    answer = payload["answer"]
    assert isinstance(answer.get("sections"), list)
    assert isinstance(answer.get("evidence_cards"), list)
    assert isinstance(answer.get("follow_up_suggestions"), list)
    assert isinstance(answer.get("action_suggestions"), list)
    assert isinstance(answer.get("scope_note"), str)
    assert isinstance(answer.get("trust_note"), str)
    assert answer.get("question_type") in {
        "executive_summary",
        "executive_digest",
        "live_analytics",
        "cross_module",
        "risk_action",
        "definition_help",
        "page_help",
        "returns_workflow",
        "returns_analytics",
        "proactive_insights",
        "anomaly_risk",
        "guided_investigation",
        "workflow_assist",
        "page_bundle",
        "history_analytics",
        "comparison_analytics",
        "export_request",
        "modify_request",
        "scheduled_digest",
    }


def test_assistant_returns_workflow_uses_lightweight_operational_tools():
    from app.assistant import service as assistant_service

    picks = assistant_service._choose_tools(
        "What approvals are pending and explain the returns workflow?",
        page="returns",
        module="returns",
        question_type="returns_workflow",
        slots=assistant_service.SemanticSlots(),
        module_access={"returns": True},
        max_calls=8,
        allow_glossary=True,
        followup=assistant_service.FollowupResolution(
            resolved_message="What approvals are pending and explain the returns workflow?",
            is_followup=False,
        ),
    )
    tool_names = [name for name, _args in picks]

    assert "get_returns_workflow_help" in tool_names
    assert "get_pending_returns" in tool_names
    assert "get_returns_status_overview" in tool_names
    assert "get_page_bundle" not in tool_names
    assert "get_returns_reason_patterns" not in tool_names


def test_assistant_definition_and_page_help_queries_use_knowledge_tools(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    metric_resp = assistant_client.post("/ai/chat", json={"message": "What is AOV?"})
    assert metric_resp.status_code == 200
    metric_payload = metric_resp.get_json()
    metric_titles = {item.get("title") for item in metric_payload["answer"]["evidence"]}
    assert "Metric Definition" in metric_titles

    page_help_resp = assistant_client.post("/ai/chat", json={"message": "How should I use this page?", "context": {"page": "products"}})
    assert page_help_resp.status_code == 200
    page_help_payload = page_help_resp.get_json()
    page_help_titles = {item.get("title") for item in page_help_payload["answer"]["evidence"]}
    assert "Page Help" in page_help_titles


@pytest.mark.parametrize("role", ["admin", "sales_manager", "sales", "warehouse"])
def test_assistant_role_permutation_chat_coverage(assistant_client, monkeypatch, role):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role=role)
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    resp = assistant_client.post(
        "/ai/chat",
        json={"message": "Summarize this page for leadership and include top risks and actions."},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] in {"ok", "forbidden", "empty"}
    assert isinstance(payload["answer"]["permission_limited"], bool)
    assert isinstance(payload["answer"].get("sections"), list)
    if role == "warehouse":
        assert payload["answer"]["permission_limited"] is True


def test_cross_module_priority_scan_does_not_fan_out_to_every_heavy_bundle(monkeypatch):
    from app.assistant import tools as assistant_tools
    from app.services.filters import FilterParams

    monkeypatch.setattr(
        assistant_tools,
        "_module_access",
        lambda _ctx: {
            "overview": True,
            "customers": True,
            "products": True,
            "regions": True,
            "suppliers": True,
            "salesreps": True,
            "returns": True,
            "admin": True,
            "notifications": True,
            "work": True,
        },
        raising=True,
    )
    monkeypatch.setattr(
        assistant_tools,
        "_overview_context",
        lambda _ctx: {"risk": {"concentration": {"hhi": 2400}, "profitability": {"margin_risk": []}}},
        raising=True,
    )
    monkeypatch.setattr(
        assistant_tools,
        "_module_bundle",
        lambda *_args, **_kwargs: pytest.fail("cross-module scan fanned out to a heavy module bundle"),
        raising=True,
    )
    monkeypatch.setattr(assistant_tools, "_mask", lambda data, _user: data, raising=True)
    monkeypatch.setattr(assistant_tools.returns_service, "list_rmas", lambda **_kwargs: [], raising=True)

    result = assistant_tools.get_priority_investigations(
        assistant_tools.ToolContext(user=None, page="overview", filters=FilterParams(), scope={}),
        {"module": "all"},
    )

    assert result["status"] == "ok"
    assert result["data"]["coverage_mode"] == "overview_fast"
    assert result["data"]["investigations"][0]["module"] == "overview"
    assert result["citations"] == ["overview_v2.build_overview_context", "returns_service.list_rmas"]


def test_assistant_viewer_revenue_ranking_is_safe_and_actionable(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service
    from app.assistant import tools as assistant_tools

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    monkeypatch.setattr(
        assistant_tools,
        "_module_bundle",
        _assistant_bundle_factory(
            {
                "products": [
                    {"product_id": "P-1", "product_name": "Viewer Ribeye", "revenue": 4200.0, "profit": 900.0, "margin_pct": 21.4, "orders": 12},
                    {"product_id": "P-2", "product_name": "Viewer Striploin", "revenue": 3100.0, "profit": 510.0, "margin_pct": 16.5, "orders": 9},
                ]
            }
        ),
        raising=True,
    )

    user, password = _make_user(role="viewer")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    resp = assistant_client.post("/ai/chat", json={"message": "Top 5 products by revenue", "context": {"page": "products"}})
    assert resp.status_code == 200
    payload = resp.get_json()
    answer = payload.get("answer") or {}
    labels = {str(row.get("label") or "") for row in (answer.get("ranked_results") or []) if isinstance(row, dict)}
    followups = " ".join(str(item) for item in (answer.get("follow_up_suggestions") or [])).lower()

    assert payload["question_type"] == "ranking_analytics"
    assert answer.get("permission_limited") is False
    assert "Viewer Ribeye" in labels
    assert "profit" not in followups


def test_assistant_viewer_margin_ranking_is_blocked_without_sensitive_leakage(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service
    from app.assistant import tools as assistant_tools

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    monkeypatch.setattr(
        assistant_tools,
        "_module_bundle",
        _assistant_bundle_factory(
            {
                "products": [
                    {"product_id": "P-1", "product_name": "Restricted Ribeye", "revenue": 4200.0, "profit": 900.0, "margin_pct": 21.4, "orders": 12},
                    {"product_id": "P-2", "product_name": "Restricted Striploin", "revenue": 3100.0, "profit": 510.0, "margin_pct": 16.5, "orders": 9},
                ]
            }
        ),
        raising=True,
    )

    user, password = _make_user(role="viewer")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    resp = assistant_client.post("/ai/chat", json={"message": "Top 5 products by margin", "context": {"page": "products"}})
    assert resp.status_code == 200
    payload = resp.get_json()
    answer = payload.get("answer") or {}
    followups = " ".join(str(item) for item in (answer.get("follow_up_suggestions") or [])).lower()

    assert payload["question_type"] == "ranking_analytics"
    assert answer.get("permission_limited") is True
    assert (answer.get("ranked_results") or []) == []
    assert "margin" not in followups
    assert "profit" not in followups
    explanation = f"{answer.get('direct_answer') or ''} {answer.get('explanation') or ''}".lower()
    assert "permission" in explanation or "hidden" in explanation or "access" in explanation


def test_assistant_viewer_nested_child_detail_is_blocked_when_child_module_is_not_allowed(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service
    from app.assistant import tools as assistant_tools

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    monkeypatch.setattr(
        assistant_tools,
        "_module_bundle",
        _assistant_bundle_factory(
            {
                "products": [
                    {"product_id": "P-1", "product_name": "Viewer Ribeye", "revenue": 4200.0, "profit": 900.0, "margin_pct": 21.4},
                    {"product_id": "P-2", "product_name": "Viewer Striploin", "revenue": 3100.0, "profit": 510.0, "margin_pct": 16.5},
                ]
            }
        ),
        raising=True,
    )

    user, password = _make_user(role="viewer")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    resp = assistant_client.post(
        "/ai/chat",
        json={"message": "top 3 products and their top 3 sales reps", "context": {"page": "products"}},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    answer = payload.get("answer") or {}

    assert payload["question_type"] == "ranking_analytics"
    assert answer.get("permission_limited") is True
    assert ((answer.get("query_slots") or {}).get("query_shape")) == "nested_ranking"
    assert list(((answer.get("nested_results") or {}).get("groups") or [])) == []


def test_assistant_analyst_margin_ranking_allows_sensitive_metric(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service
    from app.assistant import tools as assistant_tools

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    monkeypatch.setattr(
        assistant_tools,
        "_module_bundle",
        _assistant_bundle_factory(
            {
                "products": [
                    {"product_id": "P-1", "product_name": "Analyst Ribeye", "revenue": 4200.0, "profit": 900.0, "margin_pct": 21.4, "orders": 12},
                    {"product_id": "P-2", "product_name": "Analyst Striploin", "revenue": 3100.0, "profit": 510.0, "margin_pct": 16.5, "orders": 9},
                ]
            }
        ),
        raising=True,
    )

    user, password = _make_user(role="analyst")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    resp = assistant_client.post("/ai/chat", json={"message": "Top 5 products by margin", "context": {"page": "products"}})
    assert resp.status_code == 200
    payload = resp.get_json()
    answer = payload.get("answer") or {}
    ranked = [row for row in (answer.get("ranked_results") or []) if isinstance(row, dict)]

    assert payload["question_type"] == "ranking_analytics"
    assert answer.get("permission_limited") is False
    assert ranked
    assert ranked[0].get("label") == "Analyst Ribeye"
    assert ranked[0].get("metric_value") is not None


def test_assistant_sales_manager_salesrep_customer_hierarchy_is_allowed(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service
    from app.assistant import tools as assistant_tools

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    monkeypatch.setattr(
        assistant_tools,
        "_module_bundle",
        _assistant_bundle_factory(
            {
                "salesreps": [
                    {"rep_id": "REP-A", "rep_name": "Alex West", "revenue": 8100.0, "profit": 1400.0, "margin_pct": 17.3},
                    {"rep_id": "REP-B", "rep_name": "Brooke East", "revenue": 6200.0, "profit": 980.0, "margin_pct": 15.8},
                ],
                ("customers", "sales_reps", "REP-A"): [
                    {"customer_id": "C-A1", "customer_name": "Acme Foods", "revenue": 3300.0, "profit": 550.0, "margin_pct": 16.7},
                    {"customer_id": "C-A2", "customer_name": "Bistro Prime", "revenue": 2500.0, "profit": 420.0, "margin_pct": 16.8},
                ],
                ("customers", "sales_reps", "REP-B"): [
                    {"customer_id": "C-B1", "customer_name": "Central Market", "revenue": 2900.0, "profit": 460.0, "margin_pct": 15.9},
                    {"customer_id": "C-B2", "customer_name": "Delta Grill", "revenue": 2100.0, "profit": 310.0, "margin_pct": 14.8},
                ],
            }
        ),
        raising=True,
    )

    user, password = _make_user(role="sales_manager")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    resp = assistant_client.post(
        "/ai/chat",
        json={"message": "top 3 sales reps and their top 2 customers", "context": {"page": "salesreps"}},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    answer = payload.get("answer") or {}
    groups = (answer.get("nested_results") or {}).get("groups") or []

    assert payload["question_type"] == "ranking_analytics"
    assert answer.get("permission_limited") is False
    assert groups
    assert groups[0].get("parent_label") == "Alex West"
    assert any(str(child.get("label") or "") == "Acme Foods" for child in (groups[0].get("children") or []))


def test_assistant_gm_admin_page_hierarchy_prefers_business_module_over_admin_label(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service
    from app.assistant import tools as assistant_tools

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    monkeypatch.setattr(
        assistant_tools,
        "_module_bundle",
        _assistant_bundle_factory(
            {
                "suppliers": [
                    {"supplier_id": "SUP-A", "supplier_name": "North Ranch", "revenue": 8200.0, "profit": 1500.0, "margin_pct": 18.3},
                    {"supplier_id": "SUP-B", "supplier_name": "Prairie Meats", "revenue": 6100.0, "profit": 1040.0, "margin_pct": 17.0},
                ],
                ("products", "suppliers", "SUP-A"): [
                    {"product_id": "SA-1", "product_name": "North Chuck", "revenue": 3400.0, "profit": 600.0, "margin_pct": 17.6},
                    {"product_id": "SA-2", "product_name": "North Brisket", "revenue": 2800.0, "profit": 520.0, "margin_pct": 18.5},
                ],
                ("products", "suppliers", "SUP-B"): [
                    {"product_id": "SB-1", "product_name": "Prairie Flat Iron", "revenue": 2500.0, "profit": 420.0, "margin_pct": 16.8},
                    {"product_id": "SB-2", "product_name": "Prairie Sirloin", "revenue": 2100.0, "profit": 360.0, "margin_pct": 17.1},
                ],
            }
        ),
        raising=True,
    )

    user, password = _make_user(role="gm")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    resp = assistant_client.post(
        "/ai/chat",
        json={"message": "top 5 suppliers and their top 2 products", "context": {"page": "admin"}},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    answer = payload.get("answer") or {}

    assert payload["module"] in {"suppliers", "products"}
    assert answer.get("permission_limited") is False
    assert "admin" not in str(answer.get("direct_answer") or "").lower()
    assert any(group.get("parent_label") == "North Ranch" for group in ((answer.get("nested_results") or {}).get("groups") or []))


def test_assistant_analyst_export_all_columns_includes_sensitive_fields(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="analyst")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post(
        "/ai/chat",
        json={"message": "Create workbook for this page with all available columns.", "context": {"page": "products"}},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    export_columns = ((payload.get("answer") or {}).get("export_columns") or {})
    allowed = {str(col).strip().lower() for col in list(export_columns.get("all_allowed_columns") or []) if str(col).strip()}
    excluded = {str(col).strip().lower() for col in list(export_columns.get("all_excluded_columns") or []) if str(col).strip()}

    assert payload["question_type"] == "export_request"
    assert bool(export_columns.get("export_sensitive")) is True
    assert "revenue" in allowed
    assert not ({"profit", "margin_pct"} & excluded)


def test_assistant_phase4_proactive_endpoint_returns_structured_feed(assistant_client):
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    resp = assistant_client.get("/ai/proactive?page=overview&ref=/overview")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] in {"ok", "empty", "error"}
    assert isinstance(payload.get("cards"), list)
    assert isinstance(payload.get("priority_risks"), list)
    assert isinstance(payload.get("guided_paths"), list)
    assert isinstance(payload.get("next_best_questions"), list)
    assert isinstance(payload.get("tool_trace"), list)


def test_assistant_phase4_proactive_question_and_fields(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    resp = assistant_client.post("/ai/chat", json={"message": "What stands out most right now?"})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] == "proactive_insights"
    answer = payload["answer"]
    assert isinstance(answer.get("proactive_cards"), list)
    assert isinstance(answer.get("risk_narratives"), list)
    assert isinstance(answer.get("guided_investigations"), list)
    assert isinstance(answer.get("spoken_summary"), str)


def test_assistant_phase4_digest_and_workflow_assist_modes(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)

    def _fake_execute_tool(name, ctx, args=None):
        base = {
            "status": "ok",
            "module": "overview",
            "scope_used": {},
            "window_used": {},
            "notes": [],
            "next_actions": [],
            "citations": [],
            "data": {},
        }
        if name == "get_current_page_context":
            return {**base, "title": "Current Page Context", "data": {"page": "overview"}}
        if name == "get_user_scope":
            return {**base, "title": "User Scope And Permissions", "data": {"scope_mode": "all"}}
        if name == "get_executive_digest":
            return {
                **base,
                "title": "Executive Digest",
                "data": {
                    "executive_summary": "Revenue softened, but the biggest risk is still concentrated in a small set of accounts.",
                    "spoken_summary": "Revenue softened and account concentration is the main watchout.",
                    "audience": "leadership",
                    "length": "short",
                },
            }
        if name == "get_manager_digest":
            return {
                **base,
                "title": "Manager Digest",
                "data": {"executive_summary": "Manager version", "audience": "manager", "length": "short"},
            }
        if name == "get_leadership_summary":
            return {
                **base,
                "title": "Leadership Summary",
                "data": {"executive_summary": "Leadership summary", "spoken_summary": "Leadership summary"},
            }
        if name == "get_page_bundle":
            return {**base, "title": "Page Bundle", "data": {"module": "overview", "visible_sections": ["scorecard", "trend"]}}
        if name == "get_investigation_checklist":
            return {
                **base,
                "title": "Investigation Checklist",
                "data": {"checklist": [{"task": "Inspect customer movers"}, {"task": "Review margin-risk SKUs"}]},
            }
        if name == "get_workflow_assist_note":
            return {
                **base,
                "title": "Workflow Assist Draft",
                "data": {
                    "module": "overview",
                    "note_type": "next_steps",
                    "review_required": True,
                    "non_destructive": True,
                    "body_lines": ["Inspect customer movers", "Review margin-risk SKUs"],
                },
            }
        if name == "get_guided_investigation_paths":
            return {**base, "title": "Guided Investigation Paths", "data": {"paths": [{"title": "Inspect customer movers"}]}}
        if name == "get_priority_actions":
            return {**base, "title": "Priority Actions", "data": {"actions": ["Inspect customer movers first."]}}
        if name == "get_next_best_questions":
            return {**base, "title": "Next Best Questions", "data": {"questions": ["What should I do next?"]}}
        if name == "get_recommended_followups":
            return {**base, "title": "Recommended Follow-up Questions", "data": {"suggestions": ["Explain that in simpler terms."]}}
        return {**base, "title": name.replace("_", " ").title()}

    monkeypatch.setattr(assistant_service, "execute_tool", _fake_execute_tool, raising=True)

    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    digest = assistant_client.post("/ai/chat", json={"message": "Create a short leadership brief from this page."})
    assert digest.status_code == 200
    digest_payload = digest.get_json()
    assert digest_payload["question_type"] in {"executive_digest", "executive_summary"}
    assert isinstance(digest_payload["answer"].get("digest"), dict)
    assert isinstance(digest_payload["answer"].get("spoken_summary"), str)

    checklist = assistant_client.post("/ai/chat", json={"message": "Prepare an investigation checklist."})
    assert checklist.status_code == 200
    checklist_payload = checklist.get_json()
    assert checklist_payload["question_type"] == "workflow_assist"
    answer = checklist_payload.get("answer") or {}
    workflow_assist = answer.get("workflow_assist") or {}
    assert isinstance(workflow_assist, dict)
    assert workflow_assist.get("review_required") is True
    assert workflow_assist.get("non_destructive") is True


def test_assistant_phase4_guided_paths_and_mode_controls(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    resp = assistant_client.post(
        "/ai/chat",
        json={
            "message": "Which module should I open now?",
            "mode": "executive",
            "detail_level": "short",
            "voice_ready": True,
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] in {"guided_investigation", "executive_digest", "page_help"}
    answer = payload["answer"]
    assert answer.get("response_mode") == "executive"
    assert answer.get("detail_level") == "short"
    assert answer.get("voice_ready") is True
    guided = answer.get("guided_investigations") or []
    assert isinstance(guided, list)
    for row in guided:
        if isinstance(row, dict) and row.get("open_path"):
            assert str(row["open_path"]).startswith("/")


def test_assistant_history_question_routes_to_history_tools(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    resp = assistant_client.post(
        "/ai/chat",
        json={
            "message": "Show full history for this customer over time.",
            "context": {"page": "customers", "entity": {"type": "customer", "id": "CUST-1001", "label": "Customer 1001"}},
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] == "history_analytics"
    answer = payload["answer"]
    assert isinstance(answer.get("page_bundle"), dict)
    assert isinstance(answer.get("spoken_summary"), str)
    titles = {item.get("title") for item in answer.get("evidence", [])}
    assert any("History" in str(title or "") for title in titles)


def test_assistant_history_empty_series_explains_limitation_cleanly(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service
    from app.assistant import tools as assistant_tools

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)

    def _fake_overview_context(_ctx):
        return {
            "scorecard_kpis": {"revenue": 125000.0, "orders": 32},
            "bundle": {
                "meta": {"window": {"start": "2025-01-01", "end": "2025-01-31", "rows": 32}},
                "executive_briefing": {"recommended_actions": []},
            },
            "trend_series": {"monthly": {"labels": [], "revenue": []}},
            "drivers": {"mom": {"price": 0.0, "volume": 0.0, "mix": 0.0}},
            "movers": {"customer": {"gainers": [], "decliners": []}},
            "risk": {"concentration": {}, "profitability": {"margin_risk": []}},
            "data_health": {"cost_coverage_pct": 97.0},
            "forecast": {"enabled": False},
        }

    monkeypatch.setattr(assistant_tools, "_overview_context", _fake_overview_context, raising=True)

    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    resp = assistant_client.post("/ai/chat", json={"message": "Show historical business trend for this window.", "context": {"page": "overview"}})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] == "history_analytics"
    answer = payload["answer"] or {}
    direct = str(answer.get("direct_answer") or "").lower()
    explanation = str(answer.get("explanation") or "").lower()
    assert "points=0" not in direct
    assert "latest=n/a" not in direct
    assert "usable historical series" in direct
    assert "snapshot" in explanation or "comparison" in explanation


def test_assistant_export_request_generates_downloadable_excel(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    chat = assistant_client.post("/ai/chat", json={"message": "Export this page to Excel workbook."})
    assert chat.status_code == 200
    payload = chat.get_json()
    assert payload["question_type"] == "export_request"
    actions = payload["answer"].get("export_actions") or []
    assert isinstance(actions, list)
    if actions:
        first = actions[0]
        download_url = first.get("download_url")
        assert download_url
        dl = assistant_client.get(download_url)
        assert dl.status_code == 200
        content_type = dl.headers.get("Content-Type") or ""
        assert "spreadsheet" in content_type or "application/octet-stream" in content_type


def test_assistant_modify_request_returns_reviewable_draft(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    resp = assistant_client.post("/ai/chat", json={"message": "Include trends in that export and make it leadership-friendly."})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] == "modify_request"
    preview = payload["answer"].get("modify_preview") or {}
    items = preview.get("items") if isinstance(preview, dict) else None
    assert isinstance(items, list)
    if items:
        text = " ".join(str(item.get("title") or "") for item in items if isinstance(item, dict)).lower()
        assert "export" in text or "refined" in text


def test_assistant_page_bundle_question_returns_visible_state(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    resp = assistant_client.post("/ai/chat", json={"message": "What is on this page right now? show page bundle."})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] == "page_bundle"
    answer = payload["answer"]
    assert isinstance(answer.get("page_bundle"), dict)


def test_assistant_phase5_anomaly_includes_baseline_and_causal(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)

    def _fake_execute_tool(name, ctx, args=None):
        base = {
            "status": "ok",
            "module": "overview",
            "scope_used": {},
            "window_used": {},
            "notes": [],
            "next_actions": [],
            "citations": [],
            "data": {},
        }
        if name == "get_current_page_context":
            return {**base, "title": "Current Page Context", "data": {"page": "overview"}}
        if name == "get_user_scope":
            return {**base, "title": "User Scope And Permissions", "data": {"scope_mode": "all"}}
        if name == "get_anomaly_narratives":
            return {
                **base,
                "title": "Anomaly And Risk Narratives",
                "data": {"narratives": [{"title": "Revenue anomaly", "narrative": "Revenue dropped sharply versus the recent baseline."}]},
            }
        if name == "get_entity_change_explanation":
            return {
                **base,
                "title": "Entity Change Explanation",
                "data": {"summary": "Volume decline was the main contributor."},
            }
        if name == "get_causal_attribution_graph":
            return {
                **base,
                "title": "Causal Attribution Graph",
                "data": {"nodes": [{"id": "volume"}], "edges": []},
            }
        if name == "get_risk_trend_baseline":
            return {**base, "title": "Risk Trend Baseline", "data": {"baseline": {"trend": "down"}}}
        if name == "get_priority_risks":
            return {**base, "title": "Priority Risks", "data": {"risks": [{"title": "Revenue anomaly", "detail": "Revenue dropped sharply."}]}}
        if name == "get_cross_module_risk_summary":
            return {**base, "title": "Cross-Module Risk Summary", "data": {"summary": "The issue is concentrated in revenue."}}
        if name == "get_guided_investigation_paths":
            return {**base, "title": "Guided Investigation Paths", "data": {"paths": [{"title": "Inspect revenue movers"}]}}
        if name == "get_recommended_followups":
            return {**base, "title": "Recommended Follow-up Questions", "data": {"suggestions": ["Explain that in simpler terms."]}}
        if name == "get_next_best_questions":
            return {**base, "title": "Next Best Questions", "data": {"questions": ["What should I do next?"]}}
        return {**base, "title": name.replace("_", " ").title()}

    monkeypatch.setattr(assistant_service, "execute_tool", _fake_execute_tool, raising=True)

    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    resp = assistant_client.post("/ai/chat", json={"message": "Give me anomaly narrative and cause chain for this page."})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] == "anomaly_risk"
    titles = {item.get("title") for item in payload["answer"].get("evidence", [])}
    assert "Risk Trend Baseline" in titles
    assert "Causal Attribution Graph" in titles


def test_assistant_intent_routing_and_answers_are_materially_different(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    prompts = [
        ("What stands out most right now?", "proactive_insights"),
        ("What drove the revenue change this month?", "driver_mover"),
        ("Can I trust these numbers?", "trust_quality"),
        ("Export this page to Excel workbook.", "export_request"),
    ]
    seen_types: set[str] = set()
    seen_directs: set[str] = set()
    for message, expected_type in prompts:
        resp = assistant_client.post("/ai/chat", json={"message": message, "context": {"page": "overview"}})
        assert resp.status_code == 200
        payload = resp.get_json()
        assert payload["question_type"] == expected_type
        answer = payload["answer"] or {}
        direct = str(answer.get("direct_answer") or "").strip().lower()
        assert direct
        seen_types.add(payload["question_type"])
        seen_directs.add(direct)
    assert len(seen_types) == len(prompts)
    assert len(seen_directs) == len(prompts)


def test_assistant_module_specific_summaries_are_not_generic(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    customer = assistant_client.post(
        "/ai/chat",
        json={"message": "Summarize this customer", "context": {"page": "customers", "entity": {"type": "customer", "id": "C-1", "label": "Customer 1"}}},
    )
    product = assistant_client.post(
        "/ai/chat",
        json={"message": "Summarize this product", "context": {"page": "products", "entity": {"type": "product", "id": "P-1", "label": "Product 1"}}},
    )
    assert customer.status_code == 200
    assert product.status_code == 200

    c_payload = customer.get_json()
    p_payload = product.get_json()
    c_answer = str((c_payload.get("answer") or {}).get("direct_answer") or "").lower()
    p_answer = str((p_payload.get("answer") or {}).get("direct_answer") or "").lower()
    assert c_payload["module"] == "customers"
    assert p_payload["module"] == "products"
    assert c_answer != p_answer
    assert "customer" in c_answer or "customers" in c_answer
    assert "product" in p_answer or "products" in p_answer


def test_assistant_followup_export_that_uses_prior_context(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    first = assistant_client.post(
        "/ai/chat",
        json={
            "message": "Show full history for this customer over time.",
            "context": {"page": "customers", "entity": {"type": "customer", "id": "CUST-1001", "label": "Customer 1001"}},
        },
    )
    assert first.status_code == 200
    thread_id = first.get_json().get("thread_id")
    assert thread_id

    follow = assistant_client.post("/ai/chat", json={"thread_id": thread_id, "message": "Export that"})
    assert follow.status_code == 200
    payload = follow.get_json()
    assert payload["question_type"] == "export_request"
    resolved = str(payload.get("resolved_message") or "").lower()
    assert "export" in resolved and "excel" in resolved
    actions = (payload.get("answer") or {}).get("export_actions") or []
    assert isinstance(actions, list)


def test_assistant_executive_and_analyst_modes_render_different_sections(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    executive = assistant_client.post(
        "/ai/chat",
        json={"message": "How is this page performing?", "mode": "executive", "context": {"page": "overview"}},
    )
    analyst = assistant_client.post(
        "/ai/chat",
        json={"message": "How is this page performing?", "mode": "analyst", "context": {"page": "overview"}},
    )
    assert executive.status_code == 200
    assert analyst.status_code == 200
    executive_answer = (executive.get_json() or {}).get("answer") or {}
    _analyst_answer = (analyst.get_json() or {}).get("answer") or {}
    exec_sections = [str(item.get("title") or "") for item in ((executive.get_json().get("answer") or {}).get("sections") or []) if isinstance(item, dict)]
    analyst_sections = [str(item.get("title") or "") for item in ((analyst.get_json().get("answer") or {}).get("sections") or []) if isinstance(item, dict)]
    assert executive_answer.get("response_mode") == "executive"
    assert "Analyst Lens" not in exec_sections
    assert "Analyst Lens" in analyst_sections


def test_assistant_history_intent_overrides_risk_keyword(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post(
        "/ai/chat",
        json={
            "message": "Show historical risk for this region over time.",
            "context": {"page": "regions", "entity": {"type": "region", "id": "R-1", "label": "West"}},
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] == "history_analytics"
    sections = [str(item.get("title") or "") for item in ((payload.get("answer") or {}).get("sections") or []) if isinstance(item, dict)]
    assert "History Series" in sections


def test_assistant_this_month_query_does_not_inject_previous_context(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    first = assistant_client.post(
        "/ai/chat",
        json={"message": "Summarize this customer", "context": {"page": "customers", "entity": {"type": "customer", "id": "C-7", "label": "Customer 7"}}},
    )
    assert first.status_code == 200
    thread_id = first.get_json().get("thread_id")
    assert thread_id

    second = assistant_client.post("/ai/chat", json={"thread_id": thread_id, "message": "Why is revenue down this month?"})
    assert second.status_code == 200
    payload = second.get_json()
    resolved = str(payload.get("resolved_message") or "")
    assert "Context:" not in resolved


def test_assistant_live_analytics_prefers_tool_synthesis_over_provider(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    called = {"count": 0}

    def _fake_provider(*_args, **_kwargs):
        called["count"] += 1
        return "GENERIC PROVIDER FALLBACK"

    monkeypatch.setattr(assistant_service, "_provider_answer", _fake_provider, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post("/ai/chat", json={"message": "How are we doing right now?", "context": {"page": "overview"}})
    assert resp.status_code == 200
    payload = resp.get_json()
    answer = payload.get("answer") or {}
    assert called["count"] == 0
    assert "GENERIC PROVIDER FALLBACK" not in str(answer.get("direct_answer") or "")
    assert "GENERIC PROVIDER FALLBACK" not in str(answer.get("explanation") or "")


def test_assistant_cross_module_question_returns_priority_signal_sections(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post("/ai/chat", json={"message": "What should we investigate first across the business?", "context": {"page": "overview"}})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] == "cross_module"
    sections = [str(item.get("title") or "") for item in ((payload.get("answer") or {}).get("sections") or []) if isinstance(item, dict)]
    assert "Top Priority" in sections
    assert "What to Do Next" in sections


def test_assistant_ranking_question_top_regions_by_revenue(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post("/ai/chat", json={"message": "Top 5 regions by revenue", "context": {"page": "overview"}})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] == "ranking_analytics"
    answer = payload.get("answer") or {}
    slots = answer.get("query_slots") or {}
    assert str(slots.get("metric") or "") == "revenue"
    assert str(slots.get("group_by_dimension") or slots.get("primary_entity_type") or "") in {"regions", "region"}
    assert isinstance(answer.get("ranked_results"), list)


def test_assistant_ranking_metric_changes_by_question(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    revenue = assistant_client.post("/ai/chat", json={"message": "Top 5 regions by revenue", "context": {"page": "overview"}})
    margin = assistant_client.post("/ai/chat", json={"message": "Top 5 regions by margin", "context": {"page": "overview"}})
    assert revenue.status_code == 200
    assert margin.status_code == 200
    revenue_slots = ((revenue.get_json().get("answer") or {}).get("query_slots") or {})
    margin_slots = ((margin.get_json().get("answer") or {}).get("query_slots") or {})
    assert revenue_slots.get("metric") == "revenue"
    assert margin_slots.get("metric") == "margin_pct"
    assert revenue_slots.get("metric") != margin_slots.get("metric")


def test_assistant_grouped_question_revenue_by_region(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post("/ai/chat", json={"message": "Revenue by region", "context": {"page": "overview"}})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] == "grouped_analytics"
    answer = payload.get("answer") or {}
    assert isinstance(answer.get("grouped_results"), list)
    sections = [str(item.get("title") or "") for item in (answer.get("sections") or []) if isinstance(item, dict)]
    assert "Grouped View" in sections


def test_assistant_customer_page_top_products_uses_customer_context(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post(
        "/ai/chat",
        json={
            "message": "Top 10 products by revenue",
            "context": {"page": "customers", "entity": {"type": "customer", "id": "CUST-1001", "label": "Customer 1001"}},
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] == "ranking_analytics"
    slots = ((payload.get("answer") or {}).get("query_slots") or {})
    assert bool(slots.get("use_current_page_context")) is True
    assert slots.get("primary_entity_type") in {"products", "customers"}


def test_assistant_export_ranked_regions_workbook(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post("/ai/chat", json={"message": "Export top 5 regions by revenue to Excel", "context": {"page": "overview"}})
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] == "export_request"
    actions = ((payload.get("answer") or {}).get("export_actions") or [])
    assert isinstance(actions, list)
    if actions:
        first = actions[0]
        download_url = str(first.get("download_url") or "")
        assert download_url
        dl = assistant_client.get(download_url)
        assert dl.status_code == 200


def test_assistant_file_request_parses_chart_and_all_columns(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post(
        "/ai/chat",
        json={
            "message": "Create workbook for top 10 customers by revenue with charts and all available columns.",
            "context": {"page": "overview"},
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] == "export_request"
    slots = ((payload.get("answer") or {}).get("query_slots") or {})
    assert bool(slots.get("export_requested")) is True
    assert bool(slots.get("include_chart")) is True
    assert bool(slots.get("include_all_allowed_columns")) is True
    titles = {item.get("title") for item in ((payload.get("answer") or {}).get("evidence") or []) if isinstance(item, dict)}
    assert "Export Configuration Draft" in titles
    assert "Exportable Columns" in titles


def test_assistant_csv_file_request_returns_csv_download(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post(
        "/ai/chat",
        json={
            "message": "Create CSV file for top 5 regions by revenue.",
            "context": {"page": "overview"},
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] == "export_request"
    actions = ((payload.get("answer") or {}).get("export_actions") or [])
    assert isinstance(actions, list)
    if actions:
        first = actions[0]
        assert str(first.get("filename") or "").lower().endswith(".csv")
        download_url = str(first.get("download_url") or "")
        assert download_url
        dl = assistant_client.get(download_url)
        assert dl.status_code == 200
        assert "text/csv" in str(dl.headers.get("Content-Type") or "").lower()


def test_assistant_all_available_columns_request_returns_policy(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post("/ai/chat", json={"message": "Export this page with all available columns.", "context": {"page": "overview"}})
    assert resp.status_code == 200
    payload = resp.get_json()
    columns_blob = ((payload.get("answer") or {}).get("export_columns") or {})
    assert isinstance(columns_blob, dict)
    assert bool(columns_blob.get("include_all_allowed_columns")) is True
    assert isinstance(columns_blob.get("all_allowed_columns"), list)


def test_assistant_export_column_policy_respects_restricted_role(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="warehouse")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post("/ai/chat", json={"message": "Create workbook for this page with all available columns.", "context": {"page": "overview"}})
    assert resp.status_code == 200
    payload = resp.get_json()
    columns_blob = ((payload.get("answer") or {}).get("export_columns") or {})
    assert isinstance(columns_blob, dict)
    assert bool(columns_blob.get("export_sensitive")) is False


def test_assistant_async_export_job_status_flow(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post(
        "/ai/chat",
        json={
            "message": "Create async workbook for top 5 regions by revenue with charts.",
            "context": {"page": "overview"},
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] == "export_request"
    actions = ((payload.get("answer") or {}).get("export_actions") or [])
    assert actions
    first = actions[0]
    status = str(first.get("status") or "").lower()
    assert status in {"pending", "running", "completed"}
    status_url = str(first.get("status_url") or "")
    assert status_url

    final_status = status
    for _ in range(25):
        job_resp = assistant_client.get(status_url)
        assert job_resp.status_code == 200
        job_payload = job_resp.get_json()
        assert job_payload.get("status") == "ok"
        job = job_payload.get("job") or {}
        final_status = str(job.get("status") or "").lower()
        if final_status == "completed":
            export_id = str(job.get("export_id") or "")
            assert export_id
            dl_url = str(job.get("download_url") or "")
            assert dl_url
            dl = assistant_client.get(dl_url)
            assert dl.status_code == 200
            break
        time.sleep(0.05)
    assert final_status in {"completed", "running", "pending"}


def test_assistant_async_export_request_dedupes_job(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    body = {
        "message": "Create async workbook for top 10 products by revenue.",
        "context": {"page": "overview"},
    }
    first_resp = assistant_client.post("/ai/chat", json=body)
    second_resp = assistant_client.post("/ai/chat", json=body)
    assert first_resp.status_code == 200
    assert second_resp.status_code == 200
    first_payload = first_resp.get_json()
    second_payload = second_resp.get_json()
    first_actions = ((first_payload.get("answer") or {}).get("export_actions") or [])
    second_actions = ((second_payload.get("answer") or {}).get("export_actions") or [])
    assert first_actions and second_actions
    first_job = str((first_actions[0] or {}).get("job_id") or "")
    second_job = str((second_actions[0] or {}).get("job_id") or "")
    assert first_job
    assert second_job
    assert first_job == second_job


def test_assistant_chart_image_export_downloads_svg(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post(
        "/ai/chat",
        json={
            "message": "Create chart image as SVG for revenue trend on this page.",
            "context": {"page": "overview"},
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] == "export_request"
    actions = ((payload.get("answer") or {}).get("export_actions") or [])
    assert actions
    first = actions[0]
    assert str(first.get("format") or "").lower() == "svg"
    download_url = str(first.get("download_url") or "")
    assert download_url
    dl = assistant_client.get(download_url)
    assert dl.status_code == 200
    assert "image/svg+xml" in str(dl.headers.get("Content-Type") or "").lower()


def test_assistant_export_actions_prioritize_actionable_statuses():
    from app.assistant import service as assistant_service

    followup = assistant_service.FollowupResolution(resolved_message="export this", is_followup=False)
    slots = assistant_service.SemanticSlots(intent_type="ranking", export_requested=True, export_intent_type="export_ranked_list")
    results = [
        {
            "status": "ok",
            "title": "Assistant File Export",
            "data": {"status": "empty", "format": "csv"},
            "window_used": {},
            "scope_used": {},
            "notes": [],
            "next_actions": [],
            "citations": [],
        },
        {
            "status": "ok",
            "title": "Assistant File Export",
            "data": {
                "status": "completed",
                "format": "xlsx",
                "export_id": "ax_test_completed",
                "filename": "test_export.xlsx",
                "download_url": "/ai/exports/ax_test_completed/download",
                "sheets": ["Summary"],
            },
            "window_used": {},
            "scope_used": {},
            "notes": [],
            "next_actions": [],
            "citations": [],
        },
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
    actions = list(synthesized.get("export_actions") or [])
    assert actions
    assert str(actions[0].get("status") or "").lower() == "completed"
    assert str(actions[0].get("download_url") or "").strip()


def test_assistant_concentration_sanity_flags_impossible_share():
    from app.assistant import service as assistant_service

    line = assistant_service._ranking_concentration_line(
        [
            {"label": "Atlas Foods", "metric_value": 120.0},
            {"label": "Beacon Markets", "metric_value": -20.0},
        ],
        metric="revenue",
    )
    assert "should be validated" in line.lower()


def test_assistant_digest_schedule_endpoints_workflow(assistant_client):
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    create_resp = assistant_client.post(
        "/ai/digest/schedules",
        json={"module": "overview", "cadence": "weekly", "audience": "leadership", "length": "short", "hour_local": 9},
    )
    assert create_resp.status_code == 200
    create_payload = create_resp.get_json()
    assert create_payload["status"] == "ok"
    schedule = create_payload.get("schedule") or {}
    schedule_id = str(schedule.get("schedule_id") or "")
    assert schedule_id.startswith("sch_")

    list_resp = assistant_client.get("/ai/digest/schedules")
    assert list_resp.status_code == 200
    list_payload = list_resp.get_json()
    assert list_payload["status"] == "ok"
    assert any(str(item.get("schedule_id") or "") == schedule_id for item in list_payload.get("schedules") or [])

    run_resp = assistant_client.post(f"/ai/digest/schedules/{schedule_id}/run", json={})
    assert run_resp.status_code == 200
    run_payload = run_resp.get_json()
    assert run_payload["status"] in {"ok", "empty", "forbidden"}
    assert isinstance(run_payload.get("digest"), (dict, type(None)))

    delete_resp = assistant_client.delete(f"/ai/digest/schedules/{schedule_id}")
    assert delete_resp.status_code == 200
    delete_payload = delete_resp.get_json()
    assert delete_payload["status"] == "ok"
    assert delete_payload["deleted"] is True


def test_assistant_supplier_page_top_customers_uses_supplier_context(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="production")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post(
        "/ai/chat",
        json={
            "message": "Top customers for this supplier by revenue.",
            "context": {
                "page": "suppliers",
                "entity": {"type": "supplier", "id": "SUP-001", "label": "Supplier 001"},
            },
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] == "ranking_analytics"
    slots = ((payload.get("answer") or {}).get("query_slots") or {})
    assert bool(slots.get("use_current_page_context")) is True
    selected = str(slots.get("selected_entity_name") or "")
    assert "sup" in selected.lower() or "supplier" in selected.lower()
    assert "supplier" in str(payload.get("resolved_message") or "").lower()


def test_assistant_filtered_sales_rep_ranking_resolves_fraser_and_avoids_generic_admin_answer(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service
    from app.assistant import tools as assistant_tools

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    monkeypatch.setattr(
        assistant_tools,
        "_module_bundle",
        _assistant_bundle_factory(
            {
                "products": [
                    {"product_id": "P-OVERALL", "product_name": "Overall Leader", "revenue": 9999.0, "profit": 3100.0, "margin_pct": 31.0},
                ],
                ("products", "sales_reps", "REP-FRASER"): [
                    {"product_id": "P-F1", "product_name": "Fraser Ribeye", "revenue": 4200.0, "profit": 900.0, "margin_pct": 21.4, "orders": 12},
                    {"product_id": "P-F2", "product_name": "Fraser Striploin", "revenue": 2800.0, "profit": 530.0, "margin_pct": 18.9, "orders": 8},
                ],
            }
        ),
        raising=True,
    )
    monkeypatch.setattr(
        assistant_tools,
        "_resolve_entity_reference_match",
        lambda *_args, **_kwargs: {
            "entity_type": "salesreps",
            "query": "Fraser",
            "matched": True,
            "id": "REP-FRASER",
            "label": "Fraser Mittlestead",
            "score": 100,
            "filter_token": "REP-FRASER",
            "candidates": [{"id": "REP-FRASER", "label": "Fraser Mittlestead", "score": 100}],
        },
        raising=True,
    )

    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post("/ai/chat", json={"message": "top 10 products sold by fraser", "context": {"page": "admin"}})
    assert resp.status_code == 200
    payload = resp.get_json()
    answer = payload.get("answer") or {}
    ranked = answer.get("ranked_results") or []
    labels = {str(row.get("label") or "") for row in ranked if isinstance(row, dict)}

    assert payload["question_type"] == "ranking_analytics"
    assert payload["module"] == "products"
    assert "Fraser Ribeye" in labels
    assert "Overall Leader" not in labels
    assert "Fraser Mittlestead" in str(answer.get("direct_answer") or "") or "Fraser Mittlestead" in str(answer.get("explanation") or "")
    assert "admin" not in str(answer.get("direct_answer") or "").lower()
    slots = answer.get("query_slots") or {}
    assert slots.get("relationship_entity_type") == "salesreps"


def test_assistant_simpler_followup_stays_on_previous_subject(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service
    from app.assistant import tools as assistant_tools

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    monkeypatch.setattr(
        assistant_tools,
        "_module_bundle",
        _assistant_bundle_factory(
            {
                ("products", "sales_reps", "REP-FRASER"): [
                    {"product_id": "P-F1", "product_name": "Fraser Ribeye", "revenue": 4200.0, "profit": 900.0, "margin_pct": 21.4, "orders": 12},
                    {"product_id": "P-F2", "product_name": "Fraser Striploin", "revenue": 2800.0, "profit": 530.0, "margin_pct": 18.9, "orders": 8},
                ],
            }
        ),
        raising=True,
    )
    monkeypatch.setattr(
        assistant_tools,
        "_resolve_entity_reference_match",
        lambda *_args, **_kwargs: {
            "entity_type": "salesreps",
            "query": "Fraser",
            "matched": True,
            "id": "REP-FRASER",
            "label": "Fraser Mittlestead",
            "score": 100,
            "filter_token": "REP-FRASER",
            "candidates": [{"id": "REP-FRASER", "label": "Fraser Mittlestead", "score": 100}],
        },
        raising=True,
    )

    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    first = assistant_client.post("/ai/chat", json={"message": "top 5 products sold by Fraser", "context": {"page": "admin"}})
    assert first.status_code == 200
    thread_id = str((first.get_json() or {}).get("thread_id") or "")

    follow = assistant_client.post(
        "/ai/chat",
        json={"message": "Explain that in simpler terms.", "thread_id": thread_id, "context": {"page": "admin"}},
    )
    assert follow.status_code == 200
    payload = follow.get_json()
    answer = payload.get("answer") or {}
    explanation = str(answer.get("explanation") or "")
    assert answer.get("response_mode") == "simple"
    assert payload["question_type"] == "ranking_analytics"
    assert "Fraser" in str(answer.get("direct_answer") or "") or "Fraser" in explanation
    assert "In plain English:" in explanation
    assert "Context:" not in explanation


@pytest.mark.parametrize(
    ("followup_message", "expected_module", "expected_phrase"),
    [
        ("What about suppliers?", "suppliers", "prairie meats margin pressure"),
        ("What about sales reps?", "salesreps", "fraser's portfolio"),
    ],
)
def test_assistant_focus_shift_followups_keep_proactive_context(assistant_client, monkeypatch, followup_message, expected_module, expected_phrase):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    monkeypatch.setattr(assistant_service, "execute_tool", _assistant_followup_execute_tool, raising=True)

    user, password = _make_user(role="admin")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    first = assistant_client.post("/ai/chat", json={"message": "What stands out most right now?", "context": {"page": "overview"}})
    assert first.status_code == 200
    thread_id = str((first.get_json() or {}).get("thread_id") or "")
    assert thread_id

    follow = assistant_client.post(
        "/ai/chat",
        json={"message": followup_message, "thread_id": thread_id, "context": {"page": "overview"}},
    )
    assert follow.status_code == 200
    payload = follow.get_json()
    answer = payload.get("answer") or {}

    assert payload["question_type"] == "proactive_insights"
    assert payload["module"] == expected_module
    assert expected_phrase in str(answer.get("direct_answer") or "").lower()
    assert "page bundle" not in str(answer.get("direct_answer") or "").lower()


def test_assistant_repeated_focus_shift_replaces_previous_focus_cleanly(assistant_client, monkeypatch, tmp_path):
    from app.assistant import service as assistant_service
    from app.assistant import memory as assistant_memory

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    monkeypatch.setattr(assistant_service, "execute_tool", _assistant_followup_execute_tool, raising=True)

    user, password = _make_user(role="admin")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)
    with assistant_client.application.app_context():
        assistant_client.application.config["ASSISTANT_THREAD_STORE_PATH"] = (tmp_path / "assistant_threads.sqlite3").as_posix()

    first = assistant_client.post("/ai/chat", json={"message": "What stands out most right now?", "context": {"page": "overview"}})
    assert first.status_code == 200
    thread_id = str((first.get_json() or {}).get("thread_id") or "")
    assert thread_id

    second = assistant_client.post(
        "/ai/chat",
        json={"message": "What about suppliers?", "thread_id": thread_id, "context": {"page": "overview"}},
    )
    assert second.status_code == 200
    with assistant_memory._LOCK:
        assistant_memory._CACHE.clear()

    third = assistant_client.post(
        "/ai/chat",
        json={"message": "What about sales reps?", "thread_id": thread_id, "context": {"page": "overview"}},
    )
    assert third.status_code == 200
    payload = third.get_json()
    answer = payload.get("answer") or {}

    assert payload["question_type"] == "proactive_insights"
    assert payload["module"] == "salesreps"
    assert "focus on suppliers" not in str(payload.get("resolved_message") or "").lower()
    assert "sales rep risk is concentrated" in str(answer.get("direct_answer") or "").lower()


def test_assistant_compare_with_last_year_followup_uses_previous_context(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    monkeypatch.setattr(assistant_service, "execute_tool", _assistant_followup_execute_tool, raising=True)

    user, password = _make_user(role="admin")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    first = assistant_client.post("/ai/chat", json={"message": "What stands out most right now?", "context": {"page": "overview"}})
    assert first.status_code == 200
    thread_id = str((first.get_json() or {}).get("thread_id") or "")
    assert thread_id

    follow = assistant_client.post(
        "/ai/chat",
        json={"message": "Compare with last year", "thread_id": thread_id, "context": {"page": "overview"}},
    )
    assert follow.status_code == 200
    payload = follow.get_json()
    answer = payload.get("answer") or {}

    assert payload["question_type"] == "comparison_analytics"
    assert "compare the prior result" in str(payload.get("resolved_message") or "").lower()
    assert "maple foods" in str(answer.get("direct_answer") or "").lower()
    assert "page bundle" not in str(answer.get("direct_answer") or "").lower()


def test_assistant_simpler_followup_preserves_proactive_context(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    monkeypatch.setattr(assistant_service, "execute_tool", _assistant_followup_execute_tool, raising=True)

    user, password = _make_user(role="admin")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    first = assistant_client.post("/ai/chat", json={"message": "What stands out most right now?", "context": {"page": "overview"}})
    assert first.status_code == 200
    thread_id = str((first.get_json() or {}).get("thread_id") or "")
    assert thread_id

    follow = assistant_client.post(
        "/ai/chat",
        json={"message": "Explain that in simpler terms.", "thread_id": thread_id, "context": {"page": "overview"}},
    )
    assert follow.status_code == 200
    payload = follow.get_json()
    answer = payload.get("answer") or {}

    assert payload["question_type"] == "proactive_insights"
    assert answer.get("response_mode") == "simple"
    assert "in plain english:" in str(answer.get("explanation") or "").lower()
    assert "west revenue softness" in str(answer.get("explanation") or "").lower() or "revenue softness" in str(answer.get("direct_answer") or "").lower()
    assert "page bundle" not in str(answer.get("direct_answer") or "").lower()


def test_assistant_driver_question_on_products_page_uses_change_explanation(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    monkeypatch.setattr(assistant_service, "execute_tool", _assistant_followup_execute_tool, raising=True)

    user, password = _make_user(role="admin")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post("/ai/chat", json={"message": "Why is revenue changing?", "context": {"page": "products"}})
    assert resp.status_code == 200
    payload = resp.get_json()
    answer = payload.get("answer") or {}
    section_titles = [str(item.get("title") or "") for item in (answer.get("sections") or []) if isinstance(item, dict)]
    rendered = " ".join(str(item) for item in [answer.get("direct_answer"), answer.get("explanation")]).lower()

    assert payload["question_type"] == "driver_mover"
    assert section_titles == ["What Changed", "What’s Driving It"]
    assert "revenue is down" in str(answer.get("direct_answer") or "").lower()
    assert "mix shifted into lower-margin skus" in rendered
    assert "short rib" in rendered or "brisket" in rendered
    assert "page bundle" not in rendered


def test_assistant_nested_customer_product_question_returns_hierarchy(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service
    from app.assistant import tools as assistant_tools

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    monkeypatch.setattr(
        assistant_tools,
        "_module_bundle",
        _assistant_bundle_factory(
            {
                "customers": [
                    {"customer_id": "CUST-A", "customer_name": "Atlas Foods", "revenue": 7000.0, "profit": 1200.0, "margin_pct": 17.1},
                    {"customer_id": "CUST-B", "customer_name": "Beacon Markets", "revenue": 5600.0, "profit": 980.0, "margin_pct": 17.5},
                ],
                ("products", "customers", "CUST-A"): [
                    {"product_id": "PA-1", "product_name": "Atlas Prime Rib", "revenue": 2600.0, "profit": 420.0, "margin_pct": 16.2},
                    {"product_id": "PA-2", "product_name": "Atlas Ground Beef", "revenue": 1900.0, "profit": 310.0, "margin_pct": 16.3},
                ],
                ("products", "customers", "CUST-B"): [
                    {"product_id": "PB-1", "product_name": "Beacon Striploin", "revenue": 2200.0, "profit": 410.0, "margin_pct": 18.6},
                    {"product_id": "PB-2", "product_name": "Beacon Tenderloin", "revenue": 1500.0, "profit": 295.0, "margin_pct": 19.7},
                ],
            }
        ),
        raising=True,
    )

    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post("/ai/chat", json={"message": "top 10 customers and their top 10 products", "context": {"page": "overview"}})
    assert resp.status_code == 200
    payload = resp.get_json()
    answer = payload.get("answer") or {}
    nested = answer.get("nested_results") or {}
    groups = nested.get("groups") or []

    assert payload["question_type"] == "ranking_analytics"
    assert payload["module"] == "customers"
    assert answer.get("query_slots", {}).get("query_shape") == "nested_ranking"
    assert groups and len(groups) == 2
    assert groups[0]["parent_label"] == "Atlas Foods"
    assert any(str(child.get("label") or "") == "Atlas Prime Rib" for child in groups[0].get("children") or [])
    section_titles = [str(item.get("title") or "") for item in (answer.get("sections") or []) if isinstance(item, dict)]
    assert "What Stands Out" in section_titles


def test_assistant_nested_supplier_product_question_returns_grouped_breakdown(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service
    from app.assistant import tools as assistant_tools

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    monkeypatch.setattr(
        assistant_tools,
        "_module_bundle",
        _assistant_bundle_factory(
            {
                "suppliers": [
                    {"supplier_id": "SUP-A", "supplier_name": "North Ranch", "revenue": 8200.0, "profit": 1500.0, "margin_pct": 18.3},
                    {"supplier_id": "SUP-B", "supplier_name": "Prairie Meats", "revenue": 6100.0, "profit": 1040.0, "margin_pct": 17.0},
                ],
                ("products", "suppliers", "SUP-A"): [
                    {"product_id": "SA-1", "product_name": "North Chuck", "revenue": 3400.0, "profit": 600.0, "margin_pct": 17.6},
                    {"product_id": "SA-2", "product_name": "North Brisket", "revenue": 2800.0, "profit": 520.0, "margin_pct": 18.5},
                ],
                ("products", "suppliers", "SUP-B"): [
                    {"product_id": "SB-1", "product_name": "Prairie Flat Iron", "revenue": 2500.0, "profit": 420.0, "margin_pct": 16.8},
                    {"product_id": "SB-2", "product_name": "Prairie Sirloin", "revenue": 2100.0, "profit": 360.0, "margin_pct": 17.1},
                ],
            }
        ),
        raising=True,
    )

    user, password = _make_user(role="production")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post("/ai/chat", json={"message": "top 5 suppliers and their top 10 products", "context": {"page": "overview"}})
    assert resp.status_code == 200
    payload = resp.get_json()
    answer = payload.get("answer") or {}
    nested = answer.get("nested_results") or {}

    assert payload["question_type"] == "ranking_analytics"
    assert payload["module"] == "suppliers"
    assert any(group.get("parent_label") == "North Ranch" for group in nested.get("groups") or [])
    assert "North Chuck" in str(answer.get("direct_answer") or "") or "North Chuck" in str(nested)


def test_assistant_analyst_followup_is_materially_richer_than_standard_ranking(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service
    from app.assistant import tools as assistant_tools

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    monkeypatch.setattr(
        assistant_tools,
        "_module_bundle",
        _assistant_bundle_factory(
            {
                "products": [
                    {"product_id": "P-1", "product_name": "Fraser Ribeye", "revenue": 4200.0, "profit": 900.0, "margin_pct": 21.4, "orders": 12},
                    {"product_id": "P-2", "product_name": "Fraser Striploin", "revenue": 2800.0, "profit": 530.0, "margin_pct": 18.9, "orders": 8},
                ]
            }
        ),
        raising=True,
    )
    monkeypatch.setattr(
        assistant_tools,
        "_resolve_entity_reference_match",
        lambda *_args, **_kwargs: {
            "entity_type": "salesreps",
            "query": "Fraser",
            "matched": True,
            "id": "REP-FRASER",
            "label": "Fraser Mittlestead",
            "score": 100,
            "filter_token": "REP-FRASER",
            "candidates": [{"id": "REP-FRASER", "label": "Fraser Mittlestead", "score": 100}],
        },
        raising=True,
    )

    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    standard = assistant_client.post("/ai/chat", json={"message": "top 5 products sold by Fraser", "context": {"page": "admin"}})
    assert standard.status_code == 200
    thread_id = str((standard.get_json() or {}).get("thread_id") or "")
    analyst = assistant_client.post(
        "/ai/chat",
        json={"message": "Give me a more detailed analyst version", "thread_id": thread_id, "context": {"page": "admin"}},
    )
    assert analyst.status_code == 200

    standard_answer = (standard.get_json() or {}).get("answer") or {}
    analyst_payload = analyst.get_json() or {}
    analyst_answer = analyst_payload.get("answer") or {}
    analyst_sections = [str(item.get("title") or "") for item in (analyst_answer.get("sections") or []) if isinstance(item, dict)]

    assert analyst_payload["question_type"] in {"ranking_analytics", "analyst_detail"}
    assert analyst_answer.get("response_mode") == "analyst"
    assert "Analyst Lens" in analyst_sections
    assert len(str(analyst_answer.get("explanation") or "")) > len(str(standard_answer.get("explanation") or ""))
    analyst_explanation = str(analyst_answer.get("explanation") or "").lower()
    assert "analyst mode" in analyst_explanation
    assert "margin" in analyst_explanation or "concentration" in analyst_explanation or "detail:" in analyst_explanation


def test_assistant_action_followup_uses_previous_subject_and_returns_actions(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service
    from app.assistant import tools as assistant_tools

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    monkeypatch.setattr(
        assistant_tools,
        "_module_bundle",
        _assistant_bundle_factory(
            {
                ("products", "sales_reps", "REP-FRASER"): [
                    {"product_id": "P-F1", "product_name": "Fraser Ribeye", "revenue": 4200.0, "profit": 900.0, "margin_pct": 21.4, "orders": 12},
                    {"product_id": "P-F2", "product_name": "Fraser Striploin", "revenue": 2800.0, "profit": 530.0, "margin_pct": 18.9, "orders": 8},
                ],
            }
        ),
        raising=True,
    )
    monkeypatch.setattr(
        assistant_tools,
        "_resolve_entity_reference_match",
        lambda *_args, **_kwargs: {
            "entity_type": "salesreps",
            "query": "Fraser",
            "matched": True,
            "id": "REP-FRASER",
            "label": "Fraser Mittlestead",
            "score": 100,
            "filter_token": "REP-FRASER",
            "candidates": [{"id": "REP-FRASER", "label": "Fraser Mittlestead", "score": 100}],
        },
        raising=True,
    )

    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    first = assistant_client.post("/ai/chat", json={"message": "top 5 products sold by Fraser", "context": {"page": "admin"}})
    assert first.status_code == 200
    thread_id = str((first.get_json() or {}).get("thread_id") or "")

    def _fake_execute_tool(name, ctx, args=None):
        base = {
            "status": "ok",
            "module": "cross_module",
            "scope_used": {},
            "window_used": {},
            "notes": [],
            "next_actions": [],
            "citations": [],
            "data": {},
        }
        if name == "get_current_page_context":
            return {**base, "title": "Current Page Context", "module": "products", "data": {"page": "products"}}
        if name == "get_user_scope":
            return {**base, "title": "User Scope", "data": {"scope_mode": "all"}}
        if name == "get_priority_investigations":
            return {
                **base,
                "title": "Priority Investigations",
                "data": {
                    "investigations": [
                        {
                            "module": "products",
                            "priority": 95,
                            "severity": "high",
                            "title": "Fraser Ribeye margin compression",
                            "detail": "Margin on Fraser Ribeye is under pressure and needs immediate review.",
                        }
                    ]
                },
            }
        if name == "get_priority_actions":
            return {
                **base,
                "title": "Priority Actions",
                "data": {
                    "actions": [
                        "Review Fraser Ribeye pricing and customer mix this week.",
                        "Validate whether the softness is coming from volume or mix before discounting.",
                    ]
                },
            }
        if name == "get_cross_module_risk_summary":
            return {**base, "title": "Cross-Module Risk Summary", "data": {"summary": "The downside is concentrated in one product line."}}
        if name == "compare_entities":
            return {**base, "title": "Entity Comparison", "data": {"top": [], "bottom": []}}
        if name == "get_entity_relationship_context":
            return {**base, "title": "Entity Relationship Context", "data": {}}
        if name == "get_related_investigations":
            return {**base, "title": "Related Investigations", "data": {"suggestions": ["Show me the customer mix behind Fraser Ribeye."]}}
        if name == "get_recommended_followups":
            return {**base, "title": "Recommended Follow-up Questions", "data": {"suggestions": ["Show history for Fraser Ribeye."]}}
        if name == "get_next_best_questions":
            return {**base, "title": "Next Best Questions", "data": {"questions": ["Which customers drove Fraser Ribeye?"]}}
        return {**base, "title": name.replace("_", " ").title()}

    monkeypatch.setattr(assistant_service, "execute_tool", _fake_execute_tool, raising=True)

    follow = assistant_client.post(
        "/ai/chat",
        json={"message": "What should I do next?", "thread_id": thread_id, "context": {"page": "admin"}},
    )
    assert follow.status_code == 200
    payload = follow.get_json()
    answer = payload.get("answer") or {}
    actions = answer.get("action_suggestions") or []
    assert payload["question_type"] == "risk_action"
    assert "fraser" in str(payload.get("resolved_message") or "").lower()
    assert actions
    assert "review fraser ribeye pricing" in str(actions[0]).lower()
    assert "top priority" in str((answer.get("sections") or [])[0].get("title") or "").lower()


def test_assistant_customer_driver_followup_uses_previous_answer_context(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service
    from app.assistant import tools as assistant_tools

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)

    def _fake_overview_context(_ctx):
        return {
            "scorecard_kpis": {"revenue": 152000.0, "profit": 21000.0, "orders": 44},
            "bundle": {"meta": {"window": {"start": "2025-01-01", "end": "2025-01-31", "rows": 44}}},
            "trend_series": {"monthly": {"labels": ["2025-01"], "revenue": [152000.0]}},
            "drivers": {"mom": {"price": -4200.0, "volume": -11500.0, "mix": 1800.0}},
            "movers": {
                "customer": {
                    "gainers": [{"customer_name": "Atlas Foods", "revenue": 6200.0}],
                    "decliners": [{"customer_name": "Beacon Markets", "revenue": -8400.0}],
                },
                "product": {
                    "gainers": [{"product_name": "Prime Rib", "revenue": 2800.0}],
                    "decliners": [{"product_name": "Ground Beef", "revenue": -5100.0}],
                },
            },
            "risk": {"concentration": {}, "profitability": {"margin_risk": []}},
            "data_health": {"cost_coverage_pct": 96.0},
            "forecast": {"enabled": False},
        }

    monkeypatch.setattr(assistant_tools, "_overview_context", _fake_overview_context, raising=True)

    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    first = assistant_client.post("/ai/chat", json={"message": "Why is revenue changing?", "context": {"page": "overview"}})
    assert first.status_code == 200
    thread_id = str((first.get_json() or {}).get("thread_id") or "")

    follow = assistant_client.post(
        "/ai/chat",
        json={"message": "Which customers drove this change?", "thread_id": thread_id, "context": {"page": "overview"}},
    )
    assert follow.status_code == 200
    payload = follow.get_json()
    answer = payload.get("answer") or {}
    text = " ".join(str(item) for item in [answer.get("direct_answer"), answer.get("explanation")])
    assert payload["question_type"] == "driver_mover"
    assert "revenue change" in str(payload.get("resolved_message") or "").lower()
    assert "atlas foods" in text.lower() or "beacon markets" in text.lower()


def test_assistant_large_hierarchy_adds_export_path_and_compacts_inline_output(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service
    from app.assistant import tools as assistant_tools

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    monkeypatch.setattr(
        assistant_tools,
        "_module_bundle",
        _assistant_bundle_factory(
            {
                "customers": [
                    {"customer_id": f"CUST-{idx}", "customer_name": f"Customer {idx}", "revenue": float(9000 - idx * 500), "profit": 1000.0, "margin_pct": 15.0}
                    for idx in range(1, 5)
                ],
                **{
                    ("products", "customers", f"CUST-{idx}"): [
                        {
                            "product_id": f"P-{idx}-{child}",
                            "product_name": f"Customer {idx} Product {child}",
                            "revenue": float(1500 - child * 40),
                            "profit": 220.0,
                            "margin_pct": 14.8,
                        }
                        for child in range(1, 9)
                    ]
                    for idx in range(1, 5)
                },
            }
        ),
        raising=True,
    )

    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post("/ai/chat", json={"message": "top 10 customers and their top 10 products", "context": {"page": "overview"}})
    assert resp.status_code == 200
    payload = resp.get_json()
    answer = payload.get("answer") or {}
    nested = answer.get("nested_results") or {}
    section_titles = [str(item.get("title") or "") for item in (answer.get("sections") or []) if isinstance(item, dict)]

    assert nested.get("render_strategy") in {"compact_summary", "export_recommended"}
    assert "Export Path" in section_titles


def test_assistant_export_hierarchical_analysis_workbook(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service
    from app.assistant import tools as assistant_tools

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    monkeypatch.setattr(
        assistant_tools,
        "_module_bundle",
        _assistant_bundle_factory(
            {
                "suppliers": [
                    {"supplier_id": "SUP-A", "supplier_name": "North Ranch", "revenue": 8200.0, "profit": 1500.0, "margin_pct": 18.3},
                    {"supplier_id": "SUP-B", "supplier_name": "Prairie Meats", "revenue": 6100.0, "profit": 1040.0, "margin_pct": 17.0},
                ],
                ("products", "suppliers", "SUP-A"): [
                    {"product_id": "SA-1", "product_name": "North Chuck", "revenue": 3400.0, "profit": 600.0, "margin_pct": 17.6},
                ],
                ("products", "suppliers", "SUP-B"): [
                    {"product_id": "SB-1", "product_name": "Prairie Flat Iron", "revenue": 2500.0, "profit": 420.0, "margin_pct": 16.8},
                ],
            }
        ),
        raising=True,
    )

    user, password = _make_user(role="production")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post(
        "/ai/chat",
        json={"message": "export top 5 suppliers and their top 10 products to excel", "context": {"page": "overview"}},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    actions = ((payload.get("answer") or {}).get("export_actions") or [])

    assert payload["question_type"] == "export_request"
    assert actions
    first = actions[0]
    assert str(first.get("filename") or "").lower().endswith((".xlsx", ".csv"))
    assert "Child Detail" in list(first.get("sheets") or [])
    download_url = str(first.get("download_url") or "")
    assert download_url
    dl = assistant_client.get(download_url)
    assert dl.status_code == 200


def test_assistant_returns_pending_and_workflow_routes_returns_intent(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="warehouse")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post(
        "/ai/chat",
        json={
            "message": "What approvals are pending and explain the returns workflow?",
            "context": {"page": "returns"},
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] in {"returns_workflow", "returns_analytics"}
    sections = [str(item.get("title") or "") for item in (((payload.get("answer") or {}).get("sections") or [])) if isinstance(item, dict)]
    assert any(title in {"Returns Summary", "Operational Context", "Workflow Help"} for title in sections)
    evidence_titles = {str(item.get("title") or "") for item in (((payload.get("answer") or {}).get("evidence") or [])) if isinstance(item, dict)}
    assert "Pending Returns And Approvals" in evidence_titles or "Returns Workflow Help" in evidence_titles


def test_assistant_returns_only_role_handles_returns_but_not_overview(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="returns_only")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    returns_resp = assistant_client.post(
        "/ai/chat",
        json={"message": "What approvals are pending and explain the returns workflow?", "context": {"page": "returns"}},
    )
    overview_resp = assistant_client.post(
        "/ai/chat",
        json={"message": "How is business performance and margin today?", "context": {"page": "overview"}},
    )
    proactive_overview_resp = assistant_client.post(
        "/ai/chat",
        json={"message": "What stands out most right now?", "context": {"page": "overview"}},
    )
    assert returns_resp.status_code == 200
    assert overview_resp.status_code == 200
    assert proactive_overview_resp.status_code == 200

    returns_payload = returns_resp.get_json()
    overview_payload = overview_resp.get_json()
    proactive_overview_payload = proactive_overview_resp.get_json()
    assert returns_payload["question_type"] in {"returns_workflow", "returns_analytics"}
    assert ((returns_payload.get("answer") or {}).get("permission_limited")) is False
    assert ((overview_payload.get("answer") or {}).get("permission_limited")) is True
    assert ((proactive_overview_payload.get("answer") or {}).get("permission_limited")) is True
    assert "current access permissions" in str(((proactive_overview_payload.get("answer") or {}).get("direct_answer") or "")).lower()


def test_assistant_comparison_and_history_are_materially_different_same_page(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    compare = assistant_client.post(
        "/ai/chat",
        json={"message": "Compare this page with last year.", "context": {"page": "overview"}},
    )
    history = assistant_client.post(
        "/ai/chat",
        json={"message": "Show full history for this page over time.", "context": {"page": "overview"}},
    )
    assert compare.status_code == 200
    assert history.status_code == 200
    compare_payload = compare.get_json()
    history_payload = history.get_json()
    assert compare_payload["question_type"] == "comparison_analytics"
    assert history_payload["question_type"] == "history_analytics"
    compare_direct = str(((compare_payload.get("answer") or {}).get("direct_answer")) or "").strip().lower()
    history_direct = str(((history_payload.get("answer") or {}).get("direct_answer")) or "").strip().lower()
    assert compare_direct and history_direct
    assert compare_direct != history_direct


def test_assistant_sales_export_columns_exclude_sensitive_financial_fields(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    resp = assistant_client.post(
        "/ai/chat",
        json={
            "message": "Create workbook for top 10 customers by revenue with all available columns.",
            "context": {"page": "overview"},
        },
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["question_type"] == "export_request"
    export_columns = ((payload.get("answer") or {}).get("export_columns") or {})
    assert bool(export_columns.get("export_sensitive")) is False
    allowed = {str(col).strip().lower() for col in list(export_columns.get("all_allowed_columns") or []) if str(col).strip()}
    forbidden = {"cost", "unit_cost", "extended_cost", "profit", "gross_profit", "profit_per_order", "margin", "margin_pct"}
    assert allowed.isdisjoint(forbidden)


def test_assistant_followup_use_full_history_keeps_context_and_history_intent(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    first = assistant_client.post(
        "/ai/chat",
        json={
            "message": "Show history for this customer.",
            "context": {"page": "customers", "entity": {"type": "customer", "id": "CUST-001", "label": "Customer 001"}},
        },
    )
    assert first.status_code == 200
    thread_id = str((first.get_json() or {}).get("thread_id") or "")
    assert thread_id

    second = assistant_client.post(
        "/ai/chat",
        json={"message": "Use full history instead.", "thread_id": thread_id, "context": {"page": "customers"}},
    )
    assert second.status_code == 200
    payload = second.get_json()
    assert payload["question_type"] == "history_analytics"
    slots = ((payload.get("answer") or {}).get("query_slots") or {})
    assert bool(slots.get("use_full_history")) is True


def test_assistant_definition_and_ranking_have_different_section_shapes(assistant_client, monkeypatch):
    from app.assistant import service as assistant_service

    monkeypatch.setattr(assistant_service, "_provider_answer", lambda *args, **kwargs: None, raising=True)
    user, password = _make_user(role="sales")
    assistant_client.post("/auth/login", data={"username": user.username, "password": password}, follow_redirects=True)

    definition = assistant_client.post("/ai/chat", json={"message": "What is AOV?", "context": {"page": "overview"}})
    ranking = assistant_client.post("/ai/chat", json={"message": "Top 5 regions by revenue.", "context": {"page": "overview"}})
    assert definition.status_code == 200
    assert ranking.status_code == 200

    def_payload = definition.get_json()
    rank_payload = ranking.get_json()
    assert def_payload["question_type"] == "definition_help"
    assert rank_payload["question_type"] == "ranking_analytics"
    def_sections = [str(item.get("title") or "") for item in (((def_payload.get("answer") or {}).get("sections") or [])) if isinstance(item, dict)]
    rank_sections = [str(item.get("title") or "") for item in (((rank_payload.get("answer") or {}).get("sections") or [])) if isinstance(item, dict)]
    assert "Definition" in def_sections
    assert "Ranked Results" in rank_sections
