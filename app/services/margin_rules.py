from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict, Iterable, List, Mapping, Sequence

try:
    import pandas as pd
except Exception:  # pragma: no cover - pandas is available in app runtime/tests
    pd = None  # type: ignore[assignment]


_DQ_BUCKET = "Needs Protein Mapping"
_NO_COST_LABEL = "No cost visibility"
_FLAT_OVERHEAD_CHARGE = 0.85
_WEIGHT_KEYS: Sequence[str] = (
    "weight_lb",
    "weight",
    "pack_weight_lb_sum",
    "shipped_lb",
    "weight_lb_current",
    "weight_current",
    "current_weight_lb",
)
_QTY_KEYS: Sequence[str] = (
    "pricing_basis_qty",
    "basis_qty",
    "qty_basis",
    "qty",
    "quantity",
    "units",
    "item_count",
    "pack_item_count_sum",
    "qty_current",
    "units_current",
    "current_qty",
    "current_units",
)


@dataclass(frozen=True)
class MarginRule:
    family: str
    min_product_margin_pct: float | None
    min_gross_margin_pct: float | None
    min_ebitda_margin_pct: float | None
    target_product_margin_pct: float | None
    target_gross_margin_pct: float | None
    target_ebitda_margin_pct: float | None


_RULE_ROWS: Sequence[MarginRule] = (
    MarginRule("Deli", 36.0, 22.0, 9.0, 45.0, 31.0, 18.0),
    MarginRule("Charcuterie", 49.0, 35.0, 22.0, 58.0, 44.0, 31.0),
    MarginRule("Duck", 41.0, 27.0, 14.0, 50.0, 36.0, 23.0),
    MarginRule("Beef", 31.0, 17.0, 4.0, 40.0, 26.0, 13.0),
    MarginRule("Sausage", 49.0, 35.0, 22.0, 58.0, 44.0, 31.0),
    MarginRule("Chicken", 31.0, 17.0, 4.0, 40.0, 26.0, 13.0),
    MarginRule("Lamb", 34.0, 20.0, 7.0, 43.0, 29.0, 16.0),
    MarginRule("Grind", 31.0, 17.0, 4.0, 40.0, 26.0, 13.0),
    MarginRule("Eggs", 31.0, 17.0, 4.0, 40.0, 26.0, 13.0),
    MarginRule("Bones", 31.0, 17.0, 4.0, 40.0, 26.0, 13.0),
    MarginRule("Pork", 31.0, 17.0, 4.0, 40.0, 26.0, 13.0),
    MarginRule("Misc", 34.0, 20.0, 7.0, 43.0, 29.0, 16.0),
    MarginRule("Supplies", 39.0, 25.0, 12.0, 48.0, 34.0, 21.0),
    MarginRule("Rabbit", 34.0, 20.0, 7.0, 43.0, 29.0, 16.0),
    MarginRule("Imported Charcuterie", 39.0, 25.0, 12.0, 48.0, 34.0, 21.0),
    MarginRule("Veal", 34.0, 20.0, 7.0, 43.0, 29.0, 16.0),
    MarginRule("Bison", 31.0, 17.0, 4.0, 40.0, 26.0, 13.0),
    MarginRule("Seafood", 34.0, 20.0, 7.0, 43.0, 29.0, 16.0),
    MarginRule("Turkey", 31.0, 17.0, 4.0, 40.0, 26.0, 13.0),
    MarginRule("CornishHens", 34.0, 20.0, 7.0, 43.0, 29.0, 16.0),
    MarginRule("Elk", 34.0, 20.0, 7.0, 43.0, 29.0, 16.0),
    MarginRule("Wild Boar", 34.0, 20.0, 7.0, 43.0, 29.0, 16.0),
    MarginRule("Venison", 34.0, 20.0, 7.0, 43.0, 29.0, 16.0),
    MarginRule("Hiro", 31.0, 17.0, 4.0, 40.0, 26.0, 13.0),
    MarginRule("Foie", 34.0, 20.0, 7.0, 43.0, 29.0, 16.0),
    MarginRule("Hot Dog", 34.0, 20.0, -3.0, 43.0, 29.0, 16.0),
    MarginRule("Bacon", 34.0, 20.0, -2.0, 43.0, 29.0, 16.0),
    MarginRule("Broth", 39.0, 25.0, None, 48.0, 34.0, 21.0),
    MarginRule("Goat", 39.0, 25.0, 5.0, 48.0, 34.0, 21.0),
    MarginRule("Goose", 39.0, 25.0, 5.0, 48.0, 34.0, 21.0),
    MarginRule("Partridge", 39.0, 25.0, 5.0, 48.0, 34.0, 21.0),
    MarginRule("Squab", 39.0, 25.0, 5.0, 48.0, 34.0, 21.0),
    MarginRule("Quail", 39.0, 25.0, 5.0, 48.0, 34.0, 21.0),
    MarginRule("Guinea Fowl", 39.0, 25.0, 5.0, 48.0, 34.0, 21.0),
)

