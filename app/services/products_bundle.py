from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta
from urllib.parse import quote
from typing import Any, Dict, List, Sequence, Set

import numpy as np

from app.services import fact_schema as fs
from app.services import fact_store
from app.services import margin_rules


def _norm_col(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _safe_col(cols: set[str], *candidates: str) -> str | None:
    if not cols:
        return None
    lower_map = {str(c).lower(): c for c in cols}
    norm_map = {_norm_col(str(c)): c for c in cols}
    for cand in candidates:
        if cand in cols:
            return cand
        key = str(cand).lower()
        if key in lower_map:
            return lower_map[key]
        norm_key = _norm_col(str(cand))
        if norm_key in norm_map:
            return norm_map[norm_key]
    return None


def _resolve_product_columns(cols: set[str]) -> tuple[str | None, str | None, str | None]:
    """
    Resolve SKU, product id, and name columns with SKU preference for display.
    Falls back to product_id when SKU is unavailable.
    """
    prod_id_col = _safe_col(cols, fs.CANON.product_id, "ProductID", "ProductId")
    sku_col = _safe_col(cols, "SKU") or prod_id_col
    prod_name_col = _safe_col(cols, fs.CANON.product_name, "ProductName", "Description", "SkuName")
    return sku_col, prod_id_col, prod_name_col


def _product_exprs(cols: set[str]) -> dict[str, str]:
    """
    Build reusable SQL expressions for SKU, product key, product name, and display name.
    """
    sku_col, prod_id_col, prod_name_col = _resolve_product_columns(cols)
    sku_expr = f"NULLIF(CAST({sku_col} AS VARCHAR), '')" if sku_col else "NULL"
    prod_id_expr = f"NULLIF(CAST({prod_id_col} AS VARCHAR), '')" if prod_id_col else "NULL"
    name_expr = f"NULLIF(CAST({prod_name_col} AS VARCHAR), '')" if prod_name_col else "NULL"
    product_key_expr = f"COALESCE({sku_expr}, {prod_id_expr})"
    product_name_expr = f"COALESCE({name_expr}, {sku_expr}, {prod_id_expr})"
    display_name_expr = (
        "CASE "
        f"WHEN {sku_expr} IS NOT NULL AND {name_expr} IS NOT NULL THEN {sku_expr} || '  ' || {name_expr} "
        f"WHEN {sku_expr} IS NOT NULL THEN {sku_expr} "
        f"WHEN {name_expr} IS NOT NULL THEN {name_expr} "
        f"ELSE {prod_id_expr} "
        "END"
    )
    return {
        "sku_col": sku_col or "",
        "prod_id_col": prod_id_col or "",
        "prod_name_col": prod_name_col or "",
        "sku_expr": sku_expr,
        "prod_id_expr": prod_id_expr,
        "name_expr": name_expr,
        "product_key_expr": product_key_expr,
        "product_name_expr": product_name_expr,
        "display_name_expr": display_name_expr,
    }


def _family_exprs(cols: set[str]) -> dict[str, str]:
    protein_candidates = ("Protein", "ProteinType", "ProteinName", "Category", "ProductCategory")
    category_candidates = ("Category", "ProductCategory", "Protein", "ProteinType", "ProteinName")
    protein_col = _safe_col(cols, *protein_candidates)
    category_col = _safe_col(cols, *category_candidates)
    protein_base = _coalesce_text_expr(cols, protein_candidates, default="NULL")
    category_base = _coalesce_text_expr(cols, category_candidates, default="NULL")
    protein_expr = f"COALESCE({protein_base}, {category_base}, 'Unassigned')"
    category_expr = f"COALESCE({category_base}, {protein_base}, 'Unassigned')"
    return {
        "protein_col": protein_col or "",
        "category_col": category_col or "",
        "protein_expr": protein_expr,
        "category_expr": category_expr,
    }


def _coalesce_expr(available: set[str], candidates: Sequence[str], default: str = "0") -> str:
    if not available:
        return default
    lower_map = {str(c).lower(): c for c in available}
    norm_map = {_norm_col(str(c)): c for c in available}
    present: list[str] = []
    for cand in candidates:
        if cand in available:
            present.append(cand)
            continue
        key = str(cand).lower()
        actual = lower_map.get(key)
        if actual:
            present.append(actual)
            continue
        norm_key = _norm_col(str(cand))
        actual = norm_map.get(norm_key)
        if actual:
            present.append(actual)
    # Preserve order but drop duplicates.
    if present:
        present = list(dict.fromkeys(present))
    if not present:
        return default
    inner = ", ".join(present + [default])
    return f"COALESCE({inner})"


def _coalesce_text_expr(available: set[str], candidates: Sequence[str], default: str = "NULL") -> str:
    if not available:
        return default
    lower_map = {str(c).lower(): c for c in available}
    norm_map = {_norm_col(str(c)): c for c in available}
    present: list[str] = []
    for cand in candidates:
        if cand in available:
            present.append(cand)
            continue
        key = str(cand).lower()
        actual = lower_map.get(key)
        if actual:
            present.append(actual)
            continue
        norm_key = _norm_col(str(cand))
        actual = norm_map.get(norm_key)
        if actual:
            present.append(actual)
    if present:
        present = list(dict.fromkeys(present))
    if not present:
        return default
    inner = ", ".join(f"NULLIF(CAST({col} AS VARCHAR), '')" for col in present)
    return f"COALESCE({inner}, {default})"


def _to_list(val: Any) -> list:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, tuple):
        return list(val)
    if isinstance(val, np.ndarray):
        return val.tolist()
    return [val]


def _clean_num(val: Any) -> float:
    try:
        if val is None:
            return 0.0
        fval = float(val)
        if math.isnan(fval):
            return 0.0
        return fval
    except Exception:
        return 0.0


def _clean_int(val: Any, default: int = 0) -> int:
    parsed = _safe_float(val)
    if parsed is None:
        return int(default)
    return int(parsed)


def _clean_optional(val: Any) -> float | None:
    try:
        if val is None:
            return None
        fval = float(val)
        if math.isnan(fval):
            return None
        return fval
    except Exception:
        return None


LOW_BASE_REVENUE = 500.0
PRODUCT_SUMMARY_SECTIONS = frozenset({"overview", "strategy", "demand"})
PRODUCT_DETAIL_SECTIONS = frozenset({"pricing", "execution", "assortment"})
PRODUCT_TABLE_SECTIONS = frozenset({"table"})
PRODUCT_ALL_SECTIONS = frozenset(set(PRODUCT_SUMMARY_SECTIONS) | set(PRODUCT_DETAIL_SECTIONS) | set(PRODUCT_TABLE_SECTIONS))


def _requested_product_sections(args: Any) -> Set[str]:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    getlist = args.getlist if hasattr(args, "getlist") else None
    raw_values: List[Any] = []
    for key in ("_sections", "sections"):
        if getlist is not None:
            try:
                raw_values.extend(getlist(key))
            except Exception:
                pass
        value = getter(key)
        if value not in (None, ""):
            raw_values.append(value)

    requested: Set[str] = set()
    for raw in raw_values:
        if raw is None:
            continue
        parts = raw if isinstance(raw, (list, tuple, set)) else str(raw).split(",")
        for part in parts:
            token = str(part or "").strip().lower().replace("-", "_")
            if not token:
                continue
            if token in {"all", "full"}:
                return set()
            if token in PRODUCT_ALL_SECTIONS:
                requested.add(token)
    return requested


def _risk_bucket_count(risk_opportunity: Dict[str, Any], key: str) -> int:
    rows = (risk_opportunity or {}).get(key)
    if isinstance(rows, list):
        return len(rows)
    count_key = f"{key}_count"
    try:
        return int((risk_opportunity or {}).get(count_key) or 0)
    except Exception:
        return 0


def _default_recommendation(row: Dict[str, Any]) -> str:
    quick = _default_quick_rec(row)
    mapping = {
        "Raise": "Raise price",
        "Promo": "Promote / bundle",
        "Review": "Review cost / price",
        "Hold": "Hold",
    }
    return mapping.get(quick, "Review")


def _enrich_table_rows(
    rows: List[Dict[str, Any]],
    quick_rec_map: Dict[str, str] | None = None,
    action_map: Dict[str, str] | None = None,
) -> List[Dict[str, Any]]:
    quick_rec_map = quick_rec_map or {}
    action_map = action_map or {}
    for row in rows:
        sku = row.get("sku") or row.get("product_id") or row.get("key")
        if sku:
            sku_key = str(sku)
            row["quick_rec"] = quick_rec_map.get(sku_key) or _default_quick_rec(row)
            row["recommendation"] = action_map.get(sku_key) or _default_recommendation(row)
        else:
            row["quick_rec"] = _default_quick_rec(row)
            row["recommendation"] = _default_recommendation(row)
    return rows


def _coerce_date(value: Any) -> date | None:
    if value is None:
        return None
    try:
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value)).date()
    except Exception:
        return None


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _month_end(value: date) -> date:
    first = _month_start(value)
    if first.month == 12:
        next_month = date(first.year + 1, 1, 1)
    else:
        next_month = date(first.year, first.month + 1, 1)
    return next_month - timedelta(days=1)


def _shift_months(value: date, months: int) -> date:
    month_index = (value.year * 12 + (value.month - 1)) + months
    year = month_index // 12
    month = month_index % 12 + 1
    candidate = date(year, month, 1)
    return candidate + timedelta(days=min(value.day, _month_end(candidate).day) - 1)


def _date_label(value: date | None) -> str:
    if value is None:
        return "Unknown"
    return value.strftime("%b %d, %Y")


def _window_label(start: date | None, end: date | None) -> str:
    if start is None and end is None:
        return "Current filtered window"
    if start is None:
        return _date_label(end)
    if end is None or start == end:
        return _date_label(start)
    return f"{_date_label(start)} to {_date_label(end)}"


def _with_window(filters: Any, *, start: date | None, end: date | None) -> Any:
    start_dt = datetime.combine(start, datetime.min.time()) if start is not None else None
    end_dt = datetime.combine(end, datetime.min.time()) if end is not None else None
    try:
        return replace(filters, start=start_dt, end=end_dt)
    except Exception:
        if isinstance(filters, dict):
            updated = dict(filters)
            updated["start"] = start_dt
            updated["end"] = end_dt
            return updated
    return filters


def _build_comparison_window(start_iso: str | None, end_iso: str | None) -> Dict[str, Any]:
    start = _coerce_date(start_iso)
    end_exclusive = _coerce_date(end_iso)
    display_end = end_exclusive - timedelta(days=1) if end_exclusive is not None else None
    if start is None and display_end is None:
        return {
            "method": "current_window_only",
            "current_start": None,
            "current_end": None,
            "prior_start": None,
            "prior_end": None,
            "current_days": 0,
            "prior_days": 0,
            "is_partial_period": False,
            "current_label": "Current filtered window",
            "prior_label": "Prior comparable window",
            "current_short_label": "Current window",
            "prior_short_label": "Prior window",
            "comparison_label": "Current window vs prior comparable window",
            "window_label": "Live filters",
            "note": "Comparisons follow the active filtered window.",
            "trajectory_note": "Trajectory shows only the active filtered window.",
            "projection_note": "Projection uses recent completed periods when enough history exists.",
        }
    if start is None:
        start = display_end
    if display_end is None:
        display_end = start
    if display_end < start:
        start, display_end = display_end, start

    current_days = max(1, (display_end - start).days + 1)
    terminal_month_incomplete = display_end != _month_end(display_end)
    single_month_to_date = start == _month_start(display_end) and terminal_month_incomplete
    completed_month_span = start == _month_start(start) and display_end == _month_end(display_end)
    month_span_count = ((display_end.year - start.year) * 12) + (display_end.month - start.month) + 1

    if single_month_to_date:
        prior_start = _shift_months(start, -1)
        prior_end = min(_month_end(prior_start), prior_start + timedelta(days=current_days - 1))
        method = "month_to_date_vs_prior_month_same_day"
        current_label = "Current month-to-date"
        prior_label = "Prior month same day"
        current_short = "Current MTD"
        prior_short = "Prior MTD"
        comparison_label = "Month-to-date vs prior month same day"
        note = (
            f"Current filtered window is month-to-date through {_date_label(display_end)}. "
            f"Comparisons use {_window_label(prior_start, prior_end)} to avoid misleading partial-month MoM."
        )
        trajectory_note = (
            f"Trajectory shows the active filtered window. The latest month is partial, so demand change is compared against "
            f"{_window_label(prior_start, prior_end)} rather than a full prior month."
        )
        projection_note = "Next-month projection is pace-normalized from the current month-to-date run rate."
    elif completed_month_span:
        prior_start = _shift_months(start, -month_span_count)
        prior_end = start - timedelta(days=1)
        method = "completed_months_vs_prior_completed_months"
        current_label = "Current completed month set" if month_span_count > 1 else "Current completed month"
        prior_label = "Prior completed month set" if month_span_count > 1 else "Prior completed month"
        current_short = "Current window"
        prior_short = "Prior window"
        comparison_label = "Completed months vs prior completed months"
        note = (
            f"Current filtered window spans {_window_label(start, display_end)}. "
            f"Comparisons use the prior completed window {_window_label(prior_start, prior_end)}."
        )
        trajectory_note = "Trajectory uses completed periods from the active filtered window."
        projection_note = "Next-month projection is based on recent completed monthly history."
    else:
        prior_end = start - timedelta(days=1)
        prior_start = prior_end - timedelta(days=current_days - 1)
        method = "selected_window_vs_prior_matched_days"
        current_label = "Current filtered window"
        prior_label = "Prior matched-days window"
        current_short = "Current window"
        prior_short = "Prior comparable"
        comparison_label = "Selected window vs prior matched days"
        note = (
            f"Current filtered window {_window_label(start, display_end)} is compared with "
            f"{_window_label(prior_start, prior_end)} using the same number of days."
        )
        trajectory_note = "Trajectory shows only the active filtered window; deltas use the prior matched-days comparison."
        projection_note = "Next-month projection uses recent completed periods and ignores partial trailing periods where possible."

    return {
        "method": method,
        "current_start": start.isoformat(),
        "current_end": display_end.isoformat(),
        "prior_start": prior_start.isoformat(),
        "prior_end": prior_end.isoformat(),
        "history_start": min(start, prior_start).isoformat(),
        "current_days": current_days,
        "prior_days": max(1, (prior_end - prior_start).days + 1),
        "terminal_period_incomplete": terminal_month_incomplete,
        "is_partial_period": single_month_to_date,
        "current_label": current_label,
        "prior_label": prior_label,
        "current_short_label": current_short,
        "prior_short_label": prior_short,
        "comparison_label": comparison_label,
        "window_label": _window_label(start, display_end),
        "current_window_label": _window_label(start, display_end),
        "prior_window_label": _window_label(prior_start, prior_end),
        "note": note,
        "trajectory_note": trajectory_note,
        "projection_note": projection_note,
    }


def _safe_float(val: Any) -> float | None:
    try:
        if val is None:
            return None
        fval = float(val)
        if math.isnan(fval):
            return None
        return fval
    except Exception:
        return None


def _pct_or_none(val: Any) -> float | None:
    fval = _safe_float(val)
    return fval


def _margin_risk_label(
    margin_pct: float | None,
    target_margin_pct: float | None = None,
    minimum_margin_pct: float | None = None,
) -> str:
    status = margin_rules.classify_margin_status(margin_pct, minimum_margin_pct, target_margin_pct)
    return str(status.get("target_status") or "No cost visibility")


def _status_badge_text(row: Dict[str, Any]) -> str:
    text = str(row.get("target_status") or row.get("margin_risk") or "").strip()
    return text or "No cost visibility"


def _status_tone(row: Dict[str, Any]) -> str:
    tone = str(row.get("status_tone") or "").strip().lower()
    return tone or "neutral"


def _price_uplift_pct(current_price: Any, target_price: Any) -> float | None:
    current = _safe_float(current_price)
    target = _safe_float(target_price)
    if current is None or target is None or current <= 0:
        return None
    return float((target - current) / current * 100.0)


def _annotate_product_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    annotated = margin_rules.annotate_margin_rows(rows)
    for row in annotated:
        computed_minimum = _safe_float(row.get("minimum_price"))
        computed_target = _safe_float(row.get("target_price"))
        unit_price = _safe_float(row.get("unit_price"))
        current_unit_price = _safe_float(row.get("current_unit_price"))
        chosen_price = current_unit_price if current_unit_price is not None else unit_price
        if computed_minimum is not None:
            row["minimum_price"] = computed_minimum
        if computed_target is not None:
            row["target_price"] = computed_target
        uplift_pct = _price_uplift_pct(chosen_price, row.get("target_price"))
        if uplift_pct is not None:
            row["uplift_pct"] = uplift_pct
        row["margin_risk"] = _status_badge_text(row)
        row["margin_status"] = row.get("status_key")
        row["minimum_margin_pct"] = _safe_float(row.get("minimum_margin_pct"))
        row["target_margin_pct"] = _safe_float(row.get("target_margin_pct"))
        row["min_product_margin_pct"] = _safe_float(row.get("min_product_margin_pct"))
        row["target_product_margin_pct"] = _safe_float(row.get("target_product_margin_pct"))
        row["base_cost"] = _safe_float(row.get("base_cost"))
        row["effective_cost"] = _safe_float(row.get("effective_cost")) or _safe_float(row.get("cost"))
        row["base_unit_cost"] = _safe_float(row.get("base_unit_cost"))
        row["effective_unit_cost"] = _safe_float(row.get("effective_unit_cost")) or _safe_float(row.get("unit_cost"))
        row["base_cost_lb"] = _safe_float(row.get("base_cost_lb"))
        row["effective_cost_lb"] = _safe_float(row.get("effective_cost_lb")) or _safe_float(row.get("cost_lb"))
        row["minimum_price"] = _safe_float(row.get("minimum_price"))
        row["minimum_price_lb"] = _safe_float(row.get("minimum_price_lb"))
        row["target_price_lb"] = _safe_float(row.get("target_price_lb"))
        row["min_price_gap"] = _safe_float(row.get("min_price_gap"))
        row["target_price_gap"] = _safe_float(row.get("target_price_gap"))
        row["asp_lb_gap_to_min"] = _safe_float(row.get("asp_lb_gap_to_min"))
        row["asp_lb_gap_to_target"] = _safe_float(row.get("asp_lb_gap_to_target"))
        row["target_achievement_pct"] = _safe_float(row.get("target_achievement_pct"))
        profit_uplift_target = _safe_float(row.get("profit_uplift_target"))
        if profit_uplift_target is None:
            profit_uplift_target = _safe_float(row.get("profit_uplift_to_target"))
        row["profit_uplift_target"] = profit_uplift_target
        row["price_band_status"] = row.get("price_status_key") or row.get("status_key")
        row["margin_band_status"] = row.get("status_key")
        row["rule_family"] = row.get("display_family")
        row["needs_protein_mapping"] = bool(row.get("needs_protein_mapping"))
        if str(row.get("pricing_basis") or "").strip().lower() == "unit":
            row["asp_lb"] = None
            row["base_cost_lb"] = None
            row["effective_cost_lb"] = None
            row["cost_lb"] = None
            row["minimum_price_lb"] = None
            row["target_price_lb"] = None
            row["asp_lb_gap_to_min"] = None
            row["asp_lb_gap_to_target"] = None
    return annotated


def _row_below_target(row: Dict[str, Any]) -> bool:
    margin_pct = _safe_float(row.get("margin_pct"))
    target_margin_pct = _safe_float(row.get("target_margin_pct"))
    return margin_pct is not None and target_margin_pct is not None and margin_pct < target_margin_pct


def _row_below_minimum(row: Dict[str, Any]) -> bool:
    margin_pct = _safe_float(row.get("margin_pct"))
    minimum_margin_pct = _safe_float(row.get("minimum_margin_pct"))
    return margin_pct is not None and minimum_margin_pct is not None and margin_pct < minimum_margin_pct


def _row_above_target(row: Dict[str, Any]) -> bool:
    margin_pct = _safe_float(row.get("margin_pct"))
    target_margin_pct = _safe_float(row.get("target_margin_pct"))
    return margin_pct is not None and target_margin_pct is not None and margin_pct >= target_margin_pct


def _margin_band_range_label(values: List[float]) -> str | None:
    clean = sorted({round(v, 1) for v in values if v is not None})
    if not clean:
        return None
    if len(clean) == 1:
        return f"{clean[0]:.0f}%"
    return f"{clean[0]:.0f}%–{clean[-1]:.0f}%"


def _row_display_name(row: Dict[str, Any]) -> str:
    text = str(row.get("display_name") or "").strip()
    if text:
        return text
    sku = str(row.get("sku") or row.get("product_id") or "").strip()
    name = str(row.get("product_name") or row.get("name") or row.get("desc") or "").strip()
    if sku and name and sku != name:
        return f"{sku} — {name}"
    return sku or name or "Unknown SKU"


def _row_velocity(row: Dict[str, Any]) -> float:
    return _safe_float(row.get("orders_per_month")) or _safe_float(row.get("velocity_per_month")) or 0.0


def _row_gap_to_target(row: Dict[str, Any]) -> float | None:
    gap = _safe_float(row.get("asp_lb_gap_to_target"))
    if gap is not None:
        return gap
    return _safe_float(row.get("target_price_gap"))


def _row_gap_to_minimum(row: Dict[str, Any]) -> float | None:
    gap = _safe_float(row.get("asp_lb_gap_to_min"))
    if gap is not None:
        return gap
    return _safe_float(row.get("min_price_gap"))


def _row_current_price_value(row: Dict[str, Any]) -> float | None:
    if str(row.get("pricing_basis") or "").strip().lower() == "unit":
        for key in ("current_unit_price", "unit_price", "current_price", "asp_lb"):
            value = _safe_float(row.get(key))
            if value is not None:
                return value
    for key in ("asp_lb", "current_unit_price", "unit_price", "current_price"):
        value = _safe_float(row.get(key))
        if value is not None:
            return value
    return None


def _row_pricing_basis(row: Dict[str, Any]) -> str:
    explicit = str(row.get("pricing_basis") or "").strip().lower()
    if explicit in {"lb", "unit"}:
        return explicit
    if any(
        _safe_float(row.get(key)) is not None
        for key in ("asp_lb", "minimum_price_lb", "target_price_lb", "effective_cost_lb", "cost_lb")
    ):
        return "lb"
    return "unit"


def _row_has_cost_visibility(row: Dict[str, Any]) -> bool:
    return any(
        _safe_float(row.get(key)) is not None
        for key in ("effective_cost_lb", "cost_lb", "effective_unit_cost", "unit_cost", "effective_cost", "cost")
    )


def _row_has_pricing_reference(row: Dict[str, Any]) -> bool:
    return any(
        _safe_float(row.get(key)) is not None
        for key in ("minimum_price_lb", "target_price_lb", "minimum_price", "target_price")
    )


def _visual_status_key(row: Dict[str, Any]) -> str | None:
    return str(row.get("visual_status_key") or row.get("price_status_key") or row.get("status_key") or "").strip().lower() or None


def _health_profitability_value(row: Dict[str, Any]) -> float | None:
    target_achievement_pct = _safe_float(row.get("target_achievement_pct"))
    if target_achievement_pct is not None:
        return float(target_achievement_pct - 100.0)
    margin_target_achievement_pct = _safe_float(row.get("margin_target_achievement_pct"))
    if margin_target_achievement_pct is not None:
        return float(margin_target_achievement_pct - 100.0)
    gap_to_target = _row_gap_to_target(row)
    if gap_to_target is not None:
        return gap_to_target
    margin_pct = _safe_float(row.get("margin_pct"))
    target_margin_pct = _safe_float(row.get("target_margin_pct"))
    if margin_pct is not None and target_margin_pct is not None:
        return float(margin_pct - target_margin_pct)
    if margin_pct is not None:
        return margin_pct
    profit = _safe_float(row.get("profit"))
    weight = _safe_float(row.get("weight"))
    if profit is not None and weight is not None and weight > 0:
        return float(profit / weight)
    return _safe_float(row.get("contribution_lb"))


def _quantile_value(values: Sequence[float], fraction: float) -> float | None:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return float(clean[0])
    idx = (len(clean) - 1) * max(0.0, min(1.0, float(fraction)))
    lower = math.floor(idx)
    upper = math.ceil(idx)
    if lower == upper:
        return float(clean[lower])
    share = idx - lower
    return float(clean[lower] + ((clean[upper] - clean[lower]) * share))


def _prepare_visual_pricing_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    overhead = margin_rules.overhead_unit_cost()
    prepared: List[Dict[str, Any]] = []
    for raw_row in rows or []:
        if not isinstance(raw_row, dict):
            continue
        row = dict(raw_row)
        weight = _safe_float(row.get("weight"))
        qty = _safe_float(row.get("qty"))
        current_price = _safe_float(row.get("current_unit_price"))
        if current_price is None:
            current_price = _safe_float(row.get("unit_price"))
        if current_price is None:
            current_price = _safe_float(row.get("current_price"))

        basis = str(row.get("pricing_basis") or "").strip().lower()
        if basis not in {"lb", "unit"}:
            if weight is not None and weight > 0:
                basis = "lb"
            elif qty is not None and qty > 0:
                basis = "unit"
            else:
                basis = "lb" if _safe_float(row.get("asp_lb")) is not None else "unit"
        basis_qty = weight if basis == "lb" and weight is not None and weight > 0 else qty
        if basis_qty is None or basis_qty <= 0:
            basis_qty = _safe_float(row.get("pricing_basis_qty"))
        if basis_qty is not None and basis_qty > 0:
            row["pricing_basis_qty"] = basis_qty
        row["pricing_basis"] = basis
        row["current_unit_price"] = current_price
        if current_price is not None and row.get("unit_price") is None:
            row["unit_price"] = current_price

        base_unit_cost = _safe_float(row.get("base_unit_cost"))
        if base_unit_cost is None:
            base_unit_cost = _safe_float(row.get("unit_cost"))
        if base_unit_cost is None and basis == "lb":
            base_unit_cost = _safe_float(row.get("base_cost_lb")) or _safe_float(row.get("cost_lb"))

        base_total_cost = _safe_float(row.get("base_cost"))
        if base_total_cost is None and _safe_float(row.get("effective_cost")) is not None:
            base_total_cost = (_safe_float(row.get("effective_cost")) or 0.0) - overhead
        if base_total_cost is None:
            cost_value = _safe_float(row.get("cost"))
            if cost_value is not None and not math.isclose(cost_value, (base_unit_cost or 0.0), rel_tol=1e-9, abs_tol=1e-9):
                base_total_cost = cost_value
        if base_total_cost is None and base_unit_cost is not None and basis_qty is not None and basis_qty > 0:
            base_total_cost = float(base_unit_cost * basis_qty)
        if base_unit_cost is None and base_total_cost is not None and basis_qty is not None and basis_qty > 0:
            base_unit_cost = float(base_total_cost / basis_qty)

        if base_total_cost is not None:
            row["base_cost"] = base_total_cost
            row["base_cost_basis"] = base_total_cost
        if base_unit_cost is not None:
            row["base_unit_cost"] = base_unit_cost

        effective_total_cost = _safe_float(row.get("effective_cost"))
        if effective_total_cost is None and base_total_cost is not None:
            effective_total_cost = float(base_total_cost + overhead)
        if effective_total_cost is not None:
            row["effective_cost"] = effective_total_cost
            row["effective_cost_basis"] = effective_total_cost

        effective_unit_cost = _safe_float(row.get("effective_unit_cost"))
        if effective_unit_cost is None and base_unit_cost is not None:
            effective_unit_cost = float(base_unit_cost + overhead)
        if effective_unit_cost is not None:
            row["effective_unit_cost"] = effective_unit_cost

        if basis == "lb":
            if current_price is not None:
                row["asp_lb"] = current_price
            if base_unit_cost is not None:
                row["base_cost_lb"] = base_unit_cost
            if effective_unit_cost is not None:
                row["effective_cost_lb"] = effective_unit_cost
                row["cost_lb"] = effective_unit_cost
        else:
            row["asp_lb"] = None
            row["base_cost_lb"] = None
            row["effective_cost_lb"] = None
            row["cost_lb"] = None
            row["minimum_price_lb"] = None
            row["target_price_lb"] = None
            row["asp_lb_gap_to_min"] = None
            row["asp_lb_gap_to_target"] = None

        prepared.append(row)
    return prepared


def _build_health_matrix_from_rows(
    sku_rows: Sequence[Dict[str, Any]],
    *,
    total_revenue: float,
    total_profit: float = 0.0,
) -> Dict[str, Any]:
    rows = [dict(row) for row in (sku_rows or []) if isinstance(row, dict)]
    velocity_values = [
        velocity
        for velocity in (_row_velocity(row) for row in rows)
        if velocity is not None
    ]
    profitability_values = [
        value
        for value in (_health_profitability_value(row) for row in rows)
        if value is not None
    ]
    velocity_p40 = _quantile_value(velocity_values, 0.40)
    velocity_p50 = _quantile_value(velocity_values, 0.50)
    velocity_p60 = _quantile_value(velocity_values, 0.60)
    profitability_p40 = _quantile_value(profitability_values, 0.40)
    profitability_p50 = _quantile_value(profitability_values, 0.50)
    profitability_p60 = _quantile_value(profitability_values, 0.60)

    def _band(value: float | None, *, low: float | None, mid: float | None, high: float | None) -> str:
        if value is None:
            return "low"
        if high is not None and value >= high:
            return "high"
        if low is not None and value <= low:
            return "low"
        if mid is not None and value >= mid:
            return "high"
        return "low"

    summary_by_quadrant: Dict[str, Dict[str, Any]] = {
        key: {"quadrant": key, "sku_count": 0, "revenue": 0.0, "profit": 0.0}
        for key in _HEALTH_QUADRANT_META
    }
    top_rows: List[Dict[str, Any]] = []

    for row in rows:
        velocity = _row_velocity(row)
        profitability = _health_profitability_value(row)
        velocity_band = _band(velocity, low=velocity_p40, mid=velocity_p50, high=velocity_p60)
        profitability_band = _band(profitability, low=profitability_p40, mid=profitability_p50, high=profitability_p60)
        if velocity_band == "high" and profitability_band == "high":
            quadrant = "protect"
        elif velocity_band == "high":
            quadrant = "fix_margin"
        elif profitability_band == "high":
            quadrant = "grow"
        else:
            quadrant = "rationalize"
        row["quadrant"] = quadrant
        revenue = _clean_num(row.get("revenue"))
        profit = _clean_num(row.get("profit"))
        summary = summary_by_quadrant[quadrant]
        summary["sku_count"] += 1
        summary["revenue"] += revenue
        summary["profit"] += profit
        top_rows.append(
            {
                "quadrant": quadrant,
                "sku": row.get("sku") or row.get("product_id"),
                "product_id": row.get("product_id") or row.get("sku"),
                "product_name": row.get("product_name"),
                "display_name": _row_display_name(row),
                "segment": row.get("segment"),
                "revenue": revenue,
                "profit": profit,
                "margin_pct": _safe_float(row.get("margin_pct")),
                "target_margin_pct": _safe_float(row.get("target_margin_pct")),
                "minimum_margin_pct": _safe_float(row.get("minimum_margin_pct")),
                "target_achievement_pct": _safe_float(row.get("target_achievement_pct")),
                "price_status_key": row.get("price_status_key"),
                "price_status": row.get("price_status"),
                "status_key": row.get("status_key"),
                "target_status": row.get("target_status"),
                "velocity_per_month": velocity,
            }
        )

    top_rows.sort(key=lambda item: (_clean_num(item.get("revenue")), _clean_num(item.get("profit"))), reverse=True)
    limited_top_rows: List[Dict[str, Any]] = []
    quadrant_counts: Dict[str, int] = {key: 0 for key in _HEALTH_QUADRANT_META}
    for row in top_rows:
        key = str(row.get("quadrant") or "").strip().lower()
        if key not in quadrant_counts:
            continue
        if quadrant_counts[key] >= 10:
            continue
        limited_top_rows.append(row)
        quadrant_counts[key] += 1

    return _build_health_matrix(
        list(summary_by_quadrant.values()),
        limited_top_rows,
        velocity_cutoff_low=velocity_p40,
        velocity_cutoff_high=velocity_p60,
        profitability_cutoff_low=profitability_p40,
        profitability_cutoff_high=profitability_p60,
        profitability_metric="target_achievement_gap_or_target_margin_gap",
        total_revenue=total_revenue,
        total_profit=total_profit,
    )


