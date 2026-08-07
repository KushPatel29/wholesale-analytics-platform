from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Dict

from app.core.cache_manager import TTLValueCache
from app.services import filters_service
from app.services.filters import bind_filter_cache_key

# Default TTLs (seconds)
BUNDLE_TTL_SECONDS = int(os.getenv("BUNDLE_TTL_SECONDS", "1060"))

_CACHE = TTLValueCache(maxsize=128)
_LOCK = threading.RLock()
_INFLIGHT: Dict[str, Future] = {}


def _hash_payload(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_key(endpoint: str, filters: Any, scope: Dict[str, Any], dataset_version: str, extras: Dict[str, Any]) -> str:
    payload = {
        "endpoint": endpoint,
        "filters": json.loads(filters_service.canonical_json(filters)),
        "filters_hash": filters_service.filters_hash(filters),
        "scope": {
            "role": scope.get("role"),
            "user_id": scope.get("user_id"),
            "scope_mode": scope.get("scope_mode"),
            "scope_hash": scope.get("scope_hash"),
            "permissions_version": scope.get("permissions_version"),
        },
        "dataset_version": dataset_version,
        "extras": extras or {},
    }
    return _hash_payload(payload)


def cached_bundle(
    *,
    endpoint: str,
    filters: Any,
    scope: Dict[str, Any],
    dataset_version: str,
    extras: Dict[str, Any],
    ttl_seconds: int | None = None,
    builder: Callable[[], Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Cache + single-flight wrapper for bundle payloads.
    Cache key includes endpoint, canonical filters, RBAC scope, dataset_version, and pagination/sort extras.
    """
    key = _cache_key(endpoint, filters, scope, dataset_version, extras)
    bind_filter_cache_key(key)
    ttl = max(60, int(ttl_seconds or BUNDLE_TTL_SECONDS))

    def _build() -> Dict[str, Any]:
        payload = builder()
        if isinstance(payload, dict):
            payload.setdefault("meta", {})
            payload["meta"]["cache_built_at"] = int(time.time())
            payload["meta"]["dataset_version"] = dataset_version
            payload["meta"]["cached"] = False
        return payload

    result, hit = _CACHE.get_or_compute(key, ttl, _build)
    payload = copy.deepcopy(result)
    if isinstance(payload, dict):
        payload.setdefault("meta", {})
        payload["meta"]["cached"] = bool(hit)
        payload["meta"]["dataset_version"] = dataset_version
        payload["meta"]["cache_key"] = key
        payload["meta"]["cache_ttl"] = ttl
        built_at = payload["meta"].get("cache_built_at")
        if built_at is not None:
            try:
                payload["meta"]["cache_age_seconds"] = max(0, int(time.time()) - int(built_at))
            except Exception:
                payload["meta"]["cache_age_seconds"] = 0
    return payload
