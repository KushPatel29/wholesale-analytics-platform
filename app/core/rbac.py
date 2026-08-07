"""Role-based access control helpers and dataframe scoping."""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Set

import json
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pandas as pd
from flask import abort, current_app, render_template, request
from app.auth.permissions import (
    ALLOWED_ROLES as AUTH_ALLOWED_ROLES,
    DEFAULT_PERMISSION_CATALOG as AUTH_DEFAULT_PERMISSION_CATALOG,
    DEFAULT_ROLE_PERMISSION_KEYS as AUTH_DEFAULT_ROLE_PERMISSION_KEYS,
    canonical_permission_key,
    canonicalize_permission_keys,
)

try:
    from flask_login import current_user  # type: ignore
except Exception:  # pragma: no cover
    class _CurrentUserShim:
        role: Optional[str] = None
        username: Optional[str] = None
        sales_rep_id: Optional[str] = None
        first_name: Optional[str] = None
        last_name: Optional[str] = None
        is_authenticated: bool = True

        def _get_current_object(self):
            return self

    current_user = _CurrentUserShim()  # type: ignore


def _normalize_role(role: Optional[str]) -> str:
    if not role:
        return ""
    value = str(role).strip().lower()
    role_aliases = {
        "general_manager": "gm",
        "general manager": "gm",
        "administrator": "admin",
        "adminstrator": "admin",
        "superuser": "admin",
        "super_user": "admin",
        "super-user": "admin",
        "super admin": "admin",
        "super_admin": "admin",
        "all_access": "admin",
        "all-access": "admin",
        "full_access": "admin",
        "manager": "sales_manager",
    }
    return role_aliases.get(value, value)


ALLOWED_ROLES: Set[str] = {
    "admin",
    "owner",
    "gm",
    "general_manager",
    "manager",
    "sales_manager",
    "warehouse",
    "sales",
    "production",
    "analyst",
    "viewer",
    "returns_only",
}


SUPER_USERS: Dict[str, str] = {
    "admin": "admin",
    "kush patel": "admin",
    "jason pleym": "owner",
    "kyle mclaw": "gm",
}


DEFAULT_ROLE_PERMISSIONS: Mapping[str, Set[str]] = {
    "admin": {
        "manage_users",
        "manage_roles",
        "manage_visibility",
        "manage_branding",
        "manage_features",
        "view_kpis",
        "view_analytics",
        "view_downloads",
        "view_velocity",
        "view_drilldown",
        "view_product",
        "view_region",
        "view_costs",
    },
    "owner": {
        "manage_users",
        "manage_visibility",
        "manage_branding",
        "view_kpis",
        "view_analytics",
        "view_downloads",
        "view_velocity",
        "view_drilldown",
        "view_product",
        "view_region",
        "view_costs",
    },
    "gm": {
        "manage_visibility",
        "view_kpis",
        "view_analytics",
        "view_downloads",
        "view_velocity",
        "view_drilldown",
        "view_product",
        "view_region",
        "view_costs",
    },
    "sales_manager": {
        "view_kpis",
        "view_analytics",
        "view_velocity",
        "view_drilldown",
        "view_region",
        "view_product",
        "view_costs",
        "page.returns.view",
        "returns.create",
        "returns.approvals.view",
        "returns.approve.mgr",
        "returns.reject",
        "returns.pdf.export",
        "page.returns.analytics.view",
        "returns.ops.queue.view",
        "returns.ops.approve",
        "returns.ops.deny",
        "returns.ops.override",
        "page.returns.customer_portal",
        "page.returns.ops",
        "returns.approve",
        "returns.deny",
        "returns.override",
        "returns.labels.generate",
    },
    "analyst": {
        "view_kpis",
        "view_analytics",
        "view_velocity",
        "view_downloads",
        "view_drilldown",
        "view_region",
        "view_product",
        "view_costs",
    },
    "viewer": {
        "view_kpis",
        "view_analytics",
        "view_velocity",
        "view_region",
    },
    "sales": {
        "view_kpis",
        "view_analytics",
        "view_drilldown",
        "page.returns.view",
        "returns.create",
        "returns.pdf.export",
        "page.returns.analytics.view",
        "page.returns.customer_portal",
    },
    "warehouse": {
        "page.returns.view",
        "returns.approvals.view",
        "returns.approve.wh",
        "returns.reject",
        "returns.pdf.export",
        "returns.warehouse.scan",
        "returns.warehouse.receive",
        "returns.warehouse.inspect",
        "page.returns.warehouse",
    },
    "production": {
        "view_kpis",
        "view_analytics",
        "page.returns.view",
        "returns.approvals.view",
        "returns.approve.wh",
        "returns.reject",
        "returns.pdf.export",
        "returns.warehouse.scan",
        "returns.warehouse.receive",
        "returns.warehouse.inspect",
        "page.returns.warehouse",
    },
    "returns_only": {
        "page.returns.view",
        "returns.pdf.export",
    },
}

# Add modern page/export permission keys while keeping legacy keys for compatibility.
_PAGE_PERMISSION_BY_ROLE: Dict[str, Set[str]] = {
    "admin": {
        "admin.portal.view",
        "admin.users.manage",
        "admin.roles.manage",
        "admin.audit.view",
        "scope.manage",
        "page.overview.view",
        "page.customers.view",
        "page.products.view",
        "page.regions.view",
        "page.suppliers.view",
        "page.salesreps.view",
        "page.forecasting.view",
        "page.notifications.view",
        "admin.notifications.defaults",
        "export.salesrep.xlsx",
        "export.salesrep.csv",
        "export.products.csv",
        "export.suppliers.csv",
    },
    "owner": {
        "admin.portal.view",
        "admin.users.manage",
        "admin.audit.view",
        "scope.manage",
        "page.overview.view",
        "page.customers.view",
        "page.products.view",
        "page.regions.view",
        "page.suppliers.view",
        "page.salesreps.view",
        "page.forecasting.view",
        "page.notifications.view",
        "admin.notifications.defaults",
        "export.salesrep.xlsx",
        "export.salesrep.csv",
        "export.products.csv",
        "export.suppliers.csv",
    },
    "gm": {
        "admin.portal.view",
        "admin.audit.view",
        "scope.manage",
        "page.overview.view",
        "page.customers.view",
        "page.products.view",
        "page.regions.view",
        "page.suppliers.view",
        "page.salesreps.view",
        "page.forecasting.view",
        "page.notifications.view",
        "admin.notifications.defaults",
        "export.salesrep.xlsx",
        "export.salesrep.csv",
        "export.products.csv",
        "export.suppliers.csv",
    },
    "sales_manager": {
        "page.overview.view",
        "page.customers.view",
        "page.products.view",
        "page.regions.view",
        "page.salesreps.view",
        "page.notifications.view",
        "admin.notifications.defaults",
        "export.salesrep.xlsx",
        "export.salesrep.csv",
        "export.products.csv",
    },
    "sales": {
        "page.overview.view",
        "page.customers.view",
        "page.products.view",
        "page.salesreps.view",
        "page.notifications.view",
        "export.salesrep.xlsx",
        "export.salesrep.csv",
    },
    "production": {
        "page.overview.view",
        "page.products.view",
    },
    "analyst": {
        "page.overview.view",
        "page.customers.view",
        "page.products.view",
        "page.regions.view",
        "page.suppliers.view",
        "page.salesreps.view",
        "page.forecasting.view",
        "export.salesrep.xlsx",
        "export.salesrep.csv",
        "export.products.csv",
        "export.suppliers.csv",
    },
    "viewer": {
        "page.overview.view",
        "page.customers.view",
        "page.products.view",
        "page.regions.view",
    },
}
for _role_name, _keys in _PAGE_PERMISSION_BY_ROLE.items():
    DEFAULT_ROLE_PERMISSIONS.setdefault(_role_name, set()).update(_keys)

