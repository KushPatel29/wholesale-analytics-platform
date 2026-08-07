from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping

from app.core.json_sanitizer import sanitize_for_json

logger = logging.getLogger(__name__)


@dataclass
class BundleContract:
    """Simple contract describing required bundle payload keys."""

    name: str
    required: List[str] = field(default_factory=list)
    optional: List[str] = field(default_factory=list)

    def missing_keys(self, payload: Mapping[str, Any]) -> List[str]:
        return [key for key in self.required if key not in payload]


PAGE_CONTRACTS: Dict[str, BundleContract] = {
    "overview": BundleContract("overview", required=["kpis", "series", "mix", "pareto", "top", "health", "meta"]),
    "products": BundleContract("products", required=["kpis", "charts", "table", "meta"]),
    "customers": BundleContract("customers", required=["kpis", "trend", "table", "meta"]),
    "regions": BundleContract("regions", required=["kpis", "trend", "table", "meta"]),
    "suppliers": BundleContract("suppliers", required=["kpis", "trend", "table", "meta"]),
    "salesreps": BundleContract("salesreps", required=["kpis", "trend", "table", "meta"]),
}

DRILLDOWN_CONTRACTS: Dict[str, BundleContract] = {
    "products": BundleContract("products_drilldown", required=["meta", "kpis", "trend", "table"]),
    "customers": BundleContract("customers_drilldown", required=["meta", "kpis", "trend", "table"]),
    "suppliers": BundleContract("suppliers_drilldown", required=["meta", "kpis", "trend", "table"]),
    "regions": BundleContract("regions_drilldown", required=["meta", "kpis", "trend", "table"]),
    "salesreps": BundleContract("salesreps_drilldown", required=["meta", "kpis", "trend", "table"]),
}


def _error_payload(page: str, missing: Iterable[str], meta: Dict[str, Any] | None = None) -> Dict[str, Any]:
    meta = meta or {}
    meta.setdefault("cached", False)
    meta.setdefault("page_id", page)
    return {
        "error": {"message": f"Bundle missing required keys: {', '.join(missing)}"},
        "meta": meta,
    }


def validate_bundle(page: str, payload: Dict[str, Any], *, drilldown: bool = False) -> Dict[str, Any]:
    """
    Validate payload against the contract for the given page/entity.
    Returns either the original payload or a controlled error payload when keys are missing.
    """
    contract_map = DRILLDOWN_CONTRACTS if drilldown else PAGE_CONTRACTS
    contract = contract_map.get(page)
    if contract is None or not isinstance(payload, dict):
        return payload

    missing = contract.missing_keys(payload)
    if missing:
        try:
            logger.error("bundle.contract_missing", extra={"page": page, "missing": missing})
        except Exception:
            pass
        meta = payload.get("meta") if isinstance(payload, dict) else {}
        meta = meta if isinstance(meta, dict) else {}
        return _error_payload(contract.name, missing, meta=meta)
    return payload


def to_json_safe(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a JSON-safe deep copy of the payload suitable for serialization."""
    return sanitize_for_json(payload)


def payload_size(payload: Dict[str, Any]) -> int:
    """Return payload size in bytes once serialized with compact separators."""
    try:
        return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    except Exception:
        return 0
