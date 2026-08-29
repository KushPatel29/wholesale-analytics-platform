# Test values are deliberately literal business examples, and the session-scoped
# app fixture is requested to ensure metadata initialization before service calls.
# ruff: noqa: ARG001, PLR2004

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.auth.models import SessionLocal
from app.auth.permissions import DEFAULT_ROLE_PERMISSION_KEYS, permission_registry
from app.decision_ops import presentation, service
from app.decision_ops.models import (
    ApprovalRecord,
    OperationalRecord,
    OperationalRecordEvent,
    OperationalRecordLine,
    SourceContract,
    WorkItem,
    WorkItemAttachment,
    WorkItemComment,
    WorkItemDependency,
    WorkItemEvent,
)
from app.planning_scenario_models import PlanningReplenishmentRecommendation, PlanningScenario


def _actor(user_id: int = 701):
    return SimpleNamespace(id=user_id, username=f"ops-{user_id}", role="gm")


def _delete_work_items(*ids: int) -> None:
    clean = [int(value) for value in ids if value]
    if not clean:
        return
    with SessionLocal() as session:
        session.query(ApprovalRecord).filter(ApprovalRecord.target_type == "work_item", ApprovalRecord.target_id.in_(clean)).delete(synchronize_session=False)
        session.query(WorkItemDependency).filter((WorkItemDependency.work_item_id.in_(clean)) | (WorkItemDependency.depends_on_work_item_id.in_(clean))).delete(synchronize_session=False)
        session.query(WorkItemAttachment).filter(WorkItemAttachment.work_item_id.in_(clean)).delete(synchronize_session=False)
        session.query(WorkItemComment).filter(WorkItemComment.work_item_id.in_(clean)).delete(synchronize_session=False)
        session.query(WorkItemEvent).filter(WorkItemEvent.work_item_id.in_(clean)).delete(synchronize_session=False)
        session.query(WorkItem).filter(WorkItem.id.in_(clean)).delete(synchronize_session=False)
        session.commit()


def _delete_records(*ids: int) -> None:
    clean = [int(value) for value in ids if value]
    if not clean:
        return
    with SessionLocal() as session:
        session.query(ApprovalRecord).filter(ApprovalRecord.target_type == "operational_record", ApprovalRecord.target_id.in_(clean)).delete(synchronize_session=False)
        session.query(OperationalRecordEvent).filter(OperationalRecordEvent.operational_record_id.in_(clean)).delete(synchronize_session=False)
        session.query(OperationalRecordLine).filter(OperationalRecordLine.operational_record_id.in_(clean)).delete(synchronize_session=False)
        session.query(OperationalRecord).filter(OperationalRecord.id.in_(clean)).delete(synchronize_session=False)
        session.commit()


def test_action_center_tracks_approval_evidence_dependencies_and_outcomes(app):
    actor = _actor()
    first = second = None
    try:
        first = service.create_work_item({
            "title": f"Pricing review {uuid4().hex}",
            "description": "Review measured margin exposure.",
            "source_module": "products",
            "source_record_id": "SKU-100",
            "source_url": "/products/?sku=SKU-100",
            "source_context": {"filters": {"region": "West"}},
            "affected_records": [{"type": "product", "id": "SKU-100"}],
            "metric_key": "gross_margin_pct",
            "baseline_value": 18.0,
            "target_value": 24.0,
            "metric_unit": "%",
            "expected_financial_impact": 12000,
            "owner_user_id": 42,
            "due_at": "2026-09-15T12:00:00+00:00",
            "priority": "high",
            "approval_route": "category_manager",
        }, actor=actor)
        second = service.create_work_item({"title": f"Supplier cost review {uuid4().hex}", "source_module": "suppliers", "source_url": "/suppliers/"}, actor=actor)
        service.add_dependency(first["id"], second["id"], actor=actor)
        service.add_comment(first["id"], "Owner confirmed the review scope.", actor=actor)
        service.add_attachment(first["id"], {"display_name": "Certified margin view", "uri": "/products/?sku=SKU-100"}, actor=actor)
        service.transition_work_item(first["id"], "pending_approval", actor=actor)
        approved = service.transition_work_item(first["id"], "approved", actor=actor, notes="Guardrails accepted.")
        assert approved["approval_status"] == "approved"
        service.transition_work_item(first["id"], "completed", actor=actor, outcomes={"outcome_value": 25.5, "realized_financial_impact": 9400})
        detail = service.get_work_item(first["id"])
        assert detail is not None
        assert detail["status"] == "completed"
        assert detail["source_context"]["filters"]["region"] == "West"
        assert detail["outcome_value"] == 25.5
        assert detail["realized_financial_impact"] == 9400
        assert detail["dependencies"] == [second["id"]]
        assert len(detail["comments"]) == 1
        assert len(detail["attachments"]) == 1
        assert [event["to_status"] for event in detail["events"] if event["to_status"]] == ["draft", "pending_approval", "approved", "completed"]
    finally:
        _delete_work_items(first and first["id"], second and second["id"])


