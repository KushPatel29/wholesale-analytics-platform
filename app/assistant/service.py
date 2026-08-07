from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, replace
from threading import RLock
from typing import Any, Dict, List, Mapping, Sequence

from cachetools import TTLCache
from flask import current_app, request
from flask_login import current_user

from app.core import rbac
from app.core.audit import log_audit
from app.core.sensitive_data import sensitive_access_flags

from . import memory
from .context import (
    build_page_context,
    canonical_page,
    context_blob,
    has_any_access,
    module_access_map,
    resolve_filters_from_payload,
)
from .export_job_store import export_job_payload, get_export_job
from .provider import ProviderConfig, build_provider
from .tools import ToolContext, execute_tool


_NARRATIVE_CACHE = TTLCache(maxsize=256, ttl=75)
_NARRATIVE_LOCK = RLock()


@dataclass
class AssistantRuntimeConfig:
    enabled: bool
    provider: str
    model: str
    model_path: str
    base_url: str
    timeout_seconds: int
    context_window: int
    max_tokens: int
    threads: int
    batch_size: int
    gpu_layers: int
    max_tool_calls: int
    enable_rag: bool
    enable_audit: bool
    enable_page_context: bool
    enable_suggested_prompts: bool
    enable_glossary: bool
    require_tool_backing_for_metrics: bool
    enable_proactive_insights: bool
    enable_workflow_assist: bool
    enable_voice_ready: bool
    max_proactive_items: int


@dataclass
class FollowupResolution:
    resolved_message: str
    is_followup: bool
    force_module: str | None = None
    request_compare_periods: bool = False
    request_simpler: bool = False
    request_more_detail: bool = False
    request_executive: bool = False
    request_voice: bool = False
    response_mode: str = "standard"
    detail_level: str = "standard"


@dataclass
class SemanticSlots:
    intent_type: str = "live_performance"
    query_shape: str = "single_hop"
    metric: str = "revenue"
    ranking_direction: str = "top"
    limit_n: int = 5
    primary_entity_type: str = ""
    secondary_entity_type: str = ""
    parent_entity_type: str = ""
    child_entity_type: str = ""
    parent_limit_n: int = 0
    child_limit_n: int = 0
    group_by_dimension: str = ""
    selected_entity_name: str = ""
    relationship_entity_type: str = ""
    relationship_entity_name: str = ""
    comparison_target: str = ""
    time_window: str = ""
    use_current_page_context: bool = True
    use_full_history: bool = False
    use_current_filters: bool = True
    export_requested: bool = False
    export_intent_type: str = ""
    output_format: str = "xlsx"
    include_chart: bool = False
    chart_image_only: bool = False
    chart_image_format: str = ""
    include_summary_sheet: bool = True
    include_metadata_sheet: bool = True
    include_all_allowed_columns: bool = False
    async_export: bool = False
    answer_mode: str = "standard"
    risk_focus: bool = False
    trust_focus: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "intent_type": self.intent_type,
            "query_shape": self.query_shape,
            "metric": self.metric,
            "ranking_direction": self.ranking_direction,
            "limit_n": int(self.limit_n),
            "primary_entity_type": self.primary_entity_type,
            "secondary_entity_type": self.secondary_entity_type,
            "parent_entity_type": self.parent_entity_type,
            "child_entity_type": self.child_entity_type,
            "parent_limit_n": int(self.parent_limit_n),
            "child_limit_n": int(self.child_limit_n),
            "group_by_dimension": self.group_by_dimension,
            "selected_entity_name": self.selected_entity_name,
            "relationship_entity_type": self.relationship_entity_type,
            "relationship_entity_name": self.relationship_entity_name,
            "comparison_target": self.comparison_target,
            "time_window": self.time_window,
            "use_current_page_context": bool(self.use_current_page_context),
            "use_full_history": bool(self.use_full_history),
            "use_current_filters": bool(self.use_current_filters),
            "export_requested": bool(self.export_requested),
            "export_intent_type": self.export_intent_type,
            "output_format": self.output_format,
            "include_chart": bool(self.include_chart),
            "chart_image_only": bool(self.chart_image_only),
            "chart_image_format": self.chart_image_format,
            "include_summary_sheet": bool(self.include_summary_sheet),
            "include_metadata_sheet": bool(self.include_metadata_sheet),
            "include_all_allowed_columns": bool(self.include_all_allowed_columns),
            "async_export": bool(self.async_export),
            "answer_mode": self.answer_mode,
            "risk_focus": bool(self.risk_focus),
            "trust_focus": bool(self.trust_focus),
        }


_METRIC_ALIASES: Dict[str, tuple[str, ...]] = {
    "revenue": ("revenue", "sales", "net sales"),
    "profit": ("profit", "gross profit", "gp"),
    "margin_pct": ("margin", "margin %", "margin pct", "margin percentage"),
    "orders": ("orders", "order count"),
    "quantity": ("quantity", "qty", "units", "volume"),
    "asp": ("asp", "average selling price", "avg selling price"),
    "aov": ("aov", "average order value", "avg order value"),
    "shipped_weight": ("shipped weight", "weight", "lbs", "pounds", "lb"),
    "product_count": ("product count", "sku count", "skus"),
}

_DIMENSION_ALIASES: Dict[str, tuple[str, ...]] = {
    "regions": ("region", "regions", "territory", "territories"),
    "customers": ("customer", "customers", "account", "accounts"),
    "products": ("product", "products", "sku", "skus"),
    "suppliers": ("supplier", "suppliers", "vendor", "vendors"),
    "salesreps": ("sales rep", "sales reps", "salesrep", "salesreps", "rep", "reps", "portfolio"),
    "returns": ("return", "returns"),
}


def _module_token(module: str) -> str:
    token = str(module or "").strip().lower()
    mapping = {
        "sales_rep": "salesreps",
        "sales_reps": "salesreps",
        "salesrep": "salesreps",
        "sales rep": "salesreps",
        "sales reps": "salesreps",
        "rep": "salesreps",
        "reps": "salesreps",
        "customer": "customers",
        "customers": "customers",
        "account": "customers",
        "accounts": "customers",
        "product": "products",
        "products": "products",
        "sku": "products",
        "skus": "products",
        "region": "regions",
        "regions": "regions",
        "supplier": "suppliers",
        "suppliers": "suppliers",
        "vendor": "suppliers",
        "vendors": "suppliers",
    }
    return mapping.get(token, token or "overview")


def _runtime_config() -> AssistantRuntimeConfig:
    cfg = current_app.config
    return AssistantRuntimeConfig(
        enabled=bool(cfg.get("AI_ENABLED", False)),
        provider=str(cfg.get("AI_PROVIDER", "ollama") or "ollama").strip().lower(),
        model=str(cfg.get("AI_MODEL", "llama3.1") or "llama3.1").strip(),
        model_path=str(cfg.get("AI_MODEL_PATH", "") or "").strip(),
        base_url=str(cfg.get("AI_BASE_URL", "http://127.0.0.1:11434") or "http://127.0.0.1:11434").strip(),
        timeout_seconds=max(3, int(cfg.get("AI_TIMEOUT_SECONDS", 25) or 25)),
        context_window=max(1024, int(cfg.get("AI_CONTEXT_WINDOW", 4096) or 4096)),
        max_tokens=max(96, int(cfg.get("AI_MAX_TOKENS", 384) or 384)),
        threads=max(1, int(cfg.get("AI_THREADS", 2) or 2)),
        batch_size=max(64, int(cfg.get("AI_BATCH_SIZE", 256) or 256)),
        gpu_layers=max(0, int(cfg.get("AI_GPU_LAYERS", 0) or 0)),
        max_tool_calls=max(2, int(cfg.get("AI_MAX_TOOL_CALLS", 8) or 8)),
        enable_rag=bool(cfg.get("AI_ENABLE_RAG", True)),
        enable_audit=bool(cfg.get("AI_ENABLE_AUDIT", True)),
        enable_page_context=bool(cfg.get("AI_ENABLE_PAGE_CONTEXT", True)),
        enable_suggested_prompts=bool(cfg.get("AI_ENABLE_SUGGESTED_PROMPTS", True)),
        enable_glossary=bool(cfg.get("AI_ENABLE_GLOSSARY", True)),
        require_tool_backing_for_metrics=bool(cfg.get("AI_REQUIRE_TOOL_BACKING_FOR_METRICS", True)),
        enable_proactive_insights=bool(cfg.get("AI_ENABLE_PROACTIVE_INSIGHTS", True)),
        enable_workflow_assist=bool(cfg.get("AI_ENABLE_WORKFLOW_ASSIST", True)),
        enable_voice_ready=bool(cfg.get("AI_ENABLE_VOICE_READY", True)),
        max_proactive_items=max(2, int(cfg.get("AI_MAX_PROACTIVE_ITEMS", 6) or 6)),
    )


def assistant_enabled() -> bool:
    return _runtime_config().enabled


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except Exception:
        return str(value)


def _narrative_cache_key(*, user_id: Any, page: str, module: str, kind: str, payload: Mapping[str, Any] | None = None) -> str:
    raw = {
        "u": str(user_id or "anon"),
        "p": str(page or "overview"),
        "m": str(module or "overview"),
        "k": str(kind or ""),
        "d": dict(payload or {}),
    }
    token = hashlib.sha1(_stable_json(raw).encode("utf-8")).hexdigest()
    return f"assistant:{token}"


_HELPER_TITLES: set[str] = {
    "Current Page Context",
    "Current Page Visible State",
    "Page Bundle",
    "User Scope And Permissions",
    "Recommended Follow-up Questions",
    "Related Investigations",
}

_MODULE_KEYWORDS: Dict[str, tuple[str, ...]] = {
    "overview": ("overview", "business", "company", "performance", "leadership"),
    "customers": ("customer", "account", "retention", "churn", "clv", "rfm"),
    "products": ("product", "sku", "pricing", "mix", "velocity"),
    "regions": ("region", "regional", "territory", "geography"),
    "suppliers": ("supplier", "vendor", "procurement", "sourcing"),
    "salesreps": ("sales rep", "salesrep", "rep portfolio", "portfolio"),
    "returns": ("returns", "rma", "approval", "refund", "workflow", "return reasons"),
}


def _extract_metric_hint(message: str) -> str:
    text = message.lower()
    token_map = {
        "aov": "aov",
        "asp": "asp",
        "hhi": "hhi",
        "margin dispersion": "margin_dispersion",
        "price volume mix": "price_volume_mix",
        "returns workflow": "returns_workflow",
        "cost coverage": "cost_coverage",
        "pack coverage": "pack_coverage",
        "profit per pound": "profit_per_lb",
        "profit": "profit",
        "margin": "margin_pct",
        "revenue": "revenue",
    }
    for key, value in token_map.items():
        if key in text:
            return value
    return message


def _extract_schedule_id(message: str) -> str:
    text = str(message or "").strip().lower()
    match = re.search(r"(sch_[a-z0-9]{8,})", text)
    if not match:
        return ""
    return str(match.group(1)).strip()


def _looks_definition_question(message: str) -> bool:
    text = message.lower()
    hard_tokens = (
        "what is",
        "define",
        "meaning",
        "definition",
        "formula",
        "calculated",
        "calculate",
        "how do you calculate",
        "how is this calculated",
    )
    if any(token in text for token in hard_tokens if token != "what is"):
        return True
    if "what is" not in text:
        return False
    metric_terms = (
        "aov",
        "asp",
        "hhi",
        "margin dispersion",
        "cost coverage",
        "pack coverage",
        "profit per",
        "revenue growth",
        "mix growth",
        "returns workflow",
        "churn",
        "retention",
        "forecast",
        "margin",
        "profit",
    )
    anti_terms = (
        "happening",
        "changed",
        "change",
        "down",
        "up",
        "risk",
        "driver",
        "mover",
        "decline",
        "decliner",
        "gainer",
        "performing",
        "performance",
    )
    if any(token in text for token in anti_terms):
        return False
    return any(token in text for token in metric_terms)


def _looks_page_help_question(message: str) -> bool:
    text = message.lower()
    tokens = (
        "what does this section mean",
        "how should i use this page",
        "how do i use this page",
        "page help",
        "what should i do on this page",
        "which module should i open next",
    )
    return any(token in text for token in tokens)


def _looks_forecast_question(message: str) -> bool:
    text = str(message or "").lower()
    tokens = (
        "forecast",
        "outlook",
        "projection",
        "projected",
        "why is forecast unavailable",
        "how should i read this forecast",
    )
    return any(token in text for token in tokens)


def _looks_trust_question(message: str) -> bool:
    text = str(message or "").lower()
    tokens = (
        "can i trust",
        "trust these numbers",
        "data quality",
        "data health",
        "cost coverage",
        "pack coverage",
        "coverage",
        "freshness",
        "confidence",
    )
    return any(token in text for token in tokens)


def _looks_driver_question(message: str) -> bool:
    text = str(message or "").lower()
    explicit_tokens = (
        "what drove",
        "drove this",
        "drove the",
        "drivers behind",
        "contributed most",
        "driver",
        "mover",
        "gainer",
        "decliner",
        "price volume mix",
        "pvm",
    )
    if any(token in text for token in explicit_tokens):
        return True
    if "why is" in text:
        metric_tokens = ("revenue", "profit", "margin", "volume", "mix", "performance", "decline", "increase", "down", "up")
        return any(token in text for token in metric_tokens)
    return False


def _looks_risk_question(message: str) -> bool:
    text = str(message or "").lower()
    tokens = (
        "risk",
        "watchout",
        "at risk",
        "margin risk",
        "top risks",
        "urgent",
        "highest priority risk",
    )
    return any(token in text for token in tokens)


def _looks_summary_question(message: str) -> bool:
    text = str(message or "").lower()
    tokens = (
        "summarize this page",
        "summarize this customer",
        "summarize this product",
        "summarize this region",
        "summarize this supplier",
        "summarize this rep",
        "summarize this returns",
        "give me a summary",
        "summary of this",
        "what is happening",
        "what stands out",
        "how is the business performing",
        "page summary",
    )
    return any(token in text for token in tokens)


def _is_pronoun_followup_reference(message: str) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    if text in {"that", "those", "this", "why", "why?"}:
        return True
    if len(text) > 80:
        return False
    if any(
        re.search(pattern, text)
        for pattern in (
            r"\bexport that\b",
            r"\bcompare that\b",
            r"\bshow full history(?: instead)?\b",
            r"\buse full history(?: instead)?\b",
            r"\bthat risk\b",
            r"\bthose products\b",
            r"\bthis change\b",
            r"\bthat change\b",
            r"\bthat result\b",
            r"\bthis (page|customer|product|region|supplier|rep|portfolio|risk|issue)\b",
        )
    ):
        return True
    return False


def _has_explicit_history_request(message: str) -> bool:
    text = str(message or "").strip().lower()
    tokens = (
        "full history",
        "history",
        "historical",
        "over time",
        "when did",
        "timeline",
        "evolution",
        "changed over time",
        "trend over time",
    )
    return any(token in text for token in tokens)


def _has_explicit_comparison_request(message: str) -> bool:
    text = str(message or "").strip().lower()
    if "compare" in text:
        return True
    tokens = (
        " vs ",
        " versus ",
        "compared with",
        "compare with",
        "against last",
        "relative to",
    )
    return any(token in text for token in tokens)


def _looks_analyst_detail_question(message: str) -> bool:
    text = str(message or "").lower()
    tokens = (
        "analyst mode",
        "show detailed reasoning",
        "more detail",
        "show detail",
        "underlying business drivers",
        "detailed analyst",
    )
    return any(token in text for token in tokens)


def _looks_metric_question(message: str) -> bool:
    text = str(message or "").strip().lower()
    tokens = (
        "revenue",
        "profit",
        "margin",
        "cost",
        "kpi",
        "trend",
        "driver",
        "mover",
        "risk",
        "forecast",
        "overview",
        "business performance",
        "customer",
        "product",
        "region",
        "supplier",
        "sales rep",
        "returns",
        "aov",
        "asp",
        "hhi",
    )
    return any(token in text for token in tokens)


def _extract_metric_slot(text: str) -> str:
    lowered = str(text or "").strip().lower()
    for metric, aliases in _METRIC_ALIASES.items():
        if any(alias in lowered for alias in aliases):
            return metric
    return "revenue"


def _extract_dimension_slot(text: str) -> str:
    lowered = str(text or "").strip().lower()
    for dimension, aliases in _DIMENSION_ALIASES.items():
        if any(alias in lowered for alias in aliases):
            return dimension
    return ""


def _extract_limit_slot(text: str, default_limit: int = 5) -> int:
    lowered = str(text or "").strip().lower()
    match = re.search(r"\b(?:top|bottom)\s+(\d{1,2})\b", lowered)
    if not match:
        match = re.search(r"\b(?:top|bottom)\s+([a-z]+)\b", lowered)
        word_map = {
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
            "twenty": 20,
        }
        if match:
            token = str(match.group(1) or "").strip().lower()
            if token in word_map:
                return max(1, min(50, int(word_map[token])))
    if match:
        try:
            return max(1, min(50, int(match.group(1))))
        except Exception:
            pass
    return max(1, min(50, int(default_limit)))


def _extract_time_window_slot(text: str) -> str:
    lowered = str(text or "").strip().lower()
    match = re.search(r"\blast\s+(\d+)\s+(day|days|week|weeks|month|months|quarter|quarters|year|years)\b", lowered)
    if match:
        return f"last_{match.group(1)}_{match.group(2)}"
    if "this month" in lowered:
        return "this_month"
    if "this quarter" in lowered:
        return "this_quarter"
    if "this year" in lowered:
        return "this_year"
    if "last quarter" in lowered:
        return "last_quarter"
    if "last year" in lowered:
        return "last_year"
    if "full history" in lowered or "all time" in lowered:
        return "full_history"
    return ""


def _extract_nested_ranking_plan(text: str) -> Dict[str, Any]:
    lowered = str(text or "").strip().lower()
    pattern = re.compile(
        r"\b(?:top|bottom|highest|lowest|best|worst)\s*"
        r"(?P<parent_limit>\d+)?\s*"
        r"(?P<parent>regions?|customers?|accounts?|products?|skus?|suppliers?|vendors?|sales reps?|salesrep|salesreps|reps?)"
        r"\s+(?:and|with)\s+their\s+"
        r"(?:(?:top|bottom|highest|lowest|best|worst)\s*)?"
        r"(?P<child_limit>\d+)?\s*"
        r"(?P<child>regions?|customers?|accounts?|products?|skus?|suppliers?|vendors?|sales reps?|salesrep|salesreps|reps?)\b"
    )
    match = pattern.search(lowered)
    if not match:
        return {}
    parent = _module_token(match.group("parent") or "")
    child = _module_token(match.group("child") or "")
    if not parent or not child or parent == child:
        return {}
    try:
        parent_limit = max(1, min(10, int(match.group("parent_limit") or 5)))
    except Exception:
        parent_limit = 5
    try:
        child_limit = max(1, min(10, int(match.group("child_limit") or parent_limit)))
    except Exception:
        child_limit = parent_limit
    return {
        "parent_entity_type": parent,
        "child_entity_type": child,
        "parent_limit_n": parent_limit,
        "child_limit_n": child_limit,
    }


