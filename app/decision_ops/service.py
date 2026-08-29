"""Domain services for Northgate's shared decision and operations layer."""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import func, or_

from app.auth.models import SessionLocal
from app.planning_scenario_models import PlanningReplenishmentRecommendation, PlanningScenario

from .models import (
    ApprovalRecord,
    MasterDataChange,
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


class DecisionOpsError(ValueError):
    pass


PRIORITIES = ("low", "medium", "high", "critical")
ACTION_STATUSES = ("draft", "planned", "in_progress", "blocked", "pending_approval", "approved", "completed", "cancelled")
ACTION_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"planned", "pending_approval", "cancelled"},
    "planned": {"in_progress", "pending_approval", "blocked", "cancelled"},
    "in_progress": {"blocked", "pending_approval", "completed", "cancelled"},
    "blocked": {"in_progress", "cancelled"},
    "pending_approval": {"approved", "planned", "cancelled"},
    "approved": {"in_progress", "completed", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}

TERMINAL_RECORD_STATUSES = frozenset(
    {"won", "lost", "paid", "closed", "completed", "cancelled", "applied", "rejected", "resolved", "void"}
)
ATTENTION_STATES = frozenset({"overdue", "approval", "blocked", "exceptions", "unassigned", "critical", "open", "completed"})


WORKSPACES: dict[str, dict[str, Any]] = {
    "crm": {
        "label": "CRM Pipeline",
        "eyebrow": "Revenue work",
        "description": "Accounts, contacts, opportunities and activity-to-outcome execution.",
        "permission": "page.crm.view",
        "manage_permission": "crm.manage",
        "record_types": (
            "account", "contact", "lead", "opportunity", "activity", "task", "note",
            "quote", "contract", "renewal", "case", "consent",
        ),
        "statuses": ("new", "active", "inactive", "prospecting", "discovery", "proposal", "negotiation", "commit", "won", "lost"),
        "pipeline_stages": ("prospecting", "discovery", "proposal", "negotiation", "commit", "won", "lost"),
        "source_contracts": ("email_calendar", "billing_payments"),
    },
    "orders": {
        "label": "Orders & Fulfilment",
        "eyebrow": "Order to cash",
        "description": "Order exceptions from quote and credit check through fulfilment, invoice and settlement.",
        "permission": "page.orders.view",
        "manage_permission": "orders.manage",
        "record_types": ("quote", "sales_order", "shipment", "invoice", "payment", "credit_note", "hold"),
        "statuses": ("draft", "credit_check", "approved", "picking", "packed", "partially_shipped", "shipped", "invoiced", "paid", "closed", "held", "cancelled"),
        "source_contracts": ("warehouse_execution", "billing_payments"),
    },
    "procurement": {
        "label": "Procurement",
        "eyebrow": "Procure to pay",
        "description": "Requisitions, supplier decisions, draft POs, receipts and match exceptions.",
        "permission": "page.procurement.view",
        "manage_permission": "procurement.manage",
        "record_types": (
            "requisition", "rfq", "vendor_comparison", "purchase_order", "goods_receipt",
            "invoice_match", "supplier_contract", "payment_schedule", "commitment",
            "purchase_price_variance", "grni", "vendor_claim",
        ),
        "statuses": ("requisition", "draft_po", "pending_approval", "approved", "ordered", "partially_received", "received", "matched", "payment_scheduled", "closed", "exception", "cancelled"),
        "source_contracts": ("accounting_ledger", "warehouse_execution", "billing_payments"),
    },
    "finance": {
        "label": "Finance Operations",
        "eyebrow": "Record to report",
        "description": "Governed journal, reconciliation, close, budget and cash-forecast work with ledger drill-through.",
        "permission": "page.finance_ops.view",
        "manage_permission": "finance_ops.manage",
        "record_types": (
            "ledger_account", "journal_entry", "trial_balance", "ap_transaction", "ar_transaction",
            "bank_reconciliation", "fixed_asset", "budget", "cash_forecast", "close_task",
            "intercompany", "consolidation", "tax_control", "control_signoff",
        ),
        "statuses": ("draft", "pending_approval", "approved", "posted", "reconciled", "closed", "exception", "void"),
        "source_contracts": ("accounting_ledger", "billing_payments"),
    },
    "inventory": {
        "label": "Inventory Operations",
        "eyebrow": "Stock control",
        "description": "Inventory proposals, movement exceptions, approvals and accounting reconciliation.",
        "permission": "page.inventory_ops.view",
        "manage_permission": "inventory_ops.manage",
        "record_types": (
            "movement", "warehouse", "bin", "lot", "serial", "transfer", "receipt", "cycle_count",
            "adjustment", "reservation", "reorder_proposal", "putaway", "pick", "expiry",
            "available_to_promise", "valuation", "inventory_reconciliation",
        ),
        "statuses": ("proposed", "pending_approval", "approved", "in_progress", "completed", "exception", "cancelled"),
        "source_contracts": ("warehouse_execution", "accounting_ledger"),
    },
    "master-data": {
        "label": "Master Data",
        "eyebrow": "Governed records",
        "description": "Controlled changes and duplicate review across enterprise master records.",
        "permission": "page.master_data.view",
        "manage_permission": "master_data.manage",
        "record_types": (
            "customer", "account_hierarchy", "product", "sku_variant", "supplier", "warehouse",
            "location", "employee", "price_list", "unit_of_measure", "currency", "payment_terms",
            "delivery_terms", "fiscal_entity", "cost_center", "department", "duplicate_review",
        ),
        "statuses": ("draft", "pending_approval", "approved", "applied", "rejected"),
        "source_contracts": ("accounting_ledger", "warehouse_execution", "hris_payroll"),
    },
    "service": {
        "label": "Customer Service",
        "eyebrow": "Cases & SLA",
        "description": "Cases, routing, escalations, knowledge and service-outcome measurement.",
        "permission": "page.service.view",
        "manage_permission": "service.manage",
        "record_types": ("case", "interaction", "escalation", "knowledge_article", "survey_followup"),
        "statuses": ("new", "triaged", "in_progress", "pending_customer", "escalated", "resolved", "closed", "reopened"),
        "source_contracts": ("email_calendar", "returns", "marketing_automation"),
    },
    "enterprise": {
        "label": "Enterprise Administration",
        "eyebrow": "Connections & controls",
        "description": "Source contracts, connector readiness, APIs and governance without storing credentials.",
        "permission": "page.enterprise_admin.view",
        "manage_permission": "integrations.manage",
        "record_types": (),
        "statuses": (),
        "source_contracts": (
            "accounting_ledger", "billing_payments", "warehouse_execution", "email_calendar",
            "marketing_automation", "hris_payroll", "identity_sso_scim", "tenant_directory",
        ),
    },
}

DOMAIN_TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "crm": {
        "new": {"active", "inactive"}, "active": {"inactive"}, "inactive": {"active"},
        "prospecting": {"discovery", "lost"}, "discovery": {"proposal", "lost"},
        "proposal": {"negotiation", "lost"}, "negotiation": {"commit", "won", "lost"},
        "commit": {"won", "lost", "negotiation"}, "won": set(), "lost": {"prospecting"},
    },
    "orders": {
        "draft": {"credit_check", "held", "cancelled"}, "credit_check": {"approved", "held", "cancelled"},
        "approved": {"picking", "held", "cancelled"}, "picking": {"packed", "partially_shipped", "held", "cancelled"},
        "packed": {"partially_shipped", "shipped", "held"}, "partially_shipped": {"shipped", "held", "cancelled"},
        "shipped": {"invoiced", "held"}, "invoiced": {"paid", "held"}, "paid": {"closed"},
        "held": {"credit_check", "approved", "picking", "packed", "partially_shipped", "shipped", "invoiced", "cancelled"},
        "closed": set(), "cancelled": set(),
    },
    "procurement": {
        "requisition": {"draft_po", "cancelled"}, "draft_po": {"pending_approval", "cancelled"},
        "pending_approval": {"approved", "draft_po", "cancelled"}, "approved": {"ordered", "cancelled"},
        "ordered": {"partially_received", "received", "exception", "cancelled"},
        "partially_received": {"received", "exception", "cancelled"}, "received": {"matched", "exception"},
        "matched": {"payment_scheduled", "closed", "exception"}, "payment_scheduled": {"closed", "exception"},
        "exception": {"draft_po", "ordered", "received", "matched", "payment_scheduled", "cancelled"},
        "closed": set(), "cancelled": set(),
    },
    "finance": {
        "draft": {"pending_approval", "void"}, "pending_approval": {"approved", "draft", "void"},
        "approved": {"posted", "reconciled", "closed", "void"}, "posted": {"reconciled", "closed", "exception", "void"},
        "reconciled": {"closed", "exception"}, "exception": {"draft", "pending_approval", "posted", "reconciled", "void"},
        "closed": set(), "void": set(),
    },
    "inventory": {
        "proposed": {"pending_approval", "cancelled"}, "pending_approval": {"approved", "proposed", "cancelled"},
        "approved": {"in_progress", "cancelled"}, "in_progress": {"completed", "exception", "cancelled"},
        "exception": {"proposed", "approved", "in_progress", "cancelled"}, "completed": set(), "cancelled": set(),
    },
    "master-data": {
        "draft": {"pending_approval"}, "pending_approval": {"approved", "rejected", "draft"},
        "approved": {"applied", "rejected"}, "applied": set(), "rejected": {"draft"},
    },
    "service": {
        "new": {"triaged", "escalated"}, "triaged": {"in_progress", "escalated"},
        "in_progress": {"pending_customer", "escalated", "resolved"}, "pending_customer": {"in_progress", "escalated", "resolved"},
        "escalated": {"in_progress", "resolved"}, "resolved": {"closed", "reopened"},
        "closed": {"reopened"}, "reopened": {"triaged", "in_progress", "escalated"},
    },
}