_RETURNS_ADMIN_PERMS = {
    "page.returns.view",
    "returns.create",
    "returns.approvals.view",
    "returns.warehouse.view",
    "returns.approve.wh",
    "returns.approve.mgr",
    "returns.reject",
    "returns.export",
    "returns.pdf.export",
    "page.returns.analytics.view",
    "returns.ops.queue.view",
    "returns.ops.approve",
    "returns.ops.deny",
    "returns.ops.override",
    "returns.warehouse.scan",
    "returns.warehouse.receive",
    "returns.warehouse.inspect",
    "page.returns.customer_portal",
    "page.returns.ops",
    "page.returns.warehouse",
    "admin.returns.manage",
    "returns.approve",
    "returns.deny",
    "returns.override",
    "returns.labels.generate",
    "returns.refunds.issue",
}
_RETURNS_MANAGER_PERMS = {
    "page.returns.view",
    "returns.create",
    "returns.approvals.view",
    "returns.approve.mgr",
    "returns.reject",
    "returns.export",
    "returns.pdf.export",
    "page.returns.analytics.view",
    "returns.ops.queue.view",
    "returns.ops.approve",
    "returns.ops.deny",
    "returns.ops.override",
    "page.returns.customer_portal",
    "page.returns.ops",
    "returns.approve",
    "returns.deny",
    "returns.override",
    "returns.labels.generate",
}
_RETURNS_SALES_PERMS = {
    "page.returns.view",
    "returns.create",
    "returns.export",
    "returns.pdf.export",
    "page.returns.analytics.view",
    "page.returns.customer_portal",
}
_RETURNS_WAREHOUSE_PERMS = {
    "page.returns.view",
    "returns.warehouse.view",
    "returns.approvals.view",
    "returns.approve.wh",
    "returns.reject",
    "returns.export",
    "returns.pdf.export",
    "returns.warehouse.scan",
    "returns.warehouse.receive",
    "returns.warehouse.inspect",
    "page.returns.warehouse",
}
for _role_name in ("admin", "owner", "gm"):
    DEFAULT_ROLE_PERMISSIONS.setdefault(_role_name, set()).update(_RETURNS_ADMIN_PERMS)
DEFAULT_ROLE_PERMISSIONS.setdefault("sales_manager", set()).update(_RETURNS_MANAGER_PERMS)
DEFAULT_ROLE_PERMISSIONS.setdefault("sales", set()).update(_RETURNS_SALES_PERMS)
DEFAULT_ROLE_PERMISSIONS.setdefault("warehouse", set()).update(_RETURNS_WAREHOUSE_PERMS)
DEFAULT_ROLE_PERMISSIONS.setdefault("production", set()).update(_RETURNS_WAREHOUSE_PERMS)
ALLOWED_ROLES.update(AUTH_ALLOWED_ROLES)
for _role_name, _keys in AUTH_DEFAULT_ROLE_PERMISSION_KEYS.items():
    DEFAULT_ROLE_PERMISSIONS.setdefault(_normalize_role(_role_name), set()).update(
        {str(key).strip() for key in _keys if str(key).strip() and str(key).strip() != "*"}
    )


def _is_admin_request() -> bool:
    """Detect admin routes that should never honor login/authz bypass flags."""
    try:
        path = (request.path or "").lower()
        blueprint = (request.blueprint or "").lower()
    except Exception:  # pragma: no cover - outside request context
        return False
    return path.startswith("/admin") or path.startswith("/api/_admin") or blueprint in {"admin", "admin_api"}

def _get_user():
    try:
        return getattr(current_user, "_get_current_object", lambda: current_user)()
    except Exception:  # pragma: no cover
        return current_user


def _cfg_bool(key: str, default: bool = False) -> bool:
    try:
        val = current_app.config.get(key, default)
        if isinstance(val, bool):
            return val
        return str(val).lower() in {"1", "true", "yes", "on"}
    except Exception:
        return default


def _permissions_v2_enabled() -> bool:
    return _cfg_bool("ADMIN_PERMISSIONS_V2", False)


def _rbac_debug_enabled() -> bool:
    try:
        raw = current_app.config.get("RBAC_DEBUG")
    except Exception:
        raw = None
    if raw is None:
        raw = os.getenv("RBAC_DEBUG", "")
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _merge_role_maps() -> Dict[str, Set[str]]:
    if _permissions_v2_enabled():
        merged: Dict[str, Set[str]] = {
            _normalize_role(role): canonicalize_permission_keys(perms)
            for role, perms in AUTH_DEFAULT_ROLE_PERMISSION_KEYS.items()
        }
    else:
        merged = {role: set(perms) for role, perms in DEFAULT_ROLE_PERMISSIONS.items()}
    try:
        extra = current_app.config.get("AUTHZ_ROLE_PERMISSIONS") or {}
    except Exception:
        extra = {}
    if isinstance(extra, Mapping):
        for role, perms in extra.items():
            try:
                norm = _normalize_role(role)
                if not norm:
                    continue
                merged.setdefault(norm, set()).update(canonicalize_permission_keys(perms or []))
            except Exception:
                continue
    return merged