_RULES_BY_KEY: Dict[str, MarginRule] = {}
_ALIASES_BY_KEY: Dict[str, set[str]] = {}


def _normalize_key(value: Any) -> str:
    if value is None:
        raw = ""
    else:
        try:
            if pd is not None and bool(pd.isna(value)):
                raw = ""
            else:
                raw = str(value)
        except Exception:
            raw = str(value)
    return "".join(ch for ch in raw.strip().lower() if ch.isalnum())


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(value)
        if math.isnan(parsed):
            return None
        return parsed
    except Exception:
        return None


def _clamp(value: float, lower: float, upper: float) -> float:
    return float(min(max(value, lower), upper))


def _present_value(value: Any) -> bool:
    if value is None:
        return False
    try:
        if pd is not None and pd.isna(value):
            return False
    except Exception:
        pass
    return str(value) != ""


def _meaningful_key(value: Any) -> str:
    token = _normalize_key(value)
    if token in {"", "unknown", "unassigned", "none", "null", "na", "n a", "needsmapping", "needsproteinmapping"}:
        return ""
    return token


def _register_rule(rule: MarginRule, aliases: Iterable[str] | None = None) -> None:
    key = _normalize_key(rule.family)
    alias_values = {key}
    for alias in aliases or ():
        norm = _normalize_key(alias)
        if norm:
            alias_values.add(norm)
    _RULES_BY_KEY[key] = rule
    _ALIASES_BY_KEY[key] = alias_values


for _row in _RULE_ROWS:
    extra_aliases: List[str] = []
    if _row.family == "CornishHens":
        extra_aliases.extend(["Cornish Hens", "Cornish Hen"])
    if _row.family == "Wild Boar":
        extra_aliases.append("WildBoar")
    if _row.family == "Hot Dog":
        extra_aliases.append("HotDog")
    if _row.family == "Guinea Fowl":
        extra_aliases.append("GuineaFowl")
    if _row.family == "Imported Charcuterie":
        extra_aliases.append("ImportedCharcuterie")
    _register_rule(_row, extra_aliases)


_STATUS_META: Dict[str, Dict[str, Any]] = {
    "red": {
        "label": "Materially below minimum",
        "short_label": "Red",
        "color": "#c2413b",
        "tone": "danger",
        "severity": 5,
    },
    "orange": {
        "label": "Near minimum",
        "short_label": "Orange",
        "color": "#dd6b20",
        "tone": "warning",
        "severity": 4,
    },
    "yellow": {
        "label": "Between minimum and target",
        "short_label": "Yellow",
        "color": "#caa33a",
        "tone": "caution",
        "severity": 3,
    },
    "light_green": {
        "label": "Near target",
        "short_label": "Light Green",
        "color": "#7bbf6a",
        "tone": "positive",
        "severity": 2,
    },
    "green": {
        "label": "Above target",
        "short_label": "Green",
        "color": "#21884f",
        "tone": "excellent",
        "severity": 1,
    },
    "needs_mapping": {
        "label": _DQ_BUCKET,
        "short_label": "Needs Mapping",
        "color": "#7a7f87",
        "tone": "neutral",
        "severity": 6,
    },
    "no_cost": {
        "label": _NO_COST_LABEL,
        "short_label": "No Cost",
        "color": "#94a3b8",
        "tone": "muted",
        "severity": 6,
    },
}


def all_margin_rules() -> List[Dict[str, Any]]:
    return [
        {
            "family": rule.family,
            "min_product_margin_pct": rule.min_product_margin_pct,
            "min_gross_margin_pct": rule.min_gross_margin_pct,
            "min_ebitda_margin_pct": rule.min_ebitda_margin_pct,
            "target_product_margin_pct": rule.target_product_margin_pct,
            "target_gross_margin_pct": rule.target_gross_margin_pct,
            "target_ebitda_margin_pct": rule.target_ebitda_margin_pct,
        }
        for rule in _RULE_ROWS
    ]


def resolve_margin_rule(protein: Any = None, category: Any = None) -> Dict[str, Any]:
    protein_key = _meaningful_key(protein)
    category_key = _meaningful_key(category)

    for source, key in (("protein", protein_key), ("category", category_key)):
        if not key:
            continue
        for rule_key, aliases in _ALIASES_BY_KEY.items():
            if key in aliases:
                rule = _RULES_BY_KEY[rule_key]
                return {
                    "mapped": True,
                    "source": source,
                    "family": rule.family,
                    "rule_key": rule_key,
                    "display_family": rule.family,
                    "min_product_margin_pct": rule.min_product_margin_pct,
                    "min_gross_margin_pct": rule.min_gross_margin_pct,
                    "min_ebitda_margin_pct": rule.min_ebitda_margin_pct,
                    "target_product_margin_pct": rule.target_product_margin_pct,
                    "target_gross_margin_pct": rule.target_gross_margin_pct,
                    "target_ebitda_margin_pct": rule.target_ebitda_margin_pct,
                }

    return {
        "mapped": False,
        "source": None,
        "family": _DQ_BUCKET,
        "rule_key": None,
        "display_family": _DQ_BUCKET,
        "min_product_margin_pct": None,
        "min_gross_margin_pct": None,
        "min_ebitda_margin_pct": None,
        "target_product_margin_pct": None,
        "target_gross_margin_pct": None,
        "target_ebitda_margin_pct": None,
    }


