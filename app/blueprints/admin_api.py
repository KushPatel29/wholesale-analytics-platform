from __future__ import annotations

import re
import secrets
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any

import pandas as pd
from flask import Blueprint, jsonify, request, current_app
from flask_login import login_required, current_user
from sqlalchemy import case, func, or_
import json
from decimal import Decimal

from app.cache import cache
from ..core.access_policy import require_admin, bump_permissions_version, permissions_version
from ..core.rbac import ALLOWED_ROLES as RBAC_ROLES, permission_required
from ..core.audit import log_audit, log_audit_change
from ..auth.models import (
    SessionLocal,
    User,
    AuditLog,
    _coerce_scope_type,
    list_visibility_for_user,
    replace_visibility_for_user,
    UserVisibilitySalesRep,
    UserRole,
    UserScopeRule,
    UserScopeGroup,
    ScopeGroup,
    ScopeGroupMember,
    list_user_role_names,
    replace_user_roles,
    list_effective_permission_keys_for_user,
    list_user_permission_overrides,
    list_user_permission_rules,
    replace_user_permission_overrides,
    replace_user_permission_rules,
    list_user_scope_rules,
    replace_user_scope_rules,
    list_roles,
    list_permissions,
    list_role_permissions,
    replace_role_permissions,
    Role,
)
from ..auth.permissions import (
    DEFAULT_ROLE_PERMISSION_KEYS,
    canonicalize_permission_keys,
    normalize_permission_selection,
    permission_editor_schema,
    permission_registry,
    permission_selection_warnings,
)
from ..auth.password_tokens import (
    build_set_password_link,
    invalidate_password_tokens,
    issue_password_token,
    request_ip as token_request_ip,
    request_user_agent as token_request_user_agent,
)
from ..limiter import limiter
from ..services.user_invites import send_password_email
from workers import refresh as refresh_worker

bp = Blueprint("admin_api", __name__, url_prefix="/api/_admin")

LIVE_SQL_ALLOWED = (
    str(os.getenv("ALLOW_LIVE_SQL", "")).strip().lower() in {"1", "true", "yes", "on"}
    and (os.getenv("APP_MODE", "web").strip().lower() in {"etl", "job"})
)

ALL_SCOPE_TOKEN = "__all__"
ALL_SCOPE_TOKENS = {ALL_SCOPE_TOKEN, "all", "*"}
_ADMIN_METADATA_CACHE_TTL = 60 * 10
_ADMIN_USER_PERMISSIONS_CACHE_TTL = 60


@bp.after_request
def _disable_admin_api_caching(response):
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    vary = [part.strip() for part in str(response.headers.get("Vary") or "").split(",") if part.strip()]
    if "Cookie" not in vary:
        vary.append("Cookie")
    response.headers["Vary"] = ", ".join(vary)
    return response


def _error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _invites_enabled() -> bool:
    return bool(current_app.config.get("INVITES_ENABLED", True))


def _admin_debug_enabled() -> bool:
    raw = current_app.config.get("ADMIN_DEBUG")
    if raw is None:
        raw = os.getenv("ADMIN_DEBUG", "")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _cached_permission_registry() -> list[dict[str, Any]]:
    cache_key = "admin.permissions.registry.v1"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    data = permission_registry()
    cache.set(cache_key, data, timeout=_ADMIN_METADATA_CACHE_TTL)
    return data


def _cached_permission_editor_schema() -> dict[str, Any]:
    cache_key = "admin.permissions.editor_schema.v1"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    data = permission_editor_schema()
    cache.set(cache_key, data, timeout=_ADMIN_METADATA_CACHE_TTL)
    return data


def _debug_where_clause(query) -> str:
    try:
        compiled = str(query.statement.compile())
    except Exception:
        return ""
    marker = compiled.upper().find(" WHERE ")
    if marker < 0:
        return ""
    return compiled[marker + 7 :].strip()


def _issue_password_link(
    session,
    *,
    user: User,
    purpose: str,
    created_by_user_id: int | None,
    invalidate_existing: bool,
) -> str:
    token = issue_password_token(
        session,
        user_id=int(user.id),
        purpose=purpose,
        created_by_user_id=created_by_user_id,
        request_ip_addr=token_request_ip(),
        request_user_agent_value=token_request_user_agent(),
        invalidate_existing=invalidate_existing,
    )
    return build_set_password_link(token)


def _send_password_email_for_user(*, email: str, full_name: str | None, purpose: str, link: str) -> bool:
    return send_password_email(
        to_email=str(email or "").strip(),
        recipient_name=full_name,
        set_password_link=link,
        purpose=purpose,
    )


def _clean_tokens(raw) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.replace(";", ",").replace("|", ",")
        return [p.strip() for p in raw.split(",") if p.strip()]
    vals: List[str] = []
    try:
        for item in raw:
            if item is None:
                continue
            s = str(item).strip()
            if s:
                vals.append(s)
    except Exception:
        pass
    return vals


def _validate_visibility(payload: dict) -> Tuple[list[str], Optional[str]]:
    data = payload or {}
    raw = (
        data.get("visibility")
        or data.get("visible_erp_user_ids")
        or data.get("allowed_erp_user_ids")
        or data.get("sales_rep_ids")
        or []
    )
    ids = _clean_tokens(raw)
    return ids, None


def _as_bool(value: Any) -> Tuple[Optional[bool], Optional[str]]:
    if isinstance(value, bool):
        return value, None
    if value is None:
        return None, None
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value), None
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"true", "1", "yes", "on"}:
            return True, None
        if v in {"false", "0", "no", "off"}:
            return False, None
    return None, "Expected a boolean value"


def _serialize_user(user: User, visibility: Optional[list[str]] = None) -> dict:
    vis = visibility if visibility is not None else list_visibility_for_user(int(user.id))
    scope_rules = list_user_scope_rules(int(user.id))
    role_names = list_user_role_names(int(user.id))
    if not role_names:
        role_names = [str(user.role or "sales").strip().lower()]
    direct_permission_rules = list_user_permission_rules(int(user.id))
    direct_permissions = direct_permission_rules.get("allow") or []
    effective_permissions = list_effective_permission_keys_for_user(int(user.id), fallback_role=user.role)
    effective_permission_set = canonicalize_permission_keys(effective_permissions)

    rep_scope = sorted({str(v).strip() for v in (scope_rules.get("rep") or []) + list(vis or []) if str(v).strip()})
    customer_scope = sorted({str(v).strip() for v in (scope_rules.get("customer") or []) if str(v).strip()})
    region_scope = sorted({str(v).strip() for v in (scope_rules.get("region") or []) if str(v).strip()})
    supplier_scope = sorted({str(v).strip() for v in (scope_rules.get("supplier") or []) if str(v).strip()})
    rep_scope = _normalize_rep_scope_values(rep_scope)
    has_all_rep_scope = any(str(v).strip().lower() in ALL_SCOPE_TOKENS for v in rep_scope)

    status = "pending"
    if user.is_active and user.is_approved:
        status = "active"
    elif not user.is_active:
        status = "disabled"
    role = (user.role or "").strip().lower()
    is_admin = ("admin" in role_names) or role == "admin"
    scope_mode = "all" if (is_admin or has_all_rep_scope) else ("list" if (rep_scope or customer_scope or region_scope or supplier_scope) else "none")
    data = {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": bool(user.is_active),
        "is_approved": bool(user.is_approved),
        "sales_rep_id": user.sales_rep_id,
        "erp_user_id": user.erp_user_id,
        "region_id": user.region_id,
        "sales_visibility": user.sales_visibility,
        "status": status,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "updated_at": None,
        "visibility": vis,
        "visibility_count": len(vis),
        "roles": role_names,
        "permissions": sorted(effective_permission_set),
        "user_permission_overrides": sorted(direct_permissions),
        "user_permission_rules": {
            "allow": sorted(direct_permission_rules.get("allow") or []),
            "deny": sorted(direct_permission_rules.get("deny") or []),
        },
        "scope": {
            "scope_mode": scope_mode,
            "allowed_erp_user_ids": rep_scope,
            "allowed_customer_ids": customer_scope,
            "allowed_region_ids": region_scope,
            "allowed_supplier_ids": supplier_scope,
            "allowed_count": len(rep_scope),
            "allowed_customer_count": len(customer_scope),
            "allowed_region_count": len(region_scope),
            "allowed_supplier_count": len(supplier_scope),
        },
    }
    updated_at = getattr(user, "updated_at", None)
    if updated_at:
        data["updated_at"] = updated_at.isoformat()
    return data


def _status_from_user_flags(*, is_active: Any, is_approved: Any) -> str:
    if bool(is_active) and bool(is_approved):
        return "active"
    if not bool(is_active):
        return "disabled"
    return "pending"


def _row_mapping(row: Any) -> dict[str, Any]:
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return dict(mapping)
    if isinstance(row, dict):
        return dict(row)
    return {}


def _batch_role_names(session, user_ids: List[int]) -> Dict[int, List[str]]:
    if not user_ids:
        return {}
    rows = (
        session.query(UserRole.user_id, Role.name)
        .join(Role, Role.id == UserRole.role_id)
        .filter(UserRole.user_id.in_(user_ids))
        .all()
    )
    role_map: Dict[int, set[str]] = {int(user_id): set() for user_id in user_ids}
    for user_id, role_name in rows:
        if role_name:
            role_map.setdefault(int(user_id), set()).add(str(role_name).strip().lower())
    return {user_id: sorted(values) for user_id, values in role_map.items()}


def _batch_scope_rules(session, user_ids: List[int]) -> Dict[int, Dict[str, List[str]]]:
    if not user_ids:
        return {}
    results: Dict[int, Dict[str, set[str]]] = {
        int(user_id): {"rep": set(), "customer": set(), "region": set(), "supplier": set()}
        for user_id in user_ids
    }
    direct_rows = (
        session.query(UserScopeRule.user_id, UserScopeRule.scope_type, UserScopeRule.scope_value)
        .filter(
            UserScopeRule.user_id.in_(user_ids),
            UserScopeRule.scope_mode == "allow",
        )
        .all()
    )
    for user_id, scope_type, scope_value in direct_rows:
        canonical = _coerce_scope_type(scope_type)
        value = str(scope_value or "").strip()
        if canonical and value:
            results.setdefault(int(user_id), {"rep": set(), "customer": set(), "region": set(), "supplier": set()})
            results[int(user_id)].setdefault(canonical, set()).add(value)

    group_rows = (
        session.query(UserScopeGroup.user_id, ScopeGroup.scope_type, ScopeGroupMember.scope_value)
        .join(ScopeGroup, ScopeGroup.id == UserScopeGroup.group_id)
        .join(ScopeGroupMember, ScopeGroupMember.group_id == ScopeGroup.id)
        .filter(UserScopeGroup.user_id.in_(user_ids))
        .all()
    )
    for user_id, scope_type, scope_value in group_rows:
        canonical = _coerce_scope_type(scope_type)
        value = str(scope_value or "").strip()
        if canonical and value:
            results.setdefault(int(user_id), {"rep": set(), "customer": set(), "region": set(), "supplier": set()})
            results[int(user_id)].setdefault(canonical, set()).add(value)

    return {
        user_id: {scope_type: sorted(values) for scope_type, values in scope_map.items()}
        for user_id, scope_map in results.items()
    }


