"""Centralized access policy + RBAC scope helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Iterable, Optional, Mapping, Any

from flask import abort, current_app


_PERM_VERSION_KEY = "rbac.permissions_version"
_PERM_VERSION_TTL = 60 * 60 * 24 * 365 * 5  # 5 years
_ALL_SCOPE_TOKENS = {"__all__", "all", "*"}


def _as_bool(value: object) -> bool:
    try:
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    except Exception:
        return False


def _normalize_tokens(values: Iterable[object] | None) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    for item in values:
        if item is None:
            continue
        val = str(item).strip()
        if not val:
            continue
        out.append(val.lower())
    # Preserve order but de-duplicate
    seen = set()
    unique: list[str] = []
    for val in out:
        if val in seen:
            continue
        seen.add(val)
        unique.append(val)
    return unique


def _normalize_token(value: Optional[object]) -> Optional[str]:
    if value is None:
        return None
    val = str(value).strip()
    return val.lower() if val else None


def _hash_scope(scope_mode: str, allowed_map: Mapping[str, Iterable[str]]) -> str:
    parts = [scope_mode]
    for key in ("rep", "customer", "region", "supplier"):
        vals = sorted({str(v).strip().lower() for v in (allowed_map.get(key) or []) if str(v).strip()})
        parts.append(f"{key}:{'|'.join(vals)}")
    base = ";".join(parts)
    return sha256(base.encode("utf-8")).hexdigest()


def _cfg_flag(name: str, default: bool = False) -> bool:
    try:
        return _as_bool(current_app.config.get(name, default))
    except Exception:
        return default


def permissions_version() -> str:
    try:
        from app.cache import cache  # local import to avoid cycles

        val = cache.get(_PERM_VERSION_KEY)
        if val is None:
            val = 1
            cache.set(_PERM_VERSION_KEY, val, timeout=_PERM_VERSION_TTL)
        return str(val)
    except Exception:
        return "1"


def bump_permissions_version() -> str:
    try:
        from app.cache import cache

        current = cache.get(_PERM_VERSION_KEY)
        try:
            current_val = int(current) if current is not None else 0
        except Exception:
            current_val = 0
        new_val = current_val + 1
        cache.set(_PERM_VERSION_KEY, new_val, timeout=_PERM_VERSION_TTL)
        return str(new_val)
    except Exception:
        return "1"


@dataclass(frozen=True)
class AccessScope:
    is_admin: bool
    user_id: Optional[int]
    erp_user_id: Optional[str]
    allowed_erp_user_ids: list[str] = field(default_factory=list)
    allowed_customer_ids: list[str] = field(default_factory=list)
    allowed_region_ids: list[str] = field(default_factory=list)
    allowed_supplier_ids: list[str] = field(default_factory=list)
    scope_mode: str = "none"  # 'all' | 'list' | 'none'
    permissions_version: str = "1"
    scope_hash: str = ""

    def as_dict(self, *, include_allowed: bool = True) -> dict:
        payload = {
            "is_admin": bool(self.is_admin),
            "user_id": self.user_id,
            "erp_user_id": self.erp_user_id,
            "scope_mode": self.scope_mode,
            "permissions_version": self.permissions_version,
            "scope_hash": self.scope_hash,
            "allowed_count": len(self.allowed_erp_user_ids),
            "allowed_customer_count": len(self.allowed_customer_ids),
            "allowed_region_count": len(self.allowed_region_ids),
            "allowed_supplier_count": len(self.allowed_supplier_ids),
        }
        if include_allowed:
            payload["allowed_erp_user_ids"] = list(self.allowed_erp_user_ids)
            payload["allowed_customer_ids"] = list(self.allowed_customer_ids)
            payload["allowed_region_ids"] = list(self.allowed_region_ids)
            payload["allowed_supplier_ids"] = list(self.allowed_supplier_ids)
            # Backward-compatible aliases expected by existing services.
            payload["sales_rep_ids"] = list(self.allowed_erp_user_ids)
            payload["customer_ids"] = list(self.allowed_customer_ids)
            payload["region_ids"] = list(self.allowed_region_ids)
            payload["supplier_ids"] = list(self.allowed_supplier_ids)
        return payload


def _role_is_admin(user: object) -> bool:
    try:
        role = str(getattr(user, "role", "") or "").strip().lower()
    except Exception:
        role = ""
    if role == "admin":
        return True
    try:
        uid = getattr(user, "id", None)
        if uid:
            from app.auth.models import list_user_role_names  # type: ignore

            return "admin" in {str(r).strip().lower() for r in list_user_role_names(int(uid))}
    except Exception:
        pass
    return False


def _load_visibility(user_id: int) -> list[str]:
    try:
        from app.auth.models import SessionLocal, UserVisibilitySalesRep  # type: ignore

        with SessionLocal() as s:
            rows = (
                s.query(UserVisibilitySalesRep.visible_erp_user_id)
                .filter(UserVisibilitySalesRep.app_user_id == int(user_id))
                .all()
            )
        return _normalize_tokens([r[0] for r in rows])
    except Exception:
        return []


def _load_scope_rules(user_id: int) -> dict[str, list[str]]:
    try:
        from app.auth.models import list_user_scope_rules  # type: ignore

        payload = list_user_scope_rules(int(user_id))
        if isinstance(payload, dict):
            return {
                "rep": _normalize_tokens(payload.get("rep")),
                "customer": _normalize_tokens(payload.get("customer")),
                "region": _normalize_tokens(payload.get("region")),
                "supplier": _normalize_tokens(payload.get("supplier")),
            }
    except Exception:
        pass
    return {"rep": [], "customer": [], "region": [], "supplier": []}


def scope_for_user(user: object | None, *, use_cache: bool = True) -> AccessScope:
    if _cfg_flag("AUTHZ_DISABLED", False) or _cfg_flag("LOGIN_DISABLED", False):
        full_allowed: dict[str, list[str]] = {"rep": [], "customer": [], "region": [], "supplier": []}
        return AccessScope(
            is_admin=True,
            user_id=getattr(user, "id", None) if user is not None else None,
            erp_user_id=_normalize_token(
                getattr(user, "erp_user_id", None) or getattr(user, "sales_rep_id", None)
            ) if user is not None else None,
            allowed_erp_user_ids=[],
            allowed_customer_ids=[],
            allowed_region_ids=[],
            allowed_supplier_ids=[],
            scope_mode="all",
            permissions_version=permissions_version(),
            scope_hash=_hash_scope("all", full_allowed),
        )

    if user is None or not getattr(user, "is_authenticated", False):
        empty_allowed: dict[str, list[str]] = {"rep": [], "customer": [], "region": [], "supplier": []}
        return AccessScope(
            is_admin=False,
            user_id=None,
            erp_user_id=None,
            allowed_erp_user_ids=[],
            allowed_customer_ids=[],
            allowed_region_ids=[],
            allowed_supplier_ids=[],
            scope_mode="none",
            permissions_version=permissions_version(),
            scope_hash=_hash_scope("none", empty_allowed),
        )

    is_admin = _role_is_admin(user)
    user_id = getattr(user, "id", None)
    erp_user_id = _normalize_token(getattr(user, "erp_user_id", None) or getattr(user, "sales_rep_id", None))
    version = permissions_version()

    if is_admin:
        full_allowed: dict[str, list[str]] = {"rep": [], "customer": [], "region": [], "supplier": []}
        return AccessScope(
            is_admin=True,
            user_id=user_id,
            erp_user_id=erp_user_id,
            allowed_erp_user_ids=[],
            allowed_customer_ids=[],
            allowed_region_ids=[],
            allowed_supplier_ids=[],
            scope_mode="all",
            permissions_version=version,
            scope_hash=_hash_scope("all", full_allowed),
        )

    allowed: list[str] = []
    allowed_customers: list[str] = []
    allowed_regions: list[str] = []
    allowed_suppliers: list[str] = []
    if user_id is not None:
        rules = _load_scope_rules(int(user_id))
        allowed_customers = _normalize_tokens(rules.get("customer"))
        allowed_regions = _normalize_tokens(rules.get("region"))
        allowed_suppliers = _normalize_tokens(rules.get("supplier"))
        allowed = _normalize_tokens(rules.get("rep"))
        has_explicit_nonrep_scope = bool(allowed_customers or allowed_regions or allowed_suppliers)
        if use_cache:
            try:
                from app.cache import cache

                cache_key = f"rbac.scope:{user_id}:{version}"
                cached = cache.get(cache_key)
                if isinstance(cached, list):
                    cached_reps = _normalize_tokens(cached)
                elif cached is not None:
                    cached_reps = _normalize_tokens(cached if isinstance(cached, (list, tuple)) else [cached])
                else:
                    cached_reps = []
                if cached_reps:
                    allowed = cached_reps
                elif not allowed:
                    allowed = _load_visibility(int(user_id))
                    if not allowed and erp_user_id and not has_explicit_nonrep_scope:
                        allowed = [erp_user_id]
                    cache.set(cache_key, allowed, timeout=_PERM_VERSION_TTL)
            except Exception:
                if not allowed:
                    allowed = _load_visibility(int(user_id))
                    if not allowed and erp_user_id and not has_explicit_nonrep_scope:
                        allowed = [erp_user_id]
        else:
            if not allowed:
                allowed = _load_visibility(int(user_id))
                if not allowed and erp_user_id and not has_explicit_nonrep_scope:
                    allowed = [erp_user_id]

    allowed_map: dict[str, list[str]] = {
        "rep": list(allowed),
        "customer": list(allowed_customers),
        "region": list(allowed_regions),
        "supplier": list(allowed_suppliers),
    }
    has_all_rep_scope = any(str(v).strip().lower() in _ALL_SCOPE_TOKENS for v in (allowed or []))
    if has_all_rep_scope:
        allowed = []
        allowed_customers = []
        allowed_regions = []
        allowed_suppliers = []
        allowed_map = {"rep": [], "customer": [], "region": [], "supplier": []}
    scope_mode = "all" if has_all_rep_scope else ("list" if any(bool(values) for values in allowed_map.values()) else "none")
    return AccessScope(
        is_admin=False,
        user_id=user_id,
        erp_user_id=erp_user_id,
        allowed_erp_user_ids=allowed,
        allowed_customer_ids=allowed_customers,
        allowed_region_ids=allowed_regions,
        allowed_supplier_ids=allowed_suppliers,
        scope_mode=scope_mode,
        permissions_version=version,
        scope_hash=_hash_scope(scope_mode, allowed_map),
    )


def get_current_scope(*, use_cache: bool = True) -> AccessScope:
    try:
        from flask_login import current_user  # type: ignore

        return scope_for_user(current_user, use_cache=use_cache)
    except Exception:
        return scope_for_user(None, use_cache=use_cache)


def require_login(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if _cfg_flag("LOGIN_DISABLED", False) or _cfg_flag("AUTHZ_DISABLED", False):
            return fn(*args, **kwargs)
        try:
            from flask_login import current_user  # type: ignore

            if current_user.is_authenticated:
                return fn(*args, **kwargs)
        except Exception:
            pass
        abort(401, description="Authentication required")

    return wrapper


def require_admin(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            from flask_login import current_user  # type: ignore
            from app.core.rbac import has_permission, log_rbac_decision  # type: ignore

            if _cfg_flag("LOGIN_DISABLED", False) or _cfg_flag("AUTHZ_DISABLED", False):
                log_rbac_decision(
                    allowed=True,
                    reason="authz_bypass",
                    required_roles=("admin",),
                    required_any_perms=("admin.portal.view",),
                    user=current_user,
                )
                return fn(*args, **kwargs)
            if not getattr(current_user, "is_authenticated", False):
                log_rbac_decision(
                    allowed=False,
                    reason="authentication_required",
                    required_roles=("admin",),
                    required_any_perms=("admin.portal.view",),
                    user=current_user,
                )
                abort(401, description="Authentication required")
            allow = bool(_role_is_admin(current_user) or has_permission("admin.portal.view"))
            log_rbac_decision(
                allowed=allow,
                reason="authorization_granted" if allow else "missing_admin_access",
                required_roles=("admin",),
                required_any_perms=("admin.portal.view",),
                user=current_user,
            )
            if allow:
                return fn(*args, **kwargs)
        except Exception:
            pass
        abort(403, description="Admin access required")

    return wrapper


def enforce_entity_access(entity_type: str, entity_id: str, scope: Optional[AccessScope] = None) -> None:
    """Abort if a non-admin attempts to access an entity outside their scope."""
    if _cfg_flag("LOGIN_DISABLED", False) or _cfg_flag("AUTHZ_DISABLED", False):
        return
    scope_obj = scope or get_current_scope(use_cache=True)
    if scope_obj.scope_mode == "all" or scope_obj.is_admin:
        return
    if scope_obj.scope_mode == "none":
        abort(403, description="Access not configured")

    ent = (entity_type or "").strip().lower()
    allowed_set = set(scope_obj.allowed_erp_user_ids or [])
    allowed_customers = {str(v).strip().lower() for v in (scope_obj.allowed_customer_ids or []) if str(v).strip()}
    allowed_regions = {str(v).strip().lower() for v in (scope_obj.allowed_region_ids or []) if str(v).strip()}
    allowed_suppliers = {str(v).strip().lower() for v in (scope_obj.allowed_supplier_ids or []) if str(v).strip()}

    if ent in {"salesrep", "salesreps", "sales_rep", "rep"}:
        if str(entity_id or "").strip().lower() not in allowed_set:
            abort(403, description="Entity not in scope")
        return
    if ent in {"customer", "customers"} and allowed_customers:
        if str(entity_id or "").strip().lower() not in allowed_customers:
            abort(403, description="Entity not in scope")
        return
    if ent in {"region", "regions"} and allowed_regions:
        if str(entity_id or "").strip().lower() not in allowed_regions:
            abort(403, description="Entity not in scope")
        return
    if ent in {"supplier", "suppliers"} and allowed_suppliers:
        if str(entity_id or "").strip().lower() not in allowed_suppliers:
            abort(403, description="Entity not in scope")
        return

    # For other entities, confirm existence within scope using DuckDB.
    try:
        from app.services import fact_store  # type: ignore

        cols = fact_store.list_columns()
        if not cols:
            abort(404, description="Entity not found")
        mapping = {
            "customers": ("CustomerId", "CustomerName", "Customer"),
            "customer": ("CustomerId", "CustomerName", "Customer"),
            "products": ("ProductId", "SKU", "ProductName", "Product"),
            "product": ("ProductId", "SKU", "ProductName", "Product"),
            "regions": ("RegionName", "RegionId", "Region"),
            "region": ("RegionName", "RegionId", "Region"),
            "suppliers": ("SupplierId", "SupplierName", "Supplier"),
            "supplier": ("SupplierId", "SupplierName", "Supplier"),
        }
        candidates = mapping.get(ent, ())
        col = fact_store.choose_column(candidates, cols)
        if not col:
            abort(404, description="Entity not found")
        scope_clause, scope_params = fact_store.build_scope_clause(scope_obj.as_dict(), cols)
        where_sql = f"({scope_clause}) AND {fact_store.quote_identifier(col)} = ?"
        params = list(scope_params) + [entity_id]
        conn = fact_store.get_conn()
        row = conn.execute(f"SELECT 1 FROM fact WHERE {where_sql} LIMIT 1", params).fetchone()
        if not row:
            abort(404, description="Entity not found")
    except Exception:
        # Fail closed on unexpected errors
        abort(403, description="Entity not in scope")