def compute_actual_profit(revenue: Any, cost: Any) -> float | None:
    rev = _safe_float(revenue)
    cst = _safe_float(cost)
    if rev is None or cst is None:
        return None
    return float(rev - cst)


def compute_actual_margin_pct(revenue: Any, cost: Any) -> float | None:
    rev = _safe_float(revenue)
    cst = _safe_float(cost)
    if rev is None or cst is None or abs(rev) <= 1e-12:
        return None
    return float((rev - cst) / rev * 100.0)


def overhead_unit_cost() -> float:
    return float(_FLAT_OVERHEAD_CHARGE)


def basis_qty_from_values(*, basis_qty: Any = None, weight_lb: Any = None, qty: Any = None) -> float | None:
    return _first_positive_float(basis_qty, weight_lb, qty)


def effective_cost_from_values(
    cost: Any,
    *,
    basis_qty: Any = None,
    weight_lb: Any = None,
    qty: Any = None,
) -> float | None:
    base_cost = _safe_float(cost)
    if base_cost is None:
        return None
    return float(base_cost + _FLAT_OVERHEAD_CHARGE)


def sql_basis_qty_expr(weight_expr: str | None, qty_expr: str | None, *, fallback: str = "0.0") -> str:
    weight_sql = weight_expr or "NULL"
    qty_sql = qty_expr or "NULL"
    return (
        "CASE "
        f"WHEN COALESCE(CAST({weight_sql} AS DOUBLE), 0) > 0 THEN CAST({weight_sql} AS DOUBLE) "
        f"WHEN COALESCE(CAST({qty_sql} AS DOUBLE), 0) > 0 THEN CAST({qty_sql} AS DOUBLE) "
        f"ELSE {fallback} "
        "END"
    )


def sql_effective_cost_expr(cost_expr: str, weight_expr: str | None, qty_expr: str | None, *, fallback: str = "NULL") -> str:
    return (
        "CASE "
        f"WHEN CAST({cost_expr} AS DOUBLE) IS NULL THEN {fallback} "
        f"ELSE CAST({cost_expr} AS DOUBLE) + {_FLAT_OVERHEAD_CHARGE} "
        "END"
    )


def sql_effective_unit_cost_expr(unit_cost_expr: str, *, fallback: str = "NULL") -> str:
    return (
        "CASE "
        f"WHEN CAST({unit_cost_expr} AS DOUBLE) IS NULL THEN {fallback} "
        f"ELSE CAST({unit_cost_expr} AS DOUBLE) + {_FLAT_OVERHEAD_CHARGE} "
        "END"
    )


def sql_effective_profit_expr(
    revenue_expr: str,
    cost_expr: str,
    weight_expr: str | None,
    qty_expr: str | None,
    *,
    fallback: str = "NULL",
) -> str:
    effective_cost_expr = sql_effective_cost_expr(cost_expr, weight_expr, qty_expr, fallback=fallback)
    return (
        "CASE "
        f"WHEN CAST({revenue_expr} AS DOUBLE) IS NULL OR ({effective_cost_expr}) IS NULL THEN {fallback} "
        f"ELSE CAST({revenue_expr} AS DOUBLE) - ({effective_cost_expr}) "
        "END"
    )


def sql_effective_margin_expr(
    revenue_expr: str,
    cost_expr: str,
    weight_expr: str | None,
    qty_expr: str | None,
    *,
    fallback: str = "NULL",
) -> str:
    effective_profit_expr = sql_effective_profit_expr(revenue_expr, cost_expr, weight_expr, qty_expr, fallback=fallback)
    return (
        "CASE "
        f"WHEN CAST({revenue_expr} AS DOUBLE) > 0 AND ({effective_profit_expr}) IS NOT NULL "
        f"THEN (({effective_profit_expr}) / NULLIF(CAST({revenue_expr} AS DOUBLE), 0)) * 100 "
        f"ELSE {fallback} "
        "END"
    )


def sql_price_from_cost_expr(unit_cost_expr: str, margin_pct_expr: str, *, fallback: str = "NULL") -> str:
    return (
        "CASE "
        f"WHEN CAST({unit_cost_expr} AS DOUBLE) IS NULL OR CAST({margin_pct_expr} AS DOUBLE) IS NULL THEN {fallback} "
        f"WHEN (1 - (CAST({margin_pct_expr} AS DOUBLE) / 100.0)) <= 0 THEN {fallback} "
        f"ELSE CAST({unit_cost_expr} AS DOUBLE) / NULLIF(1 - (CAST({margin_pct_expr} AS DOUBLE) / 100.0), 0) "
        "END"
    )