def _superuser_roles() -> Set[str]:
    try:
        raw = current_app.config.get("AUTHZ_SUPERUSER_ROLES", {"admin"})
    except Exception:
        raw = {"admin"}
    try:
        if isinstance(raw, str):
            raw = [raw]
        return {_normalize_role(r) for r in raw}
    except Exception:
        return {"admin"}


def _bypass_header_token() -> Optional[str]:
    try:
        token = current_app.config.get("AUTHZ_BYPASS_TOKEN")
    except Exception:
        token = None
    if not token:
        return None
    return str(token)


def _is_bypass_active() -> bool:
    token = _bypass_header_token()
    if not token:
        return False
    try:
        return request.headers.get("X-Bypass-AuthZ") == token
    except Exception:
        return False


def _is_super_user(user: Any) -> bool:
    full_name = f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}".strip()
    if not full_name:
        full_name = (getattr(user, "username", "") or "").replace(".", " ").replace("_", " ").strip()
    return full_name.lower() in SUPER_USERS


def _db_role_names(user_obj: Any) -> Set[str]:
    if not _cfg_bool("AUTHZ_DB_PERMISSIONS", True):
        return set()
    if not getattr(user_obj, "is_authenticated", True):
        return set()
    uid = None
    try:
        uid = user_obj.get_id() if hasattr(user_obj, "get_id") else getattr(user_obj, "id", None)
    except Exception:
        uid = getattr(user_obj, "id", None)
    if uid in (None, ""):
        return set()
    try:
        from app.auth.models import list_user_role_names  # type: ignore

        return {_normalize_role(r) for r in list_user_role_names(int(uid))}
    except Exception:
        return set()


def _db_permission_keys(user_obj: Any, roles: Set[str]) -> Set[str]:
    if not _cfg_bool("AUTHZ_DB_PERMISSIONS", True):
        return set()
    if not getattr(user_obj, "is_authenticated", True):
        return set()
    uid = None
    try:
        uid = user_obj.get_id() if hasattr(user_obj, "get_id") else getattr(user_obj, "id", None)
    except Exception:
        uid = getattr(user_obj, "id", None)
    if uid in (None, ""):
        return set()
    fallback_role = None
    if roles:
        fallback_role = sorted(roles)[0]
    else:
        fallback_role = _normalize_role(getattr(user_obj, "role", None))
    try:
        from app.auth.models import list_effective_permission_keys_for_user  # type: ignore

        keys = list_effective_permission_keys_for_user(int(uid), fallback_role=fallback_role)
        return canonicalize_permission_keys(keys)
    except Exception:
        return set()


def _db_permission_rule_sets(user_obj: Any, roles: Set[str]) -> tuple[Set[str], Set[str]]:
    if not _cfg_bool("AUTHZ_DB_PERMISSIONS", True):
        return set(), set()
    if not getattr(user_obj, "is_authenticated", True):
        return set(), set()
    uid = None
    try:
        uid = user_obj.get_id() if hasattr(user_obj, "get_id") else getattr(user_obj, "id", None)
    except Exception:
        uid = getattr(user_obj, "id", None)
    if uid in (None, ""):
        return set(), set()
    try:
        from app.auth.models import list_user_permission_rules  # type: ignore

        rules = list_user_permission_rules(int(uid))
        allow = canonicalize_permission_keys((rules or {}).get("allow") or [])
        deny = canonicalize_permission_keys((rules or {}).get("deny") or [])
        return allow, deny
    except Exception:
        return set(), set()


def _role_names(user: Any = None) -> Set[str]:
    user_obj = user if user is not None else _get_user()
    roles: Set[str] = set()
    if not getattr(user_obj, "is_authenticated", True):
        return roles

    if hasattr(user_obj, "roles") and getattr(user_obj, "roles"):
        try:
            for role in user_obj.roles:
                if role is None:
                    continue
                name = getattr(role, "name", None)
                roles.add(_normalize_role(name if name else role))
        except Exception:
            pass

    role_attr = getattr(user_obj, "role", None)
    if role_attr:
        roles.add(_normalize_role(role_attr))

    has_role_fn = getattr(user_obj, "has_role", None)
    if callable(has_role_fn):
        try:
            for candidate in DEFAULT_ROLE_PERMISSIONS.keys():
                if has_role_fn(candidate):
                    roles.add(_normalize_role(candidate))
        except Exception:
            pass

    if _is_super_user(user_obj):
        roles.add("admin")
    roles.update(_db_role_names(user_obj))
    return {r for r in roles if r}


def roles_for(user: Any) -> Set[str]:
    return _role_names(user)


def current_roles() -> Set[str]:
    return _role_names()


def _granted_permissions(user: Any = None) -> Set[str]:
    granted, _ = _permission_rule_sets(user)
    return granted


def _permission_rule_sets(user: Any = None) -> tuple[Set[str], Set[str]]:
    user_obj = user if user is not None else _get_user()
    roles = _role_names(user_obj)
    merged = _merge_role_maps()
    perms: Set[str] = set()
    for role in roles:
        perms.update(canonicalize_permission_keys(merged.get(role, set())))
    db_effective = _db_permission_keys(user_obj, roles)
    perms.update(db_effective)
    allow_rules, deny_rules = _db_permission_rule_sets(user_obj, roles)
    perms.update(allow_rules)
    if roles & _superuser_roles() or _is_super_user(user_obj):
        if _permissions_v2_enabled():
            perms.update(canonicalize_permission_keys(AUTH_DEFAULT_PERMISSION_CATALOG.keys()))
        else:
            perms.update({"*"})
    perms.difference_update(deny_rules)
    return perms, deny_rules


def effective_permissions(user: Any = None) -> Set[str]:
    return _granted_permissions(user)


def has_role(*roles: str) -> bool:
    wanted = {_normalize_role(r) for r in roles if r}
    if not wanted:
        return True
    user_roles = _role_names()
    if user_roles & _superuser_roles():
        return True
    return bool(user_roles & wanted)


def user_has_role(user: Any, *roles: str) -> bool:
    wanted = {_normalize_role(r) for r in roles if r}
    if not wanted:
        return True
    user_roles = _role_names(user)
    if user_roles & _superuser_roles():
        return True
    return bool(user_roles & wanted)


def has_permission(*perms: str) -> bool:
    wanted = canonicalize_permission_keys(perms)
    if not wanted:
        return True
    granted, denied = _permission_rule_sets()
    if wanted & denied:
        return False
    if "*" in granted:
        return True
    return wanted.issubset(granted)