SOURCE_CONTRACT_CATALOG: dict[str, dict[str, Any]] = {
    "accounting_ledger": {
        "display_name": "Accounting & general ledger",
        "category": "Finance",
        "expected_grain": "One row per posted journal line, account, entity, currency and effective timestamp.",
        "refresh_mode": "CDC or scheduled incremental",
        "capabilities": ["chart_of_accounts", "general_ledger", "tax", "consolidation", "multi_currency"],
    },
    "billing_payments": {
        "display_name": "Billing & payments",
        "category": "Order to cash",
        "expected_grain": "One row per invoice, payment allocation, credit note and settlement event.",
        "refresh_mode": "Webhook plus daily reconciliation",
        "capabilities": ["billing", "payments", "credit_notes", "ap_ar_ledgers"],
    },
    "warehouse_execution": {
        "display_name": "Warehouse execution",
        "category": "Supply chain",
        "expected_grain": "One row per inventory movement, bin/lot/serial allocation and fulfilment event.",
        "refresh_mode": "Event stream or frequent incremental",
        "capabilities": ["pick_pack_ship", "receiving", "bins_lots_serials", "cycle_counts", "atp"],
    },
    "email_calendar": {
        "display_name": "Enterprise email & calendar",
        "category": "CRM",
        "expected_grain": "One consented activity per message, meeting, attendee and account/contact match.",
        "refresh_mode": "Provider webhook",
        "capabilities": ["email_activity", "meetings", "sequence_enrollment", "reminders"],
    },
    "marketing_automation": {
        "display_name": "Marketing automation",
        "category": "CRM",
        "expected_grain": "One consented campaign membership, touch and response per contact.",
        "refresh_mode": "Scheduled incremental",
        "capabilities": ["campaign_membership", "consent", "lead_nurture"],
    },
    "hris_payroll": {
        "display_name": "HRIS & payroll",
        "category": "People",
        "expected_grain": "One effective-dated employee, department and payroll posting record.",
        "refresh_mode": "Daily secure batch",
        "capabilities": ["employee_master", "payroll", "cost_centers"],
    },
    "identity_sso_scim": {
        "display_name": "SSO & SCIM identity provider",
        "category": "Identity",
        "expected_grain": "One immutable subject identifier and effective group/role membership change.",
        "refresh_mode": "OIDC/SAML at sign-in plus SCIM webhook",
        "capabilities": ["sso", "scim", "group_mapping", "deprovisioning"],
    },
    "tenant_directory": {
        "display_name": "Tenant directory",
        "category": "Platform",
        "expected_grain": "One tenant, legal entity, environment and data-residency policy record.",
        "refresh_mode": "Administrative event",
        "capabilities": ["tenant_management", "entity_mapping", "data_residency"],
    },
    "returns": {
        "display_name": "Native Returns workflow",
        "category": "Service",
        "expected_grain": "One RMA and immutable lifecycle event, linked by return identifier.",
        "refresh_mode": "Native transaction",
        "capabilities": ["case_to_return", "refund_exception", "root_cause"],
        "native": True,
    },
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _actor_id(actor: Any) -> int | None:
    value = getattr(actor, "id", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _loads(value: str | None, default: Any) -> Any:
    try:
        parsed = json.loads(value or "")
        return parsed
    except (TypeError, ValueError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, sort_keys=True, default=str)


def _text(value: Any, *, limit: int, required: bool = False, label: str = "Value") -> str | None:
    token = " ".join(str(value or "").strip().split())
    if required and not token:
        raise DecisionOpsError(f"{label} is required.")
    if len(token) > limit:
        raise DecisionOpsError(f"{label} must be {limit} characters or fewer.")
    return token or None


def _float(value: Any, *, minimum: float | None = None, maximum: float | None = None) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise DecisionOpsError("Numeric value is invalid.") from exc
    if not math.isfinite(number):
        raise DecisionOpsError("Numeric value must be finite.")
    if minimum is not None and number < minimum:
        raise DecisionOpsError(f"Numeric value must be at least {minimum}.")
    if maximum is not None and number > maximum:
        raise DecisionOpsError(f"Numeric value must be no more than {maximum}.")
    return number


def _date(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise DecisionOpsError("Date/time must be ISO-8601.") from exc
    return parsed


def _safe_uri(value: Any, *, required: bool = False) -> str | None:
    token = _text(value, limit=800, required=required, label="URI")
    if not token:
        return None
    parsed = urlparse(token)
    if parsed.scheme and parsed.scheme.lower() not in {"http", "https"}:
        raise DecisionOpsError("Evidence links must use HTTPS/HTTP or an internal path.")
    if parsed.username or parsed.password:
        raise DecisionOpsError("Credentials must not be embedded in stored URLs.")
    if not parsed.scheme and not token.startswith("/"):
        raise DecisionOpsError("Internal evidence links must start with '/'.")
    return token


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _display_date(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.strftime("%b %d, %Y").replace(" 0", " ")


def _owner_labels(session: Any, rows: Iterable[Any]) -> dict[int, str]:
    """Resolve the owner ids on this page to names, in one query.

    The workspace table and both detail rails printed `owner_user_id` straight
    out of the column, so every owner on a CRM/ERP screen read as `4`. No
    template joins to users, and resolving inside `_work_dict`/`_record_dict`
    would issue one query per row - hence a single lookup over the page's ids.
    """
    from app.auth.models import User

    ids = {int(row.owner_user_id) for row in rows if getattr(row, "owner_user_id", None)}
    if not ids:
        return {}
    labels: dict[int, str] = {}
    for user in session.query(User).filter(User.id.in_(ids)).all():
        full = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
        labels[int(user.id)] = full or user.username
    return labels


def _apply_owner_labels(session: Any, rows: Iterable[Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = _owner_labels(session, rows)
    for item in items:
        owner = item.get("owner_user_id")
        item["owner_name"] = labels.get(int(owner)) if owner else None
    return items


def _naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=None) if value.tzinfo else value


def _record_deadline(item: OperationalRecord) -> datetime | None:
    return item.service_due_at or item.close_at or item.due_at


def _is_record_open(item: OperationalRecord) -> bool:
    return item.status not in TERMINAL_RECORD_STATUSES


def _counter_options(values: Iterable[str | None], order: Iterable[str] | None = None) -> list[dict[str, Any]]:
    counts = Counter(str(value) for value in values if value)
    keys = [key for key in (order or ()) if counts.get(key)]
    keys.extend(sorted(key for key in counts if key not in keys))
    return [{"value": key, "label": key.replace("_", " ").title(), "count": counts[key]} for key in keys]


def _owner_options(session: Any, rows: Iterable[Any]) -> list[dict[str, Any]]:
    records = list(rows)
    counts = Counter(int(row.owner_user_id) for row in records if getattr(row, "owner_user_id", None))
    labels = _owner_labels(session, records)
    return [
        {"value": owner_id, "label": labels.get(owner_id, f"User {owner_id}"), "count": count}
        for owner_id, count in sorted(counts.items(), key=lambda pair: (labels.get(pair[0], "").lower(), pair[0]))
    ]


def active_user_choices() -> list[dict[str, Any]]:
    """Return safe assignee labels for governed create forms.

    The old form asked people to type an internal numeric primary key. A
    modern workflow should expose a named assignee while still posting the
    stable id used by the ledger.
    """
    from app.auth.models import User

    with SessionLocal() as session:
        rows = (
            session.query(User)
            .filter(User.is_active.is_(True), User.is_approved.is_(True))
            .order_by(User.first_name.asc(), User.last_name.asc(), User.username.asc())
            .all()
        )
        choices = []
        for user in rows:
            full = " ".join(part for part in (user.first_name, user.last_name) if part).strip()
            choices.append({"value": int(user.id), "label": full or user.username, "username": user.username})
        return choices


def _work_dict(item: WorkItem) -> dict[str, Any]:
    now = _now().replace(tzinfo=None)
    due = item.due_at.replace(tzinfo=None) if item.due_at and item.due_at.tzinfo else item.due_at
    is_open = item.status not in {"completed", "cancelled"}
    return {
        "id": item.id,
        "title": item.title,
        "description": item.description,
        "source_module": item.source_module,
        "source_record_id": item.source_record_id,
        "source_url": item.source_url,
        "source_context": _loads(item.source_context_json, {}),
        "affected_records": _loads(item.affected_records_json, []),
        "metric_key": item.metric_key,
        "metric_label": item.metric_label,
        "baseline_value": item.baseline_value,
        "target_value": item.target_value,
        "outcome_value": item.outcome_value,
        "metric_unit": item.metric_unit,
        "expected_financial_impact": item.expected_financial_impact,
        "realized_financial_impact": item.realized_financial_impact,
        "currency": item.currency,
        "priority": item.priority,
        "status": item.status,
        "approval_status": item.approval_status,
        "approval_route": item.approval_route,
        "owner_user_id": item.owner_user_id,
        "created_via": item.created_via,
        "due_at": _iso(item.due_at),
        "reminder_at": _iso(item.reminder_at),
        "escalation_at": _iso(item.escalation_at),
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
        "due_display": _display_date(item.due_at),
        "reminder_display": _display_date(item.reminder_at),
        "escalation_display": _display_date(item.escalation_at),
        "updated_display": _display_date(item.updated_at),
        "is_overdue": bool(is_open and due and due < now),
    }


def _record_dict(item: OperationalRecord, line_count: int = 0) -> dict[str, Any]:
    deadline = _naive(_record_deadline(item))
    now = _now().replace(tzinfo=None)
    quantity = float(item.quantity or 0)
    fulfilled = float(item.fulfilled_quantity or 0)
    return {
        "id": item.id,
        "domain": item.domain,
        "record_type": item.record_type,
        "record_number": item.record_number,
        "title": item.title,
        "description": item.description,
        "status": item.status,
        "approval_status": item.approval_status,
        "priority": item.priority,
        "owner_user_id": item.owner_user_id,
        "account_ref": item.account_ref,
        "contact_ref": item.contact_ref,
        "product_ref": item.product_ref,
        "supplier_ref": item.supplier_ref,
        "location_ref": item.location_ref,
        "amount": item.amount,
        "currency": item.currency,
        "quantity": item.quantity,
        "fulfilled_quantity": item.fulfilled_quantity,
        "probability_pct": item.probability_pct,
        "weighted_amount": (item.amount or 0) * (item.probability_pct or 0) / 100 if item.record_type == "opportunity" else None,
        "stage": item.stage,
        "forecast_category": item.forecast_category,
        "next_step": item.next_step,
        "due_at": _iso(item.due_at),
        "close_at": _iso(item.close_at),
        "service_due_at": _iso(item.service_due_at),
        "source_system": item.source_system,
        "source_record_id": item.source_record_id,
        "source_url": item.source_url,
        "metadata": _loads(item.metadata_json, {}),
        "line_count": line_count,
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
        "deadline_display": _display_date(_record_deadline(item)),
        "updated_display": _display_date(item.updated_at),
        "is_overdue": bool(_is_record_open(item) and deadline and deadline < now),
        "fulfilment_pct": min(100.0, (fulfilled / quantity) * 100) if quantity > 0 else None,
    }


def create_work_item(payload: Mapping[str, Any], *, actor: Any, created_via: str = "manual") -> dict[str, Any]:
    priority = str(payload.get("priority") or "medium").strip().lower()
    if priority not in PRIORITIES:
        raise DecisionOpsError("Priority must be low, medium, high or critical.")
    status = str(payload.get("status") or "draft").strip().lower()
    if status not in ACTION_STATUSES:
        raise DecisionOpsError("Action status is invalid.")
    source_module = _text(payload.get("source_module"), limit=64, required=True, label="Source module")
    source_url = _safe_uri(payload.get("source_url"))
    actor_id = _actor_id(actor)
    now = _now()
    item = WorkItem(
        title=_text(payload.get("title"), limit=240, required=True, label="Title"),
        description=_text(payload.get("description"), limit=4000),
        source_module=source_module,
        source_record_id=_text(payload.get("source_record_id"), limit=180),
        source_url=source_url,
        source_context_json=_dumps(payload.get("source_context") or {}),
        affected_records_json=_dumps(payload.get("affected_records") or []),
        metric_key=_text(payload.get("metric_key"), limit=160),
        metric_label=_text(payload.get("metric_label"), limit=200),
        baseline_value=_float(payload.get("baseline_value")),
        target_value=_float(payload.get("target_value")),
        outcome_value=_float(payload.get("outcome_value")),
        metric_unit=_text(payload.get("metric_unit"), limit=32),
        expected_financial_impact=_float(payload.get("expected_financial_impact")),
        realized_financial_impact=_float(payload.get("realized_financial_impact")),
        currency=(_text(payload.get("currency") or "CAD", limit=8, required=True, label="Currency") or "CAD").upper(),
        priority=priority,
        status=status,
        approval_status="pending" if status == "pending_approval" else str(payload.get("approval_status") or "not_required"),
        approval_route=_text(payload.get("approval_route"), limit=120),
        owner_user_id=int(payload["owner_user_id"]) if payload.get("owner_user_id") not in (None, "") else None,
        created_by_user_id=actor_id,
        created_via=_text(created_via, limit=24, required=True) or "manual",
        due_at=_date(payload.get("due_at")),
        reminder_at=_date(payload.get("reminder_at")),
        escalation_at=_date(payload.get("escalation_at")),
        created_at=now,
        updated_at=now,
    )
    with SessionLocal() as session:
        session.add(item)
        session.flush()
        session.add(WorkItemEvent(work_item_id=item.id, event_type="created", to_status=item.status, actor_user_id=actor_id, payload_json=_dumps({"created_via": created_via})))
        if item.status == "pending_approval":
            session.add(ApprovalRecord(target_type="work_item", target_id=item.id, route=item.approval_route or "manager", requested_by_user_id=actor_id))
        session.commit()
        session.refresh(item)
        return _work_dict(item)


def list_work_items(
    *,
    page: int = 1,
    page_size: int = 25,
    status: str | None = None,
    owner_user_id: int | None = None,
    source_module: str | None = None,
    priority: str | None = None,
    attention: str | None = None,
    q: str | None = None,
    sort: str | None = None,
) -> dict[str, Any]:
    page = max(1, int(page or 1))
    page_size = min(100, max(1, int(page_size or 25)))
    status_token = str(status or "").strip().lower()
    source_token = str(source_module or "").strip().lower()
    priority_token = str(priority or "").strip().lower()
    attention_token = str(attention or "").strip().lower()
    query_token = " ".join(str(q or "").strip().split())[:160]
    sort_token = str(sort or "due").strip().lower()
    if priority_token not in PRIORITIES:
        priority_token = ""
    if attention_token not in ATTENTION_STATES:
        attention_token = ""
    if sort_token not in {"due", "updated", "value"}:
        sort_token = "due"
    with SessionLocal() as session:
        all_rows = session.query(WorkItem).all()
        query = session.query(WorkItem)
        if status_token:
            query = query.filter(WorkItem.status == status_token)
        if owner_user_id is not None:
            query = query.filter(WorkItem.owner_user_id == int(owner_user_id))
        if source_token:
            query = query.filter(WorkItem.source_module == source_token)
        if priority_token:
            query = query.filter(WorkItem.priority == priority_token)
        if query_token:
            pattern = f"%{query_token}%"
            query = query.filter(or_(
                WorkItem.title.ilike(pattern),
                WorkItem.description.ilike(pattern),
                WorkItem.source_record_id.ilike(pattern),
                WorkItem.metric_key.ilike(pattern),
            ))
        now = _now().replace(tzinfo=None)
        if attention_token == "overdue":
            query = query.filter(~WorkItem.status.in_(("completed", "cancelled")), WorkItem.due_at.is_not(None), WorkItem.due_at < now)
        elif attention_token == "approval":
            query = query.filter(WorkItem.status == "pending_approval")
        elif attention_token in {"blocked", "exceptions"}:
            query = query.filter(WorkItem.status == "blocked")
        elif attention_token == "unassigned":
            query = query.filter(~WorkItem.status.in_(("completed", "cancelled")), WorkItem.owner_user_id.is_(None))
        elif attention_token == "critical":
            query = query.filter(~WorkItem.status.in_(("completed", "cancelled")), WorkItem.priority == "critical")
        elif attention_token == "open":
            query = query.filter(~WorkItem.status.in_(("completed", "cancelled")))
        elif attention_token == "completed":
            query = query.filter(WorkItem.status == "completed")
        total = query.count()
        if sort_token == "value":
            order_by = (WorkItem.expected_financial_impact.desc(), WorkItem.updated_at.desc())
        elif sort_token == "updated":
            order_by = (WorkItem.updated_at.desc(), WorkItem.id.desc())
        else:
            order_by = (WorkItem.due_at.is_(None), WorkItem.due_at.asc(), WorkItem.updated_at.desc())
        rows = query.order_by(*order_by).offset((page - 1) * page_size).limit(page_size).all()
        all_open = [row for row in all_rows if row.status not in {"completed", "cancelled"}]
        overdue = sum(1 for row in all_open if _naive(row.due_at) and _naive(row.due_at) < now)
        due_soon_cutoff = now + timedelta(days=7)
        expected_total = sum(float(row.expected_financial_impact or 0) for row in all_rows if row.status != "cancelled")
        realized_total = sum(float(row.realized_financial_impact or 0) for row in all_rows)
        status_counts = Counter(row.status for row in all_rows)
        summary = {
            "open": len(all_open),
            "overdue": overdue,
            "pending_approval": sum(1 for row in all_open if row.status == "pending_approval"),
            "blocked": sum(1 for row in all_open if row.status == "blocked"),
            "expected_impact": sum(float(row.expected_financial_impact or 0) for row in all_open),
            "realized_impact": realized_total,
            "unassigned": sum(1 for row in all_open if not row.owner_user_id),
            "critical": sum(1 for row in all_open if row.priority == "critical"),
            "due_soon": sum(1 for row in all_open if _naive(row.due_at) and now <= _naive(row.due_at) <= due_soon_cutoff),
            "completion_rate": (status_counts.get("completed", 0) / len(all_rows)) if all_rows else None,
            "realization_rate": (realized_total / expected_total) if expected_total else None,
            "status_flow": [
                {
                    "status": state,
                    "count": status_counts.get(state, 0),
                    "impact": sum(float(row.expected_financial_impact or 0) for row in all_rows if row.status == state),
                }
                for state in ACTION_STATUSES
            ],
        }
        items = _apply_owner_labels(session, rows, [_work_dict(row) for row in rows])
        facets = {
            "statuses": _counter_options((row.status for row in all_rows), ACTION_STATUSES),
            "priorities": _counter_options((row.priority for row in all_rows), PRIORITIES),
            "sources": _counter_options(row.source_module for row in all_rows),
            "owners": _owner_options(session, all_rows),
        }
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": max(1, math.ceil(total / page_size)),
            "summary": summary,
            "facets": facets,
            "filters": {
                "status": status_token,
                "owner_user_id": owner_user_id,
                "source_module": source_token,
                "priority": priority_token,
                "attention": attention_token,
                "q": query_token,
                "sort": sort_token,
            },
        }


def get_work_item(work_item_id: int) -> dict[str, Any] | None:
    with SessionLocal() as session:
        item = session.get(WorkItem, int(work_item_id))
        if item is None:
            return None
        out = _work_dict(item)
        _apply_owner_labels(session, [item], [out])
        allowed = ACTION_TRANSITIONS.get(item.status, set())
        out["allowed_transitions"] = [state for state in ACTION_STATUSES if state in allowed]
        out["events"] = [{"id": row.id, "event_type": row.event_type, "from_status": row.from_status, "to_status": row.to_status, "actor_user_id": row.actor_user_id, "payload": _loads(row.payload_json, {}), "created_at": _iso(row.created_at)} for row in session.query(WorkItemEvent).filter_by(work_item_id=item.id).order_by(WorkItemEvent.created_at.asc(), WorkItemEvent.id.asc()).all()]
        out["comments"] = [{"id": row.id, "body": row.body, "author_user_id": row.author_user_id, "created_at": _iso(row.created_at)} for row in session.query(WorkItemComment).filter_by(work_item_id=item.id).order_by(WorkItemComment.created_at.asc()).all()]
        out["attachments"] = [{"id": row.id, "display_name": row.display_name, "uri": row.uri, "content_type": row.content_type, "checksum": row.checksum, "created_at": _iso(row.created_at)} for row in session.query(WorkItemAttachment).filter_by(work_item_id=item.id).order_by(WorkItemAttachment.created_at.asc()).all()]
        out["dependencies"] = [row.depends_on_work_item_id for row in session.query(WorkItemDependency).filter_by(work_item_id=item.id).all()]
        return out


def transition_work_item(work_item_id: int, target_status: str, *, actor: Any, notes: str | None = None, outcomes: Mapping[str, Any] | None = None) -> dict[str, Any]:
    target = str(target_status or "").strip().lower()
    actor_id = _actor_id(actor)
    with SessionLocal() as session:
        item = session.get(WorkItem, int(work_item_id))
        if item is None:
            raise DecisionOpsError("Action not found.")
        if target not in ACTION_TRANSITIONS.get(item.status, set()):
            raise DecisionOpsError(f"Action cannot move from {item.status} to {target}.")
        before = item.status
        item.status = target
        item.updated_at = _now()
        if outcomes:
            if "outcome_value" in outcomes:
                item.outcome_value = _float(outcomes.get("outcome_value"))
            if "realized_financial_impact" in outcomes:
                item.realized_financial_impact = _float(outcomes.get("realized_financial_impact"))
        if target == "pending_approval":
            item.approval_status = "pending"
            session.add(ApprovalRecord(target_type="work_item", target_id=item.id, route=item.approval_route or "manager", requested_by_user_id=actor_id, notes=_text(notes, limit=4000)))
        elif target == "approved":
            item.approval_status = "approved"
            item.approved_by_user_id = actor_id
            item.approved_at = _now()
            approval = session.query(ApprovalRecord).filter_by(target_type="work_item", target_id=item.id, status="pending").order_by(ApprovalRecord.id.desc()).first()
            if approval:
                approval.status = "approved"
                approval.decided_by_user_id = actor_id
                approval.decided_at = _now()
                approval.notes = _text(notes, limit=4000)
        elif target == "planned" and before == "pending_approval":
            item.approval_status = "changes_requested"
        elif target == "completed":
            item.completed_by_user_id = actor_id
            item.completed_at = _now()
        session.add(WorkItemEvent(work_item_id=item.id, event_type="status_changed", from_status=before, to_status=target, actor_user_id=actor_id, payload_json=_dumps({"notes": notes, "outcomes": outcomes or {}})))
        session.commit()
        session.refresh(item)
        return _work_dict(item)


def add_comment(work_item_id: int, body: str, *, actor: Any) -> dict[str, Any]:
    clean = _text(body, limit=4000, required=True, label="Comment")
    actor_id = _actor_id(actor)
    with SessionLocal() as session:
        if session.get(WorkItem, int(work_item_id)) is None:
            raise DecisionOpsError("Action not found.")
        row = WorkItemComment(work_item_id=int(work_item_id), body=clean, author_user_id=actor_id)
        session.add(row)
        session.add(WorkItemEvent(work_item_id=int(work_item_id), event_type="comment_added", actor_user_id=actor_id, payload_json="{}"))
        session.commit()
        return {"id": row.id, "body": row.body, "author_user_id": row.author_user_id, "created_at": _iso(row.created_at)}


def add_attachment(work_item_id: int, payload: Mapping[str, Any], *, actor: Any) -> dict[str, Any]:
    actor_id = _actor_id(actor)
    with SessionLocal() as session:
        if session.get(WorkItem, int(work_item_id)) is None:
            raise DecisionOpsError("Action not found.")
        row = WorkItemAttachment(
            work_item_id=int(work_item_id),
            display_name=_text(payload.get("display_name"), limit=240, required=True, label="Evidence name"),
            uri=_safe_uri(payload.get("uri"), required=True),
            content_type=_text(payload.get("content_type"), limit=120),
            checksum=_text(payload.get("checksum"), limit=128),
            added_by_user_id=actor_id,
        )
        session.add(row)
        session.add(WorkItemEvent(work_item_id=int(work_item_id), event_type="evidence_linked", actor_user_id=actor_id, payload_json=_dumps({"display_name": row.display_name, "uri": row.uri})))
        session.commit()
        return {"id": row.id, "display_name": row.display_name, "uri": row.uri}


def add_dependency(work_item_id: int, depends_on_work_item_id: int, *, actor: Any) -> dict[str, Any]:
    if int(work_item_id) == int(depends_on_work_item_id):
        raise DecisionOpsError("An action cannot depend on itself.")
    actor_id = _actor_id(actor)
    with SessionLocal() as session:
        if session.get(WorkItem, int(work_item_id)) is None or session.get(WorkItem, int(depends_on_work_item_id)) is None:
            raise DecisionOpsError("Action dependency target was not found.")
        existing = session.query(WorkItemDependency).filter_by(work_item_id=int(work_item_id), depends_on_work_item_id=int(depends_on_work_item_id)).first()
        if existing:
            return {"id": existing.id, "work_item_id": existing.work_item_id, "depends_on_work_item_id": existing.depends_on_work_item_id}
        row = WorkItemDependency(work_item_id=int(work_item_id), depends_on_work_item_id=int(depends_on_work_item_id), created_by_user_id=actor_id)
        session.add(row)
        session.add(WorkItemEvent(work_item_id=int(work_item_id), event_type="dependency_added", actor_user_id=actor_id, payload_json=_dumps({"depends_on_work_item_id": int(depends_on_work_item_id)})))
        session.commit()
        return {"id": row.id, "work_item_id": row.work_item_id, "depends_on_work_item_id": row.depends_on_work_item_id}


def create_operational_record(payload: Mapping[str, Any], *, actor: Any, expected_domain: str | None = None) -> dict[str, Any]:
    domain = str(expected_domain or payload.get("domain") or "").strip().lower()
    if domain not in WORKSPACES or domain == "enterprise":
        raise DecisionOpsError("Operational domain is invalid.")
    config = WORKSPACES[domain]
    record_type = str(payload.get("record_type") or "").strip().lower()
    if record_type not in config["record_types"]:
        raise DecisionOpsError(f"Record type is invalid for {config['label']}.")
    default_status = "prospecting" if domain == "crm" and record_type == "opportunity" else config["statuses"][0]
    status = str(payload.get("status") or default_status).strip().lower()
    if status not in config["statuses"]:
        raise DecisionOpsError(f"Status is invalid for {config['label']}.")
    priority = str(payload.get("priority") or "medium").strip().lower()
    if priority not in PRIORITIES:
        raise DecisionOpsError("Priority must be low, medium, high or critical.")
    probability = _float(payload.get("probability_pct"), minimum=0, maximum=100)
    actor_id = _actor_id(actor)
    now = _now()
    prefix = {"crm": "CRM", "orders": "OTC", "procurement": "P2P", "finance": "FIN", "inventory": "INV", "master-data": "MDM", "service": "CASE"}[domain]
    source_system = _text(payload.get("source_system"), limit=80)
    source_record_id = _text(payload.get("source_record_id"), limit=180)
    record = OperationalRecord(
        domain=domain,
        record_type=record_type,
        record_number=_text(payload.get("record_number"), limit=80) or f"{prefix}-{now:%y%m%d}-{uuid4().hex[:8].upper()}",
        title=_text(payload.get("title"), limit=240, required=True, label="Title"),
        description=_text(payload.get("description"), limit=4000),
        status=status,
        approval_status="pending" if status == "pending_approval" else str(payload.get("approval_status") or "not_required"),
        priority=priority,
        owner_user_id=int(payload["owner_user_id"]) if payload.get("owner_user_id") not in (None, "") else None,
        account_ref=_text(payload.get("account_ref"), limit=160),
        contact_ref=_text(payload.get("contact_ref"), limit=160),
        product_ref=_text(payload.get("product_ref"), limit=160),
        supplier_ref=_text(payload.get("supplier_ref"), limit=160),
        location_ref=_text(payload.get("location_ref"), limit=160),
        amount=_float(payload.get("amount")),
        currency=(_text(payload.get("currency") or "CAD", limit=8, required=True, label="Currency") or "CAD").upper(),
        quantity=_float(payload.get("quantity"), minimum=0),
        fulfilled_quantity=_float(payload.get("fulfilled_quantity"), minimum=0),
        probability_pct=probability,
        stage=_text(payload.get("stage"), limit=64),
        forecast_category=_text(payload.get("forecast_category"), limit=32),
        next_step=_text(payload.get("next_step"), limit=240),
        due_at=_date(payload.get("due_at")),
        close_at=_date(payload.get("close_at")),
        service_started_at=_date(payload.get("service_started_at")),
        service_due_at=_date(payload.get("service_due_at")),
        parent_id=int(payload["parent_id"]) if payload.get("parent_id") not in (None, "") else None,
        related_work_item_id=int(payload["related_work_item_id"]) if payload.get("related_work_item_id") not in (None, "") else None,
        source_system=source_system,
        source_record_id=source_record_id,
        source_url=_safe_uri(payload.get("source_url")),
        metadata_json=_dumps(payload.get("metadata") or {}),
        created_by_user_id=actor_id,
        updated_by_user_id=actor_id,
        created_at=now,
        updated_at=now,
    )
    with SessionLocal() as session:
        if source_system and source_record_id:
            existing = session.query(OperationalRecord).filter_by(domain=domain, record_type=record_type, source_system=source_system, source_record_id=source_record_id).first()
            if existing:
                return _record_dict(existing, session.query(OperationalRecordLine).filter_by(operational_record_id=existing.id).count())
        session.add(record)
        session.flush()
        lines = payload.get("lines") or []
        if not isinstance(lines, list):
            raise DecisionOpsError("Lines must be a list.")
        for index, raw in enumerate(lines, start=1):
            if not isinstance(raw, Mapping):
                raise DecisionOpsError("Each line must be an object.")
            session.add(OperationalRecordLine(
                operational_record_id=record.id,
                line_number=index,
                item_ref=_text(raw.get("item_ref"), limit=160),
                description=_text(raw.get("description"), limit=300),
                quantity=_float(raw.get("quantity"), minimum=0) or 0,
                fulfilled_quantity=_float(raw.get("fulfilled_quantity"), minimum=0) or 0,
                unit_price=_float(raw.get("unit_price")),
                amount=_float(raw.get("amount")),
                metadata_json=_dumps(raw.get("metadata") or {}),
            ))
        session.add(OperationalRecordEvent(operational_record_id=record.id, event_type="created", to_status=record.status, actor_user_id=actor_id, payload_json=_dumps({"source_system": source_system})))
        if status == "pending_approval":
            session.add(ApprovalRecord(target_type="operational_record", target_id=record.id, route=str(payload.get("approval_route") or "manager"), requested_by_user_id=actor_id))
        session.commit()
        session.refresh(record)
        return _record_dict(record, len(lines))


def list_operational_records(
    domain: str,
    *,
    page: int = 1,
    page_size: int = 25,
    status: str | None = None,
    record_type: str | None = None,
    owner_user_id: int | None = None,
    priority: str | None = None,
    attention: str | None = None,
    q: str | None = None,
    sort: str | None = None,
) -> dict[str, Any]:
    domain = str(domain or "").strip().lower()
    if domain not in WORKSPACES or domain == "enterprise":
        raise DecisionOpsError("Operational domain is invalid.")
    page = max(1, int(page or 1))
    page_size = min(100, max(1, int(page_size or 25)))
    status_token = str(status or "").strip().lower()
    type_token = str(record_type or "").strip().lower()
    priority_token = str(priority or "").strip().lower()
    attention_token = str(attention or "").strip().lower()
    query_token = " ".join(str(q or "").strip().split())[:160]
    sort_token = str(sort or "updated").strip().lower()
    if priority_token not in PRIORITIES:
        priority_token = ""
    if attention_token not in ATTENTION_STATES:
        attention_token = ""
    if sort_token not in {"updated", "due", "value"}:
        sort_token = "updated"
    with SessionLocal() as session:
        all_rows = session.query(OperationalRecord).filter_by(domain=domain).all()
        query = session.query(OperationalRecord).filter_by(domain=domain)
        if status_token:
            query = query.filter(OperationalRecord.status == status_token)
        if type_token:
            query = query.filter(OperationalRecord.record_type == type_token)
        if owner_user_id is not None:
            query = query.filter(OperationalRecord.owner_user_id == int(owner_user_id))
        if priority_token:
            query = query.filter(OperationalRecord.priority == priority_token)
        if query_token:
            pattern = f"%{query_token}%"
            query = query.filter(or_(
                OperationalRecord.title.ilike(pattern),
                OperationalRecord.description.ilike(pattern),
                OperationalRecord.record_number.ilike(pattern),
                OperationalRecord.account_ref.ilike(pattern),
                OperationalRecord.supplier_ref.ilike(pattern),
                OperationalRecord.product_ref.ilike(pattern),
                OperationalRecord.source_record_id.ilike(pattern),
            ))
        now = _now().replace(tzinfo=None)
        open_statuses = tuple(state for state in WORKSPACES[domain]["statuses"] if state not in TERMINAL_RECORD_STATUSES)
        if attention_token == "overdue":
            query = query.filter(
                OperationalRecord.status.in_(open_statuses),
                or_(
                    OperationalRecord.service_due_at < now,
                    OperationalRecord.close_at < now,
                    OperationalRecord.due_at < now,
                ),
            )
        elif attention_token in {"approval"}:
            query = query.filter(OperationalRecord.status == "pending_approval")
        elif attention_token in {"exceptions", "blocked"}:
            query = query.filter(OperationalRecord.status.in_(("exception", "held", "escalated")))
        elif attention_token == "unassigned":
            query = query.filter(OperationalRecord.status.in_(open_statuses), OperationalRecord.owner_user_id.is_(None))
        elif attention_token == "critical":
            query = query.filter(OperationalRecord.status.in_(open_statuses), OperationalRecord.priority == "critical")
        elif attention_token == "open":
            query = query.filter(OperationalRecord.status.in_(open_statuses))
        elif attention_token == "completed":
            query = query.filter(OperationalRecord.status.in_(tuple(TERMINAL_RECORD_STATUSES)))
        total = query.count()
        if sort_token == "value":
            order_by = (OperationalRecord.amount.desc(), OperationalRecord.updated_at.desc())
        elif sort_token == "due":
            deadline = func.coalesce(OperationalRecord.service_due_at, OperationalRecord.close_at, OperationalRecord.due_at)
            order_by = (deadline.is_(None), deadline.asc(), OperationalRecord.updated_at.desc())
        else:
            order_by = (OperationalRecord.updated_at.desc(), OperationalRecord.id.desc())
        rows = query.order_by(*order_by).offset((page - 1) * page_size).limit(page_size).all()
        ids = [row.id for row in rows]
        line_counts = dict(session.query(OperationalRecordLine.operational_record_id, func.count(OperationalRecordLine.id)).filter(OperationalRecordLine.operational_record_id.in_(ids)).group_by(OperationalRecordLine.operational_record_id).all()) if ids else {}
        summary = _operational_summary(domain, all_rows)
        items = _apply_owner_labels(session, rows, [_record_dict(row, int(line_counts.get(row.id, 0))) for row in rows])
        facets = {
            "statuses": _counter_options((row.status for row in all_rows), WORKSPACES[domain]["statuses"]),
            "record_types": _counter_options((row.record_type for row in all_rows), WORKSPACES[domain]["record_types"]),
            "priorities": _counter_options((row.priority for row in all_rows), PRIORITIES),
            "owners": _owner_options(session, all_rows),
        }
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": max(1, math.ceil(total / page_size)),
            "summary": summary,
            "facets": facets,
            "filters": {
                "status": status_token,
                "record_type": type_token,
                "owner_user_id": owner_user_id,
                "priority": priority_token,
                "attention": attention_token,
                "q": query_token,
                "sort": sort_token,
            },
        }


def get_operational_record(record_id: int) -> dict[str, Any] | None:
    with SessionLocal() as session:
        record = session.get(OperationalRecord, int(record_id))
        if record is None:
            return None
        lines = session.query(OperationalRecordLine).filter_by(operational_record_id=record.id).order_by(OperationalRecordLine.line_number.asc()).all()
        out = _record_dict(record, len(lines))
        _apply_owner_labels(session, [record], [out])
        allowed = DOMAIN_TRANSITIONS.get(record.domain, {}).get(record.status, set())
        out["allowed_transitions"] = [state for state in WORKSPACES[record.domain]["statuses"] if state in allowed]
        out["lines"] = [{
            "id": row.id,
            "line_number": row.line_number,
            "item_ref": row.item_ref,
            "description": row.description,
            "quantity": row.quantity,
            "fulfilled_quantity": row.fulfilled_quantity,
            "unit_price": row.unit_price,
            "amount": row.amount,
            "metadata": _loads(row.metadata_json, {}),
        } for row in lines]
        out["events"] = [{
            "id": row.id,
            "event_type": row.event_type,
            "from_status": row.from_status,
            "to_status": row.to_status,
            "actor_user_id": row.actor_user_id,
            "payload": _loads(row.payload_json, {}),
            "created_at": _iso(row.created_at),
        } for row in session.query(OperationalRecordEvent).filter_by(operational_record_id=record.id).order_by(OperationalRecordEvent.created_at.asc(), OperationalRecordEvent.id.asc()).all()]
        return out


def related_operational_records(
    record_id: int,
    *,
    allowed_domains: Iterable[str],
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Return permission-scoped records connected by governed master keys.

    Cross-workspace context is the useful part of a unified CRM/ERP control
    plane, but it must never punch through RBAC.  The route supplies only the
    domains the current user can view; this service then connects records by
    exact account, supplier, product, location, or parent identity.
    """

    domains = {
        str(domain).strip().lower()
        for domain in allowed_domains
        if str(domain).strip().lower() in WORKSPACES
        and str(domain).strip().lower() != "enterprise"
    }
    if not domains:
        return []
    with SessionLocal() as session:
        record = session.get(OperationalRecord, int(record_id))
        if record is None:
            return []
        relationships = []
        for column, value in (
            (OperationalRecord.account_ref, record.account_ref),
            (OperationalRecord.supplier_ref, record.supplier_ref),
            (OperationalRecord.product_ref, record.product_ref),
            (OperationalRecord.location_ref, record.location_ref),
        ):
            if value:
                relationships.append(column == value)
        relationships.append(OperationalRecord.parent_id == record.id)
        if record.parent_id:
            relationships.append(OperationalRecord.id == record.parent_id)
        rows = (
            session.query(OperationalRecord)
            .filter(
                OperationalRecord.id != record.id,
                OperationalRecord.domain.in_(domains),
                or_(*relationships),
            )
            .order_by(OperationalRecord.updated_at.desc(), OperationalRecord.id.desc())
            .limit(min(20, max(1, int(limit))))
            .all()
        )
        line_counts = dict(
            session.query(
                OperationalRecordLine.operational_record_id,
                func.count(OperationalRecordLine.id),
            )
            .filter(OperationalRecordLine.operational_record_id.in_([row.id for row in rows]))
            .group_by(OperationalRecordLine.operational_record_id)
            .all()
        ) if rows else {}
        items = _apply_owner_labels(
            session,
            rows,
            [_record_dict(row, int(line_counts.get(row.id, 0))) for row in rows],
        )
        for row, item in zip(rows, items, strict=False):
            if record.account_ref and row.account_ref == record.account_ref:
                relation = "Same account"
            elif record.supplier_ref and row.supplier_ref == record.supplier_ref:
                relation = "Same supplier"
            elif record.product_ref and row.product_ref == record.product_ref:
                relation = "Same product"
            elif record.location_ref and row.location_ref == record.location_ref:
                relation = "Same location"
            elif row.parent_id == record.id:
                relation = "Child record"
            else:
                relation = "Parent record"
            item["relationship"] = relation
            item["workspace_label"] = WORKSPACES[row.domain]["label"]
        return items


def _operational_summary(domain: str, rows: Iterable[OperationalRecord]) -> dict[str, Any]:
    records = list(rows)
    statuses = Counter(row.status for row in records)
    now = _now().replace(tzinfo=None)
    due_soon_cutoff = now + timedelta(days=7)
    open_rows_common = [row for row in records if _is_record_open(row)]
    summary: dict[str, Any] = {
        "total": len(records),
        "exceptions": statuses.get("exception", 0) + statuses.get("held", 0) + statuses.get("escalated", 0),
        "pending_approval": statuses.get("pending_approval", 0),
        "unassigned": sum(1 for row in open_rows_common if not row.owner_user_id),
        "critical": sum(1 for row in open_rows_common if row.priority == "critical"),
        "overdue": sum(1 for row in open_rows_common if _naive(_record_deadline(row)) and _naive(_record_deadline(row)) < now),
        "due_soon": sum(
            1
            for row in open_rows_common
            if _naive(_record_deadline(row)) and now <= _naive(_record_deadline(row)) <= due_soon_cutoff
        ),
        "open_value": sum(float(row.amount or 0) for row in open_rows_common),
        "status_flow": [
            {
                "status": state,
                "count": statuses.get(state, 0),
                "value": sum(float(row.amount or 0) for row in records if row.status == state),
            }
            for state in WORKSPACES[domain]["statuses"]
        ],
    }
    if domain == "crm":
        opportunities = [row for row in records if row.record_type == "opportunity"]
        open_rows = [row for row in opportunities if row.status not in {"won", "lost"}]
        won = sum(1 for row in opportunities if row.status == "won")
        lost = sum(1 for row in opportunities if row.status == "lost")
        stage_metrics = [
            {
                "stage": stage,
                "count": sum(1 for row in opportunities if row.status == stage),
                "value": sum(
                    float(row.amount or 0) for row in opportunities if row.status == stage
                ),
                "weighted": sum(
                    float(row.amount or 0) * float(row.probability_pct or 0) / 100
                    for row in opportunities
                    if row.status == stage
                ),
                "stalled": sum(
                    1
                    for row in opportunities
                    if row.status == stage and not row.next_step
                ),
                "cards": [
                    {
                        "id": row.id,
                        "title": row.title,
                        "currency": row.currency,
                        "amount": row.amount,
                        "probability_pct": row.probability_pct,
                        "next_step": row.next_step,
                        "close_at": _iso(row.close_at),
                    }
                    for row in sorted(
                        (item for item in opportunities if item.status == stage),
                        key=lambda item: float(item.amount or 0),
                        reverse=True,
                    )[:3]
                ],
            }
            for stage in WORKSPACES[domain]["pipeline_stages"]
        ]
        summary.update({
            "pipeline_value": sum(float(row.amount or 0) for row in open_rows),
            "weighted_pipeline": sum(float(row.amount or 0) * float(row.probability_pct or 0) / 100 for row in open_rows),
            "commit_value": sum(float(row.amount or 0) for row in open_rows if row.forecast_category == "commit"),
            "win_rate": (won / (won + lost)) if won + lost else None,
            "stalled": sum(1 for row in open_rows if not row.next_step),
            "next_step_completeness": (sum(1 for row in open_rows if row.next_step) / len(open_rows)) if open_rows else None,
            "new_logo_pipeline": sum(float(row.amount or 0) for row in open_rows if _loads(row.metadata_json, {}).get("motion") == "new_logo"),
            "expansion_pipeline": sum(float(row.amount or 0) for row in open_rows if _loads(row.metadata_json, {}).get("motion") == "expansion"),
            "average_deal_size": (sum(float(row.amount or 0) for row in opportunities if row.status == "won") / won) if won else None,
            "at_risk_pipeline": sum(
                float(row.amount or 0)
                for row in open_rows
                if not row.next_step or (_naive(row.close_at) and _naive(row.close_at) < now)
            ),
            "stage_metrics": stage_metrics,
            "status_flow": [
                {
                    "status": stage["stage"],
                    "count": stage["count"],
                    "value": stage["value"],
                }
                for stage in stage_metrics
            ],
            "forecast_mix": [
                {
                    "category": category,
                    "count": sum(1 for row in open_rows if (row.forecast_category or "pipeline") == category),
                    "value": sum(float(row.amount or 0) for row in open_rows if (row.forecast_category or "pipeline") == category),
                }
                for category in ("pipeline", "best_case", "commit")
            ],
        })
    elif domain == "orders":
        orders = [row for row in records if row.record_type == "sales_order"]
        summary.update({
            "backlog_value": sum(float(row.amount or 0) for row in orders if row.status not in {"paid", "closed", "cancelled"}),
            "holds": sum(1 for row in records if row.status == "held" or row.record_type == "hold"),
            "partial_shipments": sum(1 for row in orders if row.status == "partially_shipped"),
            "backorders": sum(1 for row in orders if row.quantity is not None and float(row.fulfilled_quantity or 0) < float(row.quantity or 0) and row.status not in {"draft", "cancelled", "closed"}),
            "perfect_order_rate": (
                sum(1 for row in orders if all(_loads(row.metadata_json, {}).get(flag) is True for flag in ("on_time", "complete", "damage_free", "invoice_accurate"))) / len([row for row in orders if row.status == "closed"])
            ) if any(row.status == "closed" for row in orders) else None,
        })
    elif domain == "procurement":
        summary.update({
            "open_commitments": sum(float(row.amount or 0) for row in records if row.record_type in {"purchase_order", "commitment"} and row.status not in {"closed", "cancelled"}),
            "match_exceptions": sum(1 for row in records if row.record_type == "invoice_match" and row.status == "exception"),
            "purchase_price_variance": sum(float(row.amount or 0) for row in records if row.record_type == "purchase_price_variance"),
            "grni": sum(float(row.amount or 0) for row in records if row.record_type == "grni" and row.status not in {"closed", "cancelled"}),
        })
    elif domain == "finance":
        summary.update({
            "open_close_tasks": sum(1 for row in records if row.record_type == "close_task" and row.status not in {"closed", "void"}),
            "unreconciled": sum(1 for row in records if row.record_type == "bank_reconciliation" and row.status not in {"reconciled", "closed", "void"}),
            "cash_forecast": sum(float(row.amount or 0) for row in records if row.record_type == "cash_forecast"),
            "budget": sum(float(row.amount or 0) for row in records if row.record_type == "budget"),
            "posted_journals": sum(1 for row in records if row.record_type == "journal_entry" and row.status == "posted"),
        })
    elif domain == "inventory":
        summary.update({"open_proposals": sum(1 for row in records if row.record_type == "reorder_proposal" and row.status not in {"completed", "cancelled"}), "adjustments_pending": sum(1 for row in records if row.record_type == "adjustment" and row.status == "pending_approval")})
    elif domain == "service":
        cases = [row for row in records if row.record_type == "case"]
        summary.update({"open_cases": sum(1 for row in cases if row.status not in {"resolved", "closed"}), "sla_at_risk": sum(1 for row in cases if row.service_due_at and (row.service_due_at.replace(tzinfo=None) if row.service_due_at.tzinfo else row.service_due_at) < now and row.status not in {"resolved", "closed"}), "reopened": sum(1 for row in cases if row.status == "reopened")})
        scores = [float(_loads(row.metadata_json, {}).get("csat")) for row in cases if _loads(row.metadata_json, {}).get("csat") is not None]
        summary["csat"] = (sum(scores) / len(scores)) if scores else None
    return summary


def transition_operational_record(record_id: int, target_status: str, *, actor: Any, notes: str | None = None) -> dict[str, Any]:
    target = str(target_status or "").strip().lower()
    actor_id = _actor_id(actor)
    with SessionLocal() as session:
        record = session.get(OperationalRecord, int(record_id))
        if record is None:
            raise DecisionOpsError("Operational record not found.")
        allowed = WORKSPACES[record.domain]["statuses"]
        if target not in allowed:
            raise DecisionOpsError(f"Status is invalid for {WORKSPACES[record.domain]['label']}.")
        before = record.status
        if before == target:
            return _record_dict(record, session.query(OperationalRecordLine).filter_by(operational_record_id=record.id).count())
        if target not in DOMAIN_TRANSITIONS.get(record.domain, {}).get(before, set()):
            raise DecisionOpsError(f"Record cannot move from {before} to {target}; follow the governed {WORKSPACES[record.domain]['label']} lifecycle.")
        record.status = target
        record.updated_by_user_id = actor_id
        record.updated_at = _now()
        if target == "pending_approval":
            record.approval_status = "pending"
            session.add(ApprovalRecord(target_type="operational_record", target_id=record.id, route="manager", requested_by_user_id=actor_id, notes=_text(notes, limit=4000)))
        elif target == "approved":
            record.approval_status = "approved"
            approval = session.query(ApprovalRecord).filter_by(target_type="operational_record", target_id=record.id, status="pending").order_by(ApprovalRecord.id.desc()).first()
            if approval:
                approval.status = "approved"
                approval.decided_by_user_id = actor_id
                approval.decided_at = _now()
                approval.notes = _text(notes, limit=4000)
        session.add(OperationalRecordEvent(operational_record_id=record.id, event_type="status_changed", from_status=before, to_status=target, actor_user_id=actor_id, payload_json=_dumps({"notes": notes})))
        session.commit()
        return _record_dict(record, session.query(OperationalRecordLine).filter_by(operational_record_id=record.id).count())


def create_planning_draft_po(scenario_id: int, *, actor: Any) -> dict[str, Any]:
    source_id = f"scenario:{int(scenario_id)}"
    with SessionLocal() as session:
        scenario = session.get(PlanningScenario, int(scenario_id))
        if scenario is None:
            raise DecisionOpsError("Planning scenario not found.")
        if scenario.status != "approved":
            raise DecisionOpsError("Only an approved planning scenario can create a draft purchase order.")
        recommendations = session.query(PlanningReplenishmentRecommendation).filter_by(scenario_id=scenario.id, status="approved").order_by(PlanningReplenishmentRecommendation.id.asc()).all()
        if not recommendations:
            raise DecisionOpsError("The approved scenario has no replenishment recommendations.")
    payload = {
        "domain": "procurement",
        "record_type": "purchase_order",
        "title": f"Planning write-back — {scenario.name} v{scenario.version}",
        "description": "Draft purchase order created from approved, measured replenishment recommendations. Supplier allocation and commercial terms require procurement review.",
        "status": "draft_po",
        "amount": sum(float(row.estimated_cost or 0) for row in recommendations),
        "quantity": sum(float(row.recommended_qty or 0) for row in recommendations),
        "source_system": "planning",
        "source_record_id": source_id,
        "source_url": "/planning/#planningScenarios",
        "metadata": {"scenario_id": scenario.id, "scenario_version": scenario.version, "supplier_required": True, "commercial_terms_required": True},
        "lines": [{"item_ref": row.product_id, "description": row.product_name or row.rationale, "quantity": row.recommended_qty, "amount": row.estimated_cost, "metadata": {"recommendation_id": row.id, "rationale": row.rationale}} for row in recommendations],
    }
    return create_operational_record(payload, actor=actor, expected_domain="procurement")


def source_contracts(keys: Iterable[str] | None = None) -> list[dict[str, Any]]:
    selected = list(keys or SOURCE_CONTRACT_CATALOG.keys())
    with SessionLocal() as session:
        persisted = {row.contract_key: row for row in session.query(SourceContract).filter(SourceContract.contract_key.in_(selected)).all()}
        output = []
        for key in selected:
            definition = SOURCE_CONTRACT_CATALOG.get(key)
            if not definition:
                continue
            row = persisted.get(key)
            native = bool(definition.get("native"))
            output.append({
                "contract_key": key,
                "display_name": definition["display_name"],
                "category": definition["category"],
                "expected_grain": definition["expected_grain"],
                "refresh_mode": definition["refresh_mode"],
                "capabilities": definition["capabilities"],
                "native": native,
                "status": "native" if native else (row.status if row else "not_connected"),
                "system_name": row.system_name if row else None,
                "owner": row.owner if row else None,
                "base_url": row.base_url if row else None,
                "last_verified_at": _iso(row.last_verified_at) if row else None,
            })
        return output


def update_source_contract(contract_key: str, payload: Mapping[str, Any], *, actor: Any) -> dict[str, Any]:
    key = str(contract_key or "").strip().lower()
    definition = SOURCE_CONTRACT_CATALOG.get(key)
    if not definition or definition.get("native"):
        raise DecisionOpsError("Source contract is invalid or native-managed.")
    status = str(payload.get("status") or "not_connected").strip().lower()
    if status not in {"not_connected", "discovery", "configured", "verified", "degraded", "disabled"}:
        raise DecisionOpsError("Source status is invalid.")
    actor_id = _actor_id(actor)
    with SessionLocal() as session:
        row = session.query(SourceContract).filter_by(contract_key=key).first()
        if row is None:
            row = SourceContract(contract_key=key, display_name=definition["display_name"], category=definition["category"], expected_grain=definition["expected_grain"], refresh_mode=definition["refresh_mode"], capabilities_json=_dumps(definition["capabilities"]))
            session.add(row)
        row.status = status
        row.system_name = _text(payload.get("system_name"), limit=120)
        row.owner = _text(payload.get("owner"), limit=160)
        row.base_url = _safe_uri(payload.get("base_url"))
        row.last_verified_at = _now() if status == "verified" else row.last_verified_at
        row.updated_by_user_id = actor_id
        row.updated_at = _now()
        session.commit()
    return next(item for item in source_contracts([key]) if item["contract_key"] == key)


def create_master_data_change(payload: Mapping[str, Any], *, actor: Any) -> dict[str, Any]:
    entity_type = str(payload.get("entity_type") or "").strip().lower()
    allowed = set(WORKSPACES["master-data"]["record_types"])
    if entity_type not in allowed:
        raise DecisionOpsError("Master-data entity type is invalid.")
    before = payload.get("before") or {}
    after = payload.get("after") or {}
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        raise DecisionOpsError("Before and after values must be objects.")
    actor_id = _actor_id(actor)
    with SessionLocal() as session:
        row = MasterDataChange(
            entity_type=entity_type,
            entity_key=_text(payload.get("entity_key"), limit=180, required=True, label="Entity key"),
            change_type=_text(payload.get("change_type") or "update", limit=40, required=True, label="Change type"),
            before_json=_dumps(before),
            after_json=_dumps(after),
            duplicate_candidate_keys_json=_dumps(payload.get("duplicate_candidate_keys") or []),
            requested_by_user_id=actor_id,
        )
        session.add(row)
        session.commit()
        return {"id": row.id, "entity_type": row.entity_type, "entity_key": row.entity_key, "change_type": row.change_type, "status": row.status, "before": before, "after": after, "duplicate_candidate_keys": _loads(row.duplicate_candidate_keys_json, []), "created_at": _iso(row.created_at)}


def assistant_action(payload: Mapping[str, Any], *, actor: Any) -> dict[str, Any]:
    proposal = {
        "title": _text(payload.get("title"), limit=240, required=True, label="Title"),
        "description": _text(payload.get("description"), limit=4000),
        "source_module": _text(payload.get("source_module"), limit=64, required=True, label="Source module"),
        "source_record_id": _text(payload.get("source_record_id"), limit=180, required=True, label="Source record"),
        "source_url": _safe_uri(payload.get("source_url"), required=True),
        "source_context": payload.get("source_context") or {},
        "metric_key": _text(payload.get("metric_key"), limit=160, required=True, label="Certified metric key"),
        "metric_label": _text(payload.get("metric_label"), limit=200),
        "baseline_value": _float(payload.get("baseline_value")),
        "target_value": _float(payload.get("target_value")),
        "metric_unit": _text(payload.get("metric_unit"), limit=32),
        "expected_financial_impact": _float(payload.get("expected_financial_impact")),
        "priority": str(payload.get("priority") or "medium").strip().lower(),
        "status": "draft",
    }
    if not isinstance(proposal["source_context"], Mapping):
        raise DecisionOpsError("Source context must be an object.")
    if str(payload.get("confirmed") or "").strip().lower() not in {"1", "true", "yes", "on"} and payload.get("confirmed") is not True:
        return {"status": "preview", "requires_confirmation": True, "proposal": proposal, "governance": {"mutation": "none", "next_mutation": "create_draft_only", "retains_source_context": True}}
    created = create_work_item(proposal, actor=actor, created_via="assistant")
    return {"status": "created", "requires_confirmation": False, "action": created, "governance": {"mutation": "draft_created", "approval_bypassed": False, "retains_source_context": True}}