def sql_price_uplift_pct_expr(current_price_expr: str, target_price_expr: str, *, fallback: str = "NULL") -> str:
    return (
        "CASE "
        f"WHEN CAST({current_price_expr} AS DOUBLE) IS NULL OR CAST({target_price_expr} AS DOUBLE) IS NULL THEN {fallback} "
        f"WHEN CAST({current_price_expr} AS DOUBLE) = 0 THEN {fallback} "
        f"ELSE (CAST({target_price_expr} AS DOUBLE) - CAST({current_price_expr} AS DOUBLE)) / NULLIF(CAST({current_price_expr} AS DOUBLE), 0) * 100 "
        "END"
    )


def weighted_margin_pct(
    rows: Sequence[Mapping[str, Any]],
    *,
    value_key: str,
    revenue_key: str = "revenue",
) -> float | None:
    weighted_sum = 0.0
    total_weight = 0.0
    for row in rows:
        revenue = _safe_float(row.get(revenue_key)) if isinstance(row, Mapping) else None
        value = _safe_float(row.get(value_key)) if isinstance(row, Mapping) else None
        if revenue is None or value is None or revenue <= 0:
            continue
        weighted_sum += revenue * value
        total_weight += revenue
    if total_weight <= 0:
        return None
    return float(weighted_sum / total_weight)


def apply_effective_cost_frame(
    frame: Any,
    *,
    revenue_col: str = "revenue",
    cost_col: str = "cost",
    qty_col: str = "qty",
    weight_col: str = "weight_lb",
    profit_col: str = "profit",
    margin_col: str = "margin_pct",
    effective_cost_col: str = "effective_cost_basis",
    copy: bool = True,
):
    if pd is None or frame is None:
        return frame
    if getattr(frame, "empty", False):
        return frame.copy() if copy else frame

    out = frame.copy() if copy else frame
    revenue = pd.to_numeric(out.get(revenue_col), errors="coerce")
    base_cost = pd.to_numeric(out.get(cost_col), errors="coerce")
    qty = pd.to_numeric(out.get(qty_col), errors="coerce") if qty_col in out.columns else pd.Series(pd.NA, index=out.index, dtype="float64")
    weight = pd.to_numeric(out.get(weight_col), errors="coerce") if weight_col in out.columns else pd.Series(pd.NA, index=out.index, dtype="float64")
    basis_qty = weight.where(weight > 0)
    basis_qty = basis_qty.where(basis_qty.notna(), qty.where(qty > 0))
    effective_cost = base_cost + _FLAT_OVERHEAD_CHARGE
    effective_cost = effective_cost.where(base_cost.notna())
    profit = (revenue - effective_cost).where(revenue.notna() & effective_cost.notna())
    margin = ((profit / revenue) * 100.0).where(revenue > 0)

    out["base_cost_basis"] = base_cost
    out["pricing_basis_qty"] = basis_qty
    out["flat_overhead_unit_cost"] = float(_FLAT_OVERHEAD_CHARGE)
    out[effective_cost_col] = effective_cost
    out[cost_col] = effective_cost
    out[profit_col] = profit
    out[margin_col] = margin
    return out


def price_from_cost(unit_cost: Any, margin_pct: Any) -> float | None:
    cost_value = _safe_float(unit_cost)
    margin_value = _safe_float(margin_pct)
    if cost_value is None or margin_value is None:
        return None
    margin_ratio = 1.0 - (margin_value / 100.0)
    if margin_ratio <= 1e-12:
        return None
    return float(cost_value / margin_ratio)


def minimum_price_from_cost(unit_cost: Any, minimum_product_margin_pct: Any) -> float | None:
    return price_from_cost(unit_cost, minimum_product_margin_pct)


def target_price_from_cost(unit_cost: Any, target_product_margin_pct: Any) -> float | None:
    return price_from_cost(unit_cost, target_product_margin_pct)


def _status_payload(key: str, *, key_name: str, label_name: str, band_name: str, color_name: str, tone_name: str, severity_name: str) -> Dict[str, Any]:
    meta = _STATUS_META[key]
    return {
        key_name: key,
        label_name: meta["label"],
        band_name: meta["short_label"],
        color_name: meta["color"],
        tone_name: meta["tone"],
        severity_name: meta["severity"],
    }


def _status_band_buffers(
    minimum: float | None,
    target: float | None,
    *,
    near_target_floor: float,
    near_target_ceiling: float,
    below_min_floor: float,
    below_min_ceiling: float,
) -> tuple[float, float]:
    minimum_value = _safe_float(minimum) or 0.0
    target_value = _safe_float(target) or minimum_value
    span = max(target_value - minimum_value, 0.0)
    near_target = _clamp(span * 0.20, near_target_floor, near_target_ceiling)
    materially_below_min = _clamp(span * 0.35, below_min_floor, below_min_ceiling)
    return near_target, materially_below_min