def test_assistant_action_is_preview_then_confirmed_draft(app):
    actor = _actor(702)
    payload = {
        "title": f"Assistant margin review {uuid4().hex}",
        "source_module": "overview",
        "source_record_id": "margin-risk-149",
        "source_url": "/overview/#profitability",
        "source_context": {"filters": {"period": "current_fy"}},
        "metric_key": "gross_margin_pct",
        "baseline_value": 19.0,
        "target_value": 23.0,
    }
    created_id = None
    try:
        preview = service.assistant_action(payload, actor=actor)
        assert preview["status"] == "preview"
        assert preview["governance"]["mutation"] == "none"
        confirmed = service.assistant_action({**payload, "confirmed": True}, actor=actor)
        created_id = confirmed["action"]["id"]
        assert confirmed["action"]["status"] == "draft"
        assert confirmed["action"]["created_via"] == "assistant"
        assert confirmed["governance"]["approval_bypassed"] is False
    finally:
        _delete_work_items(created_id)


def test_crm_pipeline_has_weighted_forecast_and_governed_stage_sequence(app):
    actor = _actor(703)
    record = None
    try:
        record = service.create_operational_record({
            "domain": "crm",
            "record_type": "opportunity",
            "title": f"West expansion {uuid4().hex}",
            "status": "prospecting",
            "amount": 100000,
            "probability_pct": 30,
            "forecast_category": "pipeline",
            "next_step": "Discovery call",
            "account_ref": "ACCOUNT-7",
            "close_at": "2026-10-01T00:00:00+00:00",
        }, actor=actor)
        listing = service.list_operational_records("crm", page_size=100)
        row = next(item for item in listing["items"] if item["id"] == record["id"])
        assert row["weighted_amount"] == 30000
        assert listing["summary"]["pipeline_value"] >= 100000
        stage = next(item for item in listing["summary"]["stage_metrics"] if item["stage"] == "prospecting")
        assert stage["count"] >= 1
        assert stage["weighted"] >= 30000
        detail = service.get_operational_record(record["id"])
        assert detail["allowed_transitions"] == ["discovery", "lost"]
        with pytest.raises(service.DecisionOpsError, match="cannot move"):
            service.transition_operational_record(record["id"], "won", actor=actor)
        moved = service.transition_operational_record(record["id"], "discovery", actor=actor)
        assert moved["status"] == "discovery"
    finally:
        _delete_records(record and record["id"])


def test_record_intelligence_is_domain_specific_and_relationships_are_permission_scoped(
    client,
    app,
):
    actor = _actor(707)
    token = uuid4().hex
    crm = order = case = None
    try:
        crm = service.create_operational_record({
            "domain": "crm",
            "record_type": "opportunity",
            "title": f"Connected expansion {token}",
            "status": "proposal",
            "amount": 120000,
            "probability_pct": 45,
            "forecast_category": "best_case",
            "next_step": "Confirm the buying committee",
            "account_ref": f"ACCOUNT-{token}",
            "close_at": "2026-09-15T00:00:00+00:00",
            "source_system": "crm_source",
            "source_record_id": f"OPP-{token}",
            "source_url": "/customers/",
        }, actor=actor)
        order = service.create_operational_record({
            "domain": "orders",
            "record_type": "sales_order",
            "title": f"Connected order {token}",
            "status": "partially_shipped",
            "amount": 80000,
            "quantity": 100,
            "fulfilled_quantity": 65,
            "account_ref": f"ACCOUNT-{token}",
        }, actor=actor)
        case = service.create_operational_record({
            "domain": "service",
            "record_type": "case",
            "title": f"Connected case {token}",
            "status": "triaged",
            "account_ref": f"ACCOUNT-{token}",
        }, actor=actor)

        item = service.get_operational_record(crm["id"])
        brief = presentation.build_record_brief(
            item,
            now=datetime(2026, 8, 28, tzinfo=timezone.utc),
        )
        assert [metric["label"] for metric in brief["metrics"]] == [
            "Deal value",
            "Weighted value",
            "Probability",
            "Forecast",
            "Close date",
            "Next step",
        ]
        assert brief["metrics"][1]["value"] == "CAD 54,000"
        assert brief["timeline_title"] == "Revenue decision timeline"

        related = service.related_operational_records(
            crm["id"],
            allowed_domains={"orders"},
        )
        assert [record["id"] for record in related] == [order["id"]]
        assert related[0]["relationship"] == "Same account"

        app.config.update(LOGIN_DISABLED=True, AUTHZ_DISABLED=True, WTF_CSRF_ENABLED=False)
        body = client.get(f"/work/records/{crm['id']}").get_data(as_text=True)
        assert "Revenue decision timeline" in body
        assert "Create linked action" in body
        assert "Weighted value" in body
        assert "source_module=crm" in body
        assert "Quantity</span>" not in body
    finally:
        _delete_records(
            crm and crm["id"],
            order and order["id"],
            case and case["id"],
        )


