"""Domain-aware presentation for operational records.

The persistence envelope is intentionally shared across CRM and ERP domains,
but a shared table must not produce a generic user experience.  These helpers
translate governed fields into the six signals an operator needs for the
specific record in front of them.  Missing values stay explicit; they are never
rendered as a synthetic zero.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

DUE_SOON_DAYS = 7


TIMELINE_TITLES = {
    "crm": "Revenue decision timeline",
    "orders": "Order-to-cash timeline",
    "procurement": "Procure-to-pay timeline",
    "finance": "Financial control timeline",
    "inventory": "Inventory control timeline",
    "master-data": "Data-governance timeline",
    "service": "Case and SLA timeline",
}


def _human(value: Any, *, fallback: str = "Source required") -> str:
    token = str(value or "").strip()
    return token.replace("_", " ").title() if token else fallback


def _number(value: Any, *, suffix: str = "") -> str:
    if value is None:
        return "Source required"
    numeric = float(value)
    rendered = f"{numeric:,.0f}" if numeric.is_integer() else f"{numeric:,.1f}"
    return f"{rendered}{suffix}"


def _money(value: Any, currency: str) -> str:
    if value is None:
        return "Source required"
    return f"{currency} {float(value):,.0f}"


def _date(value: Any) -> tuple[str, datetime | None]:
    if not value:
        return "Source required", None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value), None
    return parsed.strftime("%b %d, %Y").replace(" 0", " "), parsed


def _deadline(value: Any, *, now: datetime) -> tuple[str, str, str]:
    display, parsed = _date(value)
    if parsed is None:
        return display, "No governed deadline is recorded", "source"
    comparable = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    days = (comparable.date() - now.date()).days
    if days < 0:
        return display, f"{abs(days)}d overdue", "risk"
    if days == 0:
        return display, "Due today", "warning"
    if days <= DUE_SOON_DAYS:
        return display, f"{days}d remaining", "warning"
    return display, f"{days}d remaining", "healthy"


def _metric(
    label: str,
    value: str,
    note: str,
    *,
    state: str = "neutral",
) -> dict[str, str]:
    if value == "Source required":
        state = "source"
    return {"label": label, "value": value, "note": note, "state": state}


def _scope_metric(item: Mapping[str, Any]) -> dict[str, str]:
    for key, label in (
        ("account_ref", "Account"),
        ("supplier_ref", "Supplier"),
        ("product_ref", "Product / SKU"),
        ("location_ref", "Location"),
    ):
        if item.get(key):
            return _metric(label, str(item[key]), "governed record reference")
    return _metric("Business scope", "Source required", "Link an affected master record")


def _generic_metrics(item: Mapping[str, Any], *, deadline: tuple[str, str, str]) -> list[dict[str, str]]:
    return [
        _metric("Record type", _human(item.get("record_type")), "controlled workflow type"),
        _metric("Amount", _money(item.get("amount"), str(item.get("currency") or "USD")), "recorded source value"),
        _scope_metric(item),
        _metric("Deadline", deadline[0], deadline[1], state=deadline[2]),
        _metric("Approval", _human(item.get("approval_status")), "governed control state"),
        _metric("Owner", str(item.get("owner_name") or "Unassigned"), "accountable operator", state="warning" if not item.get("owner_name") else "neutral"),
    ]


def build_record_brief(  # noqa: PLR0912, PLR0915
    item: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a domain-specific decision brief for one operational record."""

    current = now or datetime.now(timezone.utc)
    domain = str(item.get("domain") or "").strip().lower()
    record_type = str(item.get("record_type") or "").strip().lower()
    metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
    deadline_value = item.get("service_due_at") or item.get("close_at") or item.get("due_at")
    deadline = _deadline(deadline_value, now=current)
    currency = str(item.get("currency") or "USD")
    status = str(item.get("status") or "")
    metrics = _generic_metrics(item, deadline=deadline)
    headline = "Keep ownership, control state, and source evidence aligned."
    narrative = "This governed envelope records the decision without replacing the connected system of record."
    tone = "risk" if item.get("is_overdue") or status in {"exception", "held", "escalated"} else "healthy"

    if domain == "crm" and record_type == "opportunity":
        probability = item.get("probability_pct")
        next_step = str(item.get("next_step") or "").strip()
        metrics = [
            _metric("Deal value", _money(item.get("amount"), currency), "open commercial value"),
            _metric("Weighted value", _money(item.get("weighted_amount"), currency), "probability adjusted"),
            _metric("Probability", _number(probability, suffix="%"), "stage confidence"),
            _metric("Forecast", _human(item.get("forecast_category")), "manager forecast category"),
            _metric("Close date", deadline[0], deadline[1], state=deadline[2]),
            _metric("Next step", "Complete" if next_step else "Missing", next_step or "Add a dated customer commitment", state="healthy" if next_step else "risk"),
        ]
        headline = "Protect the close date and keep the next customer commitment explicit."
        narrative = f"{_human(metadata.get('motion'), fallback='Commercial')} motion for account {item.get('account_ref') or 'not linked'}; forecast and stage remain independently governed."
        if not next_step or deadline[2] == "risk":
            tone = "risk"
    elif domain == "orders" and record_type == "sales_order":
        quantity = item.get("quantity")
        fulfilled = item.get("fulfilled_quantity")
        remaining = max(float(quantity or 0) - float(fulfilled or 0), 0) if quantity is not None else None
        flags = ("on_time", "complete", "damage_free", "invoice_accurate")
        has_perfect_order = all(flag in metadata for flag in flags)
        perfect = all(metadata.get(flag) is True for flag in flags) if has_perfect_order else None
        metrics = [
            _metric("Order value", _money(item.get("amount"), currency), "booked order value"),
            _metric("Ordered", _number(quantity), "source units"),
            _metric("Fulfilled", _number(fulfilled), "picked / shipped units"),
            _metric("Fulfilment", _number(item.get("fulfilment_pct"), suffix="%"), "line-weighted completion"),
            _metric("Backordered", _number(remaining), "units still open", state="warning" if remaining else "healthy"),
            _metric("Perfect order", "Yes" if perfect else "No" if perfect is False else "Measured at closure", "on time · complete · damage free · invoice accurate", state="healthy" if perfect else "risk" if perfect is False else "neutral"),
        ]
        headline = "Clear the next fulfilment constraint without losing the order audit trail."
        narrative = f"{_human(metadata.get('channel'), fallback='Source-gated')} fulfilment channel; holds and partial shipments remain visible until invoicing and payment close the loop."
    elif domain == "procurement":
        control = metadata.get("variance_type") or (f"{metadata.get('aged_days')} days aged" if metadata.get("aged_days") is not None else None) or metadata.get("incoterm")
        metrics = [
            _metric("Commitment", _money(item.get("amount"), currency), "PO / exception value"),
            _metric("Ordered", _number(item.get("quantity")), "source units"),
            _metric("Received", _number(item.get("fulfilled_quantity")), "receipted units"),
            _metric("Receipt", _number(item.get("fulfilment_pct"), suffix="%"), "quantity completion"),
            _metric("Supplier", str(item.get("supplier_ref") or "Source required"), "governed vendor reference"),
            _metric("Control signal", _human(control), "match, variance, aging or delivery term"),
        ]
        headline = "Resolve the approval, receipt, or match exception before value leaves the business."
        narrative = "Planning write-back creates an idempotent draft PO; posting, settlement, and receipt execution remain integration-backed."
    elif domain == "finance":
        detail = None
        if metadata.get("unmatched_items") is not None:
            detail = f"{metadata['unmatched_items']} unmatched items"
        elif metadata.get("close_period"):
            detail = _human(metadata["close_period"])
        elif metadata.get("week_offset") is not None:
            detail = f"Week {int(metadata['week_offset']) + 1}"
        elif metadata.get("cost_center"):
            detail = str(metadata["cost_center"])
        metrics = [
            _metric("Controlled value", _money(item.get("amount"), currency), "entered financial amount"),
            _metric("Process", _human(record_type), "record-to-report work type"),
            _metric("Control state", _human(status), "posting / reconciliation state"),
            _metric("Due date", deadline[0], deadline[1], state=deadline[2]),
            _metric("Control detail", str(detail or "Source required"), "period, exceptions, week or cost center"),
            _metric("Approval", _human(item.get("approval_status")), "no posting bypass"),
        ]
        headline = "Close the control with evidence; do not confuse the workflow envelope with the legal ledger."
        narrative = "Journal posting, bank settlement, tax, consolidation, and multi-currency translation remain source-ledger responsibilities."
    elif domain == "inventory":
        detail = None
        if metadata.get("days_of_supply") is not None:
            detail = f"{metadata['days_of_supply']} days supply"
        elif metadata.get("reorder_point") is not None:
            detail = f"Reorder at {metadata['reorder_point']}"
        elif metadata.get("excursion_minutes") is not None:
            detail = f"{metadata['excursion_minutes']} min excursion"
        metrics = [
            _metric("Quantity", _number(item.get("quantity")), "proposed / controlled units"),
            _metric("Value", _money(item.get("amount"), currency), "recorded inventory value"),
            _metric("Product / SKU", str(item.get("product_ref") or "Source required"), "governed item reference"),
            _metric("Location", str(item.get("location_ref") or "Source required"), "warehouse / bin scope"),
            _metric("Control signal", str(detail or "Source required"), "cover, count, ATP or expiry context"),
            _metric("Approval", _human(item.get("approval_status")), "WMS execution remains gated"),
        ]
        headline = "Approve the inventory decision, then hand physical execution to the connected WMS."
        narrative = "Movement, transfer, count, adjustment, reservation, ATP, and reconciliation records retain one auditable decision state."
    elif domain == "service" and record_type == "case":
        csat = metadata.get("csat")
        metrics = [
            _metric("Priority", _human(item.get("priority")), "queue severity"),
            _metric("SLA due", deadline[0], deadline[1], state=deadline[2]),
            _metric("Account", str(item.get("account_ref") or "Source required"), "customer relationship"),
            _metric("Contact", str(item.get("contact_ref") or "Source required"), "requester / stakeholder"),
            _metric("CSAT", _number(csat, suffix=" / 5") if csat is not None else "Pending survey", "post-resolution feedback"),
            _metric("Escalation", "Active" if status == "escalated" else "Not active", "status-controlled routing", state="risk" if status == "escalated" else "healthy"),
        ]
        headline = "Protect the SLA while keeping the customer, cause, and outcome connected."
        narrative = "Cases can connect to account, return, and corrective-action evidence without pretending to be an omnichannel ticketing source."
    elif domain == "master-data":
        before = metadata.get("before") if isinstance(metadata.get("before"), Mapping) else {}
        after = metadata.get("after") if isinstance(metadata.get("after"), Mapping) else {}
        metrics = [
            _metric("Entity", _human(record_type), "governed master domain"),
            _scope_metric(item),
            _metric("Fields changed", _number(len(set(before) | set(after))), "proposed attribute set"),
            _metric("Steward", str(metadata.get("steward") or item.get("owner_name") or "Unassigned"), "accountable data owner"),
            _metric("Approval", _human(item.get("approval_status")), "four-eyes control"),
            _metric("Source", str(item.get("source_system") or "Native request"), "system identity retained"),
        ]
        headline = "Approve the master-data change only after duplicate and downstream-impact review."
        narrative = "The request retains before/after values and stewardship while the authoritative master remains in its source system."

    return {
        "metrics": metrics,
        "headline": headline,
        "narrative": narrative,
        "tone": tone,
        "timeline_title": TIMELINE_TITLES.get(domain, "Governed record timeline"),
        "deadline_display": deadline[0],
        "deadline_note": deadline[1],
    }