def _classify_threshold_status(
    actual: Any,
    minimum: Any,
    target: Any,
    *,
    near_target_floor: float,
    near_target_ceiling: float,
    below_min_floor: float,
    below_min_ceiling: float,
) -> str:
    actual_value = _safe_float(actual)
    minimum_value = _safe_float(minimum)
    target_value = _safe_float(target)
    if actual_value is None:
        return "no_cost"
    if minimum_value is None or target_value is None:
        return "needs_mapping"
    near_target, materially_below_min = _status_band_buffers(
        minimum_value,
        target_value,
        near_target_floor=near_target_floor,
        near_target_ceiling=near_target_ceiling,
        below_min_floor=below_min_floor,
        below_min_ceiling=below_min_ceiling,
    )
    if actual_value < (minimum_value - materially_below_min):
        return "red"
    if actual_value < minimum_value:
        return "orange"
    if actual_value < (target_value - near_target):
        return "yellow"
    if actual_value <= (target_value + near_target):
        return "light_green"
    return "green"


def classify_margin_status(actual_margin_pct: Any, minimum_product_margin_pct: Any, target_product_margin_pct: Any) -> Dict[str, Any]:
    key = _classify_threshold_status(
        actual_margin_pct,
        minimum_product_margin_pct,
        target_product_margin_pct,
        near_target_floor=1.0,
        near_target_ceiling=3.0,
        below_min_floor=2.0,
        below_min_ceiling=5.0,
    )

    return _status_payload(
        key,
        key_name="status_key",
        label_name="target_status",
        band_name="profitability_band",
        color_name="status_color",
        tone_name="status_tone",
        severity_name="status_severity",
    )


def classify_price_status(actual_price: Any, minimum_price: Any, target_price: Any) -> Dict[str, Any]:
    key = _classify_threshold_status(
        actual_price,
        minimum_price,
        target_price,
        near_target_floor=0.10,
        near_target_ceiling=0.75,
        below_min_floor=0.20,
        below_min_ceiling=1.50,
    )

    return _status_payload(
        key,
        key_name="price_status_key",
        label_name="price_status",
        band_name="price_band_label",
        color_name="price_status_color",
        tone_name="price_status_tone",
        severity_name="price_status_severity",
    )


def _first_present_value(row: Mapping[str, Any], candidates: Sequence[str]) -> Any:
    for key in candidates:
        if key in row and _present_value(row.get(key)):
            return row.get(key)
    return None


def _first_positive_float(*values: Any) -> float | None:
    for value in values:
        parsed = _safe_float(value)
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _infer_basis_qty(
    *,
    basis_qty: Any = None,
    weight_lb: Any = None,
    qty: Any = None,
    cost: Any = None,
    unit_cost: Any = None,
    revenue: Any = None,
    unit_price: Any = None,
) -> float | None:
    explicit = _first_positive_float(basis_qty)
    if explicit is not None:
        return explicit

    cost_value = _safe_float(cost)
    unit_cost_value = _safe_float(unit_cost)
    if cost_value is not None and unit_cost_value is not None and unit_cost_value > 1e-12:
        ratio = cost_value / unit_cost_value
        if ratio > 0:
            return float(ratio)

    revenue_value = _safe_float(revenue)
    unit_price_value = _safe_float(unit_price)
    if revenue_value is not None and unit_price_value is not None and unit_price_value > 1e-12:
        ratio = revenue_value / unit_price_value
        if ratio > 0:
            return float(ratio)

    return _first_positive_float(weight_lb, qty)