def _serialize_user_listing_row(
    row: Any,
    *,
    visibility: Optional[list[str]] = None,
    role_names: Optional[list[str]] = None,
    scope_rules: Optional[Dict[str, List[str]]] = None,
) -> dict[str, Any]:
    payload = _row_mapping(row)
    user_id = int(payload.get("id") or 0)
    vis = [str(value).strip() for value in (visibility or []) if str(value).strip()]
    scope_rules = scope_rules or {"rep": [], "customer": [], "region": [], "supplier": []}
    roles = [str(role).strip().lower() for role in (role_names or []) if str(role).strip()]
    if not roles:
        fallback_role = str(payload.get("role") or "sales").strip().lower()
        roles = [fallback_role] if fallback_role else ["sales"]

    rep_scope = _normalize_rep_scope_values((scope_rules.get("rep") or []) + vis)
    customer_scope = sorted({str(v).strip() for v in (scope_rules.get("customer") or []) if str(v).strip()})
    region_scope = sorted({str(v).strip() for v in (scope_rules.get("region") or []) if str(v).strip()})
    supplier_scope = sorted({str(v).strip() for v in (scope_rules.get("supplier") or []) if str(v).strip()})
    has_all_rep_scope = any(str(v).strip().lower() in ALL_SCOPE_TOKENS for v in rep_scope)
    role = str(payload.get("role") or "").strip().lower()
    is_admin = ("admin" in roles) or role == "admin"
    scope_mode = "all" if (is_admin or has_all_rep_scope) else ("list" if (rep_scope or customer_scope or region_scope or supplier_scope) else "none")

    created_at = payload.get("created_at")
    updated_at = payload.get("updated_at")
    last_login_at = payload.get("last_login_at")
    return {
        "id": user_id,
        "username": payload.get("username"),
        "email": payload.get("email"),
        "first_name": payload.get("first_name"),
        "last_name": payload.get("last_name"),
        "full_name": " ".join(
            [part for part in [str(payload.get("first_name") or "").strip(), str(payload.get("last_name") or "").strip()] if part]
        )
        or payload.get("username")
        or "",
        "role": payload.get("role"),
        "roles": roles,
        "is_active": bool(payload.get("is_active")),
        "is_approved": bool(payload.get("is_approved")),
        "status": _status_from_user_flags(is_active=payload.get("is_active"), is_approved=payload.get("is_approved")),
        "sales_rep_id": payload.get("sales_rep_id"),
        "erp_user_id": payload.get("erp_user_id"),
        "region_id": payload.get("region_id"),
        "sales_visibility": payload.get("sales_visibility"),
        "created_at": created_at.isoformat() if created_at else None,
        "last_login_at": last_login_at.isoformat() if last_login_at else None,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "visibility": vis,
        "visibility_count": len(vis),
        "scope": {
            "scope_mode": scope_mode,
            "allowed_erp_user_ids": rep_scope,
            "allowed_customer_ids": customer_scope,
            "allowed_region_ids": region_scope,
            "allowed_supplier_ids": supplier_scope,
            "allowed_count": len(rep_scope),
            "allowed_customer_count": len(customer_scope),
            "allowed_region_count": len(region_scope),
            "allowed_supplier_count": len(supplier_scope),
        },
    }


def _get_visibility_map(session, user_ids: List[int]) -> Dict[int, List[str]]:
    if not user_ids:
        return {}
    rows = (
        session.query(UserVisibilitySalesRep.app_user_id, UserVisibilitySalesRep.visible_erp_user_id)
        .filter(UserVisibilitySalesRep.app_user_id.in_(user_ids))
        .all()
    )
    vis_map: Dict[int, List[str]] = {uid: [] for uid in user_ids}
    for uid, erp_id in rows:
        if uid not in vis_map:
            vis_map[uid] = []
        if erp_id:
            vis_map[uid].append(str(erp_id))
    return vis_map