def _build_pricing_visual_payload(sku_rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    price_vs_velocity: List[Dict[str, Any]] = []
    performance_points: List[Dict[str, Any]] = []
    target_margin_values = [
        _safe_float(r.get("target_margin_pct"))
        for r in sku_rows
        if isinstance(r, dict) and _safe_float(r.get("target_margin_pct")) is not None
    ]
    minimum_margin_values = [
        _safe_float(r.get("minimum_margin_pct"))
        for r in sku_rows
        if isinstance(r, dict) and _safe_float(r.get("minimum_margin_pct")) is not None
    ]

    for raw_row in sku_rows:
        if not isinstance(raw_row, dict):
            continue
        r = raw_row
        current_price = _row_current_price_value(r)
        velocity = _row_velocity(r)
        pricing_basis = _row_pricing_basis(r)
        has_cost_visibility = _row_has_cost_visibility(r)
        has_pricing_reference = _row_has_pricing_reference(r)
        visual_status_key = _visual_status_key(r)
        visual_status = r.get("price_status") or r.get("target_status")

        if current_price is not None:
            price_vs_velocity.append(
                {
                    "sku": r.get("sku") or r.get("product_id"),
                    "name": r.get("product_name") or r.get("name"),
                    "display_name": _row_display_name(r),
                    "product_id": r.get("product_id"),
                    "product_name": r.get("product_name"),
                    "pricing_basis": pricing_basis,
                    "unit_price": _safe_float(r.get("unit_price")),
                    "current_price": current_price,
                    "current_unit_price": _safe_float(r.get("current_unit_price")) or _safe_float(r.get("unit_price")),
                    "asp_lb": _safe_float(r.get("asp_lb")),
                    "base_cost": _safe_float(r.get("base_cost")),
                    "effective_cost": _safe_float(r.get("effective_cost")) or _safe_float(r.get("cost")),
                    "base_unit_cost": _safe_float(r.get("base_unit_cost")),
                    "effective_unit_cost": _safe_float(r.get("effective_unit_cost")) or _safe_float(r.get("unit_cost")),
                    "base_cost_lb": _safe_float(r.get("base_cost_lb")),
                    "effective_cost_lb": _safe_float(r.get("effective_cost_lb")) or _safe_float(r.get("cost_lb")),
                    "cost_lb": _safe_float(r.get("cost_lb")),
                    "minimum_price": _safe_float(r.get("minimum_price")),
                    "target_price": _safe_float(r.get("target_price")),
                    "minimum_price_lb": _safe_float(r.get("minimum_price_lb")),
                    "target_price_lb": _safe_float(r.get("target_price_lb")),
                    "asp_lb_gap_to_min": _safe_float(r.get("asp_lb_gap_to_min")),
                    "asp_lb_gap_to_target": _safe_float(r.get("asp_lb_gap_to_target")),
                    "min_price_gap": _safe_float(r.get("min_price_gap")),
                    "target_price_gap": _safe_float(r.get("target_price_gap")),
                    "target_achievement_pct": _safe_float(r.get("target_achievement_pct")),
                    "profit_uplift_target": _safe_float(r.get("profit_uplift_target")),
                    "qty": _safe_float(r.get("qty")) or 0.0,
                    "weight": _safe_float(r.get("weight")) or 0.0,
                    "customer_count": _safe_float(r.get("customer_count")),
                    "velocity_per_month": velocity,
                    "orders_per_month": velocity,
                    "revenue": _safe_float(r.get("revenue")) or 0.0,
                    "revenue_share": _safe_float(r.get("revenue_share")),
                    "margin_pct": _safe_float(r.get("margin_pct")),
                    "minimum_margin_pct": _safe_float(r.get("minimum_margin_pct")),
                    "target_margin_pct": _safe_float(r.get("target_margin_pct")),
                    "uplift_pct": _safe_float(r.get("uplift_pct")),
                    "segment": r.get("segment"),
                    "protein_family": r.get("protein_family"),
                    "product_category": r.get("product_category"),
                    "status_key": r.get("status_key"),
                    "target_status": r.get("target_status"),
                    "price_status_key": r.get("price_status_key"),
                    "price_status": r.get("price_status"),
                    "visual_status_key": visual_status_key,
                    "visual_status": visual_status,
                    "top_customer_name": r.get("top_customer_name"),
                    "top_customer_share": _safe_float(r.get("top_customer_share")),
                    "top_region_name": r.get("top_region_name"),
                    "top_region_share": _safe_float(r.get("top_region_share")),
                    "needs_protein_mapping": bool(r.get("needs_protein_mapping")),
                    "has_cost": has_cost_visibility,
                    "has_pricing_reference": has_pricing_reference,
                }
            )

        if current_price is None:
            continue
        performance_points.append(
            {
                "sku": r.get("sku") or r.get("product_id"),
                "name": r.get("product_name") or r.get("name"),
                "display_name": _row_display_name(r),
                "product_id": r.get("product_id"),
                "product_name": r.get("product_name"),
                "pricing_basis": pricing_basis,
                "current_price": current_price,
                "current_unit_price": _safe_float(r.get("current_unit_price")) or _safe_float(r.get("unit_price")),
                "asp_lb": _safe_float(r.get("asp_lb")),
                "base_cost": _safe_float(r.get("base_cost")),
                "effective_cost": _safe_float(r.get("effective_cost")) or _safe_float(r.get("cost")),
                "base_unit_cost": _safe_float(r.get("base_unit_cost")),
                "effective_unit_cost": _safe_float(r.get("effective_unit_cost")) or _safe_float(r.get("unit_cost")),
                "base_cost_lb": _safe_float(r.get("base_cost_lb")),
                "effective_cost_lb": _safe_float(r.get("effective_cost_lb")) or _safe_float(r.get("cost_lb")),
                "cost_lb": _safe_float(r.get("cost_lb")),
                "minimum_price": _safe_float(r.get("minimum_price")),
                "target_price": _safe_float(r.get("target_price")),
                "minimum_price_lb": _safe_float(r.get("minimum_price_lb")),
                "target_price_lb": _safe_float(r.get("target_price_lb")),
                "uplift_pct": _safe_float(r.get("uplift_pct")),
                "revenue_share": _safe_float(r.get("revenue_share")),
                "revenue": _safe_float(r.get("revenue")) or 0.0,
                "profit": _safe_float(r.get("profit")) or 0.0,
                "qty": _safe_float(r.get("qty")) or 0.0,
                "weight": _safe_float(r.get("weight")) or 0.0,
                "customer_count": _safe_float(r.get("customer_count")),
                "region_breadth": _safe_float(r.get("region_breadth")),
                "velocity_per_month": velocity,
                "orders_per_month": velocity,
                "segment": r.get("segment"),
                "protein_family": r.get("protein_family"),
                "product_category": r.get("product_category"),
                "margin_pct": _safe_float(r.get("margin_pct")),
                "minimum_margin_pct": _safe_float(r.get("minimum_margin_pct")),
                "target_margin_pct": _safe_float(r.get("target_margin_pct")),
                "min_price_gap": _safe_float(r.get("min_price_gap")),
                "target_price_gap": _safe_float(r.get("target_price_gap")),
                "asp_lb_gap_to_min": _safe_float(r.get("asp_lb_gap_to_min")),
                "asp_lb_gap_to_target": _safe_float(r.get("asp_lb_gap_to_target")),
                "target_achievement_pct": _safe_float(r.get("target_achievement_pct")),
                "profit_uplift_target": _safe_float(r.get("profit_uplift_target")),
                "status_key": r.get("status_key"),
                "target_status": r.get("target_status"),
                "status_color": r.get("status_color"),
                "price_status_key": r.get("price_status_key"),
                "price_status": r.get("price_status"),
                "visual_status_key": visual_status_key,
                "visual_status": visual_status,
                "top_customer_name": r.get("top_customer_name"),
                "top_customer_share": _safe_float(r.get("top_customer_share")),
                "top_region_name": r.get("top_region_name"),
                "top_region_share": _safe_float(r.get("top_region_share")),
                "needs_protein_mapping": bool(r.get("needs_protein_mapping")),
                "risk_flag": _row_below_minimum(r),
                "has_cost": has_cost_visibility,
                "has_pricing_reference": has_pricing_reference,
            }
        )

    total_point_revenue = sum(_safe_float(item.get("revenue")) or 0.0 for item in performance_points)
    legend_rows: List[Dict[str, Any]] = []
    for key in ("red", "orange", "yellow", "light_green", "green", "needs_mapping", "no_cost"):
        bucket_rows = [item for item in performance_points if str(item.get("visual_status_key") or item.get("status_key") or "").lower() == key]
        if not bucket_rows:
            continue
        bucket_revenue = sum(_safe_float(item.get("revenue")) or 0.0 for item in bucket_rows)
        meta = margin_rules.status_meta(key)
        legend_rows.append(
            {
                "key": key,
                "label": meta.get("label"),
                "short_label": meta.get("short_label"),
                "color": meta.get("color"),
                "sku_count": len(bucket_rows),
                "revenue": bucket_revenue,
                "revenue_share": (bucket_revenue / total_point_revenue * 100.0) if total_point_revenue > 0 else None,
            }
        )

    summary_definitions = [
        {
            "key": "below_minimum",
            "label": "Below minimum",
            "status_key": "orange",
            "statuses": {"red", "orange"},
            "quick_filters": ["below_minimum_margin"],
            "section": "pricing",
            "mode": "analyst",
            "emphasis": "profit",
        },
        {
            "key": "below_target",
            "label": "Below target",
            "status_key": "yellow",
            "statuses": {"yellow"},
            "quick_filters": ["below_target_margin"],
            "section": "pricing",
            "mode": "analyst",
            "emphasis": "profit",
        },
        {
            "key": "near_target",
            "label": "Near target",
            "status_key": "light_green",
            "statuses": {"light_green"},
            "quick_filters": ["at_or_above_target"],
            "section": "pricing",
            "mode": "analyst",
            "emphasis": "profit",
        },
        {
            "key": "above_target",
            "label": "Above target",
            "status_key": "green",
            "statuses": {"green"},
            "quick_filters": ["at_or_above_target"],
            "section": "table",
            "mode": "analyst",
            "emphasis": "revenue",
        },
        {
            "key": "needs_attention",
            "label": "Mapping / cost gaps",
            "status_key": "needs_mapping",
            "statuses": {"needs_mapping", "no_cost"},
            "quick_filters": ["missing_cost"],
            "section": "execution",
            "mode": "analyst",
            "emphasis": "profit",
        },
    ]
    summary_cards: List[Dict[str, Any]] = []
    for definition in summary_definitions:
        statuses = definition["statuses"]
        revenue_value = sum(
            (_safe_float(item.get("revenue")) or 0.0)
            for item in performance_points
            if str(item.get("visual_status_key") or item.get("status_key") or "").lower() in statuses
        )
        summary_cards.append(
            {
                "key": definition["key"],
                "label": definition["label"],
                "status_key": definition["status_key"],
                "sku_count": sum(
                    1 for item in performance_points if str(item.get("visual_status_key") or item.get("status_key") or "").lower() in statuses
                ),
                "revenue": revenue_value,
                "revenue_share": (revenue_value / total_point_revenue * 100.0) if total_point_revenue > 0 else None,
                "quick_filters": definition["quick_filters"],
                "section": definition["section"],
                "mode": definition["mode"],
                "emphasis": definition["emphasis"],
            }
        )

    return {
        "price_vs_velocity": price_vs_velocity,
        "performance_points": performance_points,
        "legend_rows": legend_rows,
        "summary_cards": summary_cards,
        "target_margin_values": target_margin_values,
        "minimum_margin_values": minimum_margin_values,
    }


def _execution_log_score(value: Any, *, weight: float, cap: float) -> float:
    parsed = max(0.0, _safe_float(value) or 0.0)
    if parsed <= 0:
        return 0.0
    return min(cap, math.log1p(parsed) * weight)


def _execution_severity_bonus(status_key: Any) -> float:
    key = str(status_key or "").strip().lower()
    if key == "red":
        return 180.0
    if key == "orange":
        return 145.0
    if key == "yellow":
        return 92.0
    if key in {"needs_mapping", "no_cost"}:
        return 115.0
    if key == "light_green":
        return 28.0
    if key == "green":
        return 12.0
    return 0.0


def _execution_base_row(
    row: Dict[str, Any],
    *,
    action: str,
    reason: str,
    quick_filters: List[str],
    section: str,
    emphasis: str,
    priority_score: float,
) -> Dict[str, Any]:
    gap_to_target = _row_gap_to_target(row)
    gap_to_minimum = _row_gap_to_minimum(row)
    velocity = _row_velocity(row)
    display_name = _row_display_name(row)
    sku = row.get("sku") or row.get("product_id")
    return {
        "product_id": row.get("product_id") or sku,
        "sku": sku,
        "display_name": display_name,
        "product_name": row.get("product_name") or row.get("name"),
        "segment": row.get("segment"),
        "protein_family": row.get("protein_family") or row.get("rule_family"),
        "product_category": row.get("product_category"),
        "revenue": _clean_num(row.get("revenue")),
        "profit": _clean_num(row.get("profit")),
        "margin_pct": _safe_float(row.get("margin_pct")),
        "minimum_margin_pct": _safe_float(row.get("minimum_margin_pct")),
        "target_margin_pct": _safe_float(row.get("target_margin_pct")),
        "orders_per_month": velocity,
        "velocity_per_month": velocity,
        "customer_count": _safe_float(row.get("customer_count")),
        "top_customer_share": _safe_float(row.get("top_customer_share")),
        "top_customer_name": row.get("top_customer_name"),
        "top_region_name": row.get("top_region_name"),
        "gap_to_target": gap_to_target,
        "gap_to_minimum": gap_to_minimum,
        "target_achievement_pct": _safe_float(row.get("target_achievement_pct")),
        "profit_uplift_target": _clean_num(row.get("profit_uplift_target")),
        "status_key": row.get("status_key"),
        "target_status": row.get("target_status"),
        "action": action,
        "reason": reason,
        "quick_filters": quick_filters,
        "section": section,
        "mode": "analyst",
        "emphasis": emphasis,
        "priority_score": round(priority_score, 2),
    }


def _build_execution_lists_from_rows(
    sku_rows: List[Dict[str, Any]],
    *,
    velocity_cutoff: float | None = None,
    limit: int | None = 10,
) -> Dict[str, Any]:
    annotated_rows = _annotate_product_rows([dict(row) for row in sku_rows if isinstance(row, dict)])
    velocity_values = [_row_velocity(row) for row in annotated_rows if _row_velocity(row) > 0]
    derived_velocity_guard = velocity_cutoff
    if derived_velocity_guard is None and velocity_values:
        ordered_velocity = sorted(velocity_values)
        derived_velocity_guard = ordered_velocity[max(0, int(len(ordered_velocity) * 0.65) - 1)]
    velocity_guard = max(1.0, derived_velocity_guard or 0.0)
    pricing_fixes: List[Dict[str, Any]] = []
    cost_fixes: List[Dict[str, Any]] = []
    promote_candidates: List[Dict[str, Any]] = []
    for row in annotated_rows:
        revenue = _clean_num(row.get("revenue"))
        velocity = _row_velocity(row)
        margin_pct = _safe_float(row.get("margin_pct"))
        minimum_margin_pct = _safe_float(row.get("minimum_margin_pct"))
        target_margin_pct = _safe_float(row.get("target_margin_pct"))
        customer_count = _safe_float(row.get("customer_count")) or 0.0
        target_achievement_pct = _safe_float(row.get("target_achievement_pct"))
        profit_uplift_target = _clean_num(row.get("profit_uplift_target"))
        gap_to_target = _row_gap_to_target(row)
        gap_to_minimum = _row_gap_to_minimum(row)
        target_gap_shortfall = abs(gap_to_target) if gap_to_target is not None and gap_to_target < 0 else 0.0
        minimum_gap_shortfall = abs(gap_to_minimum) if gap_to_minimum is not None and gap_to_minimum < 0 else 0.0
        target_margin_gap_pp = max(0.0, (target_margin_pct or 0.0) - (margin_pct or 0.0))
        minimum_margin_gap_pp = max(0.0, (minimum_margin_pct or 0.0) - (margin_pct or 0.0))
        has_cost = (_safe_float(row.get("unit_cost")) is not None) or (_safe_float(row.get("cost")) is not None)
        status_key = str(row.get("status_key") or "").strip().lower()
        severity_bonus = _execution_severity_bonus(status_key)
        revenue_score = _execution_log_score(revenue, weight=19.0, cap=145.0)
        velocity_score = min(90.0, velocity * 7.0)
        customer_score = min(42.0, customer_count * 3.5)
        uplift_score = _execution_log_score(profit_uplift_target, weight=18.0, cap=120.0)
        achievement_score = max(0.0, (100.0 - target_achievement_pct) * 0.8) if target_achievement_pct is not None else 0.0

        if (not has_cost) or status_key in {"needs_mapping", "no_cost"}:
            cost_action = "Map protein / category" if status_key == "needs_mapping" else "Review cost data"
            cost_reason = (
                "Protein/category mapping is missing, so the page cannot apply the correct minimum and target gross-margin gates."
                if status_key == "needs_mapping"
                else "Missing product cost blocks minimum and target price guidance on a commercially active SKU."
            )
            cost_score = severity_bonus + revenue_score + velocity_score + customer_score
            if status_key == "needs_mapping":
                cost_score += 30.0
            cost_fixes.append(
                _execution_base_row(
                    row,
                    action=cost_action,
                    reason=cost_reason,
                    quick_filters=["missing_cost"],
                    section="execution",
                    emphasis="profit",
                    priority_score=cost_score,
                )
            )
            continue

        if _row_below_target(row):
            near_target_easy_win = target_gap_shortfall > 0 and target_gap_shortfall <= 0.25 and velocity >= max(1.0, velocity_guard * 0.8)
            pricing_score = severity_bonus + revenue_score + velocity_score + customer_score + uplift_score + achievement_score
            pricing_score += (target_margin_gap_pp * 7.0) + (minimum_margin_gap_pp * 10.0)
            pricing_score += (target_gap_shortfall * 52.0) + (minimum_gap_shortfall * 80.0)
            if _row_below_minimum(row):
                pricing_score += 70.0
                action = "Recover minimum price"
                reason = (
                    f"Below minimum by ${minimum_gap_shortfall:,.2f} with ${revenue:,.0f} revenue at risk; recover the floor before broader pricing actions."
                    if minimum_gap_shortfall > 0
                    else "Below the minimum gross-margin gate with meaningful revenue exposure."
                )
                quick_filters = ["below_minimum_margin"]
            elif near_target_easy_win:
                pricing_score += 32.0
                action = "Close easy target gap"
                reason = (
                    f"Within ${target_gap_shortfall:,.2f} of target on a high-velocity SKU; this is a low-friction margin recovery move."
                    if target_gap_shortfall > 0
                    else "Near target on a fast mover; recover the remaining gap without disrupting volume."
                )
                quick_filters = ["recover_margin"]
            else:
                action = "Recover target price"
                reason = (
                    f"Below target by ${target_gap_shortfall:,.2f} with ${profit_uplift_target:,.0f} of profit leakage at target pricing."
                    if target_gap_shortfall > 0 and profit_uplift_target > 0
                    else "Below the protein-specific target gross margin on a commercially relevant SKU."
                )
                quick_filters = ["recover_margin"]
            pricing_fixes.append(
                _execution_base_row(
                    row,
                    action=action,
                    reason=reason,
                    quick_filters=quick_filters,
                    section="pricing",
                    emphasis="profit",
                    priority_score=pricing_score,
                )
            )
            continue

        if _row_above_target(row):
            margin_surplus_pp = max(0.0, (margin_pct or 0.0) - (target_margin_pct or 0.0))
            velocity_headroom = max(0.0, velocity_guard - velocity)
            if velocity <= velocity_guard or margin_surplus_pp >= 5.0 or (target_achievement_pct or 0.0) >= 110.0:
                promote_score = revenue_score + customer_score + (margin_surplus_pp * 7.0) + (velocity_headroom * 8.0)
                if target_achievement_pct is not None:
                    promote_score += max(0.0, target_achievement_pct - 100.0) * 0.7
                reason = (
                    f"Margin is {margin_surplus_pp:.1f} pp above target but velocity is only {velocity:.1f}/mo; use sales coverage or feature support to scale it."
                    if velocity <= velocity_guard
                    else "Healthy margin profile with room to push distribution and share."
                )
                promote_candidates.append(
                    _execution_base_row(
                        row,
                        action="Promote / Expand distribution",
                        reason=reason,
                        quick_filters=["promote_candidate"],
                        section="execution",
                        emphasis="revenue",
                        priority_score=promote_score,
                    )
                )

    pricing_fixes.sort(
        key=lambda item: (
            _clean_num(item.get("priority_score")),
            _clean_num(item.get("revenue")),
            _safe_float(item.get("orders_per_month")) or 0.0,
        ),
        reverse=True,
    )
    cost_fixes.sort(
        key=lambda item: (
            _clean_num(item.get("priority_score")),
            _clean_num(item.get("revenue")),
            _safe_float(item.get("orders_per_month")) or 0.0,
        ),
        reverse=True,
    )
    promote_candidates.sort(
        key=lambda item: (
            _clean_num(item.get("priority_score")),
            _clean_num(item.get("revenue")),
            _safe_float(item.get("orders_per_month")) or 0.0,
        ),
        reverse=True,
    )
    pricing_output = pricing_fixes if limit is None else pricing_fixes[:limit]
    cost_output = cost_fixes if limit is None else cost_fixes[:limit]
    promote_output = promote_candidates if limit is None else promote_candidates[:limit]
    return {
        "pricing_fixes": pricing_output,
        "cost_fixes": cost_output,
        "promote_candidates": promote_output,
    }


def _mover_status(current_revenue: float, prior_revenue: float) -> tuple[str, float | None, bool]:
    if prior_revenue <= 0 and current_revenue > 0:
        return "New", None, False
    if current_revenue <= 0 and prior_revenue > 0:
        return "Lost", -100.0, False
    if prior_revenue <= 0:
        return "Stable", None, False
    delta_pct = ((current_revenue - prior_revenue) / prior_revenue) * 100.0
    low_base = prior_revenue < LOW_BASE_REVENUE
    if low_base:
        return "Low base", None, True
    if delta_pct >= 5:
        return "Growing", delta_pct, False
    if delta_pct <= -5:
        return "Declining", delta_pct, False
    return "Stable", delta_pct, False


_HEALTH_QUADRANT_META: dict[str, dict[str, str]] = {
    "protect": {
        "label": "Protect",
        "tone": "success",
        "description": "High velocity and high profitability. Protect availability and avoid unnecessary discounting.",
    },
    "fix_margin": {
        "label": "Fix Margin",
        "tone": "warning",
        "description": "High velocity with low profitability. Prioritize pricing, pack, and cost correction.",
    },
    "grow": {
        "label": "Grow",
        "tone": "info",
        "description": "Strong profitability but low velocity. Promote with targeted distribution and sales plays.",
    },
    "rationalize": {
        "label": "Rationalize",
        "tone": "neutral",
        "description": "Low velocity and low profitability. Review assortment and discontinuation candidates.",
    },
}


def _build_health_matrix(
    summary_rows: List[Dict[str, Any]],
    top_rows: List[Dict[str, Any]],
    *,
    velocity_cutoff_low: float | None = None,
    velocity_cutoff_high: float | None = None,
    profitability_cutoff_low: float | None = None,
    profitability_cutoff_high: float | None = None,
    profitability_metric: str = "margin_pct_or_contribution_lb",
    velocity_cutoff: float | None = None,
    margin_cutoff: float | None = None,
    total_revenue: float,
    total_profit: float = 0.0,
) -> Dict[str, Any]:
    def _row_dict(value: Any) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}

    def _clean_text(value: Any) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        if not text or text.lower() in {"nan", "<na>", "none"}:
            return ""
        return text

    if velocity_cutoff_low is None and velocity_cutoff is not None:
        velocity_cutoff_low = velocity_cutoff
    if velocity_cutoff_high is None and velocity_cutoff is not None:
        velocity_cutoff_high = velocity_cutoff
    if profitability_cutoff_low is None and margin_cutoff is not None:
        profitability_cutoff_low = margin_cutoff
    if profitability_cutoff_high is None and margin_cutoff is not None:
        profitability_cutoff_high = margin_cutoff

    quadrants: dict[str, Dict[str, Any]] = {}
    for key, meta in _HEALTH_QUADRANT_META.items():
        quadrants[key] = {
            "key": key,
            "label": meta["label"],
            "tone": meta["tone"],
            "description": meta["description"],
            "sku_count": 0,
            "revenue": 0.0,
            "profit": 0.0,
            "revenue_share": 0.0,
            "profit_share": 0.0,
            "top_items": [],
        }

    for row in summary_rows or []:
        row_dict = _row_dict(row)
        key = _clean_text(row_dict.get("quadrant")).lower()
        if key not in quadrants:
            continue
        revenue = _clean_num(row_dict.get("revenue"))
        profit = _clean_num(row_dict.get("profit"))
        sku_count_num = _safe_float(row_dict.get("sku_count"))
        quadrants[key]["sku_count"] = int(sku_count_num) if sku_count_num is not None else 0
        quadrants[key]["revenue"] = revenue
        quadrants[key]["profit"] = profit
        quadrants[key]["revenue_share"] = (revenue / total_revenue * 100.0) if total_revenue else 0.0
        quadrants[key]["profit_share"] = (profit / total_profit * 100.0) if total_profit else 0.0

    for row in top_rows or []:
        row_dict = _row_dict(row)
        key = _clean_text(row_dict.get("quadrant")).lower()
        if key not in quadrants:
            continue
        display_name = _clean_text(row_dict.get("display_name")) or _clean_text(row_dict.get("product_name"))
        quadrants[key]["top_items"].append(
            {
                "sku": row_dict.get("sku") or row_dict.get("product_id"),
                "product_id": row_dict.get("product_id"),
                "display_name": display_name,
                "product_name": row_dict.get("product_name"),
                "segment": row_dict.get("segment"),
                "revenue": _clean_num(row_dict.get("revenue")),
                "profit": _clean_num(row_dict.get("profit")),
                "margin_pct": _safe_float(row_dict.get("margin_pct")),
                "target_margin_pct": _safe_float(row_dict.get("target_margin_pct")),
                "minimum_margin_pct": _safe_float(row_dict.get("minimum_margin_pct")),
                "target_achievement_pct": _safe_float(row_dict.get("target_achievement_pct")),
                "price_status_key": row_dict.get("price_status_key"),
                "price_status": row_dict.get("price_status"),
                "status_key": row_dict.get("status_key"),
                "target_status": row_dict.get("target_status"),
                "velocity_per_month": _safe_float(row_dict.get("velocity_per_month")),
                "quadrant": key,
            }
        )

    ordered = [quadrants[key] for key in ("protect", "fix_margin", "grow", "rationalize")]
    return {
        "velocity_cutoff_low": _safe_float(velocity_cutoff_low),
        "velocity_cutoff_high": _safe_float(velocity_cutoff_high),
        "profitability_cutoff_low": _safe_float(profitability_cutoff_low),
        "profitability_cutoff_high": _safe_float(profitability_cutoff_high),
        "profitability_metric": profitability_metric,
        "quadrants": ordered,
    }