def evaluate_margin_record(
    *,
    protein: Any = None,
    category: Any = None,
    revenue: Any = None,
    cost: Any = None,
    profit: Any = None,
    margin_pct: Any = None,
    unit_cost: Any = None,
    unit_price: Any = None,
    basis_qty: Any = None,
    weight_lb: Any = None,
    qty: Any = None,
    base_cost: Any = None,
    base_unit_cost: Any = None,
    effective_cost: Any = None,
    effective_unit_cost: Any = None,
) -> Dict[str, Any]:
    rule = resolve_margin_rule(protein=protein, category=category)
    revenue_value = _safe_float(revenue)
    current_price = _safe_float(unit_price)

    base_total_cost = _safe_float(base_cost)
    base_unit_cost_value = _safe_float(base_unit_cost)
    effective_total_cost = _safe_float(effective_cost)
    effective_unit_cost_value = _safe_float(effective_unit_cost)

    raw_total_cost = _safe_float(cost)
    raw_unit_cost = _safe_float(unit_cost)
    pricing_basis_qty = _infer_basis_qty(
        basis_qty=basis_qty,
        weight_lb=weight_lb,
        qty=qty,
        cost=base_total_cost if base_total_cost is not None else raw_total_cost,
        unit_cost=base_unit_cost_value if base_unit_cost_value is not None else raw_unit_cost,
        revenue=revenue_value,
        unit_price=current_price,
    )

    if base_total_cost is None:
        base_total_cost = raw_total_cost
    if base_unit_cost_value is None:
        base_unit_cost_value = raw_unit_cost

    if base_unit_cost_value is None and base_total_cost is not None and pricing_basis_qty not in (None, 0):
        base_unit_cost_value = float(base_total_cost / pricing_basis_qty)
    if base_total_cost is None and base_unit_cost_value is not None and pricing_basis_qty not in (None, 0):
        base_total_cost = float(base_unit_cost_value * pricing_basis_qty)

    overhead_cost = float(_FLAT_OVERHEAD_CHARGE) if base_total_cost is not None else None
    if effective_unit_cost_value is None and base_unit_cost_value is not None:
        effective_unit_cost_value = float(base_unit_cost_value + _FLAT_OVERHEAD_CHARGE)
    if effective_total_cost is None:
        if base_total_cost is not None and overhead_cost is not None:
            effective_total_cost = float(base_total_cost + overhead_cost)
        elif effective_unit_cost_value is not None and pricing_basis_qty is not None:
            effective_total_cost = float(effective_unit_cost_value * pricing_basis_qty)

    actual_profit = None
    if revenue_value is not None and effective_total_cost is not None:
        actual_profit = compute_actual_profit(revenue_value, effective_total_cost)
    if actual_profit is None:
        actual_profit = _safe_float(profit)

    actual_margin = None
    if revenue_value is not None and effective_total_cost is not None:
        actual_margin = compute_actual_margin_pct(revenue_value, effective_total_cost)
    if actual_margin is None:
        actual_margin = _safe_float(margin_pct)

    min_margin = _safe_float(rule.get("min_gross_margin_pct"))
    target_margin = _safe_float(rule.get("target_gross_margin_pct"))
    minimum_price = minimum_price_from_cost(effective_unit_cost_value, min_margin)
    target_price = target_price_from_cost(effective_unit_cost_value, target_margin)
    margin_vs_min = (actual_margin - min_margin) if actual_margin is not None and min_margin is not None else None
    margin_vs_target = (actual_margin - target_margin) if actual_margin is not None and target_margin is not None else None
    target_gap = margin_vs_target
    min_gap = margin_vs_min
    target_profit = None
    if effective_total_cost is not None and target_margin is not None:
        margin_ratio = 1.0 - (target_margin / 100.0)
        if margin_ratio > 1e-12:
            target_profit = float((effective_total_cost / margin_ratio) - effective_total_cost)
    elif target_price is not None and pricing_basis_qty not in (None, 0) and effective_total_cost is not None:
        target_profit = float((target_price * pricing_basis_qty) - effective_total_cost)
    elif revenue_value is not None and target_margin is not None:
        target_profit = (revenue_value * target_margin / 100.0)
    uplift_to_target = None
    if target_profit is not None and actual_profit is not None:
        uplift_to_target = max(target_profit - actual_profit, 0.0)
    margin_target_achievement_pct = None
    if actual_margin is not None and target_margin not in (None, 0):
        margin_target_achievement_pct = (actual_margin / target_margin) * 100.0
    min_price_gap = (current_price - minimum_price) if current_price is not None and minimum_price is not None else None
    target_price_gap = (current_price - target_price) if current_price is not None and target_price is not None else None
    target_achievement_pct = None
    if current_price is not None and target_price not in (None, 0):
        target_achievement_pct = (current_price / target_price) * 100.0
    elif margin_target_achievement_pct is not None:
        target_achievement_pct = margin_target_achievement_pct

    status = classify_margin_status(actual_margin, min_margin, target_margin)
    price_status = classify_price_status(current_price, minimum_price, target_price)

    return {
        **rule,
        "flat_overhead_unit_cost": _FLAT_OVERHEAD_CHARGE,
        "pricing_basis_qty": pricing_basis_qty,
        "base_cost_basis": base_total_cost,
        "effective_cost_basis": effective_total_cost,
        "overhead_cost_basis": overhead_cost,
        "base_cost": base_total_cost,
        "effective_cost": effective_total_cost,
        "base_unit_cost": base_unit_cost_value,
        "effective_unit_cost": effective_unit_cost_value,
        "cost": effective_total_cost,
        "profit": actual_profit,
        "margin_pct": actual_margin,
        "unit_cost": effective_unit_cost_value,
        "actual_profit": actual_profit,
        "actual_margin_pct": actual_margin,
        "current_price": current_price,
        "current_sell_price": current_price,
        "minimum_margin_pct": min_margin,
        "target_margin_pct": target_margin,
        "min_price": minimum_price,
        "minimum_price": minimum_price,
        "target_price": target_price,
        "min_price_gap": min_price_gap,
        "minimum_price_gap": min_price_gap,
        "target_price_gap": target_price_gap,
        "margin_vs_min_pp": margin_vs_min,
        "margin_vs_target_pp": margin_vs_target,
        "target_gap_pct_points": target_gap,
        "minimum_gap_pct_points": min_gap,
        "target_profit": target_profit,
        "profit_uplift_to_target": uplift_to_target,
        "target_achievement_pct": target_achievement_pct,
        "target_achievement_rate": target_achievement_pct,
        "margin_target_achievement_pct": margin_target_achievement_pct,
        "margin_band_status": status.get("status_key"),
        "price_band_status": price_status.get("price_status_key"),
        "needs_protein_mapping": not bool(rule.get("mapped")),
        **status,
        **price_status,
    }