def has_any_permission(*perms: str) -> bool:
    wanted = canonicalize_permission_keys(perms)
    if not wanted:
        return True
    granted, denied = _permission_rule_sets()
    wanted = {perm for perm in wanted if perm not in denied}
    if not wanted:
        return False
    if "*" in granted:
        return True
    return bool(granted & wanted)


def user_has_permission(user: Any, *perms: str) -> bool:
    wanted = canonicalize_permission_keys(perms)
    if not wanted:
        return True
    granted, denied = _permission_rule_sets(user)
    if wanted & denied:
        return False
    if "*" in granted:
        return True
    return wanted.issubset(granted)


def user_has_any_permission(user: Any, *perms: str) -> bool:
    wanted = canonicalize_permission_keys(perms)
    if not wanted:
        return True
    granted, denied = _permission_rule_sets(user)
    wanted = {perm for perm in wanted if perm not in denied}
    if not wanted:
        return False
    if "*" in granted:
        return True
    return bool(granted & wanted)


def log_rbac_decision(
    *,
    allowed: bool,
    reason: str,
    required_roles: Iterable[str] = (),
    required_all_perms: Iterable[str] = (),
    required_any_perms: Iterable[str] = (),
    user: Any = None,
) -> None:
    if not _rbac_debug_enabled():
        return
    user_obj = user if user is not None else _get_user()
    try:
        uid = user_obj.get_id() if hasattr(user_obj, "get_id") else getattr(user_obj, "id", None)
    except Exception:
        uid = getattr(user_obj, "id", None)
    try:
        current_path = request.path
        endpoint = request.endpoint
    except Exception:  # pragma: no cover - outside request context
        current_path = None
        endpoint = None
    payload = {
        "allowed": bool(allowed),
        "reason": str(reason or "").strip() or ("allowed" if allowed else "denied"),
        "path": current_path,
        "endpoint": endpoint,
        "user_id": uid,
        "username": getattr(user_obj, "username", None),
        "email": getattr(user_obj, "email", None),
        "roles": sorted(_role_names(user_obj)),
        "permissions": sorted(_granted_permissions(user_obj)),
        "required_roles": sorted({str(v).strip() for v in required_roles if str(v).strip()}),
        "required_all_permissions": sorted({str(v).strip() for v in required_all_perms if str(v).strip()}),
        "required_any_permissions": sorted({str(v).strip() for v in required_any_perms if str(v).strip()}),
    }
    try:
        current_app.logger.info("rbac.decision", extra=payload)
    except Exception:
        pass


def _require_authz(
    check_fn: Callable[[], bool],
    error_desc: str,
    *,
    required_roles: Iterable[str] = (),
    required_all_perms: Iterable[str] = (),
    required_any_perms: Iterable[str] = (),
    deny_reason: str | None = None,
):
    def decorator(fn: Callable):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = _get_user()
            login_disabled = _cfg_bool("LOGIN_DISABLED", False)
            authz_disabled = _cfg_bool("AUTHZ_DISABLED", False) or _is_bypass_active()
            # Allow full bypass when auth is explicitly disabled (e.g., demos/dev)
            if login_disabled or authz_disabled:
                log_rbac_decision(
                    allowed=True,
                    reason="authz_bypass",
                    required_roles=required_roles,
                    required_all_perms=required_all_perms,
                    required_any_perms=required_any_perms,
                    user=user,
                )
                target = fn
                # Unwrap any decorators (e.g., login_required) so we call the real view
                while hasattr(target, "__wrapped__"):
                    target = target.__wrapped__  # type: ignore[attr-defined]
                return target(*args, **kwargs)
            if not getattr(user, "is_authenticated", False):
                log_rbac_decision(
                    allowed=False,
                    reason="authentication_required",
                    required_roles=required_roles,
                    required_all_perms=required_all_perms,
                    required_any_perms=required_any_perms,
                    user=user,
                )
                abort(401, description="Authentication required")
            allowed = bool(check_fn())
            log_rbac_decision(
                allowed=allowed,
                reason="authorization_granted" if allowed else (deny_reason or error_desc),
                required_roles=required_roles,
                required_all_perms=required_all_perms,
                required_any_perms=required_any_perms,
                user=user,
            )
            if not allowed:
                abort(403, description=error_desc)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def role_required(*roles: str):
    wanted = tuple(_normalize_role(r) for r in roles if r)
    msg = f"Insufficient role; requires any of: {', '.join(wanted) or 'N/A'}"
    return _require_authz(
        lambda: has_role(*wanted),
        msg,
        required_roles=wanted,
        deny_reason="missing_role",
    )


def permission_required(*perms: str):
    wanted = tuple(str(p).strip() for p in perms if str(p).strip())
    msg = f"Missing permission(s): {', '.join(wanted) or 'N/A'}"
    return _require_authz(
        lambda: has_permission(*wanted),
        msg,
        required_all_perms=wanted,
        deny_reason="missing_permission",
    )


def any_permission_required(*perms: str):
    wanted = tuple(str(p).strip() for p in perms if str(p).strip())
    msg = f"Requires any permission from: {', '.join(wanted) or 'N/A'}"
    return _require_authz(
        lambda: has_any_permission(*wanted),
        msg,
        required_any_perms=wanted,
        deny_reason="missing_any_permission",
    )


def requires(
    *,
    roles: Iterable[str] = (),
    all_perms: Iterable[str] = (),
    any_perms: Iterable[str] = (),
):
    roles_norm = tuple(_normalize_role(r) for r in roles if r)
    all_norm = tuple(str(p).strip() for p in all_perms if str(p).strip())
    any_norm = tuple(str(p).strip() for p in any_perms if str(p).strip())

    def _checker() -> bool:
        ok = True
        if roles_norm:
            ok &= has_role(*roles_norm)
        if all_norm:
            ok &= has_permission(*all_norm)
        if any_norm:
            ok &= has_any_permission(*any_norm)
        return ok

    desc_parts: list[str] = []
    if roles_norm:
        desc_parts.append(f"roles any of [{', '.join(roles_norm)}]")
    if all_norm:
        desc_parts.append(f"permissions all [{', '.join(all_norm)}]")
    if any_norm:
        desc_parts.append(f"permissions any [{', '.join(any_norm)}]")
    msg = "Authorization failed" + (": " + " & ".join(desc_parts) if desc_parts else "")
    return _require_authz(
        _checker,
        msg,
        required_roles=roles_norm,
        required_all_perms=all_norm,
        required_any_perms=any_norm,
        deny_reason="composite_authz_failed",
    )


