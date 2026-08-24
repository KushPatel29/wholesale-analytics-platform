"""Service layer for returns workflows and state transitions."""

from __future__ import annotations

from collections import Counter
import hashlib
import hmac
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import pandas as pd
from flask import current_app, render_template
from sqlalchemy import false, func, or_
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from app.cache import cache
from app.auth.models import User
from app.core.exports import dataframe_to_csv_response, dataframes_to_xlsx_response
from app.core.access_policy import get_current_scope, scope_for_user
from app.services import fact_store, metrics
from app.services.mailer import send_email

from . import orders as orders_provider
from . import suggestions as suggestion_engine
from .models import (
    ReturnAttachment,
    ReturnApproval,
    ReturnComment,
    ReturnCorrectiveAction,
    ReturnCorrectiveActionEvent,
    ReturnEvent,
    ReturnInspection,
    ReturnPolicyVersion,
    ReturnReasonCode,
    ReturnSetting,
    ReturnRefund,
    ReturnRMA,
    ReturnRMAItem,
    ReturnShipment,
    ReturnWebhookEvent,
    dumps_json,
    get_session,
    loads_json,
    utcnow,
)


STATUS_REQUESTED = "requested"
STATUS_AWAITING_EVIDENCE = "awaiting_evidence"
STATUS_AUTO_APPROVED = "auto_approved"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_AWAITING_RETURN = "awaiting_return"
STATUS_IN_TRANSIT = "in_transit"
STATUS_RECEIVED = "received"
STATUS_INSPECTED = "inspected"
STATUS_APPROVED_REFUND = "approved_refund"
STATUS_DENIED = "denied"
STATUS_COMPLETED = "completed"
STATUS_PENDING = "pending"
STATUS_WH_APPROVED = "wh_approved"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_AWAITING_OPS = "awaiting_ops"
STATUS_PICKUP_SCHEDULED = "pickup_scheduled"
STATUS_PICKED_UP = "picked_up"
STATUS_AWAITING_FINANCE = "awaiting_finance"
VANCOUVER_TZ = ZoneInfo("America/Vancouver")

STATUS_LABELS = {
    STATUS_REQUESTED: "Requested",
    STATUS_AWAITING_EVIDENCE: "Awaiting Evidence",
    STATUS_AUTO_APPROVED: "Auto Approved",
    STATUS_NEEDS_REVIEW: "Needs Review",
    STATUS_AWAITING_RETURN: "Awaiting Return",
    STATUS_IN_TRANSIT: "In Transit",
    STATUS_RECEIVED: "Received",
    STATUS_INSPECTED: "Inspected",
    STATUS_APPROVED_REFUND: "Approved For Refund",
    STATUS_DENIED: "Denied",
    STATUS_COMPLETED: "Completed",
    STATUS_PENDING: "Pending",
    STATUS_WH_APPROVED: "WH Approved",
    STATUS_APPROVED: "Approved",
    STATUS_REJECTED: "Rejected",
    STATUS_AWAITING_OPS: "Awaiting Operations",
    STATUS_PICKUP_SCHEDULED: "Pickup Scheduled",
    STATUS_PICKED_UP: "Picked Up",
    STATUS_AWAITING_FINANCE: "Awaiting Finance",
}

TRANSITIONS: dict[str, set[str]] = {
    STATUS_REQUESTED: {STATUS_AWAITING_EVIDENCE, STATUS_AUTO_APPROVED, STATUS_NEEDS_REVIEW},
    STATUS_AWAITING_EVIDENCE: {STATUS_AUTO_APPROVED, STATUS_NEEDS_REVIEW},
    STATUS_AUTO_APPROVED: {STATUS_AWAITING_RETURN, STATUS_DENIED},
    STATUS_NEEDS_REVIEW: {STATUS_AWAITING_RETURN, STATUS_DENIED, STATUS_AUTO_APPROVED},
    STATUS_AWAITING_RETURN: {STATUS_IN_TRANSIT, STATUS_RECEIVED, STATUS_DENIED},
    STATUS_IN_TRANSIT: {STATUS_RECEIVED},
    STATUS_RECEIVED: {STATUS_INSPECTED},
    STATUS_INSPECTED: {STATUS_APPROVED_REFUND, STATUS_DENIED, STATUS_COMPLETED},
    STATUS_APPROVED_REFUND: {STATUS_COMPLETED},
    STATUS_DENIED: {STATUS_COMPLETED},
    STATUS_COMPLETED: set(),
    STATUS_PENDING: {STATUS_WH_APPROVED, STATUS_REJECTED, STATUS_AWAITING_OPS},
    STATUS_AWAITING_OPS: {STATUS_PICKUP_SCHEDULED, STATUS_REJECTED, STATUS_WH_APPROVED, STATUS_RECEIVED},
    STATUS_PICKUP_SCHEDULED: {STATUS_PICKED_UP, STATUS_REJECTED, STATUS_RECEIVED},
    STATUS_PICKED_UP: {STATUS_RECEIVED, STATUS_REJECTED, STATUS_WH_APPROVED},
    STATUS_WH_APPROVED: {STATUS_APPROVED, STATUS_REJECTED, STATUS_AWAITING_FINANCE},
    STATUS_APPROVED: {STATUS_AWAITING_RETURN, STATUS_IN_TRANSIT, STATUS_RECEIVED, STATUS_COMPLETED, STATUS_AWAITING_FINANCE},
    STATUS_AWAITING_FINANCE: {STATUS_COMPLETED, STATUS_REJECTED},
    STATUS_REJECTED: {STATUS_COMPLETED},
}


class ReturnsError(RuntimeError):
    """Base error for returns workflows."""


class InvalidTransitionError(ReturnsError):
    """Raised when a state transition is not allowed."""


class ScopeViolationError(ReturnsError):
    """Raised when the current user scope does not allow the requested customer."""


class InvalidOrderLookupError(ReturnsError):
    """Raised when an order lookup request is invalid."""


class OrderLookupNotFoundError(ReturnsError):
    """Raised when an order lookup does not match any visible order."""


def _flag(name: str, default: bool = False) -> bool:
    try:
        value = current_app.config.get(name, default)
    except Exception:
        value = default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def returns_enabled() -> bool:
    return _flag("RETURNS_ENABLED", False)


def returns_final_v1_enabled() -> bool:
    return returns_enabled() and _flag("RETURNS_FINAL_V1", False)


def customer_portal_enabled() -> bool:
    return returns_enabled() and _flag("RETURNS_CUSTOMER_PORTAL_ENABLED", True)


def returns_v2_enabled() -> bool:
    return returns_enabled() and (_flag("RETURNS_V2_UI", _flag("RETURNS_V2", False)) or _flag("RETURNS_V2", False))


def returns_excel_form_enabled() -> bool:
    return returns_enabled() and (_flag("RETURNS_UI_EXCEL_FORM", returns_v2_enabled()) or returns_v2_enabled())


def returns_autofill_order_enabled() -> bool:
    return returns_enabled() and (_flag("RETURNS_AUTOFILL_ORDER", returns_excel_form_enabled()) or returns_v2_enabled())


def returns_analytics_enabled() -> bool:
    return returns_enabled() and (_flag("RETURNS_ANALYTICS", False) or returns_final_v1_enabled())


def labels_enabled() -> bool:
    return returns_enabled() and _flag("RETURNS_LABELS_ENABLED", False)


def refunds_enabled() -> bool:
    return returns_enabled() and _flag("RETURNS_REFUNDS_ENABLED", True)


def ai_enabled() -> bool:
    return returns_enabled() and _flag("RETURNS_AI_ENABLED", False)


def status_label(status: str) -> str:
    return STATUS_LABELS.get(str(status or "").strip().lower(), str(status or "").strip())


def attachments_root() -> Path:
    configured = current_app.config.get("RETURNS_UPLOAD_DIR")
    root = Path(configured) if configured else Path(current_app.instance_path) / "returns_uploads"
    root = root.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def safe_upload_name(filename: str) -> str:
    name = secure_filename(Path(str(filename or "")).name)
    return name or f"upload_{int(time.time())}.bin"