def annotate_margin_row(
    row: Mapping[str, Any],
    *,
    protein_keys: Sequence[str] = ("protein_family", "protein", "protein_type", "protein_name", "top_protein_family", "top_protein"),
    category_keys: Sequence[str] = ("product_category", "category", "lead_category"),
    revenue_key: str = "revenue",
    cost_key: str = "cost",
    profit_key: str = "profit",
    margin_key: str = "margin_pct",
    unit_cost_key: str = "unit_cost",
    unit_price_key: str | None = None,
) -> Dict[str, Any]:
    protein = next((row.get(key) for key in protein_keys if key in row and _present_value(row.get(key))), None)
    category = next((row.get(key) for key in category_keys if key in row and _present_value(row.get(key))), None)
    inferred_price_candidates = []
    if unit_price_key:
        inferred_price_candidates.append(unit_price_key)
    if "lb" in str(unit_cost_key or "").lower():
        inferred_price_candidates.extend(["asp_lb", "current_unit_price", "unit_price", "asp", "current_price", "price"])
    else:
        inferred_price_candidates.extend(["current_unit_price", "unit_price", "asp_lb", "asp", "current_price", "price"])
    current_price = _first_present_value(row, inferred_price_candidates)
    base_cost_input = _first_present_value(row, ("base_cost", "base_cost_basis", cost_key))
    base_unit_cost_input = _first_present_value(row, ("base_unit_cost", "base_cost_lb", unit_cost_key, "cost_lb"))
    effective_cost_input = _first_present_value(row, ("effective_cost", "effective_cost_basis"))
    effective_unit_cost_input = _first_present_value(row, ("effective_unit_cost", "effective_cost_lb"))
    basis_qty_input = _first_present_value(row, _QTY_KEYS)
    weight_input = _first_present_value(row, _WEIGHT_KEYS)
    qty_input = _first_present_value(row, ("qty", "quantity", "units", "item_count", "pack_item_count_sum"))
    computed = evaluate_margin_record(
        protein=protein,
        category=category,
        revenue=row.get(revenue_key),
        cost=row.get(cost_key),
        profit=row.get(profit_key),
        margin_pct=row.get(margin_key),
        unit_cost=row.get(unit_cost_key),
        unit_price=current_price,
        basis_qty=basis_qty_input,
        weight_lb=weight_input,
        qty=qty_input,
        base_cost=base_cost_input,
        base_unit_cost=base_unit_cost_input,
        effective_cost=effective_cost_input,
        effective_unit_cost=effective_unit_cost_input,
    )
    out = dict(row)
    out.update(computed)
    out[cost_key] = computed.get("cost")
    out[profit_key] = computed.get("profit")
    out[margin_key] = computed.get("margin_pct")
    out[unit_cost_key] = computed.get("unit_cost")
    minimum_margin_pct = _safe_float(out.get("minimum_margin_pct"))
    target_margin_pct = _safe_float(out.get("target_margin_pct"))
    base_cost_lb = _safe_float(_first_present_value(row, ("base_cost_lb",)))
    input_cost_lb = _safe_float(_first_present_value(row, ("cost_lb",)))
    asp_lb = _safe_float(row.get("asp_lb"))
    if base_cost_lb is None and input_cost_lb is not None and _safe_float(_first_present_value(row, ("effective_cost_lb", "effective_unit_cost"))) is None:
        base_cost_lb = input_cost_lb
    if base_cost_lb is None and (_safe_float(weight_input) or 0.0) > 0:
        base_cost_lb = _safe_float(out.get("base_unit_cost"))
    effective_cost_lb = _safe_float(_first_present_value(row, ("effective_cost_lb", "effective_unit_cost")))
    if effective_cost_lb is None:
        effective_cost_lb = _safe_float(out.get("effective_unit_cost")) or _safe_float(out.get("effective_cost_lb"))
    if effective_cost_lb is None and input_cost_lb is not None:
        effective_cost_lb = float(input_cost_lb + _FLAT_OVERHEAD_CHARGE)
    out["base_cost_lb"] = base_cost_lb
    out["effective_cost_lb"] = effective_cost_lb
    if effective_cost_lb is not None:
        out["cost_lb"] = effective_cost_lb
    if effective_cost_lb is not None or asp_lb is not None:
        minimum_price_lb = minimum_price_from_cost(effective_cost_lb, minimum_margin_pct)
        target_price_lb = target_price_from_cost(effective_cost_lb, target_margin_pct)
        out["current_price_lb"] = asp_lb
        out["minimum_price_lb"] = minimum_price_lb
        out["target_price_lb"] = target_price_lb
        out["asp_lb_gap_to_min"] = (asp_lb - minimum_price_lb) if asp_lb is not None and minimum_price_lb is not None else None
        out["asp_lb_gap_to_target"] = (asp_lb - target_price_lb) if asp_lb is not None and target_price_lb is not None else None
        out["target_achievement_pct_lb"] = (asp_lb / target_price_lb * 100.0) if asp_lb is not None and target_price_lb not in (None, 0) else None
    if out.get("minimum_price") is None and row.get("minimum_price") is not None:
        out["minimum_price"] = row.get("minimum_price")
    if out.get("target_price") is None and row.get("target_price") is not None:
        out["target_price"] = row.get("target_price")
    return out


