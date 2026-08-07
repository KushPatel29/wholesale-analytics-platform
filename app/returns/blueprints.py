"""Blueprints for the returns module."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from io import BytesIO

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from app.cache import cache
from app.core.access_policy import bump_permissions_version
from app.core.access_policy import get_current_scope
from app.core.rbac import any_permission_required, has_any_permission, permission_required
from app.services import fact_store
from . import service


portal_bp = Blueprint("returns_portal", __name__)
ops_bp = Blueprint("returns_ops", __name__, url_prefix="/returns/ops")
warehouse_bp = Blueprint("returns_warehouse", __name__, url_prefix="/returns/wh")
admin_bp = Blueprint("returns_admin", __name__, url_prefix="/admin/returns")
webhooks_bp = Blueprint("returns_webhooks", __name__, url_prefix="/returns/webhooks")

RETURNS_HOME_PERMISSIONS = (
    "page.returns.view",
    "page.returns.customer_portal",
    "page.returns.ops",
    "page.returns.warehouse",
    "admin.returns.manage",
)
PORTAL_VIEW_PERMISSIONS = (
    "page.returns.view",
    "page.returns.customer_portal",
    "admin.returns.manage",
)
PORTAL_CREATE_PERMISSIONS = (
    "returns.create",
    "admin.returns.manage",
)
OPS_VIEW_PERMISSIONS = (
    "returns.approvals.view",
    "returns.ops.queue.view",
    "page.returns.ops",
    "admin.returns.manage",
)
OPS_APPROVE_PERMISSIONS = (
    "returns.approve.mgr",
    "returns.ops.approve",
    "returns.approve",
    "admin.returns.manage",
)
OPS_DENY_PERMISSIONS = (
    "returns.ops.deny",
    "returns.deny",
    "admin.returns.manage",
)
OPS_OVERRIDE_PERMISSIONS = (
    "returns.ops.override",
    "returns.override",
    "admin.returns.manage",
)
WAREHOUSE_SCAN_PERMISSIONS = (
    "returns.warehouse.scan",
    "page.returns.warehouse",
    "admin.returns.manage",
)
WAREHOUSE_RECEIVE_PERMISSIONS = (
    "returns.approve.wh",
    "returns.warehouse.receive",
    "page.returns.warehouse",
    "admin.returns.manage",
)
WAREHOUSE_INSPECT_PERMISSIONS = (
    "returns.warehouse.inspect",
    "page.returns.warehouse",
    "admin.returns.manage",
)
WAREHOUSE_VIEW_PERMISSIONS = tuple(
    dict.fromkeys(WAREHOUSE_SCAN_PERMISSIONS + WAREHOUSE_RECEIVE_PERMISSIONS + WAREHOUSE_INSPECT_PERMISSIONS)
)
APPROVALS_VIEW_PERMISSIONS = tuple(dict.fromkeys(OPS_VIEW_PERMISSIONS + WAREHOUSE_RECEIVE_PERMISSIONS))
RECEIVING_EDIT_PERMISSIONS = tuple(dict.fromkeys(WAREHOUSE_RECEIVE_PERMISSIONS + OPS_APPROVE_PERMISSIONS))
REJECT_PERMISSIONS = tuple(dict.fromkeys(("returns.reject",) + OPS_DENY_PERMISSIONS + WAREHOUSE_RECEIVE_PERMISSIONS))
LABEL_PERMISSIONS = ("returns.labels.generate", "admin.returns.manage")
REFUND_PERMISSIONS = ("returns.refunds.issue", "admin.returns.manage")
PDF_EXPORT_PERMISSIONS = (
    "returns.pdf.export",
    "page.returns.view",
    "page.returns.customer_portal",
    "admin.returns.manage",
)
ANALYTICS_VIEW_PERMISSIONS = (
    "page.returns.analytics.view",
    "admin.returns.manage",
)


def _returns_on() -> None:
    if not service.returns_enabled():
        abort(404)


def _customer_portal_on() -> None:
    _returns_on()
    if not service.customer_portal_enabled():
        abort(404)


def _require_any_permission(*perms: str) -> None:
    if current_app.config.get("LOGIN_DISABLED") or current_app.config.get("AUTHZ_DISABLED"):
        return

    from app.core import rbac

    if rbac.has_any_permission(*perms):
        return
    abort(403, description=f"Requires any permission from: {', '.join(perms)}")


def _has_any_permission(*perms: str) -> bool:
    if current_app.config.get("LOGIN_DISABLED") or current_app.config.get("AUTHZ_DISABLED"):
        return True
    from app.core import rbac

    return bool(rbac.has_any_permission(*perms))


def _load_rma_or_404(rma_id: int) -> dict[str, object]:
    try:
        payload = service.get_rma_detail(rma_id, actor_user=current_user)
    except service.ScopeViolationError as exc:
        flash(str(exc), "danger")
        abort(403, description=str(exc))
    if not payload:
        abort(404)
    return payload


def _returns_v2_context() -> dict[str, object]:
    return {
        "returns_v2_enabled": service.returns_excel_form_enabled(),
        "returns_autofill_enabled": service.returns_autofill_order_enabled(),
    }


def _order_lookup_cache_key(order_id: str) -> str:
    try:
        scope = get_current_scope(use_cache=True)
        scope_hash = getattr(scope, "scope_hash", "") or ""
    except Exception:
        scope_hash = ""
    try:
        user_id = current_user.get_id() if getattr(current_user, "is_authenticated", False) else ""
    except Exception:
        user_id = ""
    dataset_version = fact_store.cache_buster()
    basis = "|".join(
        [
            "returns-order-lookup-v2",
            str(user_id or ""),
            str(scope_hash or ""),
            str(dataset_version or ""),
            str(order_id or "").strip().lower(),
        ]
    )
    token = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return f"returns:order_lookup:{token}"


def _as_float(value: object, default: float = 0.0) -> float:
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


def _line_credit_amount(price_per_lb: object, weight_lb: object, credit_pct: object = 100.0) -> float:
    price = max(_as_float(price_per_lb, 0.0), 0.0)
    weight = max(_as_float(weight_lb, 0.0), 0.0)
    pct = max(0.0, min(100.0, _as_float(credit_pct, 100.0)))
    return round(price * weight * (pct / 100.0), 2)


def _order_item_price_per_lb(item: dict[str, object]) -> float:
    return _as_float(
        item.get("price_per_lb"),
        _as_float(
            item.get("unit_price_per_lb"),
            _as_float(item.get("unit_price"), _as_float(item.get("price"), 0.0)),
        ),
    )


def _order_item_weight_lb(item: dict[str, object], default: float = 0.0) -> float:
    return _as_float(
        item.get("weight_lb"),
        _as_float(
            item.get("shipped_weight_lb"),
            _as_float(item.get("pack_weight_lb_sum"), default),
        ),
    )


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _receiving_item_updates_from_form() -> list[dict[str, object]]:
    updates: list[dict[str, object]] = []
    for raw_item_id in request.form.getlist("item_ids"):
        if not str(raw_item_id).strip().isdigit():
            continue
        item_id = int(str(raw_item_id).strip())
        updates.append(
            {
                "item_id": item_id,
                "pack_barcode": request.form.get(f"item_pack_barcode_{item_id}"),
                "packs_count": request.form.get(f"item_packs_count_{item_id}"),
                "follow_up_action": request.form.get(f"item_follow_up_action_{item_id}"),
                "supplier_credit": request.form.get(f"item_supplier_credit_{item_id}"),
                "receiving_notes": request.form.get(f"item_receiving_notes_{item_id}"),
                "warehouse_outcome": request.form.get(f"item_warehouse_outcome_{item_id}"),
                "received_weight_lb": request.form.get(f"item_received_weight_lb_{item_id}"),
            }
        )
    return updates


def _post_action_redirect(rma_id: int, *, fallback_endpoint: str = "returns_portal.approvals"):
    target = (request.form.get("return_to") or "").strip().lower()
    if target == "detail":
        return redirect(url_for("returns_portal.detail", rma_id=rma_id))
    return redirect(url_for(fallback_endpoint))


def _render_create_form(
    *,
    order: dict[str, object] | None,
    order_id: str,
    order_items_json: str,
    initial_item_rows: list[dict[str, object]],
    lookup_payload: dict[str, object] | None,
    lookup_error: str | None,
    reason_codes: list[dict[str, object]],
    returns_settings: dict[str, object],
    default_date_submitted: str,
    status_code: int = 200,
):
    template_name = "returns/new_return.html" if service.returns_excel_form_enabled() else "returns/new.html"
    return (
        render_template(
            template_name,
            order=order,
            order_id=order_id,
            order_items_json=order_items_json,
            initial_item_rows=initial_item_rows,
            lookup_payload=lookup_payload,
            lookup_error=lookup_error,
            reason_codes=reason_codes,
            returns_settings=returns_settings,
            default_date_submitted=default_date_submitted,
            **_returns_v2_context(),
        ),
        status_code,
    )


def _array_form_values(field_name: str) -> list[str]:
    values = request.form.getlist(f"{field_name}[]")
    if values:
        return values
    return request.form.getlist(field_name)


def _validate_selected_items(items: list[dict[str, object]]) -> list[str]:
    errors: list[str] = []
    for idx, item in enumerate(items, start=1):
        qty = _as_float(item.get("qty"), 0.0)
        max_qty = _as_float(
            item.get("max_qty"),
            max(_as_float(item.get("qty_shipped")), _as_float(item.get("qty_ordered")), 0.0),
        )
        if qty <= 0:
            errors.append(f"Item {idx}: return quantity/weight must be greater than zero.")
        if max_qty > 0 and qty > max_qty:
            errors.append(f"Item {idx}: return quantity cannot exceed {max_qty:g}.")
        if not str(item.get("reason_code") or "").strip():
            errors.append(f"Item {idx}: a reason is required.")
    return errors


def _lookup_payload_to_form_rows(lookup_payload: dict[str, object]) -> list[dict[str, object]]:
    suggestions = {
        str(row.get("order_line_id") or ""): row
        for row in (lookup_payload.get("suggestions") or [])
        if isinstance(row, dict)
    }
    rows: list[dict[str, object]] = []
    for item in (lookup_payload.get("items") or []):
        if not isinstance(item, dict):
            continue
        order_line_id = str(item.get("order_line_id") or "")
        suggestion = suggestions.get(order_line_id, {})
        max_qty = max(
            _as_float(item.get("qty_shipped"), 0.0),
            _as_float(item.get("qty_ordered"), 0.0),
            1.0,
        )
        price_per_lb = _order_item_price_per_lb(item)
        weight_lb = _order_item_weight_lb(item, default=0.0)
        credit_pct = 100.0
        rationale_parts = [str(item.get("mapping_warning") or "").strip(), str(suggestion.get("rationale") or "").strip()]
        rows.append(
            {
                "selected": bool(suggestion.get("selected", True)),
                "order_line_id": item.get("order_line_id"),
                "sku": item.get("sku"),
                "product_name": item.get("product_name") or item.get("description"),
                "product_code": item.get("product_code") or item.get("sku"),
                "product_desc": item.get("product_desc") or item.get("description") or item.get("product_name"),
                "description": item.get("description") or item.get("product_name"),
                "pack": item.get("pack") or "",
                "qty": suggestion.get("suggested_return_qty") or max_qty,
                "price": item.get("unit_price") or item.get("price") or 0,
                "price_per_lb": price_per_lb,
                "weight_lb": weight_lb,
                "credit_pct": credit_pct,
                "credit_amount": _line_credit_amount(price_per_lb, weight_lb, credit_pct),
                "product_returning": bool(suggestion.get("selected", True)),
                "pack_barcode": "",
                "packs_count": 0,
                "reason_code": suggestion.get("suggested_reason") or "customer_return",
                "reason_for_return": suggestion.get("suggested_reason") or "customer_return",
                "category": service.category_for_reason(suggestion.get("suggested_reason") or "customer_return"),
                "follow_up_action": "Credit",
                "supplier_credit": False,
                "receiving_notes": "",
                "condition": suggestion.get("suggested_condition") or "unopened",
                "notes": "",
                "qty_ordered": item.get("qty_ordered") or max_qty,
                "qty_shipped": item.get("qty_shipped") or max_qty,
                "max_qty": max_qty,
                "rationale": " ".join(part for part in rationale_parts if part),
            }
        )
    return rows


def _deserialize_items_json(raw: str) -> list[dict[str, object]]:
    try:
        parsed = json.loads(raw or "[]")
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    rows: list[dict[str, object]] = []
    for row in parsed:
        if isinstance(row, dict):
            rows.append(row)
    return rows


_SCAN_PREFIX_RE = re.compile(r"^\s*(?:rma|return)\s*[-:_#]?\s*(\d+)\s*$", re.IGNORECASE)
_SCAN_EMBEDDED_RE = re.compile(r"(?:rma|return)\s*[-:_#]?\s*(\d+)", re.IGNORECASE)


def _parse_scanned_rma_id(raw_value: object) -> int | None:
    token = str(raw_value or "").strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    prefixed = _SCAN_PREFIX_RE.match(token)
    if prefixed:
        return int(prefixed.group(1))
    embedded = _SCAN_EMBEDDED_RE.search(token)
    if embedded:
        return int(embedded.group(1))
    return None


def _recent_scans() -> list[dict[str, object]]:
    rows = session.get("returns_recent_scans")
    if not isinstance(rows, list):
        return []
    clean: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rma_id = row.get("rma_id")
        raw_value = str(row.get("raw_value") or "").strip()
        if not isinstance(rma_id, int):
            try:
                rma_id = int(rma_id)
            except Exception:
                continue
        clean.append({"rma_id": int(rma_id), "raw_value": raw_value})
    return clean[:8]


def _push_recent_scan(*, rma_id: int, raw_value: str) -> None:
    existing = _recent_scans()
    normalized_raw = str(raw_value or "").strip()
    deduped = [row for row in existing if int(row.get("rma_id") or 0) != int(rma_id)]
    deduped.insert(0, {"rma_id": int(rma_id), "raw_value": normalized_raw})
    session["returns_recent_scans"] = deduped[:8]


def _order_payload_from_form(existing_order: dict[str, object] | None = None) -> dict[str, object]:
    order = dict(existing_order or {})
    order_id = (request.form.get("manual_order_id") or request.form.get("order_id") or order.get("order_id") or "").strip()
    return {
        "order_id": order_id,
        "customer_id": (request.form.get("customer_id") or order.get("customer_id") or "").strip(),
        "customer_name": (request.form.get("customer_name") or order.get("customer_name") or "").strip(),
        "customer_email": (request.form.get("customer_email") or order.get("customer_email") or "").strip(),
        "customer_phone": (request.form.get("customer_phone") or order.get("customer_phone") or "").strip(),
        "rep_user_id": getattr(current_user, "id", None),
        "rep_name": (request.form.get("rep_name") or getattr(current_user, "full_name", None) or getattr(current_user, "username", "") or "").strip(),
        "date_submitted": (request.form.get("date_submitted") or "").strip(),
        "order_date": (request.form.get("order_date") or order.get("order_date") or "").strip(),
        "date_shipped": (request.form.get("date_shipped") or order.get("date_shipped") or order.get("order_date") or "").strip(),
        "return_type": (request.form.get("return_type") or "").strip(),
        "company": (request.form.get("company") or order.get("company") or "").strip(),
        "advised_customer": _as_bool(request.form.get("advised_customer")),
        "advised_customer_provided": request.form.get("advised_customer") is not None,
        "advised_customer_note": (request.form.get("advised_customer_note") or "").strip(),
        "additional_notes": (request.form.get("additional_notes") or request.form.get("notes") or "").strip(),
        "rec_prod_signoff": _as_bool(request.form.get("rec_prod_signoff")),
        "rec_prod_signed_at": (request.form.get("rec_prod_signed_at") or "").strip(),
        "receiving_notes": (request.form.get("receiving_notes") or "").strip(),
        "ship_to": (request.form.get("ship_to") or order.get("ship_to") or "").strip(),
        "workflow_mode": "legacy",
        "source": "returns_portal",
        "items": order.get("items") or [],
    }


def _parse_order_items_from_form() -> list[dict[str, object]]:
    product_codes = _array_form_values("product_code")
    product_descs = _array_form_values("product_desc")
    prices = _array_form_values("price_per_lb")
    weights = _array_form_values("weight_lb")
    credit_pcts = _array_form_values("credit_pct")
    credits = _array_form_values("credit_amount")
    order_line_ids = _array_form_values("order_line_id")
    product_returning = _array_form_values("product_returning")
    reason_values = _array_form_values("reason_for_return")
    follow_up_actions = _array_form_values("follow_up_action")
    supplier_credit = _array_form_values("supplier_credit")

    row_count = max(
        (
            len(product_codes),
            len(product_descs),
            len(prices),
            len(weights),
            len(credit_pcts),
            len(credits),
            len(order_line_ids),
            len(product_returning),
            len(reason_values),
            len(follow_up_actions),
            len(supplier_credit),
        ),
        default=0,
    )
    if row_count:
        reason_lookup = {
            str(row.get("reason_code") or "").strip().lower(): row
            for row in service.list_reason_codes()
            if str(row.get("reason_code") or "").strip()
        }

        def _value(values: list[str], idx: int) -> str:
            return values[idx] if idx < len(values) else ""

        items: list[dict[str, object]] = []
        for idx in range(row_count):
            product_code = _value(product_codes, idx).strip()
            product_desc = _value(product_descs, idx).strip()
            if not product_code and not product_desc:
                continue
            reason_input = _value(reason_values, idx).strip()
            lookup_row = reason_lookup.get(reason_input.lower()) if reason_input else None
            reason_code = str((lookup_row or {}).get("reason_code") or reason_input).strip()
            reason_text = str((lookup_row or {}).get("reason_text") or reason_input).strip()
            price_per_lb = _as_float(_value(prices, idx), 0.0)
            weight_lb = _as_float(_value(weights, idx), 0.0)
            credit_pct = max(0.0, min(100.0, _as_float(_value(credit_pcts, idx), 100.0)))
            product_returning_raw = _value(product_returning, idx).strip()
            supplier_credit_raw = _value(supplier_credit, idx).strip()
            items.append(
                {
                    "order_line_id": _value(order_line_ids, idx).strip() or None,
                    "sku": product_code or None,
                    "product_name": product_desc or product_code or "Manual line",
                    "product_code": product_code or None,
                    "product_desc": product_desc or product_code or "Manual line",
                    "price_per_lb": price_per_lb,
                    "weight_lb": weight_lb,
                    "credit_pct": credit_pct,
                    "credit_amount": _as_float(_value(credits, idx), _line_credit_amount(price_per_lb, weight_lb, credit_pct)),
                    "product_returning": _as_bool(product_returning_raw, True),
                    "product_returning_provided": bool(product_returning_raw),
                    "pack_barcode": "",
                    "packs_count": 0,
                    "qty": weight_lb,
                    "price": price_per_lb,
                    "reason_code": reason_code or None,
                    "reason_for_return": reason_text or reason_code or None,
                    "category": service.category_for_reason(reason_code, reason_text),
                    "follow_up_action": _value(follow_up_actions, idx).strip(),
                    "supplier_credit": _as_bool(supplier_credit_raw),
                    "supplier_credit_provided": bool(supplier_credit_raw),
                    "condition": "",
                    "notes": "",
                    "receiving_notes": "",
                    "metadata": {},
                }
            )
        if items:
            return items

    raw = (request.form.get("items_json") or "").strip()
    shared_reason = (request.form.get("reason_code") or "").strip()
    shared_condition = (request.form.get("condition") or "").strip()
    shared_notes = (request.form.get("notes") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                items: list[dict[str, object]] = []
                for row in parsed:
                    if not isinstance(row, dict):
                        continue
                    if not row.get("selected"):
                        continue
                    max_qty = max(
                        _as_float(row.get("max_qty"), 0.0),
                        _as_float(row.get("qty_shipped"), 0.0),
                        _as_float(row.get("qty_ordered"), 0.0),
                    )
                    price_per_lb = _as_float(row.get("price_per_lb"), _as_float(row.get("price"), 0))
                    weight_lb = _as_float(row.get("weight_lb"), _as_float(row.get("qty"), 0))
                    credit_pct = max(0.0, min(100.0, _as_float(row.get("credit_pct"), 100.0)))
                    items.append(
                        {
                            "order_line_id": row.get("order_line_id"),
                            "sku": row.get("sku") or row.get("product_code"),
                            "product_name": row.get("product_name") or row.get("product_desc"),
                            "product_code": row.get("product_code") or row.get("sku"),
                            "product_desc": row.get("product_desc") or row.get("product_name") or row.get("description"),
                            "price_per_lb": price_per_lb,
                            "weight_lb": weight_lb,
                            "credit_pct": credit_pct,
                            "credit_amount": _as_float(row.get("credit_amount"), _line_credit_amount(price_per_lb, weight_lb, credit_pct)),
                            "product_returning": _as_bool(row.get("product_returning"), True),
                            "product_returning_provided": "product_returning" in row,
                            "pack_barcode": row.get("pack_barcode") or "",
                            "packs_count": int(_as_float(row.get("packs_count"), 0)),
                            "qty": _as_float(
                                row.get("return_qty") if row.get("return_qty") is not None else row.get("qty"),
                                _as_float(row.get("weight_lb"), 0),
                            ),
                            "price": _as_float(row.get("price"), 0),
                            "reason_code": row.get("reason_code") or shared_reason,
                            "reason_for_return": row.get("reason_for_return") or row.get("reason_code") or shared_reason,
                            "category": row.get("category") or service.category_for_reason(row.get("reason_code") or shared_reason),
                            "follow_up_action": row.get("follow_up_action") or request.form.get("follow_up_action") or "",
                            "supplier_credit": _as_bool(row.get("supplier_credit")),
                            "supplier_credit_provided": "supplier_credit" in row,
                            "condition": row.get("condition") or shared_condition,
                            "notes": row.get("notes") or shared_notes,
                            "receiving_notes": row.get("receiving_notes") or "",
                            "qty_ordered": _as_float(row.get("qty_ordered"), 0),
                            "qty_shipped": _as_float(row.get("qty_shipped"), 0),
                            "max_qty": max_qty,
                            "metadata": {
                                "suggestion_rationale": row.get("rationale") or "",
                                "pack": row.get("pack") or "",
                                "description": row.get("description") or row.get("product_name") or "",
                            },
                        }
                    )
                if items:
                    return items
        except Exception:
            pass

    fallback_order_line = request.form.get("order_line_id") or ""
    fallback_sku = request.form.get("sku") or ""
    fallback_code = request.form.get("product_code") or fallback_sku
    if not fallback_order_line and not fallback_sku:
        return []
    return [
        {
            "order_line_id": fallback_order_line or None,
            "sku": fallback_sku or fallback_code or None,
            "product_name": request.form.get("product_name") or request.form.get("product_desc") or fallback_sku or "Manual line",
            "product_code": fallback_code or None,
            "product_desc": request.form.get("product_desc") or request.form.get("product_name") or fallback_code or "Manual line",
            "price_per_lb": _as_float(request.form.get("price_per_lb"), _as_float(request.form.get("price"), 0)),
            "weight_lb": _as_float(request.form.get("weight_lb"), _as_float(request.form.get("qty"), 1)),
            "credit_pct": max(0.0, min(100.0, _as_float(request.form.get("credit_pct"), 100.0))),
            "credit_amount": _as_float(
                request.form.get("credit_amount"),
                _line_credit_amount(
                    request.form.get("price_per_lb") or request.form.get("price"),
                    request.form.get("weight_lb") or request.form.get("qty"),
                    request.form.get("credit_pct"),
                ),
            ),
            "product_returning": _as_bool(request.form.get("product_returning"), True),
            "product_returning_provided": request.form.get("product_returning") is not None,
            "pack_barcode": request.form.get("pack_barcode") or "",
            "packs_count": int(_as_float(request.form.get("packs_count"), 0)),
            "qty": _as_float(request.form.get("weight_lb"), _as_float(request.form.get("qty"), 1)),
            "price": _as_float(request.form.get("price_per_lb"), _as_float(request.form.get("price"), 0)),
            "reason_code": shared_reason,
            "reason_for_return": request.form.get("reason_for_return") or shared_reason,
            "category": service.category_for_reason(shared_reason, request.form.get("reason_for_return")),
            "follow_up_action": request.form.get("follow_up_action") or "",
            "supplier_credit": _as_bool(request.form.get("supplier_credit")),
            "supplier_credit_provided": request.form.get("supplier_credit") is not None,
            "condition": shared_condition,
            "notes": shared_notes,
            "receiving_notes": request.form.get("receiving_notes") or "",
        }
    ]


_RETURNS_ONLY_OPTIONAL_PERMISSION_BY_FIELD = {
    "returns_perm_create": "returns.create",
    "returns_perm_export": "returns.export",
    "returns_perm_approvals": "returns.approvals.view",
    "returns_perm_scan": "returns.warehouse.scan",
}


def _as_slug_username(value: str) -> str:
    lowered = str(value or "").strip().lower()
    lowered = re.sub(r"[^a-z0-9._-]+", ".", lowered)
    lowered = re.sub(r"[.]{2,}", ".", lowered).strip(".")
    return lowered or "returns.user"


def _ensure_unique_username(db_session, preferred: str) -> str:
    base = _as_slug_username(preferred)
    candidate = base
    suffix = 2
    try:
        from app.auth.models import User
    except Exception as exc:
        raise service.ReturnsError("Unable to load auth models for user provisioning.") from exc

    while True:
        existing = db_session.query(User).filter(func.lower(User.username) == candidate.lower()).first()
        if not existing:
            return candidate
        candidate = f"{base}{suffix}"
        suffix += 1


def _generate_temp_password() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(14))


def _create_returns_only_user() -> dict[str, object]:
    from app.auth.models import SessionLocal, User, replace_user_permission_overrides, replace_user_roles, replace_user_scope_rules

    display_name = str(request.form.get("returns_user_name") or "").strip()
    identity = str(request.form.get("returns_user_email") or "").strip()
    temp_password_input = str(request.form.get("returns_temp_password") or "").strip()
    if not display_name:
        raise service.ReturnsError("Name is required.")
    if not identity:
        raise service.ReturnsError("Email / Username is required.")

    if "@" in identity:
        username_seed = identity.split("@", 1)[0]
        email_value = identity.lower()
    else:
        username_seed = identity
        email_value = None

    name_parts = display_name.split(None, 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else None
    temp_password = temp_password_input or _generate_temp_password()
    optional_permissions = sorted(
        {
            permission
            for field_name, permission in _RETURNS_ONLY_OPTIONAL_PERMISSION_BY_FIELD.items()
            if request.form.get(field_name)
        }
    )

    with SessionLocal() as db_session:
        if email_value:
            conflict_email = db_session.query(User).filter(func.lower(User.email) == email_value.lower()).first()
            if conflict_email:
                raise service.ReturnsError("Email is already in use.")
        username = _ensure_unique_username(db_session, username_seed)
        conflict_username = db_session.query(User).filter(func.lower(User.username) == username.lower()).first()
        if conflict_username:
            raise service.ReturnsError("Username is already in use.")

        user_row = User(
            username=username,
            email=email_value,
            first_name=first_name,
            last_name=last_name,
            role="returns_only",
            returns_only=True,
            is_active=True,
            is_approved=True,
            must_reset_password=True,
            sales_visibility="self",
        )
        user_row.set_password(temp_password)
        db_session.add(user_row)
        db_session.commit()
        db_session.refresh(user_row)
        user_id = int(user_row.id)

    replace_user_roles(user_id, ["returns_only"])
    replace_user_permission_overrides(user_id, optional_permissions)
    replace_user_scope_rules(user_id, {"rep": ["__all__"]})
    bump_permissions_version()
    return {
        "user_id": user_id,
        "username": username,
        "temp_password": temp_password,
        "permissions": sorted({"page.returns.view", "returns.pdf.export", *optional_permissions}),
    }


@portal_bp.get("/health/returns")
def ready():
    payload = service.ready_check()
    status = 200 if payload.get("ok") else 503
    return jsonify(payload), status


@portal_bp.route("/returns", methods=["GET"], strict_slashes=False)
@login_required
@any_permission_required(*RETURNS_HOME_PERMISSIONS)
def index():
    _returns_on()
    status_filter = (request.args.get("status") or "all").strip() or "all"
    rep_filter = (request.args.get("rep") or "").strip() or None
    category_filter = (request.args.get("category") or "").strip() or None
    reason_filter = (request.args.get("reason") or "").strip() or None
    company_filter = (request.args.get("company") or "").strip() or None
    approver_filter = (request.args.get("approver") or "").strip() or None
    order_filter = (request.args.get("order_id") or "").strip() or None
    customer_search = (request.args.get("customer") or "").strip() or None
    from_date = (request.args.get("from") or "").strip() or None
    to_date = (request.args.get("to") or "").strip() or None
    search = (request.args.get("q") or customer_search or "").strip() or None
    export_fmt = (request.args.get("export") or "").strip().lower()
    if export_fmt in {"csv", "xlsx"}:
        return service.tracker_export_response(
            export_fmt,
            status=status_filter,
            rep=rep_filter,
            category=category_filter,
            reason=reason_filter,
            company=company_filter,
            approval_target=approver_filter,
            order_id=order_filter,
            from_date=from_date,
            to_date=to_date,
            search=search,
            actor_user=current_user,
        )
    rows = service.list_rmas(
        status=status_filter,
        rep=rep_filter,
        category=category_filter,
        reason=reason_filter,
        company=company_filter,
        approval_target=approver_filter,
        order_id=order_filter,
        from_date=from_date,
        to_date=to_date,
        search=search,
        actor_user=current_user,
    )
    return render_template(
        "returns/index.html",
        rmas=rows,
        filters={
            "status": status_filter,
            "rep": rep_filter or "",
            "category": category_filter or "",
            "reason": reason_filter or "",
            "company": company_filter or "",
            "approver": approver_filter or "",
            "order_id": order_filter or "",
            "customer": customer_search or "",
            "from": from_date or "",
            "to": to_date or "",
            "q": search or "",
        },
    )


@portal_bp.get("/returns/admin")
@login_required
@permission_required("admin.returns.manage")
def admin_alias():
    _returns_on()
    return redirect(url_for("returns_admin.index"))


@portal_bp.route("/returns/lookup", methods=["GET", "POST"])
@login_required
@any_permission_required(*PORTAL_VIEW_PERMISSIONS)
def lookup():
    _customer_portal_on()
    results: list[dict[str, object]] = []
    if request.method == "POST":
        results = service.search_orders(
            order_id=(request.form.get("order_id") or "").strip() or None,
            email=(request.form.get("email") or "").strip() or None,
            phone=(request.form.get("phone") or "").strip() or None,
            actor_user=current_user,
        )
        if not results:
            flash("No orders matched that lookup inside your current scope.", "warning")
    return render_template("returns/lookup.html", results=results)


@portal_bp.route("/returns/new", methods=["GET", "POST"])
@login_required
@any_permission_required(*PORTAL_CREATE_PERMISSIONS)
def create():
    _customer_portal_on()
    lookup_only = _as_bool(request.form.get("lookup_order")) if request.method == "POST" else False
    submitted_items_json = (request.form.get("items_json") or "").strip() if request.method == "POST" else ""
    submitted_item_rows = _deserialize_items_json(submitted_items_json) if submitted_items_json else []
    if request.method == "POST" and not submitted_item_rows:
        submitted_item_rows = _parse_order_items_from_form()
    order_id = (request.values.get("manual_order_id") or request.values.get("order_id") or "").strip()
    default_date_submitted = request.form.get("date_submitted") or service._vancouver_now().date().isoformat()
    reason_codes = service.list_reason_codes()
    returns_settings = service.get_returns_settings()
    lookup_payload: dict[str, object] | None = None
    lookup_error: str | None = None
    lookup_error_status = 400
    order = None
    order_items_rows: list[dict[str, object]] = []
    if order_id:
        try:
            if service.returns_autofill_order_enabled():
                lookup_payload = service.lookup_order_for_return(order_id, actor_user=current_user)
                if lookup_payload.get("order"):
                    order = dict(lookup_payload["order"])  # type: ignore[index]
                order_items_rows = _lookup_payload_to_form_rows(lookup_payload)
            else:
                order = service.get_order(order_id, actor_user=current_user)
                if order:
                    order_items_rows = [{**item, "selected": True} for item in ((order or {}).get("items") or [])]
        except service.ScopeViolationError as exc:
            lookup_error = str(exc)
            lookup_error_status = 403
        except (service.InvalidOrderLookupError, service.OrderLookupNotFoundError) as exc:
            lookup_error = str(exc)
    order_items_json = json.dumps(order_items_rows) if order_items_rows else ""
    initial_item_rows = submitted_item_rows or order_items_rows
    if request.method == "POST":
        _require_any_permission(*PORTAL_CREATE_PERMISSIONS)
        if lookup_only:
            if lookup_error:
                flash(lookup_error, "danger")
            elif order:
                flash("Order loaded. Review the pre-filled details and continue.", "success")
            elif order_id:
                flash("Order not found.", "warning")
            return _render_create_form(
                order=order,
                order_id=order_id,
                order_items_json=submitted_items_json or order_items_json,
                initial_item_rows=initial_item_rows,
                lookup_payload=lookup_payload,
                lookup_error=lookup_error,
                reason_codes=reason_codes,
                returns_settings=returns_settings,
                default_date_submitted=default_date_submitted,
            )
        if lookup_error and order_id:
            # For enterprise/manual mode, allow proceeding even if order lookup failed with "not found"
            is_not_found = "not found" in lookup_error.lower()
            if not is_not_found:
                flash(lookup_error, "danger")
                return _render_create_form(
                    order=order,
                    order_id=order_id,
                    order_items_json=submitted_items_json or order_items_json,
                    initial_item_rows=initial_item_rows,
                    lookup_payload=lookup_payload,
                    lookup_error=lookup_error,
                    reason_codes=reason_codes,
                    returns_settings=returns_settings,
                    default_date_submitted=default_date_submitted,
                )
            else:
                flash(lookup_error, "warning")
        
        items = _parse_order_items_from_form()
        if not items:
            flash("Select at least one return item.", "danger")
            return _render_create_form(
                order=order,
                order_id=order_id,
                order_items_json=submitted_items_json or order_items_json,
                initial_item_rows=initial_item_rows,
                lookup_payload=lookup_payload,
                lookup_error=lookup_error,
                reason_codes=reason_codes,
                returns_settings=returns_settings,
                default_date_submitted=default_date_submitted,
                status_code=400,
            )
        item_errors = _validate_selected_items(items)
        if item_errors:
            for error in item_errors:
                flash(error, "danger")
            return _render_create_form(
                order=order,
                order_id=order_id,
                order_items_json=submitted_items_json or order_items_json,
                initial_item_rows=initial_item_rows,
                lookup_payload=lookup_payload,
                lookup_error=lookup_error,
                reason_codes=reason_codes,
                returns_settings=returns_settings,
                default_date_submitted=default_date_submitted,
                status_code=400,
            )
        if not order:
            order = _order_payload_from_form()
        else:
            merged_order = dict(order)
            for key, value in _order_payload_from_form(order).items():
                if key == "advised_customer":
                    merged_order[key] = value
                elif value not in (None, "", []):
                    merged_order[key] = value
            order = merged_order
        try:
            created = service.create_rma(
                order_payload=dict(order),
                item_payloads=items,
                actor_user=current_user,
                uploads=request.files.getlist("evidence"),
                workflow_mode="legacy",
            )
        except service.ScopeViolationError as exc:
            flash(str(exc), "danger")
            return _render_create_form(
                order=order,
                order_id=order_id,
                order_items_json=submitted_items_json or order_items_json,
                initial_item_rows=initial_item_rows,
                lookup_payload=lookup_payload,
                lookup_error=lookup_error,
                reason_codes=reason_codes,
                returns_settings=returns_settings,
                default_date_submitted=default_date_submitted,
            )
        except service.ReturnsError as exc:
            flash(str(exc), "danger")
            return _render_create_form(
                order=order,
                order_id=order_id,
                order_items_json=submitted_items_json or order_items_json,
                initial_item_rows=initial_item_rows,
                lookup_payload=lookup_payload,
                lookup_error=lookup_error,
                reason_codes=reason_codes,
                returns_settings=returns_settings,
                default_date_submitted=default_date_submitted,
                status_code=400,
            )
        flash(f"Return #{created['id']} created with status {created['status_label']}.", "success")
        return redirect(url_for("returns_portal.detail", rma_id=created["id"]))
    if lookup_error and order_id:
        flash(lookup_error, "warning" if lookup_error_status == 400 else "danger")
    return render_template(
        "returns/new_return.html" if service.returns_excel_form_enabled() else "returns/new.html",
        order=order,
        order_id=order_id,
        order_items_json=order_items_json,
        initial_item_rows=initial_item_rows,
        lookup_payload=lookup_payload,
        lookup_error=lookup_error,
        reason_codes=reason_codes,
        returns_settings=returns_settings,
        default_date_submitted=default_date_submitted,
        **_returns_v2_context(),
    )


@portal_bp.get("/returns/api/order/<order_id>")
@login_required
@any_permission_required(*PORTAL_CREATE_PERMISSIONS)
def order_lookup_api(order_id: str):
    _customer_portal_on()
    if not service.returns_autofill_order_enabled():
        abort(404)

    cache_key = _order_lookup_cache_key(order_id)
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        payload = json.loads(json.dumps(cached))
        meta = dict(payload.get("meta") or {})
        meta["cached"] = True
        payload["meta"] = meta
        return jsonify(payload), 200

    try:
        payload = service.lookup_order_for_return(order_id, actor_user=current_user)
    except service.InvalidOrderLookupError as exc:
        return jsonify({"error": "invalid_order_id", "message": str(exc)}), 400
    except service.ScopeViolationError as exc:
        message = str(exc)
        error_code = "scope_not_configured" if "no customer access configured" in message.lower() else "not_in_scope"
        status_code = 403 if error_code == "scope_not_configured" else 404
        return jsonify({"error": error_code, "message": message}), status_code
    except service.OrderLookupNotFoundError as exc:
        return jsonify({"error": "not_found", "message": str(exc)}), 404
    except service.ReturnsError as exc:
        return jsonify({"error": "lookup_failed", "message": str(exc)}), 400

    meta = dict(payload.get("meta") or {})
    meta["cached"] = False
    payload["meta"] = meta
    cache.set(
        cache_key,
        payload,
        timeout=int(current_app.config.get("RETURNS_ORDER_LOOKUP_CACHE_SECONDS") or 180),
    )
    return jsonify(payload), 200


@portal_bp.get("/returns/approvals")
@login_required
@any_permission_required(*APPROVALS_VIEW_PERMISSIONS)
def approvals():
    _returns_on()
    if _has_any_permission("admin.returns.manage"):
        statuses = [service.STATUS_PENDING, service.STATUS_WH_APPROVED]
    else:
        statuses: list[str] = []
        if _has_any_permission(*WAREHOUSE_RECEIVE_PERMISSIONS):
            statuses.append(service.STATUS_PENDING)
        if _has_any_permission(*OPS_APPROVE_PERMISSIONS):
            statuses.append(service.STATUS_WH_APPROVED)
        if not statuses:
            statuses = [service.STATUS_PENDING, service.STATUS_WH_APPROVED]
    rows = service.list_rmas(
        statuses=statuses,
        search=(request.args.get("q") or "").strip() or None,
        actor_user=current_user,
    )
    return render_template("returns/approvals.html", rmas=rows, statuses=statuses)


@portal_bp.get("/returns/analytics")
@login_required
@any_permission_required(*ANALYTICS_VIEW_PERMISSIONS)
def analytics():
    _returns_on()
    if not service.returns_analytics_enabled():
        abort(404)
    export_fmt = (request.args.get("export") or "").strip().lower()
    dataset = (request.args.get("dataset") or "").strip() or None
    from_date = (request.args.get("from") or "").strip() or None
    to_date = (request.args.get("to") or "").strip() or None
    if export_fmt in {"xlsx", "csv"}:
        export_kwargs = {
            "dataset": dataset,
            "export_format": export_fmt,
            "actor_user": current_user,
        }
        if from_date or to_date:
            export_kwargs.update({"from_date": from_date, "to_date": to_date})
        return service.returns_analytics_export_response(**export_kwargs)
    snapshot_kwargs = {"actor_user": current_user}
    if from_date or to_date:
        snapshot_kwargs.update({"from_date": from_date, "to_date": to_date})
    payload = service.returns_analytics_snapshot(**snapshot_kwargs)
    frames = payload.get("frames") or {}

    def _records(name: str, head: int | None = None):
        frame = frames.get(name)
        if frame is None or getattr(frame, "empty", True):
            return []
        if head is not None:
            frame = frame.head(head)
        return frame.to_dict(orient="records")

    preview = {
        "volume_by_week": _records("volume_by_week", head=12),
        "credit_by_week": _records("credit_by_week", head=12),
        "approval_funnel": _records("approval_funnel"),
        "top_customers": _records("top_customers", head=10),
        "top_skus": _records("top_skus", head=10),
        "reason_breakdown": _records("reason_breakdown", head=6),
        "category_breakdown": _records("category_breakdown"),
        "approval_sla": _records("approval_sla"),
        "follow_up_breakdown": _records("follow_up_breakdown"),
    }
    return render_template("returns/analytics.html", analytics=payload, preview=preview, filters={"from": from_date or "", "to": to_date or ""})


@portal_bp.post("/returns/<int:rma_id>/approve_wh")
@login_required
@any_permission_required(*WAREHOUSE_RECEIVE_PERMISSIONS)
def approve_wh(rma_id: int):
    _returns_on()
    try:
        service.approve_warehouse(
            rma_id,
            actor_user=current_user,
            notes=(request.form.get("notes") or "").strip() or None,
        )
        flash("Return approved by warehouse.", "success")
    except service.ReturnsError as exc:
        flash(str(exc), "danger")
        return _post_action_redirect(rma_id)
    return _post_action_redirect(rma_id)


@portal_bp.post("/returns/<int:rma_id>/approve_mgr")
@login_required
@any_permission_required(*OPS_APPROVE_PERMISSIONS)
def approve_mgr(rma_id: int):
    _returns_on()
    current_app.logger.info("returns.approve_manager_start", extra={"rma_id": rma_id})
    try:
        payload = service.approve_manager(
            rma_id,
            actor_user=current_user,
            notes=(request.form.get("notes") or "").strip() or None,
        )
    except service.ReturnsError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("returns_portal.approvals"))
    return send_file(
        BytesIO(payload["pdf_bytes"]),  # type: ignore[index]
        mimetype="application/pdf",
        as_attachment=True,
        download_name=str(payload.get("filename") or f"credit-po-return-{rma_id}.pdf"),
    )


@portal_bp.post("/returns/<int:rma_id>/reject")
@login_required
@any_permission_required(*REJECT_PERMISSIONS)
def reject(rma_id: int):
    _returns_on()
    try:
        service.reject_rma(
            rma_id,
            actor_user=current_user,
            reason=(request.form.get("reject_reason") or request.form.get("notes") or "").strip() or None,
        )
        flash("Return rejected.", "success")
    except service.ReturnsError as exc:
        flash(str(exc), "danger")
    return _post_action_redirect(rma_id)


@portal_bp.get("/returns/<int:rma_id>/export/sage.csv")
@login_required
@any_permission_required(*PORTAL_VIEW_PERMISSIONS)
def export_sage_csv(rma_id: int):
    _returns_on()
    try:
        csv_bytes = service.export_sage_csv([rma_id], actor_user=current_user)
    except service.ReturnsError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("returns_portal.detail", rma_id=rma_id))
    
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=return-sage-{rma_id}.csv"},
    )


@portal_bp.get("/returns/export/sage-batch.csv")
@login_required
@any_permission_required(*PORTAL_VIEW_PERMISSIONS)
def export_sage_csv_batch():
    _returns_on()
    ids_raw = request.args.get("ids", "")
    try:
        rma_ids = [int(s.strip()) for s in ids_raw.split(",") if s.strip().isdigit()]
        if not rma_ids:
            flash("No returns selected for batch export.", "warning")
            return redirect(url_for("returns_portal.index"))
        
        csv_bytes = service.export_sage_csv(rma_ids, actor_user=current_user)
        filename = f"batch-sage-export-{datetime.now().strftime('%Y%m%d-%H%M')}.csv"
        return Response(
            csv_bytes,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment;filename={filename}"},
        )
    except Exception as exc:
        flash(f"Batch export failed: {exc}", "danger")
        return redirect(url_for("returns_portal.index"))


@portal_bp.post("/returns/batch-complete")
@login_required
@any_permission_required(*OPS_APPROVE_PERMISSIONS)
def batch_complete():
    _returns_on()
    ids = request.form.getlist("ids")
    success_count = 0
    errors = []
    for rma_id in ids:
        try:
            service.complete_rma(int(rma_id), actor_user=current_user)
            success_count += 1
        except Exception as exc:
            errors.append(f"RMA #{rma_id}: {exc}")
    
    if errors:
        flash(f"Batch complete finished with {success_count} successes and {len(errors)} errors.", "warning")
    else:
        flash(f"Successfully closed {success_count} returns.", "success")
    return jsonify({"ok": True, "success_count": success_count, "error_count": len(errors)}), 200


@portal_bp.get("/returns/<int:rma_id>/pdf")
@login_required
@any_permission_required(*PDF_EXPORT_PERMISSIONS)
def export_pdf(rma_id: int):
    _returns_on()
    try:
        pdf_bytes = service.build_return_pdf_bytes(rma_id, actor_user=current_user)
    except service.ScopeViolationError as exc:
        flash(str(exc), "danger")
        abort(403, description=str(exc))
    except service.ReturnsError as exc:
        flash(str(exc), "danger")
        abort(404)
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"return-{rma_id}.pdf",
    )


@portal_bp.route("/returns/<int:rma_id>", methods=["GET", "POST"])
@login_required
@any_permission_required(*PORTAL_VIEW_PERMISSIONS)
def detail(rma_id: int):
    _customer_portal_on()
    if request.method == "POST":
        form_action = (request.form.get("form_action") or "").strip().lower()
        if form_action == "update_receiving":
            try:
                _require_any_permission(*RECEIVING_EDIT_PERMISSIONS)
                service.update_receiving_review(
                    rma_id,
                    actor_user=current_user,
                    header_updates={
                        "rec_prod_signoff": request.form.get("rec_prod_signoff"),
                        "rec_prod_signed_at": request.form.get("rec_prod_signed_at"),
                        "receiving_notes": request.form.get("receiving_notes"),
                    },
                    item_updates=_receiving_item_updates_from_form(),
                )
                if _as_bool(request.form.get("complete_after_save")):
                    _require_any_permission(*OPS_APPROVE_PERMISSIONS)
                    service.complete_rma(
                        rma_id,
                        actor_user=current_user,
                        notes=(request.form.get("completion_notes") or "").strip() or None,
                    )
                    flash("Receiving updates saved and return marked completed.", "success")
                else:
                    flash("Receiving updates saved.", "success")
                return redirect(url_for("returns_portal.detail", rma_id=rma_id))
            except service.ScopeViolationError as exc:
                flash(str(exc), "danger")
                abort(403, description=str(exc))
            except service.ReturnsError as exc:
                flash(str(exc), "danger")
                return render_template("returns/detail.html", rma=_load_rma_or_404(rma_id)), 400
        if form_action == "schedule_pickup":
            try:
                service.schedule_pickup(rma_id, actor_user=current_user)
                flash("Pickup scheduled.", "success")
                return redirect(url_for("returns_portal.detail", rma_id=rma_id))
            except service.ReturnsError as exc:
                flash(str(exc), "danger")
                return render_template("returns/detail.html", rma=_load_rma_or_404(rma_id)), 400
        if form_action == "mark_picked_up":
            try:
                service.mark_picked_up(rma_id, actor_user=current_user)
                flash("Return marked as picked up.", "info")
                return redirect(url_for("returns_portal.detail", rma_id=rma_id))
            except service.ReturnsError as exc:
                flash(str(exc), "danger")
                return render_template("returns/detail.html", rma=_load_rma_or_404(rma_id)), 400
        if form_action == "complete":
            try:
                _require_any_permission(*OPS_APPROVE_PERMISSIONS)
                if request.form.getlist("item_ids"):
                    service.update_receiving_review(
                        rma_id,
                        actor_user=current_user,
                        header_updates={
                            "rec_prod_signoff": request.form.get("rec_prod_signoff"),
                            "rec_prod_signed_at": request.form.get("rec_prod_signed_at"),
                            "receiving_notes": request.form.get("receiving_notes"),
                        },
                        item_updates=_receiving_item_updates_from_form(),
                    )
                service.complete_rma(
                    rma_id,
                    actor_user=current_user,
                    notes=(request.form.get("completion_notes") or "").strip() or None,
                )
                flash("Return marked completed.", "success")
                return redirect(url_for("returns_portal.detail", rma_id=rma_id))
            except service.ScopeViolationError as exc:
                flash(str(exc), "danger")
                abort(403, description=str(exc))
            except service.ReturnsError as exc:
                flash(str(exc), "danger")
                return render_template("returns/detail.html", rma=_load_rma_or_404(rma_id)), 400
        comment_body = (request.form.get("comment_body") or "").strip()
        uploads = request.files.getlist("evidence")
        if not uploads and not comment_body:
            flash("Add a comment or choose at least one file to upload.", "warning")
        else:
            try:
                if comment_body:
                    service.add_comment(rma_id, body=comment_body, actor_user=current_user)
                for upload in uploads:
                    service.save_attachment(rma_id=rma_id, upload=upload, actor_user=current_user)
                if uploads and comment_body:
                    flash("Comment saved and evidence uploaded.", "success")
                elif uploads:
                    flash("Evidence uploaded.", "success")
                else:
                    flash("Comment saved.", "success")
            except service.ScopeViolationError as exc:
                flash(str(exc), "danger")
                abort(403, description=str(exc))
            except service.ReturnsError as exc:
                flash(str(exc), "danger")
                return render_template("returns/detail.html", rma=_load_rma_or_404(rma_id)), 400
    payload = _load_rma_or_404(rma_id)
    
    # Calculate permissions explicitly for the template
    from app.core.rbac import has_permission
    perms = {
        "can_ops": has_permission("returns.ops.queue.view") or has_permission("admin.returns.manage"),
        "can_wh_approve": has_permission("returns.approve.wh") or has_permission("returns.warehouse.receive") or has_permission("admin.returns.manage"),
        "can_mgr_approve": has_permission("returns.approve.mgr") or has_permission("returns.ops.approve") or has_permission("admin.returns.manage"),
        "can_finance": has_permission("returns.finance.close") or has_permission("admin.returns.manage"),
        "can_reject": has_permission("returns.reject") or has_permission("admin.returns.manage"),
    }
    
    return render_template("returns/detail.html", rma=payload, **perms)


@ops_bp.get("/queue")
@login_required
@any_permission_required(*OPS_VIEW_PERMISSIONS)
def queue():
    _returns_on()
    rows = service.list_rmas(
        status=(request.args.get("status") or "").strip() or None,
        customer_id=(request.args.get("customer_id") or "").strip() or None,
        actor_user=current_user,
    )
    return render_template("returns/ops_queue.html", rmas=rows)


@ops_bp.route("/<int:rma_id>", methods=["GET", "POST"])
@login_required
@any_permission_required(*OPS_VIEW_PERMISSIONS)
def review(rma_id: int):
    _returns_on()
    if request.method == "POST":
        action = (request.form.get("action") or "").strip().lower()
        notes = (request.form.get("notes") or "").strip()
        try:
            if action == "approve":
                _require_any_permission(*OPS_APPROVE_PERMISSIONS)
                service.transition_rma(
                    rma_id,
                    service.STATUS_AWAITING_RETURN,
                    actor_user=current_user,
                    payload={"decision_summary": notes or "Approved by manager."},
                )
            elif action == "deny":
                _require_any_permission(*OPS_DENY_PERMISSIONS)
                service.transition_rma(
                    rma_id,
                    service.STATUS_DENIED,
                    actor_user=current_user,
                    payload={"decision_summary": notes or "Denied by manager."},
                )
            elif action == "override":
                _require_any_permission(*OPS_OVERRIDE_PERMISSIONS)
                override_status = (request.form.get("override_status") or "").strip().lower()
                service.transition_rma(
                    rma_id,
                    override_status,
                    actor_user=current_user,
                    payload={"decision_summary": notes or "Manual override."},
                )
            elif action == "label":
                _require_any_permission(*LABEL_PERMISSIONS)
                service.issue_label(rma_id, actor_user=current_user, carrier=request.form.get("carrier") or "manual")
            elif action == "refund":
                _require_any_permission(*REFUND_PERMISSIONS)
                try:
                    amount = float((request.form.get("refund_amount") or "0").strip() or "0")
                except ValueError as exc:
                    raise service.ReturnsError("Refund amount must be numeric.") from exc
                if amount <= 0:
                    raise service.ReturnsError("Refund amount must be greater than zero.")
                service.issue_refund(
                    rma_id,
                    amount=amount,
                    method=(request.form.get("refund_method") or "manual").strip() or "manual",
                    actor_user=current_user,
                    processor_ref=(request.form.get("refund_processor_ref") or "").strip() or None,
                )
            else:
                flash("Unknown action.", "warning")
                return redirect(url_for("returns_ops.review", rma_id=rma_id))
        except service.ReturnsError as exc:
            flash(str(exc), "danger")
            return render_template("returns/ops_detail.html", rma=_load_rma_or_404(rma_id)), 400
        flash("Return workflow updated.", "success")
        return redirect(url_for("returns_ops.review", rma_id=rma_id))
    payload = _load_rma_or_404(rma_id)
    return render_template("returns/ops_detail.html", rma=payload)


@warehouse_bp.route("/scan", methods=["GET", "POST"])
@login_required
@any_permission_required(*WAREHOUSE_SCAN_PERMISSIONS)
def scan():
    _returns_on()
    scan_error: str | None = None
    if request.method == "POST":
        raw = (request.form.get("rma_id") or "").strip()
        rma_id = _parse_scanned_rma_id(raw)
        if rma_id is None:
            scan_error = "Enter a valid RMA value (numeric, RMA-123, or RETURN-123)."
        else:
            try:
                payload = service.get_rma_detail(rma_id, actor_user=current_user)
            except service.ScopeViolationError:
                payload = None
            if payload:
                _push_recent_scan(rma_id=int(rma_id), raw_value=raw)
                return redirect(url_for("returns_warehouse.inspect_or_receive", rma_id=int(rma_id)))
            scan_error = f"No return found for scanned value '{raw}'."
    return render_template(
        "returns/warehouse_scan.html",
        scan_error=scan_error,
        recent_scans=_recent_scans(),
    )


@warehouse_bp.route("/<int:rma_id>", methods=["GET"])
@login_required
@any_permission_required(*WAREHOUSE_VIEW_PERMISSIONS)
def inspect_or_receive(rma_id: int):
    _returns_on()
    payload = _load_rma_or_404(rma_id)
    return render_template("returns/warehouse_detail.html", rma=payload)


@warehouse_bp.post("/<int:rma_id>/receive")
@login_required
@any_permission_required(*WAREHOUSE_VIEW_PERMISSIONS)
def receive(rma_id: int):
    _returns_on()
    try:
        _require_any_permission(*WAREHOUSE_RECEIVE_PERMISSIONS)
        service.receive_rma(rma_id, actor_user=current_user)
        flash("Return marked as received.", "success")
    except service.ReturnsError as exc:
        flash(str(exc), "danger")
        return render_template("returns/warehouse_detail.html", rma=_load_rma_or_404(rma_id)), 400
    return redirect(url_for("returns_warehouse.inspect_or_receive", rma_id=rma_id))


@warehouse_bp.route("/<int:rma_id>/inspect", methods=["GET", "POST"])
@login_required
@any_permission_required(*WAREHOUSE_VIEW_PERMISSIONS)
def inspect(rma_id: int):
    _returns_on()
    if request.method == "POST":
        try:
            _require_any_permission(*WAREHOUSE_INSPECT_PERMISSIONS)
            service.inspect_rma(
                rma_id,
                disposition=(request.form.get("disposition") or "").strip(),
                notes=(request.form.get("notes") or "").strip(),
                actor_user=current_user,
                photo_uploads=request.files.getlist("photos"),
            )
            flash("Inspection saved.", "success")
            return redirect(url_for("returns_warehouse.inspect_or_receive", rma_id=rma_id))
        except service.ReturnsError as exc:
            flash(str(exc), "danger")
            return render_template("returns/warehouse_detail.html", rma=_load_rma_or_404(rma_id)), 400
    payload = _load_rma_or_404(rma_id)
    return render_template("returns/warehouse_detail.html", rma=payload)


@admin_bp.route("/", methods=["GET", "POST"])
@login_required
@permission_required("admin.returns.manage")
def index():
    _returns_on()
    returns_user_result: dict[str, object] | None = None
    if request.method == "POST":
        form_action = (request.form.get("form_action") or "save_policy").strip().lower()
        if form_action == "create_returns_user":
            try:
                returns_user_result = _create_returns_only_user()
                flash("Returns-only user created.", "success")
            except service.ReturnsError as exc:
                flash(str(exc), "danger")
            except Exception:
                current_app.logger.exception("returns.admin_create_returns_only_user_failed")
                flash("Failed to create returns-only user.", "danger")
        else:
            version = (request.form.get("version") or "").strip()
            rules_raw = (request.form.get("rules_json") or "{}").strip()
            try:
                rules = json.loads(rules_raw)
                service.create_policy_version(version, rules, activate=bool(request.form.get("activate")))
                flash("Return policy saved.", "success")
                return redirect(url_for("returns_admin.index"))
            except Exception as exc:
                flash(f"Failed to save policy: {exc}", "danger")
    return render_template(
        "admin/returns/index.html",
        policies=service.list_policy_versions(),
        templates=service.email_templates_catalog(),
        integrations=service.integrations_status(),
        returns_user_result=returns_user_result,
    )


@admin_bp.get("/policies")
@login_required
@permission_required("admin.returns.manage")
def policies():
    _returns_on()
    return render_template("admin/returns/index.html", policies=service.list_policy_versions(), templates=service.email_templates_catalog(), integrations=service.integrations_status())


@admin_bp.get("/templates")
@login_required
@permission_required("admin.returns.manage")
def templates():
    _returns_on()
    return render_template("admin/returns/index.html", policies=service.list_policy_versions(), templates=service.email_templates_catalog(), integrations=service.integrations_status())


@admin_bp.get("/integrations")
@login_required
@permission_required("admin.returns.manage")
def integrations():
    _returns_on()
    return render_template("admin/returns/index.html", policies=service.list_policy_versions(), templates=service.email_templates_catalog(), integrations=service.integrations_status())


@admin_bp.route("/reasons", methods=["GET", "POST"])
@login_required
@permission_required("admin.returns.manage")
def reasons():
    _returns_on()
    if request.method == "POST":
        try:
            service.save_reason_code(
                reason_code=(request.form.get("reason_code") or "").strip(),
                reason_text=(request.form.get("reason_text") or "").strip(),
                category=(request.form.get("category") or "Other").strip(),
                active=bool(request.form.get("active")),
            )
            flash("Reason mapping saved.", "success")
            return redirect(url_for("returns_admin.reasons"))
        except service.ReturnsError as exc:
            flash(str(exc), "danger")
    return render_template("admin/returns/reasons.html", reasons=service.list_reason_codes(active_only=False))


@admin_bp.route("/settings", methods=["GET", "POST"])
@login_required
@permission_required("admin.returns.manage")
def settings():
    _returns_on()
    if request.method == "POST":
        payload = {
            "defaults": {
                "return_type": (request.form.get("default_return_type") or "").strip() or "Sales Return",
                "follow_up_action": (request.form.get("default_follow_up_action") or "").strip() or "Credit",
            },
            "workflow_options": {
                "follow_up_actions": [s.strip() for s in (request.form.get("follow_up_actions") or "").split(",") if s.strip()],
                "warehouse_outcomes": [s.strip() for s in (request.form.get("warehouse_outcomes") or "").split(",") if s.strip()],
                "companies": [s.strip() for s in (request.form.get("companies") or "").split(",") if s.strip()],
            },
            "email_templates": {
                "new_return_subject": (request.form.get("new_return_subject") or "").strip(),
                "wh_approval_subject": (request.form.get("wh_approval_subject") or "").strip(),
                "manager_approval_subject": (request.form.get("manager_approval_subject") or "").strip(),
                "rejection_subject": (request.form.get("rejection_subject") or "").strip(),
            },
        }
        service.save_returns_settings(payload, actor_user=current_user)
        flash("Returns settings saved.", "success")
        return redirect(url_for("returns_admin.settings"))
    return render_template("admin/returns/settings.html", settings=service.get_returns_settings())


@admin_bp.get("/self-check")
@login_required
@permission_required("admin.returns.manage")
def self_check():
    _returns_on()
    payload = service.ready_check()
    status = 200 if payload.get("ok") else 503
    return jsonify(payload), status


@webhooks_bp.post("/<source>")
def webhook(source: str):
    _returns_on()
    raw = request.get_data(cache=False)
    signature = request.headers.get("X-Returns-Signature") or request.headers.get("X-Signature")
    if not service.verify_webhook_signature(raw, signature, current_app.config.get("RETURNS_WEBHOOK_SECRET")):
        return jsonify({"error": "invalid_signature"}), 401
    payload = request.get_json(silent=True) or {}
    event_type = str(payload.get("event_type") or payload.get("type") or "event")
    idempotency_key = (
        request.headers.get("Idempotency-Key")
        or payload.get("idempotency_key")
        or payload.get("event_id")
        or ""
    )
    result, created = service.process_webhook(
        source=source,
        event_type=event_type,
        idempotency_key=str(idempotency_key),
        payload=payload,
        actor_user=current_user if getattr(current_user, "is_authenticated", False) else None,
    )
    return jsonify({"created": created, "event": result}), 200