def _validate_email(value: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", value or ""))


def _role_permission_union(role_names: list[str]) -> list[str]:
    keys: set[str] = set()
    for role_name in role_names or []:
        normalized = _normalize_role_name(role_name) or "sales"
        keys.update(canonicalize_permission_keys(DEFAULT_ROLE_PERMISSION_KEYS.get(normalized, set())))
        keys.update(list_role_permissions(normalized))
    return sorted(keys)


def _user_update_audit_action(before: dict[str, Any], after: dict[str, Any], updates: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    fields = sorted(str(key) for key in updates.keys())
    active_before = bool(before.get("is_active"))
    active_after = bool(after.get("is_active"))
    approved_before = bool(before.get("is_approved"))
    approved_after = bool(after.get("is_approved"))

    if {"is_active", "is_approved"}.issuperset(fields):
        if active_after and approved_after and (not active_before or not approved_before):
            return "admin_approve_user", {"fields": fields}
        if not active_after and not approved_after and (active_before or approved_before):
            return "admin_revoke_user", {"fields": fields}
        if active_before and not active_after:
            return "admin_disable_user", {"fields": fields}
        if not active_before and active_after:
            return "admin_enable_user", {"fields": fields}

    return "admin_update_user", {"fields": fields}


def _access_summary(selected_permissions: set[str], user_state: dict[str, Any] | None = None) -> dict[str, Any]:
    schema = _cached_permission_editor_schema()
    modules = schema.get("modules") or []
    accessible_modules: list[str] = []
    accessible_drilldowns: list[str] = []
    exports: list[str] = []
    for module in modules:
        label = str(module.get("label") or module.get("id") or "Module")
        for item in module.get("items") or []:
            keys = canonicalize_permission_keys([item.get("key")]) if item.get("key") else set()
            key = next(iter(keys), "")
            if not key or key not in selected_permissions:
                continue
            item_type = str(item.get("type") or "").strip().lower()
            item_label = str(item.get("label") or key)
            if item_type == "page":
                accessible_modules.append(label)
            elif item_type == "drilldown":
                accessible_drilldowns.append(label)
            elif item_type == "export":
                exports.append(f"{label}: {item_label}")
    sensitive = {
        "cost_visible": "data.cost.view" in selected_permissions,
        "margin_visible": "data.margin.view" in selected_permissions,
        "profit_visible": "data.profit.view" in selected_permissions,
        "pricing_visible": "data.price_recommendation.view" in selected_permissions,
        "margin_risk_visible": "data.margin_risk.view" in selected_permissions,
        "sensitive_exports_unmasked": "export.sensitive.unmasked" in selected_permissions,
    }
    return {
        "accessible_modules": accessible_modules,
        "accessible_drilldowns": accessible_drilldowns,
        "exports": exports,
        "scope_summary": {
            "scope_mode": ((user_state or {}).get("scope") or {}).get("scope_mode") or "none",
            "allowed_count": int((((user_state or {}).get("scope") or {}).get("allowed_count") or 0)),
            "allowed_customer_count": int((((user_state or {}).get("scope") or {}).get("allowed_customer_count") or 0)),
            "allowed_region_count": int((((user_state or {}).get("scope") or {}).get("allowed_region_count") or 0)),
            "allowed_supplier_count": int((((user_state or {}).get("scope") or {}).get("allowed_supplier_count") or 0)),
        },
        "sensitive": sensitive,
    }


def _build_user_permissions_payload(serialized: dict[str, Any]) -> dict[str, Any]:
    role_names = serialized.get("roles") or []
    role_permissions = canonicalize_permission_keys(_role_permission_union(list(role_names)))
    permission_rules = (serialized.get("user_permission_rules") or {}) if isinstance(serialized, dict) else {}
    allow_rules = canonicalize_permission_keys(permission_rules.get("allow") or [])
    deny_rules = canonicalize_permission_keys(permission_rules.get("deny") or [])
    use_role_defaults_only = not allow_rules and not deny_rules
    selected_permissions = normalize_permission_selection(
        role_permissions if use_role_defaults_only else (serialized.get("permissions") or [])
    )
    return {
        "user": serialized,
        "role_permissions": sorted(role_permissions),
        "registry": _cached_permission_registry(),
        "editor": _cached_permission_editor_schema(),
        "access_source": {
            "role_preset": (serialized.get("role") or (role_names[0] if role_names else "") or "").strip().lower(),
            "role_names": role_names,
            "use_role_defaults_only": use_role_defaults_only,
            "allow_override_count": len(allow_rules),
            "deny_override_count": len(deny_rules),
            "allow_rules": sorted(allow_rules),
            "deny_rules": sorted(deny_rules),
        },
        "editor_state": {
            "selected_permissions": sorted(selected_permissions),
            "validation": {
                "warnings": permission_selection_warnings(selected_permissions),
            },
            "summary": _access_summary(selected_permissions, serialized),
        },
    }


def _frame_audit_stats(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {
            "rows": 0,
            "distinct_orderlines": 0,
            "distinct_orders": 0,
            "distinct_products": 0,
            "revenue": 0.0,
            "date_min": None,
            "date_max": None,
        }
    revenue = Decimal(str(float(pd.to_numeric(df.get("Revenue"), errors="coerce").fillna(0.0).sum()))) if "Revenue" in df.columns else Decimal("0")
    dates = pd.to_datetime(df["Date"], errors="coerce").dropna() if "Date" in df.columns else pd.Series(dtype="datetime64[ns]")
    return {
        "rows": int(len(df)),
        "distinct_orderlines": int(df["OrderLineId"].dropna().nunique()) if "OrderLineId" in df.columns else int(len(df)),
        "distinct_orders": int(df["OrderId"].dropna().nunique()) if "OrderId" in df.columns else int(len(df)),
        "distinct_products": int(df["ProductId"].dropna().nunique()) if "ProductId" in df.columns else 0,
        "revenue": float(revenue),
        "date_min": dates.min().isoformat() if not dates.empty else None,
        "date_max": dates.max().isoformat() if not dates.empty else None,
    }

def _frame_pack_match(df: pd.DataFrame) -> dict:
    if df is None or df.empty:
        return {"orderlines": 0, "with_pack": 0, "pack_match_rate": 0.0}
    if "OrderLineId" not in df.columns:
        return {"orderlines": int(len(df)), "with_pack": 0, "pack_match_rate": 0.0}
    ol = df["OrderLineId"].dropna()
    pack_col = None
    for cand in ("PickedForOrderLine", "OrderLineId"):
        if cand in df.columns:
            pack_col = cand
            break
    with_pack = 0
    if pack_col:
        with_pack = int(pd.Series(df[pack_col]).dropna().nunique())
    total = int(ol.nunique()) if not ol.empty else 0
    rate = float(with_pack / total) if total else 0.0
    return {"orderlines": total, "with_pack": with_pack, "pack_match_rate": rate}



def _ensure_unique_username(session, desired: str) -> str:
    base = desired.strip().lower()
    if not base:
        base = "user"
    candidate = base
    i = 2
    while session.query(User).filter(User.username == candidate).first():
        candidate = f"{base}{i}"
        i += 1
    return candidate


def _admin_user_select_enabled() -> bool:
    try:
        return bool(current_app.config.get("ADMIN_USER_SELECT", True))
    except Exception:
        return True


def _parse_limit_offset(default_limit: int = 20, max_limit: int = 50) -> tuple[int, int]:
    try:
        limit = int(request.args.get("limit", default_limit) or default_limit)
    except Exception:
        limit = default_limit
    try:
        offset = int(request.args.get("offset", 0) or 0)
    except Exception:
        offset = 0
    limit = max(1, min(limit, max_limit))
    offset = max(0, offset)
    return limit, offset


def _to_lower_tokens(values: List[str]) -> List[str]:
    return [str(v).strip().lower() for v in values if str(v).strip()]


def _normalize_rep_scope_values(values: List[str]) -> list[str]:
    cleaned = []
    for value in values or []:
        token = str(value).strip()
        if token:
            cleaned.append(token)
    lowered = {token.lower() for token in cleaned}
    if lowered & ALL_SCOPE_TOKENS:
        return [ALL_SCOPE_TOKEN]
    # Preserve input order while deduplicating.
    seen: set[str] = set()
    out: list[str] = []
    for token in cleaned:
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(token)
    return out


def _scope_input_payload(payload: dict) -> dict:
    if isinstance(payload.get("scope"), dict):
        return payload.get("scope") or {}
    return payload or {}


def _scope_lists_from_payload(payload: dict) -> dict[str, list[str]]:
    scope_input = _scope_input_payload(payload)
    reps, _ = _validate_visibility(scope_input)
    reps = _normalize_rep_scope_values(reps)
    return {
        "rep": reps,
        "customer": _clean_tokens(
            scope_input.get("customer_ids")
            or scope_input.get("customers")
            or payload.get("customer_ids")
            or payload.get("customers")
        ),
        "region": _clean_tokens(
            scope_input.get("region_ids")
            or scope_input.get("regions")
            or payload.get("region_ids")
            or payload.get("regions")
        ),
        "supplier": _clean_tokens(
            scope_input.get("supplier_ids")
            or scope_input.get("suppliers")
            or payload.get("supplier_ids")
            or payload.get("suppliers")
        ),
    }


def _dimension_candidates(scope_type: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    token = str(scope_type or "").strip().lower()
    if token == "rep":
        return (
            ("SalesRepId", "PrimarySalesRepId", "SalesRepUserId", "PrimarySalesRepUserId", "RepId", "UserId"),
            ("SalesRepName", "PrimarySalesRepName", "SalesRepId", "PrimarySalesRepId"),
        )
    if token == "customer":
        return (("CustomerId", "CustomerName", "Customer"), ("CustomerName", "Customer", "CustomerId"))
    if token == "region":
        return (("RegionId", "RegionName", "Region"), ("RegionName", "Region", "RegionId"))
    if token == "supplier":
        return (("SupplierId", "SupplierName", "Supplier"), ("SupplierName", "Supplier", "SupplierId"))
    return (tuple(), tuple())


def _search_dimension(scope_type: str, query: str, limit: int, offset: int) -> list[dict[str, str]]:
    try:
        from app.services import fact_store  # type: ignore
        from app.core import access_policy  # type: ignore
    except Exception:
        return []

    try:
        cols = fact_store.list_columns()
        scope_obj = access_policy.get_current_scope(use_cache=True)
        scope_where, scope_params = fact_store.build_scope_clause(scope_obj.as_dict(include_allowed=True), cols)
    except Exception:
        current_app.logger.debug("admin.dimension_search_unavailable", exc_info=True)
        return []
    if not cols:
        return []
    id_candidates, label_candidates = _dimension_candidates(scope_type)
    id_col = fact_store.choose_column(id_candidates, cols)
    label_col = fact_store.choose_column(label_candidates, cols) or id_col
    if not id_col:
        return []
    id_q = fact_store.quote_identifier(id_col)
    label_q = fact_store.quote_identifier(label_col) if label_col else id_q

    where_parts: list[str] = [scope_where or "1=1", f"{id_q} IS NOT NULL"]
    params: list[Any] = list(scope_params or [])
    q = str(query or "").strip().lower()
    if q:
        like = f"%{q}%"
        where_parts.append(
            f"(LOWER(CAST({id_q} AS VARCHAR)) LIKE ? OR LOWER(CAST(COALESCE({label_q}, {id_q}) AS VARCHAR)) LIKE ?)"
        )
        params.extend([like, like])
    sql = f"""
        SELECT
            CAST({id_q} AS VARCHAR) AS id,
            CAST(COALESCE({label_q}, {id_q}) AS VARCHAR) AS label
        FROM fact
        WHERE {' AND '.join(where_parts)}
        GROUP BY 1, 2
        ORDER BY label ASC, id ASC
        LIMIT ? OFFSET ?
    """
    params.extend([int(limit), int(offset)])
    try:
        conn = fact_store.get_conn()
        rows = conn.execute(sql, params).fetchall()
    except Exception:
        current_app.logger.debug("admin.dimension_search_failed", exc_info=True)
        return []
    out: list[dict[str, str]] = []
    for row in rows:
        rid = str(row[0] or "").strip()
        if not rid:
            continue
        lbl = str(row[1] or rid).strip() or rid
        out.append({"id": rid, "label": lbl})
    return out


def _customer_suggestions_for_reps(rep_ids: list[str], limit: int) -> list[dict[str, str]]:
    try:
        from app.services import fact_store  # type: ignore
        from app.core import access_policy  # type: ignore
    except Exception:
        return []

    try:
        cols = fact_store.list_columns()
        scope_obj = access_policy.get_current_scope(use_cache=True)
        scope_where, scope_params = fact_store.build_scope_clause(scope_obj.as_dict(include_allowed=True), cols)
    except Exception:
        current_app.logger.debug("admin.customer_suggest_unavailable", exc_info=True)
        return []
    if not cols:
        return []
    rep_col = fact_store.choose_column(
        ("PrimarySalesRepUserId", "SalesRepUserId", "SalesRepId", "PrimarySalesRepId", "RepId", "UserId"),
        cols,
    )
    customer_id_col = fact_store.choose_column(("CustomerId", "CustomerName", "Customer"), cols)
    customer_label_col = fact_store.choose_column(("CustomerName", "Customer", "CustomerId"), cols) or customer_id_col
    if not rep_col or not customer_id_col:
        return []

    normalized_reps = [v for v in _to_lower_tokens(rep_ids) if v and v not in ALL_SCOPE_TOKENS]
    if not normalized_reps:
        return []

    rep_q = fact_store.quote_identifier(rep_col)
    customer_id_q = fact_store.quote_identifier(customer_id_col)
    customer_label_q = fact_store.quote_identifier(customer_label_col) if customer_label_col else customer_id_q
    revenue_col = fact_store.choose_column(("Revenue", "NetRevenue"), cols)
    score_expr = (
        f"SUM(COALESCE({fact_store.quote_identifier(revenue_col)}, 0))"
        if revenue_col
        else "COUNT(*)"
    )

    rep_placeholders = ", ".join("?" for _ in normalized_reps)
    where_parts = [
        scope_where or "1=1",
        f"{customer_id_q} IS NOT NULL",
        f"LOWER(CAST({rep_q} AS VARCHAR)) IN ({rep_placeholders})",
    ]
    params: list[Any] = list(scope_params or []) + normalized_reps + [int(limit)]
    sql = f"""
        SELECT
            CAST({customer_id_q} AS VARCHAR) AS id,
            CAST(COALESCE({customer_label_q}, {customer_id_q}) AS VARCHAR) AS label,
            {score_expr} AS score
        FROM fact
        WHERE {' AND '.join(where_parts)}
        GROUP BY 1, 2
        ORDER BY score DESC, label ASC
        LIMIT ?
    """
    try:
        conn = fact_store.get_conn()
        rows = conn.execute(sql, params).fetchall()
    except Exception:
        current_app.logger.debug("admin.customer_suggest_query_failed", exc_info=True)
        return []

    out: list[dict[str, str]] = []
    for row in rows:
        customer_id = str(row[0] or "").strip()
        if not customer_id:
            continue
        label = str(row[1] or customer_id).strip() or customer_id
        out.append({"id": customer_id, "label": label})
    return out


def _validate_scope_values(scope_map: dict[str, list[str]]) -> Optional[str]:
    try:
        from app.services import fact_store  # type: ignore
        from app.core import access_policy  # type: ignore
    except Exception:
        return None

    try:
        cols = fact_store.list_columns()
        scope_obj = access_policy.get_current_scope(use_cache=True)
        scope_where, scope_params = fact_store.build_scope_clause(scope_obj.as_dict(include_allowed=True), cols)
    except Exception:
        current_app.logger.debug("admin.scope_validation_unavailable", exc_info=True)
        return None
    if not cols:
        return None
    for scope_type, values in (scope_map or {}).items():
        requested = sorted({str(v).strip() for v in values if str(v).strip()})
        if not requested:
            continue
        if scope_type == "rep" and {v.lower() for v in requested} & ALL_SCOPE_TOKENS:
            continue
        id_candidates, _ = _dimension_candidates(scope_type)
        id_col = fact_store.choose_column(id_candidates, cols)
        if not id_col:
            continue
        id_q = fact_store.quote_identifier(id_col)
        lowered = _to_lower_tokens(requested)
        placeholders = ", ".join("?" for _ in lowered)
        params = list(scope_params or []) + lowered
        sql = f"""
            SELECT DISTINCT LOWER(CAST({id_q} AS VARCHAR)) AS v
            FROM fact
            WHERE ({scope_where}) AND LOWER(CAST({id_q} AS VARCHAR)) IN ({placeholders})
        """
        try:
            conn = fact_store.get_conn()
            rows = conn.execute(sql, params).fetchall()
        except Exception:
            current_app.logger.debug("admin.scope_validation_query_failed", exc_info=True)
            continue
        found = {str(r[0]).strip().lower() for r in rows if r and r[0] is not None}
        missing = [val for val in requested if val.strip().lower() not in found]
        if missing:
            label = scope_type.title()
            return f"Invalid {label} IDs: {', '.join(missing[:10])}"
    return None


def _apply_user_scope(user_id: int, scope_lists: dict[str, list[str]]) -> dict[str, list[str]]:
    rep_scope = _normalize_rep_scope_values(scope_lists.get("rep") or [])
    scoped = replace_user_scope_rules(
        int(user_id),
        {
            "rep": rep_scope,
            "customer": scope_lists.get("customer") or [],
            "region": scope_lists.get("region") or [],
            "supplier": scope_lists.get("supplier") or [],
        },
    )
    replace_visibility_for_user(int(user_id), scoped.get("rep") or [])
    return scoped


@bp.get("/users/search")
@login_required
@require_admin
@permission_required("admin.users.manage")
def users_search():
    if not _admin_user_select_enabled():
        return jsonify({"items": [], "pagination": {"limit": 0, "offset": 0, "has_more": False}})
    q = (request.args.get("q") or "").strip().lower()
    limit, offset = _parse_limit_offset(default_limit=20, max_limit=50)
    with SessionLocal() as s:
        query = s.query(User)
        if q:
            like = f"%{q}%"
            query = query.filter(
                or_(
                    func.lower(User.username).like(like),
                    func.lower(User.email).like(like),
                    func.lower(User.first_name).like(like),
                    func.lower(User.last_name).like(like),
                )
            )
        rows = (
            query.order_by(User.updated_at.desc(), User.id.desc())
            .offset(offset)
            .limit(limit + 1)
            .all()
        )
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = []
    for user in rows:
        name = user.full_name or user.username or ""
        email = user.email or ""
        if name and email:
            label = f"{name} ({email})"
        else:
            label = email or name or f"User {user.id}"
        items.append(
            {
                "id": int(user.id),
                "label": label,
                "email": email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "full_name": user.full_name,
                "role": user.role,
                "erp_user_id": user.erp_user_id,
                "is_active": bool(user.is_active),
                "is_approved": bool(user.is_approved),
            }
        )
    return jsonify({"items": items, "pagination": {"limit": limit, "offset": offset, "has_more": has_more}})


@bp.get("/reps/search")
@login_required
@require_admin
@permission_required("admin.users.manage")
def reps_search():
    if not _admin_user_select_enabled():
        return jsonify({"items": [], "pagination": {"limit": 0, "offset": 0, "has_more": False}})
    q = (request.args.get("q") or "").strip()
    q_lower = q.lower()
    limit, offset = _parse_limit_offset(default_limit=20, max_limit=50)
    items = _search_dimension("rep", q, limit + 1, offset)
    include_all = (offset == 0) and (not q_lower or "all".startswith(q_lower) or q_lower in {"*", "all"})
    if include_all:
        items = [{"id": ALL_SCOPE_TOKEN, "label": "All ERP Users (All Access)"}] + items
    has_more = len(items) > limit
    return jsonify({"items": items[:limit], "pagination": {"limit": limit, "offset": offset, "has_more": has_more}})


@bp.get("/customers/search")
@login_required
@require_admin
@permission_required("admin.users.manage")
def customers_search():
    if not _admin_user_select_enabled():
        return jsonify({"items": [], "pagination": {"limit": 0, "offset": 0, "has_more": False}})
    q = (request.args.get("q") or "").strip()
    limit, offset = _parse_limit_offset(default_limit=20, max_limit=50)
    items = _search_dimension("customer", q, limit + 1, offset)
    has_more = len(items) > limit
    return jsonify({"items": items[:limit], "pagination": {"limit": limit, "offset": offset, "has_more": has_more}})


@bp.get("/customers/suggest")
@login_required
@require_admin
@permission_required("admin.users.manage")
def customers_suggest():
    if not _admin_user_select_enabled():
        return jsonify({"items": [], "meta": {"rep_ids": [], "auto_disabled": "feature_off"}})
    raw_rep_values = request.args.getlist("rep_ids")
    if not raw_rep_values:
        raw_rep_values = [request.args.get("rep_ids")]
    rep_tokens: list[str] = []
    for raw in raw_rep_values:
        rep_tokens.extend(_clean_tokens(raw))
    rep_ids = _normalize_rep_scope_values(rep_tokens)
    limit, _ = _parse_limit_offset(default_limit=50, max_limit=200)
    if not rep_ids:
        return jsonify({"items": [], "meta": {"rep_ids": []}})
    if any(str(v).strip().lower() in ALL_SCOPE_TOKENS for v in rep_ids):
        return jsonify({"items": [], "meta": {"rep_ids": [ALL_SCOPE_TOKEN], "auto_disabled": "all_scope"}})
    items = _customer_suggestions_for_reps(rep_ids, limit)
    return jsonify({"items": items, "meta": {"rep_ids": rep_ids, "count": len(items)}})


@bp.get("/users")
@login_required
@require_admin
@permission_required("admin.users.manage")
def list_users():
    query = (request.args.get("query") or "").strip()
    role_filter = (request.args.get("role") or "").strip().lower()
    status_filter = (request.args.get("status") or "").strip().lower()
    approved_filter = (request.args.get("approved") or "").strip().lower()
    active_filter = (request.args.get("active") or "").strip().lower()
    status_defaulted = False
    if not status_filter and not approved_filter and not active_filter:
        status_filter = "active"
        status_defaulted = True
    try:
        page = max(1, int(request.args.get("page", 1) or 1))
        page_size = min(max(1, int(request.args.get("page_size", 25) or 25)), 100)
    except Exception:
        return _error("page and page_size must be integers")

    started = time.perf_counter()
    with SessionLocal() as s:
        base_query = s.query(User)
        if query:
            like = f"%{query.lower()}%"
            base_query = base_query.filter(
                or_(
                    func.lower(User.username).like(like),
                    func.lower(User.email).like(like),
                    func.lower(User.first_name).like(like),
                    func.lower(User.last_name).like(like),
                )
            )
        if role_filter:
            base_query = base_query.filter(func.lower(User.role) == role_filter)
        stats_row = base_query.with_entities(
            func.count(User.id).label("total"),
            func.coalesce(func.sum(case((User.is_approved == False, 1), else_=0)), 0).label("pending"),  # noqa: E712
            func.coalesce(func.sum(case((((User.is_active == True) & (User.is_approved == True)), 1), else_=0)), 0).label("active"),  # noqa: E712
            func.coalesce(func.sum(case((User.is_active == False, 1), else_=0)), 0).label("disabled"),  # noqa: E712
        ).one()
        stats = {
            "total": int(getattr(stats_row, "total", 0) or 0),
            "pending": int(getattr(stats_row, "pending", 0) or 0),
            "active": int(getattr(stats_row, "active", 0) or 0),
            "disabled": int(getattr(stats_row, "disabled", 0) or 0),
        }
        q = base_query
        if status_filter == "pending":
            q = q.filter(User.is_approved == False)  # noqa: E712
        elif status_filter == "disabled":
            q = q.filter(User.is_active == False)  # noqa: E712
        elif status_filter == "active":
            q = q.filter(User.is_active == True, User.is_approved == True)  # noqa: E712
        if approved_filter in {"true", "1", "yes"}:
            q = q.filter(User.is_approved == True)  # noqa: E712
        elif approved_filter in {"false", "0", "no"}:
            q = q.filter(User.is_approved == False)  # noqa: E712
        if active_filter in {"true", "1", "yes"}:
            q = q.filter(User.is_active == True)  # noqa: E712
        elif active_filter in {"false", "0", "no"}:
            q = q.filter(User.is_active == False)  # noqa: E712

        total = int(q.with_entities(func.count(User.id)).scalar() or 0)
        if _admin_debug_enabled():
            current_app.logger.info(
                "admin.users.list.debug",
                extra={
                    "total_users": stats["total"],
                    "active_users": stats["active"],
                    "filtered_total": total,
                    "filters": {
                        "query": query,
                        "role": role_filter,
                        "status": status_filter,
                        "approved": approved_filter,
                        "active": active_filter,
                        "status_defaulted": status_defaulted,
                        "page": page,
                        "page_size": page_size,
                    },
                    "where_clause": _debug_where_clause(q),
                },
            )
        rows_query = q.with_entities(
            User.id.label("id"),
            User.username.label("username"),
            User.email.label("email"),
            User.first_name.label("first_name"),
            User.last_name.label("last_name"),
            User.role.label("role"),
            User.is_active.label("is_active"),
            User.is_approved.label("is_approved"),
            User.sales_rep_id.label("sales_rep_id"),
            User.erp_user_id.label("erp_user_id"),
            User.region_id.label("region_id"),
            User.sales_visibility.label("sales_visibility"),
            User.created_at.label("created_at"),
            User.last_login_at.label("last_login_at"),
            User.updated_at.label("updated_at"),
        )
        try:
            users = (
                rows_query.order_by(User.updated_at.desc(), User.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
        except Exception:
            users = (
                rows_query.order_by(User.created_at.desc(), User.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
        user_ids = [int(_row_mapping(row).get("id") or 0) for row in users if int(_row_mapping(row).get("id") or 0) > 0]
        visibility = _get_visibility_map(s, user_ids)
        role_names = _batch_role_names(s, user_ids)
        scope_rules = _batch_scope_rules(s, user_ids)
        data = [
            _serialize_user_listing_row(
                row,
                visibility=visibility.get(int(_row_mapping(row).get("id") or 0), []),
                role_names=role_names.get(int(_row_mapping(row).get("id") or 0), []),
                scope_rules=scope_rules.get(int(_row_mapping(row).get("id") or 0), {}),
            )
            for row in users
        ]

    duration_ms = int((time.perf_counter() - started) * 1000)
    log_method = current_app.logger.info if duration_ms >= 400 else current_app.logger.debug
    try:
        log_method(
            "admin.users.list",
            extra={
                "query": query,
                "role_filter": role_filter,
                "status_filter": status_filter,
                "approved_filter": approved_filter,
                "active_filter": active_filter,
                "page": page,
                "page_size": page_size,
                "returned_rows": len(data),
                "filtered_total": total,
                "total_users": stats["total"],
                "duration_ms": duration_ms,
            },
        )
    except Exception:
        pass

    return jsonify(
        {
            "users": data,
            "pagination": {"page": page, "page_size": page_size, "total": total},
            "stats": stats,
            "meta": {"duration_ms": duration_ms, "list_mode": "batched"},
        }
    )


def _normalize_role_name(raw: Any) -> str:
    token = str(raw or "").strip().lower()
    token = re.sub(r"[^a-z0-9_.:-]+", "_", token)
    return token.strip("_")


@bp.get("/permissions")
@login_required
@require_admin
def list_permissions_api():
    rows = list_permissions()
    data = [{"key": p.key, "description": p.description} for p in rows]
    return jsonify({"permissions": data, "registry": _cached_permission_registry()})


@bp.get("/roles")
@login_required
@require_admin
def list_roles_api():
    rows = list_roles()
    data = []
    for role in rows:
        perms = _role_permission_union([role.name])
        data.append(
            {
                "id": role.id,
                "name": role.name,
                "description": role.description,
                "is_system": bool(getattr(role, "is_system", False)),
                "permissions": perms,
                "permission_count": len(perms),
            }
        )
    return jsonify({"roles": data})


@bp.post("/roles")
@login_required
@require_admin
@permission_required("admin.roles.manage")
def create_role_api():
    payload = request.get_json(force=True, silent=True) or {}
    name = _normalize_role_name(payload.get("name"))
    description = (payload.get("description") or "").strip() or None
    permission_keys = payload.get("permissions") or []
    if not name:
        return _error("Role name is required")
    with SessionLocal() as s:
        existing = s.query(Role).filter(func.lower(Role.name) == name).first()
        if existing:
            return _error("Role already exists", 409)
        role = Role(
            name=name,
            description=description,
            is_system=False,
            created_at=_now(),
            updated_at=_now(),
        )
        s.add(role)
        s.commit()
        s.refresh(role)
    replace_role_permissions(name, permission_keys)
    bump_permissions_version()
    log_audit(current_user, "admin_create_role", {"role": name})
    return jsonify({"role": {"id": role.id, "name": role.name, "description": role.description, "permissions": list_role_permissions(name)}}), 201


@bp.patch("/roles/<int:role_id>")
@login_required
@require_admin
@permission_required("admin.roles.manage")
def update_role_api(role_id: int):
    payload = request.get_json(force=True, silent=True) or {}
    description = payload.get("description")
    permission_keys = payload.get("permissions")
    with SessionLocal() as s:
        role = s.get(Role, role_id)
        if not role:
            return _error("Role not found", 404)
        before = {
            "id": role.id,
            "name": role.name,
            "description": role.description,
            "permissions": list_role_permissions(role.name),
        }
        if description is not None:
            role.description = (str(description).strip() or None)
            role.updated_at = _now()
            s.add(role)
            s.commit()
        after_perms = before["permissions"]
        if permission_keys is not None:
            after_perms = replace_role_permissions(role.name, permission_keys if isinstance(permission_keys, list) else [])
        after = {
            "id": role.id,
            "name": role.name,
            "description": role.description,
            "permissions": after_perms,
        }
    log_audit_change(current_user, "admin_update_role_permissions", target_user_id=None, before=before, after=after)
    bump_permissions_version()
    return jsonify({"role": after})


@bp.patch("/users/<int:user_id>/roles")
@login_required
@require_admin
@permission_required("admin.users.manage")
def update_user_roles_api(user_id: int):
    payload = request.get_json(force=True, silent=True) or {}
    roles = payload.get("roles")
    if not isinstance(roles, list) or not roles:
        return _error("roles must be a non-empty list")
    cleaned = [_normalize_role_name(r) for r in roles if _normalize_role_name(r)]
    if not cleaned:
        return _error("No valid roles provided")
    with SessionLocal() as s:
        u = s.get(User, user_id)
        if not u:
            return _error("User not found", 404)
        before = _serialize_user(u, list_visibility_for_user(user_id))
        replace_user_roles(user_id, cleaned)
        u = s.get(User, user_id)
        after = _serialize_user(u, list_visibility_for_user(user_id))
    log_audit_change(current_user, "admin_update_user_roles", target_user_id=user_id, before=before, after=after)
    bump_permissions_version()
    return jsonify({"user": after})


@bp.patch("/users/<int:user_id>/permissions")
@login_required
@require_admin
@permission_required("admin.users.manage")
def update_user_permissions_api(user_id: int):
    payload = request.get_json(force=True, silent=True) or {}
    reset_to_role_defaults = bool(payload.get("reset_to_role_defaults") or payload.get("use_role_defaults_only"))
    selected_permissions = payload.get("selected_permissions")
    permissions = payload.get("permissions")
    if permissions is None:
        permissions = payload.get("permission_keys")
    allow_keys = permissions if isinstance(permissions, list) else payload.get("allow")
    deny_keys = payload.get("deny") or []
    with SessionLocal() as s:
        u = s.get(User, user_id)
        if not u:
            return _error("User not found", 404)
        before = _serialize_user(u, list_visibility_for_user(user_id))
        role_names = before.get("roles") or [str(u.role or "sales").strip().lower()]
        role_permission_set = set(_role_permission_union(list(role_names)))
        if reset_to_role_defaults:
            allow_keys = []
            deny_keys = []
        elif isinstance(selected_permissions, list):
            selected_keys = normalize_permission_selection(selected_permissions)
            allow_keys = sorted(selected_keys - role_permission_set)
            deny_keys = sorted(role_permission_set - selected_keys)
        else:
            if allow_keys is None:
                allow_keys = []
            if not isinstance(allow_keys, list) or not isinstance(deny_keys, list):
                return _error("allow and deny must be arrays")
        replace_user_permission_rules(user_id, allow_keys=allow_keys, deny_keys=deny_keys)
        u = s.get(User, user_id)
        after = _serialize_user(u, list_visibility_for_user(user_id))
    log_audit_change(current_user, "admin_update_user_permissions", target_user_id=user_id, before=before, after=after)
    bump_permissions_version()
    return jsonify(_build_user_permissions_payload(after))


@bp.get("/users/<int:user_id>/permissions")
@login_required
@require_admin
@permission_required("admin.users.manage")
def user_permissions_api(user_id: int):
    started = time.perf_counter()
    cached_payload = None
    with SessionLocal() as s:
        user = s.get(User, user_id)
        if not user:
            return _error("User not found", 404)
        updated_marker = int(user.updated_at.timestamp()) if getattr(user, "updated_at", None) else 0
        login_marker = int(user.last_login_at.timestamp()) if getattr(user, "last_login_at", None) else 0
    cache_key = f"admin.user.permissions:{int(user_id)}:{permissions_version()}:{updated_marker}:{login_marker}"
    cached_payload = cache.get(cache_key)
    cache_hit = cached_payload is not None
    if cached_payload is None:
        visibility = list_visibility_for_user(user_id)
        serialized = _serialize_user(user, visibility)
        cached_payload = _build_user_permissions_payload(serialized)
        cache.set(cache_key, cached_payload, timeout=_ADMIN_USER_PERMISSIONS_CACHE_TTL)
    duration_ms = int((time.perf_counter() - started) * 1000)
    log_method = current_app.logger.info if duration_ms >= 250 else current_app.logger.debug
    try:
        log_method(
            "admin.user.permissions",
            extra={
                "user_id": int(user_id),
                "duration_ms": duration_ms,
                "cache_hit": cache_hit,
            },
        )
    except Exception:
        pass
    payload = dict(cached_payload)
    payload["meta"] = {"duration_ms": duration_ms}
    return jsonify(payload)


@bp.post("/users")
@login_required
@require_admin
@permission_required("admin.users.manage")
def create_user():
    payload = request.get_json(force=True, silent=True) or {}
    existing_user_id_raw = payload.get("existing_user_id")
    existing_user_id: Optional[int]
    try:
        existing_user_id = int(existing_user_id_raw) if existing_user_id_raw not in (None, "") else None
    except Exception:
        return _error("existing_user_id must be an integer")

    email = (payload.get("email") or "").strip().lower()
    first_name = (payload.get("first_name") or "").strip()
    last_name = (payload.get("last_name") or "").strip()
    role = (payload.get("role") or "sales").strip().lower()
    roles_payload = payload.get("roles")
    erp_user_id = (payload.get("erp_user_id") or payload.get("user_id") or "").strip()
    visibility_raw = payload.get("visibility") or payload.get("visible_erp_user_ids") or []
    auto_approve_raw = payload.get("approve", False)
    auto_approve, bool_err = _as_bool(auto_approve_raw)
    if bool_err:
        return _error("approve must be boolean")
    auto_approve = bool(auto_approve)

    if existing_user_id is None:
        if not email or not _validate_email(email):
            return _error("Valid email is required.")
    if role not in RBAC_ROLES and not roles_payload:
        return _error("Invalid role.")

    visibility_ids, vis_err = _validate_visibility({"visibility": visibility_raw})
    if vis_err:
        return _error(vis_err)
    scope_lists = _scope_lists_from_payload(payload)
    # Merge direct visibility list into canonical rep list.
    if visibility_ids and not scope_lists.get("rep"):
        scope_lists["rep"] = _normalize_rep_scope_values(visibility_ids)
    scope_err = _validate_scope_values(scope_lists)
    if scope_err:
        return _error(scope_err)

    role_names: list[str]
    if isinstance(roles_payload, list):
        role_names = [str(r).strip().lower() for r in roles_payload if str(r).strip()]
    elif roles_payload:
        role_names = [str(roles_payload).strip().lower()]
    else:
        role_names = [role]

    if existing_user_id is not None:
        target_user_id = int(existing_user_id)
        with SessionLocal() as s:
            u = s.get(User, target_user_id)
            if not u:
                return _error("Existing user not found", 404)
            before = _serialize_user(u, list_visibility_for_user(target_user_id))
            # Optional field sync when provided.
            if email:
                if not _validate_email(email):
                    return _error("Invalid email.")
                conflict = s.query(User).filter(func.lower(User.email) == email.lower(), User.id != target_user_id).first()
                if conflict:
                    return _error("Email already in use.")
                u.email = email
            if first_name:
                u.first_name = first_name
            if last_name:
                u.last_name = last_name
            if erp_user_id:
                conflict = s.query(User).filter(func.lower(User.erp_user_id) == erp_user_id.lower(), User.id != target_user_id).first()
                if conflict:
                    return _error("ERP UserId is already linked to another account.")
                u.erp_user_id = erp_user_id
                u.sales_rep_id = erp_user_id
            if auto_approve:
                u.is_active = True
                u.is_approved = True
            u.updated_at = _now()
            s.add(u)
            s.commit()

        replace_user_roles(target_user_id, role_names)
        scoped = _apply_user_scope(target_user_id, scope_lists)
        with SessionLocal() as s:
            u = s.get(User, target_user_id)
            result = _serialize_user(u, scoped.get("rep") or [])
        log_audit_change(
            current_user,
            "admin_assign_existing_user",
            target_user_id=target_user_id,
            before=before,
            after=result,
        )
        bump_permissions_version()
        return jsonify({"user": result, "mode": "existing"}), 200

    if not erp_user_id:
        return _error("ERP UserId is required.")

    created_user_id: int
    with SessionLocal() as s:
        if s.query(User).filter(func.lower(User.email) == email.lower()).first():
            return _error("User with that email already exists.")
        if s.query(User).filter(func.lower(User.erp_user_id) == erp_user_id.lower()).first():
            return _error("ERP UserId is already linked to another account.")
        username = _ensure_unique_username(s, payload.get("username") or email.split("@")[0])
        u = User(
            username=username,
            email=email,
            first_name=first_name or None,
            last_name=last_name or None,
            role=role,
            is_active=auto_approve,
            is_approved=auto_approve,
            must_reset_password=True,
            sales_rep_id=erp_user_id or None,
            erp_user_id=erp_user_id or None,
            region_id=None,
            sales_visibility="self",
            updated_at=_now(),
        )
        # Secure random initial password ensures invite link is the only practical setup path.
        u.set_password(secrets.token_urlsafe(16))
        s.add(u)
        s.commit()
        s.refresh(u)
        created_user_id = int(u.id)

    replace_user_roles(created_user_id, role_names)
    if not scope_lists.get("rep"):
        scope_lists["rep"] = [erp_user_id]
    scoped = _apply_user_scope(created_user_id, scope_lists)

    invite_link = None
    invite_error: str | None = None
    created_email: str | None = None
    created_name: str | None = None
    with SessionLocal() as s:
        u = s.get(User, created_user_id)
        result = _serialize_user(u, scoped.get("rep") or [])
        created_email = (u.email or "").strip().lower() or None
        created_name = u.full_name

        if not _invites_enabled():
            invite_error = "Invites are disabled by INVITES_ENABLED=0."
        elif not created_email:
            invite_error = "User created, but invite email was not sent because the account has no email."
        else:
            try:
                invite_link = _issue_password_link(
                    s,
                    user=u,
                    purpose="invite",
                    created_by_user_id=int(getattr(current_user, "id", 0) or 0) or None,
                    invalidate_existing=True,
                )
                s.commit()
            except Exception:
                s.rollback()
                current_app.logger.exception("admin.create_user_invite_token_failed", extra={"user_id": created_user_id})
                invite_error = "User created, but invite token generation failed."

    invite_sent = False
    if invite_link and created_email and _invites_enabled():
        invite_sent = _send_password_email_for_user(
            email=created_email,
            full_name=created_name,
            purpose="invite",
            link=invite_link,
        )
        if not invite_sent and not invite_error:
            invite_error = "User created, but invite email delivery failed."

    audit_meta = {"invite_sent": bool(invite_sent), "invite_link": "[hidden]"}
    if invite_error:
        audit_meta["invite_error"] = invite_error
    log_audit_change(current_user, "admin_create_user", target_user_id=created_user_id, before=None, after=result, meta=audit_meta)
    bump_permissions_version()
    return jsonify({"user": result, "invite_sent": bool(invite_sent), "invite_error": invite_error}), 201


@bp.patch("/users/<int:user_id>")
@login_required
@permission_required("admin.users.manage")
def update_user(user_id: int):
    payload = request.get_json(force=True, silent=True) or {}
    allowed_fields = {"first_name", "last_name", "email", "is_active", "is_approved", "sales_visibility", "erp_user_id"}
    updates = {k: v for k, v in payload.items() if k in allowed_fields}
    if not updates:
        return _error("No updatable fields provided.")

    with SessionLocal() as s:
        u = s.get(User, user_id)
        if not u:
            return _error("User not found", 404)
        before = _serialize_user(u, list_visibility_for_user(user_id))
        if "email" in updates:
            email = (updates["email"] or "").strip().lower()
            if not _validate_email(email):
                return _error("Invalid email.")
            conflict = s.query(User).filter(func.lower(User.email) == email.lower(), User.id != user_id).first()
            if conflict:
                return _error("Email already in use.")
            u.email = email
        if "erp_user_id" in updates:
            erp_user_id = (updates["erp_user_id"] or "").strip()
            if not erp_user_id:
                return _error("ERP UserId cannot be empty.")
            conflict = s.query(User).filter(func.lower(User.erp_user_id) == erp_user_id.lower(), User.id != user_id).first()
            if conflict:
                return _error("ERP UserId already linked to another account.")
            u.erp_user_id = erp_user_id
            u.sales_rep_id = erp_user_id
        if "first_name" in updates:
            u.first_name = (updates["first_name"] or "").strip() or None
        if "last_name" in updates:
            u.last_name = (updates["last_name"] or "").strip() or None
        if "is_active" in updates:
            active_val, err = _as_bool(updates["is_active"])
            if err is not None:
                return _error("is_active must be boolean")
            u.is_active = bool(active_val)
        if "is_approved" in updates:
            approved_val, err = _as_bool(updates["is_approved"])
            if err is not None:
                return _error("is_approved must be boolean")
            u.is_approved = bool(approved_val)
        if "sales_visibility" in updates:
            u.sales_visibility = str(updates["sales_visibility"]).strip() or u.sales_visibility
        u.updated_at = _now()
        s.add(u)
        s.commit()
        u = s.get(User, user_id)
        after = _serialize_user(u, list_visibility_for_user(user_id))
        audit_action, audit_meta = _user_update_audit_action(before, after, updates)
        log_audit_change(current_user, audit_action, target_user_id=user_id, before=before, after=after, meta=audit_meta)
        bump_permissions_version()
        return jsonify({"user": after})


@bp.patch("/users/<int:user_id>/activation")
@login_required
@permission_required("admin.users.manage")
def update_user_status(user_id: int):
    payload = request.get_json(force=True, silent=True) or {}
    action = str(payload.get("action") or "").strip().lower()
    transitions = {
        "approve": {
            "updates": {"is_active": True, "is_approved": True},
            "audit_action": "admin_approve_user",
        },
        "revoke": {
            "updates": {"is_active": False, "is_approved": False},
            "audit_action": "admin_revoke_user",
        },
        "disable": {
            "updates": {"is_active": False},
            "audit_action": "admin_disable_user",
        },
        "enable": {
            "updates": {"is_active": True},
            "audit_action": "admin_enable_user",
        },
    }
    transition = transitions.get(action)
    if not transition:
        return _error("Invalid status action")

    updates = transition["updates"]
    with SessionLocal() as s:
        u = s.get(User, user_id)
        if not u:
            return _error("User not found", 404)
        before = _serialize_user(u, list_visibility_for_user(user_id))
        for field, value in updates.items():
            setattr(u, field, value)
        u.updated_at = _now()
        s.add(u)
        s.commit()
        u = s.get(User, user_id)
        after = _serialize_user(u, list_visibility_for_user(user_id))

    log_audit_change(
        current_user,
        str(transition["audit_action"]),
        target_user_id=user_id,
        before=before,
        after=after,
        meta={"action": action},
    )
    bump_permissions_version()
    return jsonify({"user": after, "action": action})


@bp.patch("/users/<int:user_id>/role")
@login_required
@require_admin
@permission_required("admin.users.manage")
def update_role(user_id: int):
    payload = request.get_json(force=True, silent=True) or {}
    role = (payload.get("role") or "").strip().lower()
    roles_raw = payload.get("roles")
    role_names: list[str] = []
    if isinstance(roles_raw, list):
        role_names = [str(r).strip().lower() for r in roles_raw if str(r).strip()]
    elif roles_raw:
        role_names = [str(roles_raw).strip().lower()]
    elif role:
        role_names = [role]
    if not role_names:
        return _error("Invalid role")
    if len(role_names) == 1 and role_names[0] in RBAC_ROLES:
        role = role_names[0]
    else:
        role = role_names[0]
    with SessionLocal() as s:
        u = s.get(User, user_id)
        if not u:
            return _error("User not found", 404)
        before = _serialize_user(u, list_visibility_for_user(user_id))
        role_changed = False
        u.role = role
        u.updated_at = _now()
        s.add(u)
        s.commit()
        replace_user_roles(user_id, role_names)
        u = s.get(User, user_id)
        after = _serialize_user(u, list_visibility_for_user(user_id))
        log_audit_change(current_user, "admin_update_role", target_user_id=user_id, before=before, after=after, meta={"role": role})
        role_changed = True
        if role_changed:
            bump_permissions_version()
        return jsonify({"user": after})


@bp.patch("/users/<int:user_id>/scope")
@login_required
@require_admin
@permission_required("admin.users.manage")
def update_scope(user_id: int):
    payload = request.get_json(force=True, silent=True) or {}
    scope_lists = _scope_lists_from_payload(payload)
    scope_err = _validate_scope_values(scope_lists)
    if scope_err:
        return _error(scope_err)
    with SessionLocal() as s:
        u = s.get(User, user_id)
        if not u:
            return _error("User not found", 404)
        before = _serialize_user(u, list_visibility_for_user(user_id))
        try:
            scoped = _apply_user_scope(int(user_id), scope_lists)
        except Exception:
            current_app.logger.exception("admin.update_visibility_failed", extra={"user_id": user_id})
            return _error("Failed to update visibility")
        u.updated_at = _now()
        s.add(u)
        s.commit()
        after = _serialize_user(u, scoped.get("rep") or [])
        log_audit_change(current_user, "admin_update_visibility", target_user_id=user_id, before=before, after=after)
        bump_permissions_version()
        return jsonify({"user": after})


@bp.post("/users/<int:user_id>/reset-password")
@login_required
@require_admin
@permission_required("admin.users.manage")
@limiter.limit("5 per hour")
def reset_password(user_id: int):
    if not _invites_enabled():
        return _error("Password reset email is disabled by INVITES_ENABLED=0.", 503)

    reset_link = None
    user_state: dict | None = None
    user_email: str | None = None
    user_name: str | None = None
    with SessionLocal() as s:
        u = s.get(User, user_id)
        if not u:
            return _error("User not found", 404)
        if not (u.email or "").strip():
            return _error("User has no email address.", 400)
        user_state = _serialize_user(u, list_visibility_for_user(user_id))
        user_email = (u.email or "").strip().lower()
        user_name = u.full_name
        try:
            reset_link = _issue_password_link(
                s,
                user=u,
                purpose="reset",
                created_by_user_id=int(getattr(current_user, "id", 0) or 0) or None,
                invalidate_existing=True,
            )
        except Exception:
            s.rollback()
            current_app.logger.exception("admin.reset_password_token_failed", extra={"user_id": int(user_id)})
            return _error("Failed to create reset token.", 500)
        u.must_reset_password = True
        u.updated_at = _now()
        s.add(u)
        s.commit()

    sent = _send_password_email_for_user(
        email=user_email or "",
        full_name=user_name,
        purpose="reset",
        link=reset_link or "",
    )
    if not sent:
        return _error("Reset token created but email delivery failed. Check SMTP relay settings.", 502)

    log_audit_change(
        current_user,
        "admin_reset_password",
        target_user_id=user_id,
        before=user_state,
        after=user_state,
        meta={"reset_link": "[hidden]", "email_sent": True},
    )
    return jsonify({"status": "ok", "email_sent": True})


@bp.post("/users/<int:user_id>/resend-invite")
@login_required
@require_admin
@permission_required("admin.users.manage")
@limiter.limit("5 per hour")
def resend_invite(user_id: int):
    if not _invites_enabled():
        return _error("User invite email is disabled by INVITES_ENABLED=0.", 503)

    invite_link = None
    user_state: dict | None = None
    user_email: str | None = None
    user_name: str | None = None
    with SessionLocal() as s:
        u = s.get(User, user_id)
        if not u:
            return _error("User not found", 404)
        if not (u.email or "").strip():
            return _error("User has no email address.", 400)
        user_state = _serialize_user(u, list_visibility_for_user(user_id))
        user_email = (u.email or "").strip().lower()
        user_name = u.full_name
        invalidate_password_tokens(s, user_id=user_id, purpose="invite")
        try:
            invite_link = _issue_password_link(
                s,
                user=u,
                purpose="invite",
                created_by_user_id=int(getattr(current_user, "id", 0) or 0) or None,
                invalidate_existing=False,
            )
        except Exception:
            s.rollback()
            current_app.logger.exception("admin.resend_invite_token_failed", extra={"user_id": int(user_id)})
            return _error("Failed to create invite token.", 500)
        u.must_reset_password = True
        u.updated_at = _now()
        s.add(u)
        s.commit()

    sent = _send_password_email_for_user(
        email=user_email or "",
        full_name=user_name,
        purpose="invite",
        link=invite_link or "",
    )
    if not sent:
        return _error("Invite token created but email delivery failed. Check SMTP relay settings.", 502)

    log_audit_change(
        current_user,
        "admin_resend_invite",
        target_user_id=user_id,
        before=user_state,
        after=user_state,
        meta={"invite_link": "[hidden]", "email_sent": True},
    )
    return jsonify({"status": "ok", "email_sent": True})


@bp.get("/users/<int:user_id>/audit")
@login_required
@require_admin
@permission_required("admin.users.manage")
def audit_log(user_id: int):
    action_filter = (request.args.get("action") or "").strip()
    with SessionLocal() as s:
        q = (
            s.query(AuditLog)
            .filter((AuditLog.target_user_id == user_id) | (AuditLog.username == str(user_id)))
            .order_by(AuditLog.created_at.desc())
        )
        if action_filter:
            q = q.filter(AuditLog.action == action_filter)
        entries = q.limit(200).all()
        data = []
        for e in entries:
            try:
                before = json.loads(e.before_json) if e.before_json else None
            except Exception:
                before = e.before_json
            try:
                after = json.loads(e.after_json) if e.after_json else None
            except Exception:
                after = e.after_json
            data.append(
                {
                    "id": e.id,
                    "actor_user_id": e.actor_user_id,
                    "target_user_id": e.target_user_id,
                    "action": e.action,
                    "created_at": (e.created_at or e.ts).isoformat() if (e.created_at or e.ts) else None,
                    "ip": e.ip,
                    "user_agent": e.user_agent,
                    "meta": e.meta,
                    "before": before,
                    "after": after,
                }
            )
    return jsonify({"audit": data})


@bp.post("/users/bulk")
@login_required
@require_admin
@permission_required("admin.users.manage")
def bulk_users():
    payload = request.get_json(force=True, silent=True) or {}
    ids = payload.get("user_ids") or []
    action = (payload.get("action") or "").strip().lower()
    if not isinstance(ids, list) or not ids:
        return _error("user_ids must be a non-empty array")
    if action not in {"approve", "disable", "assign_role", "assign_scope"}:
        return _error("Invalid bulk action")
    try:
        ids = [int(i) for i in ids]
    except Exception:
        return _error("user_ids must be integers")
    role = (payload.get("role") or "").strip().lower()
    scope_payload = payload.get("scope") or {}
    if action == "assign_role" and role not in RBAC_ROLES:
        return _error("Invalid role for bulk assignment")
    visibility_ids: list[str] = []
    customer_ids: list[str] = []
    region_ids: list[str] = []
    supplier_ids: list[str] = []
    if action == "assign_scope":
        visibility_ids, scope_err = _validate_visibility(scope_payload)
        if scope_err:
            return _error(scope_err)
        customer_ids = _clean_tokens(scope_payload.get("customer_ids") or scope_payload.get("customers"))
        region_ids = _clean_tokens(scope_payload.get("region_ids") or scope_payload.get("regions"))
        supplier_ids = _clean_tokens(scope_payload.get("supplier_ids") or scope_payload.get("suppliers"))
        scope_lists = {
            "rep": _normalize_rep_scope_values(visibility_ids),
            "customer": customer_ids,
            "region": region_ids,
            "supplier": supplier_ids,
        }
        scope_err = _validate_scope_values(scope_lists)
        if scope_err:
            return _error(scope_err)
    updated = 0
    with SessionLocal() as s:
        users = s.query(User).filter(User.id.in_(ids)).all()
        for u in users:
            before = _serialize_user(u, list_visibility_for_user(u.id))
            if action == "approve":
                u.is_approved = True
                u.is_active = True
            elif action == "disable":
                u.is_active = False
            elif action == "assign_role":
                u.role = role
                replace_user_roles(u.id, [role])
            elif action == "assign_scope":
                scoped = _apply_user_scope(int(u.id), scope_lists)
            u.updated_at = _now()
            s.add(u)
            s.commit()
            after = _serialize_user(u, list_visibility_for_user(u.id))
            log_audit_change(current_user, f"admin_bulk_{action}", target_user_id=u.id, before=before, after=after)
            updated += 1
    if action == "assign_scope":
        bump_permissions_version()
    return jsonify({"status": "ok", "updated": updated})


_options_cache: Dict[str, Any] = {"regions": [], "sales_reps": [], "ts": None}


@bp.get("/users/options")
@login_required
@require_admin
@permission_required("admin.users.manage")
def user_options():
    cache_ttl_min = 15
    now = _now()
    try:
        ts = _options_cache.get("ts")
        if ts and (now - ts).total_seconds() < cache_ttl_min * 60:
            return jsonify({"regions": _options_cache["regions"], "sales_reps": _options_cache["sales_reps"]})
    except Exception:
        pass
    with SessionLocal() as s:
        regions = [r[0] for r in s.query(User.region_id).filter(User.region_id.isnot(None)).distinct().order_by(User.region_id.asc()).all() if r[0]]
        sales_reps = [r[0] for r in s.query(User.erp_user_id).filter(User.erp_user_id.isnot(None)).distinct().order_by(User.erp_user_id.asc()).all() if r[0]]
    _options_cache.update({"regions": regions, "sales_reps": sales_reps, "ts": now})
    return jsonify({"regions": regions, "sales_reps": sales_reps})


@bp.get("/erp-users")
@login_required
@require_admin
@permission_required("admin.users.manage")
def erp_users():
    query = (request.args.get("q") or "").strip()
    if not query or len(query) < 2:
        return jsonify({"users": []})
    try:
        limit = int(request.args.get("limit", 25) or 25)
    except Exception:
        limit = 25
    limit = max(1, min(limit, 50))

    cache_key = f"erp_users:{query.lower()}:{limit}"
    cached = cache.get(cache_key)
    if cached is not None:
        return jsonify({"users": cached})

    if current_app.config.get("TESTING"):
        cache.set(cache_key, [], timeout=31800)
        return jsonify({"users": []})

    try:
        import data_loader as loader  # type: ignore
        from sqlalchemy import text as _sa_text

        cfg = loader.get_config()
        if not getattr(cfg, "server", None):
            return _error("ERP lookup unavailable (missing MSSQL config).", 503)
        eng = loader.create_mssql_engine(cfg)
        like = f"%{query}%"
        sql = _sa_text(
            f"""
            SELECT TOP {limit} UserId, FirstName, LastName
            FROM dbo.UsersNames
            WHERE UserId LIKE :q OR FirstName LIKE :q OR LastName LIKE :q
            ORDER BY LastName, FirstName
            """
        )
        df = pd.read_sql(sql, eng, params={"q": like})
    except Exception:
        current_app.logger.exception("admin.erp_users_lookup_failed", extra={"query": query})
        return _error("ERP user lookup failed.", 503)

    results: list[dict[str, str]] = []
    if isinstance(df, pd.DataFrame) and not df.empty:
        for _, row in df.iterrows():
            user_id = str(row.get("UserId") or "").strip()
            if not user_id:
                continue
            first = str(row.get("FirstName") or "").strip()
            last = str(row.get("LastName") or "").strip()
            full = f"{first} {last}".strip()
            results.append(
                {
                    "user_id": user_id,
                    "first_name": first,
                    "last_name": last,
                    "full_name": full,
                }
            )

    cache.set(cache_key, results, timeout=31800)
    return jsonify({"users": results})


@bp.get("/users/<int:user_id>/preview")
@login_required
@require_admin
@permission_required("admin.users.manage")
def preview_user(user_id: int):
    started = time.perf_counter()
    with SessionLocal() as s:
        u = s.get(User, user_id)
        if not u:
            return _error("User not found", 404)
    visibility = list_visibility_for_user(user_id)
    summary = _serialize_user(u, visibility)
    metrics = {
        "orders_90d": 0,
        "orders_max": 0,
        "revenue_sum": 0.0,
        "cost_coverage_pct": 0.0,
        "top_products": [],
        "top_customers": [],
    }
    try:
        from app.services import fact_store  # type: ignore

        scope_payload = (summary.get("scope") or {}) if isinstance(summary, dict) else {}
        scope = {
            "scope_mode": scope_payload.get("scope_mode") or ("list" if visibility else "none"),
            "allowed_erp_user_ids": scope_payload.get("allowed_erp_user_ids") or visibility,
            "allowed_customer_ids": scope_payload.get("allowed_customer_ids") or [],
            "allowed_region_ids": scope_payload.get("allowed_region_ids") or [],
            "allowed_supplier_ids": scope_payload.get("allowed_supplier_ids") or [],
            "is_admin": bool((summary.get("role") or "").strip().lower() == "admin" or "admin" in (summary.get("roles") or [])),
        }

        scope_cache_key = json.dumps(
            {
                "scope_mode": scope.get("scope_mode"),
                "allowed_erp_user_ids": scope.get("allowed_erp_user_ids"),
                "allowed_customer_ids": scope.get("allowed_customer_ids"),
                "allowed_region_ids": scope.get("allowed_region_ids"),
                "allowed_supplier_ids": scope.get("allowed_supplier_ids"),
                "is_admin": scope.get("is_admin"),
                "dataset_version": fact_store.cache_buster(),
            },
            sort_keys=True,
            default=str,
        )
        metrics_cache_key = f"admin.user.preview.metrics:{int(user_id)}:{scope_cache_key}"
        cached_metrics = cache.get(metrics_cache_key)
        if cached_metrics is not None:
            metrics = cached_metrics
        elif scope.get("scope_mode") != "none" or scope.get("is_admin"):
            df_90 = fact_store.query_fact(
                filters=None,
                scope=scope,
                columns=["OrderId", "Revenue", "Cost", "ProductName", "CustomerName"],
                apply_default_window=True,
                use_cache=True,
            )

            def _safe_sum(frame: pd.DataFrame, cols: List[str]) -> float:
                for c in cols:
                    if c in frame.columns:
                        return float(pd.to_numeric(frame[c], errors="coerce").fillna(0).sum())
                return 0.0

            def _top(frame: pd.DataFrame, key_cols: List[str], value_cols: List[str], limit: int = 5):
                for key in key_cols:
                    if key in frame.columns:
                        value_col = None
                        for v in value_cols:
                            if v in frame.columns:
                                value_col = v
                                break
                        if value_col:
                            grouped = (
                                frame.groupby(key)[value_col]
                                .sum()
                                .sort_values(ascending=False)
                                .reset_index()
                                .head(limit)
                            )
                            return grouped[[key, value_col]].to_dict(orient="records")
                return []

            if isinstance(df_90, pd.DataFrame) and not df_90.empty:
                metrics["orders_90d"] = int(len(df_90))
                metrics["revenue_sum"] = _safe_sum(df_90, ["Revenue"])
                cost_sum = _safe_sum(df_90, ["Cost"])
                metrics["cost_coverage_pct"] = float(
                    0 if metrics["revenue_sum"] == 0 else min(100.0, max(0.0, (cost_sum / metrics["revenue_sum"]) * 100))
                )
                metrics["top_products"] = _top(df_90, ["ProductName", "Product", "Sku", "SKU"], ["Revenue"])
                metrics["top_customers"] = _top(df_90, ["CustomerName", "Customer", "CustomerCode"], ["Revenue"])

            df_full = fact_store.query_fact(
                filters=None,
                scope=scope,
                columns=["OrderId"],
                apply_default_window=False,
                use_cache=True,
            )
            if isinstance(df_full, pd.DataFrame) and not df_full.empty:
                metrics["orders_max"] = int(len(df_full))
            cache.set(metrics_cache_key, metrics, timeout=300)
    except Exception:
        current_app.logger.debug("admin.preview.metrics_failed", exc_info=True)
    duration_ms = int((time.perf_counter() - started) * 1000)
    log_method = current_app.logger.info if duration_ms >= 400 else current_app.logger.debug
    try:
        log_method(
            "admin.user.preview",
            extra={
                "user_id": int(user_id),
                "duration_ms": duration_ms,
                "scope_mode": ((summary.get("scope") or {}).get("scope_mode") if isinstance(summary, dict) else None),
            },
        )
    except Exception:
        pass
    return jsonify({"preview": summary, "metrics": metrics, "meta": {"duration_ms": duration_ms}})

@bp.get("/audit/window")
@login_required
@require_admin
@permission_required("admin.audit.view")
@limiter.limit("10 per minute")
def audit_window():
    """Admin-only audit comparing SQL truth, API output, and persisted snapshot."""
    if not LIVE_SQL_ALLOWED:
        return _error("live_sql_disabled", 503)

    import data_loader as loader  # type: ignore
    from app.services import fact_store  # type: ignore
    from app.services.data_access import get_fact_dataframe  # type: ignore
    from app.services.filters import FilterParams, normalize_filters, apply_filters  # type: ignore

    start_raw = (request.args.get("start") or "2018-01-01").strip()
    end_raw = (request.args.get("end") or "2035-01-01").strip()
    status_arg = request.args.get("statuses") or request.args.get("status")
    status_tokens = [s.strip() for s in (status_arg.split(",") if status_arg else []) if s.strip()]
    statuses = [s.lower() for s in status_tokens] or ["packed"]

    start_ts = pd.to_datetime(start_raw, errors="coerce")
    end_ts = pd.to_datetime(end_raw, errors="coerce")
    filters = normalize_filters(
        FilterParams(
            start=start_ts if not pd.isna(start_ts) else None,
            end=end_ts if not pd.isna(end_ts) else None,
            statuses=tuple(statuses),
        )
    )

    cfg = loader.get_config()
    eng = loader.create_mssql_engine(cfg)
    sql_truth = loader.sql_truth(start_raw, end_raw, status=statuses, engine=eng)

    try:
        api_df = get_fact_dataframe(user=current_user if current_user.is_authenticated else None, filters=filters, use_cache=False)
    except Exception:
        api_df = pd.DataFrame()
    api_df = apply_filters(api_df, filters)
    if hasattr(loader, "_audit_fact_metrics"):
        api_stats = loader._audit_fact_metrics(api_df)  # type: ignore[attr-defined]
    else:
        api_stats = _frame_audit_stats(api_df)
        api_stats.update(_frame_pack_match(api_df))
    api_stats.update({"start": sql_truth["start"], "end": sql_truth["end"], "statuses": statuses})

    try:
        persisted_df = fact_store.get_sales_fact(columns=["Date", "Revenue", "OrderLineId", "OrderId", "ProductId", "OrderStatus"])
        persisted_df = apply_filters(persisted_df, filters)
    except Exception:
        persisted_df = pd.DataFrame()
    if hasattr(loader, "_audit_fact_metrics"):
        persisted_stats = loader._audit_fact_metrics(persisted_df)  # type: ignore[attr-defined]
    else:
        persisted_stats = _frame_audit_stats(persisted_df)
        persisted_stats.update(_frame_pack_match(persisted_df))
    persisted_stats.update({"start": sql_truth["start"], "end": sql_truth["end"], "statuses": statuses})

    manifest = fact_store.get_meta() if hasattr(fact_store, "get_meta") else {}
    payload = {
        "start": sql_truth["start"],
        "end": sql_truth["end"],
        "statuses": statuses,
        "sql_truth": sql_truth,
        "api": api_stats,
        "persisted": persisted_stats,
        "loader_version": fact_store.cache_buster(),
        "manifest_version": manifest.get("dataset_version") if isinstance(manifest, dict) else None,
    }
    return jsonify(payload), 200


@bp.get("/health/data")
@login_required
@require_admin
@permission_required("admin.audit.view")
@limiter.limit("30 per minute")
def data_health():
    """Lightweight admin data health endpoint."""
    from app.services import fact_store  # type: ignore

    try:
        if current_app.config.get("TESTING"):
            try:
                import data_loader as loader  # type: ignore

                df = loader.get_fact_df(columns=["Revenue", "Cost", "ProductId", "OrderLineId", "EffectiveDate", "Date"])
            except TypeError:
                df = fact_store.get_sales_fact(columns=["Revenue", "Cost", "ProductId", "OrderLineId", "EffectiveDate", "Date"])
        else:
            df = fact_store.get_sales_fact(columns=["Revenue", "Cost", "ProductId", "OrderLineId", "EffectiveDate", "Date"])
    except Exception:
        df = pd.DataFrame()

    stats = _frame_audit_stats(df)
    meta = fact_store.get_meta() if hasattr(fact_store, "get_meta") else {}
    payload = {
        "rows": stats.get("rows", 0),
        "fact_rowcount": stats.get("rows", 0),
        "product_count": stats.get("product_ids", stats.get("distinct_products", 0)),
        "revenue_sum": stats.get("rev_sum", stats.get("revenue", 0.0)),
        "cost_sum": stats.get("cost_sum", stats.get("cost", 0.0)),
        "effective_date_null_rate": stats.get("effective_date_null_rate", 0.0),
        "cost_missing_rate": stats.get("cost_null_rate", stats.get("cost_missing_rate", 1.0)),
        "pack_match_rate": stats.get("pack_match_rate", 0.0),
        "last_refresh_ts": meta.get("last_refresh_utc") or meta.get("watermark_dt"),
        "path": meta.get("path"),
    }
    return jsonify(payload), 200


@bp.post("/refresh")
@login_required
@require_admin
@permission_required("manage_features")
def enqueue_refresh():
    """Trigger a background data refresh via the scheduler."""

    if not current_app.config.get("TESTING") and not os.getenv("ALLOW_IN_APP_REFRESH"):
        return jsonify({"status": "disabled", "error": "In-app refresh disabled. Run `python run.py refresh-fact` instead."}), 409
    try:
        refresh_worker.enqueue_refresh()
        try:
            log_audit(current_user, "manual_refresh_request", {})
        except Exception:
            pass
        return jsonify({"status": "queued"}), 202
    except Exception as exc:
        refresh_worker.LOGGER.exception("Manual refresh enqueue failed.")
        return jsonify({"status": "error", "error": str(exc)}), 500


@bp.post("/rebuild-cache")
@login_required
@require_admin
@permission_required("manage_features")
def rebuild_cache():
    """Force a full cache rebuild (non-incremental)."""
    if not current_app.config.get("TESTING") and not os.getenv("ALLOW_IN_APP_REFRESH"):
        return jsonify({"status": "disabled", "error": "In-app rebuild disabled. Run `python run.py build-fact` instead."}), 409
    try:
        from app.services import fact_store  # type: ignore

        path = fact_store.build_full(start_date=os.getenv("INITIAL_START_DATE", "2017-01-01"))
        log_audit(current_user, "manual_rebuild_request", {"path": path})
        return jsonify({"status": "rebuilt", "path": path}), 202
    except Exception as exc:
        current_app.logger.exception("admin.rebuild.failed", exc_info=True)
        return jsonify({"status": "error", "error": str(exc)}), 500


@bp.post("/debug/cache/flush")
@login_required
@require_admin
@permission_required("manage_features")
def debug_cache_flush():
    """Admin-only cache flush for debugging analytics parity."""
    try:
        cache.clear()
        return jsonify({"status": "ok", "cache_cleared": True}), 200
    except Exception as exc:
        current_app.logger.exception("admin.cache_flush_failed")
        return jsonify({"error": str(exc)}), 500