def annotate_margin_rows(rows: Sequence[Mapping[str, Any]], **kwargs: Any) -> List[Dict[str, Any]]:
    return [annotate_margin_row(row, **kwargs) for row in rows if isinstance(row, Mapping)]


def annotate_margin_frame(
    frame: Any,
    *,
    protein_col: str = "protein_family",
    category_col: str = "product_category",
    revenue_col: str = "revenue",
    cost_col: str = "cost",
    profit_col: str = "profit",
    margin_col: str = "margin_pct",
    unit_cost_col: str = "unit_cost",
    unit_price_col: str | None = None,
    copy: bool = True,
):
    if pd is None or frame is None:
        return frame
    if frame.empty:
        return frame.copy() if copy else frame
    out = frame.copy() if copy else frame
    annotations = [
        annotate_margin_row(
            out.iloc[idx].to_dict(),
            protein_keys=(protein_col,),
            category_keys=(category_col,),
            revenue_key=revenue_col,
            cost_key=cost_col,
            profit_key=profit_col,
            margin_key=margin_col,
            unit_cost_key=unit_cost_col,
            unit_price_key=unit_price_col,
        )
        for idx in range(len(out.index))
    ]
    keys_to_assign: set[str] = set()
    for item in annotations:
        keys_to_assign.update(item.keys())
    for key in keys_to_assign:
        out[key] = [item.get(key) for item in annotations]
    return out


def weighted_target_margin_pct(rows: Sequence[Mapping[str, Any]], *, revenue_key: str = "revenue", target_key: str = "target_margin_pct") -> float | None:
    return weighted_margin_pct(rows, value_key=target_key, revenue_key=revenue_key)


def weighted_minimum_margin_pct(rows: Sequence[Mapping[str, Any]], *, revenue_key: str = "revenue", minimum_key: str = "minimum_margin_pct") -> float | None:
    return weighted_margin_pct(rows, value_key=minimum_key, revenue_key=revenue_key)


def status_meta(key: str | None) -> Dict[str, Any]:
    return dict(_STATUS_META.get(str(key or ""), _STATUS_META["needs_mapping"]))


def sql_normalized_key_expr(expr: str) -> str:
    return (
        "LOWER("
        "REGEXP_REPLACE("
        f"COALESCE(NULLIF(CAST({expr} AS VARCHAR), ''), ''), "
        "'[^a-zA-Z0-9]+', '', 'g'"
        "))"
    )


def sql_margin_rule_expr(protein_expr: str, category_expr: str, field: str, *, fallback: str = "NULL") -> str:
    protein_norm = sql_normalized_key_expr(protein_expr)
    category_norm = sql_normalized_key_expr(category_expr)
    cases: List[str] = []
    for source_expr in (protein_norm, category_norm):
        for rule_key, rule in _RULES_BY_KEY.items():
            aliases = sorted(_ALIASES_BY_KEY.get(rule_key) or {rule_key})
            alias_sql = ", ".join(f"'{alias}'" for alias in aliases)
            value = getattr(rule, field)
            if value is None:
                value_sql = "NULL"
            elif isinstance(value, str):
                escaped = value.replace("'", "''")
                value_sql = f"'{escaped}'"
            else:
                value_sql = str(float(value))
            cases.append(f"WHEN {source_expr} IN ({alias_sql}) THEN {value_sql}")
    return f"(CASE {' '.join(cases)} ELSE {fallback} END)"


def sql_rule_family_expr(protein_expr: str, category_expr: str, *, fallback: str = f"'{_DQ_BUCKET}'") -> str:
    protein_norm = sql_normalized_key_expr(protein_expr)
    category_norm = sql_normalized_key_expr(category_expr)
    cases: List[str] = []
    for source_expr in (protein_norm, category_norm):
        for rule_key, rule in _RULES_BY_KEY.items():
            aliases = sorted(_ALIASES_BY_KEY.get(rule_key) or {rule_key})
            alias_sql = ", ".join(f"'{alias}'" for alias in aliases)
            escaped_family = rule.family.replace("'", "''")
            cases.append(f"WHEN {source_expr} IN ({alias_sql}) THEN '{escaped_family}'")
    return f"(CASE {' '.join(cases)} ELSE {fallback} END)"