def request_permission_policy(path: str | None = None) -> dict[str, tuple[tuple[str, ...], ...] | tuple[str, ...]] | None:
    if not _permissions_v2_enabled():
        return None
    try:
        current_path = (path or request.path or "").lower()
        current_query = request.query_string.decode(errors="ignore").lower()
    except Exception:
        current_path = str(path or "").lower()
        current_query = ""
    if not current_path:
        return None
    if current_path.startswith("/static/") or current_path.startswith("/auth/"):
        return None

    required_all: list[str] = []
    required_any: list[tuple[str, ...]] = []

    if current_path == "/" or current_path.startswith("/overview") or current_path.startswith("/api/overview"):
        required_all.append("page.overview.view")
        if current_path.endswith("/forecast"):
            required_all.append("feature.overview.forecast.view")
        elif current_path.endswith("/insights"):
            required_all.append("feature.overview.executive_insights.view")
        elif current_path.endswith("/api/movers") or current_path.endswith("/drilldown/movers"):
            required_all.append("feature.overview.movers.view")
        elif current_path.endswith("/drilldown/margin_risk"):
            required_all.append("data.margin_risk.view")
        if "export" in current_path or "download" in current_path:
            required_all.append("export.overview")
    elif current_path.startswith("/api/customers/drilldown/bundle"):
        required_all.extend(("page.customers.view", "page.customers.drilldown.view"))
    elif current_path.startswith("/api/customers/bundle"):
        required_all.extend(("page.customers.view", "feature.customers.dashboard.view"))
    elif current_path.startswith("/customers/cohorts"):
        required_all.extend(("page.customers.view", "feature.customers.cohorts.view"))
        if "download" in current_path or "export" in current_path:
            required_all.append("export.customers")
    elif current_path.startswith("/customers/rfm"):
        required_all.extend(("page.customers.view", "feature.customers.rfm.view"))
        if "export" in current_path:
            required_all.append("export.customers")
    elif current_path.startswith("/customers/clv"):
        required_all.extend(("page.customers.view", "feature.customers.clv.view"))
        if "export" in current_path:
            required_all.append("export.customers")
    elif current_path.startswith("/customers/drilldown/"):
        required_all.extend(("page.customers.view", "page.customers.drilldown.view"))
    elif current_path.startswith("/customers/export"):
        required_all.extend(("page.customers.view", "export.customers"))
        if "page=drilldown" in current_query:
            required_all.append("page.customers.drilldown.view")
        elif "page=rfm" in current_query:
            required_all.append("feature.customers.rfm.view")
        elif "page=cohorts" in current_query:
            required_all.append("feature.customers.cohorts.view")
        elif "page=clv" in current_query:
            required_all.append("feature.customers.clv.view")
        else:
            required_all.append("feature.customers.dashboard.view")
    elif current_path.startswith("/customers"):
        required_all.extend(("page.customers.view", "feature.customers.dashboard.view"))
        if "export" in current_path or "download" in current_path:
            required_all.append("export.customers")
    elif current_path.startswith("/api/products/drilldown/bundle"):
        required_all.extend(("page.products.view", "page.products.drilldown.view"))
    elif current_path.startswith("/api/products/bundle"):
        required_all.extend(("page.products.view", "feature.products.dashboard.view"))
    elif current_path.startswith("/products/api/trend_delta") or current_path.startswith("/products/api/trend"):
        required_all.extend(("page.products.view", "feature.products.trajectory.view"))
    elif current_path.startswith("/products/api/price_distribution") or current_path.startswith("/products/api/bubble"):
        required_all.extend(("page.products.view", "feature.products.pricing.view"))
    elif current_path.startswith("/products/api/table"):
        required_all.extend(("page.products.view", "feature.products.table.view"))
    elif current_path.startswith("/api/products/") and current_path.endswith("/forecast"):
        required_all.extend(("page.products.view", "page.products.drilldown.view", "feature.products.forecast.view"))
    elif current_path.startswith("/products/api/recommendations"):
        required_all.extend(
            ("page.products.view", "feature.products.recommendations.view", "data.price_recommendation.view")
        )
    elif current_path.startswith("/products/api/segments"):
        required_all.extend(("page.products.view", "feature.products.segments.view"))
    elif current_path.startswith("/products/export/execution"):
        required_all.extend(("page.products.view", "feature.products.pricing.view", "export.products"))
    elif current_path.startswith("/products/export/segment_mix") or current_path.startswith("/products/export/quadrant"):
        required_all.extend(("page.products.view", "feature.products.segments.view", "export.products"))
    elif current_path.startswith("/products/export/table"):
        required_all.extend(("page.products.view", "feature.products.table.view", "export.products"))
    elif current_path.startswith("/products/export/overview"):
        required_all.extend(("page.products.view", "feature.products.dashboard.view", "export.products"))
    elif current_path.startswith("/products/") and current_path.endswith("/drilldown/export"):
        required_all.extend(("page.products.view", "page.products.drilldown.view", "export.products"))
    elif current_path.startswith("/products/") and current_path.endswith("/export"):
        required_all.extend(("page.products.view", "page.products.drilldown.view", "export.products"))
    elif current_path.startswith("/products/") and current_path.endswith("/recommendations"):
        required_all.extend(
            (
                "page.products.view",
                "page.products.drilldown.view",
                "feature.products.recommendations.view",
                "data.price_recommendation.view",
            )
        )
    elif current_path.startswith("/products/") and current_path not in {"/products", "/products/"} and not current_path.startswith("/products/api/") and not current_path.startswith("/products/export/"):
        required_all.extend(("page.products.view", "page.products.drilldown.view"))
    elif current_path.startswith("/products/api/") or current_path.startswith("/products"):
        required_all.append("page.products.view")
        if "/drilldown" in current_path:
            required_all.append("page.products.drilldown.view")
        if "export" in current_path:
            required_all.append("export.products")
        if current_path.endswith("/api/bundle") or current_path.endswith("/api/overview"):
            required_all.append("feature.products.dashboard.view")
    elif current_path.startswith("/api/regions/drilldown/bundle"):
        required_all.extend(("page.regions.view", "page.regions.drilldown.view"))
    elif current_path.startswith("/api/regions/bundle"):
        required_all.append("page.regions.view")
    elif current_path.startswith("/regions"):
        required_all.append("page.regions.view")
        if current_path.startswith("/regions/export") or "export" in current_path or "download" in current_path:
            required_all.append("export.regions")
        elif current_path not in {"/regions", "/regions/"}:
            required_all.append("page.regions.drilldown.view")
    elif current_path.startswith("/api/suppliers/") and "/drilldown/export" in current_path:
        required_all.extend(("page.suppliers.view", "page.suppliers.drilldown.view", "export.suppliers"))
        if "products_vs_customers" in current_query or "products-vs-customers" in current_query:
            required_all.append("export.suppliers.products_vs_customers")
    elif current_path.startswith("/api/suppliers/") and current_path.endswith("/products/export.csv"):
        required_all.extend(("page.suppliers.view", "page.suppliers.drilldown.view", "export.suppliers"))
    elif current_path.startswith("/api/suppliers/export"):
        required_all.extend(("page.suppliers.view", "export.suppliers"))
    elif current_path.startswith("/api/suppliers/drilldown/bundle"):
        required_all.extend(("page.suppliers.view", "page.suppliers.drilldown.view"))
    elif current_path.startswith("/api/suppliers/bundle"):
        required_all.append("page.suppliers.view")
    elif current_path.startswith("/api/suppliers") or current_path.startswith("/suppliers"):
        required_all.append("page.suppliers.view")
        if "export" in current_path or "download" in current_path:
            required_all.append("export.suppliers")
        elif current_path not in {"/suppliers", "/suppliers/"}:
            required_all.append("page.suppliers.drilldown.view")
    elif current_path.startswith("/labor/api/") or current_path.startswith("/api/labor/"):
        required_all.append("page.labor.view")
        if "export" in current_path or "download" in current_path:
            required_all.append("export.labor")
    elif current_path.startswith("/labor"):
        required_all.append("page.labor.view")
        if "export" in current_path or "download" in current_path:
            required_all.append("export.labor")
    elif current_path.startswith("/api/salesreps/drilldown/bundle"):
        required_all.extend(("page.salesreps.view", "page.salesreps.drilldown.view"))
    elif current_path.startswith("/api/salesreps/bundle"):
        required_all.append("page.salesreps.view")
    elif current_path.startswith("/salesreps"):
        required_all.append("page.salesreps.view")
        if "export" in current_path or "format=csv" in current_query or "format=xlsx" in current_query:
            required_all.append("export.salesreps")
        elif current_path not in {"/salesreps", "/salesreps/"}:
            required_all.append("page.salesreps.drilldown.view")
    elif current_path.startswith("/admin/notifications-defaults"):
        required_all.append("admin.notifications.defaults")
    elif current_path.startswith("/admin/returns"):
        required_all.append("admin.returns.manage")
    elif current_path.startswith("/assistant") or current_path.startswith("/api/assistant"):
        required_any.append(
            (
                "page.overview.view",
                "page.customers.view",
                "page.products.view",
                "page.regions.view",
                "page.suppliers.view",
                "page.labor.view",
                "page.salesreps.view",
                "page.returns.view",
                "page.admin.view",
                "page.notifications.view",
            )
        )
    elif current_path.startswith("/admin") or current_path.startswith("/api/_admin"):
        required_all.append("page.admin.view")
    elif current_path.startswith("/returns/webhooks") or current_path.startswith("/health/returns"):
        return None
    elif current_path.startswith("/returns/ops"):
        required_any.append(("returns.ops.queue.view", "page.returns.ops", "admin.returns.manage"))
        if "export" in current_path:
            required_all.append("export.returns")
    elif current_path.startswith("/returns/wh"):
        required_any.append(
            (
                "returns.warehouse.scan",
                "returns.warehouse.receive",
                "returns.warehouse.inspect",
                "returns.warehouse.view",
                "admin.returns.manage",
            )
        )
        if "export" in current_path:
            required_all.append("export.returns")
    elif current_path.startswith("/returns"):
        required_any.append(
            (
                "page.returns.view",
                "returns.create",
                "page.returns.customer_portal",
                "admin.returns.manage",
            )
        )
        if "export" in current_path or current_path.endswith(".pdf"):
            required_all.append("export.returns")
    elif current_path.startswith("/notifications") or current_path.startswith("/settings/notifications"):
        required_all.append("page.notifications.view")

    if not required_all and not required_any:
        return None
    return {
        "all": tuple(dict.fromkeys(canonical_permission_key(key) for key in required_all if canonical_permission_key(key))),
        "any": tuple(
            tuple(
                dict.fromkeys(
                    canonical_permission_key(key)
                    for key in group
                    if canonical_permission_key(key)
                )
            )
            for group in required_any
            if group
        ),
    }


