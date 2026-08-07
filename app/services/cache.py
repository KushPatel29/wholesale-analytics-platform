from __future__ import annotations

from typing import Any, Mapping, Optional
import os

from app.services.filters import FilterParams, cache_key_from_filters
from app.services import fact_store

try:  # Local import; data_loader lives at project root
    import data_loader
except ModuleNotFoundError:  # pragma: no cover - during docs build
    data_loader = None  # type: ignore[assignment]


_CACHE_CODE_SALT = os.getenv("CACHE_CODE_SALT", "20251104_default_shipping_fallback")

_EMPTY_FILTERS = FilterParams(
    start=None,
    end=None,
    regions=tuple(),
    methods=tuple(),
    customers=tuple(),
)


def cache_key(
    filters: Optional[FilterParams],
    extras: Mapping[str, Any] | None = None,
) -> str:
    """Return a stable cache key that automatically upgrades with data version."""

    params = filters or _EMPTY_FILTERS
    scoped_extras: dict[str, Any] = dict(extras or {})
    try:
        from flask import has_request_context

        if has_request_context():
            from flask_login import current_user  # type: ignore
            from app.core.access_policy import scope_for_user  # type: ignore

            scope = scope_for_user(current_user, use_cache=True)
            scoped_extras.setdefault("user_id", current_user.get_id() if hasattr(current_user, "get_id") else getattr(current_user, "id", None))
            scoped_extras.setdefault("scope_hash", getattr(scope, "scope_hash", None))
            scoped_extras.setdefault("scope_mode", getattr(scope, "scope_mode", None))
    except Exception:
        pass
    base = cache_key_from_filters(params, scoped_extras)
    version = "0"
    try:
        version = fact_store.cache_buster()
    except Exception:
        if data_loader is not None:
            try:
                version = data_loader.current_version()
            except Exception:  # pragma: no cover - manifest read failures
                version = "0"
    if _CACHE_CODE_SALT:
        base = f"{_CACHE_CODE_SALT}:{base}"
    return f"v{version}:{base}"