def test_command_ledger_filters_attention_facets_and_search(app):
    actor = _actor(706)
    token = uuid4().hex
    action = record = None
    try:
        action = service.create_work_item({
            "title": f"Critical overdue margin action {token}",
            "source_module": "products",
            "source_record_id": f"SKU-{token}",
            "source_url": "/products/",
            "priority": "critical",
            "due_at": "2020-01-01T00:00:00+00:00",
            "expected_financial_impact": 50000,
        }, actor=actor)
        action_view = service.list_work_items(q=token, priority="critical", attention="overdue")
        assert [item["id"] for item in action_view["items"]] == [action["id"]]
        assert action_view["filters"]["attention"] == "overdue"
        assert action_view["summary"]["status_flow"]
        assert any(option["value"] == "products" for option in action_view["facets"]["sources"])
        action_detail = service.get_work_item(action["id"])
        assert action_detail["allowed_transitions"] == ["planned", "pending_approval", "cancelled"]

        record = service.create_operational_record({
            "domain": "orders",
            "record_type": "sales_order",
            "title": f"Held order exception {token}",
            "status": "held",
            "priority": "high",
            "amount": 25000,
            "due_at": "2020-01-02T00:00:00+00:00",
        }, actor=actor)
        record_view = service.list_operational_records("orders", q=token, attention="exceptions", priority="high")
        assert [item["id"] for item in record_view["items"]] == [record["id"]]
        assert record_view["items"][0]["is_overdue"] is True
        assert record_view["summary"]["exceptions"] >= 1
        assert any(item["status"] == "held" for item in record_view["summary"]["status_flow"])
    finally:
        _delete_work_items(action and action["id"])
        _delete_records(record and record["id"])


def test_approved_planning_scenario_creates_idempotent_draft_po(app):
    actor = _actor(704)
    scenario_id = po_id = None
    with SessionLocal() as session:
        scenario = PlanningScenario(name=f"PO writeback {uuid4().hex}", version=1, status="approved", assumptions_json="{}", results_json="{}", created_by_user_id=actor.id)
        session.add(scenario)
        session.flush()
        scenario_id = scenario.id
        session.add_all([
            PlanningReplenishmentRecommendation(scenario_id=scenario.id, product_id="SKU-PO-1", product_name="Planning item one", recommended_qty=10, estimated_cost=50, status="approved", approved_by_user_id=actor.id),
            PlanningReplenishmentRecommendation(scenario_id=scenario.id, product_id="SKU-PO-2", product_name="Planning item two", recommended_qty=5, estimated_cost=30, status="approved", approved_by_user_id=actor.id),
        ])
        session.commit()
    try:
        first = service.create_planning_draft_po(scenario_id, actor=actor)
        second = service.create_planning_draft_po(scenario_id, actor=actor)
        po_id = first["id"]
        assert second["id"] == first["id"]
        assert first["status"] == "draft_po"
        assert first["line_count"] == 2
        assert first["amount"] == 80
        detail = service.get_operational_record(po_id)
        assert detail is not None
        assert detail["metadata"]["supplier_required"] is True
        assert {line["item_ref"] for line in detail["lines"]} == {"SKU-PO-1", "SKU-PO-2"}
    finally:
        _delete_records(po_id)
        with SessionLocal() as session:
            session.query(PlanningReplenishmentRecommendation).filter_by(scenario_id=scenario_id).delete(synchronize_session=False)
            session.query(PlanningScenario).filter_by(id=scenario_id).delete(synchronize_session=False)
            session.commit()