def route_permission_override_allows_request() -> bool | None:
    policy = request_permission_policy()
    if policy is None:
        return None
    required_all = tuple(policy.get("all") or ())
    required_any = tuple(policy.get("any") or ())
    if required_all and not has_permission(*required_all):
        return False
    for group in required_any:
        if group and not has_any_permission(*group):
            return False
    return True


def requires_roles(*roles: str) -> Callable:
    wanted = {_normalize_role(r) for r in roles if r}
    msg = f"Insufficient role; requires any of: {', '.join(sorted(wanted)) or 'N/A'}"

    def decorator(fn: Callable):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = _get_user()
            login_disabled = _cfg_bool("LOGIN_DISABLED", False)
            authz_disabled = _cfg_bool("AUTHZ_DISABLED", False) or _is_bypass_active()
            
            if (login_disabled or authz_disabled) and not _is_admin_request():
                log_rbac_decision(
                    allowed=True,
                    reason="authz_bypass",
                    required_roles=wanted,
                    user=user,
                )
                target = fn
                while hasattr(target, "__wrapped__"):
                    target = target.__wrapped__  # type: ignore[attr-defined]
                return target(*args, **kwargs)
            if not getattr(user, "is_authenticated", False):
                log_rbac_decision(
                    allowed=False,
                    reason="authentication_required",
                    required_roles=wanted,
                    user=user,
                )
                abort(401, description="Authentication required")
            if hasattr(user, "is_approved") and not getattr(user, "is_approved"):
                log_rbac_decision(
                    allowed=False,
                    reason="user_not_approved",
                    required_roles=wanted,
                    user=user,
                )
                abort(403, description="User is not approved yet.")
            if hasattr(user, "is_active") and not getattr(user, "is_active"):
                log_rbac_decision(
                    allowed=False,
                    reason="user_disabled",
                    required_roles=wanted,
                    user=user,
                )
                abort(403, description="User account is disabled.")
            v2_override = route_permission_override_allows_request()
            if v2_override is not None:
                log_rbac_decision(
                    allowed=bool(v2_override),
                    reason="authorization_granted" if v2_override else "missing_permission",
                    required_roles=wanted,
                    user=user,
                )
                if not v2_override:
                    abort(403, description="Missing permission for this route.")
                return fn(*args, **kwargs)
            allowed = not wanted or has_role(*wanted)
            log_rbac_decision(
                allowed=allowed,
                reason="authorization_granted" if allowed else "missing_role",
                required_roles=wanted,
                user=user,
            )
            if not allowed:
                try:
                    return (
                        render_template(
                            "errors/403.html",
                            required_roles=sorted(wanted),
                            user_role=_normalize_role(getattr(user, "role", None)),
                        ),
                        403,
                    )
                except Exception:
                    abort(403, description=msg)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def feature_enabled(flag: str, default: bool = False) -> bool:
    try:
        features = current_app.config.get("FEATURES") or {}
        if isinstance(features, Mapping):
            val = features.get(flag, default)
            if isinstance(val, bool):
                return val
            return str(val).lower() in {"1", "true", "yes", "on"}
    except Exception:
        pass
    return default