def _clean_entity_reference(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    cleaned = re.sub(
        r"\s+(?:and\s+their|with\s+their|with\s+top|for\s+the|in\s+the|over\s+time|over\s+the|using\s+the|using|within\s+the)\b.*$",
        "",
        raw,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"[?.!,;:]+$", "", cleaned).strip(" '\"")
    return cleaned


def _extract_relationship_filter_slot(text: str) -> Dict[str, str]:
    raw = str(text or "").strip()
    lowered = raw.lower()
    patterns = (
        (r"\bsold by\s+(?P<name>.+?)(?=\s+(?:in|over|with|using|and\b)|$)", "salesreps"),
        (r"\bfor\s+supplier\s+(?P<name>.+?)(?=\s+(?:in|over|with|and\b)|$)", "suppliers"),
        (r"\bfor\s+customer\s+(?P<name>.+?)(?=\s+(?:in|over|with|and\b)|$)", "customers"),
        (r"\bfor\s+region\s+(?P<name>.+?)(?=\s+(?:in|over|with|and\b)|$)", "regions"),
        (r"\bfor\s+sales rep\s+(?P<name>.+?)(?=\s+(?:in|over|with|and\b)|$)", "salesreps"),
        (r"\bfor\s+rep\s+(?P<name>.+?)(?=\s+(?:in|over|with|and\b)|$)", "salesreps"),
    )
    for pattern, entity_type in patterns:
        match = re.search(pattern, raw, flags=re.IGNORECASE)
        if not match:
            continue
        name = _clean_entity_reference(match.group("name") or "")
        if not name:
            continue
        if name.lower() in {
            "this customer",
            "this supplier",
            "this region",
            "this sales rep",
            "this rep",
            "current customer",
            "current supplier",
            "current region",
            "current sales rep",
            "current rep",
        }:
            continue
        return {"entity_type": entity_type, "entity_name": name}
    if "sold by" in lowered and "sales rep" not in lowered and "rep" not in lowered:
        tail = lowered.split("sold by", 1)[-1].strip()
        if tail:
            return {"entity_type": "salesreps", "entity_name": _clean_entity_reference(raw.split("sold by", 1)[-1])}
    return {}


def _extract_secondary_entity_slot(text: str) -> str:
    lowered = str(text or "").strip().lower()
    patterns = (
        (r"\bfor\s+(?:this\s+)?customer\b", "customers"),
        (r"\bfor\s+(?:this\s+)?product\b", "products"),
        (r"\bfor\s+(?:this\s+)?region\b", "regions"),
        (r"\bfor\s+(?:this\s+)?supplier\b", "suppliers"),
        (r"\bfor\s+(?:this\s+)?sales rep\b", "salesreps"),
        (r"\bfor\s+(?:this\s+)?rep\b", "salesreps"),
        (r"\bunder\s+(?:this\s+)?customer\b", "customers"),
        (r"\bunder\s+(?:this\s+)?supplier\b", "suppliers"),
    )
    for pattern, value in patterns:
        if re.search(pattern, lowered):
            return value
    return ""


def _extract_comparison_target_slot(text: str) -> str:
    lowered = str(text or "").strip().lower()
    if "last year" in lowered or "vs last year" in lowered or "with last year" in lowered:
        return "last_year"
    if "last quarter" in lowered or "vs last quarter" in lowered or "with last quarter" in lowered:
        return "last_quarter"
    if "prior period" in lowered or "previous period" in lowered:
        return "prior_period"
    if "month over month" in lowered or "mom" in lowered:
        return "mom"
    if "year over year" in lowered or "yoy" in lowered:
        return "yoy"
    return ""


def _extract_semantic_slots(
    message: str,
    *,
    module: str,
    page_state: Mapping[str, Any] | None,
    state: Mapping[str, Any] | None,
    followup: FollowupResolution,
) -> SemanticSlots:
    text = str(message or "").strip()
    lowered = text.lower()
    selected_entity = (
        (page_state or {}).get("selected_entity")
        if isinstance((page_state or {}).get("selected_entity"), Mapping)
        else {}
    )
    selected_type = str((selected_entity or {}).get("type") or "").strip().lower()
    selected_label = str((selected_entity or {}).get("label") or (selected_entity or {}).get("id") or "").strip()
    state_entities = (state or {}).get("last_entities") if isinstance((state or {}).get("last_entities"), Mapping) else {}
    state_entity_label = str((state_entities or {}).get("label") or (state_entities or {}).get("id") or "").strip()

    ranking_direction = "bottom" if any(token in lowered for token in ("bottom", "lowest", "worst")) else "top"
    export_requested = any(
        token in lowered
        for token in (
            "export",
            "excel",
            "workbook",
            "download",
            "csv",
            "create file",
            "create workbook",
            "create excel",
            "create report",
            "generate file",
            "generate report",
            "analysis pack",
            "leadership pack",
            "chart image",
            "graph image",
            "image export",
        )
    )
    metric = _extract_metric_slot(lowered)
    dimension = _extract_dimension_slot(lowered)
    secondary = _extract_secondary_entity_slot(lowered)
    nested_plan = _extract_nested_ranking_plan(lowered)
    relationship_filter = _extract_relationship_filter_slot(text)
    comparison_target = _extract_comparison_target_slot(lowered)
    time_window = _extract_time_window_slot(lowered)
    use_full_history = bool(time_window == "full_history" or any(token in lowered for token in ("full history", "all time")))
    limit_n = _extract_limit_slot(lowered, default_limit=5)
    include_chart = any(
        token in lowered
        for token in (
            "chart",
            "charts",
            "graph",
            "graphs",
            "trend line",
            "bar chart",
            "line chart",
            "chart sheet",
        )
    )
    chart_image_only = any(
        token in lowered
        for token in (
            "chart image",
            "graph image",
            "image export",
            "png chart",
            "svg chart",
            "as png",
            "as svg",
        )
    )
    if chart_image_only and not export_requested:
        export_requested = True
    chart_image_format = ""
    if "png" in lowered:
        chart_image_format = "png"
    elif "svg" in lowered:
        chart_image_format = "svg"
    include_all_allowed_columns = any(
        token in lowered
        for token in (
            "all columns",
            "all available columns",
            "all visible columns",
            "every column",
            "all details",
        )
    )
    include_summary_sheet = not any(token in lowered for token in ("no summary", "without summary"))
    include_metadata_sheet = not any(token in lowered for token in ("no metadata", "without metadata"))
    output_format = "xlsx"
    if "csv" in lowered:
        output_format = "csv"
    elif any(token in lowered for token in ("excel", "xlsx", "workbook")):
        output_format = "xlsx"
    async_export = any(
        token in lowered
        for token in (
            "async",
            "asynchronous",
            "background",
            "queue this",
            "run in background",
        )
    )

    export_intent_type = ""
    if export_requested:
        if any(token in lowered for token in ("chart only", "graph only", "only chart", "only graph")):
            export_intent_type = "export_chart_only"
        elif include_chart:
            export_intent_type = "export_chart_plus_data"
        elif nested_plan:
            export_intent_type = "export_hierarchical_analysis"
        elif _has_explicit_comparison_request(lowered):
            export_intent_type = "export_comparison"
        elif _has_explicit_history_request(lowered):
            export_intent_type = "export_entity_history"
        elif re.search(r"\b(top|bottom|highest|lowest|best|worst)\b", lowered):
            export_intent_type = "export_ranked_list"
        elif re.search(r"\bby\s+(region|regions|customer|customers|product|products|supplier|suppliers|sales rep|sales reps|rep|reps|month|months)\b", lowered):
            export_intent_type = "export_grouped_metric"
        elif any(token in lowered for token in ("leadership", "executive", "manager report", "leadership pack")):
            export_intent_type = "export_leadership_pack"
        elif any(token in lowered for token in ("analysis pack", "bundle", "investigation workbook")):
            export_intent_type = "export_custom_analysis_pack"
        else:
            export_intent_type = "export_table"
    explicit_page_context = any(
        token in lowered
        for token in (
            "this page",
            "this customer",
            "this product",
            "this supplier",
            "this region",
            "this rep",
            "current page",
            "same filters",
        )
    )
    explicit_scope_widen = any(
        token in lowered
        for token in (
            "company-wide",
            "entire company",
            "across all data",
            "all customers",
            "all products",
            "all regions",
            "all suppliers",
            "all reps",
        )
    )
    module_context_available = module in _MODULE_KEYWORDS and module not in {"overview", "assistant"}
    entity_context_available = bool(selected_type or selected_label or state_entity_label)
    use_current_page_context = bool(
        explicit_page_context or ((module_context_available or entity_context_available) and not explicit_scope_widen)
    )
    use_current_filters = not any(token in lowered for token in ("all time", "company-wide", "entire company", "across all data"))
    risk_focus = _looks_risk_question(lowered)
    trust_focus = _looks_trust_question(lowered)

    intent_type = "live_performance"
    if _has_explicit_comparison_request(lowered):
        intent_type = "comparison"
    elif _has_explicit_history_request(lowered):
        intent_type = "history"
    elif re.search(r"\b(top|bottom|highest|lowest|best|worst)\b", lowered):
        intent_type = "ranking"
    elif re.search(r"\bby\s+(region|regions|customer|customers|product|products|supplier|suppliers|sales rep|sales reps|rep|reps)\b", lowered):
        intent_type = "grouped"
    elif _looks_definition_question(lowered):
        intent_type = "definition"
    elif _looks_page_help_question(lowered):
        intent_type = "help"
    if export_requested and intent_type in {"live_performance", "definition", "help"}:
        intent_type = "export"

    primary_entity = dimension or module
    if primary_entity == "assistant":
        primary_entity = "overview"
    if primary_entity in {"customer", "account"}:
        primary_entity = "customers"
    elif primary_entity in {"product", "sku"}:
        primary_entity = "products"
    elif primary_entity in {"region"}:
        primary_entity = "regions"
    elif primary_entity in {"supplier", "vendor"}:
        primary_entity = "suppliers"
    elif primary_entity in {"rep", "sales_rep"}:
        primary_entity = "salesreps"

    parent_entity_type = _module_token(str(nested_plan.get("parent_entity_type") or primary_entity))
    child_entity_type = _module_token(str(nested_plan.get("child_entity_type") or ""))
    parent_limit_n = int(nested_plan.get("parent_limit_n") or 0)
    child_limit_n = int(nested_plan.get("child_limit_n") or 0)

    relationship_entity_type = _module_token(str(relationship_filter.get("entity_type") or ""))
    relationship_entity_name = str(relationship_filter.get("entity_name") or "").strip()

    selected_name = ""
    if any(token in lowered for token in ("this customer", "this product", "this supplier", "this region", "this rep", "this portfolio")):
        selected_name = selected_label or state_entity_label
    if not selected_name and any(token in lowered for token in ("that", "those", "this")):
        selected_name = state_entity_label or selected_label

    if not secondary and selected_type in {"customer", "product", "region", "supplier", "salesrep", "sales_rep"}:
        if primary_entity in {"products", "customers", "regions", "suppliers", "salesreps"} and use_current_page_context:
            secondary = _module_token(selected_type)

    if nested_plan:
        primary_entity = parent_entity_type
        dimension = parent_entity_type
    if not relationship_entity_type and secondary and selected_name:
        relationship_entity_type = _module_token(secondary)
        relationship_entity_name = selected_name
    if not relationship_entity_type and selected_type in {"customer", "product", "region", "supplier", "salesrep", "sales_rep"}:
        selected_module = _module_token(selected_type)
        if use_current_page_context and selected_module and selected_module != primary_entity:
            relationship_entity_type = selected_module
            relationship_entity_name = selected_label or state_entity_label

    secondary_entity_type = _module_token(secondary) if secondary else ""
    if not secondary_entity_type and relationship_entity_type:
        secondary_entity_type = relationship_entity_type

    query_shape = "single_hop"
    if nested_plan:
        query_shape = "nested_ranking"
    elif intent_type == "ranking" and relationship_entity_type:
        query_shape = "filtered_ranking"
    elif intent_type == "grouped":
        query_shape = "grouped_metric"

    return SemanticSlots(
        intent_type=intent_type,
        query_shape=query_shape,
        metric=metric,
        ranking_direction=ranking_direction,
        limit_n=limit_n,
        primary_entity_type=_module_token(primary_entity),
        secondary_entity_type=secondary_entity_type,
        parent_entity_type=parent_entity_type if nested_plan else "",
        child_entity_type=child_entity_type,
        parent_limit_n=parent_limit_n,
        child_limit_n=child_limit_n,
        group_by_dimension=_module_token(dimension) if dimension else "",
        selected_entity_name=selected_name,
        relationship_entity_type=relationship_entity_type,
        relationship_entity_name=relationship_entity_name,
        comparison_target=comparison_target,
        time_window=time_window,
        use_current_page_context=use_current_page_context,
        use_full_history=use_full_history,
        use_current_filters=use_current_filters,
        export_requested=export_requested,
        export_intent_type=export_intent_type,
        output_format=output_format,
        include_chart=include_chart,
        chart_image_only=chart_image_only,
        chart_image_format=chart_image_format,
        include_summary_sheet=include_summary_sheet,
        include_metadata_sheet=include_metadata_sheet,
        include_all_allowed_columns=include_all_allowed_columns,
        async_export=async_export,
        answer_mode=followup.response_mode,
        risk_focus=risk_focus,
        trust_focus=trust_focus,
    )


def _detect_module(message: str, page: str, state: Mapping[str, Any] | None) -> str:
    text = str(message or "").strip().lower()
    focus_overrides = (
        ("products", ("focus on products", "focus only on products")),
        ("customers", ("focus on customers", "focus only on customers")),
        ("regions", ("focus on regions", "focus only on regions")),
        ("suppliers", ("focus on suppliers", "focus only on suppliers")),
        ("salesreps", ("focus on sales reps", "focus on salesreps", "focus only on sales reps", "focus only on salesreps")),
        ("returns", ("focus on returns", "focus only on returns")),
    )
    for module, tokens in focus_overrides:
        if any(token in text for token in tokens):
            return module
    if "show only products" in text:
        return "products"
    if "show only customers" in text:
        return "customers"
    if "show only regions" in text:
        return "regions"
    if "show only suppliers" in text:
        return "suppliers"
    if "show only sales reps" in text or "show only salesreps" in text or "show only reps" in text:
        return "salesreps"
    if "show only returns" in text:
        return "returns"
    for module, tokens in _MODULE_KEYWORDS.items():
        if any(token in text for token in tokens):
            return module
    if page in _MODULE_KEYWORDS and page != "assistant":
        return page
    last_module = str((state or {}).get("last_module") or "").strip().lower()
    if last_module in _MODULE_KEYWORDS:
        return last_module
    return "overview"


def _merge_followup_slots(
    slots: SemanticSlots,
    *,
    state: Mapping[str, Any] | None,
    followup: FollowupResolution,
) -> SemanticSlots:
    if not followup.is_followup:
        return slots
    previous = (state or {}).get("last_query_slots") if isinstance((state or {}).get("last_query_slots"), Mapping) else {}
    if not previous:
        return slots
    defaults = SemanticSlots().as_dict()
    current = slots.as_dict()
    inherit_fields = {
        "intent_type",
        "query_shape",
        "metric",
        "ranking_direction",
        "limit_n",
        "primary_entity_type",
        "secondary_entity_type",
        "parent_entity_type",
        "child_entity_type",
        "parent_limit_n",
        "child_limit_n",
        "group_by_dimension",
        "selected_entity_name",
        "relationship_entity_type",
        "relationship_entity_name",
        "time_window",
        "use_current_page_context",
        "use_full_history",
        "use_current_filters",
    }
    for field in inherit_fields:
        current_value = current.get(field)
        default_value = defaults.get(field)
        previous_value = previous.get(field)
        if previous_value in (None, "", 0, False):
            continue
        if current_value == default_value or current_value in ("", 0):
            current[field] = previous_value
    current["answer_mode"] = followup.response_mode
    try:
        return SemanticSlots(**{key: value for key, value in current.items() if key in SemanticSlots.__dataclass_fields__})
    except Exception:
        return slots


def _refine_module_from_slots(module: str, page: str, slots: SemanticSlots) -> str:
    current = str(module or page or "overview").strip().lower()
    parent = str(slots.parent_entity_type or "").strip().lower()
    if slots.query_shape == "nested_ranking" and parent in {"customers", "products", "regions", "suppliers", "salesreps", "returns"}:
        return parent
    preferred = (
        parent
        or str(slots.primary_entity_type or "").strip().lower()
        or str(slots.group_by_dimension or "").strip().lower()
    )
    if current in {"admin", "assistant"} and preferred in {"customers", "products", "regions", "suppliers", "salesreps", "returns"}:
        return preferred
    if (
        current == "overview"
        and slots.intent_type in {"ranking", "grouped"}
        and preferred in {"customers", "products", "regions", "suppliers", "salesreps", "returns"}
    ):
        return preferred
    if current == "admin" and str(slots.relationship_entity_type or "").strip().lower() in {"customers", "products", "regions", "suppliers", "salesreps"}:
        return str(slots.primary_entity_type or slots.relationship_entity_type or current).strip().lower()
    return current or "overview"


def _is_followup_message(message: str) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    if any(token in text for token in ("export that", "compare that", "show full history instead", "use full history instead")):
        return True
    if any(
        token in text
        for token in (
            "compare with last year",
            "compare to last year",
            "compare with last quarter",
            "compare to last quarter",
            "compare with the prior period",
            "compare to the prior period",
        )
    ):
        return True
    standalone_tokens = (
        "what is",
        "define",
        "export",
        "history",
        "compare",
        "forecast",
        "risk",
        "summary",
    )
    if len(text) <= 24 and any(token in text for token in standalone_tokens):
        return False
    tokens = (
        "why?",
        "why",
        "show only",
        "compare that",
        "compare with last year",
        "compare to last year",
        "in simpler terms",
        "explain that",
        "explain this",
        "show more detail",
        "what about customers",
        "what about regions",
        "what about products",
        "what about suppliers",
        "what about sales reps",
        "what about salesreps",
        "what about reps",
        "what about returns",
        "what next",
        "what should i do next",
        "what should we do next",
        "short version",
        "read this",
        "investigate next",
    )
    if any(token in text for token in tokens):
        return True
    if _is_pronoun_followup_reference(text):
        return True
    return len(text) <= 18 and text in {"why", "why?", "that", "those", "this", "more detail", "simpler", "what next"}


def _followup_subject(state: Mapping[str, Any] | None, *, fallback_module: str) -> str:
    data = dict(state or {})
    for key in ("last_subject", "last_focus", "last_dimension", "last_metric"):
        token = str(data.get(key) or "").strip()
        if token:
            return token
    return fallback_module or "the previous result"


def _followup_base_question(last_question: str) -> str:
    text = str(last_question or "").strip()
    if not text:
        return "Continue the previous analysis."
    cleaned = re.sub(r"\s+Focus(?: only)? on [^.]+\.?$", "", text, flags=re.IGNORECASE).strip()
    return cleaned or "Continue the previous analysis."


def _resolve_followup_message(
    message: str,
    *,
    state: Mapping[str, Any] | None,
    page_module: str,
) -> FollowupResolution:
    text = str(message or "").strip()
    lowered = text.lower()
    last_focus = str((state or {}).get("last_focus") or "").strip()
    last_question = str((state or {}).get("last_resolved_question") or "").strip()
    last_module = str((state or {}).get("last_module") or page_module or "overview").strip().lower()
    last_metric = str((state or {}).get("last_metric") or "").strip()
    last_comparison_target = str((state or {}).get("last_comparison_target") or "").strip()
    last_action_topic = str((state or {}).get("last_action_topic") or "").strip()
    entity_blob = (state or {}).get("last_entities") if isinstance((state or {}).get("last_entities"), Mapping) else {}
    entity_label = str((entity_blob or {}).get("label") or (entity_blob or {}).get("id") or "").strip()
    subject_text = _followup_subject(state, fallback_module=last_module)
    base_question = _followup_base_question(last_question)

    request_compare_periods = "last quarter" in lowered or "compare that" in lowered or "compare with" in lowered
    request_simpler = "simpler" in lowered or "plain language" in lowered
    request_more_detail = "more detail" in lowered or "deeper" in lowered
    request_executive = "leadership" in lowered or "executive" in lowered or "stakeholder" in lowered
    request_voice = "spoken" in lowered or "read this" in lowered or "voice" in lowered
    response_mode = "standard"
    if request_executive:
        response_mode = "executive"
    if "analyst mode" in lowered or "analyst" in lowered:
        response_mode = "analyst"
    if request_simpler or "simple mode" in lowered:
        response_mode = "simple"
    detail_level = "standard"
    if "short version" in lowered or "concise" in lowered or "brief" in lowered:
        detail_level = "short"
    elif request_more_detail or "detailed" in lowered:
        detail_level = "detailed"

    if not _is_followup_message(text):
        return FollowupResolution(
            resolved_message=text,
            is_followup=False,
            request_compare_periods=request_compare_periods,
            request_simpler=request_simpler,
            request_more_detail=request_more_detail,
            request_executive=request_executive,
            request_voice=request_voice,
            response_mode=response_mode,
            detail_level=detail_level,
        )

    if lowered in {"why", "why?"}:
        base_focus = last_focus or "the latest movement"
        resolved = f"Why did {base_focus} change in {last_module}?"
    elif lowered.startswith("explain that") or lowered.startswith("explain this"):
        resolved = f"Explain the previous answer about {subject_text} in {last_module}."
    elif "simpler" in lowered or "plain language" in lowered:
        resolved = f"Explain the previous answer in simpler terms about {subject_text} in {last_module}."
    elif "what should i do next" in lowered or lowered in {"what next", "what should we do next"}:
        focus = last_action_topic or subject_text
        resolved = f"What should I do next about {focus} in {last_module}?"
    elif "which customers drove this" in lowered:
        metric_part = f" {last_metric}" if last_metric else ""
        resolved = f"Which customers drove this{metric_part} change in {last_module}?"
    elif "which regions drove this" in lowered:
        metric_part = f" {last_metric}" if last_metric else ""
        resolved = f"Which regions drove this{metric_part} change in {last_module}?"
    elif "which products drove this" in lowered:
        metric_part = f" {last_metric}" if last_metric else ""
        resolved = f"Which products drove this{metric_part} change in {last_module}?"
    elif "which suppliers drove this" in lowered:
        metric_part = f" {last_metric}" if last_metric else ""
        resolved = f"Which suppliers drove this{metric_part} change in {last_module}?"
    elif "which sales reps drove this" in lowered or "which reps drove this" in lowered:
        metric_part = f" {last_metric}" if last_metric else ""
        resolved = f"Which sales reps drove this{metric_part} change in {last_module}?"
    elif lowered.startswith("what about customers"):
        resolved = f"{base_question} Focus on customers."
    elif lowered.startswith("what about regions"):
        resolved = f"{base_question} Focus on regions."
    elif lowered.startswith("what about products"):
        resolved = f"{base_question} Focus on products."
    elif lowered.startswith("what about suppliers"):
        resolved = f"{base_question} Focus on suppliers."
    elif lowered.startswith("what about sales reps") or lowered.startswith("what about salesreps") or lowered.startswith("what about reps"):
        resolved = f"{base_question} Focus on sales reps."
    elif lowered.startswith("what about returns"):
        resolved = f"{base_question} Focus on returns."
    elif "export that" in lowered or lowered in {"export", "export this"}:
        entity_part = f" for {entity_label}" if entity_label else ""
        resolved = f"Export the current analysis in {last_module}{entity_part} to Excel workbook."
    elif "show full history" in lowered or "use full history" in lowered:
        entity_part = f" for {entity_label}" if entity_label else ""
        resolved = f"Show full history{entity_part} in {last_module} over time."
    elif "compare that" in lowered or lowered.startswith("compare with") or lowered.startswith("compare to"):
        entity_part = f" for {entity_label}" if entity_label else ""
        target = last_comparison_target or "last year"
        resolved = f"Compare the prior result{entity_part} in {last_module} with {target}."
    elif "show only products" in lowered:
        resolved = f"{base_question} Focus only on products."
    elif "show only customers" in lowered:
        resolved = f"{base_question} Focus only on customers."
    elif "show only suppliers" in lowered:
        resolved = f"{base_question} Focus only on suppliers."
    elif "show only sales reps" in lowered or "show only salesreps" in lowered or "show only reps" in lowered:
        resolved = f"{base_question} Focus only on sales reps."
    elif "show only returns" in lowered:
        resolved = f"{base_question} Focus only on returns."
    elif "this customer" in lowered and entity_label:
        resolved = f"{text} for customer {entity_label}."
    elif "this page" in lowered:
        resolved = f"{text} for the current {last_module} page context."
    elif (request_more_detail or response_mode == "analyst" or request_simpler or request_executive) and last_question:
        resolved = f"{text} Context: {last_question}"
    elif _is_pronoun_followup_reference(lowered) and last_question:
        resolved = f"{text} Context: {last_question}"
    else:
        resolved = text

    if request_compare_periods and "compare" not in resolved.lower():
        resolved = f"{resolved} Compare with last quarter."
    return FollowupResolution(
        resolved_message=resolved.strip(),
        is_followup=True,
        force_module=last_module if _is_pronoun_followup_reference(lowered) else None,
        request_compare_periods=request_compare_periods,
        request_simpler=request_simpler,
        request_more_detail=request_more_detail,
        request_executive=request_executive,
        request_voice=request_voice,
        response_mode=response_mode,
        detail_level=detail_level,
    )


def _question_type(message: str, module: str, followup: FollowupResolution, slots: SemanticSlots) -> str:
    text = str(message or "").strip().lower()
    driver_priority = any(
        token in text
        for token in (
            "why is",
            "what drove",
            "drove this",
            "drove the",
            "drivers behind",
            "contributed most",
        )
    )
    if any(token in text for token in ("page bundle", "visible state", "what is on this page", "current page state")):
        return "page_bundle"
    if any(token in text for token in ("schedule digest", "digest schedule", "weekly digest", "daily digest", "monthly digest", "run scheduled digest")):
        return "scheduled_digest"
    if _looks_definition_question(text):
        return "definition_help"
    if _looks_page_help_question(text):
        return "page_help"
    if any(token in text for token in ("top risks", "what should i do next", "priority actions", "urgent")):
        return "risk_action"
    if any(
        token in text
        for token in (
            "what stands out",
            "changed that leadership should care",
            "deserves attention first",
            "what is unusual right now",
            "top signals",
        )
    ):
        return "proactive_insights"
    if driver_priority:
        return "driver_mover"
    if slots.intent_type == "ranking" and not slots.export_requested:
        return "ranking_analytics"
    if slots.intent_type == "grouped" and not slots.export_requested:
        return "grouped_analytics"
    if any(token in text for token in ("include trends in that export", "make it leadership-friendly", "use full history instead", "exclude low-base", "customer-only version", "modify export")):
        return "modify_request"
    if slots.export_requested or any(token in text for token in ("export", "excel", "workbook", "download workbook", "analysis pack", "leadership pack")):
        return "export_request"
    if any(token in text for token in ("draft a summary note", "follow-up note", "investigation checklist", "structured action plan")):
        return "workflow_assist"
    if any(token in text for token in ("anomaly", "unusual", "risk narrative", "why is this unusual", "cause chain")):
        return "anomaly_risk"
    if any(token in text for token in ("investigate next", "which module should i open", "highest-priority follow-up", "next best step")):
        return "guided_investigation"
    if "workflow" in text and "return" in text:
        return "returns_workflow"
    if _has_explicit_comparison_request(text):
        return "comparison_analytics"
    if _has_explicit_history_request(text):
        return "history_analytics"
    if followup.request_executive or any(
        token in text for token in ("summarize for leadership", "executive summary", "stakeholder summary", "manager summary", "leadership brief")
    ):
        return "executive_digest"
    if any(token in text for token in ("weekly leadership briefing", "manager digest", "leadership summary", "executive digest")):
        return "executive_summary"
    if any(token in text for token in ("across the business", "cross module", "cross-module", "which entities", "what should we investigate first")):
        return "cross_module"
    if _looks_forecast_question(text):
        return "forecast_outlook"
    if _looks_trust_question(text):
        return "trust_quality"
    if _looks_driver_question(text):
        return "driver_mover"
    if _looks_risk_question(text):
        return "risk_watchout"
    if _looks_summary_question(text):
        return "page_summary"
    if _looks_analyst_detail_question(text) or followup.response_mode == "analyst":
        return "analyst_detail"
    if module == "returns" or "returns" in text or "rma" in text:
        return "returns_analytics"
    return "live_analytics"


def _tool_allowed(tool_name: str, module_access: Mapping[str, bool], *, allow_glossary: bool) -> bool:
    del module_access
    if tool_name in {
        "get_user_scope",
        "get_current_page_context",
        "get_related_investigations",
        "get_recommended_followups",
        "compare_entities",
        "compare_periods",
        "get_priority_investigations",
        "get_priority_actions",
        "summarize_module_state",
        "get_entity_watchouts",
        "get_executive_summary",
        "get_entity_relationship_context",
        "get_proactive_insights",
        "get_anomaly_narratives",
        "get_priority_risks",
        "get_risk_trend_baseline",
        "get_causal_attribution_graph",
        "get_guided_investigation_paths",
        "get_executive_digest",
        "get_manager_digest",
        "get_leadership_summary",
        "get_investigation_checklist",
        "get_confidence_or_trust_summary",
        "get_entity_change_explanation",
        "get_cross_module_risk_summary",
        "get_next_best_questions",
        "get_workflow_assist_note",
        "create_digest_schedule",
        "list_digest_schedules",
        "run_digest_schedule",
        "delete_digest_schedule",
        "get_page_bundle",
        "get_entity_page_bundle",
        "get_current_page_summary",
        "get_current_page_visible_state",
        "rank_entities",
        "aggregate_by_dimension",
        "get_top_regions",
        "get_top_customers",
        "get_top_products",
        "get_top_suppliers",
        "get_top_sales_reps",
        "get_top_products_for_customer",
        "get_top_customers_for_supplier",
        "get_top_customers_for_product",
        "get_top_products_for_supplier",
        "get_top_products_for_sales_rep",
        "get_nested_rankings",
        "get_top_customers_with_top_products",
        "get_top_suppliers_with_top_products",
        "get_top_sales_reps_with_top_customers",
        "get_top_regions_with_top_products",
        "get_top_products_with_top_customers",
        "resolve_entity_reference",
        "get_detailed_metric_breakdown",
        "get_entity_driver_breakdown",
        "get_entity_margin_breakdown",
        "get_top_margin_risk_products",
        "get_top_decliners",
        "get_top_gainers",
        "compare_current_vs_prior_period_rankings",
        "get_overview_history",
        "get_customer_history",
        "get_product_history",
        "get_region_history",
        "get_supplier_history",
        "get_sales_rep_history",
        "get_returns_history",
        "compare_periods_for_entity",
        "explain_history_for_entity",
        "compare_customers",
        "compare_products",
        "compare_regions",
        "compare_suppliers",
        "compare_sales_reps",
        "get_export_options_for_page",
        "get_exportable_columns_for_context",
        "get_ranked_dataset",
        "get_grouped_dataset",
        "get_entity_history_dataset",
        "get_comparison_dataset",
        "get_summary_dataset",
        "get_chart_series",
        "get_export_metadata",
        "get_leadership_summary_dataset",
        "export_current_page_excel",
        "export_current_entity_history_excel",
        "export_analysis_bundle_excel",
        "export_leadership_pack_excel",
        "export_watchlist_excel",
        "export_custom_scoped_excel",
        "export_ranked_list_excel",
        "export_grouped_metric_excel",
        "export_comparison_excel",
        "export_nested_ranking_excel",
        "export_hierarchical_analysis_excel",
        "export_chart_series_file",
        "export_chart_image_file",
        "export_custom_analysis_file",
        "build_export_configuration",
        "refine_export_request",
        "build_saved_view_suggestion",
        "build_analysis_bundle_request",
        "set_answer_mode",
        "set_export_mode",
    }:
        return True
    if tool_name in {"search_business_glossary", "explain_metric_definition", "get_metric_definition", "get_page_help"}:
        return allow_glossary
    return True


def _choose_tools(
    message: str,
    *,
    page: str,
    module: str,
    question_type: str,
    slots: SemanticSlots,
    module_access: Mapping[str, bool],
    max_calls: int,
    allow_glossary: bool,
    followup: FollowupResolution,
) -> List[tuple[str, Dict[str, Any]]]:
    text = str(message or "").strip().lower()
    picks: List[tuple[str, Dict[str, Any]]] = [("get_current_page_context", {}), ("get_user_scope", {})]
    digest_length = "medium"
    if any(token in text for token in ("short", "concise", "brief")) or followup.detail_level == "short":
        digest_length = "short"
    elif any(token in text for token in ("long", "detailed", "deep")) or followup.detail_level == "detailed":
        digest_length = "long"
    response_mode = followup.response_mode
    rank_direction = "bottom" if slots.ranking_direction == "bottom" else "top"
    rank_limit = max(1, min(50, int(slots.limit_n or 5)))
    rank_metric = str(slots.metric or "revenue").strip().lower()
    rank_dimension = (
        str(slots.group_by_dimension or slots.primary_entity_type or module or "customers").strip().lower()
    )
    secondary_entity = str(slots.secondary_entity_type or "").strip().lower()
    filter_entity_type = str(slots.relationship_entity_type or secondary_entity or "").strip().lower()
    ranking_args = {
        "entity_type": rank_dimension,
        "metric": rank_metric,
        "direction": rank_direction,
        "limit": rank_limit,
        "query_shape": str(slots.query_shape or "single_hop"),
        "secondary_entity_type": secondary_entity,
        "parent_entity_type": str(slots.parent_entity_type or rank_dimension).strip().lower(),
        "child_entity_type": str(slots.child_entity_type or "").strip().lower(),
        "parent_limit": max(1, min(10, int(slots.parent_limit_n or rank_limit))),
        "child_limit": max(1, min(10, int(slots.child_limit_n or min(rank_limit, 5)))),
        "filter_entity_type": filter_entity_type,
        "filter_entity_name": str(slots.relationship_entity_name or "").strip(),
        "selected_entity_name": str(slots.selected_entity_name or "").strip(),
        "exclude_low_base": bool("exclude low-base" in text or "exclude low base" in text),
    }

    def add(name: str, args: Dict[str, Any] | None = None) -> None:
        item = (name, dict(args or {}))
        if item not in picks:
            picks.append(item)

    def add_ranking_tools(*, include_detail: bool = False) -> None:
        if slots.query_shape == "nested_ranking":
            parent_type = str(slots.parent_entity_type or rank_dimension or module).strip().lower()
            child_type = str(slots.child_entity_type or "").strip().lower()
            nested_name = "get_nested_rankings"
            if parent_type == "customers" and child_type == "products":
                nested_name = "get_top_customers_with_top_products"
            elif parent_type == "suppliers" and child_type == "products":
                nested_name = "get_top_suppliers_with_top_products"
            elif parent_type == "salesreps" and child_type == "customers":
                nested_name = "get_top_sales_reps_with_top_customers"
            elif parent_type == "regions" and child_type == "products":
                nested_name = "get_top_regions_with_top_products"
            elif parent_type == "products" and child_type == "customers":
                nested_name = "get_top_products_with_top_customers"
            add("resolve_entity_reference", {"entity_type": filter_entity_type, "entity_name": str(slots.relationship_entity_name or "").strip()})
            add(nested_name, ranking_args)
        elif "margin-risk" in text or "margin risk" in text:
            add("get_top_margin_risk_products", {"limit": rank_limit, "metric": rank_metric, **ranking_args})
        elif "decliner" in text or "decline" in text:
            add("get_top_decliners", {"entity_type": rank_dimension, "limit": rank_limit, "metric": rank_metric, **ranking_args})
        elif "gainer" in text or "gain" in text:
            add("get_top_gainers", {"entity_type": rank_dimension, "limit": rank_limit, "metric": rank_metric, **ranking_args})
        elif rank_dimension == "regions":
            add("get_top_regions", {"metric": rank_metric, "direction": rank_direction, "limit": rank_limit, **ranking_args})
        elif rank_dimension == "customers":
            if filter_entity_type == "suppliers":
                add("get_top_customers_for_supplier", ranking_args)
            else:
                add("get_top_customers", {"metric": rank_metric, "direction": rank_direction, "limit": rank_limit, **ranking_args})
        elif rank_dimension == "products":
            if filter_entity_type == "customers":
                add("get_top_products_for_customer", ranking_args)
            elif filter_entity_type == "suppliers":
                add("get_top_products_for_supplier", ranking_args)
            elif filter_entity_type == "salesreps":
                add("get_top_products_for_sales_rep", ranking_args)
            else:
                add("get_top_products", {"metric": rank_metric, "direction": rank_direction, "limit": rank_limit, **ranking_args})
        elif rank_dimension == "suppliers":
            add("get_top_suppliers", {"metric": rank_metric, "direction": rank_direction, "limit": rank_limit, **ranking_args})
        elif rank_dimension == "salesreps":
            add("get_top_sales_reps", {"metric": rank_metric, "direction": rank_direction, "limit": rank_limit, **ranking_args})
        else:
            add("rank_entities", ranking_args)
        if include_detail:
            add("get_detailed_metric_breakdown", ranking_args)
            add("get_entity_driver_breakdown", ranking_args)
            add("get_entity_margin_breakdown", ranking_args)

    def add_module_tools(*, include_relationships: bool = True, include_watchouts: bool = True) -> None:
        if module in {"overview", "assistant"}:
            add("get_overview_summary")
            add("get_overview_kpis")
            add("get_trend_series", {"metric": "revenue", "grain": "monthly"})
            if include_watchouts:
                add("get_concentration_risk")
                add("get_margin_watchlist")
            return
        if module == "customers":
            add("get_customer_summary")
            add("get_customer_kpis")
            add("get_customer_trend")
            if include_watchouts:
                add("get_customer_watchouts")
            if include_relationships:
                add("get_customer_relationships")
            return
        if module == "products":
            add("get_product_summary")
            add("get_product_kpis")
            add("get_product_trend")
            if include_watchouts:
                add("get_product_watchouts")
            if include_relationships:
                add("get_product_dependencies")
            return
        if module == "regions":
            add("get_region_summary")
            add("get_region_trend")
            if include_relationships:
                add("get_region_movers")
            if include_watchouts:
                add("get_region_watchouts")
            return
        if module == "suppliers":
            add("get_supplier_summary")
            add("get_supplier_trend")
            if include_watchouts:
                add("get_supplier_watchouts")
            return
        if module in {"salesreps", "sales_rep", "sales_reps"}:
            add("get_sales_rep_summary")
            add("get_sales_rep_trend")
            if include_watchouts:
                add("get_sales_rep_watchouts")
            return
        if module == "returns":
            add("get_returns_summary")
            add("get_pending_returns")
            add("get_returns_status_overview")
            add("get_returns_reason_patterns")

    if question_type == "definition_help":
        add("get_metric_definition", {"metric": _extract_metric_hint(text)})
        add("search_business_glossary", {"query": text})
        add("get_page_help", {"page": module or page})
        add("get_recommended_followups")
    elif question_type == "ranking_analytics":
        add("get_page_bundle", {"module": module})
        add_ranking_tools(include_detail=followup.response_mode == "analyst" or followup.detail_level == "detailed")
        add("get_confidence_or_trust_summary")
        add("get_next_best_questions", {"module": module})
    elif question_type == "grouped_analytics":
        add("get_page_bundle", {"module": module})
        add(
            "aggregate_by_dimension",
            {
                "dimension": rank_dimension,
                "metric": rank_metric,
                "limit": rank_limit,
                "secondary_entity_type": secondary_entity,
                "selected_entity_name": str(slots.selected_entity_name or "").strip(),
            },
        )
        add("get_confidence_or_trust_summary")
        add("get_next_best_questions", {"module": module})
    elif question_type == "page_summary":
        add("get_page_bundle", {"module": module})
        add("get_current_page_visible_state")
        add("get_current_page_summary", {"module": module})
        add_module_tools(include_relationships=False, include_watchouts=True)
        add("get_confidence_or_trust_summary")
        add("get_related_investigations")
    elif question_type == "page_bundle":
        add("get_page_bundle", {"module": module})
        add("get_current_page_visible_state")
        add("get_current_page_summary", {"module": module})
        add("get_export_options_for_page")
        add("get_recommended_followups")
    elif question_type == "driver_mover":
        add("get_page_bundle", {"module": module})
        if module in {"overview", "assistant"}:
            add("get_overview_kpis")
            add("get_price_volume_mix")
            add("get_top_movers", {"dimension": "customer"})
            add("get_top_movers", {"dimension": "product"})
            add("get_trend_series", {"metric": "revenue", "grain": "monthly"})
        else:
            add("get_entity_change_explanation", {"module": module})
            add("compare_periods_for_entity", {"module": module})
            add_module_tools(include_relationships=True, include_watchouts=True)
        add("get_next_best_questions", {"module": module})
    elif question_type == "risk_watchout":
        add("get_page_bundle", {"module": module})
        add("get_priority_risks", {"module": "all" if module in {"overview", "assistant"} else module})
        add("get_entity_watchouts", {"module": module})
        add("get_risk_trend_baseline", {"module": module})
        add("get_confidence_or_trust_summary")
        add("get_priority_actions")
        add("get_guided_investigation_paths", {"module": module})
    elif question_type == "forecast_outlook":
        add("get_page_bundle", {"module": module})
        add("get_forecast_summary")
        add("get_trend_series", {"metric": "revenue", "grain": "monthly"})
        add("compare_periods", {"module": module})
        add("get_confidence_or_trust_summary")
        add("get_next_best_questions", {"module": module})
    elif question_type == "trust_quality":
        add("get_page_bundle", {"module": module})
        add("get_data_health")
        add("get_confidence_or_trust_summary")
        add("get_current_page_summary", {"module": module})
        add("get_next_best_questions", {"module": module})
    elif question_type == "analyst_detail":
        add("get_page_bundle", {"module": module})
        if slots.intent_type == "ranking":
            add_ranking_tools(include_detail=True)
        add_module_tools(include_relationships=True, include_watchouts=True)
        add("compare_periods", {"module": module})
        add("explain_history_for_entity", {"module": module})
        add("get_entity_change_explanation", {"module": module})
        add("get_confidence_or_trust_summary")
        add("get_priority_actions")
        add("get_next_best_questions", {"module": module})
    elif question_type == "history_analytics":
        history_tool_by_module = {
            "overview": "get_overview_history",
            "assistant": "get_overview_history",
            "customers": "get_customer_history",
            "products": "get_product_history",
            "regions": "get_region_history",
            "suppliers": "get_supplier_history",
            "salesreps": "get_sales_rep_history",
            "returns": "get_returns_history",
        }
        add("get_page_bundle", {"module": module})
        add(history_tool_by_module.get(module, "get_overview_history"), {"module": module})
        add("compare_periods_for_entity", {"module": module})
        add("explain_history_for_entity", {"module": module})
        add("get_entity_change_explanation", {"module": module})
        add("get_confidence_or_trust_summary")
        add("get_guided_investigation_paths", {"module": module})
        add("get_next_best_questions", {"module": module})
    elif question_type == "comparison_analytics":
        add("get_page_bundle", {"module": module})
        if "customer" in text:
            add("compare_customers", {"metric": rank_metric})
        elif "product" in text or "sku" in text:
            add("compare_products", {"metric": rank_metric})
        elif "region" in text:
            add("compare_regions", {"metric": rank_metric})
        elif "supplier" in text:
            add("compare_suppliers", {"metric": rank_metric})
        elif "rep" in text:
            add("compare_sales_reps", {"metric": rank_metric})
        else:
            add("compare_entities", {"dimension": module if module != "assistant" else "customers", "metric": rank_metric})
        add("compare_periods_for_entity", {"module": module})
        add("get_cross_module_risk_summary")
        add("get_guided_investigation_paths", {"module": module})
        add("get_next_best_questions", {"module": module})
    elif question_type == "export_request":
        export_mode = "standard"
        if slots.answer_mode in {"executive", "analyst", "simple", "standard"}:
            export_mode = slots.answer_mode
        if "leadership" in text or "executive" in text:
            export_mode = "executive"
        elif "analyst" in text or "detailed" in text:
            export_mode = "analyst"
        requested_columns: List[str] = []
        column_match = re.search(r"columns?\s*:\s*([a-z0-9_,\s%-]+)", text)
        if column_match:
            requested_columns = [token.strip() for token in str(column_match.group(1) or "").split(",") if token.strip()]
        common_export_args: Dict[str, Any] = {
            "module": module,
            "metric": rank_metric,
            "direction": rank_direction,
            "ranking_direction": rank_direction,
            "limit": rank_limit,
            "dimension": rank_dimension,
            "group_by_dimension": rank_dimension,
            "query_shape": str(slots.query_shape or "single_hop"),
            "secondary_entity_type": secondary_entity,
            "parent_entity_type": str(slots.parent_entity_type or rank_dimension).strip().lower(),
            "child_entity_type": str(slots.child_entity_type or "").strip().lower(),
            "parent_limit": max(1, min(10, int(slots.parent_limit_n or rank_limit))),
            "child_limit": max(1, min(10, int(slots.child_limit_n or min(rank_limit, 5)))),
            "filter_entity_type": filter_entity_type,
            "filter_entity_name": str(slots.relationship_entity_name or "").strip(),
            "selected_entity_name": str(slots.selected_entity_name or "").strip(),
            "output_format": str(slots.output_format or "xlsx"),
            "include_chart": bool(slots.include_chart),
            "chart_image_only": bool(slots.chart_image_only),
            "image_format": str(slots.chart_image_format or ""),
            "include_summary_sheet": bool(slots.include_summary_sheet),
            "include_metadata_sheet": bool(slots.include_metadata_sheet),
            "include_all_allowed_columns": bool(slots.include_all_allowed_columns),
            "requested_columns": requested_columns,
            "export_mode": export_mode,
            "async_export": bool(slots.async_export),
            "use_current_page_context": bool(slots.use_current_page_context),
            "use_current_filters": bool(slots.use_current_filters),
            "use_full_history": bool(slots.use_full_history),
            "time_window": str(slots.time_window or ""),
            "exclude_low_base": bool("exclude low-base" in text or "exclude low base" in text),
            "export_intent_type": str(slots.export_intent_type or ""),
        }
        add("build_export_configuration", common_export_args)
        export_intent = str(slots.export_intent_type or "").strip().lower()
        if slots.query_shape == "nested_ranking" and export_intent in {"", "export_ranked_list"}:
            export_intent = "export_hierarchical_analysis"
        if export_intent in {"export_chart_only", "export_chart_plus_data"}:
            add("get_chart_series", common_export_args)
            if bool(slots.chart_image_only):
                add("export_chart_image_file", common_export_args)
            else:
                add("export_chart_series_file", common_export_args)
        elif export_intent in {"export_hierarchical_analysis"}:
            add("get_nested_rankings", common_export_args)
            add("export_hierarchical_analysis_excel", common_export_args)
        elif export_intent in {"export_ranked_list"} or (not export_intent and slots.intent_type == "ranking"):
            add("get_ranked_dataset", common_export_args)
            add("export_ranked_list_excel", common_export_args)
        elif export_intent in {"export_grouped_metric"} or (not export_intent and slots.intent_type == "grouped"):
            add("get_grouped_dataset", common_export_args)
            add("export_grouped_metric_excel", common_export_args)
        elif export_intent in {"export_comparison"} or (not export_intent and slots.intent_type == "comparison"):
            add("get_comparison_dataset", common_export_args)
            add("export_comparison_excel", common_export_args)
        elif export_intent in {"export_entity_history"} or (not export_intent and slots.intent_type == "history"):
            add("get_entity_history_dataset", common_export_args)
            add("export_current_entity_history_excel", common_export_args)
        elif export_intent in {"export_leadership_pack"}:
            add("get_leadership_summary_dataset", common_export_args)
            add("export_leadership_pack_excel", common_export_args)
        elif export_intent in {"export_custom_analysis_pack"}:
            add("get_summary_dataset", common_export_args)
            add("export_custom_analysis_file", common_export_args)
        else:
            if "watchlist" in text or "risk" in text:
                add("export_watchlist_excel", common_export_args)
            elif "bundle" in text or "analysis pack" in text:
                add("export_analysis_bundle_excel", common_export_args)
            elif "custom" in text:
                add("export_custom_scoped_excel", common_export_args)
            else:
                add("export_current_page_excel", common_export_args)
        add("get_exportable_columns_for_context", common_export_args)
        add("get_export_options_for_page")
        add("get_export_metadata", {"module": module, "export_type": export_intent or "export_table"})
        add("get_next_best_questions", {"module": module})
    elif question_type == "modify_request":
        add("get_page_bundle", {"module": module})
        add("build_export_configuration", {"module": module})
        add("refine_export_request", {"base": {"module": module}, "instruction": text})
        add("build_saved_view_suggestion", {"module": module})
        add("build_analysis_bundle_request", {"module": module})
        if "answer mode" in text or "analyst" in text or "simple" in text or "executive" in text:
            add("set_answer_mode", {"mode": followup.response_mode})
        if "export mode" in text or "leadership" in text:
            add("set_export_mode", {"mode": "leadership" if "leadership" in text else "standard"})
        add("get_next_best_questions", {"module": module})
    elif question_type == "page_help":
        add("get_page_help", {"page": module or page})
        add("get_current_page_visible_state")
        add("get_related_investigations")
        add("get_recommended_followups")
    elif question_type == "returns_workflow":
        add("get_returns_workflow_help", {"section": "workflow"})
        add("get_pending_returns")
        add("get_returns_status_overview")
        add("get_related_investigations")
    elif question_type == "proactive_insights":
        add("get_proactive_insights", {"module": module, "cross_module": module in {"overview", "assistant"}})
        add("get_priority_risks", {"module": "all" if module in {"overview", "assistant"} else module})
        add("get_risk_trend_baseline", {"module": module})
        add("get_confidence_or_trust_summary")
        add("get_guided_investigation_paths", {"module": module})
        add("get_next_best_questions", {"module": module})
    elif question_type == "anomaly_risk":
        add("get_anomaly_narratives", {"module": module})
        add("get_entity_change_explanation", {"module": module})
        add("get_causal_attribution_graph", {"module": module})
        add("get_risk_trend_baseline", {"module": module})
        add("get_priority_risks", {"module": module})
        add("get_cross_module_risk_summary")
        add("get_guided_investigation_paths", {"module": module})
    elif question_type == "scheduled_digest":
        schedule_id = _extract_schedule_id(text)
        if any(token in text for token in ("delete", "remove", "cancel")):
            add("delete_digest_schedule", {"schedule_id": schedule_id})
            add("list_digest_schedules")
        elif any(token in text for token in ("list", "show schedules", "show schedule", "what schedules")):
            add("list_digest_schedules")
        elif any(token in text for token in ("run", "generate now", "send now")):
            add("run_digest_schedule", {"schedule_id": schedule_id})
            add("list_digest_schedules")
        else:
            cadence = "weekly"
            if "daily" in text:
                cadence = "daily"
            elif "monthly" in text:
                cadence = "monthly"
            audience = "manager" if "manager" in text else "leadership"
            add("create_digest_schedule", {"module": module, "cadence": cadence, "audience": audience, "length": digest_length})
            add("list_digest_schedules")
    elif question_type == "guided_investigation":
        add("get_guided_investigation_paths", {"module": module})
        add("get_priority_actions")
        add("get_priority_risks", {"module": module})
        add("get_related_investigations")
        add("get_next_best_questions", {"module": module})
    elif question_type == "workflow_assist":
        add("get_page_bundle", {"module": module})
        add("get_investigation_checklist", {"module": module})
        add("get_workflow_assist_note", {"module": module, "note_type": "next_steps"})
        add("get_guided_investigation_paths", {"module": module})
        add("get_priority_actions")
        add("get_next_best_questions", {"module": module})
    elif question_type == "executive_digest":
        audience = "leadership" if ("leadership" in text or "executive" in text or response_mode == "executive") else "manager"
        add("get_executive_digest", {"module": module, "length": digest_length, "audience": audience})
        if audience == "manager":
            add("get_manager_digest", {"module": module, "length": digest_length})
        else:
            add("get_leadership_summary", {"module": module, "length": "short"})
        add("get_guided_investigation_paths", {"module": module})
        add("get_next_best_questions", {"module": module})
    elif question_type == "executive_summary":
        add("get_page_bundle", {"module": module})
        add("get_executive_summary", {"module": module})
        add("get_entity_watchouts", {"module": module})
        add("compare_periods", {"module": module})
        add("get_related_investigations")
    elif question_type in {"cross_module", "risk_action"}:
        dimension = "customers"
        if "product" in text:
            dimension = "products"
        elif "region" in text:
            dimension = "regions"
        elif "supplier" in text:
            dimension = "suppliers"
        elif "rep" in text:
            dimension = "salesreps"
        add("get_priority_investigations")
        add("get_priority_actions")
        add("compare_entities", {"dimension": dimension, "metric": "revenue"})
        add("get_cross_module_risk_summary")
        add("get_entity_relationship_context", {"module": module})
        add("get_related_investigations")
    else:
        add("get_page_bundle", {"module": module})
        add_module_tools(include_relationships=True, include_watchouts=True)
        add("get_current_page_summary", {"module": module})
        add("get_confidence_or_trust_summary")
        if module in {"overview", "assistant"} and any(token in text for token in ("driver", "price", "volume", "mix", "pvm", "why", "mover", "decliner", "gainer")):
            add("get_price_volume_mix")
            add("get_top_movers", {"dimension": "customer"})
            add("get_top_movers", {"dimension": "product"})
        if module in {"overview", "assistant"} and any(token in text for token in ("trust", "coverage", "data health", "can i trust")):
            add("get_data_health")
        if module in {"overview", "assistant"} and any(token in text for token in ("forecast", "outlook")):
            add("get_forecast_summary")

        if followup.request_compare_periods or "compare" in text or "last quarter" in text:
            add("compare_periods", {"module": module})
        if any(token in text for token in ("what next", "next steps", "priority", "urgent")):
            add("get_priority_actions")
        if any(token in text for token in ("relationship", "dependent", "linked", "relate")):
            add("get_entity_relationship_context", {"module": module})
        if followup.request_executive:
            add("get_executive_summary", {"module": module})
            add("get_executive_digest", {"module": module, "length": digest_length, "audience": "leadership"})
        if any(token in text for token in ("stand out", "unusual", "anomaly", "risk narrative")):
            add("get_proactive_insights", {"module": module, "cross_module": module in {"overview", "assistant"}})
            add("get_anomaly_narratives", {"module": module})
            add("get_priority_risks", {"module": module})
        if any(token in text for token in ("checklist", "action plan", "summary note")):
            add("get_investigation_checklist", {"module": module})
            add("get_workflow_assist_note", {"module": module, "note_type": "summary"})
        if any(token in text for token in ("simple", "spoken", "voice")):
            add("get_leadership_summary", {"module": module, "length": "short"})
        if any(token in text for token in ("history", "historical", "timeline", "over time")):
            add("explain_history_for_entity", {"module": module})
            add("compare_periods_for_entity", {"module": module})
        if any(token in text for token in ("export", "excel", "workbook", "download")):
            add("get_export_options_for_page")
            add("export_current_page_excel", {"module": module})
        if any(token in text for token in ("modify", "refine", "include trends", "leadership-friendly", "exclude low-base")):
            add("refine_export_request", {"base": {"module": module}, "instruction": text})
        add("get_related_investigations")

    add("get_recommended_followups")
    add("get_next_best_questions", {"module": module})

    filtered: List[tuple[str, Dict[str, Any]]] = []
    for name, args in picks:
        if not _tool_allowed(name, module_access, allow_glossary=allow_glossary):
            continue
        filtered.append((name, args))
        if len(filtered) >= max_calls:
            break
    return filtered


def _tool_value(data: Mapping[str, Any], *keys: str) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(key)
    return cur


def _safe_excerpt(text: str, limit: int = 500) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    return compact[:limit]


def _normalized_text(text: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())).strip()