def test_source_contracts_are_explicit_and_store_no_credentials(app):
    actor = _actor(705)
    key = "accounting_ledger"
    try:
        catalog = {row["contract_key"]: row for row in service.source_contracts()}
        assert catalog[key]["status"] == "not_connected"
        assert "general_ledger" in catalog[key]["capabilities"]
        updated = service.update_source_contract(key, {"status": "discovery", "system_name": "ERP evaluation", "owner": "Finance Systems", "base_url": "https://erp.example.test"}, actor=actor)
        assert updated["status"] == "discovery"
        assert "credentials" not in updated
        with pytest.raises(service.DecisionOpsError):
            service.update_source_contract(key, {"status": "verified", "base_url": "javascript:alert(1)"}, actor=actor)
    finally:
        with SessionLocal() as session:
            session.query(SourceContract).filter_by(contract_key=key).delete(synchronize_session=False)
            session.commit()


def test_decision_ops_pages_render_one_h1_and_source_gated_empty_states(client, app):
    app.config.update(LOGIN_DISABLED=True, AUTHZ_DISABLED=True, WTF_CSRF_ENABLED=False)
    for path, title in (
        ("/work", "Action Center"),
        ("/work/crm", "CRM Pipeline"),
        ("/work/orders", "Orders &amp; Fulfilment"),
        ("/work/procurement", "Procurement"),
        ("/work/finance", "Finance Operations"),
        ("/work/inventory", "Inventory Operations"),
        ("/work/master-data", "Master Data"),
        ("/work/service", "Customer Service"),
        ("/work/enterprise", "Enterprise Administration"),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        body = response.get_data(as_text=True)
        assert body.count("<h1") == 1, path
        assert title in body, path
        assert "navbar-expand-lg" in body
        if path != "/work/enterprise":
            assert "Command ledger" in body
            assert 'id="workspaceFilterForm"' in body
    assert "No operational history has been invented" in client.get("/work/orders").get_data(as_text=True)
    assert "not connected" in client.get("/work/enterprise").get_data(as_text=True).lower()
    action_body = client.get("/work").get_data(as_text=True)
    assert "Action execution flow" in action_body
    assert "Operational process flow" in client.get("/work/crm").get_data(as_text=True)
    assert 'name="reminder_at"' in action_body
    assert 'name="escalation_at"' in action_body
    assert '<option value="crm"' in action_body
    assert 'name="quantity"' in client.get("/work/orders").get_data(as_text=True)
    assert 'name="service_due_at"' in client.get("/work/service").get_data(as_text=True)


def test_decision_ops_json_routes_and_assistant_confirmation(client, app):
    app.config.update(LOGIN_DISABLED=True, AUTHZ_DISABLED=True, WTF_CSRF_ENABLED=False)
    action_id = record_id = None
    try:
        action_response = client.post("/work/actions", json={"title": f"Route action {uuid4().hex}", "source_module": "overview", "source_url": "/overview/"})
        assert action_response.status_code == 201
        action_id = action_response.get_json()["action"]["id"]

        record_response = client.post("/work/orders/records", json={"record_type": "sales_order", "title": f"Order {uuid4().hex}", "status": "draft", "amount": 250})
        assert record_response.status_code == 201
        record_id = record_response.get_json()["record"]["id"]

        assistant_payload = {"title": f"Governed route action {uuid4().hex}", "source_module": "products", "source_record_id": "SKU-2", "source_url": "/products/", "metric_key": "gross_margin_pct"}
        preview = client.post("/api/assistant/draft-action", json=assistant_payload)
        assert preview.status_code == 200
        assert preview.get_json()["requires_confirmation"] is True
        confirmed = client.post("/api/assistant/draft-action", json={**assistant_payload, "confirmed": True})
        assert confirmed.status_code == 201
        confirmed_id = confirmed.get_json()["action"]["id"]
        assert client.get("/api/work/actions?page_size=5").status_code == 200
        assert client.get("/api/work/orders/records?page_size=5").status_code == 200
        assert client.get("/api/work/source-contracts").status_code == 200
        _delete_work_items(confirmed_id)
    finally:
        _delete_work_items(action_id)
        _delete_records(record_id)


def test_decision_permissions_are_read_only_for_demo_and_role_scoped():
    demo = DEFAULT_ROLE_PERMISSION_KEYS["demo_viewer"]
    assert {"page.work.view", "page.crm.view", "page.orders.view", "page.procurement.view", "page.finance_ops.view"}.issubset(demo)
    assert "actions.create" not in demo
    assert "integrations.manage" not in demo
    assert "crm.manage" in DEFAULT_ROLE_PERMISSION_KEYS["sales"]
    assert "inventory_ops.manage" in DEFAULT_ROLE_PERMISSION_KEYS["warehouse"]
    assert "integrations.manage" in DEFAULT_ROLE_PERMISSION_KEYS["owner"]
    registry_keys = {item["key"] for group in permission_registry() for item in group["permissions"]}
    assert {"actions.manage", "assistant.actions.draft", "procurement.manage", "integrations.manage"}.issubset(registry_keys)