def _coerce_series_to_str(s: pd.Series) -> pd.Series:
    try:
        return s.astype(str)
    except Exception:
        return s.astype("string")


def can_view_page(page_key: str, user: Any = None) -> bool:
    token = str(page_key or "").strip().lower()
    if not token:
        return False
    permission = token if "." in token else f"page.{token}.view"
    return user_has_permission(user, permission) if user is not None else has_permission(permission)


def can_view_feature(feature_key: str, user: Any = None) -> bool:
    token = str(feature_key or "").strip().lower()
    if not token:
        return False
    permission = token if "." in token else f"feature.{token}.view"
    return user_has_permission(user, permission) if user is not None else has_permission(permission)


def can_export(export_key: str, user: Any = None) -> bool:
    token = str(export_key or "").strip().lower()
    if not token:
        return False
    permission = token if "." in token else f"export.{token}"
    return user_has_permission(user, permission) if user is not None else has_permission(permission)


def can_view_sensitive(data_key: str, user: Any = None) -> bool:
    token = str(data_key or "").strip().lower()
    if not token:
        return False
    permission = token if "." in token else f"data.{token}.view"
    return user_has_permission(user, permission) if user is not None else has_permission(permission)


def can_view_costs(user: Any = None) -> bool:
    """Check if the user has permission to view cost-related data."""
    if _cfg_bool("LOGIN_DISABLED", False) or _cfg_bool("AUTHZ_DISABLED", False) or _is_bypass_active():
        return True
    return can_view_sensitive("data.cost.view", user)


def can_view_margin(user: Any = None) -> bool:
    if not can_view_costs(user):
        return False
    return can_view_sensitive("data.margin.view", user)


def can_view_profit(user: Any = None) -> bool:
    if not can_view_costs(user):
        return False
    return can_view_sensitive("data.profit.view", user)


def can_view_price_recommendations(user: Any = None) -> bool:
    if not can_view_costs(user):
        return False
    if user is not None:
        return user_has_permission(user, "feature.products.recommendations.view", "data.price_recommendation.view")
    return has_permission("feature.products.recommendations.view", "data.price_recommendation.view")


def can_view_margin_risk(user: Any = None) -> bool:
    if not can_view_costs(user):
        return False
    return can_view_sensitive("data.margin_risk.view", user)


def can_export_sensitive_data(user: Any = None) -> bool:
    if not can_view_costs(user):
        return False
    return can_export("export.sensitive.unmasked", user)


def can_manage_visibility(user) -> bool:
    if _is_super_user(user):
        return True
    roles = roles_for(user)
    if roles & _superuser_roles():
        return True
    return bool(roles & {"admin", "owner", "gm"})


def _grants_path() -> Path:
    env_path = os.getenv("VISIBILITY_GRANTS_JSON")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return (Path(os.getenv("FLASK_INSTANCE_PATH", "instance")) / "visibility_grants.json").resolve()


def _load_grants() -> Dict[str, Any]:
    path = _grants_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}


def _user_keys_for_lookup(user) -> Sequence[str]:
    keys = []
    username = (getattr(user, "username", "") or "").strip().lower()
    if username:
        keys.append(username)
    guid = (getattr(user, "sales_rep_id", "") or "").strip().lower()
    if guid:
        keys.append(guid)
    return keys


def _apply_allowlists(df: pd.DataFrame, allow: Dict[str, Sequence[str]]) -> pd.Series:
    if df is None or df.empty or not allow:
        return pd.Series(False, index=df.index)
    mask = pd.Series(False, index=df.index)

    def add(columns, values):
        nonlocal mask
        if not values:
            return
        items = {str(v).strip() for v in values if str(v).strip()}
        for col in columns:
            mask |= _coerce_series_to_str(df[col]).isin(items)

    add([c for c in df.columns if c.lower() in {"customerid", "customer_id", "customer", "customer_code"}], allow.get("customers", []))
    add([c for c in df.columns if c.lower() in {"region", "region_name", "regionid", "region_id"}], allow.get("regions", []))
    add([c for c in df.columns if c.lower() in {"productid", "product_id", "sku", "product"}], allow.get("products", []))
    return mask


def _apply_denylists(df: pd.DataFrame, deny: Dict[str, Sequence[str]]) -> pd.Series:
    if df is None or df.empty or not deny:
        return pd.Series(False, index=df.index)
    mask = pd.Series(False, index=df.index)

    def add(columns, values):
        nonlocal mask
        if not values:
            return
        items = {str(v).strip() for v in values if str(v).strip()}
        for col in columns:
            mask |= _coerce_series_to_str(df[col]).isin(items)

    add([c for c in df.columns if c.lower() in {"customerid", "customer_id", "customer", "customer_code"}], deny.get("customers", []))
    add([c for c in df.columns if c.lower() in {"region", "region_name", "regionid", "region_id"}], deny.get("regions", []))
    add([c for c in df.columns if c.lower() in {"productid", "product_id", "sku", "product"}], deny.get("products", []))
    return mask