def _encode_path_segment(value: Any) -> str:
    """Encode a value for safe use inside a URL path segment."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return quote(text, safe="")


def _parse_segments(arg_val: Any) -> List[str]:
    if not arg_val:
        return []
    raw = str(arg_val)
    parts = [p.strip() for p in raw.replace(";", ",").split(",")]
    return [p for p in parts if p]


def _safe_iter_values(seq):
    for v in seq:
        if v is None:
            yield 0.0
            continue
        try:
            yield float(v)
        except Exception:
            yield 0.0


def _project_next_month(
    labels: List[str],
    revenues: List[float],
    *,
    comparison: Dict[str, Any] | None = None,
    current_revenue: float | None = None,
) -> Dict[str, Any]:
    partial_period = bool((comparison or {}).get("terminal_period_incomplete"))
    method = str((comparison or {}).get("method") or "")
    if partial_period and method == "month_to_date_vs_prior_month_same_day" and current_revenue is not None:
        current_end = _coerce_date((comparison or {}).get("current_end"))
        current_days = max(1, int((comparison or {}).get("current_days") or 0))
        if current_end is not None:
            month_days = _month_end(current_end).day
            pace = float(current_revenue) / max(1, current_days)
            return {
                "value": pace * month_days,
                "method": "mtd_daily_pace",
                "confidence": "medium",
                "note": f"Month-to-date pace normalized to a {month_days}-day month",
            }

    clean_revs = list(_safe_iter_values(revenues or []))
    if not labels or not clean_revs:
        return {"value": None, "method": "insufficient", "confidence": "low", "note": "Insufficient history"}
    revs = clean_revs[:-1] if partial_period and len(clean_revs) > 1 else clean_revs
    if len(revs) < 2:
        return {"value": None, "method": "insufficient", "confidence": "low", "note": "Insufficient history"}
    window = revs[-3:] if len(revs) >= 3 else revs
    avg = sum(window) / len(window) if window else 0.0
    value = avg
    method = "avg_last_3"
    confidence = "low"
    note = "Avg last 3 months"
    if len(revs) >= 13 and revs[-13] > 0:
        yoy_factor = revs[-1] / revs[-13]
        value = avg * yoy_factor
        method = "avg_last_3_yoy"
        note = "Avg last 3 months \u00b7 YoY adjusted"
    if len(revs) >= 24:
        confidence = "high"
    elif len(revs) >= 12:
        confidence = "medium"
    return {"value": value, "method": method, "confidence": confidence, "note": note}


def _classify_recommendation(
    *,
    status_key: str | None,
    dispersion: float | None,
    momentum: float | None,
    uplift_pct: float | None,
    target_price_gap: float | None,
    target_achievement_pct: float | None,
    top_customer_share: float | None,
    velocity: float | None,
) -> tuple[str, str, str]:
    action = "Hold"
    quick = "Hold"
    rationale_parts: List[str] = []
    normalized_status = str(status_key or "").strip().lower()
    if dispersion is not None:
        rationale_parts.append(f"Dispersion {dispersion:.2f}x")
    if momentum is not None:
        rationale_parts.append(f"Momentum {momentum * 100:.1f}%")
    if uplift_pct is not None:
        rationale_parts.append(f"Uplift {uplift_pct:.1f}%")
    if target_price_gap is not None:
        rationale_parts.append(f"Gap ${target_price_gap:,.2f}")
    if target_achievement_pct is not None:
        rationale_parts.append(f"Target achievement {target_achievement_pct:.1f}%")
    if top_customer_share is not None and top_customer_share >= 50:
        rationale_parts.append(f"Top customer {top_customer_share:.1f}%")

    high_disp = dispersion is not None and dispersion >= 1.4
    low_disp = dispersion is not None and dispersion <= 1.1
    strong = momentum is not None and momentum >= 0.15
    declining = momentum is not None and momentum <= -0.1
    slow_velocity = velocity is not None and velocity < 3.0

    if normalized_status == "no_cost":
        action = "Review cost coverage"
        quick = "Cost"
    elif normalized_status == "needs_mapping":
        action = "Fix protein mapping"
        quick = "Map"
    elif normalized_status in {"red", "orange"}:
        action = "Recover minimum price"
        quick = "Urgent"
    elif normalized_status == "yellow":
        action = "Recover target price"
        quick = "Raise"
    elif high_disp and strong:
        action = "Standardize price"
        quick = "Review"
    elif low_disp and declining:
        action = "Promote / bundle"
        quick = "Promo"
    elif top_customer_share is not None and top_customer_share >= 50:
        action = "Protect concentrated account"
        quick = "Focus"
    elif slow_velocity and normalized_status in {"light_green", "green"}:
        action = "Promote / expand distribution"
        quick = "Grow"
    elif strong:
        action = "Protect stock"
        quick = "Hold"
    elif declining:
        action = "Promote / review"
        quick = "Promo"
    elif uplift_pct is not None and uplift_pct >= 10:
        action = "Raise price"
        quick = "Raise"
    elif uplift_pct is not None and uplift_pct <= -10:
        action = "Reduce price"
        quick = "Promo"

    rationale = "; ".join(rationale_parts) if rationale_parts else "Insufficient signals"
    return action, quick, rationale


def _build_recommendations(sku_rows: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], Dict[str, str], Dict[str, str]]:
    recs: List[Dict[str, Any]] = []
    quick_rec_map: Dict[str, str] = {}
    action_map: Dict[str, str] = {}

    for r in sku_rows:
        if not isinstance(r, dict):
            continue
        sku = r.get("sku") or r.get("product_id") or r.get("key")
        name = r.get("product_name") or r.get("name") or sku
        display_name = r.get("display_name") or (f"{sku}  {name}" if sku and name and sku != name else (sku or name))
        if not sku:
            continue
        up_p10 = _safe_float(r.get("up_p10"))
        up_p90 = _safe_float(r.get("up_p90"))
        dispersion = (up_p90 / up_p10) if (up_p10 and up_p90 and up_p10 > 0) else None
        rev_recent = _safe_float(r.get("rev_recent"))
        rev_prior = _safe_float(r.get("rev_prior"))
        momentum = None
        if rev_recent is not None and rev_prior is not None and rev_prior > 0:
            momentum = (rev_recent - rev_prior) / rev_prior
        uplift_pct = _safe_float(r.get("uplift_pct"))
        uplift_est = uplift_pct
        if uplift_est is None and dispersion is not None:
            uplift_est = (dispersion - 1.0) * 100.0
        target_price_gap = _safe_float(r.get("asp_lb_gap_to_target"))
        if target_price_gap is None:
            target_price_gap = _safe_float(r.get("target_price_gap"))
        target_achievement_pct = _safe_float(r.get("target_achievement_pct"))
        top_customer_share = _safe_float(r.get("top_customer_share"))
        velocity = _safe_float(r.get("orders_per_month"))
        status_key = str(r.get("status_key") or "").strip().lower()

        action, quick, rationale = _classify_recommendation(
            status_key=status_key,
            dispersion=dispersion,
            momentum=momentum,
            uplift_pct=uplift_pct,
            target_price_gap=target_price_gap,
            target_achievement_pct=target_achievement_pct,
            top_customer_share=top_customer_share,
            velocity=velocity,
        )

        score = 0.0
        if uplift_est is not None:
            score += abs(uplift_est)
        if momentum is not None:
            score += abs(momentum) * 100.0
        score += _safe_float(r.get("revenue_share") or 0.0) or 0.0
        if target_achievement_pct is not None and target_achievement_pct < 100.0:
            score += max(0.0, 100.0 - target_achievement_pct)
        if target_price_gap is not None and target_price_gap < 0:
            score += min(50.0, abs(target_price_gap) * 10.0)
        if status_key == "red":
            score += 120.0
        elif status_key == "orange":
            score += 90.0
        elif status_key == "yellow":
            score += 60.0
        elif status_key in {"needs_mapping", "no_cost"}:
            score += 40.0
        if top_customer_share is not None and top_customer_share >= 50.0:
            score += min(25.0, top_customer_share / 4.0)

        recs.append(
            {
                "sku": sku,
                "product_id": r.get("product_id") or sku,
                "name": name,
                "display_name": display_name,
                "action": action,
                "rationale": rationale,
                "uplift_pct_est": uplift_est,
                "revenue": _clean_num(r.get("revenue")),
                "profit": _clean_num(r.get("profit")),
                "margin_pct": _safe_float(r.get("margin_pct")),
                "minimum_margin_pct": _safe_float(r.get("minimum_margin_pct")),
                "target_margin_pct": _safe_float(r.get("target_margin_pct")),
                "target_achievement_pct": target_achievement_pct,
                "top_customer_name": r.get("top_customer_name"),
                "top_customer_share": top_customer_share,
                "top_region_name": r.get("top_region_name"),
                "top_region_share": _safe_float(r.get("top_region_share")),
                "target_price_gap": target_price_gap,
                "status_key": r.get("status_key"),
                "target_status": r.get("target_status"),
                "priority": round(score, 2),
            }
        )
        quick_rec_map[str(sku)] = quick
        action_map[str(sku)] = action

    recs.sort(key=lambda x: x.get("priority", 0), reverse=True)
    return recs[:25], quick_rec_map, action_map


def _clean_label_text(value: Any, default: str = "Unassigned") -> str:
    text = str(value or "").strip()
    return text or default


def _protein_family_name(row: Dict[str, Any]) -> str:
    return _clean_label_text(
        row.get("family")
        or row.get("protein_family")
        or row.get("protein_type")
        or row.get("protein_name")
        or row.get("protein")
        or row.get("category")
        or row.get("product_category")
    )


def _protein_category_name(row: Dict[str, Any], family: str) -> str:
    return _clean_label_text(row.get("category") or row.get("product_category") or family, family)


def _build_protein_intelligence(
    protein_insights: Dict[str, Any],
    sku_rows: List[Dict[str, Any]],
    execution_lists: Dict[str, Any],
) -> Dict[str, Any]:
    summary = dict((protein_insights or {}).get("summary") or {})
    mix_rows = margin_rules.annotate_margin_rows(
        [dict(row) for row in ((protein_insights or {}).get("mix") or []) if isinstance(row, dict)],
        protein_keys=("family", "protein_family", "protein", "category"),
        category_keys=("category", "product_category", "family"),
    )
    mix_shift_rows = [dict(row) for row in ((protein_insights or {}).get("mix_shift") or []) if isinstance(row, dict)]
    margin_watch_rows = [dict(row) for row in ((protein_insights or {}).get("margin_watch") or []) if isinstance(row, dict)]
    growth_rows = [dict(row) for row in ((protein_insights or {}).get("growth_pockets") or []) if isinstance(row, dict)]

    if not summary and mix_rows:
        lead = max(mix_rows, key=lambda row: _clean_num(row.get("revenue")))
        summary = {
            "family_count": len(mix_rows),
            "top_family": _protein_family_name(lead),
            "top_family_share": _safe_float(lead.get("share_current")),
            "concentration_hhi": None,
        }

    family_rollup: dict[str, Dict[str, Any]] = {}
    for row in mix_rows:
        family = _protein_family_name(row)
        entry = dict(row)
        entry["family"] = family
        entry["category"] = _protein_category_name(row, family)
        family_rollup[family] = entry

    sku_rollup: dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "revenue": 0.0,
            "profit": 0.0,
            "sku_count": 0,
            "pricing_candidate_count": 0,
            "below_target_count": 0,
            "missing_cost_count": 0,
            "promote_count": 0,
            "high_velocity_count": 0,
            "uplift_revenue": 0.0,
            "uplift_weight": 0.0,
            "top_sku": None,
            "top_sku_revenue": 0.0,
            "core_revenues": [],
        }
    )
    sku_family_lookup: Dict[str, str] = {}
    for row in sku_rows or []:
        if not isinstance(row, dict):
            continue
        family = _protein_family_name(row)
        agg = sku_rollup[family]
        revenue = _clean_num(row.get("revenue"))
        profit = _clean_num(row.get("profit"))
        margin_pct = _safe_float(row.get("margin_pct"))
        uplift_pct = _safe_float(row.get("uplift_pct"))
        velocity = _safe_float(row.get("orders_per_month"))
        sku = row.get("sku") or row.get("product_id") or row.get("key")
        agg["revenue"] += revenue
        agg["profit"] += profit
        agg["sku_count"] += 1
        agg["core_revenues"].append(revenue)
        if margin_pct is None:
            agg["missing_cost_count"] += 1
        elif _row_below_target(row):
            agg["below_target_count"] += 1
        if uplift_pct is not None and uplift_pct > 0:
            agg["uplift_revenue"] += revenue * uplift_pct
            agg["uplift_weight"] += revenue
        if uplift_pct is not None and uplift_pct >= 3:
            agg["pricing_candidate_count"] += 1
        elif _row_below_target(row):
            agg["pricing_candidate_count"] += 1
        if _row_above_target(row) and (velocity or 0.0) > 0 and (velocity or 0.0) < 3:
            agg["promote_count"] += 1
        if (velocity or 0.0) >= 3:
            agg["high_velocity_count"] += 1
        if revenue > agg["top_sku_revenue"]:
            agg["top_sku_revenue"] = revenue
            agg["top_sku"] = row.get("display_name") or row.get("product_name") or sku
        if sku:
            sku_family_lookup[str(sku)] = family

    execution_rollup: dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "family": "Unassigned",
            "pricing_fixes": 0,
            "cost_gaps": 0,
            "promote_candidates": 0,
            "revenue": 0.0,
            "top_item": None,
            "top_revenue": 0.0,
        }
    )
    for list_key, counter_key in (
        ("pricing_fixes", "pricing_fixes"),
        ("cost_fixes", "cost_gaps"),
        ("promote_candidates", "promote_candidates"),
    ):
        for row in (execution_lists or {}).get(list_key) or []:
            if not isinstance(row, dict):
                continue
            sku = row.get("product_id") or row.get("sku")
            family = sku_family_lookup.get(str(sku)) if sku is not None else None
            family = family or _protein_family_name(row)
            bucket = execution_rollup[family]
            bucket["family"] = family
            bucket[counter_key] += 1
            revenue = _clean_num(row.get("revenue"))
            bucket["revenue"] += revenue
            if revenue > bucket["top_revenue"]:
                bucket["top_revenue"] = revenue
                bucket["top_item"] = row.get("display_name") or row.get("product_name") or sku

    leaders: List[Dict[str, Any]] = []
    portfolio: List[Dict[str, Any]] = []
    for row in sorted(mix_rows, key=lambda item: _clean_num(item.get("revenue")), reverse=True):
        family = _protein_family_name(row)
        category = _protein_category_name(row, family)
        sku_agg = sku_rollup.get(family) or {}
        core_revenues = sorted((sku_agg.get("core_revenues") or []), reverse=True)
        family_revenue = _clean_num(row.get("revenue")) or _clean_num(sku_agg.get("revenue"))
        core_share = (
            (sum(core_revenues[:3]) / family_revenue * 100.0)
            if family_revenue and core_revenues
            else None
        )
        long_tail_share = (100.0 - core_share) if core_share is not None else None
        share_current = _safe_float(row.get("share_current"))
        margin_pct = _safe_float(row.get("margin_pct"))
        share_delta_pp = _safe_float(row.get("share_delta_pp"))
        leader_tone = "stable"
        if _row_below_target(row):
            leader_tone = "risk"
        elif share_delta_pp is not None and share_delta_pp >= 1.0:
            leader_tone = "opportunity"
        elif share_current is not None and share_current >= 35.0:
            leader_tone = "concentration"
        leaders.append(
            {
                "family": family,
                "category": category,
                "revenue": family_revenue,
                "share_current": share_current,
                "share_delta_pp": share_delta_pp,
                "margin_pct": margin_pct,
                "target_margin_pct": _safe_float(row.get("target_margin_pct")),
                "minimum_margin_pct": _safe_float(row.get("minimum_margin_pct")),
                "target_gap_pct_points": _safe_float(row.get("target_gap_pct_points")),
                "status_key": row.get("status_key"),
                "target_status": row.get("target_status"),
                "customer_count": _clean_int(row.get("customer_count")),
                "sku_count": _clean_int(row.get("sku_count") if row.get("sku_count") is not None else sku_agg.get("sku_count")),
                "tone": leader_tone,
            }
        )
        portfolio.append(
            {
                "family": family,
                "category": category,
                "revenue": family_revenue,
                "weight": _clean_num(row.get("weight")),
                "margin_pct": margin_pct,
                "profit_per_lb": _safe_float(row.get("profit_per_lb")),
                "share_current": share_current,
                "share_delta_pp": share_delta_pp,
                "target_margin_pct": _safe_float(row.get("target_margin_pct")),
                "minimum_margin_pct": _safe_float(row.get("minimum_margin_pct")),
                "target_gap_pct_points": _safe_float(row.get("target_gap_pct_points")),
                "status_key": row.get("status_key"),
                "target_status": row.get("target_status"),
                "sku_count": _clean_int(row.get("sku_count") if row.get("sku_count") is not None else sku_agg.get("sku_count")),
                "customer_count": _clean_int(row.get("customer_count")),
                "order_count": _clean_int(row.get("order_count")),
                "below_target_skus": int(sku_agg.get("below_target_count") or 0),
                "missing_cost_skus": int(sku_agg.get("missing_cost_count") or 0),
                "pricing_candidate_skus": int(sku_agg.get("pricing_candidate_count") or 0),
                "promote_candidate_skus": int(sku_agg.get("promote_count") or 0),
                "core_revenue_share": core_share,
                "long_tail_share": long_tail_share,
                "top_sku": sku_agg.get("top_sku"),
                "signal": (
                    "Margin recovery"
                    if _row_below_target(row)
                    else "Concentration risk"
                    if share_current is not None and share_current >= 35.0
                    else "Growth pocket"
                    if share_delta_pp is not None and share_delta_pp >= 1.0
                    else "Stable"
                ),
            }
        )

    pricing_opportunities: List[Dict[str, Any]] = []
    for family, agg in sku_rollup.items():
        base = family_rollup.get(family) or {"family": family, "category": family}
        pricing_candidate_count = int(agg.get("pricing_candidate_count") or 0)
        below_target_count = int(agg.get("below_target_count") or 0)
        if not pricing_candidate_count and not below_target_count:
            continue
        avg_uplift_pct = (
            (float(agg.get("uplift_revenue") or 0.0) / float(agg.get("uplift_weight") or 0.0))
            if float(agg.get("uplift_weight") or 0.0) > 0
            else None
        )
        revenue_at_risk = _clean_num(agg.get("revenue"))
        pricing_opportunities.append(
            {
                "family": family,
                "category": _protein_category_name(base, family),
                "sku_count": pricing_candidate_count or below_target_count,
                "below_target_skus": below_target_count,
                "revenue_at_risk": revenue_at_risk,
                "avg_uplift_pct": avg_uplift_pct,
                "top_sku": agg.get("top_sku"),
                "top_sku_revenue": _clean_num(agg.get("top_sku_revenue")),
                "signal": "Recover family margin" if below_target_count else "Review price ladder",
            }
        )
    pricing_opportunities.sort(
        key=lambda row: (_clean_num(row.get("revenue_at_risk")), _clean_int(row.get("sku_count"))),
        reverse=True,
    )

    execution_watch: List[Dict[str, Any]] = []
    for family, agg in execution_rollup.items():
        total_actions = int(agg.get("pricing_fixes") or 0) + int(agg.get("cost_gaps") or 0) + int(agg.get("promote_candidates") or 0)
        if total_actions <= 0:
            continue
        execution_watch.append(
            {
                "family": family,
                "pricing_fixes": int(agg.get("pricing_fixes") or 0),
                "cost_gaps": int(agg.get("cost_gaps") or 0),
                "promote_candidates": int(agg.get("promote_candidates") or 0),
                "total_actions": total_actions,
                "revenue": _clean_num(agg.get("revenue")),
                "top_item": agg.get("top_item"),
            }
        )
    execution_watch.sort(
        key=lambda row: (_clean_int(row.get("total_actions")), _clean_num(row.get("revenue"))),
        reverse=True,
    )

    below_min_revenue = sum(_clean_num(row.get("revenue")) for row in mix_rows if _row_below_minimum(row))
    below_target_revenue = sum(_clean_num(row.get("revenue")) for row in mix_rows if _row_below_target(row))
    total_revenue = sum(_clean_num(row.get("revenue")) for row in mix_rows)
    summary["target_margin_range"] = _margin_band_range_label(
        [_safe_float(row.get("target_margin_pct")) for row in mix_rows]
    )
    summary["minimum_margin_range"] = _margin_band_range_label(
        [_safe_float(row.get("minimum_margin_pct")) for row in mix_rows]
    )
    summary["below_min_revenue"] = below_min_revenue
    summary["below_target_revenue"] = below_target_revenue
    summary["below_min_revenue_share"] = (below_min_revenue / total_revenue * 100.0) if total_revenue else None
    summary["below_target_revenue_share"] = (below_target_revenue / total_revenue * 100.0) if total_revenue else None

    gainers = [row for row in growth_rows if (_safe_float(row.get("share_delta_pp")) or 0.0) > 0]
    top_gainer = gainers[0] if gainers else (mix_shift_rows[0] if mix_shift_rows else None)
    top_watch = margin_watch_rows[0] if margin_watch_rows else None
    top_pricing = pricing_opportunities[0] if pricing_opportunities else None
    lead_family = summary.get("top_family") or (leaders[0].get("family") if leaders else None)
    lead_share = _safe_float(summary.get("top_family_share"))
    headline_parts: List[str] = []
    if lead_family and lead_share is not None:
        headline_parts.append(f"{lead_family} leads the mix at {lead_share:.1f}% of revenue.")
    elif lead_family:
        headline_parts.append(f"{lead_family} leads the visible product mix.")
    if top_gainer:
        gainer_delta = _safe_float(top_gainer.get("share_delta_pp"))
        family = _protein_family_name(top_gainer)
        if gainer_delta is not None and gainer_delta > 0:
            headline_parts.append(f"{family} is gaining share (+{gainer_delta:.1f} pp).")
    detail_parts: List[str] = []
    if top_watch:
        margin_pct = _safe_float(top_watch.get("margin_pct"))
        if margin_pct is not None:
            detail_parts.append(f"Margin pressure is concentrated in { _protein_family_name(top_watch) } at {margin_pct:.1f}% margin.")
    if top_pricing:
        detail_parts.append(
            f"{top_pricing.get('family')} has {int(top_pricing.get('sku_count') or 0)} SKUs in the pricing queue."
        )
    if execution_watch:
        detail_parts.append(
            f"{execution_watch[0].get('family')} carries the largest combined execution queue."
        )

    return {
        "summary": summary,
        "mix": mix_rows,
        "mix_shift": mix_shift_rows,
        "margin_watch": margin_watch_rows,
        "growth_pockets": growth_rows,
        "portfolio": portfolio[:8],
        "pricing_opportunities": pricing_opportunities[:6],
        "execution_watch": execution_watch[:6],
        "leaders": leaders[:6],
        "narrative": {
            "headline": " ".join(headline_parts).strip(),
            "detail": " ".join(detail_parts).strip(),
        },
    }


def _build_ai_signals(
    sku_rows: List[Dict[str, Any]],
    trajectory: Dict[str, Any],
    *,
    summary: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    total = len(sku_rows)
    if sku_rows:
        low_margin = [r for r in sku_rows if isinstance(r, dict) and _row_below_target(r)]
        missing_cost = sum(1 for r in sku_rows if isinstance(r, dict) and _safe_float(r.get("margin_pct")) is None)
        low_share = (len(low_margin) / total) if total else 0
        missing_share = (missing_cost / total) if total else 0
    else:
        summary = summary or {}
        total = max(0, int(summary.get("products") or 0))
        low_margin_count = max(0, int(summary.get("below_target_count") or 0))
        missing_cost = max(0, int(summary.get("missing_cost_sku_count") or 0))
        low_share = (low_margin_count / total) if total else 0
        missing_share = (missing_cost / total) if total else 0
        if total <= 0:
            return {
                "margin_risk": "Insufficient data",
                "pricing_action": "Insufficient data",
                "confidence": "low",
                "notes": "No SKU data available",
            }

    margin_risk = "Low"
    if low_share >= 0.3:
        margin_risk = "High"
    elif low_share >= 0.15:
        margin_risk = "Medium"
    if missing_share >= 0.5:
        margin_risk = "Unknown"

    labels = trajectory.get("labels") or []
    rev = trajectory.get("revenue") or []
    momentum = None
    if len(rev) >= 2 and rev[-2]:
        try:
            momentum = (float(rev[-1]) - float(rev[-2])) / float(rev[-2])
        except Exception:
            momentum = None

    pricing_action = "Hold prices"
    if margin_risk in {"High", "Unknown"}:
        pricing_action = "Protect margin"
    if momentum is not None and momentum <= -0.1:
        pricing_action = "Boost demand"
    if momentum is not None and momentum >= 0.15:
        pricing_action = "Protect stock"

    confidence = "low"
    usable = max(0, total - missing_cost)
    if total >= 50 and usable >= 10:
        confidence = "high"
    elif total >= 10:
        confidence = "medium"

    notes = []
    if missing_cost:
        notes.append(f"Cost missing for {int(round(missing_share * 100))}% of SKUs")
    if momentum is not None:
        notes.append(f"Recent momentum {momentum * 100:.1f}%")
    return {
        "margin_risk": margin_risk,
        "pricing_action": pricing_action,
        "confidence": confidence,
        "notes": "; ".join(notes) if notes else "Signals computed from current filters",
    }


def _dominant_quadrant(quadrants: Sequence[Dict[str, Any]] | None) -> Dict[str, Any]:
    items = [q for q in (quadrants or []) if isinstance(q, dict)]
    if not items:
        return {}
    return max(items, key=lambda q: (_safe_float(q.get("revenue_share")) or 0.0, _clean_num(q.get("revenue"))))


def _recent_revenue_delta_pct(trajectory: Dict[str, Any]) -> float | None:
    labels = trajectory.get("labels") or []
    revenue = trajectory.get("revenue") or []
    if len(labels) < 2 or len(revenue) < 2:
        return None
    try:
        prior = float(revenue[-2])
        current = float(revenue[-1])
        if not prior:
            return None
        return ((current - prior) / prior) * 100.0
    except Exception:
        return None


def _portfolio_posture_from_health(
    health_matrix: Dict[str, Any],
    concentration: Dict[str, Any],
    risk_opportunity: Dict[str, Any],
) -> Dict[str, Any]:
    dominant = _dominant_quadrant((health_matrix or {}).get("quadrants"))
    key = str(dominant.get("key") or "").strip().lower()
    share = _safe_float(dominant.get("revenue_share")) or 0.0
    top10 = _safe_float((concentration or {}).get("top10_share"))
    below_target_count = int((risk_opportunity or {}).get("below_target_count") or 0)
    high_velocity_low_margin = _risk_bucket_count(risk_opportunity, "high_velocity_low_margin")

    posture_map = {
        "protect": ("Protect core winners", "Keep high-velocity, profitable SKUs in stock and avoid unnecessary discounting."),
        "fix_margin": ("Recover margin on core movers", "Fast-moving volume is concentrated in low-margin SKUs that need pricing or cost correction."),
        "grow": ("Promote profitable laggards", "Margin-rich SKUs need distribution, cross-sell, or sales attention to unlock demand."),
        "rationalize": ("Rationalize the tail", "Low-velocity, weak-margin SKUs should be reviewed for assortment cleanup or pack changes."),
    }
    headline, detail = posture_map.get(key, ("Review portfolio posture", "Portfolio mix is balanced enough that no single posture dominates."))

    if key == "protect" and top10 is not None and top10 >= 55:
        detail = "Core winners are concentrated in a tight group of SKUs. Protect supply, pricing discipline, and service levels."
    elif key == "fix_margin" and high_velocity_low_margin:
        detail = f"{high_velocity_low_margin} high-velocity SKUs are below target gross margin and should anchor pricing review."
    elif key == "rationalize" and below_target_count:
        detail = f"{below_target_count} SKUs are below target gross margin inside the long tail. Review assortment and minimum-viable coverage."

    return {
        "headline": headline,
        "detail": detail,
        "tone": dominant.get("tone") or "neutral",
        "quadrant": dominant.get("label") or "Mixed",
        "revenue_share": round(share, 1),
    }


def _build_decision_signals(
    *,
    kpis: Dict[str, Any],
    trajectory: Dict[str, Any],
    health_matrix: Dict[str, Any],
    pricing_guardrails: Dict[str, Any],
    risk_opportunity: Dict[str, Any],
    concentration: Dict[str, Any],
    execution_lists: Dict[str, Any],
    ai_signals: Dict[str, Any],
    comparison_summary: Dict[str, Any],
    comparison: Dict[str, Any],
) -> List[Dict[str, Any]]:
    total_revenue = _clean_num(kpis.get("revenue"))
    below_target_revenue = _clean_num((risk_opportunity or {}).get("below_target_revenue"))
    below_target_share = (below_target_revenue / total_revenue * 100.0) if total_revenue else 0.0
    negative_margin_count = int((risk_opportunity or {}).get("negative_margin_count") or 0)

    if negative_margin_count or below_target_share >= 35:
        margin_value, margin_tone = "High pressure", "danger"
    elif below_target_share >= 15:
        margin_value, margin_tone = "Watch closely", "warning"
    else:
        margin_value, margin_tone = "Contained", "success"

    outside_count = int((pricing_guardrails or {}).get("outside_count") or 0)
    high_velocity_low_margin = _risk_bucket_count(risk_opportunity, "high_velocity_low_margin")
    if high_velocity_low_margin >= 5 or outside_count >= 10:
        pricing_value, pricing_tone = "Recover margin", "warning"
    elif outside_count > 0:
        pricing_value, pricing_tone = "Review outliers", "info"
    else:
        pricing_value, pricing_tone = "Guardrails stable", "success"

    revenue_delta_pct = _safe_float((comparison_summary or {}).get("revenue_delta_pct"))
    compare_label = str((comparison or {}).get("comparison_label") or "prior comparable window")
    demand_note = (
        revenue_delta_pct is None
        and "Not enough comparable data in the current filtered window."
        or f"{compare_label}: {revenue_delta_pct:+.1f}% revenue."
    )
    if revenue_delta_pct is None:
        demand_value, demand_tone = "Insufficient trend", "neutral"
    elif revenue_delta_pct <= -8:
        demand_value, demand_tone = "Demand softening", "warning"
    elif revenue_delta_pct >= 8:
        demand_value, demand_tone = "Demand accelerating", "success"
    else:
        demand_value, demand_tone = "Demand stable", "info"

    posture = _portfolio_posture_from_health(health_matrix, concentration, risk_opportunity)
    top10_share = _safe_float((concentration or {}).get("top10_share"))
    pricing_rows = len((execution_lists or {}).get("pricing_fixes") or [])
    cost_rows = len((execution_lists or {}).get("cost_fixes") or [])
    promote_rows = len((execution_lists or {}).get("promote_candidates") or [])
    dominant_key = str(_dominant_quadrant((health_matrix or {}).get("quadrants")).get("key") or "").strip().lower()
    posture_filters = {
        "protect": ["protect_core"],
        "fix_margin": ["recover_margin"],
        "grow": ["promote_candidate"],
        "rationalize": ["rationalize_candidate"],
    }

    return [
        {
            "key": "margin_pressure",
            "label": "Margin pressure",
            "value": margin_value,
            "tone": margin_tone,
            "note": f"{int((risk_opportunity or {}).get('below_target_count') or 0)} SKUs below target across {below_target_share:.1f}% of filtered revenue.",
            "action": {"section": "pricing", "quick_filters": ["recover_margin"], "mode": "analyst"},
        },
        {
            "key": "pricing_action",
            "label": "Pricing action",
            "value": pricing_value,
            "tone": pricing_tone,
            "note": f"{outside_count} guardrail exceptions; {high_velocity_low_margin} fast movers below target gross margin.",
            "action": {
                "section": "pricing",
                "quick_filters": ["outside_guardrail"] if outside_count else ["recover_margin"],
                "mode": "analyst",
            },
        },
        {
            "key": "demand_trend",
            "label": "Demand trend",
            "value": demand_value,
            "tone": demand_tone,
            "note": demand_note,
            "action": {
                "section": "demand",
                "quick_filters": ["promote_candidate"] if revenue_delta_pct is not None and revenue_delta_pct < 0 else ["protect_core"],
                "mode": "analyst",
            },
        },
        {
            "key": "portfolio_posture",
            "label": "Portfolio posture",
            "value": posture.get("headline") or (ai_signals.get("pricing_action") or "Review posture"),
            "tone": posture.get("tone") or "neutral",
            "note": posture.get("detail")
            or (
                top10_share is not None
                and f"Top 10 SKUs represent {top10_share:.1f}% of revenue."
                or "Use the health map and concentration view to set protect / grow / rationalize posture."
            ),
            "action": {
                "section": "strategy",
                "quick_filters": posture_filters.get(dominant_key) or [],
                "mode": "analyst",
            },
        },
        {
            "key": "execution_focus",
            "label": "Execution focus",
            "value": "Pricing first" if pricing_rows >= max(cost_rows, promote_rows) and pricing_rows else ("Commercial push" if promote_rows else "Cost cleanup"),
            "tone": "info" if (pricing_rows or cost_rows or promote_rows) else "neutral",
            "note": f"{pricing_rows} pricing fixes, {cost_rows} cost gaps, {promote_rows} promote candidates are already queued.",
            "action": {
                "section": "execution",
                "quick_filters": ["recover_margin"] if pricing_rows >= max(cost_rows, promote_rows) and pricing_rows else (["promote_candidate"] if promote_rows else ["missing_cost"]),
                "mode": "analyst",
            },
        },
    ]


def _build_focus_actions(
    *,
    kpis: Dict[str, Any],
    health_matrix: Dict[str, Any],
    risk_opportunity: Dict[str, Any],
    concentration: Dict[str, Any],
    execution_lists: Dict[str, Any],
) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    high_velocity_low_margin = list((risk_opportunity or {}).get("high_velocity_low_margin") or [])
    high_margin_low_velocity = list((risk_opportunity or {}).get("high_margin_low_velocity") or [])
    high_velocity_low_margin_count = len(high_velocity_low_margin) or _risk_bucket_count(risk_opportunity, "high_velocity_low_margin")
    high_margin_low_velocity_count = len(high_margin_low_velocity) or _risk_bucket_count(risk_opportunity, "high_margin_low_velocity")
    below_minimum_count = int((risk_opportunity or {}).get("below_minimum_count") or 0)
    below_minimum_revenue = _clean_num((risk_opportunity or {}).get("below_minimum_revenue"))
    pricing_queue = list((execution_lists or {}).get("pricing_fixes") or [])
    cost_queue = list((execution_lists or {}).get("cost_fixes") or [])
    promote_queue = list((execution_lists or {}).get("promote_candidates") or [])
    rationalize_quad = next(
        (q for q in ((health_matrix or {}).get("quadrants") or []) if isinstance(q, dict) and str(q.get("key") or "") == "rationalize"),
        {},
    )
    protect_quad = next(
        (q for q in ((health_matrix or {}).get("quadrants") or []) if isinstance(q, dict) and str(q.get("key") or "") == "protect"),
        {},
    )
    missing_cost_skus = int((kpis or {}).get("missing_cost_sku_count") or 0)
    top10_share = _safe_float((concentration or {}).get("top10_share")) or 0.0

    if below_minimum_count:
        lead = pricing_queue[0] if pricing_queue else {}
        lead_name = _row_display_name(lead) if isinstance(lead, dict) else ""
        actions.append(
            {
                "owner": "Pricing",
                "title": "Recover below-minimum exposure first",
                "tone": "warning",
                "detail": (
                    f"{below_minimum_count} SKUs are below minimum across ${below_minimum_revenue:,.0f} revenue."
                    f"{f' Start with {lead_name}.' if lead_name else ''}"
                ),
                "quick_filters": ["below_minimum_margin"],
                "section": "pricing",
                "confidence": "high",
                "upside": _clean_num((kpis or {}).get("risk_profit_uplift_target")),
            }
        )
    if high_velocity_low_margin_count:
        actions.append(
            {
                "owner": "Pricing",
                "title": "Recover margin on fast movers",
                "tone": "warning",
                "detail": f"{high_velocity_low_margin_count} high-velocity SKUs are below target gross margin and should anchor price review.",
                "quick_filters": ["recover_margin"],
                "section": "pricing",
                "confidence": "high" if high_velocity_low_margin_count >= 5 else "medium",
                "upside": _clean_num((kpis or {}).get("risk_profit_uplift_target")),
            }
        )
    if high_margin_low_velocity_count:
        actions.append(
            {
                "owner": "Commercial",
                "title": "Promote high-margin laggards",
                "tone": "info",
                "detail": f"{high_margin_low_velocity_count} profitable but under-rotating SKUs are candidates for upsell or feature support.",
                "quick_filters": ["promote_candidate"],
                "section": "execution",
                "confidence": "medium",
            }
        )
    if pricing_queue and not high_velocity_low_margin_count:
        lead = pricing_queue[0]
        lead_name = _row_display_name(lead) if isinstance(lead, dict) else "top pricing candidate"
        lead_gap = _safe_float((lead or {}).get("gap_to_target"))
        gap_note = f" Gap to target is ${abs(lead_gap):,.2f}." if lead_gap is not None and lead_gap < 0 else ""
        actions.append(
            {
                "owner": "Pricing",
                "title": "Close the biggest target gap",
                "tone": "info",
                "detail": f"{lead_name} leads the pricing queue under the new cost-plus target logic.{gap_note}",
                "quick_filters": list((lead or {}).get("quick_filters") or ["recover_margin"]),
                "section": "pricing",
                "confidence": "medium",
                "upside": _clean_num((lead or {}).get("profit_uplift_target")),
            }
        )
    if int(rationalize_quad.get("sku_count") or 0) >= 3:
        actions.append(
            {
                "owner": "Category",
                "title": "Review long-tail rationalization",
                "tone": "neutral",
                "detail": f"{int(rationalize_quad.get('sku_count') or 0)} SKUs sit in the rationalize quadrant and need assortment review.",
                "quick_filters": ["rationalize_candidate"],
                "section": "assortment",
                "confidence": "medium",
            }
        )
    if top10_share >= 55 or int(protect_quad.get("sku_count") or 0) >= 5:
        actions.append(
            {
                "owner": "Planning",
                "title": "Protect core supply",
                "tone": "success",
                "detail": f"Top 10 SKUs contribute {top10_share:.1f}% of revenue. Keep stock, service, and cost coverage tight on core meat lines.",
                "quick_filters": ["protect_core"],
                "section": "strategy",
                "confidence": "high",
            }
        )
    if missing_cost_skus:
        actions.append(
            {
                "owner": "Costing",
                "title": "Close cost coverage gaps",
                "tone": "warning",
                "detail": f"{missing_cost_skus} SKUs are missing cost coverage, which weakens minimum/target price guidance and execution ranking.",
                "quick_filters": ["missing_cost"],
                "section": "execution",
                "confidence": "high",
            }
        )

    fallback_counts = execution_lists or {}
    if not actions:
        actions.append(
            {
                "owner": "Portfolio",
                "title": "Keep monitoring execution queues",
                "tone": "info",
                "detail": f"{len(pricing_queue)} pricing fixes, {len(cost_queue)} cost issues, and {len(promote_queue)} promote candidates are available.",
                "quick_filters": [],
                "section": "execution",
                "confidence": "low",
            }
        )
    return actions[:4]


def _build_story_summary(
    *,
    comparison: Dict[str, Any],
    comparison_summary: Dict[str, Any],
    concentration: Dict[str, Any],
    risk_opportunity: Dict[str, Any],
) -> Dict[str, str]:
    demand_delta = _safe_float((comparison_summary or {}).get("revenue_delta_pct"))
    below_target_count = int((risk_opportunity or {}).get("below_target_count") or 0)
    top10_share = _safe_float((concentration or {}).get("top10_share")) or 0.0
    high_velocity_low_margin = _risk_bucket_count(risk_opportunity, "high_velocity_low_margin")

    if demand_delta is None:
        demand_text = "Demand direction is not yet comparable under the current filtered window."
    elif demand_delta <= -8:
        demand_text = f"Demand softened {demand_delta:.1f}% under the selected comparison window."
    elif demand_delta >= 8:
        demand_text = f"Demand improved {demand_delta:.1f}% under the selected comparison window."
    else:
        demand_text = f"Demand stayed broadly stable at {demand_delta:+.1f}% versus the prior comparable window."

    pricing_text = (
        f"Margin pressure is concentrated in {below_target_count} SKUs, including {high_velocity_low_margin} fast movers."
        if below_target_count
        else "Margin pressure is contained in the visible scope."
    )
    assortment_text = (
        f"Top 10 SKUs contribute {top10_share:.1f}% of filtered revenue."
        if top10_share
        else "Revenue concentration is limited in the current visible scope."
    )

    headline = f"{demand_text} {pricing_text} {assortment_text}"
    return {
        "headline": headline.strip(),
        "comparison_note": str((comparison or {}).get("note") or ""),
    }


def _default_quick_rec(row: Dict[str, Any]) -> str:
    uplift = _safe_float(row.get("uplift_pct"))
    margin = _safe_float(row.get("margin_pct"))
    if uplift is not None and uplift >= 10:
        return "Raise"
    if uplift is not None and uplift <= -10:
        return "Promo"
    if margin is not None and margin < 5:
        return "Review"
    return "Hold"


def _metrics_and_charts(
    comparison_where_sql: str,
    comparison_params: List[Any],
    cols: set[str],
    *,
    current_start: str,
    current_end: str,
    recent_start: str,
    recent_end: str,
    prior_start: str,
    prior_end: str,
    price_velocity_limit: int,
    movers_limit: int,
) -> Dict[str, Any]:
    """
    Single DuckDB query to compute KPIs + charts + price/velocity points + segment summaries.
    """
    date_col = _safe_col(cols, fs.CANON.date, "Date")
    revenue_col = _safe_col(cols, fs.CANON.revenue, "Revenue")
    cost_expr = _coalesce_expr(cols, (fs.CANON.cost, "Cost", "CostPrice"), "NULL")
    qty_expr = _coalesce_expr(cols, (fs.CANON.qty_units, "ShippedItems", "QuantityOrdered", "Qty", "Quantity", "Units", "ItemCount"), "0")
    weight_expr = _coalesce_expr(cols, (fs.CANON.weight_lb, "Weight", "WeightLb", "ShippedLb", "pack_weight_lb_sum"), "0")
    weight_col = _safe_col(cols, fs.CANON.weight_lb, "Weight", "WeightLb", "ShippedLb", "pack_weight_lb_sum")
    sku_col, prod_id_col, prod_name = _resolve_product_columns(cols)
    cust_id = _safe_col(cols, fs.CANON.customer_id, "CustomerID")
    cust_name = _safe_col(cols, fs.CANON.customer_name, "CustomerName", "Name")
    region_col = _safe_col(cols, fs.CANON.region, "Region", "RegionName")
    order_id = _safe_col(cols, fs.CANON.order_id, "OrderID")

    if not all([date_col, revenue_col, sku_col, prod_name, cust_id, order_id]):
        return {"error": {"message": "Required columns missing for products bundle"}, "meta": {"cached": False}}

    exprs = _product_exprs(cols)
    family_exprs = _family_exprs(cols)
    sku_expr = exprs["sku_expr"]
    prod_id_expr = exprs["prod_id_expr"]
    product_key_expr = exprs["product_key_expr"]
    product_name_expr = exprs["product_name_expr"]
    display_name_expr = exprs["display_name_expr"]
    protein_expr = family_exprs["protein_expr"]
    category_expr = family_exprs["category_expr"]
    customer_name_expr = (
        f"COALESCE({cust_name}, {cust_id})"
        if cust_name and cust_id
        else (f"{cust_name}" if cust_name else (f"{cust_id}" if cust_id else "NULL"))
    )
    region_expr = region_col or "NULL"

    try:
        velocity_limit = int(price_velocity_limit)
    except Exception:
        velocity_limit = 250
    velocity_limit = max(50, min(velocity_limit, 5000))
    try:
        movers_cap = int(movers_limit)
    except Exception:
        movers_cap = 20
    movers_cap = max(10, min(movers_cap, 500))
    product_target_margin_expr = margin_rules.sql_margin_rule_expr(
        "product_rollup.protein_family",
        "product_rollup.product_category",
        "target_gross_margin_pct",
    )
    seg_target_margin_expr = margin_rules.sql_margin_rule_expr(
        "seg_scored.protein_family",
        "seg_scored.product_category",
        "target_gross_margin_pct",
    )
    health_target_margin_expr = margin_rules.sql_margin_rule_expr(
        "health_classified.protein_family",
        "health_classified.product_category",
        "target_gross_margin_pct",
    )
    effective_cost_expr = margin_rules.sql_effective_cost_expr("base_cost", "weight", "qty", fallback="NULL")

    sql = f"""
        WITH base AS (
            SELECT
                {date_col}::DATE AS date,
                {product_key_expr} AS product_id,
                {sku_expr} AS sku,
                {product_name_expr} AS product_name,
                {display_name_expr} AS display_name,
                {protein_expr} AS protein_family,
                {category_expr} AS product_category,
                {cust_id}::VARCHAR AS customer_id,
                {customer_name_expr}::VARCHAR AS customer_name,
                {region_expr}::VARCHAR AS region,
                {order_id}::VARCHAR AS order_id,
                CAST({revenue_col} AS DOUBLE) AS revenue,
                CAST({cost_expr} AS DOUBLE) AS base_cost,
                CAST({qty_expr} AS DOUBLE) AS qty,
                CAST({weight_expr} AS DOUBLE) AS weight
            FROM fact
            WHERE {comparison_where_sql}
        ),
        enriched AS (
            SELECT
                *,
                ({effective_cost_expr}) AS cost,
                CASE
                    WHEN ({effective_cost_expr}) IS NULL THEN NULL
                    ELSE revenue - ({effective_cost_expr})
                END AS profit,
                CASE
                    WHEN weight > 0 THEN revenue / NULLIF(weight, 0)
                    WHEN qty > 0 THEN revenue / NULLIF(qty, 0)
                    ELSE NULL
                END AS unit_price,
                DATE_TRUNC('month', date)::DATE AS month_date,
                strftime('%Y-%m', date) AS month_label
            FROM base
        ),
        bounds AS (
            SELECT
                ?::DATE AS current_start,
                ?::DATE AS current_end,
                ?::DATE AS recent_start,
                ?::DATE AS recent_end,
                ?::DATE AS prior_start,
                ?::DATE AS prior_end
        ),
        current_enriched AS (
            SELECT e.*
            FROM enriched e
            CROSS JOIN bounds b
            WHERE e.date BETWEEN b.current_start AND b.current_end
        ),
        current_customer_revenue AS (
            SELECT
                product_id,
                customer_id,
                any_value(customer_name) AS customer_name,
                SUM(revenue) AS customer_revenue
            FROM current_enriched
            GROUP BY product_id, customer_id
        ),
        current_customer_totals AS (
            SELECT
                product_id,
                SUM(customer_revenue) AS total_revenue
            FROM current_customer_revenue
            GROUP BY product_id
        ),
        current_customer_ranked AS (
            SELECT
                ccr.product_id,
                COALESCE(NULLIF(ccr.customer_name, ''), ccr.customer_id, 'Unknown') AS customer_name,
                ccr.customer_revenue,
                CASE
                    WHEN cct.total_revenue > 0 THEN ccr.customer_revenue / NULLIF(cct.total_revenue, 0) * 100
                    ELSE NULL
                END AS customer_revenue_share,
                CASE
                    WHEN cct.total_revenue > 0 THEN ccr.customer_revenue / NULLIF(cct.total_revenue, 0)
                    ELSE 0
                END AS customer_revenue_ratio,
                ROW_NUMBER() OVER (
                    PARTITION BY ccr.product_id
                    ORDER BY ccr.customer_revenue DESC, COALESCE(NULLIF(ccr.customer_name, ''), ccr.customer_id, 'Unknown')
                ) AS rn
            FROM current_customer_revenue ccr
            LEFT JOIN current_customer_totals cct ON cct.product_id = ccr.product_id
        ),
        current_customer_summary AS (
            SELECT
                product_id,
                MAX(CASE WHEN rn = 1 THEN customer_name END) AS top_customer_name,
                MAX(CASE WHEN rn = 1 THEN customer_revenue_share END) AS top_customer_share,
                SUM(POWER(COALESCE(customer_revenue_ratio, 0), 2)) * 10000 AS customer_hhi
            FROM current_customer_ranked
            GROUP BY product_id
        ),
        current_region_revenue AS (
            SELECT
                product_id,
                COALESCE(NULLIF(region, ''), 'Unassigned') AS region_name,
                SUM(revenue) AS region_revenue
            FROM current_enriched
            GROUP BY product_id, region_name
        ),
        current_region_totals AS (
            SELECT
                product_id,
                SUM(region_revenue) AS total_revenue
            FROM current_region_revenue
            GROUP BY product_id
        ),
        current_region_ranked AS (
            SELECT
                crr.product_id,
                crr.region_name,
                crr.region_revenue,
                CASE
                    WHEN crt.total_revenue > 0 THEN crr.region_revenue / NULLIF(crt.total_revenue, 0) * 100
                    ELSE NULL
                END AS region_revenue_share,
                ROW_NUMBER() OVER (
                    PARTITION BY crr.product_id
                    ORDER BY crr.region_revenue DESC, crr.region_name
                ) AS rn
            FROM current_region_revenue crr
            LEFT JOIN current_region_totals crt ON crt.product_id = crr.product_id
        ),
        current_region_summary AS (
            SELECT
                product_id,
                MAX(CASE WHEN rn = 1 THEN region_name END) AS top_region_name,
                MAX(CASE WHEN rn = 1 THEN region_revenue_share END) AS top_region_share
            FROM current_region_ranked
            GROUP BY product_id
        ),
        agg AS (
            SELECT
                SUM(revenue) AS revenue,
                SUM(qty) AS qty,
                SUM(weight) AS weight,
                COUNT(DISTINCT product_id) AS products,
                COUNT(DISTINCT customer_id) AS customers,
                COUNT(DISTINCT order_id) AS orders,
                CASE WHEN SUM(cost) IS NULL THEN NULL ELSE SUM(revenue) - SUM(cost) END AS profit,
                CASE WHEN SUM(revenue) > 0 AND SUM(cost) IS NOT NULL THEN (SUM(revenue) - SUM(cost)) / SUM(revenue) * 100 ELSE NULL END AS margin_pct,
                AVG(unit_price) AS avg_price,
                median(unit_price) AS median_price,
                quantile_cont(unit_price, 0.10) AS up_p10,
                quantile_cont(unit_price, 0.50) AS up_p50,
                quantile_cont(unit_price, 0.90) AS up_p90
            FROM current_enriched
        ),
        window_span AS (
            SELECT
                MIN(date) AS min_date,
                MAX(date) AS max_date,
                date_diff('day', MIN(date), MAX(date)) + 1 AS window_days
            FROM current_enriched
        ),
        trajectory AS (
            SELECT
                CASE
                    WHEN ws.window_days < 120 THEN strftime('%Y-W%W', DATE_TRUNC('week', e.date))
                    ELSE strftime('%Y-%m', DATE_TRUNC('month', e.date))
                END AS period_label,
                CASE
                    WHEN ws.window_days < 120 THEN DATE_TRUNC('week', e.date)
                    ELSE DATE_TRUNC('month', e.date)
                END AS period_start,
                SUM(e.revenue) AS revenue,
                SUM(e.qty) AS qty,
                COUNT(DISTINCT e.order_id) AS orders,
                CASE WHEN SUM(e.cost) IS NULL THEN NULL ELSE SUM(e.revenue) - SUM(e.cost) END AS profit,
                CASE WHEN SUM(e.revenue) > 0 AND SUM(e.cost) IS NOT NULL THEN (SUM(e.revenue) - SUM(e.cost)) / SUM(e.revenue) * 100 ELSE NULL END AS margin_pct
            FROM current_enriched e
            CROSS JOIN window_span ws
            GROUP BY period_label, period_start
            ORDER BY period_start
        ),
        product_rollup AS (
            SELECT
                product_id,
                any_value(product_name) AS product_name,
                any_value(display_name) AS display_name,
                any_value(sku) AS sku,
                any_value(protein_family) AS protein_family,
                any_value(product_category) AS product_category,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END) AS revenue,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN qty ELSE 0 END) AS qty,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN weight ELSE 0 END) AS weight,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) AS cost,
                CASE
                    WHEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) IS NULL THEN NULL
                    ELSE SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END) - SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END)
                END AS profit,
                COUNT(DISTINCT CASE WHEN date BETWEEN b.current_start AND b.current_end THEN order_id END) AS orders,
                COUNT(DISTINCT CASE WHEN date BETWEEN b.current_start AND b.current_end THEN customer_id END) AS customer_count,
                COUNT(DISTINCT CASE WHEN date BETWEEN b.current_start AND b.current_end THEN region END) AS region_breadth,
                COUNT(*) AS rows,
                COUNT(DISTINCT CASE WHEN date BETWEEN b.current_start AND b.current_end THEN month_date END) AS months_active,
                MIN(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN date END) AS first_sold,
                MAX(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN date END) AS last_sold,
                CASE
                    WHEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN weight ELSE 0 END) > 0
                        THEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END) / NULLIF(SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN weight ELSE 0 END), 0)
                    WHEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN qty ELSE 0 END) > 0
                        THEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END) / NULLIF(SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN qty ELSE 0 END), 0)
                    ELSE NULL
                END AS unit_price,
                CASE
                    WHEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN weight ELSE 0 END) > 0 AND SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) IS NOT NULL
                        THEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) / NULLIF(SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN weight ELSE 0 END), 0)
                    WHEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN qty ELSE 0 END) > 0 AND SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) IS NOT NULL
                        THEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) / NULLIF(SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN qty ELSE 0 END), 0)
                    ELSE NULL
                END AS unit_cost,
                CASE
                    WHEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN weight ELSE 0 END) > 0 AND SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) IS NOT NULL
                        THEN (
                            SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END)
                            - SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END)
                        ) / NULLIF(SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN weight ELSE 0 END), 0)
                    ELSE NULL
                END AS contribution_lb,
                CASE
                    WHEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END) > 0
                        AND SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) IS NOT NULL
                        THEN (
                            SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END)
                            - SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END)
                        ) / SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END) * 100
                    ELSE NULL
                END AS margin_pct,
                quantile_cont(unit_price, 0.10) AS up_p10,
                quantile_cont(unit_price, 0.50) AS up_p50,
                quantile_cont(unit_price, 0.90) AS up_p90,
                CASE
                    WHEN COUNT(unit_price) >= 2 AND AVG(unit_price) > 0 THEN STDDEV_SAMP(unit_price) / NULLIF(AVG(unit_price), 0) * 100
                    ELSE NULL
                END AS price_cv_pct,
                SUM(CASE WHEN date BETWEEN b.recent_start AND b.recent_end THEN revenue ELSE 0 END) AS rev_recent,
                SUM(CASE WHEN date BETWEEN b.prior_start AND b.prior_end THEN revenue ELSE 0 END) AS rev_prior,
                SUM(CASE WHEN date BETWEEN b.recent_start AND b.recent_end AND cost IS NOT NULL THEN revenue - cost ELSE NULL END) AS profit_recent,
                SUM(CASE WHEN date BETWEEN b.prior_start AND b.prior_end AND cost IS NOT NULL THEN revenue - cost ELSE NULL END) AS profit_prior,
                SUM(CASE WHEN date BETWEEN b.recent_start AND b.recent_end THEN qty ELSE 0 END) AS qty_recent,
                SUM(CASE WHEN date BETWEEN b.prior_start AND b.prior_end THEN qty ELSE 0 END) AS qty_prior,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end AND cost IS NOT NULL THEN revenue ELSE 0 END) AS revenue_with_cost
            FROM enriched
            CROSS JOIN bounds b
            GROUP BY product_id
        ),
        product_rollup_enriched AS (
            SELECT
                pr.*,
                ccs.top_customer_name,
                ccs.top_customer_share,
                ccs.customer_hhi,
                crs.top_region_name,
                crs.top_region_share
            FROM product_rollup pr
            LEFT JOIN current_customer_summary ccs ON ccs.product_id = pr.product_id
            LEFT JOIN current_region_summary crs ON crs.product_id = pr.product_id
        ),
        family_rollup AS (
            SELECT
                COALESCE(NULLIF(protein_family, ''), 'Unassigned') AS family,
                COALESCE(NULLIF(product_category, ''), COALESCE(NULLIF(protein_family, ''), 'Unassigned')) AS category,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END) AS revenue_current,
                SUM(CASE WHEN date BETWEEN b.prior_start AND b.prior_end THEN revenue ELSE 0 END) AS revenue_prior,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN weight ELSE 0 END) AS weight_current,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) AS cost_current,
                COUNT(DISTINCT CASE WHEN date BETWEEN b.current_start AND b.current_end THEN product_id END) AS sku_count,
                COUNT(DISTINCT CASE WHEN date BETWEEN b.current_start AND b.current_end THEN customer_id END) AS customer_count,
                COUNT(DISTINCT CASE WHEN date BETWEEN b.current_start AND b.current_end THEN order_id END) AS order_count
            FROM enriched
            CROSS JOIN bounds b
            GROUP BY 1, 2
        ),
        family_mix_enriched AS (
            SELECT
                family,
                category,
                revenue_current AS revenue,
                revenue_prior,
                weight_current AS weight,
                sku_count,
                customer_count,
                order_count,
                CASE
                    WHEN revenue_current > 0 AND cost_current IS NOT NULL
                        THEN (revenue_current - cost_current) / revenue_current * 100
                    ELSE NULL
                END AS margin_pct,
                CASE
                    WHEN weight_current > 0 AND cost_current IS NOT NULL
                        THEN (revenue_current - cost_current) / NULLIF(weight_current, 0)
                    ELSE NULL
                END AS profit_per_lb,
                CASE WHEN SUM(revenue_current) OVER () > 0 THEN revenue_current / SUM(revenue_current) OVER () * 100 ELSE NULL END AS share_current,
                CASE WHEN SUM(revenue_prior) OVER () > 0 THEN revenue_prior / SUM(revenue_prior) OVER () * 100 ELSE NULL END AS share_prior,
                CASE
                    WHEN SUM(revenue_current) OVER () > 0 AND SUM(revenue_prior) OVER () > 0
                        THEN (revenue_current / SUM(revenue_current) OVER () * 100) - (revenue_prior / SUM(revenue_prior) OVER () * 100)
                    ELSE NULL
                END AS share_delta_pp
            FROM family_rollup
            WHERE revenue_current <> 0 OR revenue_prior <> 0
        ),
        compare_summary AS (
            SELECT
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END) AS revenue_current,
                SUM(CASE WHEN date BETWEEN b.prior_start AND b.prior_end THEN revenue ELSE 0 END) AS revenue_prior,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN qty ELSE 0 END) AS qty_current,
                SUM(CASE WHEN date BETWEEN b.prior_start AND b.prior_end THEN qty ELSE 0 END) AS qty_prior,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN weight ELSE 0 END) AS weight_current,
                SUM(CASE WHEN date BETWEEN b.prior_start AND b.prior_end THEN weight ELSE 0 END) AS weight_prior,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) AS cost_current,
                SUM(CASE WHEN date BETWEEN b.prior_start AND b.prior_end THEN cost ELSE NULL END) AS cost_prior,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end AND cost IS NOT NULL THEN revenue - cost ELSE NULL END) AS profit_current,
                SUM(CASE WHEN date BETWEEN b.prior_start AND b.prior_end AND cost IS NOT NULL THEN revenue - cost ELSE NULL END) AS profit_prior,
                COUNT(DISTINCT CASE WHEN date BETWEEN b.current_start AND b.current_end THEN order_id END) AS orders_current,
                COUNT(DISTINCT CASE WHEN date BETWEEN b.prior_start AND b.prior_end THEN order_id END) AS orders_prior
            FROM enriched
            CROSS JOIN bounds b
        ),
        pareto AS (
            SELECT
                product_id,
                display_name,
                revenue,
                SUM(revenue) OVER (ORDER BY revenue DESC) AS cum_revenue,
                SUM(revenue) OVER () AS total_revenue,
                ROW_NUMBER() OVER (ORDER BY revenue DESC) AS rn
            FROM product_rollup_enriched
        ),
        pareto_limited AS (
            SELECT
                display_name,
                revenue,
                CASE WHEN total_revenue > 0 THEN cum_revenue / total_revenue * 100 ELSE NULL END AS cum_share
            FROM pareto
            WHERE rn <= 200
        ),
        top_movers AS (
            SELECT
                product_id,
                display_name,
                rev_recent,
                rev_prior,
                rev_recent - rev_prior AS delta_revenue,
                profit_recent - profit_prior AS delta_profit,
                qty_recent - qty_prior AS delta_qty
            FROM product_rollup_enriched
            ORDER BY ABS(rev_recent - rev_prior) DESC, rev_recent - rev_prior DESC
            LIMIT {movers_cap}
        ),
        velocity AS (
            SELECT
                CASE WHEN COUNT(*) > 0 THEN SUM(qty) / NULLIF(date_diff('week', MIN(date), MAX(date)) + 1, 0) ELSE 0 END AS avg_weekly_qty,
                CASE WHEN COUNT(*) > 0 THEN SUM(revenue) / NULLIF(date_diff('week', MIN(date), MAX(date)) + 1, 0) ELSE 0 END AS weekly_revenue,
                COUNT(DISTINCT product_id) AS active_skus
            FROM current_enriched
        ),
        price_dist AS (
            SELECT
                bucket,
                COUNT(*) AS count,
                MIN(unit_price) AS min_price,
                MAX(unit_price) AS max_price,
                AVG(unit_price) AS avg_price
            FROM (
                SELECT
                    CASE
                        WHEN unit_price IS NULL THEN NULL
                        WHEN max_price <= min_price THEN 1
                        ELSE LEAST(20, GREATEST(1, CAST(FLOOR((unit_price - min_price) / NULLIF(max_price - min_price, 0) * 20) + 1 AS INTEGER)))
                    END AS bucket,
                    unit_price
                FROM (
                    SELECT
                        unit_price,
                        MIN(unit_price) OVER () AS min_price,
                        MAX(unit_price) OVER () AS max_price
                    FROM current_enriched
                    WHERE unit_price IS NOT NULL
                )
            )
            WHERE bucket IS NOT NULL
            GROUP BY bucket
            ORDER BY bucket
        ),
        seg_scored AS (
            SELECT
                *,
                COALESCE(quantile_cont(revenue, 0.80) OVER (), 0) AS rev_p80,
                COALESCE(quantile_cont(orders, 0.60) OVER (), 0) AS ord_p60,
                CASE
                    WHEN revenue >= rev_p80 AND orders >= ord_p60 THEN 'Stars'
                    WHEN revenue >= rev_p80 THEN 'Cash Cows'
                    WHEN orders >= ord_p60 THEN 'Volume Drivers'
                    WHEN margin_pct IS NOT NULL AND margin_pct < 5 THEN 'Margin Risk'
                    ELSE 'Long Tail'
                END AS segment
            FROM product_rollup_enriched
        ),
        segment_summary AS (
            SELECT
                segment,
                COUNT(*) AS sku_count,
                SUM(revenue) AS revenue
            FROM seg_scored
            GROUP BY segment
        ),
        segment_movers AS (
            SELECT
                sm.segment,
                tm.product_id,
                tm.display_name,
                tm.delta_revenue AS delta,
                tm.delta_profit,
                tm.delta_qty,
                tm.rev_recent,
                tm.rev_prior
            FROM seg_scored sm
            JOIN top_movers tm ON sm.product_id = tm.product_id
            LIMIT 20
        ),
        price_velocity AS (
            SELECT
                *,
                CASE WHEN months_active > 0 THEN orders / NULLIF(months_active, 0) ELSE NULL END AS orders_per_month,
                CASE WHEN SUM(revenue) OVER () > 0 THEN revenue / SUM(revenue) OVER () * 100 ELSE NULL END AS revenue_share,
                {seg_target_margin_expr} AS target_margin_pct,
                {margin_rules.sql_price_from_cost_expr("unit_cost", seg_target_margin_expr, fallback="NULL")} AS target_price,
                {margin_rules.sql_price_uplift_pct_expr("unit_price", margin_rules.sql_price_from_cost_expr("unit_cost", seg_target_margin_expr, fallback="NULL"), fallback="NULL")} AS uplift_pct,
                ROW_NUMBER() OVER (ORDER BY revenue DESC) AS rn
            FROM seg_scored
        ),
        health_thresholds AS (
            SELECT
                quantile_cont(orders_per_month, 0.40) FILTER (WHERE orders_per_month IS NOT NULL) AS velocity_p40,
                quantile_cont(orders_per_month, 0.50) FILTER (WHERE orders_per_month IS NOT NULL) AS velocity_p50,
                quantile_cont(orders_per_month, 0.60) FILTER (WHERE orders_per_month IS NOT NULL) AS velocity_p60,
                quantile_cont(COALESCE(margin_pct, contribution_lb), 0.40) FILTER (WHERE COALESCE(margin_pct, contribution_lb) IS NOT NULL) AS profitability_p40,
                quantile_cont(COALESCE(margin_pct, contribution_lb), 0.50) FILTER (WHERE COALESCE(margin_pct, contribution_lb) IS NOT NULL) AS profitability_p50,
                quantile_cont(COALESCE(margin_pct, contribution_lb), 0.60) FILTER (WHERE COALESCE(margin_pct, contribution_lb) IS NOT NULL) AS profitability_p60
            FROM price_velocity
        ),
        health_classified AS (
            SELECT
                pv.*,
                ht.velocity_p40,
                ht.velocity_p50,
                ht.velocity_p60,
                ht.profitability_p40,
                ht.profitability_p50,
                ht.profitability_p60,
                COALESCE(pv.margin_pct, pv.contribution_lb) AS profitability_metric_value,
                CASE
                    WHEN COALESCE(pv.orders_per_month, 0) >= COALESCE(ht.velocity_p60, COALESCE(ht.velocity_p50, 0))
                        THEN 'high'
                    WHEN COALESCE(pv.orders_per_month, 0) <= COALESCE(ht.velocity_p40, COALESCE(ht.velocity_p50, 0))
                        THEN 'low'
                    WHEN COALESCE(pv.orders_per_month, 0) >= COALESCE(ht.velocity_p50, 0)
                        THEN 'high'
                    ELSE 'low'
                END AS velocity_band,
                CASE
                    WHEN COALESCE(COALESCE(pv.margin_pct, pv.contribution_lb), -999999) >= COALESCE(ht.profitability_p60, COALESCE(ht.profitability_p50, 0))
                        THEN 'high'
                    WHEN COALESCE(COALESCE(pv.margin_pct, pv.contribution_lb), -999999) <= COALESCE(ht.profitability_p40, COALESCE(ht.profitability_p50, 0))
                        THEN 'low'
                    WHEN COALESCE(COALESCE(pv.margin_pct, pv.contribution_lb), -999999) >= COALESCE(ht.profitability_p50, 0)
                        THEN 'high'
                    ELSE 'low'
                END AS profitability_band,
                CASE
                    WHEN (
                        CASE
                            WHEN COALESCE(pv.orders_per_month, 0) >= COALESCE(ht.velocity_p60, COALESCE(ht.velocity_p50, 0)) THEN 'high'
                            WHEN COALESCE(pv.orders_per_month, 0) <= COALESCE(ht.velocity_p40, COALESCE(ht.velocity_p50, 0)) THEN 'low'
                            WHEN COALESCE(pv.orders_per_month, 0) >= COALESCE(ht.velocity_p50, 0) THEN 'high'
                            ELSE 'low'
                        END
                    ) = 'high' AND (
                        CASE
                            WHEN COALESCE(COALESCE(pv.margin_pct, pv.contribution_lb), -999999) >= COALESCE(ht.profitability_p60, COALESCE(ht.profitability_p50, 0)) THEN 'high'
                            WHEN COALESCE(COALESCE(pv.margin_pct, pv.contribution_lb), -999999) <= COALESCE(ht.profitability_p40, COALESCE(ht.profitability_p50, 0)) THEN 'low'
                            WHEN COALESCE(COALESCE(pv.margin_pct, pv.contribution_lb), -999999) >= COALESCE(ht.profitability_p50, 0) THEN 'high'
                            ELSE 'low'
                        END
                    ) = 'high' THEN 'protect'
                    WHEN (
                        CASE
                            WHEN COALESCE(pv.orders_per_month, 0) >= COALESCE(ht.velocity_p60, COALESCE(ht.velocity_p50, 0)) THEN 'high'
                            WHEN COALESCE(pv.orders_per_month, 0) <= COALESCE(ht.velocity_p40, COALESCE(ht.velocity_p50, 0)) THEN 'low'
                            WHEN COALESCE(pv.orders_per_month, 0) >= COALESCE(ht.velocity_p50, 0) THEN 'high'
                            ELSE 'low'
                        END
                    ) = 'high' THEN 'fix_margin'
                    WHEN (
                        CASE
                            WHEN COALESCE(COALESCE(pv.margin_pct, pv.contribution_lb), -999999) >= COALESCE(ht.profitability_p60, COALESCE(ht.profitability_p50, 0)) THEN 'high'
                            WHEN COALESCE(COALESCE(pv.margin_pct, pv.contribution_lb), -999999) <= COALESCE(ht.profitability_p40, COALESCE(ht.profitability_p50, 0)) THEN 'low'
                            WHEN COALESCE(COALESCE(pv.margin_pct, pv.contribution_lb), -999999) >= COALESCE(ht.profitability_p50, 0) THEN 'high'
                            ELSE 'low'
                        END
                    ) = 'high' THEN 'grow'
                    ELSE 'rationalize'
                END AS quadrant
            FROM price_velocity pv
            CROSS JOIN health_thresholds ht
        ),
        health_summary AS (
            SELECT
                quadrant,
                COUNT(*) AS sku_count,
                SUM(revenue) AS revenue,
                SUM(profit) AS profit
            FROM health_classified
            GROUP BY quadrant
        ),
        health_top AS (
            SELECT
                quadrant,
                product_id,
                display_name,
                revenue,
                profit,
                margin_pct,
                orders_per_month AS velocity_per_month,
                ROW_NUMBER() OVER (PARTITION BY quadrant ORDER BY revenue DESC) AS rn
            FROM health_classified
        ),
        segment_mix AS (
            SELECT
                segment,
                SUM(rev_recent) AS revenue_current,
                SUM(rev_prior) AS revenue_prior
            FROM seg_scored
            GROUP BY segment
        ),
        segment_mix_enriched AS (
            SELECT
                segment,
                revenue_current,
                revenue_prior,
                CASE WHEN SUM(revenue_current) OVER () > 0 THEN revenue_current / SUM(revenue_current) OVER () * 100 ELSE NULL END AS share_current,
                CASE WHEN SUM(revenue_prior) OVER () > 0 THEN revenue_prior / SUM(revenue_prior) OVER () * 100 ELSE NULL END AS share_prior,
                CASE
                    WHEN SUM(revenue_current) OVER () > 0 AND SUM(revenue_prior) OVER () > 0
                        THEN (revenue_current / SUM(revenue_current) OVER () * 100) - (revenue_prior / SUM(revenue_prior) OVER () * 100)
                    ELSE NULL
                END AS share_delta_pp
            FROM segment_mix
        ),
        execution_pricing_base AS (
            SELECT
                *,
                COALESCE(quantile_cont(revenue, 0.75) OVER (), 0) AS rev_p75
            FROM health_classified
        ),
        execution_pricing_fixes AS (
            SELECT
                product_id,
                display_name,
                revenue,
                profit,
                margin_pct,
                orders_per_month,
                'Increase / Review cost' AS action,
                'High velocity with below-target gross margin and high revenue exposure.' AS reason,
                ROW_NUMBER() OVER (ORDER BY revenue DESC) AS rn
            FROM execution_pricing_base
            WHERE velocity_band = 'high'
              AND (margin_pct IS NULL OR target_margin_pct IS NULL OR margin_pct < target_margin_pct)
              AND revenue >= rev_p75
        ),
        execution_cost_fixes AS (
            SELECT
                product_id,
                display_name,
                revenue,
                profit,
                margin_pct,
                orders_per_month,
                'Review cost data' AS action,
                'Revenue is material but cost is missing or invalid, reducing trust in margin signals.' AS reason,
                ROW_NUMBER() OVER (ORDER BY revenue DESC) AS rn
            FROM health_classified
            WHERE cost IS NULL OR cost <= 0
        ),
        execution_promote AS (
            SELECT
                product_id,
                display_name,
                revenue,
                profit,
                margin_pct,
                orders_per_month,
                'Promote / Expand distribution' AS action,
                'High profitability with low velocity indicates growth headroom.' AS reason,
                ROW_NUMBER() OVER (ORDER BY revenue DESC) AS rn
            FROM health_classified
            WHERE profitability_band = 'high'
              AND velocity_band = 'low'
        )
        SELECT
            agg.revenue,
            agg.qty,
            agg.weight,
            agg.products,
            agg.customers,
            agg.orders,
            agg.profit,
            agg.margin_pct,
            agg.avg_price,
            agg.median_price,
            agg.up_p10,
            agg.up_p50,
            agg.up_p90,
            compare_summary.revenue_current AS compare_revenue_current,
            compare_summary.revenue_prior AS compare_revenue_prior,
            compare_summary.qty_current AS compare_qty_current,
            compare_summary.qty_prior AS compare_qty_prior,
            compare_summary.weight_current AS compare_weight_current,
            compare_summary.weight_prior AS compare_weight_prior,
            compare_summary.cost_current AS compare_cost_current,
            compare_summary.cost_prior AS compare_cost_prior,
            compare_summary.profit_current AS compare_profit_current,
            compare_summary.profit_prior AS compare_profit_prior,
            compare_summary.orders_current AS compare_orders_current,
            compare_summary.orders_prior AS compare_orders_prior,
            velocity.avg_weekly_qty,
            velocity.weekly_revenue,
            velocity.active_skus,
            (SELECT CASE WHEN window_days < 120 THEN 'weekly' ELSE 'monthly' END FROM window_span) AS traj_grain,
            (SELECT list(period_label) FROM trajectory) AS traj_labels,
            (SELECT list(revenue) FROM trajectory) AS traj_revenue,
            (SELECT list(qty) FROM trajectory) AS traj_qty,
            (SELECT list(orders) FROM trajectory) AS traj_orders,
            (SELECT list(profit) FROM trajectory) AS traj_profit,
            (SELECT list(margin_pct) FROM trajectory) AS traj_margin,
            (SELECT velocity_p40 FROM health_thresholds) AS health_velocity_p40,
            (SELECT velocity_p60 FROM health_thresholds) AS health_velocity_p60,
            (SELECT profitability_p40 FROM health_thresholds) AS health_profitability_p40,
            (SELECT profitability_p60 FROM health_thresholds) AS health_profitability_p60,
            (
                SELECT MAX(rev_share)
                FROM (
                    SELECT
                        ROW_NUMBER() OVER (ORDER BY revenue DESC) AS rn,
                        CASE WHEN SUM(revenue) OVER () > 0 THEN revenue / SUM(revenue) OVER () * 100 ELSE 0 END AS rev_share
                    FROM product_rollup
                ) ranked
                WHERE rn = 1
            ) AS concentration_top1_share,
            (
                SELECT SUM(rev_share)
                FROM (
                    SELECT
                        ROW_NUMBER() OVER (ORDER BY revenue DESC) AS rn,
                        CASE WHEN SUM(revenue) OVER () > 0 THEN revenue / SUM(revenue) OVER () * 100 ELSE 0 END AS rev_share
                    FROM product_rollup
                ) ranked
                WHERE rn <= 10
            ) AS concentration_top10_share,
            (
                SELECT SUM(POWER(rev_share / 100.0, 2)) * 10000
                FROM (
                    SELECT CASE WHEN SUM(revenue) OVER () > 0 THEN revenue / SUM(revenue) OVER () * 100 ELSE 0 END AS rev_share
                    FROM product_rollup
                ) ranked
            ) AS concentration_hhi,
            (
                SELECT MIN(rn)
                FROM (
                    SELECT
                        ROW_NUMBER() OVER (ORDER BY revenue DESC) AS rn,
                        CASE
                            WHEN SUM(revenue) OVER () > 0
                                THEN SUM(revenue) OVER (ORDER BY revenue DESC) / SUM(revenue) OVER () * 100
                            ELSE NULL
                        END AS cum_share
                    FROM product_rollup
                ) ranked
                WHERE cum_share >= 80
            ) AS concentration_skus_to_80,
            (SELECT COUNT(*) FROM family_mix_enriched) AS protein_family_count,
            (SELECT family FROM family_mix_enriched ORDER BY revenue DESC NULLS LAST LIMIT 1) AS top_protein_family,
            (SELECT share_current FROM family_mix_enriched ORDER BY revenue DESC NULLS LAST LIMIT 1) AS top_protein_share,
            (
                SELECT SUM(POWER(COALESCE(share_current, 0) / 100.0, 2)) * 10000
                FROM family_mix_enriched
            ) AS protein_hhi,
            (
                SELECT COUNT(*)
                FROM product_rollup
                WHERE margin_pct IS NOT NULL
                  AND ({product_target_margin_expr}) IS NOT NULL
                  AND margin_pct < ({product_target_margin_expr})
            ) AS risk_below_target_count,
            (
                SELECT SUM(revenue)
                FROM product_rollup
                WHERE margin_pct IS NOT NULL
                  AND ({product_target_margin_expr}) IS NOT NULL
                  AND margin_pct < ({product_target_margin_expr})
            ) AS risk_below_target_revenue,
            (SELECT COUNT(*) FROM product_rollup WHERE margin_pct IS NOT NULL AND margin_pct < 0) AS risk_negative_margin_count,
            (
                SELECT SUM((revenue * (({product_target_margin_expr}) / 100.0)) - profit)
                FROM product_rollup
                WHERE margin_pct IS NOT NULL
                  AND ({product_target_margin_expr}) IS NOT NULL
                  AND margin_pct < ({product_target_margin_expr})
                  AND profit IS NOT NULL
            ) AS risk_profit_uplift_target,
            (
                SELECT CASE
                    WHEN SUM(revenue) > 0 THEN SUM(revenue_with_cost) / NULLIF(SUM(revenue), 0) * 100
                    ELSE NULL
                END
                FROM product_rollup
            ) AS cost_coverage_pct,
            (SELECT COUNT(*) FROM product_rollup WHERE cost IS NULL OR cost <= 0) AS missing_cost_sku_count,
            (SELECT quantile_cont(contribution_lb, 0.10) FROM product_rollup WHERE contribution_lb IS NOT NULL) AS contribution_lb_p10,
            (SELECT quantile_cont(contribution_lb, 0.50) FROM product_rollup WHERE contribution_lb IS NOT NULL) AS contribution_lb_p50,
            (SELECT quantile_cont(contribution_lb, 0.90) FROM product_rollup WHERE contribution_lb IS NOT NULL) AS contribution_lb_p90,
            (
                SELECT SUM(COALESCE(profit, 0))
                FROM product_rollup
                WHERE (
                    margin_pct IS NOT NULL
                    AND ({product_target_margin_expr}) IS NOT NULL
                    AND margin_pct < ({product_target_margin_expr})
                ) OR cost IS NULL
            ) AS profit_at_risk,
            (
                SELECT COUNT(*)
                FROM product_rollup
                WHERE unit_price IS NOT NULL AND up_p90 IS NOT NULL AND up_p90 > 0 AND unit_price > up_p90 * 1.15
            ) AS high_price_outlier_count,
            (
                SELECT COUNT(*)
                FROM product_rollup
                WHERE unit_price IS NOT NULL AND up_p10 IS NOT NULL AND up_p10 > 0 AND unit_price < up_p10 / 1.15
            ) AS low_price_outlier_count,
            (
                SELECT COUNT(*)
                FROM product_rollup
                WHERE unit_price IS NOT NULL AND (
                    (up_p90 IS NOT NULL AND up_p90 > 0 AND unit_price > up_p90 * 1.15)
                    OR
                    (up_p10 IS NOT NULL AND up_p10 > 0 AND unit_price < up_p10 / 1.15)
                )
            ) AS outside_guardrail_count,
            (
                SELECT CASE WHEN COUNT(*) > 0 THEN (
                    COUNT(*) FILTER (
                        WHERE unit_price IS NOT NULL AND (
                            (up_p90 IS NOT NULL AND up_p90 > 0 AND unit_price > up_p90 * 1.15)
                            OR
                            (up_p10 IS NOT NULL AND up_p10 > 0 AND unit_price < up_p10 / 1.15)
                        )
                    )::DOUBLE / COUNT(*)::DOUBLE * 100
                ) ELSE NULL END
                FROM product_rollup
            ) AS outside_guardrail_pct,
            (SELECT list(struct_pack(label:=display_name, revenue:=revenue, cumulative:=cum_share)) FROM pareto_limited) AS pareto,
            (SELECT list(struct_pack(product_id:=product_id, product_name:=display_name, display_name:=display_name, delta:=delta_revenue, delta_revenue:=delta_revenue, delta_profit:=delta_profit, delta_qty:=delta_qty, recent:=rev_recent, prior:=rev_prior)) FROM top_movers) AS movers,
            (SELECT list(struct_pack(bucket:=bucket, count:=count, min_price:=min_price, max_price:=max_price, avg_price:=avg_price)) FROM price_dist) AS price_dist,
            (SELECT list(struct_pack(family:=family, category:=category, revenue:=revenue, revenue_prior:=revenue_prior, weight:=weight, margin_pct:=margin_pct, profit_per_lb:=profit_per_lb, share_current:=share_current, share_prior:=share_prior, share_delta_pp:=share_delta_pp, sku_count:=sku_count, customer_count:=customer_count, order_count:=order_count)) FROM (SELECT * FROM family_mix_enriched ORDER BY revenue DESC NULLS LAST LIMIT 8)) AS protein_mix,
            (SELECT list(struct_pack(family:=family, category:=category, revenue:=revenue, share_current:=share_current, share_prior:=share_prior, share_delta_pp:=share_delta_pp, customer_count:=customer_count, sku_count:=sku_count)) FROM (SELECT * FROM family_mix_enriched ORDER BY ABS(share_delta_pp) DESC NULLS LAST LIMIT 8)) AS protein_mix_shift,
            (SELECT list(struct_pack(family:=family, category:=category, revenue:=revenue, margin_pct:=margin_pct, profit_per_lb:=profit_per_lb, share_current:=share_current, customer_count:=customer_count, sku_count:=sku_count)) FROM (SELECT * FROM family_mix_enriched ORDER BY COALESCE(margin_pct, 999999) ASC, revenue DESC NULLS LAST LIMIT 8)) AS protein_margin_watch,
            (SELECT list(struct_pack(family:=family, category:=category, revenue:=revenue, share_delta_pp:=share_delta_pp, margin_pct:=margin_pct, customer_count:=customer_count, sku_count:=sku_count)) FROM (SELECT * FROM family_mix_enriched ORDER BY share_delta_pp DESC NULLS LAST, revenue DESC NULLS LAST LIMIT 8)) AS protein_growth_pockets,
            (SELECT list(struct_pack(segment:=segment, sku_count:=sku_count, revenue:=revenue)) FROM segment_summary) AS segment_summary,
            (SELECT list(struct_pack(segment:=segment, revenue_current:=revenue_current, revenue_prior:=revenue_prior, share_current:=share_current, share_prior:=share_prior, share_delta_pp:=share_delta_pp)) FROM (SELECT * FROM segment_mix_enriched ORDER BY ABS(share_delta_pp) DESC)) AS segment_mix_shift,
            (SELECT list(struct_pack(segment:=segment, product_id:=product_id, product_name:=display_name, display_name:=display_name, delta:=delta, recent:=rev_recent, prior:=rev_prior)) FROM segment_movers) AS segment_movers,
            (SELECT list(struct_pack(quadrant:=quadrant, sku_count:=sku_count, revenue:=revenue, profit:=profit)) FROM health_summary) AS health_summary,
            (SELECT list(struct_pack(quadrant:=quadrant, product_id:=product_id, product_name:=display_name, display_name:=display_name, revenue:=revenue, profit:=profit, margin_pct:=margin_pct, velocity_per_month:=velocity_per_month)) FROM health_top WHERE rn <= 10) AS health_top,
            (SELECT list(struct_pack(product_id:=product_id, product_name:=display_name, display_name:=display_name, revenue:=revenue, profit:=profit, margin_pct:=margin_pct, orders_per_month:=orders_per_month, action:=action, reason:=reason)) FROM execution_pricing_fixes WHERE rn <= 10) AS execution_pricing_fixes,
            (SELECT list(struct_pack(product_id:=product_id, product_name:=display_name, display_name:=display_name, revenue:=revenue, profit:=profit, margin_pct:=margin_pct, orders_per_month:=orders_per_month, action:=action, reason:=reason)) FROM execution_cost_fixes WHERE rn <= 10) AS execution_cost_fixes,
            (SELECT list(struct_pack(product_id:=product_id, product_name:=display_name, display_name:=display_name, revenue:=revenue, profit:=profit, margin_pct:=margin_pct, orders_per_month:=orders_per_month, action:=action, reason:=reason)) FROM execution_promote WHERE rn <= 10) AS execution_promote,
            (SELECT list(struct_pack(
                product_id:=product_id,
                product_name:=product_name,
                display_name:=display_name,
                revenue:=revenue,
                qty:=qty,
                weight:=weight,
                profit:=profit,
                unit_price:=unit_price,
                current_unit_price:=unit_price,
                unit_cost:=unit_cost,
                asp_lb:=unit_price,
                cost_lb:=unit_cost,
                target_price:=target_price,
                uplift_pct:=uplift_pct,
                margin_pct:=margin_pct,
                revenue_share:=revenue_share,
                orders:=orders,
                customer_count:=customer_count,
                region_breadth:=region_breadth,
                months_active:=months_active,
                orders_per_month:=orders_per_month,
                segment:=segment,
                protein_family:=protein_family,
                product_category:=product_category,
                top_customer_name:=top_customer_name,
                top_customer_share:=top_customer_share,
                customer_hhi:=customer_hhi,
                top_region_name:=top_region_name,
                top_region_share:=top_region_share,
                rev_recent:=rev_recent,
                rev_prior:=rev_prior,
                up_p10:=up_p10,
                up_p50:=up_p50,
                up_p90:=up_p90,
                price_cv_pct:=price_cv_pct,
                contribution_lb:=contribution_lb,
                first_sold:=first_sold,
                last_sold:=last_sold
            )) FROM price_velocity WHERE rn <= {velocity_limit} AND unit_price IS NOT NULL AND orders_per_month IS NOT NULL) AS price_velocity,
            (SELECT list(struct_pack(product_id:=product_id, product_name:=product_name, display_name:=display_name, revenue:=revenue, qty:=qty, weight:=weight, profit:=profit, unit_price:=unit_price, current_unit_price:=unit_price, asp_lb:=unit_price, cost_lb:=unit_cost, margin_pct:=margin_pct, target_price:=target_price, uplift_pct:=uplift_pct, protein_family:=protein_family, product_category:=product_category, customer_count:=customer_count, region_breadth:=region_breadth, top_customer_name:=top_customer_name, top_customer_share:=top_customer_share, top_region_name:=top_region_name, top_region_share:=top_region_share)) FROM (SELECT * FROM price_velocity WHERE revenue IS NOT NULL ORDER BY revenue DESC LIMIT 150)) AS top_products
        FROM agg, velocity, compare_summary
        LIMIT 1
    """

    args = list(comparison_params) + [
        current_start,
        current_end,
        recent_start,
        recent_end,
        prior_start,
        prior_end,
    ]
    df = fact_store.execute_sql_df(
        sql,
        args,
        tag="products.metrics_bundle",
        cache_key="products.metrics_bundle",
    )
    if df.empty:
        return {}
    row = df.iloc[0]

    kpis = {
        "revenue": _clean_num(row.get("revenue")),
        "qty": _clean_num(row.get("qty")),
        "weight": _clean_num(row.get("weight")),
        "products": _clean_int(row.get("products")),
        "customers": _clean_int(row.get("customers")),
        "orders": _clean_int(row.get("orders")),
        "profit": _clean_num(row.get("profit")),
        "margin_pct": None if row.get("margin_pct") is None or math.isnan(row.get("margin_pct")) else float(row.get("margin_pct")),
        "avg_price": None if row.get("avg_price") is None or math.isnan(row.get("avg_price")) else float(row.get("avg_price")),
        "median_price": None if row.get("median_price") is None or math.isnan(row.get("median_price")) else float(row.get("median_price")),
        "unit_price_p10": None if row.get("up_p10") is None or math.isnan(row.get("up_p10")) else float(row.get("up_p10")),
        "unit_price_p50": None if row.get("up_p50") is None or math.isnan(row.get("up_p50")) else float(row.get("up_p50")),
        "unit_price_p90": None if row.get("up_p90") is None or math.isnan(row.get("up_p90")) else float(row.get("up_p90")),
        "cost_coverage_pct": _safe_float(row.get("cost_coverage_pct")),
        "missing_cost_sku_count": _clean_int(row.get("missing_cost_sku_count")),
        "contribution_lb_p10": _safe_float(row.get("contribution_lb_p10")),
        "contribution_lb_p50": _safe_float(row.get("contribution_lb_p50")),
        "contribution_lb_p90": _safe_float(row.get("contribution_lb_p90")),
        "profit_at_risk": _clean_num(row.get("profit_at_risk")),
        "high_price_outlier_count": _clean_int(row.get("high_price_outlier_count")),
        "low_price_outlier_count": _clean_int(row.get("low_price_outlier_count")),
        "outside_guardrail_count": _clean_int(row.get("outside_guardrail_count")),
        "outside_guardrail_pct": _safe_float(row.get("outside_guardrail_pct")),
        "concentration_top1_share": _safe_float(row.get("concentration_top1_share")),
        "concentration_top10_share": _safe_float(row.get("concentration_top10_share")),
        "concentration_hhi": _safe_float(row.get("concentration_hhi")),
        "concentration_skus_to_80": _clean_int(row.get("concentration_skus_to_80")),
        "protein_family_count": _clean_int(row.get("protein_family_count")),
        "top_protein_family": row.get("top_protein_family"),
        "top_protein_share": _safe_float(row.get("top_protein_share")),
        "protein_hhi": _safe_float(row.get("protein_hhi")),
        "risk_below_target_count": _clean_int(row.get("risk_below_target_count")),
        "risk_below_target_revenue": _clean_num(row.get("risk_below_target_revenue")),
        "risk_negative_margin_count": _clean_int(row.get("risk_negative_margin_count")),
        "risk_profit_uplift_target": _clean_num(row.get("risk_profit_uplift_target")),
    }
    kpis["revenue_per_product"] = kpis["revenue"] / kpis["products"] if kpis["products"] else None
    kpis["revenue_per_customer"] = kpis["revenue"] / kpis["customers"] if kpis["customers"] else None

    comparison_summary = {
        "revenue_current": _clean_num(row.get("compare_revenue_current")),
        "revenue_prior": _clean_num(row.get("compare_revenue_prior")),
        "qty_current": _clean_num(row.get("compare_qty_current")),
        "qty_prior": _clean_num(row.get("compare_qty_prior")),
        "weight_current": _clean_num(row.get("compare_weight_current")),
        "weight_prior": _clean_num(row.get("compare_weight_prior")),
        "profit_current": _clean_num(row.get("compare_profit_current")),
        "profit_prior": _clean_num(row.get("compare_profit_prior")),
        "orders_current": _clean_int(row.get("compare_orders_current")),
        "orders_prior": _clean_int(row.get("compare_orders_prior")),
    }
    comparison_summary["revenue_delta"] = comparison_summary["revenue_current"] - comparison_summary["revenue_prior"]
    comparison_summary["revenue_delta_pct"] = (
        ((comparison_summary["revenue_current"] - comparison_summary["revenue_prior"]) / comparison_summary["revenue_prior"] * 100.0)
        if comparison_summary["revenue_prior"]
        else None
    )
    comparison_summary["profit_delta"] = comparison_summary["profit_current"] - comparison_summary["profit_prior"]
    current_margin_pct = None
    if comparison_summary["revenue_current"] and row.get("compare_cost_current") is not None:
        current_margin_pct = (comparison_summary["profit_current"] / comparison_summary["revenue_current"]) * 100.0
    prior_margin_pct = None
    if comparison_summary["revenue_prior"] and row.get("compare_cost_prior") is not None:
        prior_margin_pct = (comparison_summary["profit_prior"] / comparison_summary["revenue_prior"]) * 100.0
    comparison_summary["margin_pct_current"] = current_margin_pct
    comparison_summary["margin_pct_prior"] = prior_margin_pct
    comparison_summary["margin_delta_pp"] = (
        (current_margin_pct - prior_margin_pct)
        if current_margin_pct is not None and prior_margin_pct is not None
        else None
    )

    charts = {
        "trajectory": {
            "grain": str(row.get("traj_grain") or "monthly"),
            "labels": _to_list(row.get("traj_labels")),
            "revenue": _to_list(row.get("traj_revenue")),
            "qty": _to_list(row.get("traj_qty")),
            "orders": _to_list(row.get("traj_orders")),
            "profit": _to_list(row.get("traj_profit")),
            "margin_pct": _to_list(row.get("traj_margin")),
        },
        "pareto": _to_list(row.get("pareto")),
        "movers": _to_list(row.get("movers")),
        "unit_price_dist": _to_list(row.get("price_dist")),
        "segments": {
            "summary": _to_list(row.get("segment_summary")),
            "movers": _to_list(row.get("segment_movers")),
            "mix_shift": _to_list(row.get("segment_mix_shift")),
        },
        "price_velocity": _to_list(row.get("price_velocity")),
        "top_products": _to_list(row.get("top_products")),
    }

    protein_insights = {
        "summary": {
            "family_count": _clean_int(row.get("protein_family_count")),
            "top_family": row.get("top_protein_family"),
            "top_family_share": _safe_float(row.get("top_protein_share")),
            "concentration_hhi": _safe_float(row.get("protein_hhi")),
        },
        "mix": _to_list(row.get("protein_mix")),
        "mix_shift": _to_list(row.get("protein_mix_shift")),
        "margin_watch": _to_list(row.get("protein_margin_watch")),
        "growth_pockets": _to_list(row.get("protein_growth_pockets")),
    }

    movers_enriched: List[Dict[str, Any]] = []
    for mover in charts.get("movers", []) or []:
        if not isinstance(mover, dict):
            continue
        recent_val = _clean_num(mover.get("recent"))
        prior_val = _clean_num(mover.get("prior"))
        status, delta_pct, low_base = _mover_status(recent_val, prior_val)
        mover_row = dict(mover)
        mover_row["delta"] = _safe_float(mover.get("delta_revenue"))
        mover_row["status"] = status
        mover_row["delta_pct"] = delta_pct
        mover_row["low_base"] = low_base
        movers_enriched.append(mover_row)
    charts["movers"] = movers_enriched

    segment_movers_enriched: List[Dict[str, Any]] = []
    for mover in (charts.get("segments", {}) or {}).get("movers", []) or []:
        if not isinstance(mover, dict):
            continue
        recent_val = _clean_num(mover.get("recent"))
        prior_val = _clean_num(mover.get("prior"))
        status, delta_pct, low_base = _mover_status(recent_val, prior_val)
        mover_row = dict(mover)
        mover_row["delta"] = _safe_float(mover.get("delta"))
        mover_row["status"] = status
        mover_row["delta_pct"] = delta_pct
        mover_row["low_base"] = low_base
        segment_movers_enriched.append(mover_row)
    if isinstance(charts.get("segments"), dict):
        charts["segments"]["movers"] = segment_movers_enriched

    velocity = {
        "avg_weekly": _clean_num(row.get("avg_weekly_qty")),
        "weekly_revenue": _clean_num(row.get("weekly_revenue")),
        "active_skus": _clean_int(row.get("active_skus")),
    }

    monthly_series = []
    labels = charts.get("trajectory", {}).get("labels") or []
    rev = charts.get("trajectory", {}).get("revenue") or []
    qty = charts.get("trajectory", {}).get("qty") or []
    orders = charts.get("trajectory", {}).get("orders") or []
    for idx, label in enumerate(labels):
        monthly_series.append(
            {
                "month": label,
                "revenue": _clean_num(rev[idx] if idx < len(rev) else 0),
                "units": _clean_num(qty[idx] if idx < len(qty) else 0),
                "orders": _clean_num(orders[idx] if idx < len(orders) else 0),
            }
        )

    sku_rows = _annotate_product_rows(_prepare_visual_pricing_rows([r for r in (charts.get("price_velocity") or []) if isinstance(r, dict)]))
    charts["price_velocity"] = sku_rows
    charts["top_products"] = sorted(
        [dict(r) for r in sku_rows if isinstance(r, dict)],
        key=lambda item: _clean_num(item.get("revenue")),
        reverse=True,
    )[:150]
    health_matrix = _build_health_matrix_from_rows(
        sku_rows,
        total_revenue=kpis.get("revenue") or 0.0,
        total_profit=kpis.get("profit") or 0.0,
    )
    velocity_cutoff = _safe_float(health_matrix.get("velocity_cutoff_high")) or _safe_float(row.get("health_velocity_p60")) or 0.0
    below_target_rows = [r for r in sku_rows if _row_below_target(r)]
    below_minimum_rows = [r for r in sku_rows if _row_below_minimum(r)]
    at_or_above_target_rows = [r for r in sku_rows if _row_above_target(r)]
    margin_risk_top = sorted(
        below_target_rows,
        key=lambda item: _clean_num(item.get("revenue")),
        reverse=True,
    )[:10]
    high_velocity_low_margin = sorted(
        [
            r
            for r in sku_rows
            if (_safe_float(r.get("orders_per_month")) or 0.0) >= velocity_cutoff
            and _row_below_target(r)
        ],
        key=lambda item: _clean_num(item.get("revenue")),
        reverse=True,
    )[:10]
    high_margin_low_velocity = sorted(
        [
            r
            for r in sku_rows
            if _row_above_target(r) and (_safe_float(r.get("orders_per_month")) or 0.0) < velocity_cutoff
        ],
        key=lambda item: _clean_num(item.get("revenue")),
        reverse=True,
    )[:10]
    guardrail_rows = sorted(
        sku_rows,
        key=lambda item: _clean_num(item.get("revenue")),
        reverse=True,
    )[:30]
    pricing_actions: List[Dict[str, Any]] = []
    for row_item in guardrail_rows:
        asp = _safe_float(row_item.get("unit_price"))
        p10 = _safe_float(row_item.get("up_p10"))
        p90 = _safe_float(row_item.get("up_p90"))
        margin_pct = _safe_float(row_item.get("margin_pct"))
        has_cost = _safe_float(row_item.get("unit_cost")) is not None
        action = "Hold"
        reason = "Within current pricing guardrails."
        if not has_cost:
            action = "Review cost"
            reason = "Missing cost; margin guardrails are not reliable."
        elif asp is not None and p90 is not None and p90 > 0 and asp > (p90 * 1.15):
            action = "Reduce"
            reason = "ASP is materially above the SKU historical guardrail (P90)."
        elif asp is not None and p10 is not None and p10 > 0 and asp < (p10 / 1.15):
            action = "Increase"
            reason = "ASP is below the SKU guardrail band; recover price where elasticity permits."
        elif _row_below_target(row_item):
            action = "Increase"
            reason = "Below the protein-specific target gross margin with available cost data."
        pricing_actions.append(
            {
                "product_id": row_item.get("product_id"),
                "display_name": row_item.get("display_name") or row_item.get("product_name"),
                "unit_price": asp,
                "p10": p10,
                "p50": _safe_float(row_item.get("up_p50")),
                "p90": p90,
                "price_cv_pct": _safe_float(row_item.get("price_cv_pct")),
                "revenue": _clean_num(row_item.get("revenue")),
                "margin_pct": margin_pct,
                "target_margin_pct": _safe_float(row_item.get("target_margin_pct")),
                "minimum_margin_pct": _safe_float(row_item.get("minimum_margin_pct")),
                "status_key": row_item.get("status_key"),
                "target_status": row_item.get("target_status"),
                "action": action,
                "reason": reason,
            }
        )
    execution_lists = _build_execution_lists_from_rows(sku_rows, velocity_cutoff=velocity_cutoff)
    below_target_revenue = sum(_clean_num(item.get("revenue")) for item in below_target_rows)
    below_minimum_revenue = sum(_clean_num(item.get("revenue")) for item in below_minimum_rows)
    at_or_above_target_revenue = sum(_clean_num(item.get("revenue")) for item in at_or_above_target_rows)
    negative_margin_rows = [item for item in sku_rows if _safe_float(item.get("profit")) is not None and _safe_float(item.get("profit")) < 0]
    risk_profit_uplift_target = sum(_clean_num(item.get("profit_uplift_target")) for item in below_target_rows)

    return {
        "kpis": kpis,
        "comparison_summary": comparison_summary,
        "charts": charts,
        "velocity": velocity,
        "monthly_series": monthly_series,
        "sku_metrics": charts.get("price_velocity", []),
        "health_matrix": health_matrix,
        "concentration": {
            "top1_share": _safe_float(row.get("concentration_top1_share")),
            "top10_share": _safe_float(row.get("concentration_top10_share")),
            "hhi": _safe_float(row.get("concentration_hhi")),
            "skus_to_80": _clean_int(row.get("concentration_skus_to_80")),
        },
        "risk_opportunity": {
            "below_minimum_count": len(below_minimum_rows),
            "below_minimum_revenue": below_minimum_revenue,
            "below_target_count": len(below_target_rows),
            "below_target_revenue": below_target_revenue,
            "at_or_above_target_count": len(at_or_above_target_rows),
            "at_or_above_target_revenue": at_or_above_target_revenue,
            "negative_margin_count": len(negative_margin_rows),
            "profit_uplift_target": risk_profit_uplift_target,
            "margin_risk_top": margin_risk_top,
            "high_velocity_low_margin": high_velocity_low_margin,
            "high_margin_low_velocity": high_margin_low_velocity,
        },
        "pricing_guardrails": {
            "high_outlier_count": _clean_int(row.get("high_price_outlier_count")),
            "low_outlier_count": _clean_int(row.get("low_price_outlier_count")),
            "outside_count": _clean_int(row.get("outside_guardrail_count")),
            "outside_pct": _safe_float(row.get("outside_guardrail_pct")),
            "rows": pricing_actions,
        },
        "execution_lists": execution_lists,
        "protein_insights": protein_insights,
    }


def _summary_metrics_and_context(
    comparison_where_sql: str,
    comparison_params: List[Any],
    cols: set[str],
    *,
    current_start: str,
    current_end: str,
    recent_start: str,
    recent_end: str,
    prior_start: str,
    prior_end: str,
) -> Dict[str, Any]:
    date_col = _safe_col(cols, fs.CANON.date, "Date")
    revenue_col = _safe_col(cols, fs.CANON.revenue, "Revenue")
    cost_expr = _coalesce_expr(cols, (fs.CANON.cost, "Cost", "CostPrice"), "NULL")
    qty_expr = _coalesce_expr(cols, (fs.CANON.qty_units, "ShippedItems", "QuantityOrdered", "Qty", "Quantity", "Units", "ItemCount"), "0")
    weight_expr = _coalesce_expr(cols, (fs.CANON.weight_lb, "Weight", "WeightLb", "ShippedLb", "pack_weight_lb_sum"), "0")
    sku_col, _prod_id_col, prod_name = _resolve_product_columns(cols)
    cust_id = _safe_col(cols, fs.CANON.customer_id, "CustomerID")
    order_id = _safe_col(cols, fs.CANON.order_id, "OrderID")

    if not all([date_col, revenue_col, sku_col, prod_name, cust_id, order_id]):
        return {"error": {"message": "Required columns missing for products summary"}, "meta": {"cached": False}}

    exprs = _product_exprs(cols)
    family_exprs = _family_exprs(cols)
    sku_expr = exprs["sku_expr"]
    product_key_expr = exprs["product_key_expr"]
    product_name_expr = exprs["product_name_expr"]
    display_name_expr = exprs["display_name_expr"]
    protein_expr = family_exprs["protein_expr"]
    category_expr = family_exprs["category_expr"]
    product_target_margin_expr = margin_rules.sql_margin_rule_expr(
        "product_rollup.protein_family",
        "product_rollup.product_category",
        "target_gross_margin_pct",
    )
    seg_target_margin_expr = margin_rules.sql_margin_rule_expr(
        "seg_scored.protein_family",
        "seg_scored.product_category",
        "target_gross_margin_pct",
    )
    health_target_margin_expr = margin_rules.sql_margin_rule_expr(
        "health_classified.protein_family",
        "health_classified.product_category",
        "target_gross_margin_pct",
    )
    effective_cost_expr = margin_rules.sql_effective_cost_expr("base_cost", "weight", "qty", fallback="NULL")

    sql = f"""
        WITH base AS (
            SELECT
                {date_col}::DATE AS date,
                {product_key_expr} AS product_id,
                {sku_expr} AS sku,
                {product_name_expr} AS product_name,
                {display_name_expr} AS display_name,
                {protein_expr} AS protein_family,
                {category_expr} AS product_category,
                {cust_id}::VARCHAR AS customer_id,
                {order_id}::VARCHAR AS order_id,
                CAST({revenue_col} AS DOUBLE) AS revenue,
                CAST({cost_expr} AS DOUBLE) AS base_cost,
                CAST({qty_expr} AS DOUBLE) AS qty,
                CAST({weight_expr} AS DOUBLE) AS weight
            FROM fact
            WHERE {comparison_where_sql}
        ),
        enriched AS (
            SELECT
                *,
                ({effective_cost_expr}) AS cost,
                CASE
                    WHEN ({effective_cost_expr}) IS NULL THEN NULL
                    ELSE revenue - ({effective_cost_expr})
                END AS profit,
                CASE
                    WHEN weight > 0 THEN revenue / NULLIF(weight, 0)
                    WHEN qty > 0 THEN revenue / NULLIF(qty, 0)
                    ELSE NULL
                END AS unit_price,
                DATE_TRUNC('month', date)::DATE AS month_date
            FROM base
        ),
        bounds AS (
            SELECT
                ?::DATE AS current_start,
                ?::DATE AS current_end,
                ?::DATE AS recent_start,
                ?::DATE AS recent_end,
                ?::DATE AS prior_start,
                ?::DATE AS prior_end
        ),
        current_enriched AS (
            SELECT e.*
            FROM enriched e
            CROSS JOIN bounds b
            WHERE e.date BETWEEN b.current_start AND b.current_end
        ),
        agg AS (
            SELECT
                SUM(revenue) AS revenue,
                SUM(qty) AS qty,
                SUM(weight) AS weight,
                COUNT(DISTINCT product_id) AS products,
                COUNT(DISTINCT customer_id) AS customers,
                COUNT(DISTINCT order_id) AS orders,
                CASE WHEN SUM(cost) IS NULL THEN NULL ELSE SUM(revenue) - SUM(cost) END AS profit,
                CASE WHEN SUM(revenue) > 0 AND SUM(cost) IS NOT NULL THEN (SUM(revenue) - SUM(cost)) / SUM(revenue) * 100 ELSE NULL END AS margin_pct,
                AVG(unit_price) AS avg_price,
                median(unit_price) AS median_price,
                quantile_cont(unit_price, 0.10) AS up_p10,
                quantile_cont(unit_price, 0.50) AS up_p50,
                quantile_cont(unit_price, 0.90) AS up_p90
            FROM current_enriched
        ),
        window_span AS (
            SELECT
                date_diff('day', MIN(date), MAX(date)) + 1 AS window_days
            FROM current_enriched
        ),
        trajectory AS (
            SELECT
                CASE
                    WHEN ws.window_days < 120 THEN strftime('%Y-W%W', DATE_TRUNC('week', e.date))
                    ELSE strftime('%Y-%m', DATE_TRUNC('month', e.date))
                END AS period_label,
                CASE
                    WHEN ws.window_days < 120 THEN DATE_TRUNC('week', e.date)
                    ELSE DATE_TRUNC('month', e.date)
                END AS period_start,
                SUM(e.revenue) AS revenue,
                SUM(e.qty) AS qty,
                COUNT(DISTINCT e.order_id) AS orders,
                CASE WHEN SUM(e.cost) IS NULL THEN NULL ELSE SUM(e.revenue) - SUM(e.cost) END AS profit,
                CASE WHEN SUM(e.revenue) > 0 AND SUM(e.cost) IS NOT NULL THEN (SUM(e.revenue) - SUM(e.cost)) / SUM(e.revenue) * 100 ELSE NULL END AS margin_pct
            FROM current_enriched e
            CROSS JOIN window_span ws
            GROUP BY period_label, period_start
            ORDER BY period_start
        ),
        product_rollup AS (
            SELECT
                product_id,
                any_value(product_name) AS product_name,
                any_value(display_name) AS display_name,
                any_value(sku) AS sku,
                any_value(protein_family) AS protein_family,
                any_value(product_category) AS product_category,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END) AS revenue,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN qty ELSE 0 END) AS qty,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN weight ELSE 0 END) AS weight,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) AS cost,
                CASE
                    WHEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) IS NULL THEN NULL
                    ELSE SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END) - SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END)
                END AS profit,
                COUNT(DISTINCT CASE WHEN date BETWEEN b.current_start AND b.current_end THEN order_id END) AS orders,
                COUNT(DISTINCT CASE WHEN date BETWEEN b.current_start AND b.current_end THEN month_date END) AS months_active,
                CASE
                    WHEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN weight ELSE 0 END) > 0
                        THEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END) / NULLIF(SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN weight ELSE 0 END), 0)
                    WHEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN qty ELSE 0 END) > 0
                        THEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END) / NULLIF(SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN qty ELSE 0 END), 0)
                    ELSE NULL
                END AS unit_price,
                CASE
                    WHEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN weight ELSE 0 END) > 0 AND SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) IS NOT NULL
                        THEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) / NULLIF(SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN weight ELSE 0 END), 0)
                    WHEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN qty ELSE 0 END) > 0 AND SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) IS NOT NULL
                        THEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) / NULLIF(SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN qty ELSE 0 END), 0)
                    ELSE NULL
                END AS unit_cost,
                CASE
                    WHEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN weight ELSE 0 END) > 0 AND SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) IS NOT NULL
                        THEN (
                            SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END)
                            - SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END)
                        ) / NULLIF(SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN weight ELSE 0 END), 0)
                    ELSE NULL
                END AS contribution_lb,
                CASE
                    WHEN SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END) > 0
                        AND SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) IS NOT NULL
                        THEN (
                            SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END)
                            - SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END)
                        ) / SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END) * 100
                    ELSE NULL
                END AS margin_pct,
                quantile_cont(unit_price, 0.10) AS up_p10,
                quantile_cont(unit_price, 0.50) AS up_p50,
                quantile_cont(unit_price, 0.90) AS up_p90,
                SUM(CASE WHEN date BETWEEN b.recent_start AND b.recent_end THEN revenue ELSE 0 END) AS rev_recent,
                SUM(CASE WHEN date BETWEEN b.prior_start AND b.prior_end THEN revenue ELSE 0 END) AS rev_prior,
                SUM(CASE WHEN date BETWEEN b.recent_start AND b.recent_end AND cost IS NOT NULL THEN revenue - cost ELSE NULL END) AS profit_recent,
                SUM(CASE WHEN date BETWEEN b.prior_start AND b.prior_end AND cost IS NOT NULL THEN revenue - cost ELSE NULL END) AS profit_prior,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end AND cost IS NOT NULL THEN revenue ELSE 0 END) AS revenue_with_cost
            FROM enriched
            CROSS JOIN bounds b
            GROUP BY product_id
        ),
        family_rollup AS (
            SELECT
                COALESCE(NULLIF(protein_family, ''), 'Unassigned') AS family,
                COALESCE(NULLIF(product_category, ''), COALESCE(NULLIF(protein_family, ''), 'Unassigned')) AS category,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END) AS revenue_current,
                SUM(CASE WHEN date BETWEEN b.prior_start AND b.prior_end THEN revenue ELSE 0 END) AS revenue_prior,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN weight ELSE 0 END) AS weight_current,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) AS cost_current,
                COUNT(DISTINCT CASE WHEN date BETWEEN b.current_start AND b.current_end THEN product_id END) AS sku_count,
                COUNT(DISTINCT CASE WHEN date BETWEEN b.current_start AND b.current_end THEN customer_id END) AS customer_count,
                COUNT(DISTINCT CASE WHEN date BETWEEN b.current_start AND b.current_end THEN order_id END) AS order_count
            FROM enriched
            CROSS JOIN bounds b
            GROUP BY 1, 2
        ),
        family_mix_enriched AS (
            SELECT
                family,
                category,
                revenue_current AS revenue,
                revenue_prior,
                weight_current AS weight,
                sku_count,
                customer_count,
                order_count,
                CASE
                    WHEN revenue_current > 0 AND cost_current IS NOT NULL
                        THEN (revenue_current - cost_current) / revenue_current * 100
                    ELSE NULL
                END AS margin_pct,
                CASE
                    WHEN weight_current > 0 AND cost_current IS NOT NULL
                        THEN (revenue_current - cost_current) / NULLIF(weight_current, 0)
                    ELSE NULL
                END AS profit_per_lb,
                CASE WHEN SUM(revenue_current) OVER () > 0 THEN revenue_current / SUM(revenue_current) OVER () * 100 ELSE NULL END AS share_current,
                CASE WHEN SUM(revenue_prior) OVER () > 0 THEN revenue_prior / SUM(revenue_prior) OVER () * 100 ELSE NULL END AS share_prior,
                CASE
                    WHEN SUM(revenue_current) OVER () > 0 AND SUM(revenue_prior) OVER () > 0
                        THEN (revenue_current / SUM(revenue_current) OVER () * 100) - (revenue_prior / SUM(revenue_prior) OVER () * 100)
                    ELSE NULL
                END AS share_delta_pp
            FROM family_rollup
            WHERE revenue_current <> 0 OR revenue_prior <> 0
        ),
        compare_summary AS (
            SELECT
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN revenue ELSE 0 END) AS revenue_current,
                SUM(CASE WHEN date BETWEEN b.prior_start AND b.prior_end THEN revenue ELSE 0 END) AS revenue_prior,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN qty ELSE 0 END) AS qty_current,
                SUM(CASE WHEN date BETWEEN b.prior_start AND b.prior_end THEN qty ELSE 0 END) AS qty_prior,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN weight ELSE 0 END) AS weight_current,
                SUM(CASE WHEN date BETWEEN b.prior_start AND b.prior_end THEN weight ELSE 0 END) AS weight_prior,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end THEN cost ELSE NULL END) AS cost_current,
                SUM(CASE WHEN date BETWEEN b.prior_start AND b.prior_end THEN cost ELSE NULL END) AS cost_prior,
                SUM(CASE WHEN date BETWEEN b.current_start AND b.current_end AND cost IS NOT NULL THEN revenue - cost ELSE NULL END) AS profit_current,
                SUM(CASE WHEN date BETWEEN b.prior_start AND b.prior_end AND cost IS NOT NULL THEN revenue - cost ELSE NULL END) AS profit_prior,
                COUNT(DISTINCT CASE WHEN date BETWEEN b.current_start AND b.current_end THEN order_id END) AS orders_current,
                COUNT(DISTINCT CASE WHEN date BETWEEN b.prior_start AND b.prior_end THEN order_id END) AS orders_prior
            FROM enriched
            CROSS JOIN bounds b
        ),
        velocity AS (
            SELECT
                CASE WHEN COUNT(*) > 0 THEN SUM(qty) / NULLIF(date_diff('week', MIN(date), MAX(date)) + 1, 0) ELSE 0 END AS avg_weekly_qty,
                CASE WHEN COUNT(*) > 0 THEN SUM(revenue) / NULLIF(date_diff('week', MIN(date), MAX(date)) + 1, 0) ELSE 0 END AS weekly_revenue,
                COUNT(DISTINCT product_id) AS active_skus
            FROM current_enriched
        ),
        seg_scored AS (
            SELECT
                *,
                COALESCE(quantile_cont(revenue, 0.80) OVER (), 0) AS rev_p80,
                COALESCE(quantile_cont(orders, 0.60) OVER (), 0) AS ord_p60,
                CASE
                    WHEN revenue >= rev_p80 AND orders >= ord_p60 THEN 'Stars'
                    WHEN revenue >= rev_p80 THEN 'Cash Cows'
                    WHEN orders >= ord_p60 THEN 'Volume Drivers'
                    WHEN margin_pct IS NOT NULL AND margin_pct < 5 THEN 'Margin Risk'
                    ELSE 'Long Tail'
                END AS segment
            FROM product_rollup
        ),
        segment_summary AS (
            SELECT
                segment,
                COUNT(*) AS sku_count,
                SUM(revenue) AS revenue
            FROM seg_scored
            GROUP BY segment
        ),
        segment_mix AS (
            SELECT
                segment,
                SUM(rev_recent) AS revenue_current,
                SUM(rev_prior) AS revenue_prior
            FROM seg_scored
            GROUP BY segment
        ),
        segment_mix_enriched AS (
            SELECT
                segment,
                revenue_current,
                revenue_prior,
                CASE WHEN SUM(revenue_current) OVER () > 0 THEN revenue_current / SUM(revenue_current) OVER () * 100 ELSE NULL END AS share_current,
                CASE WHEN SUM(revenue_prior) OVER () > 0 THEN revenue_prior / SUM(revenue_prior) OVER () * 100 ELSE NULL END AS share_prior,
                CASE
                    WHEN SUM(revenue_current) OVER () > 0 AND SUM(revenue_prior) OVER () > 0
                        THEN (revenue_current / SUM(revenue_current) OVER () * 100) - (revenue_prior / SUM(revenue_prior) OVER () * 100)
                    ELSE NULL
                END AS share_delta_pp
            FROM segment_mix
        ),
        price_velocity AS (
            SELECT
                *,
                CASE WHEN months_active > 0 THEN orders / NULLIF(months_active, 0) ELSE NULL END AS orders_per_month
            FROM seg_scored
        ),
        health_thresholds AS (
            SELECT
                quantile_cont(orders_per_month, 0.40) FILTER (WHERE orders_per_month IS NOT NULL) AS velocity_p40,
                quantile_cont(orders_per_month, 0.50) FILTER (WHERE orders_per_month IS NOT NULL) AS velocity_p50,
                quantile_cont(orders_per_month, 0.60) FILTER (WHERE orders_per_month IS NOT NULL) AS velocity_p60,
                quantile_cont(COALESCE(margin_pct, contribution_lb), 0.40) FILTER (WHERE COALESCE(margin_pct, contribution_lb) IS NOT NULL) AS profitability_p40,
                quantile_cont(COALESCE(margin_pct, contribution_lb), 0.50) FILTER (WHERE COALESCE(margin_pct, contribution_lb) IS NOT NULL) AS profitability_p50,
                quantile_cont(COALESCE(margin_pct, contribution_lb), 0.60) FILTER (WHERE COALESCE(margin_pct, contribution_lb) IS NOT NULL) AS profitability_p60
            FROM price_velocity
        ),
        health_classified AS (
            SELECT
                pv.*,
                CASE
                    WHEN COALESCE(pv.orders_per_month, 0) >= COALESCE(ht.velocity_p60, COALESCE(ht.velocity_p50, 0)) THEN 'high'
                    WHEN COALESCE(pv.orders_per_month, 0) <= COALESCE(ht.velocity_p40, COALESCE(ht.velocity_p50, 0)) THEN 'low'
                    WHEN COALESCE(pv.orders_per_month, 0) >= COALESCE(ht.velocity_p50, 0) THEN 'high'
                    ELSE 'low'
                END AS velocity_band,
                CASE
                    WHEN COALESCE(COALESCE(pv.margin_pct, pv.contribution_lb), -999999) >= COALESCE(ht.profitability_p60, COALESCE(ht.profitability_p50, 0)) THEN 'high'
                    WHEN COALESCE(COALESCE(pv.margin_pct, pv.contribution_lb), -999999) <= COALESCE(ht.profitability_p40, COALESCE(ht.profitability_p50, 0)) THEN 'low'
                    WHEN COALESCE(COALESCE(pv.margin_pct, pv.contribution_lb), -999999) >= COALESCE(ht.profitability_p50, 0) THEN 'high'
                    ELSE 'low'
                END AS profitability_band,
                CASE
                    WHEN (
                        CASE
                            WHEN COALESCE(pv.orders_per_month, 0) >= COALESCE(ht.velocity_p60, COALESCE(ht.velocity_p50, 0)) THEN 'high'
                            WHEN COALESCE(pv.orders_per_month, 0) <= COALESCE(ht.velocity_p40, COALESCE(ht.velocity_p50, 0)) THEN 'low'
                            WHEN COALESCE(pv.orders_per_month, 0) >= COALESCE(ht.velocity_p50, 0) THEN 'high'
                            ELSE 'low'
                        END
                    ) = 'high' AND (
                        CASE
                            WHEN COALESCE(COALESCE(pv.margin_pct, pv.contribution_lb), -999999) >= COALESCE(ht.profitability_p60, COALESCE(ht.profitability_p50, 0)) THEN 'high'
                            WHEN COALESCE(COALESCE(pv.margin_pct, pv.contribution_lb), -999999) <= COALESCE(ht.profitability_p40, COALESCE(ht.profitability_p50, 0)) THEN 'low'
                            WHEN COALESCE(COALESCE(pv.margin_pct, pv.contribution_lb), -999999) >= COALESCE(ht.profitability_p50, 0) THEN 'high'
                            ELSE 'low'
                        END
                    ) = 'high' THEN 'protect'
                    WHEN (
                        CASE
                            WHEN COALESCE(pv.orders_per_month, 0) >= COALESCE(ht.velocity_p60, COALESCE(ht.velocity_p50, 0)) THEN 'high'
                            WHEN COALESCE(pv.orders_per_month, 0) <= COALESCE(ht.velocity_p40, COALESCE(ht.velocity_p50, 0)) THEN 'low'
                            WHEN COALESCE(pv.orders_per_month, 0) >= COALESCE(ht.velocity_p50, 0) THEN 'high'
                            ELSE 'low'
                        END
                    ) = 'high' THEN 'fix_margin'
                    WHEN (
                        CASE
                            WHEN COALESCE(COALESCE(pv.margin_pct, pv.contribution_lb), -999999) >= COALESCE(ht.profitability_p60, COALESCE(ht.profitability_p50, 0)) THEN 'high'
                            WHEN COALESCE(COALESCE(pv.margin_pct, pv.contribution_lb), -999999) <= COALESCE(ht.profitability_p40, COALESCE(ht.profitability_p50, 0)) THEN 'low'
                            WHEN COALESCE(COALESCE(pv.margin_pct, pv.contribution_lb), -999999) >= COALESCE(ht.profitability_p50, 0) THEN 'high'
                            ELSE 'low'
                        END
                    ) = 'high' THEN 'grow'
                    ELSE 'rationalize'
                END AS quadrant
            FROM price_velocity pv
            CROSS JOIN health_thresholds ht
        ),
        health_summary AS (
            SELECT
                quadrant,
                COUNT(*) AS sku_count,
                SUM(revenue) AS revenue,
                SUM(profit) AS profit
            FROM health_classified
            GROUP BY quadrant
        ),
        health_top AS (
            SELECT
                quadrant,
                product_id,
                display_name,
                revenue,
                profit,
                margin_pct,
                orders_per_month AS velocity_per_month,
                ROW_NUMBER() OVER (PARTITION BY quadrant ORDER BY revenue DESC) AS rn
            FROM health_classified
        ),
        top_product AS (
            SELECT product_id, display_name, revenue
            FROM product_rollup
            ORDER BY revenue DESC NULLS LAST
            LIMIT 1
        )
        SELECT
            agg.revenue,
            agg.qty,
            agg.weight,
            agg.products,
            agg.customers,
            agg.orders,
            agg.profit,
            agg.margin_pct,
            agg.avg_price,
            agg.median_price,
            agg.up_p10,
            agg.up_p50,
            agg.up_p90,
            compare_summary.revenue_current AS compare_revenue_current,
            compare_summary.revenue_prior AS compare_revenue_prior,
            compare_summary.qty_current AS compare_qty_current,
            compare_summary.qty_prior AS compare_qty_prior,
            compare_summary.weight_current AS compare_weight_current,
            compare_summary.weight_prior AS compare_weight_prior,
            compare_summary.cost_current AS compare_cost_current,
            compare_summary.cost_prior AS compare_cost_prior,
            compare_summary.profit_current AS compare_profit_current,
            compare_summary.profit_prior AS compare_profit_prior,
            compare_summary.orders_current AS compare_orders_current,
            compare_summary.orders_prior AS compare_orders_prior,
            velocity.avg_weekly_qty,
            velocity.weekly_revenue,
            velocity.active_skus,
            (SELECT CASE WHEN window_days < 120 THEN 'weekly' ELSE 'monthly' END FROM window_span) AS traj_grain,
            (SELECT list(period_label) FROM trajectory) AS traj_labels,
            (SELECT list(revenue) FROM trajectory) AS traj_revenue,
            (SELECT list(qty) FROM trajectory) AS traj_qty,
            (SELECT list(orders) FROM trajectory) AS traj_orders,
            (SELECT list(profit) FROM trajectory) AS traj_profit,
            (SELECT list(margin_pct) FROM trajectory) AS traj_margin,
            (SELECT velocity_p40 FROM health_thresholds) AS health_velocity_p40,
            (SELECT velocity_p60 FROM health_thresholds) AS health_velocity_p60,
            (SELECT profitability_p40 FROM health_thresholds) AS health_profitability_p40,
            (SELECT profitability_p60 FROM health_thresholds) AS health_profitability_p60,
            (SELECT list(struct_pack(segment:=segment, sku_count:=sku_count, revenue:=revenue)) FROM segment_summary) AS segment_summary,
            (SELECT list(struct_pack(segment:=segment, revenue_current:=revenue_current, revenue_prior:=revenue_prior, share_current:=share_current, share_prior:=share_prior, share_delta_pp:=share_delta_pp)) FROM (SELECT * FROM segment_mix_enriched ORDER BY ABS(share_delta_pp) DESC)) AS segment_mix_shift,
            (SELECT list(struct_pack(quadrant:=quadrant, sku_count:=sku_count, revenue:=revenue, profit:=profit)) FROM health_summary) AS health_summary,
            (SELECT list(struct_pack(quadrant:=quadrant, product_id:=product_id, product_name:=display_name, display_name:=display_name, revenue:=revenue, profit:=profit, margin_pct:=margin_pct, velocity_per_month:=velocity_per_month)) FROM health_top WHERE rn <= 10) AS health_top,
            (SELECT product_id FROM top_product LIMIT 1) AS top_product_id,
            (SELECT display_name FROM top_product LIMIT 1) AS top_product_display_name,
            (SELECT revenue FROM top_product LIMIT 1) AS top_product_revenue,
            (
                SELECT MAX(rev_share)
                FROM (
                    SELECT
                        ROW_NUMBER() OVER (ORDER BY revenue DESC) AS rn,
                        CASE WHEN SUM(revenue) OVER () > 0 THEN revenue / SUM(revenue) OVER () * 100 ELSE 0 END AS rev_share
                    FROM product_rollup
                ) ranked
                WHERE rn = 1
            ) AS concentration_top1_share,
            (
                SELECT SUM(rev_share)
                FROM (
                    SELECT
                        ROW_NUMBER() OVER (ORDER BY revenue DESC) AS rn,
                        CASE WHEN SUM(revenue) OVER () > 0 THEN revenue / SUM(revenue) OVER () * 100 ELSE 0 END AS rev_share
                    FROM product_rollup
                ) ranked
                WHERE rn <= 10
            ) AS concentration_top10_share,
            (
                SELECT SUM(POWER(rev_share / 100.0, 2)) * 10000
                FROM (
                    SELECT CASE WHEN SUM(revenue) OVER () > 0 THEN revenue / SUM(revenue) OVER () * 100 ELSE 0 END AS rev_share
                    FROM product_rollup
                ) ranked
            ) AS concentration_hhi,
            (
                SELECT MIN(rn)
                FROM (
                    SELECT
                        ROW_NUMBER() OVER (ORDER BY revenue DESC) AS rn,
                        CASE
                            WHEN SUM(revenue) OVER () > 0
                                THEN SUM(revenue) OVER (ORDER BY revenue DESC) / SUM(revenue) OVER () * 100
                            ELSE NULL
                        END AS cum_share
                    FROM product_rollup
                ) ranked
                WHERE cum_share >= 80
            ) AS concentration_skus_to_80,
            (SELECT COUNT(*) FROM family_mix_enriched) AS protein_family_count,
            (SELECT family FROM family_mix_enriched ORDER BY revenue DESC NULLS LAST LIMIT 1) AS top_protein_family,
            (SELECT share_current FROM family_mix_enriched ORDER BY revenue DESC NULLS LAST LIMIT 1) AS top_protein_share,
            (
                SELECT SUM(POWER(COALESCE(share_current, 0) / 100.0, 2)) * 10000
                FROM family_mix_enriched
            ) AS protein_hhi,
            (SELECT list(struct_pack(family:=family, category:=category, revenue:=revenue, revenue_prior:=revenue_prior, weight:=weight, margin_pct:=margin_pct, profit_per_lb:=profit_per_lb, share_current:=share_current, share_prior:=share_prior, share_delta_pp:=share_delta_pp, sku_count:=sku_count, customer_count:=customer_count, order_count:=order_count)) FROM (SELECT * FROM family_mix_enriched ORDER BY revenue DESC NULLS LAST LIMIT 8)) AS protein_mix,
            (SELECT list(struct_pack(family:=family, category:=category, revenue:=revenue, share_current:=share_current, share_prior:=share_prior, share_delta_pp:=share_delta_pp, customer_count:=customer_count, sku_count:=sku_count)) FROM (SELECT * FROM family_mix_enriched ORDER BY ABS(share_delta_pp) DESC NULLS LAST LIMIT 6)) AS protein_mix_shift,
            (SELECT list(struct_pack(family:=family, category:=category, revenue:=revenue, margin_pct:=margin_pct, profit_per_lb:=profit_per_lb, share_current:=share_current, customer_count:=customer_count, sku_count:=sku_count)) FROM (SELECT * FROM family_mix_enriched ORDER BY COALESCE(margin_pct, 999999) ASC, revenue DESC NULLS LAST LIMIT 6)) AS protein_margin_watch,
            (SELECT list(struct_pack(family:=family, category:=category, revenue:=revenue, share_delta_pp:=share_delta_pp, margin_pct:=margin_pct, customer_count:=customer_count, sku_count:=sku_count)) FROM (SELECT * FROM family_mix_enriched ORDER BY share_delta_pp DESC NULLS LAST, revenue DESC NULLS LAST LIMIT 6)) AS protein_growth_pockets,
            (
                SELECT COUNT(*)
                FROM product_rollup
                WHERE margin_pct IS NOT NULL
                  AND ({product_target_margin_expr}) IS NOT NULL
                  AND margin_pct < ({product_target_margin_expr})
            ) AS risk_below_target_count,
            (
                SELECT SUM(revenue)
                FROM product_rollup
                WHERE margin_pct IS NOT NULL
                  AND ({product_target_margin_expr}) IS NOT NULL
                  AND margin_pct < ({product_target_margin_expr})
            ) AS risk_below_target_revenue,
            (SELECT COUNT(*) FROM product_rollup WHERE margin_pct IS NOT NULL AND margin_pct < 0) AS risk_negative_margin_count,
            (
                SELECT SUM((revenue * (({product_target_margin_expr}) / 100.0)) - profit)
                FROM product_rollup
                WHERE margin_pct IS NOT NULL
                  AND ({product_target_margin_expr}) IS NOT NULL
                  AND margin_pct < ({product_target_margin_expr})
                  AND profit IS NOT NULL
            ) AS risk_profit_uplift_target,
            (
                SELECT COUNT(*)
                FROM health_classified
                WHERE velocity_band = 'high'
                  AND margin_pct IS NOT NULL
                  AND ({health_target_margin_expr}) IS NOT NULL
                  AND margin_pct < ({health_target_margin_expr})
            ) AS high_velocity_low_margin_count,
            (
                SELECT COUNT(*)
                FROM health_classified
                WHERE profitability_band = 'high' AND velocity_band = 'low'
            ) AS high_margin_low_velocity_count,
            (
                SELECT CASE
                    WHEN SUM(revenue) > 0 THEN SUM(revenue_with_cost) / NULLIF(SUM(revenue), 0) * 100
                    ELSE NULL
                END
                FROM product_rollup
            ) AS cost_coverage_pct,
            (SELECT COUNT(*) FROM product_rollup WHERE cost IS NULL OR cost <= 0) AS missing_cost_sku_count,
            (SELECT quantile_cont(contribution_lb, 0.10) FROM product_rollup WHERE contribution_lb IS NOT NULL) AS contribution_lb_p10,
            (SELECT quantile_cont(contribution_lb, 0.50) FROM product_rollup WHERE contribution_lb IS NOT NULL) AS contribution_lb_p50,
            (SELECT quantile_cont(contribution_lb, 0.90) FROM product_rollup WHERE contribution_lb IS NOT NULL) AS contribution_lb_p90,
            (
                SELECT SUM(COALESCE(profit, 0))
                FROM product_rollup
                WHERE (
                    margin_pct IS NOT NULL
                    AND ({product_target_margin_expr}) IS NOT NULL
                    AND margin_pct < ({product_target_margin_expr})
                ) OR cost IS NULL
            ) AS profit_at_risk,
            (
                SELECT COUNT(*)
                FROM product_rollup
                WHERE unit_price IS NOT NULL AND up_p90 IS NOT NULL AND up_p90 > 0 AND unit_price > up_p90 * 1.15
            ) AS high_price_outlier_count,
            (
                SELECT COUNT(*)
                FROM product_rollup
                WHERE unit_price IS NOT NULL AND up_p10 IS NOT NULL AND up_p10 > 0 AND unit_price < up_p10 / 1.15
            ) AS low_price_outlier_count,
            (
                SELECT COUNT(*)
                FROM product_rollup
                WHERE unit_price IS NOT NULL AND (
                    (up_p90 IS NOT NULL AND up_p90 > 0 AND unit_price > up_p90 * 1.15)
                    OR
                    (up_p10 IS NOT NULL AND up_p10 > 0 AND unit_price < up_p10 / 1.15)
                )
            ) AS outside_guardrail_count,
            (
                SELECT CASE WHEN COUNT(*) > 0 THEN (
                    COUNT(*) FILTER (
                        WHERE unit_price IS NOT NULL AND (
                            (up_p90 IS NOT NULL AND up_p90 > 0 AND unit_price > up_p90 * 1.15)
                            OR
                            (up_p10 IS NOT NULL AND up_p10 > 0 AND unit_price < up_p10 / 1.15)
                        )
                    )::DOUBLE / COUNT(*)::DOUBLE * 100
                ) ELSE NULL END
                FROM product_rollup
            ) AS outside_guardrail_pct
        FROM agg, velocity, compare_summary
        LIMIT 1
    """

    args = list(comparison_params) + [
        current_start,
        current_end,
        recent_start,
        recent_end,
        prior_start,
        prior_end,
    ]
    df = fact_store.execute_sql_df(
        sql,
        args,
        tag="products.summary_bundle",
        cache_key="products.summary_bundle",
    )
    if df.empty:
        return {}

    row = df.iloc[0]
    kpis = {
        "revenue": _clean_num(row.get("revenue")),
        "qty": _clean_num(row.get("qty")),
        "weight": _clean_num(row.get("weight")),
        "products": _clean_int(row.get("products")),
        "customers": _clean_int(row.get("customers")),
        "orders": _clean_int(row.get("orders")),
        "profit": _clean_num(row.get("profit")),
        "margin_pct": _safe_float(row.get("margin_pct")),
        "avg_price": _safe_float(row.get("avg_price")),
        "median_price": _safe_float(row.get("median_price")),
        "unit_price_p10": _safe_float(row.get("up_p10")),
        "unit_price_p50": _safe_float(row.get("up_p50")),
        "unit_price_p90": _safe_float(row.get("up_p90")),
        "cost_coverage_pct": _safe_float(row.get("cost_coverage_pct")),
        "missing_cost_sku_count": _clean_int(row.get("missing_cost_sku_count")),
        "contribution_lb_p10": _safe_float(row.get("contribution_lb_p10")),
        "contribution_lb_p50": _safe_float(row.get("contribution_lb_p50")),
        "contribution_lb_p90": _safe_float(row.get("contribution_lb_p90")),
        "profit_at_risk": _clean_num(row.get("profit_at_risk")),
        "high_price_outlier_count": _clean_int(row.get("high_price_outlier_count")),
        "low_price_outlier_count": _clean_int(row.get("low_price_outlier_count")),
        "outside_guardrail_count": _clean_int(row.get("outside_guardrail_count")),
        "outside_guardrail_pct": _safe_float(row.get("outside_guardrail_pct")),
        "concentration_top1_share": _safe_float(row.get("concentration_top1_share")),
        "concentration_top10_share": _safe_float(row.get("concentration_top10_share")),
        "concentration_hhi": _safe_float(row.get("concentration_hhi")),
        "concentration_skus_to_80": _clean_int(row.get("concentration_skus_to_80")),
        "protein_family_count": _clean_int(row.get("protein_family_count")),
        "top_protein_family": row.get("top_protein_family"),
        "top_protein_share": _safe_float(row.get("top_protein_share")),
        "protein_hhi": _safe_float(row.get("protein_hhi")),
        "risk_below_target_count": _clean_int(row.get("risk_below_target_count")),
        "risk_below_target_revenue": _clean_num(row.get("risk_below_target_revenue")),
        "risk_negative_margin_count": _clean_int(row.get("risk_negative_margin_count")),
        "risk_profit_uplift_target": _clean_num(row.get("risk_profit_uplift_target")),
    }
    kpis["revenue_per_product"] = kpis["revenue"] / kpis["products"] if kpis["products"] else None
    kpis["revenue_per_customer"] = kpis["revenue"] / kpis["customers"] if kpis["customers"] else None

    comparison_summary = {
        "revenue_current": _clean_num(row.get("compare_revenue_current")),
        "revenue_prior": _clean_num(row.get("compare_revenue_prior")),
        "qty_current": _clean_num(row.get("compare_qty_current")),
        "qty_prior": _clean_num(row.get("compare_qty_prior")),
        "weight_current": _clean_num(row.get("compare_weight_current")),
        "weight_prior": _clean_num(row.get("compare_weight_prior")),
        "profit_current": _clean_num(row.get("compare_profit_current")),
        "profit_prior": _clean_num(row.get("compare_profit_prior")),
        "orders_current": _clean_int(row.get("compare_orders_current")),
        "orders_prior": _clean_int(row.get("compare_orders_prior")),
    }
    comparison_summary["revenue_delta"] = comparison_summary["revenue_current"] - comparison_summary["revenue_prior"]
    comparison_summary["revenue_delta_pct"] = (
        ((comparison_summary["revenue_current"] - comparison_summary["revenue_prior"]) / comparison_summary["revenue_prior"] * 100.0)
        if comparison_summary["revenue_prior"]
        else None
    )
    comparison_summary["profit_delta"] = comparison_summary["profit_current"] - comparison_summary["profit_prior"]
    current_margin_pct = None
    if comparison_summary["revenue_current"] and row.get("compare_cost_current") is not None:
        current_margin_pct = (comparison_summary["profit_current"] / comparison_summary["revenue_current"]) * 100.0
    prior_margin_pct = None
    if comparison_summary["revenue_prior"] and row.get("compare_cost_prior") is not None:
        prior_margin_pct = (comparison_summary["profit_prior"] / comparison_summary["revenue_prior"]) * 100.0
    comparison_summary["margin_pct_current"] = current_margin_pct
    comparison_summary["margin_pct_prior"] = prior_margin_pct
    comparison_summary["margin_delta_pp"] = (
        (current_margin_pct - prior_margin_pct)
        if current_margin_pct is not None and prior_margin_pct is not None
        else None
    )

    charts = {
        "trajectory": {
            "grain": str(row.get("traj_grain") or "monthly"),
            "labels": _to_list(row.get("traj_labels")),
            "revenue": _to_list(row.get("traj_revenue")),
            "qty": _to_list(row.get("traj_qty")),
            "orders": _to_list(row.get("traj_orders")),
            "profit": _to_list(row.get("traj_profit")),
            "margin_pct": _to_list(row.get("traj_margin")),
        },
        "pareto": [],
        "movers": [],
        "unit_price_dist": [],
        "segments": {
            "summary": _to_list(row.get("segment_summary")),
            "movers": [],
            "mix_shift": _to_list(row.get("segment_mix_shift")),
        },
        "price_velocity": [],
        "top_products": [],
    }

    velocity = {
        "avg_weekly": _clean_num(row.get("avg_weekly_qty")),
        "weekly_revenue": _clean_num(row.get("weekly_revenue")),
        "active_skus": _clean_int(row.get("active_skus")),
    }

    monthly_series = []
    labels = charts.get("trajectory", {}).get("labels") or []
    rev = charts.get("trajectory", {}).get("revenue") or []
    qty = charts.get("trajectory", {}).get("qty") or []
    orders = charts.get("trajectory", {}).get("orders") or []
    for idx, label in enumerate(labels):
        monthly_series.append(
            {
                "month": label,
                "revenue": _clean_num(rev[idx] if idx < len(rev) else 0),
                "units": _clean_num(qty[idx] if idx < len(qty) else 0),
                "orders": _clean_num(orders[idx] if idx < len(orders) else 0),
            }
        )

    health_matrix = _build_health_matrix(
        _to_list(row.get("health_summary")),
        _to_list(row.get("health_top")),
        velocity_cutoff_low=_safe_float(row.get("health_velocity_p40")),
        velocity_cutoff_high=_safe_float(row.get("health_velocity_p60")),
        profitability_cutoff_low=_safe_float(row.get("health_profitability_p40")),
        profitability_cutoff_high=_safe_float(row.get("health_profitability_p60")),
        profitability_metric="margin_pct_or_contribution_lb",
        total_revenue=kpis.get("revenue") or 0.0,
        total_profit=kpis.get("profit") or 0.0,
    )

    protein_insights = {
        "summary": {
            "family_count": _clean_int(row.get("protein_family_count")),
            "top_family": row.get("top_protein_family"),
            "top_family_share": _safe_float(row.get("top_protein_share")),
            "concentration_hhi": _safe_float(row.get("protein_hhi")),
        },
        "mix": _to_list(row.get("protein_mix")),
        "mix_shift": _to_list(row.get("protein_mix_shift")),
        "margin_watch": _to_list(row.get("protein_margin_watch")),
        "growth_pockets": _to_list(row.get("protein_growth_pockets")),
        "portfolio": [],
        "pricing_opportunities": [],
        "execution_watch": [],
        "leaders": [],
        "narrative": {},
    }

    return {
        "kpis": kpis,
        "comparison_summary": comparison_summary,
        "charts": charts,
        "velocity": velocity,
        "monthly_series": monthly_series,
        "sku_metrics": [],
        "health_matrix": health_matrix,
        "concentration": {
            "top1_share": _safe_float(row.get("concentration_top1_share")),
            "top10_share": _safe_float(row.get("concentration_top10_share")),
            "hhi": _safe_float(row.get("concentration_hhi")),
            "skus_to_80": _clean_int(row.get("concentration_skus_to_80")),
        },
        "risk_opportunity": {
            "below_minimum_count": 0,
            "below_minimum_revenue": 0.0,
            "below_target_count": _clean_int(row.get("risk_below_target_count")),
            "below_target_revenue": _clean_num(row.get("risk_below_target_revenue")),
            "at_or_above_target_count": 0,
            "at_or_above_target_revenue": 0.0,
            "negative_margin_count": _clean_int(row.get("risk_negative_margin_count")),
            "profit_uplift_target": _clean_num(row.get("risk_profit_uplift_target")),
            "high_velocity_low_margin_count": _clean_int(row.get("high_velocity_low_margin_count")),
            "high_margin_low_velocity_count": _clean_int(row.get("high_margin_low_velocity_count")),
            "margin_risk_top": [],
            "high_velocity_low_margin": [],
            "high_margin_low_velocity": [],
        },
        "pricing_guardrails": {
            "high_outlier_count": _clean_int(row.get("high_price_outlier_count")),
            "low_outlier_count": _clean_int(row.get("low_price_outlier_count")),
            "outside_count": _clean_int(row.get("outside_guardrail_count")),
            "outside_pct": _safe_float(row.get("outside_guardrail_pct")),
            "rows": [],
        },
        "execution_lists": {
            "pricing_fixes": [],
            "cost_fixes": [],
            "promote_candidates": [],
        },
        "protein_insights": protein_insights,
        "insights": [
            {
                "metric": "comparison_delta",
                "current": comparison_summary.get("revenue_current"),
                "prev": comparison_summary.get("revenue_prior"),
                "delta_pct": comparison_summary.get("revenue_delta_pct"),
                "label": "Current window vs prior comparable window",
            },
            {
                "metric": "top_product",
                "sku": row.get("top_product_id"),
                "label": row.get("top_product_display_name"),
                "revenue": _clean_num(row.get("top_product_revenue")),
            },
        ],
    }


def _table_payload(
    comparison_where_sql: str,
    comparison_params: List[Any],
    cols: set[str],
    args: Any,
    *,
    current_start: str,
    current_end: str,
    prior_start: str,
    prior_end: str,
) -> Dict[str, Any]:
    date_col = _safe_col(cols, fs.CANON.date, "Date")
    revenue_col = _safe_col(cols, fs.CANON.revenue, "Revenue")
    cost_expr = _coalesce_expr(cols, (fs.CANON.cost, "Cost", "CostPrice"), "NULL")
    qty_expr = _coalesce_expr(
        cols,
        (fs.CANON.qty_units, "ShippedItems", "QuantityOrdered", "Qty", "Quantity", "Units", "ItemCount"),
        "0",
    )
    weight_expr = _coalesce_expr(cols, (fs.CANON.weight_lb, "Weight", "WeightLb", "ShippedLb", "pack_weight_lb_sum"), "0")
    sku_col, prod_id_col, prod_name = _resolve_product_columns(cols)
    order_id = _safe_col(cols, fs.CANON.order_id, "OrderID")
    customer_col = _safe_col(cols, fs.CANON.customer_id, "CustomerID", "CustomerId")
    customer_name_col = _safe_col(cols, fs.CANON.customer_name, "CustomerName", "Name")
    supplier_col = _safe_col(cols, fs.CANON.supplier_name, fs.CANON.supplier_id, "Supplier", "SupplierName", "SupplierId")
    region_col = _safe_col(cols, fs.CANON.region, "Region", "RegionName")

    if not all([date_col, revenue_col, sku_col, prod_name, order_id]):
        return {"rows": [], "page": 1, "page_size": 25, "total": 0}

    try:
        page = max(1, int(args.get("page", 1)))
    except Exception:
        page = 1
    try:
        page_size = int(args.get("page_size") or args.get("per_page") or 25)
    except Exception:
        page_size = 25
    page_size = max(1, min(page_size, 200))

    search = (args.get("search") or args.get("q") or "").strip()
    sort_raw = (args.get("sort") or args.get("sort_by") or "revenue").lower()
    sort_dir_raw = (args.get("sort_dir") or args.get("direction") or "desc").lower()
    segments = _parse_segments(args.get("segments") or args.get("segment"))
    quick_filters = _parse_segments(args.get("quick_filters") or args.get("quick_filter"))

    sort_map = {
        "sku": "sku",
        "product_id": "product_id",
        "protein_family": "protein_family",
        "category": "category",
        "revenue": "revenue",
        "revenue_current": "revenue_current",
        "revenue_prior": "revenue_prior",
        "revenue_delta": "revenue_delta",
        "revenue_delta_pct": "revenue_delta_pct",
        "qty": "qty",
        "weight": "weight",
        "profit": "profit",
        "profit_current": "profit_current",
        "profit_prior": "profit_prior",
        "profit_delta": "profit_delta",
        "profit_share": "profit_share",
        "margin_pct": "margin_pct",
        "margin_pct_prior": "margin_pct_prior",
        "margin_delta_pp": "margin_delta_pp",
        "unit_price": "unit_price",
        "current_unit_price": "unit_price",
        "minimum_price": "minimum_price",
        "target_price": "target_price",
        "uplift_pct": "uplift_pct",
        "contribution_margin_lb": "contribution_margin_lb",
        "unit_cost": "unit_cost",
        "minimum_margin_pct": "minimum_margin_pct",
        "target_margin_pct": "target_margin_pct",
        "supplier_count": "supplier_count",
        "customer_count": "customer_count",
        "region_breadth": "region_breadth",
        "top_customer_share": "top_customer_share",
        "customer_hhi": "customer_hhi",
        "price_variance_vs_median": "price_variance_vs_median",
        "volatility_score": "volatility_score",
        "velocity_per_month": "velocity_per_month",
        "revenue_share": "revenue_share",
        "qty_share": "qty_share",
        "orders": "orders",
        "orders_current": "orders_current",
        "orders_prior": "orders_prior",
        "last_sold": "last_sold",
    }
    sort_col = sort_map.get(sort_raw, "revenue")
    sort_dir = "ASC" if sort_dir_raw in {"asc", "ascending", "up", "1"} else "DESC"

    where_parts: List[str] = ["1=1"]
    filter_sql_params: List[Any] = []
    if search:
        where_parts.append("(product_name ILIKE ? OR product_id ILIKE ? OR protein_family ILIKE ? OR category ILIKE ?)")
        like = f"%{search}%"
        filter_sql_params.extend([like, like, like, like])

    if segments:
        placeholders = ", ".join("?" for _ in segments)
        where_parts.append(f"segment IN ({placeholders})")
        filter_sql_params.extend(segments)

    if "below_target_margin" in quick_filters:
        where_parts.append("(margin_pct IS NOT NULL AND target_margin_pct IS NOT NULL AND margin_pct < target_margin_pct)")
    if "below_minimum_margin" in quick_filters:
        where_parts.append("(margin_pct IS NOT NULL AND minimum_margin_pct IS NOT NULL AND margin_pct < minimum_margin_pct)")
    if "negative_margin" in quick_filters:
        where_parts.append("(profit IS NOT NULL AND profit < 0)")
    if "high_velocity" in quick_filters:
        where_parts.append("(velocity_per_month IS NOT NULL AND velocity_per_month >= vel_p75)")
    if "top_revenue_20" in quick_filters:
        where_parts.append("(revenue >= rev_p80)")
    if "high_revenue_share" in quick_filters:
        where_parts.append("(revenue >= rev_p90)")
    if "high_price_outlier" in quick_filters:
        where_parts.append("(unit_price IS NOT NULL AND median_unit_price IS NOT NULL AND ABS(unit_price - median_unit_price) / NULLIF(median_unit_price, 0) >= 0.35)")
    if "missing_cost" in quick_filters:
        where_parts.append("(cost IS NULL)")
    if "elastic_risk" in quick_filters:
        where_parts.append(
            "(margin_pct IS NOT NULL AND minimum_margin_pct IS NOT NULL AND margin_pct >= minimum_margin_pct "
            "AND unit_price IS NOT NULL AND median_unit_price IS NOT NULL AND unit_price >= median_unit_price * 1.08 "
            "AND revenue_delta_pct IS NOT NULL AND revenue_delta_pct <= -6)"
        )
    if "high_customer_dependency" in quick_filters:
        where_parts.append("(top_customer_share IS NOT NULL AND top_customer_share >= 50)")
    if "outside_guardrail" in quick_filters:
        where_parts.append("(unit_price IS NOT NULL AND median_unit_price IS NOT NULL AND ABS(unit_price - median_unit_price) / NULLIF(median_unit_price, 0) >= 0.25)")
    if "protect_core" in quick_filters:
        where_parts.append("(velocity_per_month IS NOT NULL AND velocity_per_month >= vel_p75 AND revenue >= rev_p80 AND margin_pct IS NOT NULL AND target_margin_pct IS NOT NULL AND margin_pct >= target_margin_pct)")
    if "recover_margin" in quick_filters:
        where_parts.append("(velocity_per_month IS NOT NULL AND velocity_per_month >= vel_p75 AND margin_pct IS NOT NULL AND target_margin_pct IS NOT NULL AND margin_pct < target_margin_pct)")
    if "at_or_above_target" in quick_filters:
        where_parts.append("(margin_pct IS NOT NULL AND target_margin_pct IS NOT NULL AND margin_pct >= target_margin_pct)")
    if "promote_candidate" in quick_filters:
        where_parts.append("(velocity_per_month IS NOT NULL AND velocity_per_month < vel_p75 AND margin_pct IS NOT NULL AND target_margin_pct IS NOT NULL AND margin_pct >= target_margin_pct)")
    if "rationalize_candidate" in quick_filters:
        where_parts.append("(velocity_per_month IS NOT NULL AND velocity_per_month < vel_p75 AND (margin_pct IS NULL OR target_margin_pct IS NULL OR margin_pct < target_margin_pct))")

    where_clause = " AND ".join(where_parts)
    offset = (page - 1) * page_size

    unit_price_expr = """
        CASE
            WHEN weight > 0 THEN revenue / NULLIF(weight, 0)
            WHEN qty > 0 THEN revenue / NULLIF(qty, 0)
            ELSE NULL
        END
    """
    unit_cost_expr = """
        CASE
            WHEN weight > 0 AND cost IS NOT NULL THEN cost / NULLIF(weight, 0)
            WHEN qty > 0 AND cost IS NOT NULL THEN cost / NULLIF(qty, 0)
            ELSE NULL
        END
    """
    basis_qty_expr = """
        CASE
            WHEN weight > 0 THEN weight
            WHEN qty > 0 THEN qty
            ELSE 0
        END
    """
    prior_basis_qty_expr = """
        CASE
            WHEN weight_prior > 0 THEN weight_prior
            WHEN qty_prior > 0 THEN qty_prior
            ELSE 0
        END
    """
    effective_unit_cost_expr = margin_rules.sql_effective_unit_cost_expr("base_unit_cost", fallback="NULL")
    effective_cost_expr = margin_rules.sql_effective_cost_expr("base_cost", "weight", "qty", fallback="NULL")
    effective_cost_prior_expr = margin_rules.sql_effective_cost_expr("base_cost_prior", "weight_prior", "qty_prior", fallback="NULL")
    effective_profit_expr = f"""
        CASE
            WHEN ({effective_cost_expr}) IS NOT NULL THEN revenue - ({effective_cost_expr})
            ELSE NULL
        END
    """
    effective_profit_prior_expr = f"""
        CASE
            WHEN ({effective_cost_prior_expr}) IS NOT NULL THEN revenue_prior - ({effective_cost_prior_expr})
            ELSE NULL
        END
    """
    effective_margin_expr = f"""
        CASE
            WHEN revenue > 0 AND ({effective_cost_expr}) IS NOT NULL THEN ({effective_profit_expr}) / revenue * 100
            ELSE NULL
        END
    """
    effective_margin_prior_expr = f"""
        CASE
            WHEN revenue_prior > 0 AND ({effective_cost_prior_expr}) IS NOT NULL THEN ({effective_profit_prior_expr}) / revenue_prior * 100
            ELSE NULL
        END
    """
    effective_minimum_price_expr = margin_rules.sql_price_from_cost_expr(
        f"({effective_unit_cost_expr})",
        "minimum_margin_pct",
        fallback="NULL",
    )
    effective_target_price_expr = margin_rules.sql_price_from_cost_expr(
        f"({effective_unit_cost_expr})",
        "target_margin_pct",
        fallback="NULL",
    )
    effective_uplift_pct_expr = margin_rules.sql_price_uplift_pct_expr(
        unit_price_expr,
        f"({effective_target_price_expr})",
        fallback="NULL",
    )

    exprs = _product_exprs(cols)
    family_exprs = _family_exprs(cols)
    sku_expr = exprs["sku_expr"]
    product_key_expr = exprs["product_key_expr"]
    product_name_expr = exprs["product_name_expr"]
    display_name_expr = exprs["display_name_expr"]
    protein_expr = family_exprs["protein_expr"]
    category_expr = family_exprs["category_expr"]
    target_margin_pct_expr = margin_rules.sql_margin_rule_expr("rollup.protein_family", "rollup.category", "target_gross_margin_pct")
    minimum_margin_pct_expr = margin_rules.sql_margin_rule_expr("rollup.protein_family", "rollup.category", "min_gross_margin_pct")
    target_product_margin_pct_expr = margin_rules.sql_margin_rule_expr("rollup.protein_family", "rollup.category", "target_product_margin_pct")
    minimum_product_margin_pct_expr = margin_rules.sql_margin_rule_expr("rollup.protein_family", "rollup.category", "min_product_margin_pct")
    rule_family_expr = margin_rules.sql_rule_family_expr("rollup.protein_family", "rollup.category")
    customer_expr = f"{customer_col}" if customer_col else "NULL"
    customer_name_expr = (
        f"COALESCE({customer_name_col}, {customer_col})"
        if customer_name_col and customer_col
        else (f"{customer_name_col}" if customer_name_col else (f"{customer_col}" if customer_col else "NULL"))
    )
    supplier_expr = f"{supplier_col}" if supplier_col else "NULL"
    region_expr = f"{region_col}" if region_col else "NULL"

    sql = f"""
        WITH base AS (
            SELECT
                {date_col}::DATE AS date,
                {product_key_expr} AS product_id,
                {sku_expr} AS sku,
                {product_name_expr} AS product_name,
                {display_name_expr} AS display_name,
                {protein_expr} AS protein_family,
                {category_expr} AS category,
                {customer_expr}::VARCHAR AS customer_id,
                {customer_name_expr}::VARCHAR AS customer_name,
                {supplier_expr}::VARCHAR AS supplier_name,
                {region_expr}::VARCHAR AS region_name,
                CAST({revenue_col} AS DOUBLE) AS revenue,
                CAST({cost_expr} AS DOUBLE) AS cost,
                CAST({qty_expr} AS DOUBLE) AS qty,
                CAST({weight_expr} AS DOUBLE) AS weight,
                {order_id}::VARCHAR AS order_id
            FROM fact
            WHERE {comparison_where_sql}
        ),
        bounds AS (
            SELECT
                ?::DATE AS current_start,
                ?::DATE AS current_end,
                ?::DATE AS prior_start,
                ?::DATE AS prior_end
        ),
        current_base AS (
            SELECT b0.*
            FROM base b0
            CROSS JOIN bounds wb
            WHERE b0.date BETWEEN wb.current_start AND wb.current_end
        ),
        rollup AS (
            SELECT
                product_id,
                any_value(product_name) AS product_name,
                any_value(display_name) AS display_name,
                any_value(sku) AS sku,
                any_value(supplier_name) AS supplier_name,
                any_value(protein_family) AS protein_family,
                any_value(category) AS category,
                MIN(CASE WHEN date BETWEEN wb.current_start AND wb.current_end THEN date END) AS first_sold,
                MAX(CASE WHEN date BETWEEN wb.current_start AND wb.current_end THEN date END) AS last_sold,
                SUM(CASE WHEN date BETWEEN wb.current_start AND wb.current_end THEN revenue ELSE 0 END) AS revenue,
                SUM(CASE WHEN date BETWEEN wb.current_start AND wb.current_end THEN cost ELSE NULL END) AS cost,
                CASE
                    WHEN SUM(CASE WHEN date BETWEEN wb.current_start AND wb.current_end THEN cost ELSE NULL END) IS NULL THEN NULL
                    ELSE SUM(CASE WHEN date BETWEEN wb.current_start AND wb.current_end THEN revenue ELSE 0 END) - SUM(CASE WHEN date BETWEEN wb.current_start AND wb.current_end THEN cost ELSE NULL END)
                END AS profit,
                SUM(CASE WHEN date BETWEEN wb.current_start AND wb.current_end THEN qty ELSE 0 END) AS qty,
                SUM(CASE WHEN date BETWEEN wb.current_start AND wb.current_end THEN weight ELSE 0 END) AS weight,
                SUM(CASE WHEN date BETWEEN wb.prior_start AND wb.prior_end THEN qty ELSE 0 END) AS qty_prior,
                SUM(CASE WHEN date BETWEEN wb.prior_start AND wb.prior_end THEN weight ELSE 0 END) AS weight_prior,
                COUNT(DISTINCT CASE WHEN date BETWEEN wb.current_start AND wb.current_end THEN order_id END) AS orders,
                COUNT(DISTINCT CASE WHEN date BETWEEN wb.current_start AND wb.current_end THEN customer_id END) AS customer_count,
                COUNT(DISTINCT CASE WHEN date BETWEEN wb.current_start AND wb.current_end THEN supplier_name END) AS supplier_count,
                COUNT(DISTINCT CASE WHEN date BETWEEN wb.current_start AND wb.current_end THEN region_name END) AS region_breadth,
                SUM(CASE WHEN date BETWEEN wb.current_start AND wb.current_end THEN revenue ELSE 0 END) AS revenue_current,
                SUM(CASE WHEN date BETWEEN wb.prior_start AND wb.prior_end THEN revenue ELSE 0 END) AS revenue_prior,
                SUM(CASE WHEN date BETWEEN wb.current_start AND wb.current_end THEN cost ELSE NULL END) AS cost_current,
                SUM(CASE WHEN date BETWEEN wb.prior_start AND wb.prior_end THEN cost ELSE NULL END) AS cost_prior,
                SUM(CASE WHEN date BETWEEN wb.current_start AND wb.current_end AND cost IS NOT NULL THEN revenue - cost ELSE NULL END) AS profit_current,
                SUM(CASE WHEN date BETWEEN wb.prior_start AND wb.prior_end AND cost IS NOT NULL THEN revenue - cost ELSE NULL END) AS profit_prior,
                COUNT(DISTINCT CASE WHEN date BETWEEN wb.current_start AND wb.current_end THEN order_id END) AS orders_current,
                COUNT(DISTINCT CASE WHEN date BETWEEN wb.prior_start AND wb.prior_end THEN order_id END) AS orders_prior,
                COUNT(*) AS rows,
                COUNT(DISTINCT CASE WHEN date BETWEEN wb.current_start AND wb.current_end THEN DATE_TRUNC('month', date) END) AS months_active
            FROM base
            CROSS JOIN bounds wb
            GROUP BY product_id
        ),
        monthly_revenue AS (
            SELECT
                product_id,
                DATE_TRUNC('month', date) AS month_bucket,
                SUM(revenue) AS month_revenue
            FROM current_base
            GROUP BY product_id, month_bucket
        ),
        volatility AS (
            SELECT
                product_id,
                CASE
                    WHEN COUNT(*) >= 2 AND AVG(month_revenue) > 0 THEN STDDEV_SAMP(month_revenue) / NULLIF(AVG(month_revenue), 0) * 100
                    ELSE NULL
                END AS volatility_score
            FROM monthly_revenue
            GROUP BY product_id
        ),
        product_customer AS (
            SELECT
                product_id,
                customer_id,
                any_value(customer_name) AS customer_name,
                SUM(revenue) AS customer_revenue
            FROM current_base
            GROUP BY product_id, customer_id
        ),
        product_customer_totals AS (
            SELECT
                product_id,
                SUM(customer_revenue) AS total_revenue
            FROM product_customer
            GROUP BY product_id
        ),
        product_customer_share AS (
            SELECT
                pc.product_id,
                pc.customer_id,
                COALESCE(NULLIF(pc.customer_name, ''), pc.customer_id, 'Unknown') AS customer_name,
                pc.customer_revenue,
                CASE
                    WHEN pct.total_revenue > 0 THEN pc.customer_revenue / NULLIF(pct.total_revenue, 0)
                    ELSE 0
                END AS customer_revenue_ratio,
                CASE
                    WHEN pct.total_revenue > 0 THEN pc.customer_revenue / NULLIF(pct.total_revenue, 0) * 100
                    ELSE NULL
                END AS customer_revenue_share,
                ROW_NUMBER() OVER (
                    PARTITION BY pc.product_id
                    ORDER BY pc.customer_revenue DESC, COALESCE(NULLIF(pc.customer_name, ''), pc.customer_id, 'Unknown')
                ) AS rn
            FROM product_customer pc
            LEFT JOIN product_customer_totals pct ON pct.product_id = pc.product_id
        ),
        customer_concentration AS (
            SELECT
                product_id,
                MAX(CASE WHEN rn = 1 THEN customer_name END) AS top_customer_name,
                MAX(CASE WHEN rn = 1 THEN customer_revenue_share END) AS top_customer_share,
                SUM(POWER(COALESCE(customer_revenue_ratio, 0), 2)) * 10000 AS customer_hhi
            FROM product_customer_share
            GROUP BY product_id
        ),
        product_region AS (
            SELECT
                product_id,
                COALESCE(NULLIF(region_name, ''), 'Unassigned') AS region_name,
                SUM(revenue) AS region_revenue
            FROM current_base
            GROUP BY product_id, region_name
        ),
        product_region_totals AS (
            SELECT
                product_id,
                SUM(region_revenue) AS total_revenue
            FROM product_region
            GROUP BY product_id
        ),
        product_region_share AS (
            SELECT
                pr.product_id,
                pr.region_name,
                CASE
                    WHEN prt.total_revenue > 0 THEN pr.region_revenue / NULLIF(prt.total_revenue, 0) * 100
                    ELSE NULL
                END AS region_revenue_share,
                ROW_NUMBER() OVER (
                    PARTITION BY pr.product_id
                    ORDER BY pr.region_revenue DESC, pr.region_name
                ) AS rn
            FROM product_region pr
            LEFT JOIN product_region_totals prt ON prt.product_id = pr.product_id
        ),
        region_concentration AS (
            SELECT
                product_id,
                MAX(CASE WHEN rn = 1 THEN region_name END) AS top_region_name,
                MAX(CASE WHEN rn = 1 THEN region_revenue_share END) AS top_region_share
            FROM product_region_share
            GROUP BY product_id
        ),
        with_metrics_base AS (
            SELECT
                rollup.*,
                CASE WHEN revenue > 0 AND cost IS NOT NULL THEN profit / revenue * 100 ELSE NULL END AS margin_pct,
                CASE WHEN revenue_current > 0 AND cost_current IS NOT NULL THEN profit_current / revenue_current * 100 ELSE NULL END AS margin_pct_current,
                CASE WHEN revenue_prior > 0 AND cost_prior IS NOT NULL THEN profit_prior / revenue_prior * 100 ELSE NULL END AS margin_pct_prior,
                revenue_current - revenue_prior AS revenue_delta,
                CASE WHEN revenue_prior > 0 THEN (revenue_current - revenue_prior) / revenue_prior * 100 ELSE NULL END AS revenue_delta_pct,
                CASE WHEN revenue_prior > 0 AND revenue_prior < {LOW_BASE_REVENUE} THEN TRUE ELSE FALSE END AS revenue_low_base,
                CASE WHEN profit_current IS NOT NULL AND profit_prior IS NOT NULL THEN profit_current - profit_prior ELSE NULL END AS profit_delta,
                CASE WHEN revenue_current > 0 AND cost_current IS NOT NULL THEN (profit_current / revenue_current * 100) - (CASE WHEN revenue_prior > 0 AND cost_prior IS NOT NULL THEN profit_prior / revenue_prior * 100 ELSE NULL END) ELSE NULL END AS margin_delta_pp,
                {unit_price_expr} AS unit_price,
                {unit_cost_expr} AS unit_cost,
                cost AS base_cost,
                cost_current AS base_cost_current,
                cost_prior AS base_cost_prior,
                {unit_cost_expr} AS base_unit_cost,
                {rule_family_expr} AS rule_family,
                {minimum_margin_pct_expr} AS minimum_margin_pct,
                {target_margin_pct_expr} AS target_margin_pct,
                {minimum_product_margin_pct_expr} AS min_product_margin_pct,
                {target_product_margin_pct_expr} AS target_product_margin_pct,
                {margin_rules.sql_price_from_cost_expr(unit_cost_expr, minimum_margin_pct_expr, fallback="NULL")} AS minimum_price,
                {margin_rules.sql_price_from_cost_expr(unit_cost_expr, target_margin_pct_expr, fallback="NULL")} AS target_price,
                {margin_rules.sql_price_uplift_pct_expr(unit_price_expr, margin_rules.sql_price_from_cost_expr(unit_cost_expr, target_margin_pct_expr, fallback="NULL"), fallback="NULL")} AS uplift_pct,
                CASE WHEN months_active > 0 THEN orders / NULLIF(months_active, 0) ELSE NULL END AS velocity_per_month,
                volatility.volatility_score,
                customer_concentration.top_customer_name,
                customer_concentration.top_customer_share,
                customer_concentration.customer_hhi,
                region_concentration.top_region_name,
                region_concentration.top_region_share
            FROM rollup
            LEFT JOIN volatility ON volatility.product_id = rollup.product_id
            LEFT JOIN customer_concentration ON customer_concentration.product_id = rollup.product_id
            LEFT JOIN region_concentration ON region_concentration.product_id = rollup.product_id
        ),
        with_metrics AS (
            SELECT
                * REPLACE(
                    ({effective_cost_expr}) AS cost,
                    ({effective_cost_expr}) AS cost_current,
                    ({effective_cost_prior_expr}) AS cost_prior,
                    ({effective_profit_expr}) AS profit,
                    ({effective_profit_expr}) AS profit_current,
                    ({effective_profit_prior_expr}) AS profit_prior,
                    CASE
                        WHEN ({effective_profit_expr}) IS NOT NULL AND ({effective_profit_prior_expr}) IS NOT NULL
                            THEN ({effective_profit_expr}) - ({effective_profit_prior_expr})
                        ELSE NULL
                    END AS profit_delta,
                    ({effective_margin_expr}) AS margin_pct,
                    ({effective_margin_expr}) AS margin_pct_current,
                    ({effective_margin_prior_expr}) AS margin_pct_prior,
                    CASE
                        WHEN ({effective_margin_expr}) IS NOT NULL AND ({effective_margin_prior_expr}) IS NOT NULL
                            THEN ({effective_margin_expr}) - ({effective_margin_prior_expr})
                        ELSE NULL
                    END AS margin_delta_pp,
                    ({effective_unit_cost_expr}) AS unit_cost,
                    ({effective_minimum_price_expr}) AS minimum_price,
                    ({effective_target_price_expr}) AS target_price,
                    ({effective_uplift_pct_expr}) AS uplift_pct
                )
            FROM with_metrics_base
        ),
        with_segment AS (
            SELECT
                *,
                COALESCE(quantile_cont(revenue, 0.80) OVER (), 0) AS rev_p80,
                COALESCE(quantile_cont(revenue, 0.90) OVER (), 0) AS rev_p90,
                COALESCE(quantile_cont(orders, 0.60) OVER (), 0) AS ord_p60,
                COALESCE(quantile_cont(velocity_per_month, 0.75) OVER (), 0) AS vel_p75,
                quantile_cont(unit_price, 0.50) OVER () AS median_unit_price,
                CASE
                    WHEN revenue >= rev_p80 AND orders >= ord_p60 THEN 'Stars'
                    WHEN revenue >= rev_p80 THEN 'Cash Cows'
                    WHEN orders >= ord_p60 THEN 'Volume Drivers'
                    WHEN margin_pct IS NOT NULL AND margin_pct < 5 THEN 'Margin Risk'
                    ELSE 'Long Tail'
                END AS segment
            FROM with_metrics
        ),
        filtered AS (
            SELECT
                *,
                CASE WHEN weight > 0 AND profit IS NOT NULL THEN profit / NULLIF(weight, 0) ELSE NULL END AS contribution_margin_lb,
                CASE WHEN median_unit_price IS NOT NULL AND unit_price IS NOT NULL THEN unit_price - median_unit_price ELSE NULL END AS price_variance_vs_median
            FROM with_segment
            WHERE {where_clause}
        ),
        ranked AS (
            SELECT
                *,
                COUNT(*) OVER () AS total_rows,
                SUM(revenue) OVER () AS total_revenue,
                SUM(qty) OVER () AS total_qty,
                CASE
                    WHEN ABS(SUM(COALESCE(profit, 0)) OVER ()) > 0 AND profit IS NOT NULL
                        THEN profit / NULLIF(SUM(COALESCE(profit, 0)) OVER (), 0) * 100
                    ELSE NULL
                END AS profit_share,
                CASE WHEN SUM(revenue) OVER () > 0 THEN revenue / SUM(revenue) OVER () * 100 ELSE NULL END AS revenue_share,
                CASE WHEN SUM(qty) OVER () > 0 THEN qty / SUM(qty) OVER () * 100 ELSE NULL END AS qty_share
            FROM filtered
        )
        SELECT *
        FROM ranked
        ORDER BY {sort_col} {sort_dir}
        LIMIT ? OFFSET ?
    """
    sql_params = list(comparison_params) + [
        current_start,
        current_end,
        prior_start,
        prior_end,
    ] + filter_sql_params + [page_size, offset]

    df = fact_store.execute_sql_df(
        sql,
        sql_params,
        tag="products.table_bundle",
        cache_key="products.table_bundle",
    )
    rows: List[Dict[str, Any]] = []
    total = 0
    if not df.empty:
        total = int(df.iloc[0].get("total_rows", len(df)) or 0)
        for _, r in df.iterrows():
            def _num(field: str) -> float | None:
                val = r.get(field)
                if val is None:
                    return None
                try:
                    f = float(val)
                    return None if math.isnan(f) else f
                except Exception:
                    return None

            product_id = r.get("product_id")
            pid_safe = _encode_path_segment(product_id)
            margin_pct = _num("margin_pct")
            profit = _num("profit")
            unit_price = _num("unit_price")
            rows.append(
                {
                    "key": product_id,
                    "product_id": product_id,
                    "sku": r.get("sku") or product_id,
                    "product_name": r.get("product_name"),
                    "label": r.get("display_name") or r.get("product_name"),
                    "display_name": r.get("display_name") or r.get("product_name"),
                    "protein_family": r.get("protein_family"),
                    "protein_type": r.get("protein_family"),
                    "protein_name": r.get("protein_family"),
                    "category": r.get("category"),
                    "product_category": r.get("category"),
                    "segment": r.get("segment"),
                    "supplier": r.get("supplier_name"),
                    "supplier_count": int(r.get("supplier_count") or 0),
                    "customer_count": int(r.get("customer_count") or 0),
                    "region_breadth": int(r.get("region_breadth") or 0),
                    "revenue": _clean_num(r.get("revenue")),
                    "revenue_current": _clean_num(r.get("revenue_current")),
                    "revenue_prior": _clean_num(r.get("revenue_prior")),
                    "revenue_delta": _num("revenue_delta"),
                    "revenue_delta_pct": _num("revenue_delta_pct"),
                    "revenue_low_base": bool(r.get("revenue_low_base")),
                    "revenue_share": _num("revenue_share"),
                    "weight": _clean_num(r.get("weight")),
                    "orders": int(r.get("orders") or 0),
                    "orders_current": int(r.get("orders_current") or 0),
                    "orders_prior": int(r.get("orders_prior") or 0),
                    "velocity_per_month": _num("velocity_per_month"),
                    "qty": _clean_num(r.get("qty")),
                    "qty_share": _num("qty_share"),
                    "unit_price": unit_price,
                    "current_unit_price": unit_price,
                    "unit_cost": _num("unit_cost"),
                    "base_unit_cost": _num("base_unit_cost"),
                    "effective_unit_cost": _num("unit_cost"),
                    "minimum_price": _num("minimum_price"),
                    "target_price": _num("target_price"),
                    "uplift_pct": _num("uplift_pct"),
                    "cost": _num("cost"),
                    "base_cost": _num("base_cost"),
                    "effective_cost": _num("cost"),
                    "profit": profit,
                    "profit_current": _num("profit_current"),
                    "profit_prior": _num("profit_prior"),
                    "profit_delta": _num("profit_delta"),
                    "profit_share": _num("profit_share"),
                    "margin_pct": margin_pct,
                    "minimum_margin_pct": _num("minimum_margin_pct"),
                    "target_margin_pct": _num("target_margin_pct"),
                    "min_product_margin_pct": _num("min_product_margin_pct"),
                    "target_product_margin_pct": _num("target_product_margin_pct"),
                    "rule_family": r.get("rule_family"),
                    "margin_pct_current": _num("margin_pct_current"),
                    "margin_pct_prior": _num("margin_pct_prior"),
                    "margin_delta_pp": _num("margin_delta_pp"),
                    "margin": profit,
                    "contribution_margin_lb": _num("contribution_margin_lb"),
                    "asp_lb": unit_price,
                    "base_cost_lb": _num("base_unit_cost"),
                    "effective_cost_lb": _num("unit_cost"),
                    "cost_lb": _num("unit_cost"),
                    "contribution_lb": _num("contribution_margin_lb"),
                    "top_customer_name": r.get("top_customer_name"),
                    "top_customer_share": _num("top_customer_share"),
                    "customer_hhi": _num("customer_hhi"),
                    "top_region_name": r.get("top_region_name"),
                    "top_region_share": _num("top_region_share"),
                    "price_variance_vs_median": _num("price_variance_vs_median"),
                    "volatility_score": _num("volatility_score"),
                    "margin_risk": _margin_risk_label(
                        margin_pct,
                        _num("target_margin_pct"),
                        _num("minimum_margin_pct"),
                    ),
                    "first_sold": str(r.get("first_sold")) if r.get("first_sold") is not None else None,
                    "last_sold": str(r.get("last_sold")) if r.get("last_sold") is not None else None,
                    "recommendation": None,
                    "quick_rec": None,
                    "intel_url": f"/products/{pid_safe}/drilldown" if pid_safe else None,
                }
            )

    return {
        "rows": rows,
        "page": page,
        "page_size": page_size,
        "total": total,
        "sort_by": sort_col,
        "sort_dir": sort_dir.lower(),
        "search": search,
        "segments": segments,
        "quick_filters": quick_filters,
    }


def _empty_products_metrics() -> Dict[str, Any]:
    return {
        "kpis": {
            "revenue": 0.0,
            "qty": 0.0,
            "weight": 0.0,
            "products": 0,
            "customers": 0,
            "orders": 0,
            "profit": 0.0,
            "margin_pct": None,
            "avg_price": None,
            "median_price": None,
            "unit_price_p10": None,
            "unit_price_p50": None,
            "unit_price_p90": None,
            "revenue_per_product": None,
            "revenue_per_customer": None,
        },
        "charts": {
            "trajectory": {"grain": "monthly", "labels": [], "revenue": [], "qty": [], "profit": [], "margin_pct": []},
            "pareto": [],
            "movers": [],
            "unit_price_dist": [],
            "segments": {"summary": [], "movers": [], "mix_shift": []},
            "price_velocity": [],
            "top_products": [],
        },
        "velocity": {"avg_weekly": 0.0, "weekly_revenue": 0.0, "active_skus": 0},
        "monthly_series": [],
        "sku_metrics": [],
        "pricing_guardrails": {"high_outlier_count": 0, "low_outlier_count": 0, "outside_count": 0, "outside_pct": None, "rows": []},
        "execution_lists": {"pricing_fixes": [], "cost_fixes": [], "promote_candidates": []},
        "protein_insights": {
            "summary": {"family_count": 0, "top_family": None, "top_family_share": None, "concentration_hhi": None},
            "mix": [],
            "mix_shift": [],
            "margin_watch": [],
            "growth_pockets": [],
            "portfolio": [],
            "pricing_opportunities": [],
            "execution_watch": [],
            "leaders": [],
            "narrative": {},
        },
    }


def _empty_products_table(args: Any) -> Dict[str, Any]:
    try:
        page = max(1, int(args.get("page", 1)))
    except Exception:
        page = 1
    try:
        page_size = int(args.get("page_size") or args.get("per_page") or 25)
    except Exception:
        page_size = 25
    page_size = max(1, min(page_size, 200))
    return {
        "rows": [],
        "page": page,
        "page_size": page_size,
        "total": 0,
        "sort_by": (args.get("sort") or args.get("sort_by") or "revenue"),
        "sort_dir": str(args.get("sort_dir") or args.get("direction") or "desc").lower(),
        "search": (args.get("search") or args.get("q") or "").strip() if hasattr(args, "get") else "",
        "segments": _parse_segments(args.get("segments") or args.get("segment")) if hasattr(args, "get") else [],
        "quick_filters": _parse_segments(args.get("quick_filters") or args.get("quick_filter")) if hasattr(args, "get") else [],
    }


def build_products_bundle(
    filters: Any,
    scope: Dict[str, Any],
    args: Any,
    *,
    requested_sections: Sequence[str] | None = None,
) -> Dict[str, Any]:
    started = time.perf_counter()
    cols = fact_store.list_columns()
    where_sql, where_params, start_iso, end_iso = fact_store.build_where_clause(filters, cols, scope, apply_default_window=True)
    comparison = _build_comparison_window(start_iso, end_iso)
    comparison_filters = _with_window(
        filters,
        start=_coerce_date(comparison.get("history_start")),
        end=_coerce_date(comparison.get("current_end")),
    )
    comparison_where_sql, comparison_where_params, _, _ = fact_store.build_where_clause(
        comparison_filters,
        cols,
        scope,
        apply_default_window=True,
    )
    try:
        end_dt = datetime.fromisoformat(str(comparison.get("current_end"))) if comparison.get("current_end") else datetime.utcnow()
    except Exception:
        end_dt = datetime.utcnow()
    requested = {
        str(section or "").strip().lower().replace("-", "_")
        for section in (requested_sections or _requested_product_sections(args) or ())
        if str(section or "").strip()
    }
    requested = {section for section in requested if section in PRODUCT_ALL_SECTIONS}
    full_bundle = not requested
    want_summary = full_bundle or bool(requested & PRODUCT_SUMMARY_SECTIONS)
    want_detail = full_bundle or bool(requested & PRODUCT_DETAIL_SECTIONS)
    want_table = full_bundle or bool(requested & PRODUCT_TABLE_SECTIONS)
    bundle_mode = "full" if full_bundle else (
        "table"
        if requested == PRODUCT_TABLE_SECTIONS
        else "summary"
        if requested and requested.issubset(PRODUCT_SUMMARY_SECTIONS)
        else "detail"
        if requested and requested.isdisjoint(PRODUCT_TABLE_SECTIONS)
        else "hybrid"
    )
    try:
        bubble_top_n = int((args.get("bubble_top_n") if hasattr(args, "get") else None) or 250)
    except Exception:
        bubble_top_n = 250
    bubble_top_n = max(50, min(bubble_top_n, 5000))
    try:
        movers_limit = int((args.get("movers_limit") if hasattr(args, "get") else None) or 20)
    except Exception:
        movers_limit = 20
    movers_limit = max(10, min(movers_limit, 500))
    recent_start = str(comparison.get("current_start") or start_iso or end_dt.date().isoformat())
    recent_end = str(comparison.get("current_end") or end_dt.date().isoformat())
    prior_start = str(comparison.get("prior_start") or recent_start)
    prior_end = str(comparison.get("prior_end") or recent_end)

    metrics: Dict[str, Any]
    if want_detail:
        metrics = _metrics_and_charts(
            comparison_where_sql,
            comparison_where_params,
            cols,
            current_start=recent_start,
            current_end=recent_end,
            recent_start=recent_start,
            recent_end=recent_end,
            prior_start=prior_start,
            prior_end=prior_end,
            price_velocity_limit=bubble_top_n,
            movers_limit=movers_limit,
        )
    elif want_summary:
        metrics = _summary_metrics_and_context(
            comparison_where_sql,
            comparison_where_params,
            cols,
            current_start=recent_start,
            current_end=recent_end,
            recent_start=recent_start,
            recent_end=recent_end,
            prior_start=prior_start,
            prior_end=prior_end,
        )
    else:
        metrics = {}
    table = (
        _table_payload(
            comparison_where_sql,
            comparison_where_params,
            cols,
            args,
            current_start=recent_start,
            current_end=recent_end,
            prior_start=prior_start,
            prior_end=prior_end,
        )
        if want_table
        else _empty_products_table(args)
    )

    if isinstance(metrics, dict) and metrics.get("error"):
        return {
            **metrics,
            "table": table,
            "comparison": comparison,
            "meta": {
                "page_id": "products",
                "window": {"start": comparison.get("current_start"), "end": comparison.get("current_end")},
                "sections": sorted(requested),
                "bundle_mode": bundle_mode,
            },
        }

    if not metrics or not isinstance(metrics, dict) or not metrics.get("kpis"):
        metrics = _empty_products_metrics()

    meta = {"page_id": "products", "sections": sorted(requested), "bundle_mode": bundle_mode}
    if comparison.get("current_start") or comparison.get("current_end"):
        meta["window"] = {"start": comparison.get("current_start"), "end": comparison.get("current_end")}
    if start_iso or end_iso:
        meta["window_exclusive"] = {"start": start_iso, "end": end_iso}

    duration_ms = int((time.perf_counter() - started) * 1000)
    meta["duration_ms"] = duration_ms
    try:
        from flask import has_request_context, g  # type: ignore

        if has_request_context():
            stats = getattr(g, "_duckdb_stats", None)
            if stats:
                meta["duckdb_query_count"] = int(stats.get("count", 0))
                meta["duckdb_ms"] = int(stats.get("total_ms", 0))
    except Exception:
        meta.setdefault("duckdb_query_count", None)

    payload = {**metrics, "table": table, "comparison": comparison, "meta": meta}
    payload.setdefault("charts", {})
    payload.setdefault(
        "health_matrix",
        {
            "velocity_cutoff_low": None,
            "velocity_cutoff_high": None,
            "profitability_cutoff_low": None,
            "profitability_cutoff_high": None,
            "profitability_metric": "margin_pct_or_contribution_lb",
            "quadrants": [],
        },
    )
    payload.setdefault("pricing_guardrails", {"high_outlier_count": 0, "low_outlier_count": 0, "outside_count": 0, "outside_pct": None, "rows": []})
    payload.setdefault("execution_lists", {"pricing_fixes": [], "cost_fixes": [], "promote_candidates": []})
    payload.setdefault(
        "protein_insights",
        {
            "summary": {"family_count": 0, "top_family": None, "top_family_share": None, "concentration_hhi": None},
            "mix": [],
            "mix_shift": [],
            "margin_watch": [],
            "growth_pockets": [],
            "portfolio": [],
            "pricing_opportunities": [],
            "execution_watch": [],
            "leaders": [],
            "narrative": {},
        },
    )
    if "trend" not in payload:
        payload["trend"] = payload.get("charts", {}).get("trajectory") or {}

    # Lightweight insights
    try:
        comparison_summary = payload.get("comparison_summary") or {}
        insight_list: List[Dict[str, Any]] = []
        if comparison_summary:
            insight_list.append(
                {
                    "metric": "comparison_delta",
                    "current": comparison_summary.get("revenue_current"),
                    "prev": comparison_summary.get("revenue_prior"),
                    "delta_pct": comparison_summary.get("revenue_delta_pct"),
                    "label": comparison.get("comparison_label"),
                }
            )
        top_list = payload.get("charts", {}).get("top_products") or []
        if top_list:
            top0 = top_list[0]
            insight_list.append({"metric": "top_product", "sku": top0.get("sku") or top0.get("product_id"), "label": top0.get("display_name") or top0.get("product_name"), "revenue": top0.get("revenue")})
        payload["insights"] = insight_list
    except Exception:
        payload["insights"] = []

    # Velocity pulse payload
    vel = metrics.get("velocity", {}) if isinstance(metrics, dict) else {}
    kpis = metrics.get("kpis", {}) if isinstance(metrics, dict) else {}
    payload["velocity"] = {
        "avg_weekly": vel.get("avg_weekly") or 0.0,
        "weekly_revenue": vel.get("weekly_revenue") or 0.0,
        "rev_per_product": (kpis.get("revenue") or 0.0) / max(1, kpis.get("products") or 0),
        "active_skus": vel.get("active_skus") or kpis.get("products") or 0,
        "roi_pct": kpis.get("margin_pct"),
        "customers": kpis.get("customers") or 0,
    }
    # Derived datasets for pricing + AI signals
    sku_metrics = metrics.get("sku_metrics") if isinstance(metrics, dict) else []
    sku_rows = _annotate_product_rows(_prepare_visual_pricing_rows([r for r in sku_metrics if isinstance(r, dict)]))
    if isinstance(payload.get("table"), dict):
        payload["table"]["rows"] = _annotate_product_rows(
            [r for r in ((payload.get("table") or {}).get("rows") or []) if isinstance(r, dict)]
        )
    payload["sku_metrics"] = sku_rows
    if isinstance(payload.get("charts"), dict):
        payload["charts"]["price_velocity"] = sku_rows
        payload["charts"]["top_products"] = sorted(
            [dict(r) for r in sku_rows if isinstance(r, dict)],
            key=lambda item: _clean_num(item.get("revenue")),
            reverse=True,
        )[:150]
    top_products = (payload.get("charts") or {}).get("top_products") or []
    payload["insights"] = [
        item
        for item in (
            (
                {
                    "metric": "comparison_delta",
                    "current": (payload.get("comparison_summary") or {}).get("revenue_current"),
                    "prev": (payload.get("comparison_summary") or {}).get("revenue_prior"),
                    "delta_pct": (payload.get("comparison_summary") or {}).get("revenue_delta_pct"),
                    "label": comparison.get("comparison_label"),
                }
                if payload.get("comparison_summary")
                else None
            ),
            (
                {
                    "metric": "top_product",
                    "sku": (top_products[0] or {}).get("sku") or (top_products[0] or {}).get("product_id"),
                    "label": (top_products[0] or {}).get("display_name") or (top_products[0] or {}).get("product_name"),
                    "revenue": (top_products[0] or {}).get("revenue"),
                }
                if top_products
                else None
            ),
        )
        if item
    ]
    weighted_target_margin = margin_rules.weighted_target_margin_pct(sku_rows)
    weighted_minimum_margin = margin_rules.weighted_target_margin_pct(sku_rows, target_key="minimum_margin_pct")
    pricing_visuals = _build_pricing_visual_payload(sku_rows)

    payload["price_vs_velocity"] = pricing_visuals.get("price_vs_velocity") or []
    payload["performance_bubble"] = {
        "target_margin": (weighted_target_margin / 100.0) if weighted_target_margin is not None else None,
        "floor_margin": (weighted_minimum_margin / 100.0) if weighted_minimum_margin is not None else None,
        "target_margin_label": _margin_band_range_label(pricing_visuals.get("target_margin_values") or []),
        "floor_margin_label": _margin_band_range_label(pricing_visuals.get("minimum_margin_values") or []),
        "points": pricing_visuals.get("performance_points") or [],
        "legend": pricing_visuals.get("legend_rows") or [],
        "summary_cards": pricing_visuals.get("summary_cards") or [],
    }
    payload["protein_insights"] = _build_protein_intelligence(
        payload.get("protein_insights") or {},
        sku_rows,
        payload.get("execution_lists") or {},
    )
    payload["margin_matrix"] = {
        "rules": margin_rules.all_margin_rules(),
        "status_meta": {
            key: margin_rules.status_meta(key)
            for key in ("red", "orange", "yellow", "light_green", "green", "needs_mapping", "no_cost")
        },
        "target_margin_range": _margin_band_range_label(pricing_visuals.get("target_margin_values") or []),
        "minimum_margin_range": _margin_band_range_label(pricing_visuals.get("minimum_margin_values") or []),
        "weighted_target_margin_pct": weighted_target_margin,
        "weighted_minimum_margin_pct": weighted_minimum_margin,
    }

    # Recommendations + AI signals
    recommendations, quick_rec_map, action_map = _build_recommendations(sku_rows)
    payload["recommendations"] = recommendations
    payload["ai_signals"] = _build_ai_signals(
        sku_rows,
        payload.get("charts", {}).get("trajectory", {}),
        summary={
            **(payload.get("kpis") or {}),
            "below_target_count": int(((payload.get("risk_opportunity") or {}).get("below_target_count") or 0)),
            "missing_cost_sku_count": int(((payload.get("kpis") or {}).get("missing_cost_sku_count") or 0)),
        },
    )
    payload["portfolio_posture"] = _portfolio_posture_from_health(
        payload.get("health_matrix") or {},
        payload.get("concentration") or {},
        payload.get("risk_opportunity") or {},
    )
    payload["decision_signals"] = _build_decision_signals(
        kpis=payload.get("kpis") or {},
        trajectory=payload.get("charts", {}).get("trajectory") or {},
        health_matrix=payload.get("health_matrix") or {},
        pricing_guardrails=payload.get("pricing_guardrails") or {},
        risk_opportunity=payload.get("risk_opportunity") or {},
        concentration=payload.get("concentration") or {},
        execution_lists=payload.get("execution_lists") or {},
        ai_signals=payload.get("ai_signals") or {},
        comparison_summary=payload.get("comparison_summary") or {},
        comparison=comparison,
    )
    payload["focus_actions"] = _build_focus_actions(
        kpis=payload.get("kpis") or {},
        health_matrix=payload.get("health_matrix") or {},
        risk_opportunity=payload.get("risk_opportunity") or {},
        concentration=payload.get("concentration") or {},
        execution_lists=payload.get("execution_lists") or {},
    )
    payload["story"] = _build_story_summary(
        comparison=comparison,
        comparison_summary=payload.get("comparison_summary") or {},
        concentration=payload.get("concentration") or {},
        risk_opportunity=payload.get("risk_opportunity") or {},
    )

    table_rows = payload.get("table", {}).get("rows", [])
    if isinstance(table_rows, list):
        payload["table"]["rows"] = _enrich_table_rows(table_rows, quick_rec_map=quick_rec_map, action_map=action_map)

    # Projected next month
    proj = _project_next_month(
        payload.get("charts", {}).get("trajectory", {}).get("labels") or [],
        payload.get("charts", {}).get("trajectory", {}).get("revenue") or [],
        comparison=comparison,
        current_revenue=_safe_float((payload.get("comparison_summary") or {}).get("revenue_current")),
    )
    payload["projected_next_month"] = proj
    try:
        insights = payload.get("insights") or []
        insights.append({"metric": "projected_next_month", **proj})
        payload["insights"] = insights
    except Exception:
        pass

    # Optional lightweight forecast overlay
    forecast_flag = str(args.get("forecast") or args.get("forecast_overlay") or "").lower() in {"1", "true", "yes"}
    if forecast_flag:
        labels = payload.get("charts", {}).get("trajectory", {}).get("labels") or []
        rev = payload.get("charts", {}).get("trajectory", {}).get("revenue") or []
        if labels and rev:
            last_label = labels[-1]
            try:
                last_dt = datetime.strptime(last_label + "-01", "%Y-%m-%d")
            except Exception:
                last_dt = None
            future_points = []
            base = proj.get("value") if isinstance(proj, dict) else None
            base_val = float(base) if base is not None else (sum(rev[-3:]) / max(1, min(3, len(rev))))
            for i in range(1, 7):
                if last_dt is not None:
                    next_dt = (last_dt.replace(day=1) + timedelta(days=32 * i)).replace(day=1)
                    label = next_dt.strftime("%Y-%m")
                else:
                    label = f"F{i}"
                future_points.append({"month": label, "revenue": base_val})
            payload["forecast_overlay"] = future_points

    try:
        from flask import current_app, has_request_context  # type: ignore

        if has_request_context():
            log_method = current_app.logger.info if duration_ms >= 750 else current_app.logger.debug
            log_method(
                "products.bundle.completed",
                extra={
                    "bundle_mode": bundle_mode,
                    "sections": sorted(requested),
                    "duration_ms": duration_ms,
                    "duckdb_query_count": meta.get("duckdb_query_count"),
                    "duckdb_ms": meta.get("duckdb_ms"),
                    "table_total": int((payload.get("table") or {}).get("total") or 0),
                    "scope_mode": scope.get("scope_mode"),
                    "scope_hash": scope.get("scope_hash"),
                },
            )
    except Exception:
        pass

    return payload


def build_products_drilldown(product_id: str, filters: Any, scope: Dict[str, Any], args: Any) -> Dict[str, Any]:
    """
    Lightweight drilldown bundle for a single product. Returns KPIs, trend, and related customers.
    """
    cols = fact_store.list_columns()
    date_col = _safe_col(cols, fs.CANON.date, "Date")
    revenue_col = _safe_col(cols, fs.CANON.revenue, "Revenue")
    cost_expr = _coalesce_expr(cols, (fs.CANON.cost, "Cost", "CostPrice"), "NULL")
    qty_expr = _coalesce_expr(
        cols,
        (fs.CANON.qty_units, "ShippedItems", "QuantityOrdered", "Qty", "Quantity", "Units", "ItemCount"),
        "0",
    )
    weight_expr = _coalesce_expr(cols, (fs.CANON.weight_lb, "Weight", "WeightLb", "ShippedLb", "pack_weight_lb_sum"), "0")
    cust_id = _safe_col(cols, fs.CANON.customer_id, "CustomerID")
    cust_name = _safe_col(cols, fs.CANON.customer_name, "CustomerName", "Name")
    sku_col, prod_id_col, prod_name = _resolve_product_columns(cols)
    order_id = _safe_col(cols, fs.CANON.order_id, "OrderID")
    region_col = _safe_col(cols, fs.CANON.region, "Region", "RegionName")
    supplier_col = _safe_col(cols, fs.CANON.supplier_id, fs.CANON.supplier_name, "Supplier", "SupplierName")

    if not all([date_col, revenue_col, qty_expr, sku_col, cust_id, order_id]):
        return {"error": {"message": "Required columns missing for drilldown"}, "meta": {"cached": False}}

    where_sql, where_params, start_iso, end_iso = fact_store.build_where_clause(filters, cols, scope, apply_default_window=True)
    # Extend date window for classification/lifecycle while honoring non-date filters + RBAC.
    try:
        end_dt = datetime.fromisoformat(end_iso) if end_iso else datetime.utcnow()
    except Exception:
        end_dt = datetime.utcnow()
    start_dt = None
    if start_iso:
        try:
            start_dt = datetime.fromisoformat(start_iso)
        except Exception:
            start_dt = None
    if start_dt:
        window_days = max(1, (end_dt.date() - start_dt.date()).days + 1)
        base_weeks = max(1, int(math.ceil(window_days / 7)))
    else:
        base_weeks = 0
    class_weeks = min(max(base_weeks, 26), 52) if base_weeks else 26
    class_start_dt = end_dt - timedelta(days=class_weeks * 7)
    lifecycle_start_dt = end_dt - timedelta(days=365)
    extended_start_dt = min(class_start_dt, lifecycle_start_dt)
    try:
        extended_filters = replace(filters, start=extended_start_dt, end=end_dt)
    except Exception:
        if isinstance(filters, dict):
            extended_filters = {**filters, "start": extended_start_dt, "end": end_dt}
        else:
            extended_filters = filters
    ext_where_sql, ext_where_params, _, _ = fact_store.build_where_clause(
        extended_filters, cols, scope, apply_default_window=False
    )
    region_expr = region_col or "NULL"
    supplier_expr = supplier_col or "NULL"
    exprs = _product_exprs(cols)
    sku_expr = exprs["sku_expr"]
    prod_id_expr = exprs["prod_id_expr"]
    product_key_expr = exprs["product_key_expr"]
    product_name_expr = exprs["product_name_expr"]
    display_name_expr = exprs["display_name_expr"]
    cust_name_expr = f"COALESCE({cust_name}, {cust_id})" if cust_name else f"{cust_id}"
    # Use aliases from product_base/product_ext (weight/qty) to avoid referencing raw columns in later CTEs.
    demand_expr = "CASE WHEN weight > 0 THEN weight ELSE qty END"

    include_extras = str(args.get("extras") or args.get("include_extras") or "").lower() in {"1", "true", "yes"}
    cross_sell_cte = ""
    cross_sell_select = "NULL AS cross_sell_list"
    base_params = list(where_params)
    ext_params = list(ext_where_params)
    # Params order: base filters, extended filters, product_id (base key/raw), product_id (extended key/raw),
    # product_id (co-orders key/raw), [+ product_id for cross-sell]
    args_all = base_params + ext_params + [
        product_id, product_id,  # product_base
        product_id, product_id,  # product_ext
        product_id, product_id,  # co_orders
    ]
    if include_extras:
        cross_sell_cte = """
        ,
        cross_sell AS (
            SELECT
                b.product_id AS product_id,
                any_value(b.product_name) AS product_name,
                any_value(b.display_name) AS display_name,
                COUNT(DISTINCT b.order_id) AS co_orders,
                SUM(b.revenue) AS paired_revenue
            FROM base b
            JOIN orders_for_product o ON b.order_id = o.order_id
            WHERE b.product_id <> ?
            GROUP BY b.product_id
            ORDER BY co_orders DESC
            LIMIT 15
        )
        """
        cross_sell_select = "(SELECT list(struct_pack(product_id:=product_id, product_name:=product_name, display_name:=display_name, co_orders:=co_orders, paired_revenue:=paired_revenue)) FROM cross_sell) AS cross_sell_list"
        args_all.append(product_id)

    sql = f"""
        WITH base AS (
            SELECT
                {date_col}::DATE AS date,
                {product_key_expr} AS product_id,
                {prod_id_expr} AS product_id_raw,
                {sku_expr} AS sku,
                {product_name_expr} AS product_name,
                {display_name_expr} AS display_name,
                {cust_id}::VARCHAR AS customer_id,
                {cust_name_expr}::VARCHAR AS customer_name,
                {order_id}::VARCHAR AS order_id,
                CAST({revenue_col} AS DOUBLE) AS revenue,
                CAST({cost_expr} AS DOUBLE) AS cost,
                CAST({qty_expr} AS DOUBLE) AS qty,
                CAST({weight_expr} AS DOUBLE) AS weight,
                {region_expr}::VARCHAR AS region,
                {supplier_expr}::VARCHAR AS supplier
            FROM fact
            WHERE {where_sql}
        ),
        extended AS (
            SELECT
                {date_col}::DATE AS date,
                {product_key_expr} AS product_id,
                {prod_id_expr} AS product_id_raw,
                {sku_expr} AS sku,
                {product_name_expr} AS product_name,
                {display_name_expr} AS display_name,
                {cust_id}::VARCHAR AS customer_id,
                {cust_name_expr}::VARCHAR AS customer_name,
                {order_id}::VARCHAR AS order_id,
                CAST({revenue_col} AS DOUBLE) AS revenue,
                CAST({cost_expr} AS DOUBLE) AS cost,
                CAST({qty_expr} AS DOUBLE) AS qty,
                CAST({weight_expr} AS DOUBLE) AS weight,
                {region_expr}::VARCHAR AS region,
                {supplier_expr}::VARCHAR AS supplier
            FROM fact
            WHERE {ext_where_sql}
        ),
        product_base AS (
            SELECT
                *,
                CASE
                    WHEN weight > 0 THEN revenue / NULLIF(weight, 0)
                    WHEN qty > 0 THEN revenue / NULLIF(qty, 0)
                    ELSE NULL
                END AS unit_price
            FROM base
            WHERE product_id = ? OR product_id_raw = ?
        ),
        product_ext AS (
            SELECT
                *,
                CASE
                    WHEN weight > 0 THEN revenue / NULLIF(weight, 0)
                    WHEN qty > 0 THEN revenue / NULLIF(qty, 0)
                    ELSE NULL
                END AS unit_price
            FROM extended
            WHERE product_id = ? OR product_id_raw = ?
        ),
        orders_for_product AS (
            SELECT DISTINCT order_id FROM product_base
        ),
        orders_count AS (
            SELECT COUNT(*) AS total_orders FROM orders_for_product
        ),
        orders_all AS (
            SELECT COUNT(DISTINCT order_id) AS total_orders_all FROM base
        ),
        other_orders AS (
            SELECT product_id, COUNT(DISTINCT order_id) AS orders_with_other
            FROM base
            GROUP BY product_id
        ),
        co_orders AS (
            SELECT
                b.product_id AS product_id,
                any_value(b.sku) AS sku,
                any_value(b.product_name) AS product_name,
                any_value(b.display_name) AS display_name,
                COUNT(DISTINCT b.order_id) AS co_orders,
                SUM(b.revenue) AS paired_revenue
            FROM base b
            JOIN orders_for_product o ON b.order_id = o.order_id
            WHERE b.product_id <> ? AND (b.product_id_raw IS NULL OR b.product_id_raw <> ?)
            GROUP BY b.product_id
            ORDER BY co_orders DESC
            LIMIT 50
        ),
        co_enriched AS (
            SELECT
                c.*,
                (SELECT total_orders FROM orders_count) AS base_orders,
                (SELECT total_orders_all FROM orders_all) AS total_orders_all,
                oo.orders_with_other AS orders_with_other,
                CASE
                    WHEN (SELECT total_orders FROM orders_count) > 0
                        THEN c.co_orders * 1.0 / (SELECT total_orders FROM orders_count)
                    ELSE NULL
                END AS confidence,
                CASE
                    WHEN (SELECT total_orders FROM orders_count) > 0 AND oo.orders_with_other > 0 AND (SELECT total_orders_all FROM orders_all) > 0
                        THEN (c.co_orders * 1.0 / (SELECT total_orders FROM orders_count))
                             / (oo.orders_with_other * 1.0 / (SELECT total_orders_all FROM orders_all))
                    ELSE NULL
                END AS lift
            FROM co_orders c
            LEFT JOIN other_orders oo ON c.product_id = oo.product_id
        ),
        kpis AS (
            SELECT
                SUM(revenue) AS revenue,
                SUM(qty) AS qty,
                SUM(weight) AS weight,
                SUM(cost) AS cost,
                COUNT(DISTINCT customer_id) AS customers,
                COUNT(DISTINCT order_id) AS orders,
                COUNT(*) AS rows,
                MIN(date) AS first_sold,
                MAX(date) AS last_sold,
                COUNT(DISTINCT region) AS region_count,
                COUNT(DISTINCT supplier) AS supplier_count
            FROM product_base
        ),
        monthly AS (
            SELECT
                strftime('%Y-%m', date) AS month,
                SUM(revenue) AS revenue,
                SUM(qty) AS qty,
                COUNT(DISTINCT order_id) AS orders
            FROM product_base
            GROUP BY 1
            ORDER BY 1
        ),
        lifecycle_monthly AS (
            SELECT
                strftime('%Y-%m', date) AS month,
                SUM(revenue) AS revenue,
                SUM(qty) AS qty
            FROM product_ext
            GROUP BY 1
            ORDER BY 1
        ),
        weekly AS (
            SELECT
                DATE_TRUNC('week', date)::DATE AS week_start,
                SUM({demand_expr}) AS demand
            FROM product_ext
            GROUP BY 1
            ORDER BY 1
        ),
        weekly_nonzero AS (
            SELECT demand FROM weekly WHERE demand > 0
        ),
        class_stats AS (
            SELECT
                COUNT(*) AS weeks_nonzero,
                AVG(demand) AS mean_demand,
                STDDEV_SAMP(demand) AS std_demand
            FROM weekly_nonzero
        ),
        top_customers AS (
            SELECT
                customer_id,
                any_value(customer_name) AS customer_name,
                SUM(revenue) AS revenue,
                SUM(qty) AS qty,
                COUNT(DISTINCT order_id) AS orders
            FROM product_base
            GROUP BY customer_id
            ORDER BY revenue DESC
            LIMIT 15
        ),
        top_regions AS (
            SELECT
                region,
                SUM(revenue) AS revenue
            FROM product_base
            WHERE region IS NOT NULL
            GROUP BY region
            ORDER BY revenue DESC
            LIMIT 10
        ),
        top_suppliers AS (
            SELECT
                supplier,
                SUM(revenue) AS revenue
            FROM product_base
            WHERE supplier IS NOT NULL
            GROUP BY supplier
            ORDER BY revenue DESC
            LIMIT 10
        ),
        weekday AS (
            SELECT
                strftime('%w', date) AS weekday,
                SUM(revenue) AS revenue,
                COUNT(DISTINCT order_id) AS orders
            FROM product_base
            GROUP BY 1
            ORDER BY 1
        ),
        unit_price_stats AS (
            SELECT
                quantile_cont(unit_price, 0.10) AS p10,
                quantile_cont(unit_price, 0.50) AS p50,
                quantile_cont(unit_price, 0.90) AS p90
            FROM product_base
            WHERE unit_price IS NOT NULL
        ),
        unit_price_sample AS (
            SELECT unit_price
            FROM product_base
            WHERE unit_price IS NOT NULL
            LIMIT 10000
        )
        {cross_sell_cte}
        SELECT
            (SELECT any_value(product_name) FROM product_base) AS product_name,
            (SELECT any_value(display_name) FROM product_base) AS display_name,
            (SELECT revenue FROM kpis) AS revenue,
            (SELECT cost FROM kpis) AS cost,
            (SELECT qty FROM kpis) AS qty,
            (SELECT weight FROM kpis) AS weight,
            (SELECT customers FROM kpis) AS customers,
            (SELECT orders FROM kpis) AS orders,
            (SELECT rows FROM kpis) AS rows,
            (SELECT first_sold FROM kpis) AS first_sold,
            (SELECT last_sold FROM kpis) AS last_sold,
            (SELECT region_count FROM kpis) AS region_count,
            (SELECT supplier_count FROM kpis) AS supplier_count,
            (SELECT list(month) FROM monthly) AS labels,
            (SELECT list(revenue) FROM monthly) AS rev_series,
            (SELECT list(qty) FROM monthly) AS qty_series,
            (SELECT list(orders) FROM monthly) AS order_series,
            (SELECT list(struct_pack(CustomerId:=customer_id, Customer:=customer_name, Revenue:=revenue, Qty:=qty, Orders:=orders)) FROM top_customers) AS customers_list,
            (SELECT list(struct_pack(region:=region, revenue:=revenue)) FROM top_regions) AS regions_list,
            (SELECT list(struct_pack(supplier:=supplier, revenue:=revenue)) FROM top_suppliers) AS suppliers_list,
            (SELECT list(struct_pack(weekday:=weekday, revenue:=revenue, orders:=orders)) FROM weekday) AS weekday_list,
            (SELECT p10 FROM unit_price_stats) AS up_p10,
            (SELECT p50 FROM unit_price_stats) AS up_p50,
            (SELECT p90 FROM unit_price_stats) AS up_p90,
            (SELECT list(unit_price) FROM unit_price_sample) AS unit_prices,
            (SELECT list(struct_pack(week:=week_start, demand:=demand)) FROM weekly) AS weekly_series,
            (SELECT weeks_nonzero FROM class_stats) AS class_weeks,
            (SELECT mean_demand FROM class_stats) AS class_mean,
            (SELECT std_demand FROM class_stats) AS class_std,
            (SELECT list(month) FROM lifecycle_monthly) AS lc_labels,
            (SELECT list(revenue) FROM lifecycle_monthly) AS lc_revenue,
            (SELECT list(qty) FROM lifecycle_monthly) AS lc_qty,
            (SELECT list(struct_pack(
                product_id:=product_id,
                other_product_id:=product_id,
                sku:=sku,
                other_sku:=sku,
                product_name:=product_name,
                other_name:=product_name,
                display_name:=display_name,
                co_orders:=co_orders,
                confidence:=confidence,
                lift:=lift,
                paired_revenue:=paired_revenue,
                revenue:=paired_revenue,
                orders_with_other:=orders_with_other
            )) FROM co_enriched) AS bought_together_list,
            (SELECT total_orders FROM orders_count) AS bt_base_orders,
            (SELECT total_orders_all FROM orders_all) AS bt_total_orders,
            (SELECT MAX(co_orders) FROM co_enriched) AS bt_max_co_orders,
            {cross_sell_select}
        LIMIT 1
    """

    df = fact_store.execute_sql_df(sql, args_all, tag="products.drilldown.bundle")
    if df.empty:
        return {
            "error": {"message": "Product not found"},
            "meta": {"page_id": "product_drilldown", "entity_id": product_id, "entity_label": product_id},
        }

    row = df.iloc[0]
    revenue = _clean_num(row.get("revenue"))
    cost = _safe_float(row.get("cost"))
    profit = (revenue - cost) if (cost is not None) else None
    margin_pct = (profit / revenue * 100) if (revenue and profit is not None) else None
    rows_count = _clean_int(row.get("rows"))
    if rows_count == 0:
        return {
            "error": {"message": "Product not found"},
            "meta": {"page_id": "product_drilldown", "entity_id": product_id, "entity_label": product_id},
        }

    kpis = {
        "revenue": revenue,
        "cost": cost,
        "profit": profit,
        "qty": _clean_num(row.get("qty")),
        "weight": _clean_num(row.get("weight")),
        "customers": _clean_int(row.get("customers")),
        "orders": _clean_int(row.get("orders")),
        "rows": rows_count,
        "margin_pct": margin_pct,
        "first_sold": str(row.get("first_sold")) if row.get("first_sold") is not None else None,
        "last_sold": str(row.get("last_sold")) if row.get("last_sold") is not None else None,
        "region_count": _clean_int(row.get("region_count")),
        "supplier_count": _clean_int(row.get("supplier_count")),
    }

    trend = {
        "labels": _to_list(row.get("labels")),
        "revenue": _to_list(row.get("rev_series")),
        "qty": _to_list(row.get("qty_series")),
        "orders": _to_list(row.get("order_series")),
    }

    table_rows = []
    for cust_row in _to_list(row.get("customers_list")):
        if not isinstance(cust_row, dict):
            continue
        table_rows.append(
            {
                "key": cust_row.get("customer_id"),
                "label": cust_row.get("Customer") or cust_row.get("customer_name"),
                "revenue": _clean_num(cust_row.get("Revenue") or cust_row.get("revenue")),
                "qty": _clean_num(cust_row.get("Qty") or cust_row.get("qty")),
                "orders": _clean_int(cust_row.get("Orders") if cust_row.get("Orders") is not None else cust_row.get("orders")),
            }
        )

    monthly_series = []
    labels = trend.get("labels") or []
    rev = trend.get("revenue") or []
    qty = trend.get("qty") or []
    orders = trend.get("orders") or []
    for idx, label in enumerate(labels):
        monthly_series.append(
            {
                "month": label,
                "revenue": _clean_num(rev[idx] if idx < len(rev) else 0),
                "units": _clean_num(qty[idx] if idx < len(qty) else 0),
                "orders": _clean_num(orders[idx] if idx < len(orders) else 0),
            }
        )

    # ---------- Classification (XYZ variability) ----------
    class_weeks = _clean_int(row.get("class_weeks"))
    class_mean = _safe_float(row.get("class_mean"))
    class_std = _safe_float(row.get("class_std"))
    cv_val = (class_std / class_mean) if (class_mean and class_std is not None) else None
    if class_weeks < 8:
        class_label = "Insufficient history"
        variability_label = "Insufficient history"
        class_notes = "Need at least 8 non-zero weeks."
    else:
        if cv_val is None:
            class_label = "Insufficient history"
            variability_label = "Insufficient history"
            class_notes = "Not enough signal to classify variability."
        elif cv_val < 0.5:
            class_label = "Stable"
            variability_label = "Stable"
            class_notes = f"Computed over {class_weeks} weeks."
        elif cv_val <= 1.0:
            class_label = "Variable"
            variability_label = "Variable"
            class_notes = f"Computed over {class_weeks} weeks."
        else:
            class_label = "Highly Variable"
            variability_label = "Highly Variable"
            class_notes = f"Computed over {class_weeks} weeks."

    classification = {
        "label": class_label,
        "cv": cv_val,
        "cv_pct": (cv_val * 100) if cv_val is not None else None,
        "variability_label": variability_label,
        "notes": class_notes,
        "weeks_nonzero": class_weeks,
    }

    # ---------- Lifecycle ----------
    lc_labels = _to_list(row.get("lc_labels"))
    lc_revenue = [ _clean_num(v) for v in _to_list(row.get("lc_revenue")) ]
    lc_qty = [ _clean_num(v) for v in _to_list(row.get("lc_qty")) ]
    # Align lengths defensively
    lc_len = min(len(lc_labels), len(lc_revenue), len(lc_qty))
    lc_labels = lc_labels[:lc_len]
    lc_revenue = lc_revenue[:lc_len]
    lc_qty = lc_qty[:lc_len]

    use_revenue = sum(lc_revenue) > 0
    lc_metric = lc_revenue if use_revenue else lc_qty
    total_months = len(lc_metric)
    period_len = 6 if total_months >= 12 else (3 if total_months >= 6 else 0)
    recent_vals = lc_metric[-period_len:] if period_len else []
    prior_vals = lc_metric[-2 * period_len:-period_len] if period_len else []
    recent_avg = (sum(recent_vals) / period_len) if period_len and recent_vals else None
    prior_avg = (sum(prior_vals) / period_len) if period_len and prior_vals else None
    growth_rate = ((recent_avg - prior_avg) / prior_avg) if (prior_avg and recent_avg is not None) else None

    recent_nonzero = sum(1 for v in recent_vals if v > 0) if period_len else 0
    prior_nonzero = sum(1 for v in prior_vals if v > 0) if period_len else 0
    conf = int(min(100, ((recent_nonzero + prior_nonzero) / max(1, period_len * 2)) * 100)) if period_len else 0

    stage = "Insufficient history"
    message = "Not enough months to determine lifecycle stage."
    if period_len:
        stage = "Mature"
        message = ""
        if growth_rate is not None:
            if growth_rate > 0.15 and (recent_avg or 0) > 0:
                stage = "Growth"
            elif growth_rate < -0.15:
                stage = "Decline"
            elif abs(growth_rate) <= 0.10:
                stage = "Mature"
            else:
                stage = "Transition"
        # Override for new products
        try:
            first_sold_dt = datetime.fromisoformat(str(kpis.get("first_sold"))) if kpis.get("first_sold") else None
        except Exception:
            first_sold_dt = None
        if first_sold_dt and (end_dt.date() - first_sold_dt.date()).days <= 60:
            stage = "New"
            message = "Recently introduced SKU."

    lifecycle = {
        "stage": stage,
        "confidence": conf,
        "growth_rate": (growth_rate * 100) if growth_rate is not None else None,
        "recent_avg_revenue": recent_avg if use_revenue else None,
        "recent_avg_units": (sum(lc_qty[-period_len:]) / period_len) if period_len and lc_qty else None,
        "message": message,
    }

    # ---------- Forecast ----------
    forecast_labels = trend.get("labels") or []
    forecast_revenue = [ _clean_num(v) for v in trend.get("revenue") or [] ]
    forecast_qty = [ _clean_num(v) for v in trend.get("qty") or [] ]
    forecast_metric = forecast_revenue if sum(forecast_revenue) > 0 else forecast_qty
    forecast_actual = [
        {"date": label, "actual": _clean_num(forecast_metric[idx] if idx < len(forecast_metric) else 0)}
        for idx, label in enumerate(forecast_labels)
    ]

    forecast = {
        "model": "baseline",
        "confidence": "low",
        "mape": None,
        "series": forecast_actual,
        "forecast": [],
        "message": None,
    }
    non_zero_points = sum(1 for v in forecast_metric if v > 0)
    if len(forecast_metric) < 8 or non_zero_points < 2:
        forecast["message"] = "Not enough history to forecast."
    else:
        # Simple seasonal-naive or rolling mean forecast (6 months)
        if len(forecast_metric) >= 12:
            model = "seasonal_naive"
            season = forecast_metric[-12:]
        else:
            model = "rolling_mean_3"
            season = None
        forecast["model"] = model
        forecast["confidence"] = "medium" if len(forecast_metric) >= 12 else "low"
        last_label = forecast_labels[-1] if forecast_labels else None
        try:
            last_dt = datetime.strptime(f"{last_label}-01", "%Y-%m-%d") if last_label else None
        except Exception:
            last_dt = None
        for i in range(1, 7):
            if season is not None:
                yhat = season[(i - 1) % len(season)]
            else:
                window = forecast_metric[-min(3, len(forecast_metric)):]
                yhat = sum(window) / max(1, len(window))
            band = yhat * 0.15
            if last_dt is not None:
                next_dt = (last_dt.replace(day=1) + timedelta(days=32 * i)).replace(day=1)
                label = next_dt.strftime("%Y-%m")
            else:
                label = f"F{i}"
            forecast["forecast"].append(
                {
                    "date": label,
                    "yhat": _clean_num(yhat),
                    "yhat_lower": _clean_num(yhat - band),
                    "yhat_upper": _clean_num(yhat + band),
                }
            )

    # ---------- Bought Together ----------
    bt_rows = _to_list(row.get("bought_together_list"))
    bt_base_orders = int(_clean_num(row.get("bt_base_orders")))
    bt_total_orders = int(_clean_num(row.get("bt_total_orders")))
    bt_max_co = int(_clean_num(row.get("bt_max_co_orders")))
    if bt_base_orders < 10 or bt_max_co < 3:
        bt_rows = []
        bt_message = "Not enough co-orders to show related products."
    else:
        bt_message = None
    bought_together = {
        "mode": "confidence",
        "rows": bt_rows,
        "base_orders": bt_base_orders,
        "total_orders": bt_total_orders,
        "message": bt_message,
    }

    entity_display = row.get("display_name") or row.get("product_name") or product_id
    payload = {
        "kpis": kpis,
        "trend": trend,
        "table": {"rows": table_rows, "page": 1, "page_size": len(table_rows) or 15, "total": len(table_rows)},
        "monthly_series": monthly_series,
        "classification": classification,
        "lifecycle": lifecycle,
        "forecast": forecast,
        "bought_together": bought_together,
        "top_customers": _to_list(row.get("customers_list")),
        "top_regions": _to_list(row.get("regions_list")),
        "top_suppliers": _to_list(row.get("suppliers_list")),
        "weekday_distribution": _to_list(row.get("weekday_list")),
        "price_distribution": {
            "p10": _safe_float(row.get("up_p10")),
            "p50": _safe_float(row.get("up_p50")),
            "p90": _safe_float(row.get("up_p90")),
            "samples": _to_list(row.get("unit_prices")),
        },
        "cross_sell": _to_list(row.get("cross_sell_list")),
        "meta": {"page_id": "product_drilldown", "entity_id": product_id, "entity_label": entity_display, "entity_display_name": entity_display},
    }
    return payload