def build_public_url(path: str) -> str:
    clean_path = "/" + str(path or "").lstrip("/")
    base = str(current_app.config.get("APP_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if not base:
        return clean_path
    return f"{base}{clean_path}"


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _is_valid_email_address(value: Any) -> bool:
    email = str(value or "").strip()
    if not email:
        return False
    return bool(_EMAIL_RE.match(email))


def _list_config_emails(key: str) -> list[str]:
    raw = current_app.config.get(key)
    if raw is None:
        raw = current_app.config.get(str(key).replace("_EMAILS", "_EMAIL"))
    if isinstance(raw, str):
        values = [item.strip() for item in raw.replace(";", ",").split(",")]
    elif isinstance(raw, (list, tuple, set)):
        values = [str(item or "").strip() for item in raw]
    else:
        values = []
    return [item for item in values if item]


def _actor_name(actor_user: Any) -> str:
    if actor_user is None:
        return ""
    full_name = str(getattr(actor_user, "full_name", "") or "").strip()
    if full_name:
        return full_name
    first = str(getattr(actor_user, "first_name", "") or "").strip()
    last = str(getattr(actor_user, "last_name", "") or "").strip()
    combined = " ".join(part for part in (first, last) if part)
    if combined:
        return combined
    return str(getattr(actor_user, "username", "") or "").strip()


def _vancouver_now() -> datetime:
    return datetime.now(VANCOUVER_TZ)


def _coerce_datetime(value: Any) -> datetime | None:
    if value in (None, "", False):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _coerce_date(value: Any) -> date | None:
    if value in (None, "", False):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    dt_value = _coerce_datetime(value)
    if dt_value is not None:
        return dt_value.date()
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except Exception:
        return None


def _scope_payload(scope: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if scope is not None:
        return scope
    scope_obj = get_current_scope(use_cache=True)
    return scope_obj.as_dict()


def _scope_for_actor(
    actor_user: Any = None,
    *,
    scope: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if scope is not None:
        return _scope_payload(scope)
    if actor_user is not None:
        try:
            return scope_for_user(actor_user, use_cache=True).as_dict()
        except Exception:
            pass
    return _scope_payload()


def _actor_id(actor_user: Any) -> Optional[int]:
    try:
        uid = getattr(actor_user, "id", None)
        return int(uid) if uid is not None else None
    except Exception:
        return None


def _int_or_none(value: Any) -> Optional[int]:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except Exception:
        return None


def order_lookup_scope_error_message(
    *,
    actor_user: Any = None,
    scope: Optional[dict[str, Any]] = None,
    default_message: str = "Order not in your scope.",
) -> str:
    payload = _scope_for_actor(actor_user, scope=scope)
    mode = str(payload.get("scope_mode") or "none").strip().lower()
    if bool(payload.get("is_admin")) or mode == "all":
        return str(default_message)

    allowed_customers = [
        str(item).strip()
        for item in (payload.get("allowed_customer_ids") or payload.get("customer_ids") or [])
        if str(item).strip()
    ]
    secondary_scope = any(
        str(item).strip()
        for key in (
            "allowed_erp_user_ids",
            "allowed_region_ids",
            "allowed_supplier_ids",
            "erp_user_ids",
            "region_ids",
            "supplier_ids",
            "sales_rep_ids",
        )
        for item in (payload.get(key) or [])
    )
    if mode == "none" or (not allowed_customers and not secondary_scope):
        current_app.logger.warning(
            "returns.user_scope_empty",
            extra={
                "actor_user_id": _actor_id(actor_user),
                "scope_mode": mode,
                "scope_hash": payload.get("scope_hash"),
            },
        )
        return "Your account has no customer access configured. Contact admin."
    return str(default_message)


def _policy_record(session: Any) -> ReturnPolicyVersion | None:
    active = (
        session.query(ReturnPolicyVersion)
        .filter(ReturnPolicyVersion.is_active == True)  # noqa: E712
        .order_by(ReturnPolicyVersion.created_at.desc(), ReturnPolicyVersion.id.desc())
        .first()
    )
    if active:
        return active
    return session.query(ReturnPolicyVersion).order_by(ReturnPolicyVersion.created_at.desc(), ReturnPolicyVersion.id.desc()).first()


def active_policy() -> dict[str, Any]:
    with get_session() as session:
        policy = _policy_record(session)
        if not policy:
            return {"version": "1", "rules": {}}
        return {"version": policy.version, "rules": loads_json(policy.rules_json, {})}


def list_policy_versions() -> list[dict[str, Any]]:
    with get_session() as session:
        rows = (
            session.query(ReturnPolicyVersion)
            .order_by(ReturnPolicyVersion.created_at.desc(), ReturnPolicyVersion.id.desc())
            .all()
        )
        return [
            {
                "id": int(row.id),
                "version": row.version,
                "is_active": bool(row.is_active),
                "rules": loads_json(row.rules_json, {}),
                "created_at": row.created_at.isoformat() if row.created_at else "",
            }
            for row in rows
        ]


def create_policy_version(version: str, rules: dict[str, Any], *, activate: bool = True) -> dict[str, Any]:
    clean_version = str(version or "").strip()
    if not clean_version:
        raise ReturnsError("Policy version is required.")
    with get_session() as session:
        existing = (
            session.query(ReturnPolicyVersion)
            .filter(ReturnPolicyVersion.version == clean_version)
            .first()
        )
        if existing:
            existing.rules_json = dumps_json(rules)
            existing.is_active = bool(activate)
            row = existing
        else:
            row = ReturnPolicyVersion(
                version=clean_version,
                rules_json=dumps_json(rules),
                is_active=bool(activate),
            )
            session.add(row)
            session.flush()
        if activate:
            session.query(ReturnPolicyVersion).filter(ReturnPolicyVersion.id != row.id).update({"is_active": False})
        session.add(row)
        session.commit()
        return {
            "id": int(row.id),
            "version": row.version,
            "is_active": bool(row.is_active),
            "rules": loads_json(row.rules_json, {}),
        }


_DEFAULT_REASON_CODES: tuple[tuple[str, str, str], ...] = (
    ("quality_issue", "Quality Issue", "Production"),
    ("damaged", "Damaged", "Warehouse"),
    ("wrong_item", "Wrong Item", "Sales"),
    ("customer_return", "Customer Return", "Sales"),
    ("short_issue", "Short / Issue", "Warehouse"),
    ("vendor_return", "Vendor Return", "Other"),
)


def _normalize_reason_category(value: Any) -> str:
    clean = str(value or "").strip().lower()
    mapping = {
        "production": "Production",
        "warehouse": "Warehouse",
        "sales": "Sales",
        "other": "Other",
    }
    return mapping.get(clean, "Other")


def list_reason_codes(*, active_only: bool = True) -> list[dict[str, Any]]:
    with get_session() as session:
        query = session.query(ReturnReasonCode)
        if active_only:
            query = query.filter(ReturnReasonCode.active == True)  # noqa: E712
        rows = query.order_by(ReturnReasonCode.reason_text.asc(), ReturnReasonCode.reason_code.asc()).all()
        if not rows:
            return [
                {
                    "reason_code": code,
                    "reason_text": text,
                    "category": category,
                    "active": True,
                }
                for code, text, category in _DEFAULT_REASON_CODES
            ]
        return [
            {
                "id": int(row.id),
                "reason_code": row.reason_code,
                "reason_text": row.reason_text,
                "category": _normalize_reason_category(row.category),
                "active": bool(row.active),
            }
            for row in rows
        ]


def _reason_lookup_map(session: Any | None = None) -> dict[str, dict[str, Any]]:
    if session is None:
        rows = list_reason_codes(active_only=False)
        return {str(row.get("reason_code") or "").strip().lower(): row for row in rows if str(row.get("reason_code") or "").strip()}

    rows = (
        session.query(ReturnReasonCode)
        .order_by(ReturnReasonCode.reason_text.asc(), ReturnReasonCode.reason_code.asc())
        .all()
    )
    if not rows:
        return {
            code.lower(): {
                "reason_code": code,
                "reason_text": text,
                "category": category,
                "active": True,
            }
            for code, text, category in _DEFAULT_REASON_CODES
        }
    return {
        str(row.reason_code or "").strip().lower(): {
            "id": int(row.id),
            "reason_code": row.reason_code,
            "reason_text": row.reason_text,
            "category": _normalize_reason_category(row.category),
            "active": bool(row.active),
        }
        for row in rows
        if str(row.reason_code or "").strip()
    }


def category_for_reason(reason_code: Any, reason_text: Any = None, *, lookup: dict[str, dict[str, Any]] | None = None) -> str:
    clean_code = str(reason_code or "").strip().lower()
    catalog = lookup or _reason_lookup_map()
    if clean_code and clean_code in catalog:
        return _normalize_reason_category(catalog[clean_code].get("category"))
    raw_reason = " ".join(part for part in (str(reason_code or "").strip(), str(reason_text or "").strip()) if part).lower()
    if any(token in raw_reason for token in ("damage", "short", "warehouse")):
        return "Warehouse"
    if any(token in raw_reason for token in ("quality", "production", "temp", "spec")):
        return "Production"
    if raw_reason:
        return "Sales"
    return "Other"


def save_reason_code(
    *,
    reason_code: str,
    reason_text: str,
    category: str,
    active: bool = True,
) -> dict[str, Any]:
    clean_code = str(reason_code or "").strip().lower()
    clean_text = str(reason_text or "").strip()
    if not clean_code:
        raise ReturnsError("Reason code is required.")
    if not clean_text:
        raise ReturnsError("Reason text is required.")
    clean_category = _normalize_reason_category(category)
    with get_session() as session:
        row = (
            session.query(ReturnReasonCode)
            .filter(func.lower(ReturnReasonCode.reason_code) == clean_code)
            .first()
        )
        if not row:
            row = ReturnReasonCode(reason_code=clean_code)
            session.add(row)
            session.flush()
        row.reason_code = clean_code
        row.reason_text = clean_text
        row.category = clean_category
        row.active = bool(active)
        session.add(row)
        session.commit()
        return {
            "id": int(row.id),
            "reason_code": row.reason_code,
            "reason_text": row.reason_text,
            "category": _normalize_reason_category(row.category),
            "active": bool(row.active),
        }


def get_returns_settings() -> dict[str, Any]:
    defaults = {
        "defaults": {
            "return_type": "Sales Return",
            "follow_up_action": "Credit",
        },
        "workflow_options": {
            "follow_up_actions": ["Credit", "Replacement", "Discount", "No Action"],
            "warehouse_outcomes": ["Returning to Inventory", "Spoilage"],
            "companies": ["Northgate Retail Group", "Alderwood Grocers"],
        },
        "email_templates": {
            "new_return_subject": "New return pending review for order {{ order_id }}",
            "wh_approval_subject": "Warehouse approved return #{{ rma_id }}",
            "manager_approval_subject": "Return #{{ rma_id }} approved",
            "rejection_subject": "Return #{{ rma_id }} rejected",
        },
    }
    with get_session() as session:
        row = (
            session.query(ReturnSetting)
            .filter(ReturnSetting.setting_key == "returns")
            .first()
        )
        if not row:
            return defaults
        payload = loads_json(row.setting_value, {})
        if not isinstance(payload, dict):
            payload = {}
        merged = json.loads(json.dumps(defaults))
        for key, value in payload.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
        return merged


def save_returns_settings(payload: dict[str, Any], *, actor_user: Any = None) -> dict[str, Any]:
    settings_payload = payload if isinstance(payload, dict) else {}
    with get_session() as session:
        row = (
            session.query(ReturnSetting)
            .filter(ReturnSetting.setting_key == "returns")
            .first()
        )
        if not row:
            row = ReturnSetting(setting_key="returns")
            session.add(row)
            session.flush()
        row.setting_value = dumps_json(settings_payload)
        row.updated_by_user_id = _actor_id(actor_user)
        row.updated_at = utcnow()
        session.add(row)
        session.commit()
    return get_returns_settings()


def can_transition(from_status: str, to_status: str) -> bool:
    current = str(from_status or "").strip().lower()
    target = str(to_status or "").strip().lower()
    return target in TRANSITIONS.get(current, set())


def _compute_risk_score(item_payloads: Iterable[dict[str, Any]]) -> float:
    score = 0.0
    total_qty = 0.0
    total_value = 0.0
    risky_reasons = {"fraud", "missing", "not_as_described"}
    for item in item_payloads:
        qty = float(item.get("qty") or 0)
        price = float(item.get("price") or 0)
        total_qty += qty
        total_value += max(price, 0)
        reason = str(item.get("reason_code") or "").strip().lower()
        if reason in risky_reasons:
            score += 0.35
        if reason in {"damaged", "quality_issue"}:
            score += 0.08
    if total_qty >= 5:
        score += 0.12
    if total_value >= 750:
        score += 0.18
    if ai_enabled():
        score += 0.02
    return min(round(score, 4), 1.0)


def _initial_status(
    *,
    item_payloads: list[dict[str, Any]],
    attachment_count: int,
    rules: dict[str, Any],
    risk_score: float,
) -> tuple[str, str]:
    reason_codes = {str(item.get("reason_code") or "").strip().lower() for item in item_payloads if item}
    evidence_required = {
        str(code).strip().lower()
        for code in (rules.get("evidence_required_reason_codes") or [])
        if str(code).strip()
    }
    auto_approve = {
        str(code).strip().lower()
        for code in (rules.get("auto_approve_reason_codes") or [])
        if str(code).strip()
    }
    threshold = float(rules.get("auto_review_risk_threshold") or 0.35)
    if evidence_required.intersection(reason_codes) and attachment_count <= 0:
        return STATUS_AWAITING_EVIDENCE, "Customer evidence is required before approval."
    if risk_score <= threshold and (not reason_codes or reason_codes.issubset(auto_approve)):
        return STATUS_AUTO_APPROVED, "Auto-approved by policy."
    return STATUS_NEEDS_REVIEW, "Queued for manager review."


def export_sage_csv(rma_ids: list[int], *, actor_user: Any = None) -> bytes:
    """Generate a structured CSV export for Sage import."""
    import pandas as pd
    from io import BytesIO

    with get_session() as session:
        rmas = session.query(ReturnRMA).filter(ReturnRMA.id.in_(rma_ids)).all()
        rma_map = {rma.id: rma for rma in rmas}
        
        items = (
            session.query(ReturnRMAItem)
            .filter(ReturnRMAItem.rma_id.in_(rma_ids))
            .all()
        )
        
        rows = []
        for item in items:
            rma = rma_map.get(item.rma_id)
            if not rma:
                continue
            
            metadata = loads_json(rma.metadata_json, {})
            item_metadata = loads_json(item.metadata_json, {})
            
            rows.append({
                "RMA Number": rma.rma_number,
                "Company": rma.company or "Northgate Retail Group",
                "Customer ID": rma.customer_id,
                "Customer Name": rma.customer_name,
                "Original Invoice": rma.order_id,
                "SKU": item.sku or item.product_code,
                "Description": item.product_name or item.product_desc,
                "Weight (lb)": item.received_weight_lb if item.received_weight_lb is not None else item.weight_lb,
                "Credit Amount": item.credit_amount,
                "Tax Treatment": item_metadata.get("tax_treatment") or metadata.get("tax_treatment") or "Taxable",
                "Reason": item.reason_for_return or item.reason_code or rma.primary_reason,
                "Warehouse Outcome": item.warehouse_outcome,
                "Approval Target": rma.approval_target,
                "Date Submitted": rma.date_submitted.isoformat() if rma.date_submitted else "",
            })
            
    df = pd.DataFrame(rows)
    output = BytesIO()
    df.to_csv(output, index=False)
    return output.getvalue()


def _add_event(
    session: Any,
    *,
    rma_id: int,
    event_type: str,
    from_status: str | None,
    to_status: str | None,
    actor_user_id: int | None,
    payload: Optional[dict[str, Any]] = None,
    field_name: str | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
) -> ReturnEvent:
    row = ReturnEvent(
        rma_id=int(rma_id),
        event_type=str(event_type or "event"),
        from_status=from_status,
        to_status=to_status,
        field_name=field_name,
        old_value=old_value,
        new_value=new_value,
        actor_user_id=actor_user_id,
        payload_json=dumps_json(payload or {}),
    )
    session.add(row)
    session.flush()
    return row


def _log_field_change(
    session: Any,
    rma_id: int,
    field_name: str,
    old_val: Any,
    new_val: Any,
    *,
    actor_user: Any = None,
) -> None:
    """Record a granular field change for auditing."""
    if str(old_val) == str(new_val):
        return
    _add_event(
        session,
        rma_id=rma_id,
        event_type="field_changed",
        from_status=None,
        to_status=None,
        actor_user_id=_actor_id(actor_user),
        field_name=field_name,
        old_value=str(old_val) if old_val is not None else None,
        new_value=str(new_val) if new_val is not None else None,
    )


def _serialize_temporal(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _serialize_rma(row: ReturnRMA) -> dict[str, Any]:
    metadata = loads_json(row.metadata_json, {})
    return {
        "id": int(row.id),
        "rma_number": row.rma_number or f"RMA-{int(row.id):06d}",
        "customer_id": row.customer_id,
        "customer_name": row.customer_name,
        "customer_email": row.customer_email,
        "customer_phone": row.customer_phone,
        "order_id": row.order_id,
        "order_date": _serialize_temporal(row.order_date),
        "rep_user_id": row.rep_user_id,
        "rep_name": row.rep_name,
        "date_submitted": _serialize_temporal(row.date_submitted),
        "date_shipped": _serialize_temporal(row.date_shipped),
        "return_type": row.return_type,
        "company": row.company,
        "approval_target": row.approval_target,
        "advised_customer": bool(row.advised_customer),
        "additional_notes": row.additional_notes,
        "total_credit_amount": round(float(row.total_credit_amount or 0), 2),
        "total_weight_lb": round(float(row.total_weight_lb or 0), 3),
        "total_packs": int(row.total_packs or 0),
        "primary_reason": row.primary_reason,
        "primary_category": row.primary_category,
        "primary_follow_up": metadata.get("primary_follow_up"),
        "rec_prod_signoff": bool(row.rec_prod_signoff),
        "rec_prod_signed_by_user_id": row.rec_prod_signed_by_user_id,
        "rec_prod_signed_at": _serialize_temporal(row.rec_prod_signed_at),
        "wh_approved_by_user_id": row.wh_approved_by_user_id,
        "wh_approved_at": _serialize_temporal(row.wh_approved_at),
        "mgr_approved_by_user_id": row.mgr_approved_by_user_id,
        "mgr_approved_at": _serialize_temporal(row.mgr_approved_at),
        "rejected_by_user_id": row.rejected_by_user_id,
        "rejected_at": _serialize_temporal(row.rejected_at),
        "reject_reason": row.reject_reason,
        "status": row.status,
        "status_label": status_label(row.status),
        "created_by_user_id": row.created_by_user_id,
        "assigned_user_id": row.assigned_user_id,
        "decision_summary": row.decision_summary,
        "policy_version": row.policy_version,
        "risk_score": float(row.risk_score or 0),
        "external_reference": row.external_reference,
        "metadata": metadata,
        "created_at": _serialize_temporal(row.created_at),
        "updated_at": _serialize_temporal(row.updated_at),
        "last_updated": _serialize_temporal(row.updated_at or row.created_at),
    }


def _serialize_item(row: ReturnRMAItem) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "rma_id": int(row.rma_id),
        "order_line_id": row.order_line_id,
        "sku": row.sku,
        "product_name": row.product_name,
        "product_code": row.product_code or row.sku,
        "product_desc": row.product_desc or row.product_name,
        "price_per_lb": float(row.price_per_lb or 0),
        "weight_lb": float(row.weight_lb or 0),
        "credit_pct": _coerce_credit_pct(row.credit_pct, 100.0),
        "credit_amount": float(row.credit_amount or 0),
        "product_returning": bool(row.product_returning),
        "packs_count": int(row.packs_count or 0),
        "pack_barcode": row.pack_barcode,
        "reason_for_return": row.reason_for_return or row.reason_code,
        "follow_up_action": row.follow_up_action,
        "category": row.category,
        "supplier_credit": bool(row.supplier_credit),
        "qty": float(row.qty or 0),
        "price": float(row.price or 0),
        "reason_code": row.reason_code,
        "warehouse_outcome": row.warehouse_outcome,
        "received_weight_lb": float(row.received_weight_lb) if row.received_weight_lb is not None else None,
        "condition": row.item_condition,
        "notes": row.notes,
        "receiving_notes": row.receiving_notes,
        "metadata": loads_json(row.metadata_json, {}),
    }


def _serialize_event(row: ReturnEvent) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "rma_id": int(row.rma_id),
        "event_type": row.event_type,
        "from_status": row.from_status,
        "to_status": row.to_status,
        "actor_user_id": row.actor_user_id,
        "payload": loads_json(row.payload_json, {}),
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


def _serialize_approval(row: ReturnApproval | None) -> dict[str, Any]:
    if not row:
        return {}
    return {
        "rma_id": int(row.rma_id),
        "wh_approved_by": row.wh_approved_by,
        "wh_approved_at": row.wh_approved_at.isoformat() if row.wh_approved_at else "",
        "mgr_approved_by": row.mgr_approved_by,
        "mgr_approved_at": row.mgr_approved_at.isoformat() if row.mgr_approved_at else "",
        "rejected_by": row.rejected_by,
        "rejected_at": row.rejected_at.isoformat() if row.rejected_at else "",
        "reject_reason": row.reject_reason,
    }


def _serialize_attachment(row: ReturnAttachment) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "rma_id": int(row.rma_id),
        "item_id": row.item_id,
        "filename": row.filename,
        "mimetype": row.mimetype or row.mime,
        "storage_path": row.storage_path or row.file_path,
        "file_path": row.file_path,
        "mime": row.mime,
        "size": int(row.size or 0),
        "uploaded_by_user_id": row.uploaded_by_user_id,
        "uploaded_at": _serialize_temporal(row.uploaded_at or row.created_at),
        "created_at": _serialize_temporal(row.created_at),
    }


def _serialize_inspection(row: ReturnInspection) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "rma_id": int(row.rma_id),
        "disposition": row.disposition,
        "notes": row.notes,
        "photos": loads_json(row.photos_json, []),
        "inspected_by_user_id": row.inspected_by_user_id,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


def _serialize_shipment(row: ReturnShipment) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "rma_id": int(row.rma_id),
        "carrier": row.carrier,
        "label_url": row.label_url,
        "tracking_number": row.tracking_number,
        "shipping_cost": float(row.shipping_cost or 0),
        "status": row.status,
        "metadata": loads_json(row.metadata_json, {}),
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


def _serialize_refund(row: ReturnRefund) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "rma_id": int(row.rma_id),
        "amount": float(row.amount or 0),
        "method": row.method,
        "status": row.status,
        "processor_ref": row.processor_ref,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


def _serialize_comment(row: ReturnComment) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "rma_id": int(row.rma_id),
        "user_id": row.user_id,
        "body": row.body,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


def _status_filter_values(status: str | None) -> list[str] | None:
    clean = str(status or "").strip().lower()
    if not clean or clean == "all":
        return None
    aliases = {
        "accepted": [STATUS_APPROVED],
        "rejected": [STATUS_REJECTED, STATUS_DENIED],
        "pending": [STATUS_PENDING],
        "wh approved": [STATUS_WH_APPROVED],
        "wh_approved": [STATUS_WH_APPROVED],
        "approved": [STATUS_APPROVED],
    }
    return aliases.get(clean, [clean])


def _ensure_approval_row(session: Any, rma_id: int) -> ReturnApproval:
    row = (
        session.query(ReturnApproval)
        .filter(ReturnApproval.rma_id == int(rma_id))
        .first()
    )
    if row:
        return row
    row = ReturnApproval(rma_id=int(rma_id))
    session.add(row)
    session.flush()
    return row


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        if isinstance(value, str):
            cleaned = value.strip().replace(",", "")
            if cleaned == "":
                return float(default)
            return float(cleaned)
        return float(value)
    except Exception:
        return float(default)


def _coerce_credit_pct(value: Any, default: float = 100.0) -> float:
    numeric = _coerce_float(value, default)
    numeric = max(0.0, min(100.0, numeric))
    return round(numeric, 2)


def _credit_amount_for_item(item_payload: dict[str, Any]) -> float:
    price_per_lb = _coerce_float(item_payload.get("price_per_lb"), _coerce_float(item_payload.get("price"), 0.0))
    # Use received_weight_lb if present, otherwise fallback to weight_lb
    weight_lb = _coerce_float(item_payload.get("received_weight_lb"), _coerce_float(item_payload.get("weight_lb"), _coerce_float(item_payload.get("qty"), 0.0)))
    credit_pct = _coerce_credit_pct(item_payload.get("credit_pct"), 100.0)
    credit = price_per_lb * weight_lb * (credit_pct / 100.0)
    return round(max(credit, 0.0), 2)


def _can_manage_workflow(actor_user: Any) -> bool:
    if actor_user is None:
        return False
    try:
        from app.core.rbac import effective_permissions  # type: ignore

        perms = effective_permissions(actor_user)
    except Exception:
        perms = set()
    wanted = {
        "returns.approve.wh",
        "returns.approve.mgr",
        "admin.returns.manage",
        "returns.manage.overrides",
        "returns.ops.approve",
        "returns.ops.override",
    }
    return "*" in perms or bool(set(perms).intersection(wanted))


def _validate_submission_payload(order_payload: dict[str, Any], item_payloads: list[dict[str, Any]]) -> None:
    if not item_payloads:
        raise ReturnsError("At least one return item is required.")
    if not str(order_payload.get("order_id") or "").strip():
        raise ReturnsError("Order number is required.")
    if not str(order_payload.get("customer_id") or "").strip():
        raise ReturnsError("Customer ID is required.")
    if not str(order_payload.get("customer_name") or "").strip():
        raise ReturnsError("Customer name is required.")
    if _coerce_datetime(order_payload.get("date_submitted")) is None:
        raise ReturnsError("Submission date is required.")
    if not str(order_payload.get("return_type") or "").strip():
        raise ReturnsError("Return type is required.")

    requires_advised_customer = any(not bool(item.get("product_returning", True)) for item in item_payloads)
    advised_customer_provided = bool(order_payload.get("advised_customer_provided"))
    if requires_advised_customer and not advised_customer_provided:
        raise ReturnsError("Indicate whether the customer was advised when any item is not being returned.")

    for idx, item in enumerate(item_payloads, start=1):
        product_code = str(item.get("product_code") or item.get("sku") or "").strip()
        product_desc = str(item.get("product_desc") or item.get("product_name") or "").strip()
        weight_lb = _coerce_float(item.get("weight_lb"), _coerce_float(item.get("qty"), 0.0))
        price_per_lb = _coerce_float(item.get("price_per_lb"), _coerce_float(item.get("price"), 0.0))
        credit_pct = _coerce_credit_pct(item.get("credit_pct"), 100.0)
        item["credit_pct"] = credit_pct
        product_returning_provided = bool(
            item.get("product_returning_provided")
            if item.get("product_returning_provided") is not None
            else item.get("product_returning") is not None
        )
        supplier_credit_provided = bool(
            item.get("supplier_credit_provided")
            if item.get("supplier_credit_provided") is not None
            else item.get("supplier_credit") is not None
        )
        if not product_code:
            raise ReturnsError(f"Item {idx}: product code is required.")
        if not product_desc:
            raise ReturnsError(f"Item {idx}: product description is required.")
        if weight_lb <= 0:
            raise ReturnsError(f"Item {idx}: weight must be greater than zero.")
        if price_per_lb < 0:
            raise ReturnsError(f"Item {idx}: price per lb cannot be negative.")
        if not product_returning_provided:
            raise ReturnsError(f"Item {idx}: indicate whether the product is returning.")
        if not str(item.get("reason_for_return") or item.get("reason_code") or "").strip():
            raise ReturnsError(f"Item {idx}: reason for return is required.")
        if not str(item.get("follow_up_action") or "").strip():
            raise ReturnsError(f"Item {idx}: follow-up action is required.")
        if not supplier_credit_provided:
            raise ReturnsError(f"Item {idx}: indicate whether supplier credit applies.")


def _validate_warehouse_transition(row: ReturnRMA, items: list[ReturnRMAItem], rejecting: bool = False, payload: Optional[dict[str, Any]] = None) -> None:
    if rejecting:
        reject_reason = str((payload or {}).get("reject_reason") or (payload or {}).get("reason") or "").strip()
        if not reject_reason:
            raise ReturnsError("Reject reason is required.")
        return
    if not items:
        raise ReturnsError("Warehouse approval requires at least one line item.")
    for idx, item in enumerate(items, start=1):
        has_pack_data = bool(str(item.pack_barcode or "").strip()) or int(item.packs_count or 0) > 0
        if not has_pack_data:
            raise ReturnsError(f"Item {idx}: warehouse approval requires a pack barcode or packs count.")
        if not str(item.follow_up_action or "").strip():
            raise ReturnsError(f"Item {idx}: warehouse approval requires a follow-up action.")


def _validate_manager_transition(row: ReturnRMA, items: list[ReturnRMAItem]) -> None:
    if not items:
        raise ReturnsError("Manager approval requires at least one line item.")
    computed_total = round(sum(_credit_amount_for_item(_serialize_item(item)) for item in items), 2)
    stored_total = round(float(row.total_credit_amount or 0), 2)
    total_weight = round(sum(float(item.received_weight_lb if item.received_weight_lb is not None else item.weight_lb) for item in items), 3)
    if total_weight <= 0:
        raise ReturnsError("Manager approval requires computed totals from at least one valid line item.")
    if abs(computed_total - stored_total) > 0.01:
        raise ReturnsError("Manager approval is blocked until totals are recalculated.")


def _summarize_item_rollups(
    item_payloads: Iterable[dict[str, Any]],
    *,
    reason_lookup: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    total_credit = 0.0
    total_weight = 0.0
    total_packs = 0
    reason_counter: Counter[str] = Counter()
    category_counter: Counter[str] = Counter()
    follow_up_counter: Counter[str] = Counter()
    catalog = reason_lookup or _reason_lookup_map()
    for item in item_payloads:
        if not item:
            continue
        credit = _credit_amount_for_item(item)
        # Prioritize received_weight_lb if available
        weight = float(item.get("received_weight_lb") or item.get("weight_lb") or item.get("qty") or 0)
        packs = _coerce_int(item.get("packs_count") or item.get("packs") or 0, 0)
        reason_text = str(item.get("reason_for_return") or item.get("reason_code") or "").strip()
        category = str(item.get("category") or "").strip() or category_for_reason(
            item.get("reason_code"),
            reason_text,
            lookup=catalog,
        )
        follow_up = str(item.get("follow_up_action") or "").strip()
        total_credit += credit
        total_weight += max(weight, 0.0)
        total_packs += max(packs, 0)
        if reason_text:
            reason_counter[reason_text] += 1
        if category:
            category_counter[_normalize_reason_category(category)] += 1
        if follow_up:
            follow_up_counter[follow_up] += 1
    primary_reason = reason_counter.most_common(1)[0][0] if reason_counter else None
    primary_category = category_counter.most_common(1)[0][0] if category_counter else None
    primary_follow_up = follow_up_counter.most_common(1)[0][0] if follow_up_counter else None
    return {
        "total_credit_amount": round(total_credit, 2),
        "total_weight_lb": round(total_weight, 3),
        "total_packs": int(total_packs),
        "primary_reason": primary_reason,
        "primary_category": primary_category,
        "primary_follow_up": primary_follow_up,
    }


def _update_rma_metadata(row: ReturnRMA, **updates: Any) -> dict[str, Any]:
    metadata = loads_json(row.metadata_json, {})
    for key, value in updates.items():
        if value is not None:
            metadata[key] = value
    row.metadata_json = dumps_json(metadata)
    return metadata


def _apply_rma_rollups(
    row: ReturnRMA,
    item_payloads: Iterable[dict[str, Any]],
    *,
    reason_lookup: dict[str, dict[str, Any]] | None = None,
    metadata_updates: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    rollups = _summarize_item_rollups(item_payloads, reason_lookup=reason_lookup)
    row.total_credit_amount = rollups["total_credit_amount"]
    row.total_weight_lb = rollups["total_weight_lb"]
    row.total_packs = rollups["total_packs"]
    row.primary_reason = rollups["primary_reason"]
    row.primary_category = rollups["primary_category"]
    merged_updates = dict(metadata_updates or {})
    merged_updates["primary_follow_up"] = rollups["primary_follow_up"] or ""
    _update_rma_metadata(row, **merged_updates)
    return rollups


def _active_user_emails_for_permissions(*perms: str) -> list[str]:
    wanted = {str(perm or "").strip() for perm in perms if str(perm or "").strip()}
    if not wanted:
        return []
    try:
        from app.auth.models import User, list_effective_permission_keys_for_user
    except Exception:
        return []

    recipients: list[str] = []
    with get_session() as session:
        users = (
            session.query(User)
            .filter(User.is_active == True, User.is_approved == True)  # noqa: E712
            .order_by(User.id.asc())
            .all()
        )
        for user in users:
            email = str(getattr(user, "email", "") or "").strip()
            if not email:
                current_app.logger.info(
                    "returns.email_skip",
                    extra={"reason": "missing_email", "user_id": int(user.id), "permission_filter": sorted(wanted)},
                )
                continue
            if not _is_valid_email_address(email):
                current_app.logger.info(
                    "returns.email_skip",
                    extra={"reason": "invalid_email", "user_id": int(user.id), "email": email, "permission_filter": sorted(wanted)},
                )
                continue
            try:
                keys = set(list_effective_permission_keys_for_user(int(user.id), fallback_role=user.role))
            except Exception:
                keys = set()
            if "*" in keys or keys.intersection(wanted):
                recipients.append(email)
    return recipients


def _user_email(user_id: Any) -> str:
    uid = _int_or_none(user_id)
    if uid is None:
        return ""
    try:
        from app.auth.models import User
    except Exception:
        return ""
    with get_session() as session:
        user = session.get(User, int(uid))
        if not user:
            return ""
        return str(getattr(user, "email", "") or "").strip()


def _rma_submitter_recipients(rma_payload: dict[str, Any]) -> list[str]:
    recipients: list[str] = []
    submitter_email = _user_email(rma_payload.get("created_by_user_id") or rma_payload.get("rep_user_id"))
    if _is_valid_email_address(submitter_email):
        recipients.append(submitter_email)
    elif submitter_email:
        current_app.logger.info(
            "returns.email_skip",
            extra={"reason": "invalid_email", "email": submitter_email, "context": "submitter"},
        )
    fallback_email = str(rma_payload.get("customer_email") or "").strip()
    if _is_valid_email_address(fallback_email) and fallback_email.lower() not in {item.lower() for item in recipients}:
        recipients.append(fallback_email)
    elif fallback_email:
        current_app.logger.info(
            "returns.email_skip",
            extra={"reason": "invalid_email", "email": fallback_email, "context": "customer"},
        )
    return recipients


def _unique_emails(*groups: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for raw in group or ():
            email = str(raw or "").strip()
            key = email.lower()
            if not email or key in seen or not _is_valid_email_address(email):
                continue
            seen.add(key)
            out.append(email)
    return out


def _return_email_subject(rma_payload: dict[str, Any]) -> str:
    return (
        f"Return #{rma_payload.get('id')} — "
        f"{rma_payload.get('status_label') or status_label(rma_payload.get('status') or '')} — "
        f"{rma_payload.get('customer_name') or rma_payload.get('customer_id') or 'Customer'} — "
        f"Order {rma_payload.get('order_id') or '-'}"
    )


def _return_email_context(
    rma_payload: dict[str, Any],
    *,
    event_key: str,
    detail: str | None = None,
) -> dict[str, Any]:
    items = list(rma_payload.get("items") or [])
    total_packs = rma_payload.get("total_packs")
    if total_packs in (None, "", 0):
        total_packs = sum(int(item.get("packs_count") or 0) for item in items)
    return {
        "event_key": event_key,
        "event_label": {
            "return_submitted": "Return Submitted",
            "warehouse_approved": "Warehouse Approved",
            "manager_approved": "Manager Approved",
            "rejected": "Rejected",
            "comment_added": "Comment Added",
            "completed": "Completed",
        }.get(event_key, "Return Update"),
        "detail": str(detail or rma_payload.get("decision_summary") or "").strip(),
        "return_url": build_public_url(f"/returns/{int(rma_payload['id'])}"),
        "rma": rma_payload,
        "top_items": items[: min(len(items), 5)],
        "summary": {
            "total_credit_amount": round(float(rma_payload.get("total_credit_amount") or 0), 2),
            "total_weight_lb": round(float(rma_payload.get("total_weight_lb") or 0), 3),
            "total_packs": int(total_packs or 0),
            "primary_reason": str(rma_payload.get("primary_reason") or "").strip(),
            "primary_category": str(rma_payload.get("primary_category") or "").strip(),
            "primary_follow_up": str(rma_payload.get("primary_follow_up") or "").strip(),
            "rep_name": str(rma_payload.get("rep_name") or "").strip(),
            "date_submitted": str(rma_payload.get("date_submitted") or rma_payload.get("created_at") or "").strip(),
            "wh_approved_at": str(rma_payload.get("wh_approved_at") or "").strip(),
            "mgr_approved_at": str(rma_payload.get("mgr_approved_at") or "").strip(),
        },
    }


def render_email(template_name: str, context: dict[str, Any]) -> tuple[str | None, str]:
    base_template = str(template_name or "event").strip() or "event"
    try:
        text_body = render_template(f"emails/returns/{base_template}.txt", **context)
    except Exception:
        fallback_url = str(context.get("return_url") or "").strip()
        text_body = (
            f"{context.get('event_label', 'Return Update')}\n"
            f"Return #{context.get('rma', {}).get('id')} is now "
            f"{context.get('rma', {}).get('status_label') or context.get('rma', {}).get('status') or ''}.\n"
            f"{fallback_url}"
        ).strip()
    try:
        html_body = render_template(f"emails/returns/{base_template}.html", **context)
    except Exception:
        html_body = None
    return html_body, text_body


def _return_event_recipients(rma_payload: dict[str, Any], event_key: str) -> list[str]:
    warehouse_group = _active_user_emails_for_permissions(
        "returns.approve.wh",
        "returns.warehouse.receive",
        "returns.warehouse.scan",
        "admin.returns.manage",
    )
    manager_group = _active_user_emails_for_permissions(
        "returns.approve.mgr",
        "returns.ops.approve",
        "returns.ops.queue.view",
        "admin.returns.manage",
    )
    submitter_group = _rma_submitter_recipients(rma_payload)
    accounting_group = _list_config_emails("RETURNS_ACCOUNTING_EMAILS")
    if event_key == "return_submitted":
        return _unique_emails(submitter_group, warehouse_group)
    if event_key == "warehouse_approved":
        return _unique_emails(manager_group)
    if event_key == "manager_approved":
        return _unique_emails(submitter_group, accounting_group)
    if event_key == "rejected":
        return _unique_emails(submitter_group)
    if event_key == "comment_added":
        return _unique_emails(submitter_group, warehouse_group, manager_group)
    if event_key == "completed":
        return _unique_emails(submitter_group, accounting_group)
    return _unique_emails(submitter_group)


def _send_return_event_email(
    rma_payload: dict[str, Any],
    event_key: str,
    *,
    detail: str | None = None,
    attachments: Optional[list[dict[str, Any]]] = None,
) -> int:
    recipients = _return_event_recipients(rma_payload, event_key)
    if not recipients:
        return 0
    context = _return_email_context(rma_payload, event_key=event_key, detail=detail)
    html_body, text_body = render_email("event", context)
    return _send_rma_bulk_email(
        int(rma_payload["id"]),
        recipients,
        _return_email_subject(rma_payload),
        text_body,
        html_body=html_body,
        attachments=attachments,
    )


def _record_system_event(
    rma_id: int,
    *,
    event_type: str,
    actor_user_id: int | None = None,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    try:
        with get_session() as session:
            row = session.get(ReturnRMA, int(rma_id))
            if not row:
                return
            _add_event(
                session,
                rma_id=int(rma_id),
                event_type=event_type,
                from_status=row.status,
                to_status=row.status,
                actor_user_id=actor_user_id,
                payload=payload or {},
            )
            row.updated_at = utcnow()
            session.add(row)
            session.commit()
    except Exception:
        pass


def _send_bulk_email(
    recipients: Iterable[str],
    subject: str,
    text_body: str,
    *,
    html_body: str | None = None,
    attachments: Optional[list[dict[str, Any]]] = None,
) -> int:
    if bool(current_app.config.get("MAIL_SUPPRESS_SEND", False)):
        current_app.logger.info(
            "returns.email_skip",
            extra={"reason": "suppressed", "recipient_count": len(list(recipients)), "subject": subject},
        )
        return len(list(recipients))

    delivered = 0
    seen: set[str] = set()
    for raw in recipients:
        email = str(raw or "").strip()
        if not email or email.lower() in seen:
            continue
        if not _is_valid_email_address(email):
            current_app.logger.info(
                "returns.email_skip",
                extra={"reason": "invalid_email", "email": email, "subject": subject},
            )
            continue
        seen.add(email.lower())
        if send_email(email, subject, text_body, html_body=html_body, attachments=attachments, raise_on_error=False):
            delivered += 1
    return delivered


def _send_rma_bulk_email(
    rma_id: int,
    recipients: Iterable[str],
    subject: str,
    text_body: str,
    *,
    html_body: str | None = None,
    attachments: Optional[list[dict[str, Any]]] = None,
) -> int:
    delivered = _send_bulk_email(
        recipients,
        subject,
        text_body,
        html_body=html_body,
        attachments=attachments,
    )
    if delivered:
        _record_system_event(
            int(rma_id),
            event_type="email_sent",
            payload={"subject": subject, "delivered": delivered},
        )
    return delivered


def _pdf_escape(value: Any) -> str:
    text = str(value if value is not None else "")
    text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    safe = "".join(ch if 32 <= ord(ch) <= 126 else "?" for ch in text)
    return safe


def _build_simple_pdf(lines: list[str]) -> bytes:
    clean_lines = [_pdf_escape(line) for line in lines if str(line or "").strip()]
    if not clean_lines:
        clean_lines = ["Return document"]
    commands = ["BT", "/F1 11 Tf", "50 780 Td", "14 TL"]
    for idx, line in enumerate(clean_lines):
        if idx == 0:
            commands.append(f"({_pdf_escape(line)}) Tj")
        else:
            commands.append("T*")
            commands.append(f"({_pdf_escape(line)}) Tj")
    commands.append("ET")
    stream = "\n".join(commands).encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    parts = [b"%PDF-1.4\n"]
    offsets: list[int] = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(sum(len(part) for part in parts))
        parts.append(f"{index} 0 obj\n".encode("latin-1") + body + b"\nendobj\n")
    xref_start = sum(len(part) for part in parts)
    xref = [f"xref\n0 {len(objects) + 1}\n".encode("latin-1"), b"0000000000 65535 f \n"]
    for offset in offsets[1:]:
        xref.append(f"{offset:010d} 00000 n \n".encode("latin-1"))
    trailer = f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n".encode("latin-1")
    return b"".join(parts + xref + [trailer])


def _pdf_text(value: Any, max_length: int | None = None) -> str:
    text = str(value if value is not None else "")
    safe = text.encode("latin-1", "replace").decode("latin-1")
    if max_length and len(safe) > max_length:
        return safe[: max(0, max_length - 1)] + "..."
    return safe


def _build_structured_return_pdf(
    detail: dict[str, Any],
    *,
    generated_at: str,
    credit_po: bool = False,
) -> bytes:
    document_title = "Credit PO" if credit_po else "Return Form"
    try:
        from fpdf import FPDF
    except Exception:
        lines = [
            document_title,
            f"Return #{detail.get('id')}",
            f"Order: {detail.get('order_id')}",
            f"Customer: {detail.get('customer_name') or detail.get('customer_id')}",
            f"Status: {detail.get('status_label')}",
            f"Generated: {generated_at}",
        ]
        return _build_simple_pdf(lines)

    metadata = detail.get("metadata") if isinstance(detail.get("metadata"), dict) else {}
    advised_customer = metadata.get("advised_customer_note") or ("Yes" if detail.get("advised_customer") else "No")
    notes = detail.get("additional_notes") or detail.get("decision_summary") or "-"
    meta_rows = [
        ("Return ID", detail.get("rma_number") or f"RMA-{detail.get('id')}", "Status", detail.get("status_label") or detail.get("status") or "-"),
        ("Order #", detail.get("order_id") or "-", "Customer", detail.get("customer_name") or detail.get("customer_id") or "-"),
        ("Rep", detail.get("rep_name") or "-", "Submitted", detail.get("date_submitted") or detail.get("created_at") or "-"),
        ("Order Date", detail.get("order_date") or "-", "Shipped Date", detail.get("date_shipped") or "-"),
        ("Return Type", detail.get("return_type") or "-", "Advised Customer", advised_customer),
        ("Notes", notes, "Generated", generated_at),
    ]
    items = detail.get("items") if isinstance(detail.get("items"), list) else []
    total_weight = float(detail.get("total_weight_lb") or 0)
    total_credit = float(detail.get("total_credit_amount") or 0)

    pdf = FPDF(orientation="P", unit="mm", format="Letter")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    pdf.set_margins(10, 10, 10)
    pdf.set_fill_color(243, 244, 246)
    pdf.set_draw_color(209, 213, 219)
    
    # Enterprise Dynamic Branding
    company = str(detail.get("company") or "").strip().lower()
    if "alderwood" in company:
        logo_file = "wa-logo-badge.png"
        company_header = "Alderwood Grocers Meats"
    else:
        logo_file = "wa-logo-badge.png"
        company_header = "Northgate Retail Group"
        
    logo_path = Path(current_app.root_path) / "static" / "img" / logo_file
    top_y = pdf.get_y()
    if logo_path.exists():
        try:
            pdf.image(str(logo_path), x=10, y=top_y + 1, w=28)
        except Exception:
            pass
    pdf.set_xy(42, top_y + 1)
    pdf.set_text_color(150, 89, 81)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 7, _pdf_text(f"{company_header} - Credit PO / Return", 90), ln=1)
    pdf.set_x(42)
    pdf.set_text_color(75, 85, 99)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, _pdf_text(document_title, 90), ln=1)
    pdf.ln(4)
    pdf.set_text_color(31, 41, 55)

    label_width = 28
    value_width = 67
    pdf.set_font("Helvetica", "B", 9)
    for left_label, left_value, right_label, right_value in meta_rows:
        pdf.cell(label_width, 7, _pdf_text(left_label, 24), border=1, fill=True)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(value_width, 7, _pdf_text(left_value, 42), border=1)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(label_width, 7, _pdf_text(right_label, 24), border=1, fill=True)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(value_width, 7, _pdf_text(right_value, 42), border=1, ln=1)
        pdf.set_font("Helvetica", "B", 9)

    pdf.ln(4)

    headers = [
        ("Code", 12),
        ("Description", 30),
        ("Orig Wgt", 14),
        ("Adj Wgt", 14),
        ("Price/lb", 14),
        ("Credit %", 14),
        ("Credit Amt", 16),
        ("Return", 14),
        ("Outcome", 24),
        ("Follow-up", 31),
    ]
    pdf.set_font("Helvetica", "B", 7)
    for title, width in headers:
        pdf.cell(width, 7, _pdf_text(title, 18), border=1, fill=True)
    pdf.ln()

    pdf.set_font("Helvetica", "", 7)
    if items:
        for item in items:
            row_values = [
                _pdf_text(item.get("product_code") or item.get("sku") or "-", 10),
                _pdf_text(item.get("product_desc") or item.get("product_name") or "-", 24),
                f"{float(item.get('weight_lb') or 0):.3f}",
                f"{float(item.get('received_weight_lb')):.3f}" if item.get("received_weight_lb") is not None else "-",
                f"{float(item.get('price_per_lb') or 0):.2f}",
                f"{_coerce_credit_pct(item.get('credit_pct'), 100.0):.2f}",
                f"{float(item.get('credit_amount') or 0):.2f}",
                "Yes" if item.get("product_returning") else "No",
                _pdf_text(item.get("warehouse_outcome") or "-", 16),
                _pdf_text(item.get("follow_up_action") or "-", 20),
            ]
            for (header, width), value in zip(headers, row_values):
                pdf.cell(width, 6, value, border=1)
            pdf.ln()
    else:
        pdf.cell(sum(width for _title, width in headers), 7, "No line items.", border=1, ln=1)

    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(label_width, 7, "Total Weight", border=1, fill=True)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(value_width, 7, f"{total_weight:.3f} lb", border=1)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(label_width, 7, "Total Credit", border=1, fill=True)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(value_width, 7, f"{total_credit:.2f}", border=1, ln=1)

    pdf.ln(8)
    sign_width = (label_width + value_width) - 2
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(sign_width, 7, "Warehouse Reviewed By", border="T")
    pdf.cell(4, 7, "")
    pdf.cell(sign_width, 7, "Manager Digitally Approved By", border="T", ln=1)
    
    pdf.set_font("Helvetica", "", 8)
    pdf.cell(sign_width, 5, _pdf_text(detail.get("metadata", {}).get("wh_reviewer_name") or "-"))
    pdf.cell(4, 5, "")
    
    approval_text = "-"
    if detail.get("status") in [STATUS_APPROVED, STATUS_AWAITING_FINANCE, STATUS_COMPLETED]:
        approval_text = str(detail.get("approval_target") or "Authorized Manager")
    pdf.cell(sign_width, 5, _pdf_text(approval_text), ln=1)
    
    pdf.ln(10)
    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(156, 163, 175)
    audit_id = f"{detail.get('id')}-{str(detail.get('last_updated', '')).replace(':', '').replace('-', '').replace(' ', '')}"
    pdf.cell(0, 5, f"Enterprise Audit ID: {audit_id} | Generated for {detail.get('company', 'Northgate Retail Group')} Finance Review", ln=1)

    pdf_output = pdf.output(dest="S")
    return pdf_output.encode("latin-1") if isinstance(pdf_output, str) else bytes(pdf_output)


def _format_subject(template: str, rma_payload: dict[str, Any]) -> str:
    subject = str(template or "").strip()
    if not subject:
        return ""
    replacements = {
        "{{ rma_id }}": str(rma_payload.get("id") or ""),
        "{{ order_id }}": str(rma_payload.get("order_id") or ""),
        "{{ status }}": str(rma_payload.get("status_label") or rma_payload.get("status") or ""),
    }
    for needle, value in replacements.items():
        subject = subject.replace(needle, value)
    return subject


def _lookup_customer_exists_in_scope(customer_id: str, scope_payload: dict[str, Any]) -> bool:
    return str(customer_id or "").strip().lower() in _lookup_customers_in_scope([customer_id], scope_payload)


def _lookup_customers_in_scope(customer_ids: Iterable[str], scope_payload: dict[str, Any]) -> set[str]:
    normalized = sorted({str(item or "").strip().lower() for item in (customer_ids or []) if str(item or "").strip()})
    if not normalized:
        return set()
    cols = fact_store.list_columns()
    customer_col = fact_store.choose_column(("CustomerId", "CustomerID"), cols)
    if not customer_col:
        return set()
    scope_sql, scope_params = fact_store.build_scope_clause(scope_payload, cols)
    customer_sql = f"LOWER(CAST({fact_store.quote_identifier(customer_col)} AS VARCHAR))"
    visible: set[str] = set()
    chunk_size = 250
    for start in range(0, len(normalized), chunk_size):
        chunk = normalized[start : start + chunk_size]
        placeholders = ", ".join("?" for _ in chunk)
        sql = f"""
            SELECT DISTINCT {customer_sql} AS customer_id
            FROM fact
            WHERE ({scope_sql})
              AND {customer_sql} IN ({placeholders})
        """
        params = list(scope_params) + chunk
        df = fact_store.execute_sql_df(sql, params, tag="returns.scope.customer_lookup")
        if df.empty or "customer_id" not in df.columns:
            continue
        visible.update(str(value or "").strip().lower() for value in df["customer_id"].tolist() if str(value or "").strip())
    return visible


def can_access_customer(
    actor_user: Any,
    customer_id: str,
    *,
    scope: Optional[dict[str, Any]] = None,
) -> bool:
    payload = _scope_for_actor(actor_user, scope=scope)
    mode = str(payload.get("scope_mode") or "none").strip().lower()
    if mode == "all" or bool(payload.get("is_admin")):
        return True
    target = str(customer_id or "").strip().lower()
    if not target:
        return False
    allowed_customers = {
        str(item).strip().lower()
        for item in (payload.get("allowed_customer_ids") or payload.get("customer_ids") or [])
        if str(item).strip()
    }
    if allowed_customers:
        return target in allowed_customers
    if mode == "none":
        return False
    return _lookup_customer_exists_in_scope(target, payload)


def customer_in_scope(
    customer_id: str,
    scope: Optional[dict[str, Any]] = None,
    *,
    actor_user: Any = None,
) -> bool:
    return can_access_customer(actor_user, customer_id, scope=scope)


def assert_customer_in_scope(
    customer_id: str,
    scope: Optional[dict[str, Any]] = None,
    *,
    actor_user: Any = None,
) -> None:
    if not can_access_customer(actor_user, customer_id, scope=scope):
        raise ScopeViolationError("Customer is outside the current user's authorized scope.")


def apply_scope_filters(
    query: Any,
    *,
    scope: Optional[dict[str, Any]] = None,
    actor_user: Any = None,
) -> Any:
    payload = _scope_for_actor(actor_user, scope=scope)
    mode = str(payload.get("scope_mode") or "none").strip().lower()
    if mode == "all" or bool(payload.get("is_admin")):
        return query
    allowed_customers = {
        str(item).strip().lower()
        for item in (payload.get("allowed_customer_ids") or payload.get("customer_ids") or [])
        if str(item).strip()
    }
    if allowed_customers:
        return query.filter(func.lower(ReturnRMA.customer_id).in_(sorted(allowed_customers)))
    if mode == "none":
        return query.filter(false())
    return query


def search_orders(
    *,
    order_id: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    actor_user: Any = None,
    scope: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    return orders_provider.search_orders(
        order_id=order_id,
        email=email,
        phone=phone,
        scope=_scope_for_actor(actor_user, scope=scope),
        limit=100,
    )


def get_order(
    order_id: str,
    *,
    actor_user: Any = None,
    scope: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    return orders_provider.get_order(order_id, scope=_scope_for_actor(actor_user, scope=scope))


def lookup_order_for_return(
    order_id: str,
    *,
    actor_user: Any = None,
    scope: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    try:
        clean_order_id = orders_provider.normalize_order_id(order_id)
    except ValueError as exc:
        raise InvalidOrderLookupError(str(exc)) from exc

    lookup_started = time.perf_counter()
    payload_scope = _scope_for_actor(actor_user, scope=scope)
    detail = orders_provider.get_order_detail(clean_order_id, scope=payload_scope)
    if not detail:
        if orders_provider.order_exists(clean_order_id):
            raise ScopeViolationError(order_lookup_scope_error_message(actor_user=actor_user, scope=payload_scope))
        raise OrderLookupNotFoundError("Order not found.")

    order_payload = dict(detail.get("order") or {})
    order_payload["date_shipped"] = order_payload.get("date_shipped") or order_payload.get("order_date")
    items = [dict(item) for item in (detail.get("items") or [])]
    normalized_items: list[dict[str, Any]] = []
    for item in items:
        normalized = dict(item)
        normalized["product_code"] = normalized.get("product_code") or normalized.get("sku")
        normalized["product_desc"] = normalized.get("product_desc") or normalized.get("description") or normalized.get("product_name")
        weight_lb = _coerce_float(
            normalized.get("weight_lb"),
            _coerce_float(normalized.get("shipped_weight_lb"), _coerce_float(normalized.get("pack_weight_lb_sum"), 0.0)),
        )
        price_per_lb = _coerce_float(
            normalized.get("price_per_lb"),
            _coerce_float(
                normalized.get("unit_price_per_lb"),
                _coerce_float(normalized.get("unit_price"), _coerce_float(normalized.get("price"), 0.0)),
            ),
        )
        credit_pct = _coerce_credit_pct(normalized.get("credit_pct"), 100.0)
        normalized["weight_lb"] = weight_lb
        normalized["shipped_weight_lb"] = weight_lb
        normalized["price_per_lb"] = price_per_lb
        normalized["unit_price_per_lb"] = price_per_lb
        normalized["credit_pct"] = credit_pct
        normalized["credit_amount"] = _credit_amount_for_item(
            {
                "price_per_lb": price_per_lb,
                "weight_lb": weight_lb,
                "credit_pct": credit_pct,
            }
        )
        normalized_items.append(normalized)
    suggestions = suggestion_engine.suggest_returns(order_payload, normalized_items, payload_scope)
    result = {
        "order": order_payload,
        "items": normalized_items,
        "suggestions": suggestions,
        "meta": {
            "order_id": clean_order_id,
            "lookup_ms": round((time.perf_counter() - lookup_started) * 1000, 2),
            "scope_hash": payload_scope.get("scope_hash"),
            "dataset_version": fact_store.cache_buster(),
        },
    }
    current_app.logger.info(
        "returns.order_lookup",
        extra={
            "order_id": clean_order_id,
            "customer_id": order_payload.get("customer_id"),
            "item_count": len(items),
            "lookup_ms": result["meta"]["lookup_ms"],
            "actor_user_id": _actor_id(actor_user),
            "scope_hash": payload_scope.get("scope_hash"),
        },
    )
    return result


def list_rmas(
    *,
    status: str | None = None,
    statuses: Optional[Iterable[str]] = None,
    customer_id: str | None = None,
    rep: str | None = None,
    category: str | None = None,
    reason: str | None = None,
    company: str | None = None,
    approval_target: str | None = None,
    order_id: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
    actor_user: Any = None,
    scope: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    payload = _scope_for_actor(actor_user, scope=scope)
    with get_session() as session:
        query = session.query(ReturnRMA)
        query = apply_scope_filters(query, scope=payload)
        
        # Enterprise Filters
        if company:
            query = query.filter(ReturnRMA.company == company)
        if approval_target:
            query = query.filter(ReturnRMA.approval_target == approval_target)

        normalized_statuses: list[str] = []
        if statuses:
            normalized_statuses.extend(str(item or "").strip().lower() for item in statuses if str(item or "").strip())
        else:
            normalized_statuses.extend(_status_filter_values(status) or [])
        if normalized_statuses:
            query = query.filter(ReturnRMA.status.in_(normalized_statuses))
        if customer_id:
            query = query.filter(func.lower(ReturnRMA.customer_id) == str(customer_id).strip().lower())
        rep_needle = str(rep or "").strip().lower()
        if rep_needle:
            like = f"%{rep_needle}%"
            query = query.filter(func.lower(func.coalesce(ReturnRMA.rep_name, "")).like(like))
        category_needle = str(category or "").strip().lower()
        if category_needle:
            query = query.outerjoin(ReturnRMAItem, ReturnRMAItem.rma_id == ReturnRMA.id).filter(
                or_(
                    func.lower(func.coalesce(ReturnRMA.primary_category, "")) == category_needle,
                    func.lower(func.coalesce(ReturnRMAItem.category, "")) == category_needle,
                )
            )
        reason_needle = str(reason or "").strip().lower()
        if reason_needle:
            query = query.outerjoin(ReturnRMAItem, ReturnRMAItem.rma_id == ReturnRMA.id).filter(
                or_(
                    func.lower(func.coalesce(ReturnRMA.primary_reason, "")).like(f"%{reason_needle}%"),
                    func.lower(func.coalesce(ReturnRMAItem.reason_code, "")).like(f"%{reason_needle}%"),
                    func.lower(func.coalesce(ReturnRMAItem.reason_for_return, "")).like(f"%{reason_needle}%"),
                )
            )
        order_needle = str(order_id or "").strip().lower()
        if order_needle:
            query = query.filter(func.lower(func.coalesce(ReturnRMA.order_id, "")).like(f"%{order_needle}%"))
        start = _coerce_datetime(from_date)
        end = _coerce_datetime(to_date)
        if start:
            query = query.filter(func.coalesce(ReturnRMA.date_submitted, ReturnRMA.created_at) >= start)
        if end:
            query = query.filter(func.coalesce(ReturnRMA.date_submitted, ReturnRMA.created_at) <= end)
        needle = str(search or "").strip().lower()
        if needle:
            like = f"%{needle}%"
            query = query.outerjoin(ReturnRMAItem, ReturnRMAItem.rma_id == ReturnRMA.id).filter(
                or_(
                    func.lower(func.coalesce(ReturnRMA.order_id, "")).like(like),
                    func.lower(func.coalesce(ReturnRMA.customer_name, "")).like(like),
                    func.lower(func.coalesce(ReturnRMA.customer_id, "")).like(like),
                    func.lower(func.coalesce(ReturnRMAItem.sku, "")).like(like),
                    func.lower(func.coalesce(ReturnRMAItem.product_code, "")).like(like),
                    func.lower(func.coalesce(ReturnRMAItem.product_name, "")).like(like),
                    func.lower(func.coalesce(ReturnRMAItem.product_desc, "")).like(like),
                )
            ).distinct()
        elif category_needle or reason_needle:
            query = query.distinct()
        rows = query.order_by(ReturnRMA.created_at.desc(), ReturnRMA.id.desc()).all()
        mode = str(payload.get("scope_mode") or "none").strip().lower()
        is_admin_scope = bool(payload.get("is_admin"))
        visible_customers = {
            str(item).strip().lower()
            for item in (payload.get("allowed_customer_ids") or payload.get("customer_ids") or [])
            if str(item).strip()
        }
        if not is_admin_scope and mode not in {"all", "none"} and not visible_customers:
            visible_customers = _lookup_customers_in_scope((row.customer_id for row in rows), payload)
        out: list[dict[str, Any]] = []
        for row in rows:
            if is_admin_scope or mode == "all":
                out.append(_serialize_rma(row))
                continue
            if mode == "none":
                continue
            if str(row.customer_id or "").strip().lower() in visible_customers:
                out.append(_serialize_rma(row))
        return out


def paginate_rmas(
    *,
    page: int = 1,
    page_size: int = 25,
    **filters: Any,
) -> dict[str, Any]:
    """Return one tracker page without changing the full-row export contract."""
    try:
        requested_size = int(page_size or 25)
    except (TypeError, ValueError):
        requested_size = 25
    safe_size = min(100, max(10, requested_size))

    rows = list_rmas(**filters)
    total_rows = len(rows)
    total_pages = max(1, (total_rows + safe_size - 1) // safe_size)
    try:
        requested_page = int(page or 1)
    except (TypeError, ValueError):
        requested_page = 1
    safe_page = min(total_pages, max(1, requested_page))
    start_index = (safe_page - 1) * safe_size
    page_rows = rows[start_index : start_index + safe_size]

    return {
        "rows": page_rows,
        "page": safe_page,
        "page_size": safe_size,
        "total_rows": total_rows,
        "total_pages": total_pages,
        "start_row": start_index + 1 if total_rows else 0,
        "end_row": min(start_index + safe_size, total_rows),
        "has_previous": safe_page > 1,
        "has_next": safe_page < total_pages,
    }


def tracker_export_frame(
    *,
    status: str | None = None,
    statuses: Optional[Iterable[str]] = None,
    customer_id: str | None = None,
    rep: str | None = None,
    category: str | None = None,
    reason: str | None = None,
    company: str | None = None,
    approval_target: str | None = None,
    order_id: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
    actor_user: Any = None,
    scope: Optional[dict[str, Any]] = None,
) -> pd.DataFrame:
    rows = list_rmas(
        status=status,
        statuses=statuses,
        customer_id=customer_id,
        rep=rep,
        category=category,
        reason=reason,
        company=company,
        approval_target=approval_target,
        order_id=order_id,
        from_date=from_date,
        to_date=to_date,
        search=search,
        actor_user=actor_user,
        scope=scope,
    )
    export_rows = [
        {
            "RMA #": row.get("rma_number"),
            "Customer": row.get("customer_name") or row.get("customer_id"),
            "Order #": row.get("order_id"),
            "Rep": row.get("rep_name"),
            "Order Date": row.get("order_date"),
            "Date Submitted": row.get("date_submitted"),
            "Status": row.get("status_label"),
            "Total Credit": row.get("total_credit_amount"),
            "Total Weight": row.get("total_weight_lb"),
            "Total Packs": row.get("total_packs"),
            "Primary Reason": row.get("primary_reason"),
            "Category": row.get("primary_category"),
            "Follow-up": row.get("primary_follow_up"),
            "Rec/Prod Signoff": "Yes" if row.get("rec_prod_signoff") else "No",
            "Last Updated": row.get("last_updated"),
        }
        for row in rows
    ]
    return pd.DataFrame(export_rows)


def tracker_export_response(
    export_format: str,
    **filters: Any,
):
    fmt = str(export_format or "").strip().lower()
    frame = tracker_export_frame(**filters)
    if fmt == "csv":
        return dataframe_to_csv_response(frame, filename="returns_tracker.csv")
    return dataframes_to_xlsx_response({"Returns Tracker": frame}, filename="returns_tracker.xlsx")


def _analytics_frames(
    *,
    actor_user: Any = None,
    scope: Optional[dict[str, Any]] = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, pd.DataFrame]:
    payload = _scope_for_actor(actor_user, scope=scope)
    with get_session() as session:
        query = apply_scope_filters(session.query(ReturnRMA), scope=payload)
        start = _coerce_datetime(from_date)
        end = _coerce_datetime(to_date)
        if start:
            query = query.filter(func.coalesce(ReturnRMA.date_submitted, ReturnRMA.created_at) >= start)
        if end:
            query = query.filter(func.coalesce(ReturnRMA.date_submitted, ReturnRMA.created_at) <= end)
        rma_rows = query.all()
        rma_ids = [int(row.id) for row in rma_rows]
        item_rows: list[ReturnRMAItem] = []
        if rma_ids:
            item_rows = (
                session.query(ReturnRMAItem)
                .filter(ReturnRMAItem.rma_id.in_(rma_ids))
                .all()
            )

    headers = pd.DataFrame(
        [
            {
                "rma_id": int(row.id),
                "rma_number": row.rma_number or f"RMA-{int(row.id):06d}",
                "customer_id": row.customer_id,
                "customer_name": row.customer_name or row.customer_id,
                "rep_name": row.rep_name or "",
                "status": row.status,
                "status_label": status_label(row.status),
                "order_id": row.order_id,
                "order_date": _serialize_temporal(row.order_date),
                "date_submitted": pd.to_datetime(row.date_submitted or row.created_at),
                "wh_approved_at": pd.to_datetime(row.wh_approved_at),
                "mgr_approved_at": pd.to_datetime(row.mgr_approved_at),
                "fin_cleared_at": pd.to_datetime(row.fin_cleared_at),
                "closed_at": pd.to_datetime(
                    row.fin_cleared_at
                    or row.rejected_at
                    or row.mgr_approved_at
                    or (row.updated_at if row.status in {STATUS_COMPLETED, STATUS_APPROVED, STATUS_REJECTED} else None)
                ),
                "return_handling_cost": loads_json(row.metadata_json, {}).get("return_handling_cost"),
                "total_credit_amount": float(row.total_credit_amount or 0),
                "total_weight_lb": float(row.total_weight_lb or 0),
                "total_packs": int(row.total_packs or 0),
                "primary_reason": row.primary_reason or "",
                "primary_category": row.primary_category or "",
            }
            for row in rma_rows
        ]
    )
    items = pd.DataFrame(
        [
            {
                "rma_id": int(row.rma_id),
                "product_code": row.product_code or row.sku or "",
                "product_desc": row.product_desc or row.product_name or "",
                "credit_amount": float(row.credit_amount or 0),
                "qty": float(row.qty or 0),
                "weight_lb": float(row.weight_lb or 0),
                "reason_code": row.reason_code or "",
                "reason_for_return": row.reason_for_return or row.reason_code or "",
                "category": row.category or category_for_reason(row.reason_code, row.reason_for_return),
                "follow_up_action": row.follow_up_action or "",
                "supplier_credit": bool(row.supplier_credit),
                "supplier_credit_observed": bool(
                    loads_json(row.metadata_json, {}).get("supplier_credit_provided")
                ),
            }
            for row in item_rows
        ]
    )
    if headers.empty:
        empty = pd.DataFrame()
        return {
            "headers": empty,
            "volume_by_week": empty,
            "volume_by_month": empty,
            "credit_by_week": empty,
            "credit_by_month": empty,
            "top_customers": empty,
            "top_skus": empty,
            "reason_breakdown": empty,
            "category_breakdown": empty,
            "approval_funnel": empty,
            "approval_detail": empty,
            "approval_sla": empty,
            "supplier_credit": empty,
            "follow_up_breakdown": empty,
            "return_rate_basis": empty,
            "preventability": empty,
            "reason_pareto": empty,
            "sku_return_adjusted_margin": empty,
            "time_to_return": empty,
            "backlog_aging": empty,
            "workflow_sla": empty,
            "repeat_returners": empty,
            "customer_value_impact": empty,
        }

    headers["week"] = headers["date_submitted"].dt.to_period("W").astype(str)
    headers["month"] = headers["date_submitted"].dt.to_period("M").astype(str)

    header_records = headers.to_dict(orient="records")
    item_records = items.to_dict(orient="records")

    def _frame(rows: list[dict[str, Any]], columns: list[str]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame(columns=columns)
        return pd.DataFrame(rows, columns=columns)

    week_totals: dict[str, dict[str, Any]] = {}
    month_totals: dict[str, dict[str, Any]] = {}
    customer_totals: dict[tuple[str, str], dict[str, Any]] = {}
    for row in header_records:
        week_key = str(row.get("week") or "")
        month_key = str(row.get("month") or "")
        credit_total = float(row.get("total_credit_amount") or 0)
        weight_total = float(row.get("total_weight_lb") or 0)
        customer_id = str(row.get("customer_id") or "")
        customer_name = str(row.get("customer_name") or customer_id)

        week_bucket = week_totals.setdefault(week_key, {"week": week_key, "return_count": 0, "total_credit_amount": 0.0})
        week_bucket["return_count"] += 1
        week_bucket["total_credit_amount"] += credit_total

        month_bucket = month_totals.setdefault(month_key, {"month": month_key, "return_count": 0, "total_credit_amount": 0.0})
        month_bucket["return_count"] += 1
        month_bucket["total_credit_amount"] += credit_total

        customer_key = (customer_id, customer_name)
        customer_bucket = customer_totals.setdefault(
            customer_key,
            {
                "customer_id": customer_id,
                "customer_name": customer_name,
                "total_credit_amount": 0.0,
                "return_count": 0,
                "total_weight_lb": 0.0,
            },
        )
        customer_bucket["total_credit_amount"] += credit_total
        customer_bucket["return_count"] += 1
        customer_bucket["total_weight_lb"] += weight_total

    volume_by_week = _frame(
        sorted(
            ({"week": row["week"], "return_count": row["return_count"]} for row in week_totals.values()),
            key=lambda row: row["week"],
        ),
        ["week", "return_count"],
    )
    volume_by_month = _frame(
        sorted(
            ({"month": row["month"], "return_count": row["return_count"]} for row in month_totals.values()),
            key=lambda row: row["month"],
        ),
        ["month", "return_count"],
    )
    credit_by_week = _frame(
        sorted(
            (
                {
                    "week": row["week"],
                    "total_credit_amount": round(float(row["total_credit_amount"]), 2),
                }
                for row in week_totals.values()
            ),
            key=lambda row: row["week"],
        ),
        ["week", "total_credit_amount"],
    )
    credit_by_month = _frame(
        sorted(
            (
                {
                    "month": row["month"],
                    "total_credit_amount": round(float(row["total_credit_amount"]), 2),
                }
                for row in month_totals.values()
            ),
            key=lambda row: row["month"],
        ),
        ["month", "total_credit_amount"],
    )
    top_customers = _frame(
        sorted(
            customer_totals.values(),
            key=lambda row: (-float(row["total_credit_amount"]), -int(row["return_count"]), str(row["customer_name"])),
        ),
        ["customer_id", "customer_name", "total_credit_amount", "return_count", "total_weight_lb"],
    )

    if items.empty:
        top_skus = pd.DataFrame(columns=["product_code", "product_desc", "total_credit_amount", "return_count"])
        reason_breakdown = pd.DataFrame(columns=["reason_for_return", "reason_code", "total_credit_amount", "line_count"])
        category_breakdown = pd.DataFrame(columns=["category", "total_credit_amount", "line_count"])
        supplier_credit = pd.DataFrame(
            [{
                "supplier_credit_lines": 0,
                "observed_lines": 0,
                "unobserved_lines": 0,
                "total_lines": 0,
                "supplier_credit_pct": None,
            }]
        )
        follow_up_breakdown = pd.DataFrame(columns=["follow_up_action", "line_count", "total_credit_amount"])
    else:
        sku_totals: dict[tuple[str, str], dict[str, Any]] = {}
        reason_totals: dict[tuple[str, str], dict[str, Any]] = {}
        category_totals: dict[str, dict[str, Any]] = {}
        observed_credit_rows = [row for row in item_records if bool(row.get("supplier_credit_observed"))]
        supplier_credit_lines = sum(1 for row in observed_credit_rows if bool(row.get("supplier_credit")))
        observed_lines = len(observed_credit_rows)
        total_lines = len(item_records)
        supplier_credit = pd.DataFrame(
            [
                {
                    "supplier_credit_lines": supplier_credit_lines,
                    "observed_lines": observed_lines,
                    "unobserved_lines": total_lines - observed_lines,
                    "total_lines": total_lines,
                    "supplier_credit_pct": round(supplier_credit_lines / observed_lines, 4) if observed_lines else None,
                }
            ]
        )
        follow_up_totals: dict[str, dict[str, Any]] = {}
        for row in item_records:
            product_code = str(row.get("product_code") or "")
            product_desc = str(row.get("product_desc") or "")
            reason_text = str(row.get("reason_for_return") or "")
            reason_code = str(row.get("reason_code") or "")
            category = str(row.get("category") or "")
            follow_up_action = str(row.get("follow_up_action") or "").strip() or "Unspecified"
            credit_total = float(row.get("credit_amount") or 0)

            sku_key = (product_code, product_desc)
            sku_bucket = sku_totals.setdefault(
                sku_key,
                {
                    "product_code": product_code,
                    "product_desc": product_desc,
                    "total_credit_amount": 0.0,
                    "return_count": 0,
                },
            )
            sku_bucket["total_credit_amount"] += credit_total
            sku_bucket["return_count"] += 1

            reason_key = (reason_text, reason_code)
            reason_bucket = reason_totals.setdefault(
                reason_key,
                {
                    "reason_for_return": reason_text,
                    "reason_code": reason_code,
                    "total_credit_amount": 0.0,
                    "line_count": 0,
                },
            )
            reason_bucket["total_credit_amount"] += credit_total
            reason_bucket["line_count"] += 1

            category_bucket = category_totals.setdefault(
                category,
                {
                    "category": category,
                    "total_credit_amount": 0.0,
                    "line_count": 0,
                },
            )
            category_bucket["total_credit_amount"] += credit_total
            category_bucket["line_count"] += 1

            follow_up_bucket = follow_up_totals.setdefault(
                follow_up_action,
                {
                    "follow_up_action": follow_up_action,
                    "line_count": 0,
                    "total_credit_amount": 0.0,
                },
            )
            follow_up_bucket["line_count"] += 1
            follow_up_bucket["total_credit_amount"] += credit_total

        top_skus = _frame(
            sorted(
                sku_totals.values(),
                key=lambda row: (-float(row["total_credit_amount"]), -int(row["return_count"]), str(row["product_code"])),
            ),
            ["product_code", "product_desc", "total_credit_amount", "return_count"],
        )
        reason_breakdown = _frame(
            sorted(
                reason_totals.values(),
                key=lambda row: (-float(row["total_credit_amount"]), -int(row["line_count"]), str(row["reason_for_return"])),
            ),
            ["reason_for_return", "reason_code", "total_credit_amount", "line_count"],
        )
        category_breakdown = _frame(
            sorted(
                category_totals.values(),
                key=lambda row: (-float(row["total_credit_amount"]), -int(row["line_count"]), str(row["category"])),
            ),
            ["category", "total_credit_amount", "line_count"],
        )
        follow_up_breakdown = _frame(
            sorted(
                follow_up_totals.values(),
                key=lambda row: (-int(row["line_count"]), -float(row["total_credit_amount"]), str(row["follow_up_action"])),
            ),
            ["follow_up_action", "line_count", "total_credit_amount"],
        )

    approval_rows: list[dict[str, Any]] = []
    for row in rma_rows:
        pending_to_wh = None
        wh_to_mgr = None
        if row.date_submitted and row.wh_approved_at:
            pending_to_wh = round((row.wh_approved_at - row.date_submitted).total_seconds() / 3600, 2)
        if row.wh_approved_at and row.mgr_approved_at:
            wh_to_mgr = round((row.mgr_approved_at - row.wh_approved_at).total_seconds() / 3600, 2)
        approval_rows.append(
            {
                "rma_id": int(row.id),
                "rma_number": row.rma_number or f"RMA-{int(row.id):06d}",
                "pending_to_wh_hours": pending_to_wh,
                "wh_to_mgr_hours": wh_to_mgr,
            }
        )
    approval_detail = pd.DataFrame(approval_rows)
    funnel_order = [
        (STATUS_PENDING, "Pending"),
        (STATUS_WH_APPROVED, "WH Approved"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_COMPLETED, "Completed"),
    ]
    approval_funnel = pd.DataFrame(
        [
            {
                "status": status_key,
                "status_label": label,
                "return_count": int(sum(1 for value in headers["status"].tolist() if value == status_key)) if not headers.empty else 0,
            }
            for status_key, label in funnel_order
        ]
    )
    approval_sla = pd.DataFrame(
        [
            {
                "metric": "Pending -> WH Approved",
                "median_hours": round(float(approval_detail["pending_to_wh_hours"].dropna().median()), 2) if not approval_detail["pending_to_wh_hours"].dropna().empty else None,
                "p90_hours": round(float(approval_detail["pending_to_wh_hours"].dropna().quantile(0.9)), 2) if not approval_detail["pending_to_wh_hours"].dropna().empty else None,
            },
            {
                "metric": "WH Approved -> Approved",
                "median_hours": round(float(approval_detail["wh_to_mgr_hours"].dropna().median()), 2) if not approval_detail["wh_to_mgr_hours"].dropna().empty else None,
                "p90_hours": round(float(approval_detail["wh_to_mgr_hours"].dropna().quantile(0.9)), 2) if not approval_detail["wh_to_mgr_hours"].dropna().empty else None,
            },
        ]
    )

    depth_frames = _returns_depth_frames(headers, items, from_date=from_date, to_date=to_date)
    return {
        "headers": headers,
        "volume_by_week": volume_by_week,
        "volume_by_month": volume_by_month,
        "credit_by_week": credit_by_week,
        "credit_by_month": credit_by_month,
        "top_customers": top_customers,
        "top_skus": top_skus,
        "reason_breakdown": reason_breakdown,
        "category_breakdown": category_breakdown,
        "approval_funnel": approval_funnel,
        "approval_detail": approval_detail,
        "approval_sla": approval_sla,
        "supplier_credit": supplier_credit,
        "follow_up_breakdown": follow_up_breakdown,
        **depth_frames,
    }


def _returns_depth_frames(
    headers: pd.DataFrame,
    items: pd.DataFrame,
    *,
    from_date: str | None,
    to_date: str | None,
) -> dict[str, pd.DataFrame]:
    """Build supported Returns depth from captured workflow and sales facts."""
    names = (
        "return_rate_basis",
        "preventability",
        "reason_pareto",
        "sku_return_adjusted_margin",
        "time_to_return",
        "backlog_aging",
        "workflow_sla",
        "repeat_returners",
        "customer_value_impact",
    )
    if headers.empty:
        return {name: pd.DataFrame() for name in names}
    submitted = pd.to_datetime(headers["date_submitted"], errors="coerce").dropna()
    start = _coerce_date(from_date) or submitted.min().date()
    end = _coerce_date(to_date) or submitted.max().date()
    sales = _sales_window_basis(start, end, cause_type="reason", cause_value="all")
    sales_orders, sales_units, sales_revenue = (
        sales.get("order_count"),
        sales.get("units"),
        sales.get("revenue"),
    )
    returned_orders = int(headers["order_id"].dropna().astype(str).nunique())
    returned_units = (
        float(pd.to_numeric(items["qty"], errors="coerce").fillna(0).clip(lower=0).sum())
        if not items.empty else 0.0
    )
    return_credit = (
        float(pd.to_numeric(items["credit_amount"], errors="coerce").fillna(0).clip(lower=0).sum())
        if not items.empty else 0.0
    )
    rate_basis = pd.DataFrame([
        {
            "basis": "Orders", "returned": returned_orders, "sales_base": sales_orders,
            "rate_pct": metrics.return_rate(returned_orders, sales_orders),
            "definition": "Distinct returned order IDs / distinct sales order IDs",
        },
        {
            "basis": "Units", "returned": round(returned_units, 3), "sales_base": sales_units,
            "rate_pct": round(returned_units / float(sales_units) * 100, 2) if sales_units not in (None, 0) else None,
            "definition": "Captured returned quantity / shipped quantity",
        },
        {
            "basis": "Revenue", "returned": round(return_credit, 2), "sales_base": sales_revenue,
            "rate_pct": round(return_credit / float(sales_revenue) * 100, 2) if sales_revenue not in (None, 0) else None,
            "definition": "Return credit value / sales revenue",
        },
    ])

    preventability = pd.DataFrame()
    reason_pareto = pd.DataFrame()
    if not items.empty:
        line_work = items.copy()
        line_work["classification"] = line_work.apply(
            lambda row: "Preventable" if is_preventable_return(
                row.get("reason_code"), row.get("reason_for_return"), row.get("category")
            ) else "Non-preventable",
            axis=1,
        )
        joined = line_work.merge(headers[["rma_id", "order_id"]], on="rma_id", how="left")
        preventability = joined.groupby("classification", dropna=False).agg(
            return_orders=("order_id", "nunique"),
            line_count=("rma_id", "size"),
            credit_amount=("credit_amount", "sum"),
        ).reset_index()
        preventability["order_rate_pct"] = preventability["return_orders"].map(
            lambda value: metrics.return_rate(value, sales_orders)
        )
        line_work["reason"] = (
            line_work["reason_for_return"].replace("", pd.NA)
            .fillna(line_work["reason_code"].replace("", "Unspecified"))
        )
        reason_pareto = line_work.groupby("reason", dropna=False).agg(
            credit_amount=("credit_amount", "sum"), line_count=("rma_id", "size")
        ).reset_index().sort_values(
            ["credit_amount", "line_count", "reason"], ascending=[False, False, True]
        ).reset_index(drop=True)
        total_reason_credit = float(reason_pareto["credit_amount"].sum())
        reason_pareto["share_pct"] = reason_pareto["credit_amount"].map(
            lambda value: round(float(value) / total_reason_credit * 100, 1) if total_reason_credit else None
        )
        reason_pareto["cumulative_pct"] = reason_pareto["credit_amount"].cumsum().map(
            lambda value: round(float(value) / total_reason_credit * 100, 1) if total_reason_credit else None
        )

    timing = headers.copy()
    timing["order_date_parsed"] = pd.to_datetime(timing["order_date"], errors="coerce")
    timing["days_to_return"] = (timing["date_submitted"] - timing["order_date_parsed"]).dt.days
    valid_days = timing.loc[timing["days_to_return"].notna() & (timing["days_to_return"] >= 0), "days_to_return"]
    timing_buckets = [(-1, 7, "0–7 days"), (7, 14, "8–14 days"), (14, 30, "15–30 days"), (30, 60, "31–60 days"), (60, float("inf"), "61+ days")]
    time_to_return = pd.DataFrame([
        {
            "bucket": label,
            "return_count": int(((valid_days > low) & (valid_days <= high)).sum()),
            "share_pct": round(float(((valid_days > low) & (valid_days <= high)).sum()) / len(valid_days) * 100, 1) if len(valid_days) else None,
        }
        for low, high, label in timing_buckets
    ])

    open_rows = headers.loc[~headers["status"].isin({STATUS_COMPLETED, STATUS_REJECTED, STATUS_DENIED})].copy()
    open_rows["age_days"] = (
        pd.Timestamp(end) - pd.to_datetime(open_rows["date_submitted"], errors="coerce")
    ).dt.days.clip(lower=0)
    aging_buckets = [(-1, 2, "0–2 days"), (2, 7, "3–7 days"), (7, 14, "8–14 days"), (14, float("inf"), "15+ days")]
    backlog_aging = pd.DataFrame([
        {
            "bucket": label,
            "open_returns": int(((open_rows["age_days"] > low) & (open_rows["age_days"] <= high)).sum()),
            "credit_exposure": round(float(open_rows.loc[(open_rows["age_days"] > low) & (open_rows["age_days"] <= high), "total_credit_amount"].sum()), 2),
            "as_of": end.isoformat(),
        }
        for low, high, label in aging_buckets
    ])

    sla_specs = (
        ("Warehouse approval", "date_submitted", "wh_approved_at", 24.0),
        ("Manager approval", "wh_approved_at", "mgr_approved_at", 24.0),
        ("Refund / finance clearance", "mgr_approved_at", "fin_cleared_at", 48.0),
        ("End-to-end resolution", "date_submitted", "closed_at", 72.0),
    )
    sla_rows = []
    for label, start_col, end_col, target_hours in sla_specs:
        elapsed = (
            (pd.to_datetime(headers[end_col], errors="coerce") - pd.to_datetime(headers[start_col], errors="coerce"))
            .dt.total_seconds().div(3600).dropna()
        )
        elapsed = elapsed.loc[elapsed >= 0]
        sla_rows.append({
            "metric": label,
            "target_hours": target_hours,
            "completed_n": int(len(elapsed)),
            "median_hours": round(float(elapsed.median()), 2) if len(elapsed) else None,
            "p90_hours": round(float(elapsed.quantile(0.9)), 2) if len(elapsed) else None,
            "attainment_pct": round(float((elapsed <= target_hours).mean()) * 100, 1) if len(elapsed) else None,
        })
    workflow_sla = pd.DataFrame(sla_rows)

    customer = headers.groupby(["customer_id", "customer_name"], dropna=False).agg(
        return_count=("rma_id", "size"), credit_amount=("total_credit_amount", "sum")
    ).reset_index()
    total_customer_credit = float(customer["credit_amount"].sum())
    customer["credit_share_pct"] = customer["credit_amount"].map(
        lambda value: round(float(value) / total_customer_credit * 100, 1) if total_customer_credit else None
    )
    repeat_returners = customer.loc[customer["return_count"] > 1].sort_values(
        ["credit_amount", "return_count"], ascending=[False, False]
    ).reset_index(drop=True)

    sku_margin, customer_impact = _return_adjusted_value_frames(items, customer, start=start, end=end)
    return {
        "return_rate_basis": rate_basis,
        "preventability": preventability,
        "reason_pareto": reason_pareto,
        "sku_return_adjusted_margin": sku_margin,
        "time_to_return": time_to_return,
        "backlog_aging": backlog_aging,
        "workflow_sla": workflow_sla,
        "repeat_returners": repeat_returners,
        "customer_value_impact": customer_impact,
    }


def _return_adjusted_value_frames(
    items: pd.DataFrame,
    customer_returns: pd.DataFrame,
    *,
    start: date,
    end: date,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return SKU margin and customer value inputs; withhold when cost coverage is absent."""
    sku_margin = pd.DataFrame()
    customer_impact = pd.DataFrame()
    try:
        columns = fact_store.list_columns()
        revenue_col = next((name for name in ("Revenue", "GrossSales") if name in columns), None)
        cost_col = "Cost" if "Cost" in columns else None
        if not revenue_col or not cost_col:
            return sku_margin, customer_impact
        if "ProductId" in columns:
            sales_sku = fact_store.execute_sql_df(
                f"""SELECT CAST(ProductId AS VARCHAR) AS product_code,
                           SUM(CAST({revenue_col} AS DOUBLE)) AS sales_revenue,
                           SUM(CAST({cost_col} AS DOUBLE)) AS sales_cost
                    FROM fact
                    WHERE CAST(Date AS DATE) >= CAST(? AS DATE) AND CAST(Date AS DATE) <= CAST(? AS DATE)
                    GROUP BY 1""",
                [start.isoformat(), end.isoformat()],
                tag="returns.analytics.adjusted_margin",
            )
            returned = items.groupby("product_code", dropna=False).agg(
                return_credit=("credit_amount", "sum")
            ).reset_index() if not items.empty else pd.DataFrame(columns=["product_code", "return_credit"])
            sku_margin = sales_sku.merge(returned, on="product_code", how="left")
            sku_margin["return_credit"] = sku_margin["return_credit"].fillna(0.0)
            sku_margin["gross_margin_before_returns"] = sku_margin["sales_revenue"] - sku_margin["sales_cost"]
            sku_margin["return_adjusted_margin"] = sku_margin["gross_margin_before_returns"] - sku_margin["return_credit"]
            sku_margin["unprofitable_after_returns"] = sku_margin["return_adjusted_margin"] < 0
            sku_margin = sku_margin.sort_values(["return_adjusted_margin", "return_credit"], ascending=[True, False])
        if "CustomerId" in columns:
            sales_customer = fact_store.execute_sql_df(
                f"""SELECT CAST(CustomerId AS VARCHAR) AS customer_id,
                           SUM(CAST({revenue_col} AS DOUBLE)) AS sales_revenue,
                           SUM(CAST({cost_col} AS DOUBLE)) AS sales_cost
                    FROM fact
                    WHERE CAST(Date AS DATE) >= CAST(? AS DATE) AND CAST(Date AS DATE) <= CAST(? AS DATE)
                    GROUP BY 1""",
                [start.isoformat(), end.isoformat()],
                tag="returns.analytics.customer_value_impact",
            )
            customer_impact = sales_customer.merge(customer_returns, on="customer_id", how="left")
            customer_impact["credit_amount"] = customer_impact["credit_amount"].fillna(0.0)
            customer_impact["gross_profit_before_returns"] = customer_impact["sales_revenue"] - customer_impact["sales_cost"]
            customer_impact["gross_profit_after_returns"] = customer_impact["gross_profit_before_returns"] - customer_impact["credit_amount"]
            customer_impact["value_reduction_pct"] = customer_impact.apply(
                lambda row: round(float(row["credit_amount"]) / float(row["gross_profit_before_returns"]) * 100, 1)
                if float(row["gross_profit_before_returns"] or 0) > 0 else None,
                axis=1,
            )
            customer_impact = customer_impact.sort_values("credit_amount", ascending=False)
    except Exception:
        pass
    return sku_margin.reset_index(drop=True), customer_impact.reset_index(drop=True)


def _analytics_cache_key(
    *,
    actor_user: Any = None,
    scope: Optional[dict[str, Any]] = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> str:
    payload = _scope_for_actor(actor_user, scope=scope)
    scope_hash = str(payload.get("scope_hash") or "no-scope")
    user_id = _actor_id(actor_user) or "anon"
    start = str(from_date or "all").strip() or "all"
    end = str(to_date or "all").strip() or "all"
    return f"returns:analytics:v1:{user_id}:{scope_hash}:{start}:{end}"


def _returns_commercial_summary(
    headers: pd.DataFrame,
    *,
    from_date: str | None,
    to_date: str | None,
) -> dict[str, Any]:
    """Return rate, credit burden, and end-to-end resolution at governed grain."""
    def optional_number(value: Any) -> float | None:
        try:
            parsed = float(value)
            return parsed if pd.notna(parsed) else None
        except (TypeError, ValueError):
            return None

    if headers.empty:
        return {
            "orders": None,
            "net_sales": None,
            "return_rate_pct": None,
            "return_cost_rate_pct": None,
            "avg_resolution_hours": None,
            "resolved_returns": 0,
        }
    submitted = pd.to_datetime(headers["date_submitted"], errors="coerce").dropna()
    requested_start = _coerce_datetime(from_date)
    requested_end = _coerce_datetime(to_date)
    start = requested_start.date() if requested_start else submitted.min().date()
    end = requested_end.date() if requested_end else submitted.max().date()

    sales: dict[str, Any] = {}
    try:
        cols = fact_store.list_columns()
        gross_expr = "SUM(CAST(GrossSales AS DOUBLE))" if "GrossSales" in cols else "NULL::DOUBLE"
        discount_expr = "SUM(CAST(DiscountAmount AS DOUBLE))" if "DiscountAmount" in cols else "NULL::DOUBLE"
        discount_cost_expr = "SUM(CAST(DiscountHandlingCost AS DOUBLE))" if "DiscountHandlingCost" in cols else "NULL::DOUBLE"
        frame = fact_store.execute_sql_df(
            f"""
            SELECT COUNT(DISTINCT OrderId) AS orders,
                   {gross_expr} AS gross_sales,
                   {discount_expr} AS discounts,
                   {discount_cost_expr} AS discount_handling_cost
            FROM fact
            WHERE CAST(Date AS DATE) >= CAST(? AS DATE) AND CAST(Date AS DATE) <= CAST(? AS DATE)
            """,
            [start.isoformat(), end.isoformat()],
            tag="returns.analytics.commercial_basis",
        )
        sales = frame.iloc[0].to_dict() if not frame.empty else {}
    except Exception:
        sales = {}

    credits = float(pd.to_numeric(headers["total_credit_amount"], errors="coerce").fillna(0.0).sum())
    handling = pd.to_numeric(headers.get("return_handling_cost"), errors="coerce")
    return_handling_cost = (
        float(handling.sum()) if len(handling.index) == len(headers.index) and not handling.isna().any() else None
    )
    discount_cost = optional_number(sales.get("discount_handling_cost"))
    adjustment_costs = (
        discount_cost + return_handling_cost
        if discount_cost is not None and return_handling_cost is not None
        else None
    )
    reconciliation = metrics.net_sales_reconciliation(
        sales.get("gross_sales"), sales.get("discounts"), credits, adjustment_costs
    )
    pairs = [
        (row.date_submitted, row.closed_at)
        for row in headers.itertuples(index=False)
        if pd.notna(getattr(row, "closed_at", None))
    ]
    orders = int(sales.get("orders")) if optional_number(sales.get("orders")) is not None else None
    return {
        "orders": orders,
        "net_sales": reconciliation.get("net_sales"),
        "return_rate_pct": metrics.return_rate(len(headers.index), orders),
        "return_cost_rate_pct": metrics.return_cost_rate(credits, reconciliation.get("net_sales")),
        "avg_resolution_hours": metrics.average_resolution_hours(pairs),
        "resolved_returns": len(pairs),
    }


def returns_analytics_snapshot(
    *,
    actor_user: Any = None,
    scope: Optional[dict[str, Any]] = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    cache_key = _analytics_cache_key(actor_user=actor_user, scope=scope, from_date=from_date, to_date=to_date)
    try:
        cached = cache.get(cache_key)
    except Exception:
        cached = None
    if isinstance(cached, dict) and "summary" in cached and "frames" in cached:
        return cached

    frames = _analytics_frames(actor_user=actor_user, scope=scope, from_date=from_date, to_date=to_date)
    headers = frames["headers"]
    header_records = headers.to_dict(orient="records") if not headers.empty else []
    total_credit_amount = sum(float(row.get("total_credit_amount") or 0) for row in header_records)
    total_weight_lb = sum(float(row.get("total_weight_lb") or 0) for row in header_records)
    total_packs = sum(int(row.get("total_packs") or 0) for row in header_records)
    commercial = _returns_commercial_summary(headers, from_date=from_date, to_date=to_date)
    supplier_credit_row = (
        frames["supplier_credit"].iloc[0].to_dict()
        if not frames["supplier_credit"].empty
        else {"supplier_credit_pct": None, "observed_lines": 0, "total_lines": 0}
    )
    supplier_credit_ratio = supplier_credit_row.get("supplier_credit_pct")
    observed_credit_lines = int(supplier_credit_row.get("observed_lines") or 0)
    total_credit_lines = int(supplier_credit_row.get("total_lines") or 0)
    supplier_credit_pct = (
        round(float(supplier_credit_ratio) * 100, 1)
        if supplier_credit_ratio is not None and not pd.isna(supplier_credit_ratio)
        else None
    )
    supplier_credit_state = metrics.governed_metric_state(
        supplier_credit_pct,
        source_available=observed_credit_lines > 0,
        sample_size=observed_credit_lines,
    )
    supplier_credit_state.update(
        {
            "observed_lines": observed_credit_lines,
            "unobserved_lines": max(0, total_credit_lines - observed_credit_lines),
            "total_lines": total_credit_lines,
            "required_source": "Supplier-credit applicability on each return line",
        }
    )
    summary = {
        "total_returns": int(len(headers.index)) if not headers.empty else 0,
        "total_credit_amount": round(total_credit_amount, 2),
        "total_weight_lb": round(total_weight_lb, 3),
        "total_packs": int(total_packs),
        "supplier_credit_pct": supplier_credit_pct,
        "supplier_credit_state": supplier_credit_state,
        **commercial,
    }
    payload_out = {"summary": summary, "frames": frames}
    try:
        cache.set(cache_key, payload_out, timeout=int(current_app.config.get("CACHE_DEFAULT_TIMEOUT") or 300))
    except Exception:
        pass
    return payload_out


def returns_analytics_export_response(
    *,
    dataset: str | None = None,
    export_format: str = "xlsx",
    actor_user: Any = None,
    scope: Optional[dict[str, Any]] = None,
    from_date: str | None = None,
    to_date: str | None = None,
):
    frames = _analytics_frames(actor_user=actor_user, scope=scope, from_date=from_date, to_date=to_date)
    selected = str(dataset or "").strip()
    if selected and selected in frames:
        export_frames = {selected[:31]: frames[selected]}
    else:
        export_frames = {name[:31]: frame for name, frame in frames.items() if name != "headers"}
    if str(export_format or "").strip().lower() == "csv" and len(export_frames) == 1:
        frame = next(iter(export_frames.values()))
        return dataframe_to_csv_response(frame, filename=f"returns_{next(iter(export_frames.keys()))}.csv")
    return dataframes_to_xlsx_response(export_frames, filename="returns_analytics.xlsx")


CORRECTIVE_ACTION_TRANSITIONS: dict[str, set[str]] = {
    "open": {"in_progress", "cancelled"},
    "in_progress": {"pending_approval", "cancelled"},
    "pending_approval": {"approved", "in_progress", "cancelled"},
    "approved": {"resolved", "in_progress"},
    "resolved": set(),
    "cancelled": set(),
}
PREVENTABLE_RETURN_CATEGORIES = {"Production", "Warehouse"}
PREVENTABLE_RETURN_REASON_CODES = {
    "quality_issue",
    "damaged",
    "wrong_item",
    "short_issue",
    "vendor_return",
}


def is_preventable_return(
    reason_code: Any,
    reason_text: Any = None,
    category: Any = None,
) -> bool:
    """Classify operationally preventable returns using a visible, stable rule."""
    code = str(reason_code or "").strip().lower()
    resolved_category = _normalize_reason_category(
        category or category_for_reason(reason_code, reason_text)
    )
    return code in PREVENTABLE_RETURN_REASON_CODES or resolved_category in PREVENTABLE_RETURN_CATEGORIES


def _confidence_for_sample(sample_size: int) -> str:
    if int(sample_size) >= 20:
        return "high"
    if int(sample_size) >= 5:
        return "medium"
    return "low"


def _default_returns_window(
    *,
    actor_user: Any = None,
    scope: Optional[dict[str, Any]] = None,
) -> tuple[date, date]:
    payload = _scope_for_actor(actor_user, scope=scope)
    with get_session() as session:
        latest = (
            apply_scope_filters(session.query(func.max(ReturnRMA.date_submitted)), scope=payload)
            .scalar()
        )
    window_end = (latest.date() if isinstance(latest, datetime) else latest) or date.today()
    return window_end - timedelta(days=89), window_end


def _cause_matches_item(cause_type: str, cause_value: str, item: ReturnRMAItem) -> bool:
    wanted = str(cause_value or "").strip().casefold()
    if cause_type == "reason":
        values = (item.reason_code, item.reason_for_return)
    elif cause_type == "category":
        values = (item.category or category_for_reason(item.reason_code, item.reason_for_return),)
    elif cause_type == "sku":
        values = (item.product_code, item.sku)
    elif cause_type == "supplier":
        metadata = loads_json(item.metadata_json, {})
        values = (metadata.get("supplier_id"), metadata.get("supplier_name"))
    else:
        return False
    return any(str(value or "").strip().casefold() == wanted for value in values)


def _sales_window_basis(
    start: date,
    end: date,
    *,
    cause_type: str,
    cause_value: str,
) -> dict[str, float | int | None]:
    """Return governed sales denominators; missing columns stay missing instead of becoming zero."""
    try:
        columns = fact_store.list_columns()
        revenue_col = next((name for name in ("Revenue", "GrossSales") if name in columns), None)
        units_col = next((name for name in ("QuantityShipped", "QuantityOrdered") if name in columns), None)
        where = ["CAST(Date AS DATE) >= CAST(? AS DATE)", "CAST(Date AS DATE) <= CAST(? AS DATE)"]
        params: list[Any] = [start.isoformat(), end.isoformat()]
        if cause_type == "sku" and "ProductId" in columns:
            where.append("LOWER(CAST(ProductId AS VARCHAR)) = LOWER(CAST(? AS VARCHAR))")
            params.append(cause_value)
        elif cause_type == "supplier":
            supplier_col = next((name for name in ("SupplierId", "SupplierName") if name in columns), None)
            if supplier_col:
                where.append(f"LOWER(CAST({supplier_col} AS VARCHAR)) = LOWER(CAST(? AS VARCHAR))")
                params.append(cause_value)
        revenue_expr = f"SUM(CAST({revenue_col} AS DOUBLE))" if revenue_col else "NULL::DOUBLE"
        units_expr = f"SUM(CAST({units_col} AS DOUBLE))" if units_col else "NULL::DOUBLE"
        frame = fact_store.execute_sql_df(
            f"""
            SELECT COUNT(DISTINCT OrderId) AS order_count,
                   {units_expr} AS units,
                   {revenue_expr} AS revenue
            FROM fact
            WHERE {' AND '.join(where)}
            """,
            params,
            tag="returns.corrective_action.sales_basis",
        )
        if frame.empty:
            return {"order_count": None, "units": None, "revenue": None}
        raw = frame.iloc[0].to_dict()

        def optional_float(value: Any) -> float | None:
            try:
                parsed = float(value)
                return parsed if pd.notna(parsed) else None
            except (TypeError, ValueError):
                return None

        order_count = optional_float(raw.get("order_count"))
        return {
            "order_count": int(order_count) if order_count is not None else None,
            "units": optional_float(raw.get("units")),
            "revenue": optional_float(raw.get("revenue")),
        }
    except Exception:
        return {"order_count": None, "units": None, "revenue": None}


def returns_cause_measurement(
    *,
    cause_type: str,
    cause_value: str,
    start_date: date | str,
    end_date: date | str,
    actor_user: Any = None,
    scope: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Measure one cause against order, unit, and revenue denominators for a fixed window."""
    clean_type = str(cause_type or "").strip().lower()
    clean_value = str(cause_value or "").strip()
    start = _coerce_date(start_date)
    end = _coerce_date(end_date)
    if clean_type not in {"reason", "category", "sku", "supplier"}:
        raise ReturnsError("Root-cause type must be reason, category, SKU, or supplier.")
    if not clean_value:
        raise ReturnsError("Root-cause value is required.")
    if not start or not end or end < start:
        raise ReturnsError("A valid measurement window is required.")
    payload = _scope_for_actor(actor_user, scope=scope)
    with get_session() as session:
        header_rows = (
            apply_scope_filters(session.query(ReturnRMA), scope=payload)
            .filter(func.coalesce(ReturnRMA.date_submitted, ReturnRMA.created_at) >= datetime.combine(start, datetime.min.time()))
            .filter(func.coalesce(ReturnRMA.date_submitted, ReturnRMA.created_at) <= datetime.combine(end, datetime.max.time()))
            .all()
        )
        header_map = {int(row.id): row for row in header_rows}
        item_rows = (
            session.query(ReturnRMAItem)
            .filter(ReturnRMAItem.rma_id.in_(list(header_map)))
            .all()
            if header_map
            else []
        )
        matching = [item for item in item_rows if _cause_matches_item(clean_type, clean_value, item)]

    returned_order_ids = {
        str(header_map[int(item.rma_id)].order_id)
        for item in matching
        if int(item.rma_id) in header_map and header_map[int(item.rma_id)].order_id
    }
    return_count = len(returned_order_ids)
    returned_units = round(sum(max(float(item.qty or 0), 0.0) for item in matching), 3)
    credit_amount = round(sum(max(float(item.credit_amount or 0), 0.0) for item in matching), 2)
    sales = _sales_window_basis(start, end, cause_type=clean_type, cause_value=clean_value)
    orders = sales.get("order_count")
    units = sales.get("units")
    revenue = sales.get("revenue")
    order_rate = metrics.return_rate(return_count, orders)
    unit_rate = round(returned_units / float(units) * 100, 2) if units not in (None, 0) else None
    revenue_rate = round(credit_amount / float(revenue) * 100, 2) if revenue not in (None, 0) else None
    return {
        "cause_type": clean_type,
        "cause_value": clean_value,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "return_count": return_count,
        "returned_units": returned_units,
        "credit_amount": credit_amount,
        "order_count": orders,
        "sales_units": units,
        "sales_revenue": revenue,
        "order_rate_pct": order_rate,
        "unit_rate_pct": unit_rate,
        "revenue_rate_pct": revenue_rate,
        "confidence_label": _confidence_for_sample(return_count),
        "confidence_n": return_count,
    }


def returns_root_causes(
    *,
    actor_user: Any = None,
    scope: Optional[dict[str, Any]] = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict[str, Any]]:
    """Rank explainable causes by credit impact and retain drill-through filter state."""
    start, end = _default_returns_window(actor_user=actor_user, scope=scope)
    start = _coerce_date(from_date) or start
    end = _coerce_date(to_date) or end
    snapshot = returns_analytics_snapshot(
        actor_user=actor_user,
        scope=scope,
        from_date=start.isoformat(),
        to_date=end.isoformat(),
    )
    return root_causes_from_frames(snapshot.get("frames") or {}, start=start, end=end)


def root_causes_from_frames(
    frames: dict[str, pd.DataFrame],
    *,
    start: date | str,
    end: date | str,
) -> list[dict[str, Any]]:
    """Build the root-cause Pareto from an existing analytics snapshot."""
    start_date = _coerce_date(start)
    end_date = _coerce_date(end)
    if not start_date or not end_date:
        raise ReturnsError("A valid root-cause window is required.")
    causes: list[dict[str, Any]] = []
    specs = (
        ("reason", "reason_breakdown", "reason_code", "reason_for_return"),
        ("sku", "top_skus", "product_code", "product_desc"),
    )
    for cause_type, frame_name, value_key, label_key in specs:
        frame = frames.get(frame_name)
        if frame is None or frame.empty:
            continue
        for raw in frame.to_dict(orient="records"):
            value = str(raw.get(value_key) or raw.get(label_key) or "").strip()
            if not value:
                continue
            label = str(raw.get(label_key) or value).strip()
            sample_size = int(raw.get("line_count") or raw.get("return_count") or 0)
            causes.append(
                {
                    "cause_type": cause_type,
                    "cause_value": value,
                    "label": label,
                    "credit_amount": round(float(raw.get("total_credit_amount") or 0), 2),
                    "sample_size": sample_size,
                    "confidence_label": _confidence_for_sample(sample_size),
                    "preventable": (
                        is_preventable_return(value, label)
                        if cause_type == "reason"
                        else True
                    ),
                    "from": start_date.isoformat(),
                    "to": end_date.isoformat(),
                }
            )
    causes.sort(key=lambda row: (-float(row["credit_amount"]), -int(row["sample_size"]), str(row["label"])))
    total_impact = sum(float(row["credit_amount"]) for row in causes if row["cause_type"] == "reason")
    running = 0.0
    for row in causes:
        if row["cause_type"] == "reason":
            running += float(row["credit_amount"])
            row["pareto_cumulative_pct"] = round(running / total_impact * 100, 1) if total_impact else None
        else:
            row["pareto_cumulative_pct"] = None
    return causes


def returns_root_cause_detail(
    *,
    cause_type: str,
    cause_value: str,
    actor_user: Any = None,
    scope: Optional[dict[str, Any]] = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    start, end = _default_returns_window(actor_user=actor_user, scope=scope)
    start = _coerce_date(from_date) or start
    end = _coerce_date(to_date) or end
    measurement = returns_cause_measurement(
        cause_type=cause_type,
        cause_value=cause_value,
        start_date=start,
        end_date=end,
        actor_user=actor_user,
        scope=scope,
    )
    measurement["preventable"] = (
        is_preventable_return(cause_value, cause_value)
        if str(cause_type).lower() == "reason"
        else True
    )
    measurement["classification_rule"] = (
        "Preventable includes quality, damage, wrong-item, short/issue, and vendor-return reasons; "
        "customer-choice returns are non-preventable."
    )
    return measurement


def list_corrective_action_owners() -> list[dict[str, Any]]:
    with get_session() as session:
        rows = (
            session.query(User)
            .filter(User.is_active == True)  # noqa: E712
            .order_by(User.first_name.asc(), User.last_name.asc(), User.username.asc())
            .all()
        )
        return [{"id": int(row.id), "name": row.full_name, "role": row.role} for row in rows]


def _add_corrective_action_event(
    session: Any,
    action: ReturnCorrectiveAction,
    *,
    event_type: str,
    actor_user: Any = None,
    from_status: str | None = None,
    to_status: str | None = None,
    payload: Optional[dict[str, Any]] = None,
) -> ReturnCorrectiveActionEvent:
    row = ReturnCorrectiveActionEvent(
        corrective_action_id=int(action.id),
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        actor_user_id=_actor_id(actor_user),
        payload_json=dumps_json(payload or {}),
    )
    session.add(row)
    session.flush()
    return row


def _serialize_corrective_action(
    row: ReturnCorrectiveAction,
    *,
    owner_name: str | None = None,
    events: Optional[list[ReturnCorrectiveActionEvent]] = None,
) -> dict[str, Any]:
    payload = {
        "id": int(row.id),
        "cause_type": row.cause_type,
        "cause_value": row.cause_value,
        "title": row.title,
        "description": row.description or "",
        "owner_user_id": int(row.owner_user_id),
        "owner_name": owner_name or f"User {int(row.owner_user_id)}",
        "due_date": _serialize_temporal(row.due_date),
        "status": row.status,
        "source_filters": loads_json(row.source_filters_json, {}),
        "baseline": {
            "start": _serialize_temporal(row.baseline_start),
            "end": _serialize_temporal(row.baseline_end),
            "return_count": int(row.baseline_return_count or 0),
            "order_count": row.baseline_order_count,
            "rate_pct": row.baseline_rate_pct,
            "credit_amount": float(row.baseline_credit_amount or 0),
            "revenue": row.baseline_revenue,
        },
        "target_rate_pct": row.target_rate_pct,
        "outcome": {
            "start": _serialize_temporal(row.outcome_start),
            "end": _serialize_temporal(row.outcome_end),
            "return_count": row.outcome_return_count,
            "order_count": row.outcome_order_count,
            "rate_pct": row.outcome_rate_pct,
            "credit_amount": row.outcome_credit_amount,
            "revenue": row.outcome_revenue,
        },
        "realized_impact_amount": row.realized_impact_amount,
        "confidence_label": row.confidence_label,
        "confidence_n": int(row.confidence_n or 0),
        "approval_notes": row.approval_notes or "",
        "resolution_notes": row.resolution_notes or "",
        "created_by_user_id": row.created_by_user_id,
        "approved_by_user_id": row.approved_by_user_id,
        "resolved_by_user_id": row.resolved_by_user_id,
        "created_at": _serialize_temporal(row.created_at),
        "updated_at": _serialize_temporal(row.updated_at),
        "approved_at": _serialize_temporal(row.approved_at),
        "resolved_at": _serialize_temporal(row.resolved_at),
    }
    if events is not None:
        payload["events"] = [
            {
                "id": int(event.id),
                "event_type": event.event_type,
                "from_status": event.from_status,
                "to_status": event.to_status,
                "actor_user_id": event.actor_user_id,
                "payload": loads_json(event.payload_json, {}),
                "created_at": _serialize_temporal(event.created_at),
            }
            for event in events
        ]
    return payload


def create_corrective_action(
    *,
    cause_type: str,
    cause_value: str,
    title: str,
    owner_user_id: Any,
    due_date: date | str,
    baseline_start: date | str,
    baseline_end: date | str,
    description: str | None = None,
    target_rate_pct: Any = None,
    source_filters: Optional[dict[str, Any]] = None,
    actor_user: Any = None,
    scope: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    clean_title = str(title or "").strip()
    owner_id = _int_or_none(owner_user_id)
    due = _coerce_date(due_date)
    start = _coerce_date(baseline_start)
    end = _coerce_date(baseline_end)
    if not clean_title:
        raise ReturnsError("Corrective-action title is required.")
    if not owner_id or owner_id < 1:
        raise ReturnsError("A valid owner is required.")
    if not due:
        raise ReturnsError("A due date is required.")
    if not start or not end or end < start:
        raise ReturnsError("A valid baseline window is required.")
    baseline = returns_cause_measurement(
        cause_type=cause_type,
        cause_value=cause_value,
        start_date=start,
        end_date=end,
        actor_user=actor_user,
        scope=scope,
    )
    target = None
    if target_rate_pct not in (None, ""):
        try:
            target = float(target_rate_pct)
        except (TypeError, ValueError) as exc:
            raise ReturnsError("Target return rate must be numeric.") from exc
        if target < 0:
            raise ReturnsError("Target return rate cannot be negative.")
    elif baseline.get("order_rate_pct") is not None:
        target = round(float(baseline["order_rate_pct"]) * 0.75, 2)
    with get_session() as session:
        row = ReturnCorrectiveAction(
            cause_type=str(cause_type or "").strip().lower(),
            cause_value=str(cause_value or "").strip(),
            title=clean_title,
            description=str(description or "").strip() or None,
            owner_user_id=owner_id,
            due_date=due,
            status="open",
            source_filters_json=dumps_json(source_filters or {}),
            baseline_start=start,
            baseline_end=end,
            baseline_return_count=int(baseline["return_count"]),
            baseline_order_count=baseline.get("order_count"),
            baseline_rate_pct=baseline.get("order_rate_pct"),
            baseline_credit_amount=float(baseline["credit_amount"]),
            baseline_revenue=baseline.get("sales_revenue"),
            target_rate_pct=target,
            confidence_label=str(baseline["confidence_label"]),
            confidence_n=int(baseline["confidence_n"]),
            created_by_user_id=_actor_id(actor_user),
        )
        session.add(row)
        session.flush()
        _add_corrective_action_event(
            session,
            row,
            event_type="created",
            actor_user=actor_user,
            to_status="open",
            payload={"baseline": baseline, "target_rate_pct": target},
        )
        session.commit()
        session.refresh(row)
        return _serialize_corrective_action(row)


def list_corrective_actions(
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    with get_session() as session:
        query = session.query(ReturnCorrectiveAction)
        if status:
            query = query.filter(ReturnCorrectiveAction.status == str(status).strip().lower())
        rows = query.order_by(ReturnCorrectiveAction.created_at.desc(), ReturnCorrectiveAction.id.desc()).limit(max(1, min(int(limit), 500))).all()
        owner_ids = {int(row.owner_user_id) for row in rows}
        owners = {
            int(user.id): user.full_name
            for user in session.query(User).filter(User.id.in_(owner_ids)).all()
        } if owner_ids else {}
        return [_serialize_corrective_action(row, owner_name=owners.get(int(row.owner_user_id))) for row in rows]


def get_corrective_action(action_id: int) -> dict[str, Any] | None:
    with get_session() as session:
        row = session.get(ReturnCorrectiveAction, int(action_id))
        if not row:
            return None
        owner = session.get(User, int(row.owner_user_id))
        events = (
            session.query(ReturnCorrectiveActionEvent)
            .filter(ReturnCorrectiveActionEvent.corrective_action_id == int(row.id))
            .order_by(ReturnCorrectiveActionEvent.created_at.asc(), ReturnCorrectiveActionEvent.id.asc())
            .all()
        )
        return _serialize_corrective_action(row, owner_name=owner.full_name if owner else None, events=events)


def transition_corrective_action(
    action_id: int,
    *,
    target_status: str,
    notes: str | None = None,
    outcome_start: date | str | None = None,
    outcome_end: date | str | None = None,
    actor_user: Any = None,
    scope: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    target = str(target_status or "").strip().lower()
    with get_session() as session:
        row = session.get(ReturnCorrectiveAction, int(action_id))
        if not row:
            raise ReturnsError("Corrective action not found.")
        current = str(row.status or "open").strip().lower()
        if target not in CORRECTIVE_ACTION_TRANSITIONS.get(current, set()):
            raise InvalidTransitionError(f"Cannot move corrective action from {current} to {target}.")
        event_payload: dict[str, Any] = {"notes": str(notes or "").strip()}
        now = utcnow()
        if target == "approved":
            row.approved_by_user_id = _actor_id(actor_user)
            row.approved_at = now
            row.approval_notes = str(notes or "").strip() or None
        if target == "resolved":
            start = _coerce_date(outcome_start) or (row.baseline_end + timedelta(days=1))
            end = _coerce_date(outcome_end) or date.today()
            if end < start:
                raise ReturnsError("Outcome window must end on or after it starts.")
            outcome = returns_cause_measurement(
                cause_type=row.cause_type,
                cause_value=row.cause_value,
                start_date=start,
                end_date=end,
                actor_user=actor_user,
                scope=scope,
            )
            # get_session() is a scoped session. The measurement opens and
            # closes that same scope, so re-attach the action before writing
            # its outcome fields.
            session.add(row)
            row.outcome_start = start
            row.outcome_end = end
            row.outcome_return_count = int(outcome["return_count"])
            row.outcome_order_count = outcome.get("order_count")
            row.outcome_rate_pct = outcome.get("order_rate_pct")
            row.outcome_credit_amount = float(outcome["credit_amount"])
            row.outcome_revenue = outcome.get("sales_revenue")
            expected_credit = None
            if row.baseline_revenue not in (None, 0) and row.outcome_revenue is not None:
                expected_credit = float(row.baseline_credit_amount or 0) / float(row.baseline_revenue) * float(row.outcome_revenue)
            elif row.baseline_order_count not in (None, 0) and row.outcome_order_count is not None:
                expected_credit = float(row.baseline_credit_amount or 0) / float(row.baseline_order_count) * float(row.outcome_order_count)
            row.realized_impact_amount = (
                round(expected_credit - float(row.outcome_credit_amount or 0), 2)
                if expected_credit is not None
                else None
            )
            row.resolved_by_user_id = _actor_id(actor_user)
            row.resolved_at = now
            row.resolution_notes = str(notes or "").strip() or None
            event_payload["outcome"] = outcome
            event_payload["expected_credit_at_baseline_rate"] = expected_credit
            event_payload["realized_impact_amount"] = row.realized_impact_amount
        row.status = target
        row.updated_at = now
        _add_corrective_action_event(
            session,
            row,
            event_type="status_changed",
            actor_user=actor_user,
            from_status=current,
            to_status=target,
            payload=event_payload,
        )
        session.commit()
        session.refresh(row)
        return _serialize_corrective_action(row)


def resolved_corrective_action_summary(*, limit: int = 3) -> list[dict[str, Any]]:
    """Small Overview-ready contract for demonstrably closed Returns exceptions."""
    return list_corrective_actions(status="resolved", limit=limit)


def get_rma_detail(
    rma_id: int,
    *,
    actor_user: Any = None,
    scope: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    payload = _scope_for_actor(actor_user, scope=scope)
    with get_session() as session:
        row = session.get(ReturnRMA, int(rma_id))
        if not row:
            return None
        assert_customer_in_scope(row.customer_id, scope=payload, actor_user=actor_user)
        items = (
            session.query(ReturnRMAItem)
            .filter(ReturnRMAItem.rma_id == int(row.id))
            .order_by(ReturnRMAItem.id.asc())
            .all()
        )
        events = (
            session.query(ReturnEvent)
            .filter(ReturnEvent.rma_id == int(row.id))
            .order_by(ReturnEvent.created_at.asc(), ReturnEvent.id.asc())
            .all()
        )
        attachments = (
            session.query(ReturnAttachment)
            .filter(ReturnAttachment.rma_id == int(row.id))
            .order_by(ReturnAttachment.created_at.asc(), ReturnAttachment.id.asc())
            .all()
        )
        approval = (
            session.query(ReturnApproval)
            .filter(ReturnApproval.rma_id == int(row.id))
            .first()
        )
        comments = (
            session.query(ReturnComment)
            .filter(ReturnComment.rma_id == int(row.id))
            .order_by(ReturnComment.created_at.asc(), ReturnComment.id.asc())
            .all()
        )
        inspections = (
            session.query(ReturnInspection)
            .filter(ReturnInspection.rma_id == int(row.id))
            .order_by(ReturnInspection.created_at.asc(), ReturnInspection.id.asc())
            .all()
        )
        shipments = (
            session.query(ReturnShipment)
            .filter(ReturnShipment.rma_id == int(row.id))
            .order_by(ReturnShipment.created_at.asc(), ReturnShipment.id.asc())
            .all()
        )
        refunds = (
            session.query(ReturnRefund)
            .filter(ReturnRefund.rma_id == int(row.id))
            .order_by(ReturnRefund.created_at.asc(), ReturnRefund.id.asc())
            .all()
        )
        payload_out = _serialize_rma(row)
        payload_out["items"] = [_serialize_item(item) for item in items]
        payload_out["events"] = [_serialize_event(item) for item in events]
        payload_out["approval"] = _serialize_approval(approval)
        payload_out["attachments"] = [_serialize_attachment(item) for item in attachments]
        payload_out["comments"] = [_serialize_comment(item) for item in comments]
        payload_out["inspections"] = [_serialize_inspection(item) for item in inspections]
        payload_out["shipments"] = [_serialize_shipment(item) for item in shipments]
        payload_out["refunds"] = [_serialize_refund(item) for item in refunds]
        return payload_out


def add_comment(
    rma_id: int,
    *,
    body: str,
    actor_user: Any = None,
    scope: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    text_body = str(body or "").strip()
    if not text_body:
        raise ReturnsError("Comment text is required.")
    payload_scope = _scope_for_actor(actor_user, scope=scope)
    with get_session() as session:
        row = session.get(ReturnRMA, int(rma_id))
        if not row:
            raise ReturnsError("RMA not found.")
        assert_customer_in_scope(row.customer_id, scope=payload_scope, actor_user=actor_user)
        current = str(row.status or "").strip().lower()
        if returns_final_v1_enabled() and not _can_manage_workflow(actor_user) and current in {
            STATUS_APPROVED,
            STATUS_REJECTED,
            STATUS_COMPLETED,
            STATUS_DENIED,
        }:
            raise ReturnsError("Comments are locked after the return reaches a final state.")
        comment = ReturnComment(
            rma_id=int(rma_id),
            user_id=_actor_id(actor_user),
            body=text_body,
        )
        session.add(comment)
        session.flush()
        _add_event(
            session,
            rma_id=int(rma_id),
            event_type="comment_added",
            from_status=row.status,
            to_status=row.status,
            actor_user_id=_actor_id(actor_user),
            payload={"body": text_body[:200]},
        )
        row.updated_at = utcnow()
        session.add(row)
        session.commit()
        payload_out = _serialize_comment(comment)
    if returns_final_v1_enabled():
        detail_payload = get_rma_detail(int(rma_id), actor_user=actor_user, scope=payload_scope) or {}
        if detail_payload:
            _send_return_event_email(detail_payload, "comment_added", detail=text_body[:200])
    return payload_out


def save_attachment(
    *,
    rma_id: int,
    upload: Any,
    actor_user: Any = None,
    item_id: int | None = None,
    scope: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = _scope_for_actor(actor_user, scope=scope)
    with get_session() as session:
        owner = session.get(ReturnRMA, int(rma_id))
        if not owner:
            raise ReturnsError("RMA not found.")
        assert_customer_in_scope(owner.customer_id, scope=payload, actor_user=actor_user)
        current = str(owner.status or "").strip().lower()
        if returns_final_v1_enabled() and not _can_manage_workflow(actor_user) and current in {
            STATUS_APPROVED,
            STATUS_REJECTED,
            STATUS_COMPLETED,
            STATUS_DENIED,
        }:
            raise ReturnsError("Attachments are locked after the return reaches a final state.")
    filename = safe_upload_name(getattr(upload, "filename", "upload.bin"))
    data = upload.read() if hasattr(upload, "read") else bytes(upload)
    root = attachments_root() / str(int(rma_id))
    root.mkdir(parents=True, exist_ok=True)
    target = (root / filename).resolve()
    if root.resolve() not in target.parents:
        raise ReturnsError("Invalid attachment path.")
    target.write_bytes(data)
    rel_path = target.relative_to(attachments_root()).as_posix()
    mime = getattr(upload, "mimetype", None) or "application/octet-stream"
    with get_session() as session:
        row = ReturnAttachment(
            rma_id=int(rma_id),
            item_id=item_id,
            filename=filename,
            mimetype=str(mime),
            storage_path=rel_path,
            file_path=rel_path,
            mime=str(mime),
            size=len(data),
            uploaded_by_user_id=_actor_id(actor_user),
            uploaded_at=utcnow(),
        )
        session.add(row)
        owner = session.get(ReturnRMA, int(rma_id))
        if owner:
            owner.updated_at = utcnow()
            session.add(owner)
        _add_event(
            session,
            rma_id=int(rma_id),
            event_type="attachment_added",
            from_status=owner.status if owner else None,
            to_status=owner.status if owner else None,
            actor_user_id=_actor_id(actor_user),
            payload={"filename": filename, "item_id": item_id},
        )
        session.commit()
        payload_out = _serialize_attachment(row)
    current_app.logger.info(
        "returns.upload",
        extra={
            "rma_id": int(rma_id),
            "actor_user_id": _actor_id(actor_user),
            "item_id": item_id,
            "file_path": payload_out["file_path"],
        },
    )
    return payload_out


def update_receiving_review(
    rma_id: int,
    *,
    header_updates: Optional[dict[str, Any]] = None,
    item_updates: Optional[Iterable[dict[str, Any]]] = None,
    actor_user: Any = None,
    scope: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload_scope = _scope_for_actor(actor_user, scope=scope)
    updates = dict(header_updates or {})
    requested_items = [dict(item or {}) for item in (item_updates or [])]
    reason_lookup = _reason_lookup_map()
    with get_session() as session:
        row = session.get(ReturnRMA, int(rma_id))
        if not row:
            raise ReturnsError("RMA not found.")
        assert_customer_in_scope(row.customer_id, scope=payload_scope, actor_user=actor_user)
        current = str(row.status or "").strip().lower()
        if current in {STATUS_REJECTED, STATUS_DENIED, STATUS_COMPLETED}:
            raise InvalidTransitionError(f"Receiving updates are not allowed when status is {current}.")

        items = (
            session.query(ReturnRMAItem)
            .filter(ReturnRMAItem.rma_id == int(rma_id))
            .order_by(ReturnRMAItem.id.asc())
            .all()
        )
        item_map = {int(item.id): item for item in items}
        changed_item_ids: list[int] = []
        for payload in requested_items:
            item_id = _int_or_none(payload.get("item_id"))
            if item_id is None:
                continue
            item = item_map.get(int(item_id))
            if not item:
                continue
            
            # Enterprise Audit Logging for line-level changes
            _log_field_change(session, int(rma_id), f"item_{item.id}_pack_barcode", item.pack_barcode, payload.get("pack_barcode"), actor_user=actor_user)
            _log_field_change(session, int(rma_id), f"item_{item.id}_packs_count", item.packs_count, payload.get("packs_count"), actor_user=actor_user)
            _log_field_change(session, int(rma_id), f"item_{item.id}_follow_up", item.follow_up_action, payload.get("follow_up_action"), actor_user=actor_user)
            _log_field_change(session, int(rma_id), f"item_{item.id}_outcome", item.warehouse_outcome, payload.get("warehouse_outcome"), actor_user=actor_user)
            _log_field_change(session, int(rma_id), f"item_{item.id}_weight_lb", item.received_weight_lb, payload.get("received_weight_lb"), actor_user=actor_user)

            item.pack_barcode = str(payload.get("pack_barcode") or "").strip() or None
            item.packs_count = _coerce_int(payload.get("packs_count"), item.packs_count or 0)
            item.follow_up_action = str(payload.get("follow_up_action") or "").strip() or None
            item.warehouse_outcome = str(payload.get("warehouse_outcome") or "").strip() or None
            item.received_weight_lb = _coerce_float(payload.get("received_weight_lb"))
            if item.received_weight_lb is not None:
                # Recalculate credit amount based on adjusted weight
                item_payload = _serialize_item(item)
                item_payload["received_weight_lb"] = item.received_weight_lb
                item.credit_amount = _credit_amount_for_item(item_payload)
            if "supplier_credit" in payload:
                item.supplier_credit = _coerce_bool(payload.get("supplier_credit"))
                item_metadata = loads_json(item.metadata_json, {})
                item_metadata["supplier_credit_provided"] = True
                item.metadata_json = dumps_json(item_metadata)
            item.receiving_notes = str(payload.get("receiving_notes") or "").strip() or None
            session.add(item)
            changed_item_ids.append(int(item.id))

        if "rec_prod_signoff" in updates:
            signoff = _coerce_bool(updates.get("rec_prod_signoff"))
            row.rec_prod_signoff = signoff
            if signoff:
                row.rec_prod_signed_by_user_id = row.rec_prod_signed_by_user_id or _actor_id(actor_user)
                row.rec_prod_signed_at = (
                    _coerce_datetime(updates.get("rec_prod_signed_at"))
                    or row.rec_prod_signed_at
                    or utcnow()
                )
            else:
                row.rec_prod_signed_by_user_id = None
                row.rec_prod_signed_at = None

        item_payloads = [_serialize_item(item) for item in items]
        receiving_notes = (
            str(updates.get("receiving_notes")).strip()
            if "receiving_notes" in updates and updates.get("receiving_notes") is not None
            else loads_json(row.metadata_json, {}).get("receiving_notes", "")
        )
        _apply_rma_rollups(
            row,
            item_payloads,
            reason_lookup=reason_lookup,
            metadata_updates={"receiving_notes": receiving_notes},
        )
        row.updated_at = utcnow()
        session.add(row)
        _add_event(
            session,
            rma_id=int(rma_id),
            event_type="receiving_updated",
            from_status=current,
            to_status=current,
            actor_user_id=_actor_id(actor_user),
            payload={
                "item_ids": changed_item_ids,
                "rec_prod_signoff": bool(row.rec_prod_signoff),
                "receiving_notes": receiving_notes,
            },
        )
        session.commit()

    current_app.logger.info(
        "returns.receiving_updated",
        extra={
            "rma_id": int(rma_id),
            "actor_user_id": _actor_id(actor_user),
            "item_count": len(changed_item_ids),
        },
    )
    return get_rma_detail(int(rma_id), actor_user=actor_user, scope=payload_scope) or {}


def complete_rma(
    rma_id: int,
    *,
    actor_user: Any = None,
    notes: str | None = None,
    scope: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    detail = transition_return(
        rma_id,
        STATUS_COMPLETED,
        actor_user=actor_user,
        event_type="completed",
        payload={"notes": str(notes or "").strip()},
        scope=scope,
    )
    if returns_final_v1_enabled():
        _send_return_event_email(detail, "completed", detail=str(notes or "").strip() or None)
    current_app.logger.info(
        "returns.completed",
        extra={"rma_id": int(rma_id), "actor_user_id": _actor_id(actor_user)},
    )
    return detail


def _send_notification_email(rma_payload: dict[str, Any], event_key: str, *, detail: str | None = None) -> bool:
    recipients = _rma_submitter_recipients(rma_payload)
    if not recipients:
        return False
    subject_map = {
        "return_requested": f"Return requested for order {rma_payload['order_id']}",
        "evidence_required": f"Evidence required for return #{rma_payload['id']}",
        "approved": f"Return #{rma_payload['id']} approved",
        "denied": f"Return #{rma_payload['id']} denied",
        "label_issued": f"Shipping label issued for return #{rma_payload['id']}",
        "refund_processed": f"Refund processed for return #{rma_payload['id']}",
        "status_update": f"Return #{rma_payload['id']} updated",
    }
    context = {
        "rma": rma_payload,
        "event_key": event_key,
        "detail": detail or rma_payload.get("decision_summary") or "",
        "status_label": status_label(rma_payload.get("status") or ""),
        "return_url": build_public_url(f"/returns/{rma_payload['id']}"),
    }
    try:
        text_body = render_template("emails/returns/update.txt", **context)
        html_body = render_template("emails/returns/update.html", **context)
    except Exception:
        text_body = f"Return #{rma_payload['id']} is now {status_label(rma_payload.get('status') or '')}."
        html_body = None
    delivered = _send_rma_bulk_email(
        int(rma_payload["id"]),
        recipients,
        subject_map.get(event_key, subject_map["status_update"]),
        text_body,
        html_body=html_body,
    )
    return bool(delivered)


def create_rma(
    *,
    order_payload: dict[str, Any],
    item_payloads: list[dict[str, Any]],
    actor_user: Any = None,
    uploads: Optional[Iterable[Any]] = None,
    workflow_mode: str | None = None,
) -> dict[str, Any]:
    if not item_payloads:
        raise ReturnsError("At least one return item is required.")
    order_id = str(order_payload.get("order_id") or "").strip()
    if not order_id:
        raise ReturnsError("Order ID is required.")
    customer_id = str(order_payload.get("customer_id") or "").strip()
    if not customer_id:
        raise ReturnsError("Customer ID is required.")
    assert_customer_in_scope(customer_id, actor_user=actor_user)
    reason_lookup = _reason_lookup_map()
    normalized_items: list[dict[str, Any]] = []
    for idx, raw_item in enumerate(item_payloads, start=1):
        item = dict(raw_item or {})
        item["price_per_lb"] = _coerce_float(item.get("price_per_lb"), _coerce_float(item.get("price"), 0.0))
        item["weight_lb"] = _coerce_float(item.get("weight_lb"), _coerce_float(item.get("qty"), 0.0))
        item["product_code"] = str(item.get("product_code") or item.get("sku") or "").strip() or None
        item["product_desc"] = str(item.get("product_desc") or item.get("product_name") or "").strip() or None
        item["credit_pct"] = _coerce_credit_pct(item.get("credit_pct"), 100.0)
        item["credit_amount"] = _credit_amount_for_item(item)
        item["qty"] = _coerce_float(item.get("qty"), item["weight_lb"] or 0.0)
        item["price"] = _coerce_float(item.get("price"), item["price_per_lb"] or 0.0)
        if not item["product_code"]:
            raise ReturnsError(f"Item {idx}: product code is required.")
        if not item["product_desc"]:
            raise ReturnsError(f"Item {idx}: product description is required.")
        if float(item["weight_lb"]) <= 0:
            raise ReturnsError(f"Item {idx}: weight must be greater than zero.")
        if float(item["price_per_lb"]) < 0:
            raise ReturnsError(f"Item {idx}: price per lb cannot be negative.")
        item["packs_count"] = _coerce_int(item.get("packs_count") or item.get("packs") or 0, 0)
        item["pack_barcode"] = str(item.get("pack_barcode") or "").strip() or None
        item["reason_code"] = str(item.get("reason_code") or "").strip() or None
        item["reason_for_return"] = str(item.get("reason_for_return") or item.get("reason_code") or "").strip() or None
        item["category"] = str(item.get("category") or "").strip() or category_for_reason(
            item.get("reason_code"),
            item.get("reason_for_return"),
            lookup=reason_lookup,
        )
        item["follow_up_action"] = str(item.get("follow_up_action") or "Credit").strip() or "Credit"
        item["receiving_notes"] = str(item.get("receiving_notes") or "").strip() or None
        item["product_returning_provided"] = bool(
            item.get("product_returning_provided")
            if item.get("product_returning_provided") is not None
            else ("product_returning" in item)
        )
        item["product_returning"] = _coerce_bool(item.get("product_returning", True))
        item["supplier_credit_provided"] = bool(
            item.get("supplier_credit_provided")
            if item.get("supplier_credit_provided") is not None
            else ("supplier_credit" in item)
        )
        item["supplier_credit"] = _coerce_bool(item.get("supplier_credit"))
        normalized_items.append(item)
    if returns_final_v1_enabled():
        _validate_submission_payload(order_payload, normalized_items)
    upload_list = list(uploads or [])
    policy = active_policy()
    risk_score = _compute_risk_score(normalized_items)
    rollups = _summarize_item_rollups(normalized_items, reason_lookup=reason_lookup)
    selected_mode = str(workflow_mode or order_payload.get("workflow_mode") or "policy").strip().lower()
    if returns_final_v1_enabled():
        selected_mode = "final_v1"
        status = STATUS_AWAITING_OPS
        summary = "Awaiting operations coordination."
    elif selected_mode == "legacy":
        status = STATUS_PENDING
        summary = "Pending warehouse approval."
    else:
        status, summary = _initial_status(
            item_payloads=normalized_items,
            attachment_count=len(upload_list),
            rules=policy.get("rules") or {},
            risk_score=risk_score,
        )
    submitted_at = _coerce_datetime(order_payload.get("date_submitted")) or _vancouver_now()
    advised_customer_note = str(order_payload.get("advised_customer_note") or "").strip()
    receiving_notes = str(order_payload.get("receiving_notes") or "").strip()
    company = str(order_payload.get("company") or "").strip() or "Northgate Retail Group"
    with get_session() as session:
        row = ReturnRMA(
            customer_id=customer_id,
            customer_name=str(order_payload.get("customer_name") or "").strip() or None,
            customer_email=str(order_payload.get("customer_email") or "").strip() or None,
            customer_phone=str(order_payload.get("customer_phone") or "").strip() or None,
            order_id=order_id,
            order_date=_coerce_date(order_payload.get("order_date")),
            rep_user_id=_int_or_none(order_payload.get("rep_user_id")) or _actor_id(actor_user),
            rep_name=str(order_payload.get("rep_name") or _actor_name(actor_user) or "").strip() or None,
            date_submitted=submitted_at,
            date_shipped=_coerce_datetime(order_payload.get("date_shipped")),
            return_type=str(order_payload.get("return_type") or "").strip() or None,
            advised_customer=_coerce_bool(order_payload.get("advised_customer")),
            additional_notes=str(order_payload.get("additional_notes") or order_payload.get("notes") or "").strip() or None,
            total_credit_amount=rollups["total_credit_amount"],
            total_weight_lb=rollups["total_weight_lb"],
            total_packs=rollups["total_packs"],
            primary_reason=rollups["primary_reason"],
            primary_category=rollups["primary_category"],
            rec_prod_signoff=_coerce_bool(order_payload.get("rec_prod_signoff")),
            rec_prod_signed_by_user_id=_int_or_none(order_payload.get("rec_prod_signed_by_user_id")),
            rec_prod_signed_at=_coerce_datetime(order_payload.get("rec_prod_signed_at")),
            status=status,
            company=company,
            created_by_user_id=_actor_id(actor_user),
            decision_summary=summary,
            policy_version=str(policy.get("version") or "1"),
            risk_score=risk_score,
            metadata_json=dumps_json(
                {
                    "source": order_payload.get("source") or "returns_portal",
                    "ai_enabled": ai_enabled(),
                    "workflow_mode": selected_mode,
                    "ship_to": order_payload.get("ship_to") or "",
                    "primary_follow_up": rollups["primary_follow_up"],
                    "advised_customer_note": advised_customer_note,
                    "receiving_notes": receiving_notes,
                }
            ),
        )
        session.add(row)
        session.flush()
        if not row.rma_number:
            row.rma_number = f"RMA-{int(row.id):06d}"
            session.add(row)
        _add_event(
            session,
            rma_id=int(row.id),
            event_type="created",
            from_status=None,
            to_status=status,
            actor_user_id=_actor_id(actor_user),
            payload={
                "item_count": len(normalized_items),
                "workflow_mode": selected_mode,
                "total_credit_amount": rollups["total_credit_amount"],
                "total_weight_lb": rollups["total_weight_lb"],
                "total_packs": rollups["total_packs"],
            },
        )
        if selected_mode != "legacy" and status != STATUS_REQUESTED:
            _add_event(
                session,
                rma_id=int(row.id),
                event_type="decision",
                from_status=STATUS_REQUESTED,
                to_status=status,
                actor_user_id=_actor_id(actor_user),
                payload={"decision_summary": summary, "risk_score": risk_score},
            )
        for item in normalized_items:
            session.add(
                ReturnRMAItem(
                    rma_id=int(row.id),
                    order_line_id=str(item.get("order_line_id") or "").strip() or None,
                    sku=str(item.get("sku") or item.get("product_code") or "").strip() or None,
                    product_name=str(item.get("product_name") or item.get("product_desc") or "").strip() or None,
                    product_code=str(item.get("product_code") or item.get("sku") or "").strip() or None,
                    product_desc=str(item.get("product_desc") or item.get("product_name") or "").strip() or None,
                    price_per_lb=float(item.get("price_per_lb") or item.get("price") or 0),
                    weight_lb=float(item.get("weight_lb") or item.get("qty") or 0),
                    credit_pct=_coerce_credit_pct(item.get("credit_pct"), 100.0),
                    credit_amount=float(item.get("credit_amount") or _credit_amount_for_item(item)),
                    product_returning=_coerce_bool(item.get("product_returning", True)),
                    packs_count=_coerce_int(item.get("packs_count") or 0, 0),
                    pack_barcode=str(item.get("pack_barcode") or "").strip() or None,
                    reason_for_return=str(item.get("reason_for_return") or item.get("reason_code") or "").strip() or None,
                    follow_up_action=str(item.get("follow_up_action") or "").strip() or None,
                    category=str(item.get("category") or "").strip() or None,
                    supplier_credit=_coerce_bool(item.get("supplier_credit")),
                    qty=float(item.get("qty") or 0),
                    price=float(item.get("price") or item.get("price_per_lb") or 0),
                    reason_code=str(item.get("reason_code") or "").strip() or None,
                    item_condition=str(item.get("condition") or "").strip() or None,
                    notes=str(item.get("notes") or "").strip() or None,
                    receiving_notes=str(item.get("receiving_notes") or "").strip() or None,
                    metadata_json=dumps_json(
                        {
                            **(item.get("metadata") or {}),
                            "supplier_credit_provided": bool(item.get("supplier_credit_provided")),
                        }
                    ),
                )
            )
        row.updated_at = utcnow()
        session.add(row)
        session.commit()
        rma_id = int(row.id)

    for upload in upload_list:
        save_attachment(rma_id=rma_id, upload=upload, actor_user=actor_user)

    payload = get_rma_detail(rma_id, actor_user=actor_user) or {}
    if returns_final_v1_enabled():
        _send_return_event_email(payload, "return_submitted")
    else:
        settings = get_returns_settings()
        _send_notification_email(payload, "return_requested")
        if status == STATUS_AWAITING_EVIDENCE:
            _send_notification_email(payload, "evidence_required")
        warehouse_recipients = _active_user_emails_for_permissions(
            "returns.approve.wh",
            "returns.warehouse.receive",
            "returns.warehouse.scan",
            "admin.returns.manage",
        )
        if warehouse_recipients:
            _send_rma_bulk_email(
                rma_id,
                warehouse_recipients,
                _format_subject(
                    settings.get("email_templates", {}).get("new_return_subject", f"New return pending review for order {order_id}"),
                    payload,
                ) or f"New return pending review for order {order_id}",
                f"Return #{rma_id} for order {order_id} was submitted and is awaiting warehouse review.",
            )
    current_app.logger.info(
        "returns.create_rma",
        extra={
            "rma_id": rma_id,
            "customer_id": customer_id,
            "order_id": order_payload.get("order_id"),
            "status": status,
            "actor_user_id": _actor_id(actor_user),
            "workflow_mode": selected_mode,
        },
    )
    return payload


def transition_return(
    rma_id: int,
    to_status: str,
    *,
    actor_user: Any = None,
    event_type: str | None = None,
    payload: Optional[dict[str, Any]] = None,
    scope: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    target = str(to_status or "").strip().lower()
    event_payload = dict(payload or {})
    resolved_event_name = event_type or {
        STATUS_WH_APPROVED: "warehouse_approved",
        STATUS_APPROVED: "manager_approved",
        STATUS_REJECTED: "rejected",
        STATUS_COMPLETED: "completed",
    }.get(target, "transition")
    payload_scope = _scope_for_actor(actor_user, scope=scope)
    with get_session() as session:
        row = session.get(ReturnRMA, int(rma_id))
        if not row:
            raise ReturnsError("RMA not found.")
        assert_customer_in_scope(row.customer_id, scope=payload_scope, actor_user=actor_user)
        current = str(row.status or "").strip().lower()
        if current == target:
            return get_rma_detail(int(rma_id), actor_user=actor_user, scope=payload_scope) or {}
        allow_legacy_reject = target == STATUS_REJECTED and current in {
            STATUS_REQUESTED,
            STATUS_NEEDS_REVIEW,
            STATUS_AUTO_APPROVED,
            STATUS_AWAITING_EVIDENCE,
        }
        if not can_transition(current, target) and not allow_legacy_reject:
            raise InvalidTransitionError(f"Invalid transition: {current} -> {target}")
        items = (
            session.query(ReturnRMAItem)
            .filter(ReturnRMAItem.rma_id == int(rma_id))
            .order_by(ReturnRMAItem.id.asc())
            .all()
        )
        approval = None
        if target in {STATUS_WH_APPROVED, STATUS_APPROVED, STATUS_REJECTED}:
            approval = _ensure_approval_row(session, int(rma_id))
        now = utcnow()
        if target == STATUS_WH_APPROVED:
            assert approval is not None
            if returns_final_v1_enabled():
                _validate_warehouse_transition(row, items, payload=event_payload)
            approval.wh_approved_by = _actor_id(actor_user)
            approval.wh_approved_at = now
            approval.rejected_by = None
            approval.rejected_at = None
            approval.reject_reason = None
            row.wh_approved_by_user_id = approval.wh_approved_by
            row.wh_approved_at = approval.wh_approved_at
            row.rejected_by_user_id = None
            row.rejected_at = None
            row.reject_reason = None
        elif target == STATUS_APPROVED:
            assert approval is not None
            if returns_final_v1_enabled():
                _validate_manager_transition(row, items)
            approval.mgr_approved_by = _actor_id(actor_user)
            approval.mgr_approved_at = now
            approval.rejected_by = None
            approval.rejected_at = None
            approval.reject_reason = None
            row.mgr_approved_by_user_id = approval.mgr_approved_by
            row.mgr_approved_at = approval.mgr_approved_at
            row.rejected_by_user_id = None
            row.rejected_at = None
            row.reject_reason = None
        elif target == STATUS_REJECTED:
            assert approval is not None
            if returns_final_v1_enabled():
                _validate_warehouse_transition(row, items, rejecting=True, payload=event_payload)
            reject_reason = str(event_payload.get("reject_reason") or event_payload.get("reason") or "").strip()
            approval.rejected_by = _actor_id(actor_user)
            approval.rejected_at = now
            approval.reject_reason = reject_reason
            row.rejected_by_user_id = approval.rejected_by
            row.rejected_at = approval.rejected_at
            row.reject_reason = reject_reason
            event_payload.setdefault("reject_reason", reject_reason)
        elif target == STATUS_COMPLETED:
            _update_rma_metadata(
                row,
                completed_at=now.isoformat(),
                completed_by_user_id=_actor_id(actor_user),
            )
        
        # Enterprise SLA Tracking
        if target == STATUS_PICKUP_SCHEDULED:
            row.ops_cleared_at = now
        elif target == STATUS_WH_APPROVED:
            row.wh_reviewed_at = now
        elif target == STATUS_COMPLETED:
            row.fin_cleared_at = now

        row.status = target
        row.updated_at = now
        notes = str(event_payload.get("decision_summary") or event_payload.get("notes") or "").strip()
        if target == STATUS_REJECTED and not notes:
            notes = str(event_payload.get("reject_reason") or "").strip()
        if notes:
            row.decision_summary = notes
        if approval is not None:
            session.add(approval)
        session.add(row)
        _add_event(
            session,
            rma_id=int(row.id),
            event_type=resolved_event_name,
            from_status=current,
            to_status=target,
            actor_user_id=_actor_id(actor_user),
            payload=event_payload,
        )
        session.commit()

    detail = get_rma_detail(int(rma_id), actor_user=actor_user, scope=payload_scope) or {}
    if not returns_final_v1_enabled():
        if target == STATUS_AWAITING_EVIDENCE:
            _send_notification_email(detail, "evidence_required")
        elif target in {STATUS_AWAITING_RETURN, STATUS_AUTO_APPROVED}:
            _send_notification_email(detail, "approved")
        elif target == STATUS_DENIED:
            _send_notification_email(detail, "denied")
        elif target == STATUS_COMPLETED and detail.get("refunds"):
            _send_notification_email(detail, "refund_processed")
    current_app.logger.info(
        "returns.transition",
        extra={
            "rma_id": int(rma_id),
            "to_status": target,
            "event_type": resolved_event_name,
            "actor_user_id": _actor_id(actor_user),
        },
    )
    return detail


def transition_rma(
    rma_id: int,
    to_status: str,
    *,
    actor_user: Any = None,
    event_type: str = "transition",
    payload: Optional[dict[str, Any]] = None,
    scope: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return transition_return(
        rma_id,
        to_status,
        actor_user=actor_user,
        event_type=event_type,
        payload=payload,
        scope=scope,
    )


def build_return_pdf_bytes(
    rma_id: int,
    *,
    actor_user: Any = None,
    credit_po: bool = False,
    scope: Optional[dict[str, Any]] = None,
) -> bytes:
    detail = get_rma_detail(int(rma_id), actor_user=actor_user, scope=scope)
    if not detail:
        raise ReturnsError("RMA not found.")
    generated_at = _vancouver_now().strftime("%Y-%m-%d %H:%M %Z")
    document_title = "Credit PO" if credit_po else "Return Form"
    
    # Enterprise Dynamic Branding
    company = str(detail.get("company") or "").strip().lower()
    if "alderwood" in company:
        logo_file = "wa-logo-badge.png"
        company_header = "Alderwood Grocers Meats"
    else:
        logo_file = "wa-logo-badge.png"
        company_header = "Northgate Retail Group"
        
    logo_path = Path(current_app.root_path) / "static" / "img" / logo_file
    logo_uri = logo_path.resolve().as_uri() if logo_path.exists() else None
    
    _rendered_html = render_template(
        "returns/return_form.html",
        document_title=document_title,
        company_header=company_header,
        r=detail,
        items=detail.get("items") or [],
        generated_at=generated_at,
        logo_uri=logo_uri,
    )
    pdf_bytes: bytes | None = None
    # Enterprise Optimization: Always use structured PDF (FPDF) for speed and reliability.
    # WeasyPrint is skipped due to performance and environmental bottlenecks.
    pdf_bytes = _build_structured_return_pdf(
        detail,
        generated_at=generated_at,
        credit_po=credit_po,
    )
    
    current_app.logger.info(
        "returns.pdf_generated",
        extra={"rma_id": int(rma_id), "credit_po": bool(credit_po), "actor_user_id": _actor_id(actor_user)},
    )
    _record_system_event(
        int(rma_id),
        event_type="pdf_exported",
        actor_user_id=_actor_id(actor_user),
        payload={"credit_po": bool(credit_po)},
    )
    return pdf_bytes


def _determine_approval_target(rma: ReturnRMA, items: list[ReturnRMAItem]) -> str:
    """Determine the manager target for approval based on business rules."""
    # Rule A: Not returning or Discount -> Scott
    not_returning = any(not item.product_returning for item in items)
    has_discount = any(str(item.follow_up_action or "").strip().lower() == "discount" for item in items)
    
    if not_returning or has_discount:
        return "Scott"
    
    # Rule B & C: Returning and credit threshold -> Brian (< 300), Kyle (>= 300)
    # Use the warehouse-adjusted credit total
    total_credit = sum(float(item.credit_amount or 0.0) for item in items)
    
    if total_credit < 300:
        return "Brian"
    return "Kyle"


def schedule_pickup(rma_id: int, *, actor_user: Any = None, notes: str | None = None) -> dict[str, Any]:
    """Transition RMA to pickup_scheduled status."""
    return transition_return(
        rma_id,
        STATUS_PICKUP_SCHEDULED,
        actor_user=actor_user,
        event_type="pickup_scheduled",
        payload={"notes": str(notes or "").strip()},
    )


def mark_picked_up(rma_id: int, *, actor_user: Any = None, notes: str | None = None) -> dict[str, Any]:
    """Transition RMA to picked_up status."""
    return transition_return(
        rma_id,
        STATUS_PICKED_UP,
        actor_user=actor_user,
        event_type="picked_up",
        payload={"notes": str(notes or "").strip()},
    )


def approve_warehouse(
    rma_id: int,
    *,
    actor_user: Any = None,
    notes: str | None = None,
    scope: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    with get_session() as session:
        row = session.get(ReturnRMA, int(rma_id))
        if not row:
            raise ReturnsError("RMA not found.")
        
        items = (
            session.query(ReturnRMAItem)
            .filter(ReturnRMAItem.rma_id == int(rma_id))
            .all()
        )
        
        # Calculate and store approval target
        target_name = _determine_approval_target(row, items)
        row.approval_target = target_name
        session.add(row)
        session.commit()
        
        decision_summary = str(notes or "").strip() or f"Approved by warehouse. Routed to {target_name}."

    detail = transition_return(
        rma_id,
        STATUS_WH_APPROVED,
        actor_user=actor_user,
        event_type="warehouse_approved",
        payload={
            "notes": str(notes or "").strip(),
            "decision_summary": decision_summary,
            "approval_target": target_name,
        },
        scope=scope,
    )
    if returns_final_v1_enabled():
        _send_return_event_email(detail, "warehouse_approved", detail=str(notes or "").strip() or None)
    else:
        settings = get_returns_settings()
        manager_recipients = _active_user_emails_for_permissions(
            "returns.approvals.view",
            "returns.approve.mgr",
            "returns.ops.queue.view",
            "returns.ops.approve",
            "admin.returns.manage",
        )
        if manager_recipients:
            _send_rma_bulk_email(
                int(rma_id),
                manager_recipients,
                _format_subject(
                    settings.get("email_templates", {}).get("wh_approval_subject", f"Warehouse approved return #{rma_id}"),
                    detail,
                ) or f"Warehouse approved return #{rma_id}",
                f"Return #{rma_id} is ready for manager approval.",
            )
    current_app.logger.info(
        "returns.approve_warehouse",
        extra={"rma_id": int(rma_id), "actor_user_id": _actor_id(actor_user)},
    )
    return detail


def _send_finance_notification_email(rma_detail: dict[str, Any], pdf_bytes: bytes | None = None) -> bool:
    """Send an automated notification to Finance after manager approval."""
    company = str(rma_detail.get("company") or "").strip().lower()
    if "alderwood" in company:
        recipient = "ar@alderwoodgrocers.example.com"
    else:
        # Northgate Retail Group is the default book; Alderwood is the second.
        recipient = "ar@wholesaleprovisions.example.com"
    
    rma_id = rma_detail.get("id")
    rma_number = rma_detail.get("rma_number") or f"#{rma_id}"
    subject = f"Return {rma_number} - Approved for Finance Review"
    
    body = (
        f"Return {rma_number} for {rma_detail.get('customer_name')} has been approved by management "
        f"and is now awaiting Finance processing.\n\n"
        f"Customer: {rma_detail.get('customer_name')} ({rma_detail.get('customer_id')})\n"
        f"Total Credit: ${rma_detail.get('total_credit_amount'):.2f}\n"
        f"RMA Number: {rma_number}\n\n"
        f"Please review the attached Credit-PO and finalize the credit."
    )
    
    attachments = None
    if pdf_bytes:
        attachments = [{"filename": f"credit-po-{rma_id}.pdf", "mimetype": "application/pdf", "data": pdf_bytes}]
    else:
        try:
            generated = build_return_pdf_bytes(int(rma_id), credit_po=True)
            attachments = [{"filename": f"return-{rma_id}.pdf", "mimetype": "application/pdf", "data": generated}]
        except Exception:
            pass
        
    return _send_rma_bulk_email(int(rma_id), [recipient], subject, body, attachments=attachments)


def approve_manager(
    rma_id: int,
    *,
    actor_user: Any = None,
    notes: str | None = None,
    scope: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    target_status = STATUS_APPROVED
    if returns_final_v1_enabled():
        target_status = STATUS_AWAITING_FINANCE

    with get_session() as session:
        row = session.get(ReturnRMA, int(rma_id))
        if not row:
            raise ReturnsError("RMA not found.")
        
        items = (
            session.query(ReturnRMAItem)
            .filter(ReturnRMAItem.rma_id == int(rma_id))
            .all()
        )
        
        # Enterprise Hardening: Sync rollups one last time before manager validation
        item_payloads = [_serialize_item(item) for item in items]
        reason_lookup = _reason_lookup_map()
        _apply_rma_rollups(row, item_payloads, reason_lookup=reason_lookup)
        session.add(row)
        session.commit()

    detail = transition_return(
        rma_id,
        target_status,
        actor_user=actor_user,
        event_type="manager_approved",
        payload={
            "notes": str(notes or "").strip(),
            "decision_summary": str(notes or "").strip() or "Approved by manager. Awaiting Finance.",
        },
        scope=scope,
    )
    pdf_bytes = build_return_pdf_bytes(int(rma_id), actor_user=actor_user, credit_po=True, scope=scope)
    attachments = [{"filename": f"credit-po-return-{rma_id}.pdf", "mimetype": "application/pdf", "data": pdf_bytes}]
    if returns_final_v1_enabled():
        _send_return_event_email(detail, "manager_approved", detail=str(notes or "").strip() or None, attachments=attachments)
        _send_finance_notification_email(detail, pdf_bytes=pdf_bytes)
    else:
        settings = get_returns_settings()
        recipients = _rma_submitter_recipients(detail)
        if recipients:
            _send_rma_bulk_email(
                int(rma_id),
                recipients,
                _format_subject(
                    settings.get("email_templates", {}).get("manager_approval_subject", f"Return #{rma_id} approved"),
                    detail,
                ) or f"Return #{rma_id} approved",
                f"Return #{rma_id} has been approved. Your Credit-PO is attached.",
                attachments=attachments,
            )
    current_app.logger.info(
        "returns.approve_manager",
        extra={"rma_id": int(rma_id), "actor_user_id": _actor_id(actor_user)},
    )
    # Include pdf_bytes in the return payload so the blueprint can reuse it
    detail["pdf_bytes"] = pdf_bytes
    return {
        "detail": detail,
        "pdf_bytes": pdf_bytes,
        "filename": f"credit-po-return-{rma_id}.pdf",
    }


def reject_rma(
    rma_id: int,
    *,
    actor_user: Any = None,
    reason: str | None = None,
    scope: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    clean_reason = str(reason or "").strip()
    detail = transition_return(
        rma_id,
        STATUS_REJECTED,
        actor_user=actor_user,
        event_type="rejected",
        payload={
            "reason": clean_reason,
            "reject_reason": clean_reason,
            "decision_summary": clean_reason or "Rejected.",
        },
        scope=scope,
    )
    if returns_final_v1_enabled():
        _send_return_event_email(detail, "rejected", detail=clean_reason or None)
    else:
        settings = get_returns_settings()
        recipients = _rma_submitter_recipients(detail)
        if recipients:
            _send_rma_bulk_email(
                int(rma_id),
                recipients,
                _format_subject(
                    settings.get("email_templates", {}).get("rejection_subject", f"Return #{rma_id} rejected"),
                    detail,
                ) or f"Return #{rma_id} rejected",
                f"Return #{rma_id} was rejected. {clean_reason}".strip(),
            )
    current_app.logger.info(
        "returns.reject",
        extra={"rma_id": int(rma_id), "actor_user_id": _actor_id(actor_user)},
    )
    return detail


def issue_label(rma_id: int, *, actor_user: Any = None, carrier: str = "manual") -> dict[str, Any]:
    if not labels_enabled():
        raise ReturnsError("Label generation is disabled.")
    detail = get_rma_detail(int(rma_id), actor_user=actor_user)
    if not detail:
        raise ReturnsError("RMA not found.")
    with get_session() as session:
        row = session.get(ReturnRMA, int(rma_id))
        if not row:
            raise ReturnsError("RMA not found.")
        prior_status = str(row.status or "")
        tracking = f"RMA-{int(rma_id)}-{int(time.time())}"
        shipment = ReturnShipment(
            rma_id=int(rma_id),
            carrier=str(carrier or "manual"),
            label_url=build_public_url(f"/returns/{int(rma_id)}?label=1"),
            tracking_number=tracking,
            shipping_cost=0.0,
            status="label_created",
            metadata_json=dumps_json({"created_by_user_id": _actor_id(actor_user)}),
        )
        session.add(shipment)
        session.flush()
        _add_event(
            session,
            rma_id=int(rma_id),
            event_type="label_generated",
            from_status=row.status,
            to_status=row.status,
            actor_user_id=_actor_id(actor_user),
            payload={"tracking_number": tracking, "carrier": carrier},
        )
        if row.status in {STATUS_AUTO_APPROVED, STATUS_NEEDS_REVIEW}:
            if not can_transition(row.status, STATUS_AWAITING_RETURN):
                raise InvalidTransitionError(f"Invalid transition: {row.status} -> {STATUS_AWAITING_RETURN}")
            row.status = STATUS_AWAITING_RETURN
            row.updated_at = utcnow()
            session.add(row)
            _add_event(
                session,
                rma_id=int(rma_id),
                event_type="transition",
                from_status=prior_status,
                to_status=STATUS_AWAITING_RETURN,
                actor_user_id=_actor_id(actor_user),
                payload={"reason": "label_generated"},
            )
        session.commit()
    detail = get_rma_detail(int(rma_id), actor_user=actor_user) or {}
    _send_notification_email(detail, "label_issued")
    current_app.logger.info(
        "returns.label",
        extra={"rma_id": int(rma_id), "carrier": carrier, "actor_user_id": _actor_id(actor_user)},
    )
    return detail


def receive_rma(rma_id: int, *, actor_user: Any = None) -> dict[str, Any]:
    detail = get_rma_detail(int(rma_id), actor_user=actor_user)
    if not detail:
        raise ReturnsError("RMA not found.")
    current = str(detail.get("status") or "")
    if current == STATUS_AWAITING_RETURN:
        detail = transition_rma(int(rma_id), STATUS_RECEIVED, actor_user=actor_user, event_type="received")
    elif current == STATUS_IN_TRANSIT:
        detail = transition_rma(int(rma_id), STATUS_RECEIVED, actor_user=actor_user, event_type="received")
    elif current == STATUS_RECEIVED:
        return detail
    else:
        raise InvalidTransitionError(f"Invalid transition: {current} -> {STATUS_RECEIVED}")
    return detail


def inspect_rma(
    rma_id: int,
    *,
    disposition: str,
    notes: str | None = None,
    actor_user: Any = None,
    photo_uploads: Optional[Iterable[Any]] = None,
) -> dict[str, Any]:
    detail = get_rma_detail(int(rma_id), actor_user=actor_user)
    if not detail:
        raise ReturnsError("RMA not found.")
    photos: list[str] = []
    for upload in list(photo_uploads or []):
        attachment = save_attachment(rma_id=int(rma_id), upload=upload, actor_user=actor_user)
        photos.append(attachment["file_path"])
    with get_session() as session:
        row = session.get(ReturnRMA, int(rma_id))
        if not row:
            raise ReturnsError("RMA not found.")
        current = str(row.status or "")
        if current != STATUS_RECEIVED:
            if current == STATUS_AWAITING_RETURN:
                raise InvalidTransitionError(f"Invalid transition: {current} -> {STATUS_INSPECTED}")
            if current != STATUS_INSPECTED:
                raise InvalidTransitionError(f"Invalid transition: {current} -> {STATUS_INSPECTED}")
        inspection = ReturnInspection(
            rma_id=int(rma_id),
            disposition=str(disposition or "").strip().lower() or None,
            notes=str(notes or "").strip() or None,
            photos_json=dumps_json(photos, default="[]"),
            inspected_by_user_id=_actor_id(actor_user),
        )
        session.add(inspection)
        session.flush()
        if current == STATUS_RECEIVED:
            row.status = STATUS_INSPECTED
            row.updated_at = utcnow()
            session.add(row)
            _add_event(
                session,
                rma_id=int(rma_id),
                event_type="inspected",
                from_status=STATUS_RECEIVED,
                to_status=STATUS_INSPECTED,
                actor_user_id=_actor_id(actor_user),
                payload={"disposition": disposition},
            )
        session.commit()

    disposition_map = {
        "refund": STATUS_APPROVED_REFUND,
        "approved_refund": STATUS_APPROVED_REFUND,
        "deny": STATUS_DENIED,
        "denied": STATUS_DENIED,
        "restock": STATUS_COMPLETED,
        "completed": STATUS_COMPLETED,
    }
    final_status = disposition_map.get(str(disposition or "").strip().lower())
    if final_status:
        return transition_rma(
            int(rma_id),
            final_status,
            actor_user=actor_user,
            event_type="inspection_disposition",
            payload={"disposition": disposition, "notes": notes or ""},
        )
    return get_rma_detail(int(rma_id), actor_user=actor_user) or {}


def issue_refund(
    rma_id: int,
    *,
    amount: float,
    method: str = "manual",
    actor_user: Any = None,
    processor_ref: str | None = None,
) -> dict[str, Any]:
    if not refunds_enabled():
        raise ReturnsError("Refund issuance is disabled.")
    detail = get_rma_detail(int(rma_id), actor_user=actor_user)
    if not detail:
        raise ReturnsError("RMA not found.")
    current = str(detail.get("status") or "")
    if current != STATUS_APPROVED_REFUND:
        detail = transition_rma(
            int(rma_id),
            STATUS_APPROVED_REFUND,
            actor_user=actor_user,
            event_type="refund_approved",
            payload={"amount": float(amount)},
        )
    with get_session() as session:
        row = ReturnRefund(
            rma_id=int(rma_id),
            amount=float(amount or 0),
            method=str(method or "manual"),
            status="issued",
            processor_ref=str(processor_ref or "").strip() or None,
        )
        session.add(row)
        session.flush()
        _add_event(
            session,
            rma_id=int(rma_id),
            event_type="refund_issued",
            from_status=STATUS_APPROVED_REFUND,
            to_status=STATUS_COMPLETED,
            actor_user_id=_actor_id(actor_user),
            payload={"amount": float(amount or 0), "method": method},
        )
        owner = session.get(ReturnRMA, int(rma_id))
        if owner:
            owner.status = STATUS_COMPLETED
            owner.updated_at = utcnow()
            session.add(owner)
        session.commit()
    detail = get_rma_detail(int(rma_id), actor_user=actor_user) or {}
    _send_notification_email(detail, "refund_processed")
    current_app.logger.info(
        "returns.refund",
        extra={
            "rma_id": int(rma_id),
            "amount": float(amount or 0),
            "method": method,
            "actor_user_id": _actor_id(actor_user),
        },
    )
    return detail


def process_webhook(
    *,
    source: str,
    event_type: str,
    idempotency_key: str,
    payload: dict[str, Any],
    actor_user: Any = None,
) -> tuple[dict[str, Any], bool]:
    key = str(idempotency_key or "").strip()
    if not key:
        key = hashlib.sha256(dumps_json(payload).encode("utf-8")).hexdigest()
    created_id: int
    with get_session() as session:
        existing = (
            session.query(ReturnWebhookEvent)
            .filter(ReturnWebhookEvent.idempotency_key == key)
            .first()
        )
        if existing:
            return {
                "id": int(existing.id),
                "status": existing.status,
                "processed_at": existing.processed_at.isoformat() if existing.processed_at else "",
                "idempotency_key": existing.idempotency_key,
            }, False
        row = ReturnWebhookEvent(
            source=str(source or "").strip() or "unknown",
            event_type=str(event_type or "").strip() or "event",
            idempotency_key=key,
            payload_json=dumps_json(payload or {}),
            status="pending",
        )
        session.add(row)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            existing = (
                session.query(ReturnWebhookEvent)
                .filter(ReturnWebhookEvent.idempotency_key == key)
                .first()
            )
            if existing:
                return {
                    "id": int(existing.id),
                    "status": existing.status,
                    "processed_at": existing.processed_at.isoformat() if existing.processed_at else "",
                    "idempotency_key": existing.idempotency_key,
                }, False
            raise
        session.add(row)
        session.commit()
        created_id = int(row.id)

    status_value = "processed"
    rma_id = payload.get("rma_id") or payload.get("return_id")
    try:
        admin_scope = {"is_admin": True, "scope_mode": "all"}
        if rma_id:
            if event_type in {"tracking.in_transit", "shipment.in_transit"}:
                transition_rma(
                    int(rma_id),
                    STATUS_IN_TRANSIT,
                    actor_user=actor_user,
                    event_type="webhook",
                    scope=admin_scope,
                )
            elif event_type in {"tracking.received", "tracking.delivered", "shipment.received"}:
                transition_rma(
                    int(rma_id),
                    STATUS_RECEIVED,
                    actor_user=actor_user,
                    event_type="webhook",
                    scope=admin_scope,
                )
    except Exception as exc:
        status_value = f"error:{exc}"

    with get_session() as session:
        row = session.get(ReturnWebhookEvent, created_id)
        if not row:
            return {"id": created_id, "status": status_value, "processed_at": "", "idempotency_key": key}, True
        row.status = status_value
        row.processed_at = utcnow()
        session.add(row)
        session.commit()
        return {
            "id": int(row.id),
            "status": row.status,
            "processed_at": row.processed_at.isoformat() if row.processed_at else "",
            "idempotency_key": row.idempotency_key,
        }, True


def verify_webhook_signature(raw_body: bytes, signature: str | None, secret: str | None) -> bool:
    if not secret:
        return True
    presented = str(signature or "").strip()
    if not presented:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body or b"", hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, presented)


def email_templates_catalog() -> list[dict[str, str]]:
    return [
        {"key": "return_requested", "name": "Return Requested"},
        {"key": "evidence_required", "name": "Evidence Required"},
        {"key": "approved", "name": "Approved / Awaiting Return"},
        {"key": "denied", "name": "Denied"},
        {"key": "label_issued", "name": "Label Issued / Tracking Update"},
        {"key": "refund_processed", "name": "Refund Processed"},
    ]


def integrations_status() -> dict[str, Any]:
    return {
        "returns_enabled": returns_enabled(),
        "customer_portal_enabled": customer_portal_enabled(),
        "returns_excel_form_enabled": returns_excel_form_enabled(),
        "returns_autofill_order_enabled": returns_autofill_order_enabled(),
        "returns_analytics_enabled": returns_analytics_enabled(),
        "labels_enabled": labels_enabled(),
        "refunds_enabled": refunds_enabled(),
        "ai_enabled": ai_enabled(),
        "smtp_configured": bool(current_app.config.get("SMTP_SERVER")),
        "webhook_secret_configured": bool(current_app.config.get("RETURNS_WEBHOOK_SECRET")),
        "attachments_dir": str(attachments_root()),
    }


def ready_check() -> dict[str, Any]:
    db_ok = True
    try:
        with get_session() as session:
            session.query(ReturnRMA.id).limit(1).all()
    except Exception:
        db_ok = False
    uploads_ok = True
    try:
        attachments_root()
    except Exception:
        uploads_ok = False
    return {"ok": bool(db_ok and uploads_ok), "db": db_ok, "attachments_dir_writable": uploads_ok}