def scope_dataframe(df: pd.DataFrame, user) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df

    df = df.copy() # Make a copy to avoid modifying the original DataFrame
    
    # Bypass if auth is disabled in config
    if _cfg_bool("AUTHZ_DISABLED", False) or _cfg_bool("LOGIN_DISABLED", False):
        return df

    user_obj = user or _get_user()
    if user_obj is None or not getattr(user_obj, "is_authenticated", False):
        return df.head(0)  # Return empty frame for unauthenticated users
    if hasattr(user_obj, "is_approved") and not getattr(user_obj, "is_approved", True):
        return df.head(0)
    if hasattr(user_obj, "is_active") and not getattr(user_obj, "is_active", True):
        return df.head(0)

    # Shim for placeholder/mock users in dev/testing
    if user_obj.__class__.__name__ == "_CurrentUserShim":
        return df

    roles = roles_for(user_obj)
    
    # Log user details for easier debugging
    try:
        current_app.logger.debug(
            "Scoping dataframe for user",
            extra={
                "user_id": getattr(user_obj, "id", "N/A"),
                "user_name": getattr(user_obj, "username", "N/A"),
                "user_roles": sorted(list(roles)),
            },
        )
    except Exception:
        pass  # Avoid logging failures in production

    def _tokens(raw) -> list[str]:
        if raw is None:
            return []
        if isinstance(raw, str):
            return [p.strip() for p in raw.replace(";", ",").replace("|", ",").split(",") if p.strip()]
        vals = []
        try:
            for item in raw:
                if item is None:
                    continue
                sval = str(item).strip()
                if sval:
                    vals.append(sval)
        except Exception:
            pass
        return vals

    # Pull persisted scope (if available)
    scope_data = {"region_ids": [], "sales_rep_ids": [], "is_super_user": False, "max_history_days": None, "effective_start_date": None}
    try:
        from app.auth.models import get_scope_for_user  # type: ignore
        uid = getattr(user_obj, "id", None)
        if uid:
            scope_obj = get_scope_for_user(int(uid))
            scope_data = scope_obj.as_dict()
    except Exception:
        current_app.logger.debug("scope_dataframe.scope_lookup_failed", exc_info=True)

    region_tokens = _tokens(scope_data.get("region_ids")) or _tokens(getattr(user_obj, "region_id", None))
    rep_tokens = _tokens(scope_data.get("sales_rep_ids")) or _tokens(getattr(user_obj, "sales_rep_id", None))
    scope_effective_start = scope_data.get("effective_start_date")
    scope_max_history = scope_data.get("max_history_days")
    scope_super = bool(scope_data.get("is_super_user"))
    is_super_user = bool(scope_super or (roles & {"admin", "owner", "gm"}))

    def mask_by_guid() -> pd.Series:
        if not rep_tokens:
            return pd.Series(False, index=df.index)
        token_set = {t for t in rep_tokens if t}
        if not token_set:
            return pd.Series(False, index=df.index)
        mask = pd.Series(False, index=df.index)

        rep_cols = [c for c in df.columns if c.lower() in {"salesrepid", "sales_rep_id", "userid", "user_id"}]
        for col in rep_cols:
            mask |= _coerce_series_to_str(df[col]).isin(token_set)

        primary_cols = [c for c in df.columns if c.lower() in {"primarysalesrepid", "customer_primarysalesrepid", "primary_sales_rep_id"}]
        for col in primary_cols:
            mask |= _coerce_series_to_str(df[col]).isin(token_set)
            
        return mask

    def mask_by_region() -> pd.Series:
        if not region_tokens:
            return pd.Series(False, index=df.index)
        mask = pd.Series(False, index=df.index)
        region_cols = [c for c in df.columns if c.lower() in {"region", "region_name", "regionid", "region_id"}]
        if not region_cols:
            return mask
        for col in region_cols:
            mask |= _coerce_series_to_str(df[col]).isin(region_tokens)
        return mask

    selected_role = _normalize_role(getattr(user_obj, "role", None))
    if "sales_manager" in roles:
        selected_role = "sales_manager"
    elif "sales" in roles:
        selected_role = "sales"

    # Admin/gm/owner should always see all data (no scoping), regardless of any stored region/rep tokens.
    if is_super_user:
        base = pd.Series(True, index=df.index)
    elif region_tokens and (selected_role == "sales_manager" or not rep_tokens):
        base = mask_by_region()
        if rep_tokens:
            base |= mask_by_guid()
    else:
        base = mask_by_guid()
        if region_tokens and selected_role == "sales_manager":
            base |= mask_by_region()
    if not is_super_user and base.empty:
        return df.head(0)

    # Apply visibility grants (allow/deny lists)
    grants = _load_grants()
    grant = None
    for key in _user_keys_for_lookup(user_obj):
        if key in grants:
            grant = grants[key]
            break

    if grant:
        allow = grant.get("allow") or {}
        deny = grant.get("deny") or {}
        add_mask = _apply_allowlists(df, allow) if allow else pd.Series(False, index=df.index)
        drop_mask = _apply_denylists(df, deny) if deny else pd.Series(False, index=df.index)
        
        final_mask = (base | add_mask) & (~drop_mask)
        current_app.logger.debug(
            "Applying grants", 
            extra={"user": getattr(user_obj, "username", "N/A"), "grant_found": True, "base_rows": base.sum(), "add_rows": add_mask.sum(), "drop_rows": drop_mask.sum(), "final_rows": final_mask.sum()}
        )
        work = df.loc[final_mask]
    else:
        current_app.logger.debug(
            "No grants found, applying base filter", 
            extra={"user": getattr(user_obj, "username", "N/A"), "base_rows": base.sum()}
        )
        work = df.loc[base]

    # Apply history window (default 3 months) unless super user with no limits
    def _history_cutoff() -> Optional[pd.Timestamp]:
        default_days = 90
        try:
            default_days = int(os.getenv("DEFAULT_HISTORY_DAYS", "90"))
        except Exception:
            default_days = 90
        candidates = []
        now_date = datetime.now(timezone.utc).date()
        if (not is_super_user) or scope_max_history or scope_effective_start:
            candidates.append(now_date - timedelta(days=default_days))
        if scope_max_history:
            try:
                candidates.append(now_date - timedelta(days=int(scope_max_history)))
            except Exception:
                pass
        if scope_effective_start:
            try:
                start_dt = pd.to_datetime(scope_effective_start).date()
                candidates.append(start_dt)
            except Exception:
                pass
        if not candidates:
            return None
        return pd.to_datetime(max(candidates))

    def _apply_history(frame: pd.DataFrame, cutoff: Optional[pd.Timestamp]) -> pd.DataFrame:
        if cutoff is None:
            return frame
        date_cols = [c for c in frame.columns if c.lower() in {"date", "orderdate", "order_date", "shipdate", "ship_date", "invoice_date", "invoicedate"}]
        if not date_cols:
            return frame
        mask = pd.Series(False, index=frame.index)
        for col in date_cols:
            try:
                mask |= pd.to_datetime(frame[col], errors="coerce") >= cutoff
            except Exception:
                continue
        return frame.loc[mask]

    cutoff_ts = _history_cutoff()
    return _apply_history(work, cutoff_ts)