def _is_near_duplicate_text(current: Any, previous: Any) -> bool:
    cur = _normalized_text(current)
    prev = _normalized_text(previous)
    if not cur or not prev:
        return False
    if cur == prev:
        return True
    if len(cur) >= 24 and (cur in prev or prev in cur):
        return True
    return False


def _fmt_value(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if abs(value) >= 1000:
            return f"{value:,.2f}"
        return f"{value:.2f}"
    return str(value)


def _fmt_currency(value: Any) -> str:
    number = _to_float(value, 0.0)
    decimals = 0 if abs(number) >= 100 else 2
    return f"${number:,.{decimals}f}"


def _fmt_percent(value: Any, *, decimals: int = 1) -> str:
    number = _to_float(value, 0.0)
    return f"{number:.{decimals}f}%"


def _metric_label(metric: str) -> str:
    token = str(metric or "").strip().lower()
    labels = {
        "revenue": "revenue",
        "delta_revenue": "revenue",
        "delta_revenue_pct": "revenue",
        "revenue_change_pct": "revenue",
        "profit": "profit",
        "delta_profit": "profit",
        "delta_profit_pct": "profit",
        "profit_change_pct": "profit",
        "margin": "margin",
        "margin_pct": "margin",
        "delta_margin": "margin",
        "delta_margin_pct": "margin",
        "margin_change_pct": "margin",
        "orders": "orders",
        "delta_orders": "orders",
        "delta_orders_pct": "orders",
        "orders_change_pct": "orders",
        "units": "units",
        "weight": "weight",
        "volume": "volume",
        "price": "price",
        "mix": "mix",
        "count": "count",
        "risk_score": "risk score",
        "confidence_score": "confidence",
        "cost_coverage_pct": "cost coverage",
        "pack_coverage_pct": "pack coverage",
        "total_returns": "returns",
        "pending_count": "pending returns",
    }
    return labels.get(token, token.replace("_", " ") or "metric")


def _fmt_metric_value(metric: str, value: Any) -> str:
    token = str(metric or "").strip().lower()
    if value is None:
        return "n/a"
    if any(marker in token for marker in ("margin", "pct", "percent", "coverage", "confidence", "share")):
        return _fmt_percent(value)
    if any(marker in token for marker in ("revenue", "profit", "cost", "amount", "sales", "credit")):
        return _fmt_currency(value)
    if any(marker in token for marker in ("orders", "count", "returns", "units")):
        try:
            return f"{int(round(float(value))):,}"
        except Exception:
            return _fmt_value(value)
    return _fmt_value(value)


def _trend_direction(value: Any) -> str:
    number = _to_float(value, 0.0)
    if number > 0:
        return "up"
    if number < 0:
        return "down"
    return "flat"


def _assistant_debug_available(user: Any) -> bool:
    return bool(
        rbac.user_has_role(user, "admin", "owner", "gm")
        or rbac.user_has_any_permission(user, "admin.portal.view")
    )


def _detail_panel(
    title: str,
    *,
    body: str = "",
    items: Sequence[str] | None = None,
    tone: str = "neutral",
    admin_only: bool = False,
) -> Dict[str, Any]:
    return {
        "title": str(title or "Details").strip() or "Details",
        "body": str(body or "").strip(),
        "items": [str(item).strip() for item in list(items or []) if str(item).strip()],
        "tone": str(tone or "neutral").strip().lower() or "neutral",
        "admin_only": bool(admin_only),
    }


def _compact_comparison_line(summary: Mapping[str, Any] | None, *, metric: str = "") -> str:
    data = dict(summary or {})
    if not data:
        return ""
    priority = (
        "delta_revenue_pct",
        "delta_profit_pct",
        "delta_margin_pct",
        "delta_orders_pct",
        "revenue_change_pct",
        "profit_change_pct",
        "margin_change_pct",
        "orders_change_pct",
        "delta_revenue",
        "delta_profit",
        "delta_margin",
        "delta_orders",
    )
    for key in priority:
        value = data.get(key)
        if value is None:
            continue
        label = _metric_label(key)
        if "pct" in key or "percent" in key:
            return f"{label} is {_trend_direction(value)} {_fmt_percent(abs(_to_float(value, 0.0)))} versus the comparison period."
        base_metric = metric or key
        return f"{label} is {_trend_direction(value)} {_fmt_metric_value(base_metric, abs(_to_float(value, 0.0)))} versus the comparison period."
    top_fields: List[str] = []
    for key, value in list(data.items())[:3]:
        if value is None:
            continue
        top_fields.append(f"{_metric_label(key)} {_fmt_metric_value(key, value)}")
    return ", ".join(top_fields)


def _extract_highlights(result: Mapping[str, Any], *, max_items: int = 5) -> List[Dict[str, Any]]:
    data = result.get("data")
    if not isinstance(data, Mapping):
        return []
    highlights: List[Dict[str, Any]] = []
    preferred = (
        "revenue",
        "profit",
        "margin_pct",
        "orders",
        "cost_coverage_pct",
        "at_risk_90",
        "total_returns",
        "pending_count",
        "confidence_score",
        "z_score",
        "risk_score",
    )
    for key in preferred:
        value = data.get(key)
        if value is not None:
            highlights.append({"label": key, "value": value})
        if len(highlights) >= max_items:
            return highlights
    kpis = data.get("kpis")
    if isinstance(kpis, Mapping):
        for key in preferred:
            value = kpis.get(key)
            if value is not None and all(item["label"] != f"kpis.{key}" for item in highlights):
                highlights.append({"label": f"kpis.{key}", "value": value})
            if len(highlights) >= max_items:
                return highlights
        for key, value in kpis.items():
            if isinstance(value, (int, float)) and len(highlights) < max_items:
                highlights.append({"label": f"kpis.{key}", "value": value})
    return highlights[:max_items]


def _module_label(module: str) -> str:
    token = str(module or "overview").strip().lower()
    return {
        "overview": "Overview",
        "assistant": "Overview",
        "customers": "Customers",
        "products": "Products",
        "regions": "Regions",
        "suppliers": "Suppliers",
        "salesreps": "Sales Reps",
        "sales_rep": "Sales Reps",
        "sales_reps": "Sales Reps",
        "returns": "Returns",
    }.get(token, token.title() or "Overview")


def _first_non_null(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _pick_result(
    results: Sequence[Mapping[str, Any]],
    *,
    titles: Sequence[str] = (),
    contains: Sequence[str] = (),
) -> Mapping[str, Any] | None:
    exact = [str(item).strip() for item in titles if str(item).strip()]
    partial = [str(item).strip().lower() for item in contains if str(item).strip()]
    matched: List[Mapping[str, Any]] = []
    for result in results:
        title = str(result.get("title") or "").strip()
        if exact and title in exact:
            matched.append(result)
            continue
        lowered = title.lower()
        if partial and any(token in lowered for token in partial):
            matched.append(result)
    if not matched:
        return None
    for result in matched:
        if str(result.get("status") or "").lower() == "ok":
            return result
    return matched[0]


def _pick_data(
    results: Sequence[Mapping[str, Any]],
    *,
    titles: Sequence[str] = (),
    contains: Sequence[str] = (),
) -> Dict[str, Any]:
    result = _pick_result(results, titles=titles, contains=contains)
    data = result.get("data") if isinstance(result, Mapping) else {}
    if isinstance(data, Mapping):
        return dict(data)
    return {}


def _row_label(row: Mapping[str, Any]) -> str:
    for key in (
        "label",
        "name",
        "title",
        "customer",
        "customer_name",
        "product",
        "product_name",
        "region",
        "supplier",
        "supplier_name",
        "sales_rep",
        "rep_name",
        "id",
        "entity",
    ):
        token = row.get(key)
        if token not in (None, ""):
            return str(token)
    return "entity"


def _row_value(row: Mapping[str, Any]) -> Any:
    for key in (
        "revenue",
        "profit",
        "margin_pct",
        "delta_revenue",
        "delta_revenue_pct",
        "change_pct",
        "risk_score",
        "total_credit_amount",
        "count",
        "value",
    ):
        if row.get(key) is not None:
            return row.get(key)
    return None


def _rows_total(rows: Sequence[Mapping[str, Any]]) -> float:
    total = 0.0
    for row in rows:
        try:
            value = _first_non_null(row.get("metric_value"), row.get("value"), _row_value(row))
            if value is None:
                continue
            total += float(value)
        except Exception:
            continue
    return total


def _ranking_scope_phrase(slots: SemanticSlots, ranking: Mapping[str, Any]) -> str:
    filter_name = str(ranking.get("filter_entity_label") or slots.relationship_entity_name or slots.selected_entity_name or "").strip()
    filter_type = str(ranking.get("filter_entity_type") or slots.relationship_entity_type or slots.secondary_entity_type or "").strip().lower()
    if not filter_name or not filter_type:
        return ""
    if filter_name and filter_name == filter_name.lower():
        filter_name = filter_name.title()
    labels = {
        "customers": "customer",
        "products": "product",
        "regions": "region",
        "suppliers": "supplier",
        "salesreps": "sales rep",
    }
    return f"within {filter_name}'s {labels.get(filter_type, filter_type)} scope" if filter_type != "salesreps" else f"within {filter_name}'s sales"


def _ranking_concentration_line(rows: Sequence[Mapping[str, Any]], *, metric: str) -> str:
    scoped_rows = [row for row in rows if isinstance(row, Mapping)]
    total = _rows_total(scoped_rows)
    if total <= 0 or not scoped_rows:
        return ""
    lead = scoped_rows[0]
    top_three = _rows_total(scoped_rows[:3])
    lead_value = _first_non_null(lead.get("metric_value"), lead.get("value"), _row_value(lead))
    try:
        lead_share = (float(lead_value or 0.0) / total) * 100.0
    except Exception:
        lead_share = 0.0
    top_three_share = (top_three / total) * 100.0 if total else 0.0
    if lead_share > 100.5 or top_three_share > 100.5:
        return "One concentration metric looks unusually high and should be validated before actioning."
    metric_label = _metric_label(metric)
    return (
        f"{_row_label(lead)} drives about {_fmt_percent(lead_share)} of the displayed {metric_label}; "
        f"the top 3 account for {_fmt_percent(top_three_share)}."
    )


def _nested_result_summary(nested_payload: Mapping[str, Any]) -> str:
    groups = [item for item in list(nested_payload.get("groups") or []) if isinstance(item, Mapping)]
    if not groups:
        return ""
    lead = groups[0]
    child_rows = [row for row in list(lead.get("children") or []) if isinstance(row, Mapping)]
    child_head = ", ".join(
        f"{_row_label(row)} ({_fmt_value(_first_non_null(row.get('metric_value'), row.get('value'), _row_value(row)))})"
        for row in child_rows[:3]
    )
    return (
        f"{_row_label(lead)} leads with {_fmt_value(_first_non_null(lead.get('metric_value'), _row_value(lead)))}"
        + (f"; top child entities: {child_head}." if child_head else ".")
    )


def _decorate_metric_rows(rows: Sequence[Mapping[str, Any]], *, metric: str) -> List[Dict[str, Any]]:
    decorated: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        item = dict(row)
        value = _first_non_null(item.get("metric_value"), item.get("value"), _row_value(item))
        item["display_value"] = _fmt_metric_value(metric, value)
        item["display_label"] = _row_label(item)
        item["metric_name"] = metric
        decorated.append(item)
    return decorated


def _decorate_nested_payload(nested_payload: Mapping[str, Any], *, metric: str) -> Dict[str, Any]:
    data = dict(nested_payload or {})
    groups: List[Dict[str, Any]] = []
    for group in list(data.get("groups") or []):
        if not isinstance(group, Mapping):
            continue
        item = dict(group)
        item["display_value"] = _fmt_metric_value(metric, _first_non_null(item.get("metric_value"), _row_value(item)))
        item["parent_label"] = str(item.get("parent_label") or _row_label(item)).strip() or "Parent"
        children: List[Dict[str, Any]] = []
        for child in list(item.get("children") or []):
            if not isinstance(child, Mapping):
                continue
            child_item = dict(child)
            child_item["display_value"] = _fmt_metric_value(
                metric,
                _first_non_null(child_item.get("metric_value"), child_item.get("child_metric_value"), child_item.get("value"), _row_value(child_item)),
            )
            child_item["display_label"] = str(child_item.get("label") or child_item.get("child_label") or _row_label(child_item)).strip() or "Child"
            children.append(child_item)
        item["children"] = children
        groups.append(item)
    data["groups"] = groups
    return data


def _metric_snapshot(module: str, results: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    summary_title = {
        "overview": "Overview Executive Briefing",
        "assistant": "Overview Executive Briefing",
        "customers": "Customer Portfolio Summary",
        "products": "Product Portfolio Summary",
        "regions": "Regional Summary",
        "suppliers": "Supplier Summary",
        "salesreps": "Sales Rep Portfolio Summary",
        "returns": "Returns Analytics Summary",
    }.get(module, "Module State Summary")
    kpi_title = {
        "overview": "Overview KPI Command Center",
        "assistant": "Overview KPI Command Center",
        "customers": "Customer KPI Summary",
        "products": "Product KPI Summary",
        "returns": "Returns Analytics Summary",
    }.get(module)

    summary = _pick_data(results, titles=(summary_title,))
    kpis = _pick_data(results, titles=(kpi_title,)) if kpi_title else {}
    revenue = _first_non_null(
        _tool_value(kpis, "kpis", "revenue"),
        kpis.get("revenue"),
        _tool_value(summary, "kpis", "revenue"),
        summary.get("revenue"),
    )
    profit = _first_non_null(
        _tool_value(kpis, "kpis", "profit"),
        kpis.get("profit"),
        _tool_value(summary, "kpis", "profit"),
        summary.get("profit"),
    )
    margin = _first_non_null(
        _tool_value(kpis, "kpis", "margin_pct"),
        kpis.get("margin_pct"),
        _tool_value(summary, "kpis", "margin_pct"),
        summary.get("margin_pct"),
    )
    orders = _first_non_null(
        _tool_value(kpis, "kpis", "orders"),
        kpis.get("orders"),
        _tool_value(summary, "kpis", "orders"),
        summary.get("orders"),
    )
    total_returns = _first_non_null(_tool_value(kpis, "summary", "total_returns"), _tool_value(summary, "summary", "total_returns"))
    return {
        "revenue": revenue,
        "profit": profit,
        "margin_pct": margin,
        "orders": orders,
        "total_returns": total_returns,
    }


def _metric_line(metrics: Mapping[str, Any]) -> str:
    segments: List[str] = []
    revenue = metrics.get("revenue")
    profit = metrics.get("profit")
    margin = metrics.get("margin_pct")
    orders = metrics.get("orders")
    total_returns = metrics.get("total_returns")
    if revenue is not None:
        segments.append(f"revenue {_fmt_metric_value('revenue', revenue)}")
    if profit is not None:
        segments.append(f"profit {_fmt_metric_value('profit', profit)}")
    if margin is not None:
        segments.append(f"margin {_fmt_metric_value('margin_pct', margin)}")
    if orders is not None:
        segments.append(f"orders {_fmt_metric_value('orders', orders)}")
    if total_returns is not None:
        segments.append(f"returns {_fmt_metric_value('total_returns', total_returns)}")
    return ", ".join(segments)


def _context_summary(results: Sequence[Mapping[str, Any]], module: str) -> Dict[str, Any]:
    ctx = _pick_data(results, titles=("Current Page Context",))
    page_state = ctx.get("page_state") if isinstance(ctx.get("page_state"), Mapping) else {}
    selected_entity = page_state.get("selected_entity") if isinstance(page_state.get("selected_entity"), Mapping) else {}
    label = str(selected_entity.get("label") or selected_entity.get("id") or "").strip()
    active_window = page_state.get("active_window") if isinstance(page_state.get("active_window"), Mapping) else {}
    window = str(active_window.get("label") or "").strip()
    if not window:
        first = next((item for item in results if isinstance(item.get("window_used"), Mapping)), {})
        used = first.get("window_used") if isinstance(first.get("window_used"), Mapping) else {}
        start = str(used.get("start") or "").strip()
        end = str(used.get("end") or "").strip()
        if start or end:
            window = f"{start or 'auto'} -> {end or 'auto'}"
    visible_sections = list(page_state.get("visible_sections") or []) if isinstance(page_state, Mapping) else []
    return {
        "entity_label": label,
        "window": window or "current scoped window",
        "visible_sections": visible_sections,
        "module_label": _module_label(module),
    }


def _scope_note(scope: Mapping[str, Any], permission_limited: bool) -> str:
    mode = str(scope.get("scope_mode") or "unknown").strip().lower()
    allowed_count = scope.get("allowed_count")
    if mode == "all":
        note = "Based on the current company-wide scope."
    elif mode == "list":
        count_text = f"{allowed_count}" if allowed_count is not None else "a limited set of"
        note = f"Based on your assigned scope ({count_text} entities)."
    elif mode == "none":
        note = "No scoped entities are configured for this account."
    else:
        note = "Based on the current scoped filters."
    if permission_limited:
        note = f"{note} Some results were narrowed by your permissions."
    return note


def _trust_note(trust_flags: Mapping[str, Any], results: Sequence[Mapping[str, Any]]) -> str:
    notes: List[str] = []
    if not trust_flags.get("cost"):
        notes.append("cost data is hidden for your role")
    if not trust_flags.get("profit"):
        notes.append("profit data is hidden for your role")
    if not trust_flags.get("margin"):
        notes.append("margin data is hidden for your role")
    for result in results:
        if str(result.get("title") or "") == "Data Trust And Health":
            data = result.get("data")
            if isinstance(data, Mapping):
                cov = data.get("cost_coverage_pct") or data.get("cost_coverage")
                if cov is not None:
                    notes.append(f"cost coverage is {_fmt_percent(cov)}")
                pack = data.get("pack_coverage_pct") or data.get("pack_coverage")
                if pack is not None:
                    notes.append(f"pack coverage is {_fmt_percent(pack)}")
    if not notes:
        return ""
    if len(notes) == 1:
        return notes[0][:1].upper() + notes[0][1:] + "."
    return "Caveats: " + "; ".join(notes[:3]) + "."


def _trust_caveat_items(
    trust_flags: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
    *,
    permission_limited: bool = False,
) -> List[str]:
    items: List[str] = []
    trust_line = _trust_note(trust_flags, results)
    if trust_line:
        items.append(trust_line)
    if permission_limited:
        items.append("Some underlying rows or metrics are outside your access scope.")
    return items


def _legacy_answer_type(question_type: str, presentation_type: str) -> str:
    token = str(question_type or "").strip().lower()
    if token == "definition_help":
        return "definition"
    if token == "page_help":
        return "help"
    if token == "history_analytics":
        return "history"
    if token == "comparison_analytics":
        return "comparison"
    if token == "ranking_analytics":
        return "ranking"
    if token == "grouped_analytics":
        return "grouped"
    if token in {"returns_analytics", "returns_workflow"}:
        return "returns"
    if token == "export_request":
        return "export"
    if token in {"executive_digest", "executive_summary"}:
        return "executive"
    return str(presentation_type or "summary").strip().lower() or "summary"


def _legacy_export_actions(export_actions: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    for item in export_actions:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or "").strip().lower()
        filename = str(item.get("filename") or "").strip()
        download_url = str(item.get("download_url") or item.get("api_download_url") or "").strip()
        status_url = str(item.get("status_url") or item.get("api_status_url") or "").strip()

        if download_url:
            actions.append(
                {
                    "kind": "download",
                    "label": f"Download {filename}" if filename else "Download export",
                    "url": download_url,
                    "status": status or "completed",
                    "filename": filename or None,
                }
            )
            continue

        if status_url:
            if status in {"pending", "running"}:
                label = f"Check export status for {filename}" if filename else "Check export status"
            elif status in {"rate_limited", "busy"}:
                label = f"Retry export for {filename}" if filename else "Retry export"
            else:
                label = f"View export status for {filename}" if filename else "View export status"
            actions.append(
                {
                    "kind": "status",
                    "label": label,
                    "url": status_url,
                    "status": status or None,
                    "filename": filename or None,
                }
            )

    return actions[:6]


def _synthesize_answer(
    message: str,
    results: Sequence[Dict[str, Any]],
    *,
    module: str,
    question_type: str,
    slots: SemanticSlots,
    permission_limited: bool,
    scope: Mapping[str, Any],
    trust_flags: Mapping[str, Any],
    followup: FollowupResolution,
) -> Dict[str, Any]:
    ok_results = [r for r in results if r.get("status") == "ok"]
    forbidden = [r for r in results if r.get("status") == "forbidden"]
    substantive = [r for r in ok_results if str(r.get("title") or "") not in _HELPER_TITLES]

    followups: List[str] = []
    actions: List[str] = []
    proactive_cards: List[Dict[str, Any]] = []
    risk_narratives: List[Dict[str, Any]] = []
    guided_investigations: List[Dict[str, Any]] = []
    next_best_questions: List[str] = []
    digest_payload: Dict[str, Any] = {}
    workflow_assist: Dict[str, Any] = {}
    trust_summary: Dict[str, Any] = {}
    export_actions: List[Dict[str, Any]] = []
    export_plan: Dict[str, Any] = {}
    export_columns: Dict[str, Any] = {}
    ranked_rows: List[Dict[str, Any]] = []
    grouped_rows: List[Dict[str, Any]] = []
    nested_results: Dict[str, Any] = {}
    detail_panels: List[Dict[str, Any]] = []
    detailed_breakdown: Dict[str, Any] = {}
    driver_breakdown: Dict[str, Any] = {}
    margin_breakdown: Dict[str, Any] = {}
    modify_preview: Dict[str, Any] = {}
    page_bundle_payload: Dict[str, Any] = {}
    for result in results:
        title = str(result.get("title") or "")
        data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
        if title == "Recommended Follow-up Questions":
            suggestions = _tool_value(data, "suggestions")
            if isinstance(suggestions, list):
                followups.extend(str(item) for item in suggestions if item)
        if title == "Next Best Questions":
            qs = _tool_value(data, "questions")
            if isinstance(qs, list):
                next_best_questions.extend(str(item) for item in qs if item)
        if title == "Proactive Insights":
            cards = _tool_value(data, "cards")
            if isinstance(cards, list):
                proactive_cards.extend(dict(item) for item in cards if isinstance(item, Mapping))
        if title == "Anomaly And Risk Narratives":
            ns = _tool_value(data, "narratives")
            if isinstance(ns, list):
                risk_narratives.extend(dict(item) for item in ns if isinstance(item, Mapping))
        if title == "Guided Investigation Paths":
            paths = _tool_value(data, "paths")
            if isinstance(paths, list):
                guided_investigations.extend(dict(item) for item in paths if isinstance(item, Mapping))
        if title in {"Executive Digest", "Manager Digest", "Leadership Summary"} and not digest_payload:
            digest_payload = dict(data)
        if title == "Workflow Assist Draft":
            workflow_assist = dict(data)
        if title == "Confidence And Trust Summary":
            trust_summary = dict(data)
        if title in {
            "Nested Rankings",
            "Top Customers With Top Products",
            "Top Suppliers With Top Products",
            "Top Sales Reps With Top Customers",
            "Top Regions With Top Products",
            "Top Products With Top Customers",
        } and not nested_results:
            nested_results = dict(data)
        if title == "Detailed Metric Breakdown":
            detailed_breakdown = dict(data)
        if title == "Entity Driver Breakdown":
            driver_breakdown = dict(data)
        if title == "Entity Margin Breakdown":
            margin_breakdown = dict(data)
        if title in {"Assistant Excel Export", "Assistant File Export", "Assistant Chart Export"}:
            result_status = str(result.get("status") or "").strip().lower()
            status_token = str(data.get("status") or "").strip().lower()
            export_id = data.get("export_id")
            job_id = data.get("job_id")
            download_url = data.get("download_url") or data.get("api_download_url")
            if not status_token:
                if job_id:
                    status_token = "pending"
                elif download_url or export_id:
                    status_token = "completed"
                elif result_status in {"error", "rate_limited", "busy", "empty", "forbidden"}:
                    status_token = result_status
                elif result_status == "ok":
                    status_token = "completed"
            if result_status == "forbidden" and status_token in {"", "forbidden"}:
                continue
            if not status_token and not (job_id or download_url or export_id):
                continue
            if not download_url and export_id:
                download_url = f"/ai/exports/{export_id}/download"
            export_actions.append(
                {
                    "export_id": export_id,
                    "job_id": job_id,
                    "status": status_token or None,
                    "filename": data.get("filename"),
                    "format": data.get("format"),
                    "status_url": data.get("status_url"),
                    "api_status_url": data.get("api_status_url"),
                    "download_url": download_url,
                    "api_download_url": data.get("api_download_url"),
                    "expires_at": data.get("expires_at"),
                    "sheets": data.get("sheets") or [],
                    "chart_count": data.get("chart_count"),
                    "chart_embedded": data.get("chart_embedded"),
                    "retry_after_seconds": data.get("retry_after_seconds"),
                }
            )
            if not export_plan and isinstance(data.get("export_plan"), Mapping):
                export_plan = dict(data.get("export_plan") or {})
        if title in {
            "Ranked Entities",
            "Top Regions",
            "Top Customers",
            "Top Products",
            "Top Suppliers",
            "Top Sales Reps",
            "Top Products For Customer",
            "Top Customers For Supplier",
            "Top Customers For Product",
            "Top Products For Supplier",
            "Top Products For Sales Rep",
            "Top Margin-Risk Products",
            "Top Decliners",
            "Top Gainers",
        }:
            rows = list(data.get("rows") or data.get("ranked_rows") or [])
            ranked_rows.extend(dict(item) for item in rows if isinstance(item, Mapping))
        if title in {"Grouped Metric Aggregation"}:
            rows = list(data.get("groups") or data.get("rows") or [])
            grouped_rows.extend(dict(item) for item in rows if isinstance(item, Mapping))
        if title == "Nested Rankings" and not ranked_rows:
            parent_rows = list(data.get("parents") or [])
            ranked_rows.extend(dict(item) for item in parent_rows if isinstance(item, Mapping))
        if title in {"Export Configuration Draft", "Refined Export Request", "Saved View Suggestion", "Analysis Bundle Request Draft", "Answer Mode Set", "Export Mode Set"}:
            modify_preview.setdefault("items", []).append({"title": title, "data": data})
            if title in {"Export Configuration Draft", "Refined Export Request"} and not export_plan:
                if isinstance(data.get("export_plan"), Mapping):
                    export_plan = dict(data.get("export_plan") or {})
                elif isinstance(data.get("configuration"), Mapping):
                    export_plan = dict(data.get("configuration") or {})
        if title == "Exportable Columns":
            export_columns = dict(data)
        if title in {"Page Bundle", "Current Page Visible State"}:
            page_bundle_payload.setdefault("items", []).append({"title": title, "data": data})
        for action in (result.get("next_actions") or []):
            actions.append(str(action))
        if title == "Priority Actions":
            action_rows = _tool_value(data, "actions")
            if isinstance(action_rows, list):
                actions.extend(str(item) for item in action_rows if item)

    if not workflow_assist:
        workflow_assist = _pick_data(results, titles=("Workflow Assist Draft",))

    context_info = _context_summary(results, module)
    module_label = str(context_info.get("module_label") or _module_label(module))
    entity_label = str(context_info.get("entity_label") or "").strip()
    window_label = str(context_info.get("window") or "current scoped window").strip()
    metric_text = _metric_line(_metric_snapshot(module, results))
    primary_metric = str(slots.metric or "revenue").strip().lower() or "revenue"
    risk_rows = list((_pick_data(results, titles=("Priority Risks",)).get("risks") or []))
    top_risk = risk_rows[0] if risk_rows and isinstance(risk_rows[0], Mapping) else {}
    top_risk_title = str(top_risk.get("title") or "").strip()
    top_risk_detail = str(top_risk.get("detail") or "").strip()
    top_risk_score = top_risk.get("risk_score")
    trust_caveats = _trust_caveat_items(trust_flags, results, permission_limited=permission_limited)

    if not substantive and forbidden:
        direct = "I can’t provide the full request with your current access permissions."
        explanation = "I only return data allowed by your module permissions, scope, and sensitive-data visibility."
    elif not ok_results:
        direct = "I don’t have enough data for that question in the current scope/window."
        explanation = "Try widening the date window or adjusting filters, then ask again."
    else:
        direct = ""
        explanation = ""
        if question_type == "definition_help":
            metric = _pick_result(results, titles=("Metric Definition", "Business Glossary Search"))
            raw = metric.get("data") if isinstance(metric, Mapping) else None
            first = raw[0] if isinstance(raw, list) and raw and isinstance(raw[0], Mapping) else {}
            term = str(first.get("term") or first.get("title") or first.get("metric") or "").strip()
            definition = str(first.get("definition") or first.get("description") or first.get("body") or "").strip()
            direct = f"{term}: {definition}" if term and definition else "I found the glossary definition for your metric question."
            explanation = "Definition/help answers come from curated glossary content and do not pull unrestricted business data."
        elif question_type == "page_help":
            page_help = _pick_data(results, titles=("Page Help",))
            matches = list(page_help.get("matches") or [])
            first = matches[0] if matches and isinstance(matches[0], Mapping) else {}
            title = str(first.get("title") or first.get("term") or "Page guidance").strip()
            detail = str(first.get("body") or first.get("description") or first.get("definition") or "").strip()
            direct = f"{title}: {detail}" if detail else "I pulled page guidance for the current module."
            explanation = "Use the follow-up prompts to jump to the next recommended drill path from this page."
        elif question_type == "page_bundle":
            sections = list(context_info.get("visible_sections") or [])
            entity_part = f" for {entity_label}" if entity_label else ""
            direct = f"I’m using the current {module_label}{entity_part} page context and {window_label} filters for this answer."
            if sections:
                explanation = f"The most relevant visible sections right now are {', '.join(sections[:4])}."
            else:
                explanation = "This answer is grounded in the current page filters, entity context, and visible analytics sections."
        elif question_type == "ranking_analytics":
            ranking = _pick_data(
                results,
                contains=(
                    "ranked entities",
                    "top regions",
                    "top customers",
                    "top products",
                    "top suppliers",
                    "top sales reps",
                    "top margin-risk products",
                    "top decliners",
                    "top gainers",
                ),
            )
            if slots.query_shape == "nested_ranking" and nested_results:
                metric = str(nested_results.get("metric") or slots.metric or "revenue").strip()
                parent_dimension = str(nested_results.get("parent_type") or slots.parent_entity_type or slots.primary_entity_type or module_label).strip()
                child_dimension = str(nested_results.get("child_type") or slots.child_entity_type or "").strip()
                groups = [item for item in list(nested_results.get("groups") or []) if isinstance(item, Mapping)]
                if permission_limited and not groups:
                    direct = "I can’t provide the full parent/child ranking with your current access permissions."
                    explanation = "Nested detail for one or more requested modules or metrics is hidden by your role, scope, or sensitive-data visibility."
                else:
                    parent_count = min(len(groups), max(1, int(slots.parent_limit_n or slots.limit_n or 5)))
                    child_count = max(1, int(slots.child_limit_n or 5))
                    metric_label = _metric_label(metric)
                    direct = (
                        f"Top {parent_count} {parent_dimension} by {metric_label} are shown below, with each one’s top {child_count} {child_dimension}."
                    )
                    summary_line = _nested_result_summary(nested_results)
                    if summary_line:
                        direct = f"{direct} {summary_line}"
                    render_strategy = str(nested_results.get("render_strategy") or "inline").strip().lower()
                    filter_phrase = _ranking_scope_phrase(slots, nested_results)
                    explanation_bits = []
                    if filter_phrase:
                        explanation_bits.append(f"This view is scoped {filter_phrase}.")
                    if render_strategy in {"compact_summary", "export_recommended"}:
                        explanation_bits.append("The inline view is compacted for readability; use export for the full parent-and-child detail.")
                    else:
                        explanation_bits.append(f"It reflects the current filters and {window_label}.")
                    explanation = " ".join(bit for bit in explanation_bits if bit)
            else:
                dimension = str(ranking.get("dimension") or slots.group_by_dimension or slots.primary_entity_type or module_label).strip()
                metric = str(ranking.get("metric") or slots.metric or "revenue").strip()
                direction = str(ranking.get("direction") or slots.ranking_direction or "top").strip().lower()
                rows = list(ranking.get("rows") or ranking.get("ranked_rows") or ranked_rows)
                if permission_limited and not rows:
                    direct = "I can’t provide that ranking with your current access permissions."
                    explanation = "The requested ranking depends on a restricted module or a sensitive metric that is hidden for your role."
                else:
                    first_rows = [row for row in rows if isinstance(row, Mapping)][:3]
                    head = "; ".join(
                        f"{_row_label(row)} ({_fmt_metric_value(metric, _first_non_null(row.get('metric_value'), _row_value(row)))})"
                        for row in first_rows
                    )
                    filter_phrase = _ranking_scope_phrase(slots, ranking)
                    metric_label = _metric_label(metric)
                    direction_label = "Top" if direction != "bottom" else "Bottom"
                    scope_text = f" {filter_phrase}" if filter_phrase else ""
                    direct = (
                        f"{direction_label} {min(len(rows), max(1, int(slots.limit_n)))} {dimension} by {metric_label}{scope_text}"
                        + (f": {head}." if head else ".")
                    )
                    explanation = f"This reflects the current filters and {window_label}."
                    concentration_line = _ranking_concentration_line(rows, metric=metric)
                    if concentration_line:
                        explanation = f"{explanation} {concentration_line}"
        elif question_type == "grouped_analytics":
            grouped = _pick_data(results, titles=("Grouped Metric Aggregation",))
            dimension = str(grouped.get("dimension") or slots.group_by_dimension or slots.primary_entity_type or module_label).strip()
            metric = str(grouped.get("metric") or slots.metric or "revenue").strip()
            rows = list(grouped.get("groups") or grouped.get("rows") or grouped_rows)
            if permission_limited and not rows:
                direct = "I can’t provide that grouped metric with your current access permissions."
                explanation = "The requested grouped result depends on a restricted module or a sensitive metric that is hidden for your role."
            else:
                first_rows = [row for row in rows if isinstance(row, Mapping)][:4]
                snapshot = "; ".join(
                    f"{_row_label(row)} {_fmt_metric_value(metric, _first_non_null(row.get('value'), row.get('metric_value'), _row_value(row)))}"
                    for row in first_rows
                )
                direct = f"{_metric_label(metric).title()} by {dimension}: {snapshot or 'no grouped rows available'}."
                explanation = f"This grouped view reflects the current filters and {window_label}."
        elif question_type == "page_summary":
            entity_part = f" for {entity_label}" if entity_label else ""
            baseline = f"{module_label}{entity_part} in {window_label}"
            risk_clause = f" Top risk: {top_risk_title}." if top_risk_title else ""
            direct = f"{baseline}: {metric_text or 'core KPIs were retrieved'}{risk_clause}"
            explanation = "This is the current business snapshot based on the page filters, visible KPIs, and active watchouts."
        elif question_type in {"live_analytics", "analyst_detail"}:
            primary = substantive[0] if substantive else ok_results[0]
            title = str(primary.get("title") or "analytics result").strip()
            highlights = _extract_highlights(primary, max_items=4)
            highlight_line = ", ".join(
                f"{_metric_label(str(item.get('label') or 'metric'))} {_fmt_metric_value(str(item.get('label') or ''), item.get('value'))}"
                for item in highlights
                if isinstance(item, Mapping) and item.get("label")
            )
            entity_part = f" for {entity_label}" if entity_label else ""
            direct = (
                f"{module_label}{entity_part}: {title}."
                f" {highlight_line if highlight_line else (metric_text or 'Scoped KPIs were retrieved')}."
            ).strip()
            explanation = "This summary is grounded in the current page context and permission-scoped analytics."
            comparison = _pick_data(results, titles=("Period Comparison", "Entity Period Comparison"))
            compare_fields = comparison.get("comparison") if isinstance(comparison.get("comparison"), Mapping) else comparison
            if isinstance(compare_fields, Mapping) and compare_fields:
                top_fields = _compact_comparison_line(compare_fields, metric=primary_metric)
                if top_fields:
                    explanation = f"{explanation} {top_fields}"
        elif question_type == "driver_mover":
            pvm = _pick_data(results, titles=("Price / Volume / Mix Drivers",))
            bucket = pvm.get("mom") if isinstance(pvm.get("mom"), Mapping) else pvm
            contributions = {
                "price": _to_float(bucket.get("price"), 0.0),
                "volume": _to_float(bucket.get("volume"), 0.0),
                "mix": _to_float(bucket.get("mix"), 0.0),
            } if isinstance(bucket, Mapping) else {}
            driver_name = ""
            driver_value = None
            if contributions:
                ranked = sorted(contributions.items(), key=lambda item: abs(item[1]), reverse=True)
                driver_name, driver_value = ranked[0]
            movers = _pick_data(results, contains=("top movers", "regional movers"))
            gainers = list(movers.get("gainers") or movers.get("rows") or [])
            decliners = list(movers.get("decliners") or [])
            gainer = gainers[0] if gainers and isinstance(gainers[0], Mapping) else {}
            decliner = decliners[0] if decliners and isinstance(decliners[0], Mapping) else {}
            gainer_text = f"{_row_label(gainer)} ({_fmt_metric_value(primary_metric, _row_value(gainer))})" if gainer else ""
            decliner_text = f"{_row_label(decliner)} ({_fmt_metric_value(primary_metric, _row_value(decliner))})" if decliner else ""
            change_explanation = _pick_data(results, titles=("Entity Change Explanation",))
            change_summary = str(change_explanation.get("summary") or "").strip()
            comparison = (
                change_explanation.get("comparison")
                if isinstance(change_explanation.get("comparison"), Mapping)
                else _pick_data(results, titles=("Period Comparison", "Entity Period Comparison")).get("comparison")
            )
            comparison = comparison if isinstance(comparison, Mapping) else {}
            comparison_line = _compact_comparison_line(comparison, metric=primary_metric) if comparison else ""
            entity_detail = change_explanation.get("entity_detail") if isinstance(change_explanation.get("entity_detail"), Mapping) else {}
            entity_rows = [row for row in list(entity_detail.get("top_rows") or []) if isinstance(row, Mapping)]
            entity_row_text = "; ".join(
                f"{_row_label(row)} {_fmt_metric_value(primary_metric, _row_value(row))}"
                for row in entity_rows[:3]
                if _row_value(row) is not None
            )
            usable_change_summary = change_summary if "explained using period comparison" not in change_summary.lower() else ""
            if module not in {"overview", "assistant"} and (comparison_line or usable_change_summary or entity_row_text):
                if comparison_line:
                    direct = comparison_line[0].upper() + comparison_line[1:]
                elif usable_change_summary:
                    direct = usable_change_summary
                else:
                    direct = f"I found the main drivers behind the recent change in {module_label}."
            elif driver_name:
                direct = f"{driver_name.title()} is the main reason {_metric_label(primary_metric)} moved in {module_label}."
            else:
                direct = f"I found the main movers behind the recent change in {module_label}."
            detail_bits = []
            if usable_change_summary and usable_change_summary != direct:
                detail_bits.append(usable_change_summary)
            if driver_name:
                detail_bits.append(f"{driver_name.title()} contributed {_fmt_metric_value(primary_metric, driver_value)} in the current decomposition.")
            if comparison_line and comparison_line != direct:
                detail_bits.append(comparison_line)
            if gainer_text:
                detail_bits.append(f"Biggest positive contributor: {gainer_text}.")
            if decliner_text:
                detail_bits.append(f"Biggest drag: {decliner_text}.")
            if entity_row_text:
                detail_bits.append(f"Current entities to review: {entity_row_text}.")
            explanation = " ".join(detail_bits) or "I found mover detail, but the decomposition is partial for this slice."
        elif question_type == "risk_watchout":
            severity = str(top_risk.get("severity") or "medium").strip().lower() if isinstance(top_risk, Mapping) else "medium"
            if top_risk_title:
                direct = f"The main risk in {module_label} right now is {top_risk_title}."
                explanation = top_risk_detail or f"It is currently flagged as {severity} severity."
            else:
                watchouts = _pick_data(results, contains=("watchout", "risk watchlist", "entity watchouts"))
                watch_rows = list(watchouts.get("watchouts") or watchouts.get("risk_rows") or [])
                direct = "No single risk signal is dominating this view right now."
                explanation = (
                    f"I still found {_fmt_metric_value('count', len(watch_rows))} watchout rows worth monitoring."
                    if watch_rows
                    else "No material watchouts cleared the current threshold."
                )
        elif question_type == "proactive_insights":
            top_cards = [item for item in proactive_cards if isinstance(item, Mapping)]
            if permission_limited and not top_cards and not top_risk_title:
                direct = "I can’t provide the full request with your current access permissions."
                explanation = "I only return data allowed by your module permissions, scope, and sensitive-data visibility."
            elif top_cards:
                first_card = top_cards[0]
                direct = str(first_card.get("narrative") or first_card.get("title") or "The assistant found a material signal worth attention.").strip()
                extra = [
                    str(item.get("title") or item.get("narrative") or "").strip()
                    for item in top_cards[1:3]
                    if str(item.get("title") or item.get("narrative") or "").strip()
                ]
                explanation = " ".join(
                    bit
                    for bit in (
                        f"Most important right now: {str(first_card.get('title') or 'primary signal').strip()}." if first_card.get("title") else "",
                        f"Also watch: {'; '.join(extra)}." if extra else "",
                    )
                    if bit
                )
            elif top_risk_title:
                direct = f"The highest-signal issue right now is {top_risk_title}."
                explanation = top_risk_detail or "It stands out because it has the strongest current risk signal in your scope."
            else:
                direct = "Nothing unusually material is standing out right now."
                explanation = "The current watchouts are either low-confidence or below the materiality threshold."
        elif question_type == "forecast_outlook":
            forecast = _pick_data(results, titles=("Forecast Outlook",))
            enabled = bool(forecast.get("enabled"))
            if enabled:
                detail = ", ".join(
                    f"{_metric_label(key)} {_fmt_metric_value(key, value)}"
                    for key, value in list(forecast.items())[:4]
                    if value is not None
                )
                direct = f"A forecast is available for this selection. {detail}".strip()
                explanation = "Use it directionally and pair it with coverage checks before making a commitment."
            else:
                direct = "A reliable forecast is not available for this selection yet."
                explanation = "That usually means the historical series is too thin, too sparse, or too noisy to support a dependable outlook."
        elif question_type == "trust_quality":
            health = _pick_data(results, titles=("Data Trust And Health",))
            confidence = _pick_data(results, titles=("Confidence And Trust Summary",))
            cov = _first_non_null(health.get("cost_coverage_pct"), health.get("cost_coverage"))
            pack = _first_non_null(health.get("pack_coverage_pct"), health.get("pack_coverage"))
            conf_score = confidence.get("confidence_score")
            conf_level = confidence.get("confidence_level")
            if conf_level:
                direct = f"Data quality for this selection looks {str(conf_level).lower()}."
            else:
                direct = "Data quality for this selection is mixed."
            trust_bits = []
            if cov is not None:
                trust_bits.append(f"Cost coverage is {_fmt_percent(cov)}.")
            if pack is not None:
                trust_bits.append(f"Pack coverage is {_fmt_percent(pack)}.")
            if conf_score is not None:
                trust_bits.append(f"Overall confidence is {_fmt_percent(conf_score)}.")
            explanation = " ".join(trust_bits) or "Use extra caution if you are making margin or cost decisions from this slice."
        elif question_type == "history_analytics":
            history = _pick_result(results, contains=(" history",))
            history_data = history.get("data") if isinstance(history, Mapping) and isinstance(history.get("data"), Mapping) else {}
            trend = history_data.get("trend") if isinstance(history_data.get("trend"), Mapping) else {}
            labels = list(trend.get("labels") or [])
            values = list(trend.get("values") or trend.get("revenue") or trend.get("series") or [])
            rows = list(history_data.get("history_rows") or [])
            if not rows:
                table = history_data.get("table")
                if isinstance(table, Mapping):
                    rows = list(table.get("rows") or [])
            entity = str(history_data.get("entity") or entity_label or module_label).strip()
            latest = values[-1] if values else None
            comparison = _pick_data(results, titles=("Entity Period Comparison", "Period Comparison"))
            summary = comparison.get("comparison") if isinstance(comparison.get("comparison"), Mapping) else comparison
            history_explanation = _pick_data(results, titles=("History Explanation", "Entity Change Explanation"))
            history_summary = str(history_explanation.get("summary") or history_explanation.get("explanation") or "").strip()
            if not values and not rows:
                direct = f"I don’t have a usable historical series for {entity} in the current selection."
                if isinstance(summary, Mapping) and summary:
                    explanation = (
                        f"Instead, the available period comparison shows {_compact_comparison_line(summary, metric=primary_metric)}"
                    )
                elif metric_text:
                    explanation = f"The best available view is the current snapshot: {metric_text}."
                else:
                    explanation = "Try widening the date window or relaxing filters to recover a usable trend."
            else:
                point_count = len(labels) or len(rows)
                first_value = values[0] if values else None
                direction = "up" if latest is not None and first_value is not None and _to_float(latest) > _to_float(first_value) else "down" if latest is not None and first_value is not None and _to_float(latest) < _to_float(first_value) else "mixed"
                direct = (
                    f"{entity} is {direction} over the selected period, with the latest point at "
                    f"{_fmt_metric_value(primary_metric, latest)} across {_fmt_metric_value('count', point_count)} observations."
                )
                if history_summary:
                    explanation = history_summary
                elif isinstance(summary, Mapping) and summary:
                    explanation = _compact_comparison_line(summary, metric=primary_metric)
                else:
                    explanation = f"This trend reflects the current filters and {window_label}."
        elif question_type == "comparison_analytics":
            comparison = _pick_data(results, contains=(" comparison",))
            top_rows = list(comparison.get("top") or [])
            bottom_rows = list(comparison.get("bottom") or [])
            metric = str(comparison.get("metric") or "revenue").strip()
            dimension = str(comparison.get("dimension") or comparison.get("module") or module_label).strip()
            top = top_rows[0] if top_rows and isinstance(top_rows[0], Mapping) else {}
            bottom = bottom_rows[0] if bottom_rows and isinstance(bottom_rows[0], Mapping) else {}
            top_text = f"{_row_label(top)} ({_fmt_metric_value(metric, _row_value(top))})" if top else ""
            bottom_text = f"{_row_label(bottom)} ({_fmt_metric_value(metric, _row_value(bottom))})" if bottom else ""
            direct = f"The biggest upside in the {dimension} comparison is {top_text or 'not available'}."
            period = _pick_data(results, titles=("Period Comparison", "Entity Period Comparison"))
            if period:
                snippet = period.get("comparison") if isinstance(period.get("comparison"), Mapping) else period
                if isinstance(snippet, Mapping) and snippet:
                    explanation = _compact_comparison_line(snippet, metric=metric)
                else:
                    explanation = "The comparison combines entity-level movers with the selected comparison period."
            else:
                explanation = "The comparison uses only the entities visible in your current scope."
            if bottom_text:
                explanation = f"{explanation} The biggest downside is {bottom_text}."
        elif question_type in {"cross_module", "risk_action"}:
            investigations = _pick_data(results, titles=("Priority Investigations",))
            risk_summary = _pick_data(results, titles=("Cross-Module Risk Summary",))
            actions_payload = _pick_data(results, titles=("Priority Actions",))
            inv_rows = list(investigations.get("investigations") or [])
            top_issue = inv_rows[0] if inv_rows and isinstance(inv_rows[0], Mapping) else {}
            top_title = str(top_issue.get("title") or "").strip()
            top_module = str(top_issue.get("module") or "").strip()
            top_detail = str(top_issue.get("detail") or "").strip()
            if top_title:
                direct = f"The top priority right now is {top_title}."
                explanation = top_detail or f"It is concentrated in {top_module or 'the current scope'}."
            else:
                direct = "No single issue clearly dominates the cross-functional view right now."
                explanation = "I still reviewed the available investigations and actions in your current scope."
            summary_line = str(risk_summary.get("summary") or "").strip()
            if summary_line:
                explanation = f"{explanation} {summary_line}"
            action_rows = list(actions_payload.get("actions") or [])
            if action_rows:
                explanation = f"{explanation} First move: {action_rows[0]}"
        elif question_type == "returns_analytics":
            returns_summary = _pick_data(results, titles=("Returns Analytics Summary",))
            pending = _pick_data(results, titles=("Pending Returns And Approvals",))
            reasons = _pick_data(results, titles=("Returns Reason Patterns",))
            total_returns = _first_non_null(
                returns_summary.get("total_returns"),
                _tool_value(returns_summary, "summary", "total_returns"),
            )
            pending_count = _first_non_null(pending.get("pending_count"), pending.get("count"))
            top_reasons = list(reasons.get("top_reasons") or reasons.get("reasons") or [])
            top_reason = top_reasons[0] if top_reasons and isinstance(top_reasons[0], Mapping) else {}
            reason_label = str(top_reason.get("reason") or top_reason.get("name") or "").strip()
            direct = (
                f"Returns are running at {_fmt_metric_value('total_returns', total_returns)} with "
                f"{_fmt_metric_value('pending_count', pending_count)} pending approvals."
            )
            explanation = "This view combines queue status and reason patterns so you can triage what needs attention first."
            if reason_label:
                explanation = f"{explanation} The main reason pattern is {reason_label}."
        elif question_type == "export_request":
            if export_actions:
                first = export_actions[0]
                export_status = str(first.get("status") or "completed").strip().lower()
                if export_status in {"pending", "running"}:
                    direct = "Your export is queued and still running."
                    explanation = "The file is being generated in the background so the page stays responsive. The download card below will update automatically."
                elif export_status in {"rate_limited", "busy", "error"}:
                    direct = "Export request was accepted but generation is currently limited."
                    retry_seconds = first.get("retry_after_seconds")
                    if retry_seconds is not None:
                        explanation = f"Please retry in about {_fmt_value(retry_seconds)} seconds, or narrow the export scope."
                    else:
                        explanation = "Please retry shortly, or narrow the export scope to reduce workload."
                else:
                    direct = f"Your workbook is ready: {first.get('filename') or 'assistant_export.xlsx'}."
                    fmt = str(first.get("format") or "").strip().lower()
                    if fmt:
                        direct = f"{direct[:-1]} ({fmt})."
                    if ranked_rows:
                        explanation = "The workbook includes the ranked result plus the current scoped metadata."
                    elif grouped_rows:
                        explanation = "The workbook includes the grouped breakdown plus the current scoped metadata."
                    else:
                        explanation = "You can download it now or refine the workbook contents without changing production data."
                if export_plan:
                    plan_sheets = list(export_plan.get("sheets") or [])
                    if plan_sheets and isinstance(plan_sheets[0], Mapping):
                        sheet_names = ", ".join(str(item.get("name") or "") for item in plan_sheets[:6] if str(item.get("name") or "").strip())
                    else:
                        sheet_names = ", ".join(str(item) for item in plan_sheets[:6] if str(item).strip())
                    if sheet_names:
                        explanation = f"{explanation} Included sheets: {sheet_names}."
                if bool(first.get("chart_count")):
                    explanation = f"{explanation} Charts requested: {_fmt_metric_value('count', first.get('chart_count'))}."
            else:
                opts = _pick_data(results, titles=("Export Options",))
                options = list(opts.get("options") or [])
                direct = f"Export options are available for {module_label}, but no workbook was generated yet."
                explanation = "Available exports: " + ", ".join(str(item.get("label")) for item in options[:4] if isinstance(item, Mapping))
        elif question_type == "modify_request":
            direct = "I prepared a reviewable change draft rather than making the change directly."
            explanation = "Nothing was applied automatically. Review the suggested export, view, or analysis changes before proceeding."
        elif question_type == "scheduled_digest":
            schedule = _pick_data(results, titles=("Digest Schedule Created", "Digest Schedules", "Scheduled Digest Run"))
            if schedule.get("schedule_id"):
                direct = "The digest schedule was created successfully."
            elif schedule.get("run_id") or schedule.get("digest"):
                direct = "The digest run completed for the requested schedule."
            elif schedule.get("schedules"):
                direct = "I found the available digest schedules for your scope."
            else:
                direct = "I processed the digest scheduling request."
            if schedule:
                explanation = "Scheduling actions are permission-checked and auditable, so the result is limited to the modules and audience you are allowed to manage."
            else:
                explanation = "I could not confirm the schedule details. Try specifying cadence, audience, and module more explicitly."
        elif question_type == "anomaly_risk":
            first = risk_narratives[0] if risk_narratives else {}
            if first:
                direct = str(first.get("narrative") or first.get("title") or "Anomaly narrative is available.")
                explanation = str(first.get("why_unusual") or "This was flagged because it cleared the current anomaly or materiality threshold.")
            else:
                direct = "No strong anomaly narratives exceeded configured materiality thresholds in this scope."
                explanation = "Signals under threshold were suppressed to reduce low-value noise."
        elif question_type == "guided_investigation":
            first = guided_investigations[0] if guided_investigations else {}
            if first:
                direct = f"Best next step: {first.get('title') or 'Open the top guided path'}."
                explanation = str(first.get("why") or first.get("question") or "This is the cleanest next investigation path from the current evidence.")
            else:
                direct = "No permission-valid guided path was found for this request."
                explanation = "Try narrowing to a specific entity/module for stronger path recommendations."
        elif question_type in {"executive_digest", "executive_summary"}:
            summary_text = str(digest_payload.get("executive_summary") or "").strip()
            if not summary_text:
                executive = _pick_data(results, titles=("Executive Summary",))
                summary_text = str(executive.get("summary") or executive.get("module") or "").strip()
            direct = summary_text or f"{module_label} executive digest is ready for leadership."
            explanation = "This is a leadership-ready summary of the biggest movement, the main risk, and the next action."
        elif question_type == "workflow_assist":
            lines = list(workflow_assist.get("body_lines") or [])
            direct = f"A workflow draft is ready with {_fmt_metric_value('count', len(lines))} prepared lines."
            explanation = "It is review-only and can be edited before you share it or use it in a workflow."
        elif question_type == "returns_workflow":
            pending = _pick_data(results, titles=("Pending Returns And Approvals",))
            pending_count = pending.get("pending_count")
            direct = f"There are {_fmt_metric_value('pending_count', pending_count)} returns approvals waiting in {window_label}."
            explanation = "Use the queue and status detail to triage the highest-impact items first."
        else:
            primary = substantive[0] if substantive else ok_results[0]
            title = str(primary.get("title") or "Result").strip()
            direct = f"{module_label} analysis returned {title.lower()} for {window_label}."
            explanation = "Ask a follow-up for historical comparison, risk ranking, export, or deeper driver explanation."

        if question_type in {"ranking_analytics", "analyst_detail"}:
            detail_bits: List[str] = []
            concentration = str(detailed_breakdown.get("concentration_note") or "").strip()
            if concentration:
                detail_bits.append(concentration)
            breakdown_totals = detailed_breakdown.get("totals") if isinstance(detailed_breakdown.get("totals"), Mapping) else {}
            if breakdown_totals:
                total_line = ", ".join(
                    f"{_metric_label(key)} {_fmt_metric_value(key, value)}"
                    for key, value in list(breakdown_totals.items())[:4]
                    if value is not None
                )
                if total_line:
                    detail_bits.append(f"Totals: {total_line}.")
            driver_rows = list(driver_breakdown.get("rows") or [])
            if driver_rows:
                driver_head = "; ".join(
                    f"{_row_label(row)} revenue {_fmt_metric_value('revenue', row.get('revenue'))}"
                    for row in driver_rows[:3]
                    if isinstance(row, Mapping)
                )
                if driver_head:
                    detail_bits.append(f"Driver view: {driver_head}.")
            margin_rows = list(margin_breakdown.get("rows") or [])
            if margin_rows:
                margin_head = "; ".join(
                    f"{_row_label(row)} margin {_fmt_metric_value('margin_pct', _first_non_null(row.get('margin_pct'), row.get('margin')))}"
                    for row in margin_rows[:3]
                    if isinstance(row, Mapping)
                )
                if margin_head:
                    detail_bits.append(f"Margin lens: {margin_head}.")
            if detail_bits:
                explanation = f"{explanation} {' '.join(detail_bits)}".strip()
        if question_type == "export_request" and nested_results:
            group_count = len(list(nested_results.get("groups") or []))
            if group_count:
                explanation = f"{explanation} Hierarchical export includes {group_count} parent groups plus child detail rows."
        if not explanation:
            notes: List[str] = []
            for result in ok_results[:4]:
                for note in (result.get("notes") or [])[:1]:
                    notes.append(str(note))
            explanation = " ".join(notes) if notes else "Ask a follow-up to drill into drivers, risk, dependency, or actions."

    if followup.request_simpler:
        explanation = f"In plain English: {direct} {explanation}".strip()
    elif followup.request_more_detail and results:
        explanation = f"{explanation} I added the deeper supporting detail that is available for this slice."
    if followup.response_mode == "analyst":
        analyst_additions: List[str] = []
        if ranked_rows:
            concentration_line = _ranking_concentration_line(ranked_rows[:10], metric=str(slots.metric or "revenue"))
            if concentration_line:
                analyst_additions.append(concentration_line)
            margin_bits = []
            for item in ranked_rows[:3]:
                source = item.get("row") if isinstance(item.get("row"), Mapping) else item
                margin = _first_non_null(source.get("margin_pct"), item.get("margin_pct"))
                profit = _first_non_null(source.get("profit"), item.get("profit"))
                if margin is None and profit is None:
                    continue
                margin_bits.append(
                    f"{_row_label(source)} margin {_fmt_metric_value('margin_pct', margin)} profit {_fmt_metric_value('profit', profit)}"
                )
            if margin_bits:
                analyst_additions.append("Margin lens: " + "; ".join(margin_bits) + ".")
        elif nested_results:
            nested_line = _nested_result_summary(nested_results)
            if nested_line:
                analyst_additions.append(f"Hierarchy check: {nested_line}")
        if analyst_additions:
            explanation = f"{explanation} {' '.join(analyst_additions)}"
        explanation = f"{explanation} Analyst mode adds concentration, margin, and driver context so you can validate the result before escalating it."
    elif followup.response_mode == "executive":
        explanation = f"{explanation} Executive mode keeps the focus on business impact, risk, and the next action."

    evidence_cards = []
    for result in results[:8]:
        if str(result.get("title") or "") in _HELPER_TITLES and result.get("status") == "ok":
            continue
        evidence_cards.append(
            {
                "title": result.get("title"),
                "status": result.get("status"),
                "module": result.get("module"),
                "scope": result.get("scope_used"),
                "window": result.get("window_used"),
                "highlights": _extract_highlights(result),
                "notes": list(result.get("notes") or [])[:3],
            }
        )
    top_entity_label = ""
    if ranked_rows:
        first_ranked = ranked_rows[0] if isinstance(ranked_rows[0], Mapping) else {}
        top_entity_label = _row_label(first_ranked)
    elif nested_results and list(nested_results.get("groups") or []):
        first_group = next((item for item in list(nested_results.get("groups") or []) if isinstance(item, Mapping)), {})
        top_entity_label = str(first_group.get("parent_label") or _row_label(first_group)).strip()

    if not followups:
        defaults_by_type: Dict[str, List[str]] = {
            "history_analytics": [
                "Compare that with last year.",
                "Which entities drove the biggest change?",
                "Export this history to Excel.",
                "Summarize this trend for leadership.",
            ],
            "comparison_analytics": [
                "Show the driver breakdown behind this difference.",
                "Compare the same entities over full history.",
                "Export this comparison workbook.",
                "What should we investigate next?",
            ],
            "ranking_analytics": [
                "Show the bottom entities by the same metric.",
                "Switch metric to profit.",
                "Use full history instead of current window.",
                "Export this ranked list to Excel.",
            ],
            "grouped_analytics": [
                "Show top entities in this grouped view.",
                "Switch grouped metric to margin.",
                "Compare this grouped result with last year.",
                "Export this grouped table to Excel.",
            ],
            "export_request": [
                "Include trends and risk sheets in that export.",
                "Make this export leadership-friendly.",
                "Use full history in the workbook.",
                "Create a customer-only version.",
            ],
            "risk_watchout": [
                "Which entities are highest risk right now?",
                "What actions should I prioritize first?",
                "Show risk trend over time.",
                "Summarize this risk for leadership.",
            ],
            "driver_mover": [
                "Which customers drove this change?",
                "Show only products.",
                "Compare this with last quarter.",
                "Export the movers table.",
            ],
            "returns_analytics": [
                "What approvals are pending right now?",
                "Which return reasons are growing?",
                "Explain the returns workflow steps.",
                "Export returns summary to Excel.",
            ],
        }
        followups = defaults_by_type.get(
            question_type,
            [
                "Explain that in simpler terms.",
                "Show more detail.",
                "Summarize for leadership.",
                "What should I do next?",
            ],
        )
        if question_type == "ranking_analytics" and slots.query_shape == "nested_ranking":
            followups = [
                "Export this hierarchical result to Excel.",
                "Compare these parent entities with the prior period.",
                "Show history for the top parent entity.",
                "Give me an analyst version of this hierarchy.",
            ]
        if not trust_flags.get("profit"):
            followups = [item for item in followups if "profit" not in str(item).lower()]
        if not trust_flags.get("margin"):
            followups = [item for item in followups if "margin" not in str(item).lower()]
        if permission_limited and question_type in {"ranking_analytics", "grouped_analytics"}:
            followups.append("Switch to a revenue-based view.")
            followups.append("Show only metrics available to my role.")
    if next_best_questions:
        followups.extend(next_best_questions[:6])
    if top_entity_label:
        if question_type == "ranking_analytics":
            followups.append(f"Show history for {top_entity_label}.")
        if question_type in {"history_analytics", "comparison_analytics", "driver_mover"}:
            followups.append(f"Which customers drove {top_entity_label}?")

    deduped_followups: List[str] = []
    blocked_followup_tokens: List[str] = []
    if not trust_flags.get("profit"):
        blocked_followup_tokens.append("profit")
    if not trust_flags.get("margin"):
        blocked_followup_tokens.append("margin")
    for item in followups:
        token = str(item).strip()
        lowered = token.lower()
        if blocked_followup_tokens and any(marker in lowered for marker in blocked_followup_tokens):
            continue
        if token and token not in deduped_followups:
            deduped_followups.append(token)
    deduped_actions: List[str] = []
    for item in actions:
        token = str(item).strip()
        if token and token not in deduped_actions:
            deduped_actions.append(token)

    if not deduped_actions and question_type in {"risk_action", "cross_module", "proactive_insights", "risk_watchout"}:
        if top_entity_label:
            deduped_actions.append(f"Review {top_entity_label} first to confirm the size of the issue and the quickest containment step.")
        elif top_risk_title:
            deduped_actions.append(f"Validate {top_risk_title} first before escalating it.")
        deduped_actions.append("Compare the current result with the prior period to separate a one-off issue from a sustained shift.")
        if question_type != "returns_analytics":
            deduped_actions.append("Assign an owner for the top issue and track the next update window.")

    trust_line = _scope_note(scope, permission_limited)
    trust_note = _trust_note(trust_flags, results)
    if trust_summary.get("confidence_score") is not None:
        confidence_line = (
            f"Assistant confidence for this slice is {_fmt_percent(trust_summary.get('confidence_score'))}"
            + (
                f" ({str(trust_summary.get('confidence_level') or '').lower()})"
                if str(trust_summary.get("confidence_level") or "").strip()
                else ""
            )
            + "."
        )
        if confidence_line not in trust_caveats:
            trust_caveats.append(confidence_line)

    methodology_line = {
        "history_analytics": "Trend summary uses the available historical series first, then period comparison when the series is incomplete.",
        "comparison_analytics": "Comparison answers combine the requested comparison period with the biggest positive and negative movers.",
        "ranking_analytics": "Rankings reflect the current filters, scope, and requested metric.",
        "grouped_analytics": "Grouped answers aggregate the current scoped rows by the requested business dimension.",
        "driver_mover": "Driver analysis uses the available decomposition and top movers for the current slice.",
        "proactive_insights": "Proactive insight output is limited to the highest-signal issues in the current scope.",
        "risk_action": "Actions are prioritized from the available risk and investigation signals.",
        "export_request": "Export output is scoped to your current permissions and requested workbook settings.",
    }.get(question_type, "")

    if trust_line:
        detail_panels.append(_detail_panel("Filters and Scope", body=f"{trust_line} Window: {window_label}."))
    if trust_caveats:
        detail_panels.append(_detail_panel("Caveats", items=trust_caveats[:4], tone="caution"))
    if methodology_line:
        detail_panels.append(_detail_panel("Methodology", body=methodology_line))
    if export_columns:
        allowed_count = len(list(export_columns.get("all_allowed_columns") or []))
        excluded_count = len(list(export_columns.get("all_excluded_columns") or []))
        detail_panels.append(
            _detail_panel(
                "Column Access",
                body=f"Allowed columns: {allowed_count}. Excluded sensitive columns: {excluded_count}.",
            )
        )

    sections: List[Dict[str, str]] = []
    if question_type == "history_analytics":
        sections = [
            {"title": "History Series", "body": direct},
            {"title": "What Changed", "body": explanation},
        ]
    elif question_type == "comparison_analytics":
        sections = [
            {"title": "Comparison", "body": direct},
            {"title": "What Matters", "body": explanation},
        ]
    elif question_type == "definition_help":
        sections = [
            {"title": "Definition", "body": direct},
            {"title": "How to Use It", "body": explanation},
        ]
    elif question_type == "page_help":
        sections = [
            {"title": "How To Use This Page", "body": direct},
            {"title": "Good Next Questions", "body": explanation},
        ]
    elif question_type == "driver_mover":
        sections = [
            {"title": "What Changed", "body": direct},
            {"title": "What’s Driving It", "body": explanation},
        ]
    elif question_type in {"risk_watchout", "anomaly_risk", "proactive_insights"}:
        sections = [
            {"title": "What Stands Out", "body": direct},
            {"title": "Why It Matters", "body": explanation},
        ]
    elif question_type in {"forecast_outlook", "trust_quality"}:
        sections = [
            {"title": "Assessment", "body": direct},
            {"title": "Interpretation", "body": explanation},
        ]
    elif question_type == "ranking_analytics":
        sections = [
            {"title": "Ranked Results", "body": direct},
            {"title": "What Stands Out", "body": explanation},
        ]
        if slots.query_shape == "nested_ranking" and nested_results:
            render_strategy = str(nested_results.get("render_strategy") or "inline").strip().lower()
            if render_strategy in {"compact_summary", "export_recommended"}:
                sections.append({"title": "Export Path", "body": "The inline view is compacted. Use export for the complete hierarchy."})
    elif question_type == "grouped_analytics":
        sections = [
            {"title": "Grouped View", "body": direct},
            {"title": "What Stands Out", "body": explanation},
        ]
    elif question_type in {"cross_module", "risk_action"}:
        sections = [
            {"title": "Top Priority", "body": direct},
            {"title": "What to Do Next", "body": explanation},
        ]
    elif question_type == "returns_workflow":
        sections = [
            {"title": "Returns Summary", "body": direct},
            {"title": "Workflow Help", "body": explanation},
        ]
    elif question_type == "returns_analytics":
        sections = [
            {"title": "Returns Summary", "body": direct},
            {"title": "Operational Context", "body": explanation},
        ]
    elif question_type == "export_request":
        sections = [
            {"title": "Export Status", "body": direct},
            {"title": "Included Content", "body": explanation},
        ]
    else:
        sections = [
            {"title": "Direct Answer", "body": direct},
            {"title": "What Matters", "body": explanation},
        ]
    if question_type == "analyst_detail" or followup.response_mode == "analyst":
        analyst_bits: List[str] = []
        if detailed_breakdown.get("concentration_note"):
            analyst_bits.append(str(detailed_breakdown.get("concentration_note")))
        if driver_breakdown.get("summary"):
            analyst_bits.append(str(driver_breakdown.get("summary")))
        if margin_breakdown.get("summary"):
            analyst_bits.append(str(margin_breakdown.get("summary")))
        if slots.query_shape == "nested_ranking" and nested_results:
            analyst_bits.append("Nested answers are bounded to safe parent/child limits to avoid query explosion.")
        analyst_body = " ".join(bit for bit in analyst_bits if str(bit).strip()) or "This version adds deeper driver, margin, and concentration context."
        sections.insert(2, {"title": "Analyst Lens", "body": analyst_body})
    if question_type == "guided_investigation":
        sections.append({"title": "Next Step", "body": "Use the guided path below to continue the investigation."})
    if question_type == "workflow_assist":
        sections.append({"title": "Workflow Safety", "body": "This is a review-only draft and does not execute any workflow for you."})
    if question_type == "modify_request":
        sections.append({"title": "Review", "body": "Nothing was changed automatically."})
    if slots.query_shape == "nested_ranking" and nested_results and not export_actions:
        detail_panels.append(_detail_panel("Full Hierarchy", body="Use export if you need the full parent-and-child result set."))

    spoken_summary = str((digest_payload.get("spoken_summary") if isinstance(digest_payload, Mapping) else None) or direct).strip()
    spoken_blocks = [f"{section.get('title')}: {section.get('body')}" for section in sections[:3] if section.get("title") and section.get("body")]
    if export_actions:
        status_rank = {
            "completed": 0,
            "running": 1,
            "pending": 2,
            "rate_limited": 3,
            "busy": 4,
            "error": 5,
            "empty": 6,
            "forbidden": 7,
        }

        def _export_priority(item: Mapping[str, Any]) -> tuple[int, int, int]:
            token = str(item.get("status") or "").strip().lower()
            rank = status_rank.get(token, 8)
            download_penalty = 0 if str(item.get("download_url") or "").strip() else 1
            status_url_penalty = 0 if str(item.get("status_url") or item.get("api_status_url") or "").strip() else 1
            return rank, download_penalty, status_url_penalty

        export_actions = sorted(export_actions, key=_export_priority)

    presentation_type = {
        "history_analytics": "trend",
        "comparison_analytics": "comparison",
        "ranking_analytics": "ranking",
        "grouped_analytics": "grouped",
        "driver_mover": "drivers",
        "risk_watchout": "risk",
        "proactive_insights": "signal",
        "cross_module": "action_plan",
        "risk_action": "action_plan",
        "returns_analytics": "operations",
        "returns_workflow": "operations",
        "executive_digest": "executive",
        "executive_summary": "executive",
        "export_request": "export",
    }.get(question_type, "summary")
    answer_type = _legacy_answer_type(question_type, presentation_type)
    actions = _legacy_export_actions(export_actions)
    subject_focus = entity_label or top_entity_label or module_label
    action_topic = top_risk_title or top_entity_label or subject_focus

    return {
        "direct_answer": direct,
        "explanation": explanation,
        "answer_type": answer_type,
        "actions": actions,
        "sections": sections,
        "detail_panels": detail_panels,
        "evidence_cards": evidence_cards,
        "proactive_cards": proactive_cards[:10],
        "risk_narratives": risk_narratives[:10],
        "guided_investigations": guided_investigations[:10],
        "digest": digest_payload,
        "workflow_assist": workflow_assist,
        "trust_summary": trust_summary,
        "export_actions": export_actions[:6],
        "export_plan": export_plan,
        "export_columns": export_columns,
        "ranked_results": _decorate_metric_rows(ranked_rows[:20], metric=primary_metric),
        "grouped_results": _decorate_metric_rows(grouped_rows[:20], metric=primary_metric),
        "nested_results": _decorate_nested_payload(nested_results, metric=primary_metric) if nested_results else {},
        "modify_preview": modify_preview,
        "page_bundle": page_bundle_payload,
        "query_slots": slots.as_dict(),
        "follow_up_suggestions": deduped_followups[:10],
        "action_suggestions": deduped_actions[:10],
        "scope_note": trust_line,
        "trust_note": trust_note,
        "spoken_summary": spoken_summary,
        "spoken_blocks": spoken_blocks,
        "presentation_type": presentation_type,
        "subject": subject_focus,
        "metric": primary_metric,
        "dimension": str(slots.group_by_dimension or slots.primary_entity_type or "").strip(),
        "comparison_target": str(slots.comparison_target or "").strip(),
        "action_topic": action_topic,
        "response_mode": followup.response_mode,
        "detail_level": followup.detail_level,
        "voice_ready": bool(followup.request_voice),
        "focus": subject_focus or module or "overview",
    }


def _provider_answer(
    message: str,
    results: Sequence[Dict[str, Any]],
    history: Sequence[Dict[str, Any]],
    *,
    scope: Mapping[str, Any],
    window: Mapping[str, Any],
    trust_flags: Mapping[str, Any],
) -> str | None:
    cfg = _runtime_config()
    provider = build_provider(
        ProviderConfig(
            enabled=cfg.enabled,
            provider=cfg.provider,
            model=cfg.model,
            model_path=cfg.model_path,
            base_url=cfg.base_url,
            timeout_seconds=cfg.timeout_seconds,
            context_window=cfg.context_window,
            max_tokens=cfg.max_tokens,
            threads=cfg.threads,
            batch_size=cfg.batch_size,
            gpu_layers=cfg.gpu_layers,
        )
    )
    return provider.generate(
        message=message,
        tool_results=list(results),
        history=list(history),
        context={"scope": scope, "window": window, "trust_flags": trust_flags},
    )


def _tool_trace_summary(tool_name: str, result: Mapping[str, Any], latency_ms: int) -> Dict[str, Any]:
    return {
        "tool": tool_name,
        "status": result.get("status"),
        "title": result.get("title"),
        "module": result.get("module"),
        "latency_ms": int(latency_ms),
    }


def _audit_interaction(
    *,
    message: str,
    resolved_message: str,
    thread_id: str,
    page: str,
    module: str,
    entity: Mapping[str, Any] | None,
    question_type: str,
    tool_trace: Sequence[Mapping[str, Any]],
    permission_limited: bool,
    status: str,
    latency_ms: int,
    executive_mode: bool,
    proactive_mode: bool,
    digest_mode: bool,
    workflow_assist_mode: bool,
    response_mode: str,
    detail_level: str,
    voice_mode: bool,
    confidence_limited: bool,
    tool_failures: int,
    page_bundle_used: bool,
    history_mode: bool,
    comparison_mode: bool,
    export_requested: bool,
    export_generated: bool,
    export_count: int,
    modify_mode: bool,
    scheduled_digest_mode: bool,
    glossary_mode: bool,
    followup: bool,
    query_slots: Mapping[str, Any] | None = None,
) -> None:
    cfg = _runtime_config()
    if not cfg.enable_audit:
        return
    meta = {
        "provider": cfg.provider,
        "model": cfg.model,
        "page": page,
        "module": module,
        "entity": dict(entity or {}),
        "thread_id": thread_id,
        "question_type": question_type,
        "executive_mode": bool(executive_mode),
        "proactive_mode": bool(proactive_mode),
        "digest_mode": bool(digest_mode),
        "workflow_assist_mode": bool(workflow_assist_mode),
        "response_mode": str(response_mode or "standard"),
        "detail_level": str(detail_level or "standard"),
        "voice_mode": bool(voice_mode),
        "confidence_limited": bool(confidence_limited),
        "page_bundle_used": bool(page_bundle_used),
        "history_mode": bool(history_mode),
        "comparison_mode": bool(comparison_mode),
        "export_requested": bool(export_requested),
        "export_generated": bool(export_generated),
        "export_count": int(export_count),
        "modify_mode": bool(modify_mode),
        "scheduled_digest_mode": bool(scheduled_digest_mode),
        "glossary_mode": bool(glossary_mode),
        "followup": bool(followup),
        "query_slots": dict(query_slots or {}),
        "question_excerpt": _safe_excerpt(message),
        "resolved_excerpt": _safe_excerpt(resolved_message),
        "tool_count": len(tool_trace),
        "tool_calls": [dict(item) for item in tool_trace],
        "tool_failures": int(tool_failures),
        "permission_limited": bool(permission_limited),
        "status": status,
        "latency_ms": int(latency_ms),
    }
    log_audit(current_user, "assistant.query", meta=meta, target_user_id=getattr(current_user, "id", None))


def _prompt_map() -> Dict[str, List[str]]:
    return {
        "overview": [
            "What stands out most right now?",
            "Summarize this page for leadership.",
            "Top 5 regions by revenue in current filters.",
            "Revenue by region in current window.",
            "Why is revenue changing?",
            "Show historical business trend for this window.",
            "Export an overview leadership workbook.",
            "Schedule a weekly leadership digest.",
            "What are the biggest risks?",
            "Which entities drove change?",
            "What should I investigate next?",
        ],
        "customers": [
            "Create a manager summary for this customer.",
            "Summarize this customer.",
            "Top 10 products for this customer by revenue.",
            "Show full history for this customer.",
            "Export this customer history to Excel.",
            "What are the biggest watchouts here?",
            "Which products matter most?",
            "Is this customer at risk?",
            "What changed recently?",
        ],
        "products": [
            "What is unusual for this product right now?",
            "Summarize this product.",
            "Top customers for this product by revenue.",
            "Show full history for this product.",
            "Export this product workbook.",
            "Why is this product's margin changing?",
            "Which customers drive this SKU?",
            "Is this product risky?",
            "What should sales review?",
        ],
        "regions": [
            "What changed here that leadership should care about?",
            "Summarize this region.",
            "Compare this region with last year.",
            "Show region history and compare with last year.",
            "Export this region analysis pack.",
            "What is changing here?",
            "Which customers or products matter most?",
            "Are there profitability issues?",
        ],
        "suppliers": [
            "Prepare a concise supplier manager brief.",
            "Summarize this supplier.",
            "Top customers for this supplier by revenue.",
            "Show supplier history over time.",
            "Export this supplier analysis workbook.",
            "Are there dependency risks?",
            "What changed in supplier performance?",
            "Which products matter most?",
        ],
        "salesreps": [
            "Which rep portfolios are most exposed?",
            "Summarize this portfolio.",
            "Top products for this rep by revenue.",
            "Show this sales rep portfolio history.",
            "Export this rep portfolio workbook.",
            "Which customers are driving this rep's performance?",
            "Where are the risks?",
            "What changed recently?",
        ],
        "returns": [
            "What operational issue should we address first?",
            "Summarize returns in my scope.",
            "Show returns history and trend.",
            "Export returns summary workbook.",
            "What approvals are pending?",
            "What are the main return reasons?",
            "Explain the workflow here.",
        ],
    }


def suggested_prompts(page_hint: str | None = None, ref_path: str | None = None) -> List[str]:
    cfg = _runtime_config()
    if not cfg.enable_suggested_prompts:
        return []
    page = canonical_page(page_hint or ref_path or request.path)
    mapping = _prompt_map()
    prompts = list(mapping.get(page, mapping["overview"]))
    prompts.extend(
        [
            "What stands out most right now?",
            "Top 5 regions by revenue.",
            "Top 10 products by profit.",
            "Revenue by region.",
            "Compare current period with last year.",
            "Create a short leadership brief.",
            "Prepare an investigation checklist.",
            "Export this analysis to Excel.",
            "Explain that in simpler terms.",
            "Show more detail.",
            "Summarize for leadership.",
            "What should I do next?",
        ]
    )
    deduped: List[str] = []
    for prompt in prompts:
        if prompt not in deduped:
            deduped.append(prompt)
    return deduped[:10]


def initial_context(page_hint: str | None = None, ref_path: str | None = None) -> Dict[str, Any]:
    cfg = _runtime_config()
    context = build_page_context(
        None,
        page_hint=page_hint,
        ref_path=ref_path,
        enable_page_context=cfg.enable_page_context,
    )
    return {
        "enabled": cfg.enabled,
        "provider": cfg.provider,
        "model": cfg.model,
        "features": {
            "proactive_insights": cfg.enable_proactive_insights,
            "workflow_assist": cfg.enable_workflow_assist,
            "voice_ready": cfg.enable_voice_ready,
        },
        "page": context.get("page"),
        "filters": context.get("filters"),
        "scope": context.get("scope"),
        "module_access": context.get("module_access"),
        "sensitive_data_access": context.get("sensitive_data_access"),
        "entity": context.get("entity"),
        "page_state": context.get("page_state"),
        "suggested_prompts": suggested_prompts(page_hint=page_hint, ref_path=ref_path),
        "debug_available": _assistant_debug_available(current_user),
    }


def assistant_health() -> Dict[str, Any]:
    cfg = _runtime_config()
    provider = build_provider(
        ProviderConfig(
            enabled=cfg.enabled,
            provider=cfg.provider,
            model=cfg.model,
            model_path=cfg.model_path,
            base_url=cfg.base_url,
            timeout_seconds=cfg.timeout_seconds,
            context_window=cfg.context_window,
            max_tokens=cfg.max_tokens,
            threads=cfg.threads,
            batch_size=cfg.batch_size,
            gpu_layers=cfg.gpu_layers,
        )
    )
    provider_state = provider.health()
    status = "disabled" if not cfg.enabled else ("ok" if provider_state.get("status") == "ok" else "degraded")
    return {
        "status": status,
        "enabled": cfg.enabled,
        "provider": cfg.provider,
        "model": cfg.model,
        "timeout_seconds": cfg.timeout_seconds,
        "max_tool_calls": cfg.max_tool_calls,
        "max_proactive_items": cfg.max_proactive_items,
        "features": {
            "audit": cfg.enable_audit,
            "page_context": cfg.enable_page_context,
            "suggested_prompts": cfg.enable_suggested_prompts,
            "glossary": cfg.enable_glossary,
            "tool_backing_required_for_metrics": cfg.require_tool_backing_for_metrics,
            "proactive_insights": cfg.enable_proactive_insights,
            "workflow_assist": cfg.enable_workflow_assist,
            "voice_ready": cfg.enable_voice_ready,
        },
        "provider_health": provider_state,
    }


def get_thread_state(thread_id: str) -> Dict[str, Any]:
    resolved = str(thread_id or "").strip()
    if not resolved:
        return {"status": "error", "error": "thread_id_required", "message": "thread_id is required."}
    snapshot = memory.thread_snapshot(getattr(current_user, "id", "anon"), resolved, limit=20)
    return {"status": "ok", "thread": snapshot}


def get_export_job_status(job_id: str | None) -> Dict[str, Any]:
    cfg = _runtime_config()
    if not cfg.enabled:
        return {"status": "disabled", "error": "assistant_disabled", "message": "Assistant is disabled."}
    token = str(job_id or "").strip()
    if not token:
        return {"status": "error", "error": "job_id_required", "message": "job_id is required."}
    job = get_export_job(getattr(current_user, "id", "anon"), token)
    if job is None:
        return {"status": "error", "error": "export_job_not_found", "message": "Export job not found or expired."}
    payload = export_job_payload(job)
    payload["status_url"] = f"/ai/exports/jobs/{token}"
    payload["api_status_url"] = f"/api/assistant/exports/jobs/{token}"
    export_id = str(payload.get("export_id") or "").strip()
    if export_id:
        payload["download_url"] = f"/ai/exports/{export_id}/download"
        payload["api_download_url"] = f"/api/assistant/exports/{export_id}/download"
    if cfg.enable_audit:
        try:
            log_audit(
                current_user,
                "assistant.export.job_status",
                meta={
                    "job_id": token,
                    "job_status": payload.get("status"),
                    "export_id": payload.get("export_id"),
                    "module": (payload.get("meta") or {}).get("module") if isinstance(payload.get("meta"), Mapping) else None,
                },
                target_user_id=getattr(current_user, "id", None),
            )
        except Exception:
            pass
    return {"status": "ok", "job": payload}


def _extract_state_entity(context: Mapping[str, Any], results: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    entity = context.get("entity")
    if isinstance(entity, Mapping):
        return {
            "type": entity.get("type"),
            "id": entity.get("id"),
            "label": entity.get("label"),
        }
    for result in results:
        data = result.get("data")
        if not isinstance(data, Mapping):
            continue
        for key in ("customer_id", "product_id", "region", "supplier_id", "rep_id", "salesrep_id"):
            if data.get(key):
                return {"type": key.replace("_id", ""), "id": data.get(key), "label": data.get(key)}
    return {}


def get_proactive_feed(payload: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    cfg = _runtime_config()
    if not cfg.enabled:
        return {"status": "disabled", "error": "assistant_disabled", "message": "Assistant is disabled by configuration."}
    if not has_any_access(current_user):
        return {"status": "forbidden", "error": "no_module_access", "message": "No module access is configured for this account."}
    if not cfg.enable_proactive_insights:
        return {"status": "disabled", "error": "proactive_disabled", "message": "Proactive insights are disabled by configuration."}

    started = time.perf_counter()
    payload = dict(payload or {})
    context = build_page_context(payload, enable_page_context=cfg.enable_page_context)
    page = canonical_page(context.get("page") or request.path)
    filters = resolve_filters_from_payload(payload if cfg.enable_page_context else None)
    scope = dict(context.get("scope") or {})
    sensitive = dict(context.get("sensitive_data_access") or sensitive_access_flags(current_user))
    ctx_blob = context_blob(payload) if cfg.enable_page_context else {}
    page_state = dict(context.get("page_state") or {})
    module = page if page != "assistant" else "overview"

    cache_key = _narrative_cache_key(
        user_id=getattr(current_user, "id", "anon"),
        page=page,
        module=module,
        kind="proactive_feed",
        payload={"filters": context.get("filters"), "scope": scope.get("scope_mode"), "allowed_count": scope.get("allowed_count")},
    )
    with _NARRATIVE_LOCK:
        cached = _NARRATIVE_CACHE.get(cache_key)
    if isinstance(cached, Mapping):
        out = dict(cached)
        out["cached"] = True
        return out

    tool_ctx = ToolContext(
        user=current_user,
        page=page,
        filters=filters,
        scope=scope,
        raw_context=ctx_blob,
        page_state=page_state,
        sensitive_flags=sensitive,
        enable_glossary=cfg.enable_glossary,
        conversation={},
    )

    selected = [
        ("get_proactive_insights", {"module": module, "cross_module": module in {"overview", "assistant"}}),
        ("get_priority_risks", {"module": "all" if module in {"overview", "assistant"} else module}),
        ("get_guided_investigation_paths", {"module": module}),
        ("get_next_best_questions", {"module": module}),
        ("get_confidence_or_trust_summary", {}),
    ][: max(2, min(cfg.max_proactive_items, 8))]

    tool_trace: List[Dict[str, Any]] = []
    cards: List[Dict[str, Any]] = []
    risks: List[Dict[str, Any]] = []
    paths: List[Dict[str, Any]] = []
    questions: List[str] = []
    trust_summary: Dict[str, Any] = {}
    failures = 0
    for name, args in selected:
        started_tool = time.perf_counter()
        result = execute_tool(name, tool_ctx, args)
        elapsed = int((time.perf_counter() - started_tool) * 1000)
        tool_trace.append(_tool_trace_summary(name, result, elapsed))
        if result.get("status") == "error":
            failures += 1
        data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
        title = str(result.get("title") or "")
        if title == "Proactive Insights":
            rows = data.get("cards")
            if isinstance(rows, list):
                cards.extend(dict(item) for item in rows if isinstance(item, Mapping))
        elif title == "Priority Risks":
            rows = data.get("risks")
            if isinstance(rows, list):
                risks.extend(dict(item) for item in rows if isinstance(item, Mapping))
        elif title == "Guided Investigation Paths":
            rows = data.get("paths")
            if isinstance(rows, list):
                paths.extend(dict(item) for item in rows if isinstance(item, Mapping))
        elif title == "Next Best Questions":
            rows = data.get("questions")
            if isinstance(rows, list):
                questions.extend(str(item) for item in rows if item)
        elif title == "Confidence And Trust Summary":
            trust_summary = dict(data)

    status = "ok" if cards or risks or paths else ("error" if failures >= len(selected) else "empty")
    total_ms = int((time.perf_counter() - started) * 1000)
    out = {
        "status": status,
        "module": module,
        "cards": cards[:10],
        "priority_risks": risks[:10],
        "guided_paths": paths[:8],
        "next_best_questions": questions[:10],
        "trust_summary": trust_summary,
        "tool_trace": tool_trace,
        "latency_ms": total_ms,
        "cached": False,
        "permission_limited": any(item.get("status") == "forbidden" for item in tool_trace),
    }
    with _NARRATIVE_LOCK:
        _NARRATIVE_CACHE[cache_key] = dict(out)

    if cfg.enable_audit:
        meta = {
            "provider": cfg.provider,
            "model": cfg.model,
            "page": page,
            "module": module,
            "scope": {"mode": scope.get("scope_mode"), "allowed_count": scope.get("allowed_count")},
            "triggered_by": str(payload.get("triggered_by") or "page_load"),
            "tool_count": len(tool_trace),
            "tool_calls": tool_trace,
            "status": status,
            "latency_ms": total_ms,
            "failures": failures,
        }
        log_audit(current_user, "assistant.proactive", meta=meta, target_user_id=getattr(current_user, "id", None))
    return out


def _build_tool_context(payload: Mapping[str, Any] | None = None) -> ToolContext:
    cfg = _runtime_config()
    incoming = dict(payload or {})
    context = build_page_context(incoming, enable_page_context=cfg.enable_page_context)
    page = canonical_page(context.get("page") or request.path)
    filters = resolve_filters_from_payload(incoming if cfg.enable_page_context else None)
    scope = dict(context.get("scope") or {})
    sensitive = dict(context.get("sensitive_data_access") or sensitive_access_flags(current_user))
    ctx_blob = context_blob(incoming) if cfg.enable_page_context else {}
    page_state = dict(context.get("page_state") or {})
    return ToolContext(
        user=current_user,
        page=page,
        filters=filters,
        scope=scope,
        raw_context=ctx_blob,
        page_state=page_state,
        sensitive_flags=sensitive,
        enable_glossary=cfg.enable_glossary,
        conversation={},
    )


def list_digest_schedules(payload: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    cfg = _runtime_config()
    if not cfg.enabled:
        return {"status": "disabled", "error": "assistant_disabled"}
    if not has_any_access(current_user):
        return {"status": "forbidden", "error": "no_module_access"}
    tool_ctx = _build_tool_context(payload)
    result = execute_tool("list_digest_schedules", tool_ctx, {})
    status = str(result.get("status") or "ok").lower()
    out = {"status": status, "schedules": ((result.get("data") or {}).get("schedules") if isinstance(result.get("data"), Mapping) else [])}
    if cfg.enable_audit:
        try:
            log_audit(
                current_user,
                "assistant.digest_schedule.list",
                meta={"status": status, "count": len(out["schedules"] or [])},
                target_user_id=getattr(current_user, "id", None),
            )
        except Exception:
            pass
    return out


def create_digest_schedule(payload: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    cfg = _runtime_config()
    if not cfg.enabled:
        return {"status": "disabled", "error": "assistant_disabled"}
    if not has_any_access(current_user):
        return {"status": "forbidden", "error": "no_module_access"}
    incoming = dict(payload or {})
    tool_ctx = _build_tool_context(incoming)
    args = {
        "module": incoming.get("module") or incoming.get("page") or tool_ctx.page,
        "cadence": incoming.get("cadence"),
        "audience": incoming.get("audience"),
        "length": incoming.get("length"),
        "timezone": incoming.get("timezone"),
        "hour_local": incoming.get("hour_local"),
    }
    result = execute_tool("create_digest_schedule", tool_ctx, args)
    status = str(result.get("status") or "ok").lower()
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    out = {"status": status, "schedule": data.get("schedule"), "message": "; ".join(result.get("notes") or [])}
    if cfg.enable_audit:
        try:
            log_audit(
                current_user,
                "assistant.digest_schedule.create",
                meta={
                    "status": status,
                    "module": args.get("module"),
                    "cadence": args.get("cadence"),
                    "audience": args.get("audience"),
                },
                target_user_id=getattr(current_user, "id", None),
            )
        except Exception:
            pass
    return out


def run_digest_schedule(schedule_id: str | None, payload: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    cfg = _runtime_config()
    if not cfg.enabled:
        return {"status": "disabled", "error": "assistant_disabled"}
    if not has_any_access(current_user):
        return {"status": "forbidden", "error": "no_module_access"}
    tool_ctx = _build_tool_context(payload)
    result = execute_tool("run_digest_schedule", tool_ctx, {"schedule_id": str(schedule_id or "").strip()})
    status = str(result.get("status") or "ok").lower()
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    out = {
        "status": status,
        "schedule": data.get("schedule"),
        "digest": data.get("digest"),
        "digest_status": data.get("digest_status"),
    }
    if cfg.enable_audit:
        try:
            log_audit(
                current_user,
                "assistant.digest_schedule.run",
                meta={
                    "status": status,
                    "schedule_id": ((out.get("schedule") or {}).get("schedule_id") if isinstance(out.get("schedule"), Mapping) else schedule_id),
                    "digest_status": out.get("digest_status"),
                },
                target_user_id=getattr(current_user, "id", None),
            )
        except Exception:
            pass
    return out


def delete_digest_schedule(schedule_id: str | None, payload: Mapping[str, Any] | None = None) -> Dict[str, Any]:
    cfg = _runtime_config()
    if not cfg.enabled:
        return {"status": "disabled", "error": "assistant_disabled"}
    if not has_any_access(current_user):
        return {"status": "forbidden", "error": "no_module_access"}
    tool_ctx = _build_tool_context(payload)
    result = execute_tool("delete_digest_schedule", tool_ctx, {"schedule_id": str(schedule_id or "").strip()})
    status = str(result.get("status") or "ok").lower()
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    out = {"status": status, "schedule_id": data.get("schedule_id"), "deleted": bool(data.get("deleted"))}
    if cfg.enable_audit:
        try:
            log_audit(
                current_user,
                "assistant.digest_schedule.delete",
                meta={"status": status, "schedule_id": out.get("schedule_id"), "deleted": bool(out.get("deleted"))},
                target_user_id=getattr(current_user, "id", None),
            )
        except Exception:
            pass
    return out


def handle_chat(payload: Mapping[str, Any]) -> Dict[str, Any]:
    started = time.perf_counter()
    cfg = _runtime_config()
    if not cfg.enabled:
        return {
            "status": "disabled",
            "error": "assistant_disabled",
            "message": "Assistant is disabled by configuration.",
        }
    if not has_any_access(current_user):
        return {
            "status": "forbidden",
            "error": "no_module_access",
            "message": "No module access is configured for this account.",
        }

    message = str(payload.get("message") or "").strip()
    if not message:
        return {"status": "error", "error": "message_required", "message": "Message is required."}
    if len(message) > 4000:
        return {"status": "error", "error": "message_too_long", "message": "Message exceeds maximum length."}

    context = build_page_context(payload, enable_page_context=cfg.enable_page_context)
    page = canonical_page(context.get("page") or request.path)
    filters = resolve_filters_from_payload(payload if cfg.enable_page_context else None)
    scope = dict(context.get("scope") or {})
    sensitive = dict(context.get("sensitive_data_access") or sensitive_access_flags(current_user))
    ctx_blob = context_blob(payload) if cfg.enable_page_context else {}
    page_state = dict(context.get("page_state") or {})

    thread_id = str(payload.get("thread_id") or "").strip() or memory.new_thread_id()
    history = memory.recent_messages(getattr(current_user, "id", "anon"), thread_id, limit=10)
    thread_state = memory.thread_state(getattr(current_user, "id", "anon"), thread_id)
    module_access = module_access_map(current_user)

    followup = _resolve_followup_message(message, state=thread_state, page_module=page)
    payload_mode = str(payload.get("mode") or ((payload.get("context") or {}).get("mode") if isinstance(payload.get("context"), Mapping) else "") or "").strip().lower()
    if payload_mode in {"standard", "executive", "analyst", "simple"}:
        followup.response_mode = payload_mode
        if payload_mode == "executive":
            followup.request_executive = True
        if payload_mode == "simple":
            followup.request_simpler = True
    payload_detail = str(payload.get("detail_level") or ((payload.get("context") or {}).get("detail_level") if isinstance(payload.get("context"), Mapping) else "") or "").strip().lower()
    if payload_detail in {"short", "standard", "detailed"}:
        followup.detail_level = payload_detail
        if payload_detail == "detailed":
            followup.request_more_detail = True
    payload_voice = payload.get("voice_ready")
    if isinstance(payload_voice, bool) and payload_voice:
        followup.request_voice = True

    module = _detect_module(followup.resolved_message, page, thread_state)
    if followup.force_module:
        module = followup.force_module
    slots = _extract_semantic_slots(
        followup.resolved_message,
        module=module,
        page_state=page_state,
        state=thread_state,
        followup=followup,
    )
    slots = _merge_followup_slots(slots, state=thread_state, followup=followup)
    module = _refine_module_from_slots(module, page, slots)
    question_type = _question_type(followup.resolved_message, module, followup, slots)
    prior_question_type = str((thread_state or {}).get("last_question_type") or "").strip()
    if (
        followup.is_followup
        and followup.response_mode == "analyst"
        and question_type == "analyst_detail"
        and prior_question_type in {"ranking_analytics", "grouped_analytics", "comparison_analytics", "history_analytics"}
    ):
        question_type = prior_question_type
    raw_followup = str(message or "").strip().lower()
    if (
        followup.is_followup
        and prior_question_type in {
            "ranking_analytics",
            "grouped_analytics",
            "history_analytics",
            "comparison_analytics",
            "driver_mover",
            "risk_watchout",
            "risk_action",
            "proactive_insights",
            "page_summary",
            "returns_analytics",
            "returns_workflow",
        }
        and question_type in {"live_analytics", "page_summary"}
        and (
            followup.request_simpler
            or followup.request_more_detail
            or raw_followup.startswith("what about ")
            or raw_followup.startswith("show only ")
            or raw_followup.startswith("explain ")
            or _is_pronoun_followup_reference(raw_followup)
        )
    ):
        question_type = prior_question_type
    if followup.response_mode == "executive" and question_type == "live_analytics":
        question_type = "executive_digest"

    tool_ctx = ToolContext(
        user=current_user,
        page=page,
        filters=filters,
        scope=scope,
        raw_context=ctx_blob,
        page_state=page_state,
        sensitive_flags=sensitive,
        enable_glossary=cfg.enable_glossary,
        conversation=thread_state,
    )

    selected_tools = _choose_tools(
        followup.resolved_message,
        page=page,
        module=module,
        question_type=question_type,
        slots=slots,
        module_access=module_access,
        max_calls=cfg.max_tool_calls,
        allow_glossary=cfg.enable_glossary,
        followup=followup,
    )

    tool_results: List[Dict[str, Any]] = []
    tool_trace: List[Dict[str, Any]] = []
    tool_failures = 0
    for tool_name, tool_args in selected_tools:
        tool_started = time.perf_counter()
        result = execute_tool(tool_name, tool_ctx, tool_args)
        elapsed = int((time.perf_counter() - tool_started) * 1000)
        tool_results.append(result)
        tool_trace.append(_tool_trace_summary(tool_name, result, elapsed))
        if str(result.get("status") or "").lower() == "error":
            tool_failures += 1

    permission_limited = any(item.get("status") == "forbidden" for item in tool_results)
    substantive_ok = any(
        item.get("status") == "ok" and str(item.get("title") or "") not in _HELPER_TITLES
        for item in tool_results
    )

    synthesis = _synthesize_answer(
        followup.resolved_message,
        tool_results,
        module=module,
        question_type=question_type,
        slots=slots,
        permission_limited=permission_limited,
        scope=scope,
        trust_flags=sensitive,
        followup=followup,
    )

    should_use_provider = True
    if question_type in {
        "proactive_insights",
        "anomaly_risk",
        "scheduled_digest",
        "guided_investigation",
        "executive_digest",
        "workflow_assist",
        "page_bundle",
        "page_summary",
        "driver_mover",
        "risk_watchout",
        "forecast_outlook",
        "trust_quality",
        "analyst_detail",
        "history_analytics",
        "comparison_analytics",
        "ranking_analytics",
        "grouped_analytics",
        "export_request",
        "modify_request",
        "definition_help",
        "page_help",
        "returns_workflow",
        "returns_analytics",
        "live_analytics",
    }:
        should_use_provider = False
    if cfg.require_tool_backing_for_metrics and _looks_metric_question(followup.resolved_message) and not substantive_ok:
        should_use_provider = False
    if substantive_ok:
        should_use_provider = False

    provider_text = None
    if should_use_provider:
        provider_text = _provider_answer(
            followup.resolved_message,
            tool_results,
            history,
            scope=scope,
            window=tool_results[0].get("window_used") if tool_results else {},
            trust_flags=sensitive,
        )

    if provider_text:
        first_line = provider_text.splitlines()[0].strip()
        direct_answer = first_line[:320] if first_line else synthesis["direct_answer"]
        explanation = provider_text
        if followup.request_simpler:
            explanation = f"Simple summary: {synthesis['direct_answer']}"
    else:
        direct_answer = synthesis["direct_answer"]
        explanation = synthesis["explanation"]

    prior_direct_answer = str((thread_state or {}).get("last_direct_answer") or "").strip()
    prior_question_type = str((thread_state or {}).get("last_question_type") or "").strip()
    prior_user_question = str((thread_state or {}).get("last_user_question") or "").strip()
    if _is_near_duplicate_text(direct_answer, prior_direct_answer) and (
        question_type != prior_question_type or _normalized_text(message) != _normalized_text(prior_user_question)
    ):
        candidate = ""
        for section in synthesis.get("sections") or []:
            if not isinstance(section, Mapping):
                continue
            title = str(section.get("title") or "").strip().lower()
            if title in {"window", "scope", "trust"}:
                continue
            body = str(section.get("body") or "").strip()
            if body and not _is_near_duplicate_text(body, prior_direct_answer):
                candidate = body
                break
        if candidate:
            direct_answer = candidate[:320]
        else:
            prefix = f"{_module_label(module)} {question_type.replace('_', ' ')}"
            direct_answer = f"{prefix}: {synthesis.get('direct_answer')}".strip()[:320]

    if substantive_ok:
        status = "ok"
    elif permission_limited:
        status = "forbidden"
    elif any(item.get("status") == "ok" for item in tool_results):
        status = "ok"
    elif any(item.get("status") == "error" for item in tool_results):
        status = "error"
    else:
        status = "empty"

    citations: List[str] = []
    next_actions: List[str] = []
    for result in tool_results:
        for cite in result.get("citations") or []:
            token = str(cite).strip()
            if token and token not in citations:
                citations.append(token)
        for action in result.get("next_actions") or []:
            token = str(action).strip()
            if token and token not in next_actions:
                next_actions.append(token)

    follow_up_suggestions = list(synthesis.get("follow_up_suggestions") or [])
    action_suggestions = list(synthesis.get("action_suggestions") or [])
    if not next_actions:
        next_actions = action_suggestions or suggested_prompts(page_hint=module)
    if not follow_up_suggestions:
        follow_up_suggestions = suggested_prompts(page_hint=module)

    total_ms = int((time.perf_counter() - started) * 1000)
    assistant_answer = f"{direct_answer}\n\n{explanation}".strip()
    state_entity = _extract_state_entity(context, tool_results)
    memory.append_turn(
        getattr(current_user, "id", "anon"),
        thread_id,
        user_message=message,
        assistant_answer=assistant_answer,
        tool_trace=tool_trace,
        state_update={
            "last_module": module,
            "last_question_type": question_type,
            "last_user_question": message,
            "last_resolved_question": followup.resolved_message,
            "last_entities": state_entity,
            "last_focus": synthesis.get("focus"),
            "last_tools": [item["tool"] for item in tool_trace],
            "last_window": tool_results[0].get("window_used") if tool_results else {},
            "last_permission_limited": permission_limited,
            "last_executive_mode": bool(question_type in {"executive_summary", "executive_digest"} or followup.request_executive),
            "last_proactive_mode": bool(question_type == "proactive_insights"),
            "last_workflow_assist_mode": bool(question_type == "workflow_assist"),
            "last_response_mode": followup.response_mode,
            "last_detail_level": followup.detail_level,
            "last_voice_mode": bool(followup.request_voice),
            "last_direct_answer": direct_answer,
            "last_query_slots": slots.as_dict(),
            "last_subject": synthesis.get("subject"),
            "last_metric": synthesis.get("metric"),
            "last_dimension": synthesis.get("dimension"),
            "last_comparison_target": synthesis.get("comparison_target") or slots.comparison_target,
            "last_action_topic": synthesis.get("action_topic"),
            "last_presentation_type": synthesis.get("presentation_type"),
            "last_action_suggestions": action_suggestions[:5],
        },
        max_turns=12,
    )

    trust_note_text = str(synthesis.get("trust_note") or "").lower()
    confidence_limited = bool(
        "hidden" in trust_note_text
        or "coverage" in trust_note_text
        or not sensitive.get("cost")
        or not sensitive.get("profit")
        or not sensitive.get("margin")
    )
    export_actions = list(synthesis.get("export_actions") or [])
    page_bundle_used = any(item.get("tool") in {"get_page_bundle", "get_entity_page_bundle", "get_current_page_visible_state"} for item in tool_trace)
    history_mode = question_type == "history_analytics" or any(item.get("tool") in {"get_overview_history", "get_customer_history", "get_product_history", "get_region_history", "get_supplier_history", "get_sales_rep_history", "get_returns_history"} for item in tool_trace)
    comparison_mode = question_type == "comparison_analytics" or any(item.get("tool") in {"compare_entities", "compare_customers", "compare_products", "compare_regions", "compare_suppliers", "compare_sales_reps", "compare_periods_for_entity"} for item in tool_trace)
    export_requested = question_type == "export_request" or any("export" in str(item.get("tool") or "") for item in tool_trace)
    export_generated = bool(export_actions)

    _audit_interaction(
        message=message,
        resolved_message=followup.resolved_message,
        thread_id=thread_id,
        page=page,
        module=module,
        entity=state_entity,
        question_type=question_type,
        tool_trace=tool_trace,
        permission_limited=permission_limited,
        status=status,
        latency_ms=total_ms,
        executive_mode=bool(question_type in {"executive_summary", "executive_digest"} or followup.request_executive),
        proactive_mode=bool(question_type == "proactive_insights"),
        digest_mode=bool(question_type in {"executive_summary", "executive_digest"}),
        workflow_assist_mode=bool(question_type == "workflow_assist"),
        response_mode=followup.response_mode,
        detail_level=followup.detail_level,
        voice_mode=bool(followup.request_voice),
        confidence_limited=confidence_limited,
        tool_failures=tool_failures,
        page_bundle_used=page_bundle_used,
        history_mode=history_mode,
        comparison_mode=comparison_mode,
        export_requested=export_requested,
        export_generated=export_generated,
        export_count=len(export_actions),
        modify_mode=bool(question_type == "modify_request"),
        scheduled_digest_mode=bool(question_type == "scheduled_digest"),
        glossary_mode=bool(question_type in {"definition_help", "page_help", "returns_workflow"}),
        followup=followup.is_followup,
        query_slots=slots.as_dict(),
    )

    return {
        "status": status,
        "thread_id": thread_id,
        "resolved_message": followup.resolved_message,
        "question_type": question_type,
        "module": module,
        "answer": {
            "direct_answer": direct_answer,
            "explanation": explanation,
            "answer_type": synthesis.get("answer_type") or _legacy_answer_type(question_type, synthesis.get("presentation_type") or ""),
            "actions": synthesis.get("actions") or [],
            "sections": synthesis.get("sections") or [],
            "detail_panels": synthesis.get("detail_panels") or [],
            "evidence_cards": synthesis.get("evidence_cards") or [],
            "proactive_cards": synthesis.get("proactive_cards") or [],
            "risk_narratives": synthesis.get("risk_narratives") or [],
            "guided_investigations": synthesis.get("guided_investigations") or [],
            "digest": synthesis.get("digest") or {},
            "workflow_assist": synthesis.get("workflow_assist") or {},
            "export_actions": synthesis.get("export_actions") or [],
            "export_plan": synthesis.get("export_plan") or {},
            "export_columns": synthesis.get("export_columns") or {},
            "ranked_results": synthesis.get("ranked_results") or [],
            "grouped_results": synthesis.get("grouped_results") or [],
            "nested_results": synthesis.get("nested_results") or {},
            "modify_preview": synthesis.get("modify_preview") or {},
            "page_bundle": synthesis.get("page_bundle") or {},
            "spoken_summary": synthesis.get("spoken_summary") or direct_answer,
            "spoken_blocks": synthesis.get("spoken_blocks") or [],
            "presentation_type": synthesis.get("presentation_type") or "summary",
            "subject": synthesis.get("subject"),
            "metric": synthesis.get("metric"),
            "dimension": synthesis.get("dimension"),
            "comparison_target": synthesis.get("comparison_target"),
            "action_topic": synthesis.get("action_topic"),
            "follow_up_suggestions": follow_up_suggestions[:10],
            "action_suggestions": action_suggestions[:10],
            "scope_note": synthesis.get("scope_note"),
            "trust_note": synthesis.get("trust_note"),
            "trust_summary": synthesis.get("trust_summary") or {},
            "question_type": question_type,
            "module": module,
            "executive_mode": bool(question_type in {"executive_summary", "executive_digest"} or followup.request_executive),
            "proactive_mode": bool(question_type == "proactive_insights"),
            "workflow_assist_mode": bool(question_type == "workflow_assist"),
            "response_mode": followup.response_mode,
            "detail_level": followup.detail_level,
            "voice_ready": bool(followup.request_voice),
            "query_slots": synthesis.get("query_slots") or slots.as_dict(),
            "evidence": tool_results,
            "scope_used": scope,
            "window_used": tool_results[0].get("window_used") if tool_results else {},
            "trust_flags": sensitive,
            "next_actions": next_actions[:10],
            "citations": citations[:16],
            "permission_limited": permission_limited,
            "debug": {
                "question_type": question_type,
                "module": module,
                "response_mode": followup.response_mode,
                "detail_level": followup.detail_level,
                "query_slots": synthesis.get("query_slots") or slots.as_dict(),
                "page_bundle": synthesis.get("page_bundle") or {},
                "tool_trace": tool_trace,
                "latency_ms": total_ms,
            },
        },
        "tool_trace": tool_trace,
        "latency_ms": total_ms,
        "history": memory.summarize_history(getattr(current_user, "id", "anon"), thread_id),
    }
