from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import date, datetime, timezone, timedelta
from dataclasses import replace, is_dataclass
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd
from flask import current_app

from app.core.cache_manager import TTLValueCache
from app.services import fact_schema as fs
from app.services import fact_store
from app.services import margin_rules


def _safe_col(cols: set[str], *candidates: str) -> str | None:
    for cand in candidates:
        if cand in cols:
            return cand
    return None


def _coalesce_text_expr(cols: set[str], *candidates: str, default: str = "NULL") -> str:
    expressions: List[str] = []
    for cand in candidates:
        if cand not in cols:
            continue
        expressions.append(f"NULLIF(CAST({cand} AS VARCHAR), '')")
    if not expressions:
        return default
    return f"COALESCE({', '.join(expressions + [default])})"


def _coalesce_numeric_expr(cols: set[str], candidates: Sequence[str], *, default: str = "NULL") -> str:
    expressions: List[str] = []
    seen: set[str] = set()
    for cand in candidates:
        if cand not in cols or cand in seen:
            continue
        seen.add(cand)
        expressions.append(f"CAST({cand} AS DOUBLE)")
    if not expressions:
        return default
    return f"COALESCE({', '.join(expressions + [default])})"


# Cross-sell cache (12h TTL enforced at call site)
CROSS_SELL_CACHE = TTLValueCache(maxsize=256)


def _pagination(args: Any, default_size: int = 25, max_size: int = 5000) -> Tuple[int, int]:
    try:
        page = max(1, int(args.get("page", 1)))
    except Exception:
        page = 1
    try:
        size = int(args.get("page_size") or args.get("per_page") or default_size)
    except Exception:
        size = default_size
    size = max(1, min(size, max_size))
    return page, size


def _sort_fields(args: Any) -> Tuple[str, bool]:
    sort_raw = (args.get("sort") or args.get("sort_by") or "revenue").lower()
    dir_raw = (args.get("sort_dir") or args.get("direction") or args.get("dir") or "desc").lower()
    mapping = {
        "revenue": "revenue",
        "revenue_prior": "revenue_prior_window",
        "revenue_prior_window": "revenue_prior_window",
        "delta_revenue": "delta_revenue",
        "delta_revenue_pct": "delta_revenue_pct",
        "profit": "profit",
        "margin": "margin_pct",
        "margin_pct": "margin_pct",
        "orders": "orders",
        "orders_prior": "orders_prior_window",
        "orders_prior_window": "orders_prior_window",
        "delta_orders": "delta_orders",
        "delta_orders_pct": "delta_orders_pct",
        "orders_last_30": "orders_last_30",
        "orders_90": "orders_last_90",
        "orders_last_90": "orders_last_90",
        "qty": "qty",
        "days_since": "days_since_last",
        "days_since_last_order": "days_since_last",
        "days_since_last": "days_since_last",
        "aov": "avg_order_value",
        "avg_order_value": "avg_order_value",
        "asp": "asp",
        "segment": "segment_label",
        "churn_risk": "churn_risk_band",
        "risk": "churn_risk_band",
        "first_order": "first_order",
        "name": "customer_name",
        "customer": "customer_name",
        "last_order": "last_order",
    }
    col = mapping.get(sort_raw, "revenue")
    ascending = dir_raw in {"asc", "ascending", "up", "1"}
    return col, ascending


def _clean_float(val: Any) -> float:
    try:
        fval = float(val)
        if math.isnan(fval):
            return 0.0
        return fval
    except Exception:
        return 0.0


def _clean_optional_float(val: Any) -> float | None:
    raw = _none_if_na(val)
    if raw is None:
        return None
    try:
        fval = float(raw)
        if math.isnan(fval):
            return None
        return fval
    except Exception:
        return None


def _clean_numeric_series(series: pd.Series, *, default: float | None = 0.0) -> pd.Series:
    values: List[float] = []
    fallback = np.nan if default is None else float(default)
    for raw in series.tolist():
        if type(raw).__name__ == "_NoValueType":
            values.append(fallback)
            continue
        try:
            fval = float(raw)
            if math.isnan(fval):
                values.append(fallback)
            else:
                values.append(fval)
        except Exception:
            values.append(fallback)
    return pd.Series(values, index=series.index, dtype="float64")


def _clean_datetime_series(series: pd.Series) -> pd.Series:
    values: List[Any] = []
    for raw in series.tolist():
        if type(raw).__name__ == "_NoValueType" or raw in (None, ""):
            values.append(pd.NaT)
            continue
        try:
            ts = pd.Timestamp(raw)
            values.append(ts if pd.notna(ts) else pd.NaT)
        except Exception:
            values.append(pd.NaT)
    return pd.Series(values, index=series.index, dtype="datetime64[ns]")


def _iso_date_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    ts = pd.to_datetime(value, errors="coerce")
    if pd.notna(ts):
        return ts.date().isoformat()
    text = str(value).strip()
    return text or None


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        if val is None or pd.isna(val):
            return default
    except Exception:
        if val is None:
            return default
    try:
        return int(val)
    except Exception:
        return default


def _none_if_na(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, (list, tuple, dict, set)):
        return val
    try:
        return None if pd.isna(val) else val
    except Exception:
        return val


def _scope_subset(df: pd.DataFrame, scope_value: str) -> pd.DataFrame:
    if df is None or df.empty:
        cols = list(df.columns) if isinstance(df, pd.DataFrame) else []
        return pd.DataFrame(columns=cols)
    cols = list(df.columns)
    target = str(scope_value or "").strip().lower()
    try:
        records = df.to_dict(orient="records")
    except Exception:
        return pd.DataFrame(columns=cols)
    keep: List[Dict[str, Any]] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        scope_raw = _none_if_na(rec.get("scope"))
        if str(scope_raw or "").strip().lower() == target:
            keep.append(rec)
    return pd.DataFrame.from_records(keep, columns=cols)


def _numeric_values(seq: Any) -> List[float]:
    out: List[float] = []
    if seq is None:
        return out
    try:
        iterator = list(seq)
    except Exception:
        iterator = [seq]
    for raw in iterator:
        val = _none_if_na(raw)
        if val is None:
            continue
        try:
            fval = float(val)
        except Exception:
            continue
        if math.isnan(fval):
            continue
        out.append(fval)
    return out


def _mean_numeric(seq: Any) -> float:
    vals = _numeric_values(seq)
    return (sum(vals) / len(vals)) if vals else 0.0


def _median_numeric(seq: Any) -> float:
    vals = _numeric_values(seq)
    if not vals:
        return 0.0
    return float(pd.Series(vals).median())


def _std_numeric(seq: Any) -> float:
    vals = _numeric_values(seq)
    if len(vals) < 2:
        return 0.0
    return float(pd.Series(vals).std(ddof=0))


def _sum_numeric(seq: Any) -> float:
    vals = _numeric_values(seq)
    return sum(vals) if vals else 0.0


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on", "y"}


def _normalize_requested_sections(requested_sections: Sequence[str] | None) -> set[str] | None:
    if not requested_sections:
        return None
    aliases = {
        "all": "all",
        "full": "all",
        "overview": "overview",
        "summary": "overview",
        "kpis": "overview",
        "clv": "clv",
        "rfm": "rfm",
        "cohort": "cohorts",
        "cohorts": "cohorts",
    }
    normalized: set[str] = set()
    for raw in requested_sections:
        if raw is None:
            continue
        parts = raw if isinstance(raw, (list, tuple, set)) else str(raw).split(",")
        for part in parts:
            token = str(part or "").strip().lower().replace("-", "_")
            if not token:
                continue
            resolved = aliases.get(token)
            if resolved == "all":
                return None
            if resolved:
                normalized.add(resolved)
    return normalized or None


def _empty_rfm_payload() -> Dict[str, Any]:
    return {
        "settings": {},
        "filters": {},
        "insights": {},
        "risk_opportunity": {},
        "segment_summary": [],
        "segment_leaderboard": [],
        "segment_playbooks": [],
        "matrix": {"rows": [], "selected_cell": {"r_score": None, "f_score": None}},
        "histograms": {"recency": [], "monetary": []},
        "segment_table": {"rows": [], "total_rows": 0},
        "customers_table": {"rows": [], "total_rows": 0, "page": 1, "page_size": 25, "total_pages": 0},
        "scatter_v2": {
            "mode": "frequency_monetary",
            "quadrants": {"frequency_median": 0.0, "monetary_median": 0.0, "recency_median": 0.0},
            "points": [],
        },
        "donut": [],
        "exports": {
            "datasets": [
                "customers_full",
                "top_customers",
                "segments",
                "segment_leaderboard",
                "matrix_cells",
                "heatmap_customers",
            ]
        },
        "segments": [],
        "top": [],
        "scatter": {"frequency": [], "monetary": [], "labels": [], "scores": [], "segments": [], "recency": []},
        "updated_at": date.today().isoformat(),
    }


def _empty_clv_payload(
    *,
    window_start_ts: pd.Timestamp,
    window_end_ts: pd.Timestamp,
    prior_start_ts: pd.Timestamp,
    prior_end_ts: pd.Timestamp,
    cost_coverage_pct: float | None,
) -> Dict[str, Any]:
    return {
        "settings": {
            "lookback_months": 12,
            "horizon_months": 12,
            "discount_rate_pct": 8.0,
            "requested_monetary_basis": "gross_profit",
            "monetary_basis": "revenue",
            "frequency_basis": "orders_year",
            "retention_model": "simple",
            "lookback_start": window_start_ts.date().isoformat(),
            "lookback_end": window_end_ts.date().isoformat(),
            "prior_start": prior_start_ts.date().isoformat(),
            "prior_end": prior_end_ts.date().isoformat(),
            "monetary_caveat": None,
            "params_hash": None,
            "computed_window_note": "",
            "no_activity_policy": "excluded",
            "cost_coverage_pct": cost_coverage_pct,
        },
        "filters": {},
        "cards": {},
        "concentration": {},
        "clv_at_risk": {},
        "margin_leverage": {},
        "segment_summary": [],
        "segment_leaderboard": [],
        "segment_playbooks": [],
        "leaderboard": [],
        "at_risk_high_value": [],
        "customers_table": {"rows": [], "total_rows": 0, "page": 1, "page_size": 25, "total_pages": 0},
        "charts": {
            "recency_distribution": [],
            "clv_distribution": [],
            "clv_vs_churn": {"points": []},
            "revenue_orders": {"points": [], "quadrants": {"revenue_median": 0.0, "orders_median": 0.0}},
            "margin_top_customers": {"labels": [], "values": []},
        },
        "exports": {"datasets": ["customers", "segments", "at_risk_high_value"]},
        "top": [],
        "histogram": [],
        "avg_clv": 0.0,
    }


def _empty_cohorts_payload() -> Dict[str, Any]:
    return {"matrix": [], "sizes": [], "heatmap": {"x": [], "y": [], "z": [], "rows": []}}


def _parse_optional_int(value: Any) -> int | None:
    if value in (None, "", "none", "null"):
        return None
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _safe_pct(numerator: float, denominator: float) -> float | None:
    if not denominator:
        return None
    return float(numerator / denominator * 100.0)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _utc_today_ts_naive() -> pd.Timestamp:
    """Return UTC 'today' as a timezone-naive normalized pandas timestamp."""
    return pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()


def _to_timestamp(value: Any) -> pd.Timestamp | None:
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return None
    return ts if pd.notna(ts) else None


def _coerce_window_bounds(start_iso: Any, end_iso: Any, ref_date: date) -> tuple[pd.Timestamp, pd.Timestamp]:
    start_ts = _to_timestamp(start_iso)
    end_ts = _to_timestamp(end_iso)
    if start_ts is None and end_ts is None:
        end_ts = pd.Timestamp(ref_date)
        start_ts = end_ts - pd.Timedelta(days=89)
    elif start_ts is None and end_ts is not None:
        start_ts = end_ts - pd.Timedelta(days=89)
    elif start_ts is not None and end_ts is None:
        end_ts = start_ts + pd.Timedelta(days=89)
    if start_ts is None:
        start_ts = pd.Timestamp(ref_date) - pd.Timedelta(days=89)
    if end_ts is None:
        end_ts = pd.Timestamp(ref_date)
    if end_ts < start_ts:
        start_ts, end_ts = end_ts, start_ts
    return start_ts.normalize(), end_ts.normalize()


def _churn_risk_band(days_since: Any) -> str:
    days = _clean_float(days_since)
    if days >= 90:
        return "High"
    if days >= 60:
        return "Medium"
    return "Low"


def _customer_segment_label(
    *,
    revenue: float,
    revenue_prior: float,
    days_since: float | None,
    first_order: Any,
    window_start: pd.Timestamp,
) -> str:
    rev = max(0.0, float(revenue or 0.0))
    prior = max(0.0, float(revenue_prior or 0.0))
    days_val = None if days_since is None else float(days_since)
    first_ts = _to_timestamp(first_order)

    if rev <= 0.0 and prior > 0.0:
        return "Churned"
    if rev > 0.0 and prior <= 0.0:
        if first_ts is not None and first_ts >= window_start:
            return "New"
        return "Reactivated"

    if rev <= 0.0 and prior <= 0.0:
        return "Stable"

    delta_pct = ((rev - prior) / prior * 100.0) if prior > 0.0 else None
    if days_val is not None and days_val >= 90:
        return "At Risk"
    if delta_pct is not None and delta_pct >= 15.0:
        return "Growing"
    if delta_pct is not None and delta_pct <= -15.0:
        return "At Risk"
    return "Stable"


def _safe_ratio_value(numerator: Any, denominator: Any, *, pct: bool = False) -> float | None:
    try:
        denom = float(denominator)
    except Exception:
        return None
    if denom == 0:
        return None
    try:
        value = float(numerator) / denom
    except Exception:
        return None
    return value * 100.0 if pct else value


def _count_positive(values: Sequence[Any]) -> int:
    count = 0
    for raw in values:
        try:
            if float(raw) > 0:
                count += 1
        except Exception:
            continue
    return count


def _chart_state(status: str, reason: str | None = None, *, scope: str | None = None) -> Dict[str, Any]:
    state: Dict[str, Any] = {"status": str(status or "empty")}
    if reason:
        state["reason"] = str(reason)
    if scope:
        state["scope"] = str(scope)
    return state


def _clamp_score(score: Any) -> float | None:
    if score is None:
        return None
    try:
        val = float(score)
    except Exception:
        return None
    if math.isnan(val):
        return None
    return max(0.0, min(100.0, val))


def _score_band(score: Any) -> str:
    val = _clamp_score(score)
    if val is None:
        return "Unknown"
    if val >= 85:
        return "Leading"
    if val >= 70:
        return "Strong"
    if val >= 55:
        return "Stable"
    if val >= 40:
        return "Watch"
    return "Risk"


def _score_tone(score: Any) -> str:
    val = _clamp_score(score)
    if val is None:
        return "neutral"
    if val >= 70:
        return "positive"
    if val >= 50:
        return "neutral"
    if val >= 35:
        return "warning"
    return "danger"


def _metric_item(
    label: str,
    value: Any,
    *,
    fmt: str = "number",
    detail: str | None = None,
    scope: str | None = None,
    tone: str = "neutral",
    suffix: str | None = None,
    emphasize: bool = False,
) -> Dict[str, Any]:
    return {
        "label": label,
        "value": _none_if_na(value),
        "format": fmt,
        "detail": detail,
        "scope": scope,
        "tone": tone,
        "suffix": suffix,
        "emphasize": bool(emphasize),
    }


def _score_item(
    key: str,
    label: str,
    score: Any,
    detail: str,
    *,
    implication: str | None = None,
    risk: bool = False,
) -> Dict[str, Any]:
    clean = _clamp_score(score)
    tone_score = (100.0 - clean) if (risk and clean is not None) else clean
    return {
        "key": key,
        "label": label,
        "score": round(clean, 1) if clean is not None else None,
        "detail": detail,
        "implication": implication,
        "band": _score_band(tone_score),
        "tone": _score_tone(tone_score),
        "risk": bool(risk),
    }


def _action_item(
    *,
    lane: str,
    action_type: str,
    title: str,
    why: str,
    urgency: str,
    confidence: float | None,
    owner: str | None,
    related_products: Sequence[str] | None = None,
    related_categories: Sequence[str] | None = None,
    related_families: Sequence[str] | None = None,
    revenue_upside: float | None = None,
    profit_upside: float | None = None,
    margin_upside_pp: float | None = None,
    scope_label: str | None = None,
    pathway_label: str | None = None,
    tone: str = "neutral",
    priority_score: float | None = None,
) -> Dict[str, Any]:
    return {
        "lane": lane,
        "type": action_type,
        "title": title,
        "why": why,
        "detail": why,
        "urgency": urgency,
        "confidence": round(float(confidence), 1) if confidence is not None else None,
        "owner": owner,
        "related_products": [str(v) for v in (related_products or []) if str(v or "").strip()],
        "related_categories": [str(v) for v in (related_categories or []) if str(v or "").strip()],
        "related_families": [str(v) for v in (related_families or []) if str(v or "").strip()],
        "revenue_upside": _none_if_na(revenue_upside),
        "profit_upside": _none_if_na(profit_upside),
        "margin_upside_pp": _none_if_na(margin_upside_pp),
        "scope_label": scope_label,
        "pathway_label": pathway_label,
        "tone": tone,
        "priority_score": round(float(priority_score), 1) if priority_score is not None else None,
    }


def _rolling_average(values: Sequence[Any], window: int = 3) -> List[float | None]:
    if window <= 1:
        return [float(v) if v is not None else None for v in values]
    cleaned = [_none_if_na(v) for v in values]
    out: List[float | None] = []
    for idx in range(len(cleaned)):
        subset = [float(v) for v in cleaned[max(0, idx - window + 1) : idx + 1] if v is not None]
        out.append((sum(subset) / len(subset)) if subset else None)
    return out


def _top_n_share(records: Sequence[Dict[str, Any]], key: str, top_n: int = 1) -> float | None:
    values = sorted([float(rec.get(key) or 0.0) for rec in records if isinstance(rec, dict)], reverse=True)
    total = sum(values)
    if total <= 0:
        return None
    return sum(values[: max(int(top_n), 1)]) / total * 100.0


def _active_streak_months(month_labels: Sequence[str]) -> int:
    stamps: List[pd.Timestamp] = []
    for raw in month_labels:
        ts = pd.to_datetime(raw, errors="coerce")
        if pd.notna(ts):
            stamps.append(ts.to_period("M").to_timestamp())
    if not stamps:
        return 0
    stamps = sorted(set(stamps))
    streak = 1
    for idx in range(len(stamps) - 1, 0, -1):
        if (stamps[idx].year - stamps[idx - 1].year) * 12 + (stamps[idx].month - stamps[idx - 1].month) == 1:
            streak += 1
        else:
            break
    return streak


def _month_name(ts: pd.Timestamp | None) -> str | None:
    if ts is None or pd.isna(ts):
        return None
    try:
        return str(ts.strftime("%b"))
    except Exception:
        return None


def _series_direction(current: Any, prior: Any, *, threshold_pct: float = 5.0) -> str:
    pct = _safe_ratio_value((_clean_float(current) - _clean_float(prior)), prior, pct=True)
    if pct is None:
        return "stable"
    if pct >= threshold_pct:
        return "growing"
    if pct <= -threshold_pct:
        return "declining"
    return "stable"


def _segment_filter_value(segment_label: str) -> str:
    return str(segment_label or "").strip().lower().replace(" ", "_")


def _definition_payload() -> Dict[str, Any]:
    return {
        "kpis": {
            "nrr": "Net Revenue Retention = current-window revenue from prior-window customers divided by prior-window revenue.",
            "grr": "Gross Revenue Retention = retained revenue from prior customers capped at prior spend, divided by prior-window revenue.",
            "growth_composition": "Composition of current-window customers split into New, Returning, and Reactivated.",
            "concentration": "Dependency on large customers using Top 1/Top 5 share and Herfindahl-Hirschman Index (HHI).",
            "profitability_dispersion": "Margin distribution across customers: P10, P50, P90 and negative-margin concentration.",
            "lifetime_proxy": "Average tenure in days from first observed order to current window reference date.",
            "revenue_at_stake": "Revenue at stake = sum of last-90-day revenue for high churn-risk customers.",
            "frequency_recency": "Median days between consecutive customer orders in the selected window.",
            "basket_quality": "Median AOV, units/order, and weight/order across customers.",
            "cost_coverage_pct": "Share of revenue with non-missing cost on source rows.",
        },
        "rfm": {
            "recency": "Recency = days since last purchase (lower is better).",
            "frequency": "Frequency = count of distinct orders in the RFM lookback window.",
            "monetary": "Monetary = sum of selected value metric (Revenue/Profit/Gross profit proxy) in lookback.",
            "score_scale": "R, F, and M each use a 1-5 scale where 5 is best.",
            "score_method_quantile": "Quantile scoring assigns 1-5 scores using the current customer distribution.",
            "score_method_fixed": "Fixed threshold scoring assigns 1-5 scores using configured cutoffs.",
            "matrix": "R x F matrix cell shows customer count and revenue share for that score combination.",
            "segments": "Core segments: Champions, Loyal, Big Spenders, Potential Loyalists, New Customers, Promising, Needs Attention, About to Sleep, At Risk, Can't Lose Them, Hibernating, Lost.",
            "repeat_rate": "Repeat rate = share of customers with frequency > 1 in the lookback window.",
            "reactivation_rate": "Reactivation rate = customers active in lookback with zero prior-window revenue and history before lookback.",
            "at_risk_revenue_stake": "At-risk revenue at stake = sum of prior-window revenue for At Risk / Can't Lose Them segments.",
        },
        "clv": {
            "overview": "Expected value over selected horizon using observed order value, purchase frequency, and retention proxy.",
            "formula": "CLV = (AOV × Orders/Year × BasisFactor) × HorizonYears × RetentionFactor × DiscountFactor.",
            "recency": "Recency = days since last order in the selected window.",
            "frequency": "Frequency = distinct order count in lookback, normalized to monthly/yearly cadence.",
            "monetary_basis": "Monetary basis can be Revenue or Gross Profit (falls back to Revenue when cost coverage is low).",
            "retention_simple": "Simple retention uses repeat behavior and recency as a transparent proxy.",
            "retention_advanced": "Advanced retention uses a stricter recency-driven churn proxy.",
            "clv_at_risk": "CLV at risk = CLV (12m) × churn probability proxy.",
            "concentration": "Top1/Top10 CLV share and HHI measure dependency on a small set of customers.",
            "margin_leverage": "Margin leverage estimates profit uplift if low-margin high-CLV accounts reach target margin.",
        },
        "columns": {
            "first_order": "First observed order date for this customer in the filtered context.",
            "last_order": "Most recent order date for this customer in the filtered context.",
            "days_since_last_order": "Days from last order to the selected window end date.",
            "revenue_prior_window": "Customer revenue in the previous window with matching duration.",
            "delta_revenue": "Current-window revenue minus previous-window revenue.",
            "delta_revenue_pct": "Percent change versus previous window. Blank when previous window revenue is zero.",
            "margin_pct": "Profit as a percent of revenue.",
            "orders_last_90": "Distinct orders in the last 90 days from the window reference date.",
            "asp": "Average selling price (revenue divided by quantity).",
            "segment_label": "Operational segment based on recency and revenue trajectory (New/Growing/Stable/At Risk/Churned/Reactivated).",
        },
    }


def _rfm_scores(recency: pd.Series, frequency: pd.Series, monetary: pd.Series) -> pd.DataFrame:
    def qscore(series: pd.Series, reverse: bool = False) -> pd.Series:
        s = pd.to_numeric(series, errors="coerce")
        valid = s.dropna()
        if valid.empty:
            return pd.Series(1, index=s.index, dtype=int)
        if valid.nunique() <= 1:
            return pd.Series(1, index=s.index, dtype=int)

        n_unique = valid.nunique()
        n_quantiles = min(4, n_unique)
        try:
            if n_quantiles < 4:
                ranks = s.rank(method="average", na_option="keep")
                labels = list(range(1, n_quantiles + 1))
                bins = pd.qcut(ranks, q=n_quantiles, labels=labels, duplicates="drop")
                bins = bins.astype("float")
                if n_quantiles > 1:
                    bins = ((bins - 1) / (n_quantiles - 1) * 3 + 1).round().fillna(1)
                else:
                    bins = bins.fillna(1)
            else:
                bins = pd.qcut(s, q=4, labels=[1, 2, 3, 4], duplicates="drop")
                bins = bins.astype("float").fillna(1)
            bins = bins.astype(int)
            return (5 - bins) if reverse else bins
        except Exception:
            ranks = s.rank(method="average", na_option="keep", pct=True)
            scores = (ranks * 3 + 1).round().fillna(1).astype(int)
            scores = scores.clip(1, 4)
            return (5 - scores) if reverse else scores

    r = qscore(recency, reverse=True)
    f = qscore(frequency, reverse=False)
    m = qscore(monetary, reverse=False)
    out = pd.DataFrame({"r_score": r, "f_score": f, "m_score": m})
    out["rfm_score"] = out[["r_score", "f_score", "m_score"]].sum(axis=1)
    return out


def _segment(score: float) -> str:
    if score >= 10:
        return "Champion"
    if score >= 8:
        return "Loyal"
    if score >= 6:
        return "At Risk"
    return "Dormant"


def _parse_thresholds_csv(raw: Any, default_values: Sequence[float]) -> List[float]:
    if raw in (None, ""):
        return [float(v) for v in default_values]
    if isinstance(raw, (list, tuple)):
        parts = [str(v).strip() for v in raw]
    else:
        parts = [p.strip() for p in str(raw).split(",")]
    values: List[float] = []
    for part in parts:
        if not part:
            continue
        try:
            values.append(float(part))
        except Exception:
            continue
    if len(values) < 4:
        return [float(v) for v in default_values]
    values = sorted(values[:4])
    return [float(v) for v in values]


def _rfm_score_quantile(series: pd.Series, *, reverse: bool) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    valid = s.dropna()
    if valid.empty:
        return pd.Series(1, index=s.index, dtype="int64")
    if valid.nunique() <= 1:
        score = 5 if reverse else 1
        return pd.Series(score, index=s.index, dtype="int64")
    ranks = s.rank(method="average", na_option="keep", pct=True)
    bins = (ranks * 5.0).apply(np.ceil)
    bins = bins.clip(lower=1, upper=5).fillna(1).astype("int64")
    if reverse:
        return (6 - bins).astype("int64")
    return bins.astype("int64")


def _rfm_score_fixed(series: pd.Series, thresholds: Sequence[float], *, reverse: bool) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    t = list(thresholds[:4]) if thresholds else []
    if len(t) < 4:
        t = [30.0, 60.0, 90.0, 120.0] if reverse else [1.0, 2.0, 4.0, 8.0]
    out = pd.Series(1, index=s.index, dtype="int64")
    if reverse:
        out = pd.Series(1, index=s.index, dtype="int64")
        out[s <= t[3]] = 2
        out[s <= t[2]] = 3
        out[s <= t[1]] = 4
        out[s <= t[0]] = 5
    else:
        out = pd.Series(1, index=s.index, dtype="int64")
        out[s > t[0]] = 2
        out[s > t[1]] = 3
        out[s > t[2]] = 4
        out[s > t[3]] = 5
    out[s.isna()] = 1
    return out.astype("int64")


def _rfm_scores_v2(
    recency: pd.Series,
    frequency: pd.Series,
    monetary: pd.Series,
    *,
    method: str = "quantile",
    recency_thresholds: Sequence[float] | None = None,
    frequency_thresholds: Sequence[float] | None = None,
    monetary_thresholds: Sequence[float] | None = None,
) -> pd.DataFrame:
    method_norm = str(method or "quantile").strip().lower()
    use_fixed = method_norm == "fixed"
    if use_fixed:
        r = _rfm_score_fixed(recency, recency_thresholds or [30.0, 60.0, 90.0, 120.0], reverse=True)
        f = _rfm_score_fixed(frequency, frequency_thresholds or [1.0, 2.0, 4.0, 8.0], reverse=False)
        m = _rfm_score_fixed(monetary, monetary_thresholds or [1000.0, 5000.0, 15000.0, 30000.0], reverse=False)
    else:
        r = _rfm_score_quantile(recency, reverse=True)
        f = _rfm_score_quantile(frequency, reverse=False)
        m = _rfm_score_quantile(monetary, reverse=False)
    out = pd.DataFrame({"r_score": r, "f_score": f, "m_score": m})
    out["rfm_score"] = out[["r_score", "f_score", "m_score"]].sum(axis=1).astype("int64")
    return out


def _rfm_segment_v2(r_score: int, f_score: int, m_score: int, recency_days: float | None) -> str:
    r = int(r_score or 1)
    f = int(f_score or 1)
    m = int(m_score or 1)
    recency = float(recency_days) if recency_days is not None and not pd.isna(recency_days) else None

    if r >= 5 and f >= 4 and m >= 4:
        return "Champions"
    if r <= 1 and (f >= 4 or m >= 4):
        return "Can't Lose Them"
    if r >= 4 and f >= 4:
        return "Loyal"
    if r >= 4 and m >= 4 and f >= 2:
        return "Big Spenders"
    if r >= 4 and f == 3:
        return "Potential Loyalists"
    if r >= 5 and f <= 2:
        return "New Customers"
    if r >= 4 and f <= 2:
        return "Promising"
    if r == 3 and f >= 3:
        return "Needs Attention"
    if r in {2, 3} and f <= 2:
        return "About to Sleep"
    if r <= 2 and (f >= 4 or m >= 4):
        return "At Risk"
    if r <= 1 and f <= 2 and m <= 2:
        if recency is not None and recency >= 180:
            return "Lost"
        return "Hibernating"
    if r <= 2 and f <= 3:
        return "Hibernating"
    return "Needs Attention"


def _rfm_playbook_actions(segment: str) -> List[str]:
    playbooks: Dict[str, List[str]] = {
        "Champions": [
            "Protect with priority service and proactive inventory recommendations.",
            "Offer strategic upsell bundles tied to recent purchase categories.",
            "Use as references for win-back outreach to similar accounts.",
        ],
        "Loyal": [
            "Increase cadence with scheduled reorder prompts.",
            "Cross-sell adjacent products based on recent mix.",
            "Review pricing/margin quarterly to protect profitability.",
        ],
        "Big Spenders": [
            "Create account plan focused on margin-safe expansion.",
            "Assign ownership for monthly executive check-ins.",
            "Bundle freight or service incentives for contract renewals.",
        ],
        "Potential Loyalists": [
            "Push second and third purchase campaigns within 30 days.",
            "Offer low-friction add-ons aligned to prior baskets.",
            "Use sales reminders around expected reorder cycle.",
        ],
        "New Customers": [
            "Run onboarding sequence with first 60-day success milestones.",
            "Set follow-up tasks after first order delivery confirmation.",
            "Offer starter bundles to increase frequency safely.",
        ],
        "Promising": [
            "Trigger nurture outreach with next-best-product suggestions.",
            "Send reorder prompts before expected stock-out dates.",
            "Monitor for conversion into Potential Loyalists.",
        ],
        "Needs Attention": [
            "Review account health and remove ordering blockers.",
            "Target with offers tied to previously high-performing SKUs.",
            "Escalate outreach if no activity in next cycle.",
        ],
        "About to Sleep": [
            "Launch re-engagement call list prioritized by prior revenue.",
            "Use limited-time reorder incentives.",
            "Route to manager if inactivity crosses 90 days.",
        ],
        "At Risk": [
            "Immediate outreach with account-specific win-back proposal.",
            "Prioritize service recovery and fulfillment reliability checks.",
            "Track weekly until recency improves.",
        ],
        "Can't Lose Them": [
            "Escalate to senior owner with 7-day action plan.",
            "Audit price, service, and product availability gaps.",
            "Create retention offer tied to historic spend profile.",
        ],
        "Hibernating": [
            "Move to low-cost automated reactivation journeys.",
            "Retarget with category-specific reminders quarterly.",
            "Requalify account ownership before manual outreach.",
        ],
        "Lost": [
            "Run structured win-back campaign with business review.",
            "Suppress from high-cost outreach if repeated inactivity persists.",
            "Capture churn reason in CRM for root-cause analysis.",
        ],
    }
    return playbooks.get(segment, ["Review account and assign targeted follow-up action."])


def _rfm_playbook_goal(segment: str) -> str:
    goal_map = {
        "Champions": "Retain and grow",
        "Loyal": "Retain and expand",
        "Big Spenders": "Protect and grow",
        "Potential Loyalists": "Convert to loyal",
        "New Customers": "Onboard and activate",
        "Promising": "Increase frequency",
        "Needs Attention": "Stabilize engagement",
        "About to Sleep": "Win back now",
        "At Risk": "Prevent churn",
        "Can't Lose Them": "Executive rescue",
        "Hibernating": "Low-cost reactivation",
        "Lost": "Selective win-back",
    }
    return goal_map.get(segment, "Review and assign next best action")


def _parse_rfm_segments(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        source = []
        for item in raw:
            source.extend(str(item).split(","))
    else:
        source = str(raw).split(",")
    segments = [str(item).strip() for item in source if str(item).strip()]
    seen = set()
    out: List[str] = []
    for seg in segments:
        key = seg.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(seg)
    return out


def _parse_score_bound(raw: Any, default: int, *, minimum: int = 1, maximum: int = 5) -> int:
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def _build_histogram_payload(values: pd.Series, *, weights: pd.Series | None = None, max_bins: int = 10) -> List[Dict[str, Any]]:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if numeric.empty:
        return []
    unique_values = numeric.nunique()
    bins = int(max(4, min(max_bins, unique_values)))
    if bins <= 1:
        bins = 4
    counts, edges = np.histogram(numeric.to_numpy(dtype="float64"), bins=bins)

    weighted: List[float] = [0.0 for _ in range(len(counts))]
    if weights is not None:
        paired = pd.DataFrame({"value": pd.to_numeric(values, errors="coerce"), "weight": pd.to_numeric(weights, errors="coerce")})
        paired = paired.dropna(subset=["value"])
        if not paired.empty:
            inds = np.digitize(paired["value"].to_numpy(dtype="float64"), edges, right=False) - 1
            inds = np.clip(inds, 0, len(counts) - 1)
            for idx, w in zip(inds.tolist(), paired["weight"].fillna(0.0).to_numpy(dtype="float64").tolist(), strict=False):
                weighted[idx] += float(w)

    rows: List[Dict[str, Any]] = []
    for i in range(len(counts)):
        rows.append(
            {
                "bin_start": float(edges[i]),
                "bin_end": float(edges[i + 1]),
                "count": int(counts[i]),
                "weight": float(weighted[i]),
            }
        )
    return rows


def _parse_rfm_settings(
    args: Any,
    *,
    window_end_ts: pd.Timestamp,
    cost_available: bool,
    cost_coverage_pct: float | None,
) -> Dict[str, Any]:
    lookback_raw = _safe_int(args.get("rfm_lookback_months") or args.get("lookback_months"), 12)
    lookback_months = lookback_raw if lookback_raw in {6, 12, 24, 36} else 12

    scoring_method_raw = str(args.get("rfm_scoring_method") or args.get("scoring_method") or "quantile").strip().lower()
    scoring_method = scoring_method_raw if scoring_method_raw in {"quantile", "fixed"} else "quantile"

    monetary_raw = str(args.get("rfm_monetary_metric") or args.get("monetary_metric") or "revenue").strip().lower()
    requested_metric = monetary_raw if monetary_raw in {"revenue", "profit", "gross_profit"} else "revenue"
    monetary_metric = requested_metric
    coverage_ok = bool(cost_available) and ((cost_coverage_pct is None) or (float(cost_coverage_pct) >= 85.0))
    caveat: str | None = None
    if requested_metric in {"profit", "gross_profit"} and not coverage_ok:
        monetary_metric = "revenue"
        caveat = "Profit-based monetary scoring fell back to revenue due to incomplete cost coverage."

    top_mode_raw = str(args.get("rfm_top_mode") or args.get("top_mode") or "rfm_score").strip().lower()
    top_mode = top_mode_raw if top_mode_raw in {"rfm_score", "monetary", "at_risk"} else "rfm_score"

    scatter_mode_raw = str(args.get("rfm_scatter_mode") or args.get("scatter_mode") or "frequency_monetary").strip().lower()
    scatter_mode = scatter_mode_raw if scatter_mode_raw in {"frequency_monetary", "recency_monetary"} else "frequency_monetary"

    recency_thresholds = _parse_thresholds_csv(
        args.get("rfm_recency_thresholds"),
        [30.0, 60.0, 90.0, 120.0],
    )
    frequency_thresholds = _parse_thresholds_csv(
        args.get("rfm_frequency_thresholds"),
        [1.0, 2.0, 4.0, 8.0],
    )
    monetary_thresholds = _parse_thresholds_csv(
        args.get("rfm_monetary_thresholds"),
        [1000.0, 5000.0, 15000.0, 30000.0],
    )

    search = str(args.get("rfm_search") or args.get("search") or "").strip()
    segments = _parse_rfm_segments(args.get("rfm_segments") or args.get("segment"))
    at_risk_only = _is_truthy(args.get("rfm_at_risk_only") or args.get("at_risk_only"))
    selected_r = _parse_optional_int(args.get("heat_r"))
    selected_f = _parse_optional_int(args.get("heat_f"))
    if selected_r is not None and not (1 <= selected_r <= 5):
        selected_r = None
    if selected_f is not None and not (1 <= selected_f <= 5):
        selected_f = None

    r_min = _parse_score_bound(args.get("r_min"), 1)
    r_max = _parse_score_bound(args.get("r_max"), 5)
    f_min = _parse_score_bound(args.get("f_min"), 1)
    f_max = _parse_score_bound(args.get("f_max"), 5)
    m_min = _parse_score_bound(args.get("m_min"), 1)
    m_max = _parse_score_bound(args.get("m_max"), 5)
    if r_min > r_max:
        r_min, r_max = r_max, r_min
    if f_min > f_max:
        f_min, f_max = f_max, f_min
    if m_min > m_max:
        m_min, m_max = m_max, m_min

    try:
        table_page = max(1, int(args.get("rfm_page") or args.get("page") or 1))
    except Exception:
        table_page = 1
    try:
        table_page_size = int(args.get("rfm_page_size") or args.get("page_size") or 25)
    except Exception:
        table_page_size = 25
    table_page_size = max(1, min(5000, table_page_size))
    top_n = _safe_int(args.get("rfm_top_n"), 25)
    top_n = max(1, min(1000, top_n))

    lookback_end = window_end_ts.normalize()
    lookback_start = (lookback_end - pd.DateOffset(months=lookback_months)).normalize()
    prior_end = (lookback_start - pd.Timedelta(days=1)).normalize()
    prior_start = (lookback_start - pd.DateOffset(months=lookback_months)).normalize()

    params_hash_payload = {
        "lookback_months": lookback_months,
        "scoring_method": scoring_method,
        "requested_monetary_metric": requested_metric,
        "effective_monetary_metric": monetary_metric,
        "top_mode": top_mode,
        "scatter_mode": scatter_mode,
        "recency_thresholds": recency_thresholds,
        "frequency_thresholds": frequency_thresholds,
        "monetary_thresholds": monetary_thresholds,
        "search": search.lower(),
        "segments": sorted([seg.lower() for seg in segments]),
        "at_risk_only": at_risk_only,
        "heat_r": selected_r,
        "heat_f": selected_f,
        "r_min": r_min,
        "r_max": r_max,
        "f_min": f_min,
        "f_max": f_max,
        "m_min": m_min,
        "m_max": m_max,
        "table_page": table_page,
        "table_page_size": table_page_size,
        "top_n": top_n,
    }
    params_hash = hashlib.sha256(
        json.dumps(params_hash_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    return {
        "lookback_months": lookback_months,
        "lookback_start": lookback_start,
        "lookback_end": lookback_end,
        "prior_start": prior_start,
        "prior_end": prior_end,
        "scoring_method": scoring_method,
        "requested_monetary_metric": requested_metric,
        "monetary_metric": monetary_metric,
        "monetary_caveat": caveat,
        "top_mode": top_mode,
        "scatter_mode": scatter_mode,
        "recency_thresholds": recency_thresholds,
        "frequency_thresholds": frequency_thresholds,
        "monetary_thresholds": monetary_thresholds,
        "search": search,
        "segments": segments,
        "at_risk_only": at_risk_only,
        "heat_r": selected_r,
        "heat_f": selected_f,
        "r_min": r_min,
        "r_max": r_max,
        "f_min": f_min,
        "f_max": f_max,
        "m_min": m_min,
        "m_max": m_max,
        "table_page": table_page,
        "table_page_size": table_page_size,
        "top_n": top_n,
        "params_hash": params_hash,
    }


def _rfm_filter_frame(frame: pd.DataFrame, settings: Dict[str, Any]) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=list(frame.columns) if isinstance(frame, pd.DataFrame) else [])
    out = frame.copy()
    search = str(settings.get("search") or "").strip().lower()
    if search:
        id_match = out["customer_id"].astype(str).str.lower().str.contains(search, na=False)
        name_match = out["customer_name"].astype(str).str.lower().str.contains(search, na=False)
        out = out[id_match | name_match]

    segments = settings.get("segments") or []
    if segments:
        allowed = {str(seg).strip().lower() for seg in segments}
        out = out[out["segment"].astype(str).str.lower().isin(allowed)]

    if settings.get("at_risk_only"):
        out = out[out["segment"].isin(["At Risk", "Can't Lose Them", "About to Sleep", "Hibernating", "Lost"])]

    heat_r = settings.get("heat_r")
    heat_f = settings.get("heat_f")
    if heat_r is not None:
        out = out[out["r_score"] == int(heat_r)]
    if heat_f is not None:
        out = out[out["f_score"] == int(heat_f)]

    r_min = int(settings.get("r_min") or 1)
    r_max = int(settings.get("r_max") or 5)
    f_min = int(settings.get("f_min") or 1)
    f_max = int(settings.get("f_max") or 5)
    m_min = int(settings.get("m_min") or 1)
    m_max = int(settings.get("m_max") or 5)
    out = out[
        (out["r_score"] >= r_min)
        & (out["r_score"] <= r_max)
        & (out["f_score"] >= f_min)
        & (out["f_score"] <= f_max)
        & (out["m_score"] >= m_min)
        & (out["m_score"] <= m_max)
    ]
    return out


def _cohort_payload(df: pd.DataFrame) -> Dict[str, Any]:
    if df.empty:
        return {"matrix": [], "sizes": [], "heatmap": [], "updated_at": None}
    sizes: Dict[str, float] = {}
    matrix_rows: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        kind = str(row.get("kind"))
        cohort_raw = row.get("cohort_month")
        activity_raw = row.get("activity_month")
        cohort = str(cohort_raw) if pd.notna(cohort_raw) else None
        activity = str(activity_raw) if pd.notna(activity_raw) else None
        if kind == "size":
            sizes[cohort] = float(row.get("cohort_size") or 0)
        else:
            matrix_rows.append(
                {
                    "cohort_month": cohort,
                    "activity_month": activity,
                    "customers": float(row.get("customers_active") or 0.0),
                }
            )
    # Build heatmap grid
    cohort_labels = sorted({r["cohort_month"] for r in matrix_rows if r.get("cohort_month")})
    activity_labels = sorted({r["activity_month"] for r in matrix_rows if r.get("activity_month")})
    heatmap: List[List[float | None]] = []
    for cohort in cohort_labels:
        size = sizes.get(cohort, 0) or 0
        row_vals: List[float | None] = []
        for act in activity_labels:
            match = next((r for r in matrix_rows if r.get("cohort_month") == cohort and r.get("activity_month") == act), None)
            if match and size:
                row_vals.append(round((match.get("customers") or 0) / size * 100.0, 2))
            else:
                row_vals.append(0.0)
        heatmap.append(row_vals)
    return {
        "matrix": [
            {
                "cohort_month": r.get("cohort_month"),
                "activity_month": r.get("activity_month"),
                "customers": float(r.get("customers") or 0.0),
            }
            for r in matrix_rows
        ],
        "sizes": [{"cohort_month": k, "size": float(v or 0.0)} for k, v in sizes.items()],
        "heatmap": {
            "rows": [[float(x or 0.0) for x in row] for row in heatmap],
            "cohorts": cohort_labels,
            "activity": activity_labels,
        },
        "updated_at": date.today().isoformat(),
    }


def _parse_clv_segments(raw: Any) -> List[str]:
    valid = {
        "whales": "Whales",
        "high value": "High Value",
        "high_value": "High Value",
        "growth": "Growth",
        "at risk high value": "At Risk High Value",
        "at_risk_high_value": "At Risk High Value",
        "low value / nurture": "Low Value / Nurture",
        "low_value_nurture": "Low Value / Nurture",
        "low value": "Low Value / Nurture",
    }
    if raw is None:
        return []
    if isinstance(raw, str):
        tokens = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        tokens = [str(part).strip() for part in raw]
    else:
        tokens = [str(raw).strip()]
    out: List[str] = []
    seen: set[str] = set()
    for token in tokens:
        key = token.lower().replace("-", " ").replace("_", " ")
        normalized = valid.get(key)
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _clv_playbook_goal(segment: str) -> str:
    goals = {
        "Whales": "Retain",
        "High Value": "Retain and grow",
        "Growth": "Grow",
        "At Risk High Value": "Win back",
        "Low Value / Nurture": "Nurture efficiently",
    }
    return goals.get(segment, "Manage")


def _clv_playbook_actions(segment: str) -> List[str]:
    mapping = {
        "Whales": [
            "Lock renewal cadence with executive touchpoints.",
            "Offer strategic bundle expansion before renewal dates.",
            "Track service incidents with 24h response SLA.",
        ],
        "High Value": [
            "Run upsell campaign on complementary products.",
            "Schedule quarterly business review with account owners.",
            "Prioritize margin optimization on large recurring SKUs.",
        ],
        "Growth": [
            "Trigger onboarding sequence for repeat purchase acceleration.",
            "Recommend best-next products from top adjacent categories.",
            "Set call tasks for sales within 7 days after last order.",
        ],
        "At Risk High Value": [
            "Escalate to save plan with personalized outreach.",
            "Offer targeted win-back incentive with expiry.",
            "Review fulfillment issues and resolve top blockers immediately.",
        ],
        "Low Value / Nurture": [
            "Use low-touch automation for reorder reminders.",
            "Bundle entry-level offers to improve frequency.",
            "Route only high-propensity accounts to human follow-up.",
        ],
    }
    return mapping.get(segment, ["Review account health and next best action."])


def _parse_clv_settings(
    args: Any,
    *,
    window_start_ts: pd.Timestamp,
    window_end_ts: pd.Timestamp,
    cost_available: bool,
    cost_coverage_pct: float | None,
) -> Dict[str, Any]:
    lookback_raw = _safe_int(
        args.get("clv_lookback_months") or args.get("clv_lookback") or args.get("lookback_months"),
        12,
    )
    lookback_months = lookback_raw if lookback_raw in {6, 12, 24, 36} else 12

    horizon_raw = _safe_int(
        args.get("clv_horizon_months") or args.get("clv_horizon") or args.get("horizon_months"),
        12,
    )
    horizon_months = horizon_raw if horizon_raw in {6, 12, 24} else 12

    discount_rate_raw = args.get("clv_discount_rate") or args.get("discount_rate") or 8.0
    try:
        discount_rate_pct = float(discount_rate_raw)
    except Exception:
        discount_rate_pct = 8.0
    discount_rate_pct = max(0.0, min(15.0, discount_rate_pct))
    discount_rate = discount_rate_pct / 100.0

    basis_raw = str(
        args.get("clv_monetary_basis")
        or args.get("clv_monetary_metric")
        or args.get("monetary_basis")
        or "gross_profit"
    ).strip().lower()
    requested_basis = basis_raw if basis_raw in {"revenue", "gross_profit", "profit"} else "gross_profit"
    effective_basis = "gross_profit" if requested_basis in {"gross_profit", "profit"} else "revenue"
    coverage_ok = bool(cost_available) and (
        (cost_coverage_pct is None) or (_clean_float(cost_coverage_pct) >= 85.0)
    )
    monetary_caveat: str | None = None
    if effective_basis == "gross_profit" and not coverage_ok:
        effective_basis = "revenue"
        monetary_caveat = "Cost coverage is incomplete; CLV uses revenue proxy."

    freq_basis_raw = str(args.get("clv_frequency_basis") or args.get("frequency_basis") or "auto").strip().lower()
    if freq_basis_raw not in {"auto", "orders_year", "orders_month"}:
        freq_basis_raw = "auto"
    frequency_basis = "orders_year" if (freq_basis_raw == "auto" and lookback_months >= 12) else freq_basis_raw
    if frequency_basis == "auto":
        frequency_basis = "orders_month"

    retention_raw = str(args.get("clv_retention_model") or args.get("retention_model") or "simple").strip().lower()
    retention_model = retention_raw if retention_raw in {"simple", "advanced"} else "simple"

    top_mode_raw = str(args.get("clv_top_mode") or "clv").strip().lower()
    top_mode = top_mode_raw if top_mode_raw in {"clv", "clv_at_risk", "growth_potential"} else "clv"

    scatter_mode_raw = str(args.get("clv_scatter_mode") or "revenue_orders").strip().lower()
    scatter_mode = scatter_mode_raw if scatter_mode_raw in {"revenue_orders", "clv_churn"} else "revenue_orders"

    search = str(args.get("clv_search") or args.get("search") or "").strip()
    segments = _parse_clv_segments(args.get("clv_segments") or args.get("segment"))
    min_clv = max(0.0, _clean_float(args.get("clv_min_clv")))
    high_risk_only = _is_truthy(args.get("clv_high_risk_only"))
    low_margin_only = _is_truthy(args.get("clv_low_margin_only"))

    sort_raw = str(args.get("clv_sort_by") or "clv_12m").strip().lower()
    if sort_raw not in {
        "customer", "customer_name", "segment", "clv", "clv_12m", "clv_at_risk",
        "revenue", "profit", "margin", "margin_pct", "aov", "orders_per_year",
        "orders_per_month", "recency", "days_since_last", "delta_revenue",
    }:
        sort_raw = "clv_12m"
    sort_dir_raw = str(args.get("clv_sort_dir") or "desc").strip().lower()
    sort_dir = "asc" if sort_dir_raw in {"asc", "ascending", "up", "1"} else "desc"

    table_page = max(1, _safe_int(args.get("clv_page"), 1))
    table_page_size_raw = _safe_int(args.get("clv_page_size"), 25)
    table_page_size = table_page_size_raw if table_page_size_raw in {25, 50, 100} else 25

    top_n = max(5, min(100, _safe_int(args.get("clv_top_n"), 10)))
    export_all = _is_truthy(args.get("export_all")) or _is_truthy(args.get("clv_export_all"))

    lookback_end = window_end_ts.normalize()
    lookback_start = (lookback_end - pd.DateOffset(months=lookback_months) + pd.Timedelta(days=1)).normalize()
    lookback_start = max(lookback_start, window_start_ts.normalize())
    lookback_days = max(1, int((lookback_end - lookback_start).days) + 1)
    prior_end = (lookback_start - pd.Timedelta(days=1)).normalize()
    prior_start = (prior_end - pd.Timedelta(days=lookback_days - 1)).normalize()
    if prior_end < prior_start:
        prior_start = prior_end

    params_hash_payload = {
        "lookback_months": lookback_months,
        "horizon_months": horizon_months,
        "discount_rate_pct": round(discount_rate_pct, 4),
        "requested_basis": requested_basis,
        "basis": effective_basis,
        "frequency_basis": frequency_basis,
        "retention_model": retention_model,
        "top_mode": top_mode,
        "scatter_mode": scatter_mode,
        "search": search,
        "segments": segments,
        "min_clv": round(min_clv, 4),
        "high_risk_only": bool(high_risk_only),
        "low_margin_only": bool(low_margin_only),
        "sort_by": sort_raw,
        "sort_dir": sort_dir,
    }
    params_hash = hashlib.sha256(
        json.dumps(params_hash_payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()

    return {
        "lookback_months": lookback_months,
        "horizon_months": horizon_months,
        "discount_rate_pct": float(discount_rate_pct),
        "discount_rate": float(discount_rate),
        "requested_monetary_basis": requested_basis,
        "monetary_basis": effective_basis,
        "frequency_basis": frequency_basis,
        "retention_model": retention_model,
        "top_mode": top_mode,
        "scatter_mode": scatter_mode,
        "search": search,
        "segments": segments,
        "min_clv": min_clv,
        "high_risk_only": bool(high_risk_only),
        "low_margin_only": bool(low_margin_only),
        "sort_by": sort_raw,
        "sort_dir": sort_dir,
        "table_page": table_page,
        "table_page_size": table_page_size,
        "top_n": top_n,
        "export_all": bool(export_all),
        "lookback_start": lookback_start,
        "lookback_end": lookback_end,
        "lookback_days": lookback_days,
        "prior_start": prior_start,
        "prior_end": prior_end,
        "monetary_caveat": monetary_caveat,
        "params_hash": params_hash,
        "no_activity_policy": "excluded",
        "computed_window_note": (
            f"Computed using last {lookback_months} months ending on {lookback_end.date().isoformat()}."
        ),
    }


def _clv_payload(
    cust: pd.DataFrame,
    *,
    args: Any,
    window_start_ts: pd.Timestamp,
    window_end_ts: pd.Timestamp,
    cost_available: bool,
    cost_coverage_pct: float | None,
) -> Dict[str, Any]:
    settings = _parse_clv_settings(
        args,
        window_start_ts=window_start_ts,
        window_end_ts=window_end_ts,
        cost_available=cost_available,
        cost_coverage_pct=cost_coverage_pct,
    )

    empty = {
        "settings": {
            "lookback_months": int(settings["lookback_months"]),
            "horizon_months": int(settings["horizon_months"]),
            "discount_rate_pct": float(settings["discount_rate_pct"]),
            "requested_monetary_basis": settings["requested_monetary_basis"],
            "monetary_basis": settings["monetary_basis"],
            "frequency_basis": settings["frequency_basis"],
            "retention_model": settings["retention_model"],
            "lookback_start": settings["lookback_start"].date().isoformat(),
            "lookback_end": settings["lookback_end"].date().isoformat(),
            "prior_start": settings["prior_start"].date().isoformat(),
            "prior_end": settings["prior_end"].date().isoformat(),
            "monetary_caveat": settings["monetary_caveat"],
            "params_hash": settings["params_hash"],
            "computed_window_note": settings["computed_window_note"],
            "no_activity_policy": settings["no_activity_policy"],
            "cost_coverage_pct": None if cost_coverage_pct is None else float(cost_coverage_pct),
        },
        "filters": {
            "search": settings["search"],
            "segments": settings["segments"],
            "min_clv": settings["min_clv"],
            "high_risk_only": bool(settings["high_risk_only"]),
            "low_margin_only": bool(settings["low_margin_only"]),
        },
        "cards": {
            "customers_lookback": 0,
            "revenue_lookback": 0.0,
            "repeat_rate_lookback": 0.0,
            "at_risk_revenue_stake": 0.0,
            "reactivation_rate": 0.0,
            "top_segment_by_revenue": None,
            "top_segment_by_growth": None,
            "total_clv": 0.0,
            "avg_clv_12m": 0.0,
        },
        "risk_opportunity": {
            "at_risk_customers": 0,
            "at_risk_revenue_stake": 0.0,
            "cant_lose_customers": 0,
            "new_customers": 0,
            "reactivated_customers": 0,
        },
        "concentration": {"top1_share_pct": None, "top10_share_pct": None, "hhi": None},
        "clv_at_risk": {"total": 0.0, "pct_total": None},
        "margin_leverage": {"target_margin_pct": None, "estimated_uplift": 0.0, "eligible_customers": 0},
        "segment_summary": [],
        "segment_leaderboard": [],
        "segment_playbooks": [],
        "leaderboard": [],
        "at_risk_high_value": [],
        "customers_table": {"rows": [], "total_rows": 0, "page": 1, "page_size": int(settings["table_page_size"]), "total_pages": 0},
        "charts": {
            "clv_distribution": [],
            "clv_vs_churn": {"points": [], "x_label": "CLV (12m)", "y_label": "Churn risk %"},
            "revenue_orders": {"points": [], "quadrants": {"revenue_median": 0.0, "orders_median": 0.0}},
            "margin_top_customers": {"labels": [], "values": []},
        },
        "top": [],
        "histogram": [],
        "avg_clv": 0.0,
        "exports": {"datasets": ["customers", "segments", "at_risk_high_value"]},
        "updated_at": date.today().isoformat(),
    }
    if cust.empty:
        return empty

    df = cust.copy()
    required_defaults = {
        "customer_id": None,
        "customer_name": None,
        "revenue": 0.0,
        "cost": 0.0,
        "profit": 0.0,
        "orders": 0.0,
        "avg_order_value": 0.0,
        "margin_pct": np.nan,
        "days_since_last": np.nan,
        "first_order": pd.NaT,
        "last_order": pd.NaT,
        "delta_revenue": 0.0,
        "delta_revenue_pct": np.nan,
        "revenue_prior_window": 0.0,
        "segment_label": None,
    }
    for col, default_val in required_defaults.items():
        if col not in df.columns:
            df[col] = default_val

    for col in ("revenue", "cost", "profit", "orders", "avg_order_value", "margin_pct", "days_since_last", "delta_revenue", "delta_revenue_pct", "revenue_prior_window"):
        df[col] = _clean_numeric_series(df[col], default=0.0 if col != "margin_pct" else None)
    for col in ("first_order", "last_order"):
        df[col] = _clean_datetime_series(df[col])
    df["orders"] = df["orders"].clip(lower=0.0)
    df["revenue"] = df["revenue"].clip(lower=0.0)

    active = df[df["revenue"] > 0].copy()
    if active.empty:
        return empty

    lookback_days = max(1, int(settings["lookback_days"]))
    lookback_months = max(1, int(settings["lookback_months"]))
    annual_orders = (active["orders"] / lookback_days * 365.25).clip(lower=0.0)
    monthly_orders = (annual_orders / 12.0).clip(lower=0.0)

    if settings["monetary_basis"] == "gross_profit":
        monetary_total = active["profit"]
    else:
        monetary_total = active["revenue"]
    monetary_total = pd.to_numeric(monetary_total, errors="coerce").fillna(0.0)
    monetary_per_order = np.where(active["orders"] > 0, monetary_total / active["orders"], 0.0)

    repeat_component = np.where(active["orders"] > 1, 0.85, 0.55)
    recency_component = 1.0 - (active["days_since_last"].fillna(lookback_days) / lookback_days)
    recency_component = np.clip(recency_component, 0.2, 1.0)

    if settings["retention_model"] == "advanced":
        churn_probability = np.clip(active["days_since_last"].fillna(lookback_days) / max(45.0, lookback_days * 1.15), 0.05, 0.95)
        retention_factor = 1.0 - churn_probability
    else:
        retention_factor = np.clip((repeat_component + recency_component) / 2.0, 0.15, 1.0)
        churn_probability = 1.0 - retention_factor

    annual_value = monetary_per_order * annual_orders
    horizon_years = max(0.25, float(settings["horizon_months"]) / 12.0)
    discount_rate = max(0.0, float(settings["discount_rate"]))
    discount_factor_h = 1.0 / ((1.0 + discount_rate) ** horizon_years) if discount_rate > 0 else 1.0
    discount_factor_12 = 1.0 / (1.0 + discount_rate) if discount_rate > 0 else 1.0

    clv_12m = np.maximum(annual_value * retention_factor * discount_factor_12, 0.0)
    clv_selected = np.maximum(annual_value * horizon_years * retention_factor * discount_factor_h, 0.0)
    clv_at_risk = np.maximum(clv_12m * churn_probability, 0.0)

    active["orders_per_year"] = annual_orders
    active["orders_per_month"] = monthly_orders
    active["retention_factor"] = retention_factor
    active["churn_probability"] = churn_probability
    active["clv_12m"] = clv_12m
    active["clv_selected"] = clv_selected
    active["clv_at_risk"] = clv_at_risk
    active["recency_days"] = active["days_since_last"].fillna(lookback_days).clip(lower=0.0)

    growth_norm = np.clip((active["delta_revenue_pct"].fillna(0.0) + 20.0) / 70.0, 0.0, 1.0)
    annual_q75 = float(np.nanpercentile(active["orders_per_year"], 75)) if len(active) else 1.0
    freq_norm = np.clip(active["orders_per_year"] / max(annual_q75, 1.0), 0.0, 1.5)
    active["growth_potential_score"] = np.clip((growth_norm * 0.45) + (freq_norm * 0.35) + (recency_component * 0.20), 0.0, 1.0)

    clv_vals = active["clv_12m"].to_numpy(dtype="float64")
    p95 = float(np.nanpercentile(clv_vals, 95)) if len(clv_vals) else 0.0
    p80 = float(np.nanpercentile(clv_vals, 80)) if len(clv_vals) else 0.0
    p50 = float(np.nanpercentile(clv_vals, 50)) if len(clv_vals) else 0.0
    growth_cutoff = float(np.nanpercentile(active["growth_potential_score"], 75)) if len(active) else 0.5

    def _segment_row(row: pd.Series) -> str:
        clv_val = _clean_float(row.get("clv_12m"))
        risk = _clean_float(row.get("churn_probability"))
        growth_score = _clean_float(row.get("growth_potential_score"))
        delta_pct = _clean_float(row.get("delta_revenue_pct"))
        if clv_val >= p80 and risk >= 0.55:
            return "At Risk High Value"
        if clv_val >= p95 and clv_val > 0.0:
            return "Whales"
        if clv_val >= p80 and clv_val > 0.0:
            return "High Value"
        if clv_val >= p50 and (growth_score >= growth_cutoff or delta_pct >= 20.0):
            return "Growth"
        return "Low Value / Nurture"

    active["clv_segment"] = active.apply(_segment_row, axis=1)
    active["risk_band"] = np.where(
        active["churn_probability"] >= 0.66,
        "High",
        np.where(active["churn_probability"] >= 0.40, "Medium", "Low"),
    )

    segment_order = ["Whales", "High Value", "Growth", "At Risk High Value", "Low Value / Nurture"]
    total_revenue = _sum_numeric(active["revenue"])
    total_clv = _sum_numeric(active["clv_12m"])
    repeat_rate_lb = float((active["orders"] > 1).sum() / len(active) * 100.0) if len(active) else 0.0

    segment_summary: List[Dict[str, Any]] = []
    for seg in segment_order:
        seg_df = active[active["clv_segment"] == seg]
        seg_revenue = _sum_numeric(seg_df["revenue"])
        segment_summary.append(
            {
                "segment": seg,
                "customers": int(len(seg_df)),
                "revenue": seg_revenue,
                "profit": _sum_numeric(seg_df["profit"]),
                "avg_clv": float(pd.to_numeric(seg_df["clv_12m"], errors="coerce").mean()) if not seg_df.empty else 0.0,
                "churn_risk_avg": float(pd.to_numeric(seg_df["churn_probability"], errors="coerce").mean() * 100.0) if not seg_df.empty else 0.0,
                "avg_orders": float(pd.to_numeric(seg_df["orders"], errors="coerce").mean()) if not seg_df.empty else 0.0,
                "median_recency_days": float(pd.to_numeric(seg_df["recency_days"], errors="coerce").median()) if not seg_df.empty else 0.0,
                "share_pct": (_safe_pct(seg_revenue, total_revenue) or 0.0),
                "delta_revenue": _sum_numeric(seg_df["delta_revenue"]),
                "playbook": _clv_playbook_actions(seg),
            }
        )

    segment_playbooks: List[Dict[str, Any]] = []
    for seg in segment_order:
        seg_df = active[active["clv_segment"] == seg].sort_values(["clv_12m", "revenue"], ascending=[False, False]).head(5)
        key_accounts: List[Dict[str, Any]] = []
        for _, account in seg_df.iterrows():
            customer_id_val = str(account.get("customer_id") or "").strip()
            key_accounts.append(
                {
                    "customer_id": customer_id_val or None,
                    "customer_name": account.get("customer_name"),
                    "clv_12m": _clean_float(account.get("clv_12m")),
                    "revenue": _clean_float(account.get("revenue")),
                    "url": f"/customers/drilldown/{customer_id_val}" if customer_id_val else None,
                }
            )
        segment_playbooks.append(
            {
                "segment": seg,
                "goal": _clv_playbook_goal(seg),
                "actions": _clv_playbook_actions(seg),
                "customers": int((active["clv_segment"] == seg).sum()),
                "revenue": _sum_numeric(active.loc[active["clv_segment"] == seg, "revenue"]),
                "key_accounts": key_accounts,
            }
        )

    at_risk_high_value_df = active[active["clv_segment"] == "At Risk High Value"].sort_values(
        ["clv_at_risk", "clv_12m"], ascending=[False, False]
    )
    at_risk_high_value = [
        {
            "customer_id": row.customer_id,
            "customer_name": row.customer_name,
            "clv_12m": _clean_float(row.clv_12m),
            "clv_at_risk": _clean_float(row.clv_at_risk),
            "churn_probability_pct": _clean_float(row.churn_probability) * 100.0,
            "revenue": _clean_float(row.revenue),
            "risk_band": row.risk_band,
            "drilldown_url": f"/customers/drilldown/{row.customer_id}",
        }
        for row in at_risk_high_value_df.itertuples()
    ]

    top1_share = (_safe_pct(float(np.nanmax(clv_vals) if len(clv_vals) else 0.0), total_clv) or 0.0) if total_clv else None
    top10_share = (
        _safe_pct(float(np.sort(clv_vals)[::-1][:10].sum() if len(clv_vals) else 0.0), total_clv) or 0.0
    ) if total_clv else None
    hhi = float((((active["clv_12m"] / total_clv).fillna(0.0) ** 2).sum()) * 10000.0) if total_clv else None

    total_clv_at_risk = _sum_numeric(active["clv_at_risk"])
    clv_at_risk_pct = _safe_pct(total_clv_at_risk, total_clv)

    target_margin = None
    high_value_cutoff = p80 if p80 > 0 else float(pd.to_numeric(active["clv_12m"], errors="coerce").median())
    if "target_margin_pct" in active.columns:
        target_margin_series = pd.to_numeric(active["target_margin_pct"], errors="coerce")
        target_margin = float(target_margin_series.dropna().median()) if not target_margin_series.dropna().empty else None
        leverage_df = active[
            (pd.to_numeric(active["margin_pct"], errors="coerce") < target_margin_series)
            & (active["clv_12m"] >= high_value_cutoff)
        ]
        estimated_uplift = _sum_numeric(
            ((target_margin_series.loc[leverage_df.index] - leverage_df["margin_pct"]).clip(lower=0.0) / 100.0) * leverage_df["revenue"]
        ) if not leverage_df.empty else 0.0
    else:
        leverage_df = active.iloc[0:0].copy()
        estimated_uplift = 0.0

    top_segment_revenue = max(segment_summary, key=lambda rec: _clean_float(rec.get("revenue")), default=None)
    top_segment_growth = max(segment_summary, key=lambda rec: _clean_float(rec.get("delta_revenue")), default=None)

    top_mode = settings["top_mode"]
    if top_mode == "clv_at_risk":
        top_sorted = active.sort_values(["clv_at_risk", "clv_12m"], ascending=[False, False])
    elif top_mode == "growth_potential":
        top_sorted = active.sort_values(["growth_potential_score", "clv_12m"], ascending=[False, False])
    else:
        top_sorted = active.sort_values(["clv_12m", "revenue"], ascending=[False, False])
    leaderboard_df = top_sorted.head(int(settings["top_n"]))

    sort_map = {
        "customer": "customer_name",
        "customer_name": "customer_name",
        "segment": "clv_segment",
        "clv": "clv_12m",
        "clv_12m": "clv_12m",
        "clv_at_risk": "clv_at_risk",
        "revenue": "revenue",
        "profit": "profit",
        "margin": "margin_pct",
        "margin_pct": "margin_pct",
        "aov": "avg_order_value",
        "orders_per_year": "orders_per_year",
        "orders_per_month": "orders_per_month",
        "recency": "recency_days",
        "days_since_last": "recency_days",
        "delta_revenue": "delta_revenue",
    }
    sort_col = sort_map.get(settings["sort_by"], "clv_12m")
    sort_asc = settings["sort_dir"] == "asc"

    filtered_df = active.copy()
    search = str(settings["search"] or "").strip().lower()
    if search:
        id_match = filtered_df["customer_id"].astype(str).str.lower().str.contains(search, na=False)
        name_match = filtered_df["customer_name"].astype(str).str.lower().str.contains(search, na=False)
        filtered_df = filtered_df[id_match | name_match]
    if settings["segments"]:
        allowed = {str(seg).strip().lower() for seg in settings["segments"]}
        filtered_df = filtered_df[filtered_df["clv_segment"].astype(str).str.lower().isin(allowed)]
    if settings["min_clv"] > 0:
        filtered_df = filtered_df[filtered_df["clv_12m"] >= float(settings["min_clv"])]
    if settings["high_risk_only"]:
        filtered_df = filtered_df[filtered_df["risk_band"] == "High"]
    if settings["low_margin_only"]:
        filtered_df = filtered_df[filtered_df["margin_pct"] < target_margin]

    filtered_df = filtered_df.sort_values([sort_col, "customer_id"], ascending=[sort_asc, True], na_position="last")
    total_rows = int(len(filtered_df))
    page_num = int(settings["table_page"])
    page_size_num = int(settings["table_page_size"])
    if settings["export_all"]:
        paged_df = filtered_df
        total_pages = 1 if total_rows > 0 else 0
    else:
        total_pages = int(math.ceil(total_rows / page_size_num)) if total_rows else 0
        if total_pages:
            page_num = min(page_num, total_pages)
        start_idx = (page_num - 1) * page_size_num
        end_idx = start_idx + page_size_num
        paged_df = filtered_df.iloc[start_idx:end_idx]

    def _row_payload(row: pd.Series) -> Dict[str, Any]:
        customer_id_val = str(row.get("customer_id") or "").strip()
        return {
            "customer_id": customer_id_val or None,
            "customer_name": row.get("customer_name"),
            "segment": row.get("clv_segment"),
            "clv_12m": _clean_float(row.get("clv_12m")),
            "clv_selected": _clean_float(row.get("clv_selected")),
            "clv_at_risk": _clean_float(row.get("clv_at_risk")),
            "revenue": _clean_float(row.get("revenue")),
            "profit": _clean_float(row.get("profit")),
            "margin_pct": _clean_float(row.get("margin_pct")),
            "avg_order_value": _clean_float(row.get("avg_order_value")),
            "orders_per_year": _clean_float(row.get("orders_per_year")),
            "orders_per_month": _clean_float(row.get("orders_per_month")),
            "orders": _clean_float(row.get("orders")),
            "recency_days": _clean_float(row.get("recency_days")),
            "last_order_date": (
                pd.to_datetime(row.get("last_order"), errors="coerce").date().isoformat()
                if pd.notna(pd.to_datetime(row.get("last_order"), errors="coerce"))
                else None
            ),
            "delta_revenue": _clean_float(row.get("delta_revenue")),
            "delta_revenue_pct": _none_if_na(row.get("delta_revenue_pct")),
            "churn_probability": _clean_float(row.get("churn_probability")),
            "churn_probability_pct": _clean_float(row.get("churn_probability")) * 100.0,
            "risk_band": row.get("risk_band"),
            "growth_potential_score": _clean_float(row.get("growth_potential_score")),
            "drilldown_url": f"/customers/drilldown/{customer_id_val}" if customer_id_val else None,
        }

    leaderboard = [_row_payload(row) for _, row in leaderboard_df.iterrows()]
    customers_rows = [_row_payload(row) for _, row in paged_df.iterrows()]
    full_filtered_rows = [_row_payload(row) for _, row in filtered_df.iterrows()]

    recency_hist = _build_histogram_payload(active["recency_days"], max_bins=10)
    clv_hist = _build_histogram_payload(active["clv_12m"], max_bins=12)

    scatter_clv_churn_points: List[Dict[str, Any]] = []
    scatter_rev_orders_points: List[Dict[str, Any]] = []
    for row in filtered_df.itertuples():
        customer_id_val = str(getattr(row, "customer_id", "") or "").strip()
        point_common = {
            "customer_id": customer_id_val or None,
            "customer_name": getattr(row, "customer_name", None),
            "segment": getattr(row, "clv_segment", None),
            "clv_12m": _clean_float(getattr(row, "clv_12m", None)),
            "clv_at_risk": _clean_float(getattr(row, "clv_at_risk", None)),
            "revenue": _clean_float(getattr(row, "revenue", None)),
            "orders": _clean_float(getattr(row, "orders", None)),
            "margin_pct": _clean_float(getattr(row, "margin_pct", None)),
            "recency_days": _clean_float(getattr(row, "recency_days", None)),
            "churn_probability_pct": _clean_float(getattr(row, "churn_probability", None)) * 100.0,
            "risk_band": getattr(row, "risk_band", None),
            "url": f"/customers/drilldown/{customer_id_val}" if customer_id_val else None,
        }
        scatter_clv_churn_points.append(
            {
                **point_common,
                "x": _clean_float(getattr(row, "clv_12m", None)),
                "y": _clean_float(getattr(row, "churn_probability", None)) * 100.0,
                "bubble": _clean_float(getattr(row, "revenue", None)),
            }
        )
        scatter_rev_orders_points.append(
            {
                **point_common,
                "x": _clean_float(getattr(row, "revenue", None)),
                "y": _clean_float(getattr(row, "orders", None)),
                "bubble": _clean_float(getattr(row, "clv_12m", None)),
            }
        )

    revenue_median = float(pd.to_numeric(filtered_df["revenue"], errors="coerce").median()) if not filtered_df.empty else 0.0
    orders_median = float(pd.to_numeric(filtered_df["orders"], errors="coerce").median()) if not filtered_df.empty else 0.0

    margin_top = active.sort_values(["clv_12m", "revenue"], ascending=[False, False]).head(12)
    margin_labels = [
        str(rec.get("customer_name") or rec.get("customer_id") or "")
        for rec in margin_top.to_dict(orient="records")
    ]
    margin_values = [float(x) if pd.notna(x) else 0.0 for x in margin_top.get("margin_pct", pd.Series(dtype="float64")).tolist()]

    at_risk_customers = int((active["risk_band"] == "High").sum())
    cant_lose_customers = int(((active["risk_band"] == "High") & (active["clv_12m"] >= p80)).sum())
    new_customers = int((active["segment_label"].astype(str).str.lower() == "new").sum())
    reactivated_customers = int((active["segment_label"].astype(str).str.lower() == "reactivated").sum())
    at_risk_revenue = _sum_numeric(active.loc[active["risk_band"] == "High", "revenue_prior_window"])
    reactivation_rate = float(reactivated_customers / len(active) * 100.0) if len(active) else 0.0

    legacy_top = active.sort_values(["clv_12m", "revenue"], ascending=[False, False]).head(10)
    legacy_top_rows = [
        {
            "customer_id": row.customer_id,
            "customer_name": row.customer_name,
            "clv": _clean_float(row.clv_12m),
            "avg_order_value": _clean_float(row.avg_order_value),
            "orders_per_year": _clean_float(row.orders_per_year),
            "margin_pct": _clean_float(row.margin_pct),
            "profit": _clean_float(row.profit),
            "revenue": _clean_float(row.revenue),
            "orders": _safe_int(row.orders, 0),
            "segment": row.clv_segment,
            "clv_at_risk": _clean_float(row.clv_at_risk),
            "growth_potential_score": _clean_float(row.growth_potential_score),
        }
        for row in legacy_top.itertuples()
    ]
    legacy_hist = []
    for bin_row in clv_hist:
        legacy_hist.append(
            {
                "label": f"${_clean_float(bin_row.get('bin_start')):,.0f} - ${_clean_float(bin_row.get('bin_end')):,.0f}",
                "count": _safe_int(bin_row.get("count"), 0),
            }
        )

    payload = {
        "settings": {
            "lookback_months": int(settings["lookback_months"]),
            "horizon_months": int(settings["horizon_months"]),
            "discount_rate_pct": float(settings["discount_rate_pct"]),
            "requested_monetary_basis": settings["requested_monetary_basis"],
            "monetary_basis": settings["monetary_basis"],
            "frequency_basis": settings["frequency_basis"],
            "retention_model": settings["retention_model"],
            "lookback_start": settings["lookback_start"].date().isoformat(),
            "lookback_end": settings["lookback_end"].date().isoformat(),
            "prior_start": settings["prior_start"].date().isoformat(),
            "prior_end": settings["prior_end"].date().isoformat(),
            "monetary_caveat": settings["monetary_caveat"],
            "params_hash": settings["params_hash"],
            "computed_window_note": settings["computed_window_note"],
            "no_activity_policy": settings["no_activity_policy"],
            "cost_coverage_pct": None if cost_coverage_pct is None else float(cost_coverage_pct),
        },
        "filters": {
            "search": settings["search"],
            "segments": settings["segments"],
            "min_clv": settings["min_clv"],
            "high_risk_only": bool(settings["high_risk_only"]),
            "low_margin_only": bool(settings["low_margin_only"]),
        },
        "cards": {
            "customers_lookback": int(len(active)),
            "revenue_lookback": total_revenue,
            "repeat_rate_lookback": repeat_rate_lb,
            "at_risk_revenue_stake": at_risk_revenue,
            "reactivation_rate": reactivation_rate,
            "top_segment_by_revenue": top_segment_revenue.get("segment") if top_segment_revenue else None,
            "top_segment_by_growth": top_segment_growth.get("segment") if top_segment_growth else None,
            "total_clv": total_clv,
            "avg_clv_12m": float(pd.to_numeric(active["clv_12m"], errors="coerce").mean()) if len(active) else 0.0,
        },
        "risk_opportunity": {
            "at_risk_customers": at_risk_customers,
            "at_risk_revenue_stake": at_risk_revenue,
            "cant_lose_customers": cant_lose_customers,
            "new_customers": new_customers,
            "reactivated_customers": reactivated_customers,
        },
        "concentration": {"top1_share_pct": top1_share, "top10_share_pct": top10_share, "hhi": hhi},
        "clv_at_risk": {"total": total_clv_at_risk, "pct_total": clv_at_risk_pct},
        "margin_leverage": {"target_margin_pct": target_margin, "estimated_uplift": estimated_uplift, "eligible_customers": int(len(leverage_df))},
        "segment_summary": segment_summary,
        "segment_leaderboard": segment_summary,
        "segment_playbooks": segment_playbooks,
        "leaderboard": leaderboard,
        "at_risk_high_value": at_risk_high_value,
        "customers_table": {
            "rows": customers_rows,
            "total_rows": total_rows,
            "page": page_num if total_rows else 1,
            "page_size": page_size_num,
            "total_pages": total_pages,
            "sort_by": sort_col,
            "sort_dir": settings["sort_dir"],
            "rows_all": full_filtered_rows if settings["export_all"] else [],
        },
        "charts": {
            "recency_distribution": recency_hist,
            "clv_distribution": clv_hist,
            "clv_vs_churn": {
                "points": scatter_clv_churn_points,
                "x_label": "CLV (12m)",
                "y_label": "Churn risk %",
                "mode": "clv_churn",
            },
            "revenue_orders": {
                "points": scatter_rev_orders_points,
                "quadrants": {
                    "revenue_median": revenue_median,
                    "orders_median": orders_median,
                },
                "mode": "revenue_orders",
            },
            "margin_top_customers": {"labels": margin_labels, "values": margin_values},
        },
        "top": legacy_top_rows,
        "histogram": legacy_hist,
        "avg_clv": float(pd.to_numeric(active["clv_12m"], errors="coerce").mean()) if len(active) else 0.0,
        "exports": {"datasets": ["customers", "segments", "at_risk_high_value"]},
        "updated_at": date.today().isoformat(),
    }
    return payload


def _table_payload(cust: pd.DataFrame, args: Any) -> Tuple[Dict[str, Any], Dict[str, float], int, int]:
    page, page_size = _pagination(args, default_size=25, max_size=5000)
    search = (args.get("search") or args.get("q") or "").strip()
    sort_col, ascending = _sort_fields(args)
    quick_filter_raw = str(args.get("quick_filter") or args.get("quick_filters") or "all").strip().lower()
    export_all = _is_truthy(args.get("export_all"))
    at_risk_only = _is_truthy(args.get("at_risk"))
    top_n = _parse_optional_int(args.get("top_n") or args.get("topN") or args.get("top"))

    records = cust.to_dict(orient="records")

    if quick_filter_raw not in {"", "all"}:
        normalized_quick = quick_filter_raw.replace("-", "_").replace(" ", "_")
        records = [
            rec
            for rec in records
            if isinstance(rec, dict)
            and _segment_filter_value(rec.get("segment_label") or "") == normalized_quick
        ]

    if at_risk_only:
        records = [
            rec
            for rec in records
            if isinstance(rec, dict) and str(rec.get("churn_risk_band") or "").strip().lower() in {"high", "medium"}
        ]

    if search:
        needle = search.lower()
        filtered: List[Dict[str, Any]] = []
        for rec in records:
            if not isinstance(rec, dict):
                continue
            fields = (
                rec.get("customer_name"),
                rec.get("customer_id"),
                rec.get("segment_label"),
                rec.get("churn_risk_band"),
            )
            blob = " ".join(str(_none_if_na(v) or "") for v in fields).lower()
            if needle in blob:
                filtered.append(rec)
        records = filtered

    if top_n is not None:
        records = sorted(
            [rec for rec in records if isinstance(rec, dict)],
            key=lambda rec: _clean_float(rec.get("revenue")),
            reverse=True,
        )[:top_n]

    total_rows = len(records)
    total_pages = max(1, math.ceil(total_rows / page_size)) if total_rows else 0
    if export_all:
        page = 1
        offset = 0
        page_records = records
        page_size = max(total_rows, 1)
        total_pages = 1 if total_rows else 0
    else:
        offset = (page - 1) * page_size
        if offset >= total_rows:
            offset = 0
            page = 1
        numeric_sort_fields = {
            "revenue",
            "revenue_prior_window",
            "delta_revenue",
            "delta_revenue_pct",
            "profit_prior_window",
            "delta_profit",
            "delta_profit_pct",
            "margin_prior_pct",
            "delta_margin_pct",
            "cost",
            "profit",
            "margin_pct",
            "orders",
            "orders_prior_window",
            "delta_orders",
            "delta_orders_pct",
            "orders_last_90",
            "orders_last_30",
            "avg_order_value",
            "asp",
            "days_since_last",
        }
        if sort_col in numeric_sort_fields:
            records = sorted(
                records,
                key=lambda rec: _clean_float(rec.get(sort_col)) if isinstance(rec, dict) else 0.0,
                reverse=not ascending,
            )
        else:
            records = sorted(
                records,
                key=lambda rec: str(_none_if_na(rec.get(sort_col)) or "") if isinstance(rec, dict) else "",
                reverse=not ascending,
            )
        page_records = records[offset : offset + page_size]

    if export_all:
        numeric_sort_fields = {
            "revenue",
            "revenue_prior_window",
            "delta_revenue",
            "delta_revenue_pct",
            "profit_prior_window",
            "delta_profit",
            "delta_profit_pct",
            "margin_prior_pct",
            "delta_margin_pct",
            "cost",
            "profit",
            "margin_pct",
            "orders",
            "orders_prior_window",
            "delta_orders",
            "delta_orders_pct",
            "orders_last_90",
            "orders_last_30",
            "avg_order_value",
            "asp",
            "days_since_last",
        }
        if sort_col in numeric_sort_fields:
            page_records = sorted(
                page_records,
                key=lambda rec: _clean_float(rec.get(sort_col)) if isinstance(rec, dict) else 0.0,
                reverse=not ascending,
            )
        else:
            page_records = sorted(
                page_records,
                key=lambda rec: str(_none_if_na(rec.get(sort_col)) or "") if isinstance(rec, dict) else "",
                reverse=not ascending,
            )

    rows: List[Dict[str, Any]] = []
    for rec in page_records:
        if not isinstance(rec, dict):
            continue
        revenue = _clean_float(rec.get("revenue"))
        revenue_prior = _clean_float(rec.get("revenue_prior_window"))
        delta_revenue = _clean_float(rec.get("delta_revenue"))
        delta_revenue_pct = rec.get("delta_revenue_pct")
        revenue_delta_status = str(rec.get("delta_revenue_status") or "normal").strip().lower()
        margin_pct = rec.get("margin_pct")
        first_order_ts = _to_timestamp(rec.get("first_order"))
        last_order_ts = _to_timestamp(rec.get("last_order"))
        orders_prior = _safe_int(rec.get("orders_prior_window"), 0)
        delta_orders = _safe_int(rec.get("delta_orders"), 0)
        rows.append(
            {
                "key": rec.get("customer_id"),
                "label": rec.get("customer_name"),
                "customer_id": rec.get("customer_id"),
                "customer_name": rec.get("customer_name"),
                "first_order": first_order_ts.date().isoformat() if first_order_ts is not None else None,
                "last_order": last_order_ts.date().isoformat() if last_order_ts is not None else None,
                "days_since_last_order": (
                    None if _none_if_na(rec.get("days_since_last")) is None else _safe_int(rec.get("days_since_last"), 0)
                ),
                "revenue": revenue,
                "revenue_prior_window": revenue_prior,
                "delta_revenue": delta_revenue,
                "delta_revenue_pct": None if revenue_prior <= 0.0 else _clean_float(delta_revenue_pct),
                "delta_revenue_status": revenue_delta_status,
                "delta_revenue_label": ("New" if revenue_delta_status == "new" else ("Low base" if revenue_delta_status == "low_base" else None)),
                "cost": _clean_float(rec.get("cost")),
                "profit": _clean_float(rec.get("profit")),
                "profit_prior_window": _clean_float(rec.get("profit_prior_window")),
                "delta_profit": _clean_float(rec.get("delta_profit")),
                "delta_profit_pct": None if _clean_float(rec.get("profit_prior_window")) <= 0.0 else _clean_float(rec.get("delta_profit_pct")),
                "margin_pct": None if revenue == 0 else _clean_float(margin_pct),
                "minimum_margin_pct": _clean_optional_float(rec.get("minimum_margin_pct")),
                "target_margin_pct": _clean_optional_float(rec.get("target_margin_pct")),
                "target_gap_pct_points": _clean_optional_float(rec.get("target_gap_pct_points")),
                "status_key": rec.get("status_key"),
                "target_status": rec.get("target_status"),
                "margin_prior_pct": None if revenue_prior == 0 else _clean_float(rec.get("margin_prior_pct")),
                "delta_margin_pct": _none_if_na(rec.get("delta_margin_pct")),
                "orders": _safe_int(rec.get("orders"), 0),
                "orders_prior_window": orders_prior,
                "delta_orders": delta_orders,
                "delta_orders_pct": None if orders_prior <= 0 else _clean_float(rec.get("delta_orders_pct")),
                "orders_last_30": _safe_int(rec.get("orders_last_30"), 0),
                "orders_last_90": _safe_int(rec.get("orders_last_90"), 0),
                "avg_order_value": _clean_float(rec.get("avg_order_value")),
                "asp": _clean_float(rec.get("asp")),
                "qty": _clean_float(rec.get("qty")),
                "revenue_last_90": _clean_float(rec.get("revenue_last_90")),
                "churn_risk_band": rec.get("churn_risk_band"),
                "churn_risk": rec.get("churn_risk_band"),
                "segment_label": rec.get("segment_label"),
            }
        )

    page_totals = {
        "revenue": _sum_numeric([rec.get("revenue") for rec in page_records]) if page_records else 0.0,
        "cost": _sum_numeric([rec.get("cost") for rec in page_records]) if page_records else 0.0,
        "profit": _sum_numeric([rec.get("profit") for rec in page_records]) if page_records else 0.0,
        "orders": _sum_numeric([rec.get("orders") for rec in page_records]) if page_records else 0.0,
    }

    table = {
        "columns": [
            "customer_id",
            "customer_name",
            "first_order",
            "last_order",
            "days_since_last_order",
            "revenue",
            "revenue_prior_window",
            "delta_revenue",
            "delta_revenue_pct",
            "delta_revenue_status",
            "cost",
            "profit",
            "profit_prior_window",
            "delta_profit",
            "delta_profit_pct",
            "margin_pct",
            "margin_prior_pct",
            "delta_margin_pct",
            "orders",
            "orders_prior_window",
            "delta_orders",
            "delta_orders_pct",
            "orders_last_30",
            "orders_last_90",
            "avg_order_value",
            "asp",
            "segment_label",
            "churn_risk_band",
        ],
        "rows": rows,
        "page": page,
        "page_size": page_size,
        "total_rows": total_rows,
        "total_pages": total_pages,
        "sort_by": sort_col,
        "sort_dir": "asc" if ascending else "desc",
        "search": search,
        "quick_filter": quick_filter_raw or "all",
        "top_n": top_n,
        "export_all": export_all,
        "page_totals": page_totals,
    }
    return table, page_totals, total_rows, total_pages


def build_customers_bundle(
    filters: Any,
    scope: Dict[str, Any],
    args: Any,
    requested_sections: Sequence[str] | None = None,
) -> Dict[str, Any]:
    started = time.perf_counter()
    normalized_sections = _normalize_requested_sections(requested_sections)
    include_overview = normalized_sections is None or "overview" in normalized_sections
    include_rfm = normalized_sections is None or "rfm" in normalized_sections
    include_clv = normalized_sections is None or "clv" in normalized_sections
    include_cohorts = normalized_sections is None or "cohorts" in normalized_sections
    requested_section_list = sorted(normalized_sections) if normalized_sections else ["overview", "rfm", "clv", "cohorts"]
    cols = fact_store.list_columns()
    date_col = _safe_col(cols, fs.CANON.date, "Date")
    revenue_col = _safe_col(cols, fs.CANON.revenue, "Revenue")
    cust_id = _safe_col(cols, fs.CANON.customer_id, "CustomerID")
    cust_name = _safe_col(cols, fs.CANON.customer_name, "Customer")
    order_col = _safe_col(cols, fs.CANON.order_id, "OrderID")
    cost_candidates = (fs.CANON.cost, "Cost", "CostPrice")
    qty_candidates = (fs.CANON.qty_units, "ShippedItems", "QuantityOrdered", "Qty", "Quantity", "Units", "ItemCount", "pack_item_count_sum")
    weight_candidates = (fs.CANON.weight_lb, "Weight", "WeightLb", "ShippedLb", "pack_weight_lb_sum")
    cost_available = any(cand in cols for cand in cost_candidates)
    weight_available = any(cand in cols for cand in weight_candidates)

    if not all([date_col, revenue_col, cust_id, cust_name, order_col]):
        return {"error": {"message": "Required columns missing for customers bundle"}, "meta": {"cached": False}}

    where_sql, where_params, start_iso, end_iso = fact_store.build_where_clause(
        filters, cols, scope, apply_default_window=True
    )

    today_ref = _utc_today_ts_naive().date()
    window_start_ts, window_end_ts = _coerce_window_bounds(start_iso, end_iso, today_ref)
    window_days = max(1, int((window_end_ts - window_start_ts).days) + 1)
    prior_end_ts = window_start_ts - pd.Timedelta(days=1)
    prior_start_ts = prior_end_ts - pd.Timedelta(days=window_days - 1)

    if is_dataclass(filters):
        prior_filters = replace(
            filters,
            start=prior_start_ts,
            end=prior_end_ts,
            preset=None,
            complete_months_only=False,
        )
    elif isinstance(filters, dict):
        prior_filters = dict(filters)
        prior_filters["start"] = prior_start_ts
        prior_filters["end"] = prior_end_ts
        prior_filters["preset"] = None
        prior_filters["complete_months_only"] = False
    else:
        prior_filters = filters
    prior_where_sql, prior_where_params, _, _ = fact_store.build_where_clause(
        prior_filters, cols, scope, apply_default_window=False
    )

    cost_raw_expr = _coalesce_numeric_expr(cols, cost_candidates, default="NULL::DOUBLE")
    qty_expr = _coalesce_numeric_expr(
        cols,
        qty_candidates,
        default="0::DOUBLE",
    )
    weight_expr = _coalesce_numeric_expr(
        cols,
        weight_candidates,
        default="0::DOUBLE",
    )
    ref_date_param = window_end_ts.date().isoformat()
    effective_cost_alias_expr = margin_rules.sql_effective_cost_expr("cost_raw", "weight_lb", "qty", fallback="NULL::DOUBLE")

    cust_sql = f"""
        WITH base AS (
            SELECT
                {cust_id} AS customer_id,
                {cust_name} AS customer_name,
                CAST({date_col} AS DATE) AS order_date,
                {revenue_col} AS revenue,
                {cost_raw_expr} AS cost_raw,
                {qty_expr} AS qty,
                {weight_expr} AS weight_lb,
                {order_col} AS order_id
            FROM fact
            WHERE {where_sql}
        ),
        scoped AS (
            SELECT
                customer_id,
                customer_name,
                order_date,
                revenue,
                cost_raw,
                qty,
                weight_lb,
                order_id,
                ({effective_cost_alias_expr}) AS cost
            FROM base
        ),
        meta AS (
            SELECT CAST(? AS DATE) AS ref_date
        )
        SELECT
            customer_id,
            ANY_VALUE(customer_name) AS customer_name,
            SUM(revenue) AS revenue,
            SUM(CASE WHEN cost_raw IS NOT NULL THEN cost ELSE 0 END) AS cost,
            SUM(revenue) - SUM(CASE WHEN cost_raw IS NOT NULL THEN cost ELSE 0 END) AS profit,
            SUM(CASE WHEN cost_raw IS NULL THEN revenue ELSE 0 END) AS revenue_missing_cost,
            SUM(qty) AS qty,
            SUM(weight_lb) AS weight_lb,
            COUNT(DISTINCT order_id) AS orders,
            MAX(order_date) AS last_order,
            MIN(order_date) AS first_order,
            DATE_DIFF('month', MIN(order_date), ref.ref_date) + 1 AS months_active,
            SUM(CASE WHEN order_date >= ref.ref_date - INTERVAL 90 DAY THEN revenue END) AS revenue_last_90,
            SUM(CASE WHEN order_date >= ref.ref_date - INTERVAL 30 DAY THEN revenue END) AS revenue_last_30,
            COUNT(DISTINCT CASE WHEN order_date >= ref.ref_date - INTERVAL 90 DAY THEN order_id END) AS orders_last_90,
            COUNT(DISTINCT CASE WHEN order_date >= ref.ref_date - INTERVAL 30 DAY THEN order_id END) AS orders_last_30,
            SUM(CASE WHEN order_date >= ref.ref_date - INTERVAL 90 DAY THEN qty END) AS qty_last_90,
            ref.ref_date AS ref_date
        FROM scoped
        CROSS JOIN meta ref
        GROUP BY 1, ref.ref_date
    """
    prior_sql = f"""
        WITH base AS (
            SELECT
                {cust_id} AS customer_id,
                {cust_name} AS customer_name,
                CAST({date_col} AS DATE) AS order_date,
                {revenue_col} AS revenue,
                {cost_raw_expr} AS cost_raw,
                {qty_expr} AS qty,
                {weight_expr} AS weight_lb,
                {order_col} AS order_id
            FROM fact
            WHERE {prior_where_sql}
        ),
        scoped AS (
            SELECT
                customer_id,
                customer_name,
                order_date,
                revenue,
                cost_raw,
                qty,
                weight_lb,
                order_id,
                ({effective_cost_alias_expr}) AS cost
            FROM base
        )
        SELECT
            customer_id,
            ANY_VALUE(customer_name) AS customer_name_prior,
            SUM(revenue) AS revenue_prior_window,
            SUM(CASE WHEN cost_raw IS NOT NULL THEN cost ELSE 0 END) AS cost_prior_window,
            SUM(qty) AS qty_prior_window,
            SUM(weight_lb) AS weight_prior_window,
            COUNT(DISTINCT order_id) AS orders_prior_window,
            MAX(order_date) AS last_order_prior,
            MIN(order_date) AS first_order_prior
        FROM scoped
        GROUP BY 1
    """

    cust_df = fact_store.execute_sql_df(
        cust_sql,
        list(where_params) + [ref_date_param],
        tag="customers.bundle.cust",
        cache_key=None,
    )
    prior_df = fact_store.execute_sql_df(
        prior_sql,
        prior_where_params,
        tag="customers.bundle.prior",
        cache_key=None,
    )

    if cust_df.empty and prior_df.empty:
        empty_payload = {
            "kpis": {
                "revenue": 0.0,
                "qty": 0.0,
                "cost": 0.0,
                "profit": 0.0,
                "margin_pct": None,
                "orders": 0,
                "customers": 0,
                "avg_order_value": 0.0,
                "repeat_rate": 0.0,
                "repeat_rate_avg": 0.0,
                "loyal_customers": 0,
                "loyal_share_pct": 0.0,
                "at_risk_90": 0,
                "active_last_30": 0,
                "new_last_90": 0,
                "revenue_last_90_total": 0.0,
                "revenue_last_30_total": 0.0,
                "top_customer": None,
                "nrr": None,
                "grr": None,
                "cost_coverage_pct": None,
                "scorecard": {},
                "executive_narrative": "",
            },
            "executive_scorecard": {},
            "executive_narrative": "",
            "churn_risk_summary": {"distribution": [], "top_at_risk_customers": []},
            "health_strip": {"chips": [], "narrative": ""},
            "trend": {"labels": [], "revenue": [], "qty": []},
            "charts": {
                "trend": {"labels": [], "revenue": [], "qty": []},
                "churn_risk_distribution": [],
                "revenue_by_segment": [],
                "lifecycle_funnel": [],
                "revenue_composition": [],
            },
            "drivers": {
                "movers": [],
                "top_gainers": [],
                "top_decliners": [],
                "decomposition": {},
                "segment_mix_change": [],
                "recommended_actions": [],
                "top_at_risk_customers": [],
            },
            "table": {
                "columns": [],
                "rows": [],
                "page": 1,
                "page_size": args.get("page_size") or 25,
                "total_rows": 0,
                "total_pages": 0,
            },
            "rfm": _empty_rfm_payload(),
            "cohorts": _empty_cohorts_payload(),
            "clv": _empty_clv_payload(
                window_start_ts=window_start_ts,
                window_end_ts=window_end_ts,
                prior_start_ts=prior_start_ts,
                prior_end_ts=prior_end_ts,
                cost_coverage_pct=None,
            ),
            "definitions": _definition_payload(),
            "meta": {
                "page": 1,
                "page_size": args.get("page_size") or 25,
                "total_rows": 0,
                "total_pages": 0,
                "sections": requested_section_list,
            },
        }
        return empty_payload

    try:
        sanitized_records: List[Dict[str, Any]] = []
        for rec in cust_df.to_dict(orient="records"):
            if not isinstance(rec, dict):
                continue
            clean_rec: Dict[str, Any] = {}
            for key, value in rec.items():
                clean_rec[key] = None if type(value).__name__ == "_NoValueType" else value
            sanitized_records.append(clean_rec)
        if sanitized_records:
            cust_df = pd.DataFrame.from_records(sanitized_records, columns=list(cust_df.columns))
    except Exception:
        pass

    try:
        sanitized_prior: List[Dict[str, Any]] = []
        for rec in prior_df.to_dict(orient="records"):
            if not isinstance(rec, dict):
                continue
            clean_rec: Dict[str, Any] = {}
            for key, value in rec.items():
                clean_rec[key] = None if type(value).__name__ == "_NoValueType" else value
            sanitized_prior.append(clean_rec)
        if sanitized_prior:
            prior_df = pd.DataFrame.from_records(sanitized_prior, columns=list(prior_df.columns))
    except Exception:
        pass

    merged = cust_df.merge(prior_df, on="customer_id", how="outer")
    merged["customer_name"] = merged.get("customer_name").where(
        merged.get("customer_name").notna(),
        merged.get("customer_name_prior"),
    )

    for col in (
        "revenue",
        "cost",
        "profit",
        "revenue_missing_cost",
        "qty",
        "weight_lb",
        "orders",
        "revenue_last_90",
        "revenue_last_30",
        "orders_last_90",
        "orders_last_30",
        "qty_last_90",
        "revenue_prior_window",
        "cost_prior_window",
        "qty_prior_window",
        "weight_prior_window",
        "orders_prior_window",
    ):
        if col in merged.columns:
            merged[col] = _clean_numeric_series(merged[col], default=0.0)
    for col in ("months_active",):
        if col in merged.columns:
            merged[col] = _clean_numeric_series(merged[col], default=None)
    for col in ("first_order", "last_order", "first_order_prior", "last_order_prior", "ref_date"):
        if col in merged.columns:
            merged[col] = _clean_datetime_series(merged[col])

    merged["first_order"] = merged["first_order"].where(merged["first_order"].notna(), merged["first_order_prior"])
    merged["last_order"] = merged["last_order"].where(merged["last_order"].notna(), merged["last_order_prior"])

    reference_ts = window_end_ts.normalize()
    merged["days_since_last"] = (
        (reference_ts - pd.to_datetime(merged.get("last_order"), errors="coerce")).dt.days.astype("float64")
    )
    merged["months_active"] = merged["months_active"].where(
        merged["months_active"].notna(),
        ((reference_ts.year - merged["first_order"].dt.year) * 12 + (reference_ts.month - merged["first_order"].dt.month) + 1),
    )
    merged["months_active"] = merged["months_active"].replace([np.inf, -np.inf], np.nan)

    merged["cost"] = pd.to_numeric(merged.get("cost"), errors="coerce")
    merged["profit"] = (
        pd.to_numeric(merged.get("revenue"), errors="coerce") - pd.to_numeric(merged.get("cost"), errors="coerce")
    ).where(pd.to_numeric(merged.get("cost"), errors="coerce").notna())

    merged["margin_pct"] = np.where(
        merged["revenue"] > 0,
        (merged["profit"] / merged["revenue"] * 100.0),
        np.nan,
    )
    merged["avg_order_value"] = np.where(
        merged["orders"] > 0,
        merged["revenue"] / merged["orders"],
        0.0,
    )
    merged["asp"] = np.where(
        merged["qty"] > 0,
        merged["revenue"] / merged["qty"],
        np.nan,
    )
    merged["units_per_order"] = np.where(
        merged["orders"] > 0,
        merged["qty"] / merged["orders"],
        np.nan,
    )
    merged["weight_per_order"] = np.where(
        merged["orders"] > 0,
        merged["weight_lb"] / merged["orders"],
        np.nan,
    )
    merged["delta_revenue"] = merged["revenue"] - merged["revenue_prior_window"]
    merged["delta_revenue_pct"] = np.where(
        merged["revenue_prior_window"] > 0,
        (merged["delta_revenue"] / merged["revenue_prior_window"]) * 100.0,
        np.nan,
    )
    merged["cost_prior_window"] = pd.to_numeric(merged.get("cost_prior_window"), errors="coerce")
    merged["profit_prior_window"] = (merged["revenue_prior_window"] - merged["cost_prior_window"]).where(
        pd.to_numeric(merged.get("cost_prior_window"), errors="coerce").notna()
    )
    merged["margin_prior_pct"] = np.where(
        merged["revenue_prior_window"] > 0,
        (merged["profit_prior_window"] / merged["revenue_prior_window"]) * 100.0,
        np.nan,
    )
    merged["delta_profit"] = merged["profit"] - merged["profit_prior_window"]
    merged["delta_profit_pct"] = np.where(
        merged["profit_prior_window"] > 0,
        (merged["delta_profit"] / merged["profit_prior_window"]) * 100.0,
        np.nan,
    )
    merged["delta_margin_pct"] = merged["margin_pct"] - merged["margin_prior_pct"]
    merged["delta_orders"] = merged["orders"] - merged["orders_prior_window"]
    merged["delta_orders_pct"] = np.where(
        merged["orders_prior_window"] > 0,
        (merged["delta_orders"] / merged["orders_prior_window"]) * 100.0,
        np.nan,
    )
    merged["delta_revenue_status"] = np.select(
        [
            (merged["revenue_prior_window"] <= 0) & (merged["revenue"] > 0),
            (merged["revenue_prior_window"] > 0) & (merged["revenue_prior_window"] < 500),
        ],
        ["new", "low_base"],
        default="normal",
    )
    merged["churn_risk_band"] = merged["days_since_last"].apply(_churn_risk_band)
    merged["segment_label"] = merged.apply(
        lambda row: _customer_segment_label(
            revenue=_clean_float(row.get("revenue")),
            revenue_prior=_clean_float(row.get("revenue_prior_window")),
            days_since=None if pd.isna(row.get("days_since_last")) else float(row.get("days_since_last")),
            first_order=row.get("first_order"),
            window_start=window_start_ts,
        ),
        axis=1,
    )

    current_mask = merged["revenue"] > 0
    prior_mask = merged["revenue_prior_window"] > 0
    active_current_df = merged.loc[current_mask].copy()
    active_prior_df = merged.loc[prior_mask].copy()

    total_revenue = _sum_numeric(merged.get("revenue", []))
    total_cost = _sum_numeric(merged.get("cost", []))
    total_profit = total_revenue - total_cost
    total_orders = int(round(_sum_numeric(merged.get("orders", []))))
    active_customers = int(current_mask.sum())
    total_customers_in_table = int(len(merged))
    avg_order_value = (total_revenue / total_orders) if total_orders else 0.0
    repeat_customers = int((active_current_df["orders"] > 1).sum()) if not active_current_df.empty else 0
    repeat_rate = float(repeat_customers / active_customers * 100.0) if active_customers else 0.0
    active_last_30 = int((active_current_df["revenue_last_30"] > 0).sum()) if not active_current_df.empty else 0
    at_risk_mask = merged["days_since_last"] >= 90
    at_risk_90 = int((current_mask & at_risk_mask).sum())
    churned_180 = int((merged["days_since_last"] >= 180).sum())
    loyal_rows = active_current_df[active_current_df["orders"] >= 4]
    loyal_customers = int(len(loyal_rows))
    loyal_share_pct = (
        _sum_numeric(loyal_rows.get("revenue", [])) / total_revenue * 100.0
        if total_revenue
        else 0.0
    )

    top_customer = None
    if not active_current_df.empty:
        top_row = active_current_df.sort_values(["revenue", "orders"], ascending=[False, False]).iloc[0]
        top_customer = {
            "id": top_row.get("customer_id"),
            "name": top_row.get("customer_name"),
            "revenue": _clean_float(top_row.get("revenue")),
            "orders": _safe_int(top_row.get("orders"), 0),
            "share": (_clean_float(top_row.get("revenue")) / total_revenue * 100.0) if total_revenue else 0.0,
            "revenue_last_90": _clean_float(top_row.get("revenue_last_90")),
            "profit": _clean_float(top_row.get("profit")),
            "churn_risk": top_row.get("churn_risk_band"),
        }

    prior_total_revenue = _sum_numeric(merged.loc[prior_mask, "revenue_prior_window"])
    retained_current_revenue = _sum_numeric(merged.loc[prior_mask, "revenue"])
    grr_numerator = 0.0
    if prior_total_revenue > 0:
        grr_numerator = float(
            (
                np.minimum(
                    merged.loc[prior_mask, "revenue"].to_numpy(dtype="float64"),
                    merged.loc[prior_mask, "revenue_prior_window"].to_numpy(dtype="float64"),
                )
            ).sum()
        )
    nrr = (retained_current_revenue / prior_total_revenue) if prior_total_revenue else None
    grr = (grr_numerator / prior_total_revenue) if prior_total_revenue else None

    prior_total_cost = _sum_numeric(merged.loc[prior_mask, "cost_prior_window"])
    prior_total_profit = prior_total_revenue - prior_total_cost
    prior_total_orders = int(round(_sum_numeric(merged.loc[prior_mask, "orders_prior_window"])))
    prior_active_customers = int(prior_mask.sum())
    prior_avg_order_value = (prior_total_revenue / prior_total_orders) if prior_total_orders else 0.0
    prior_repeat_customers = int((merged.loc[prior_mask, "orders_prior_window"] > 1).sum()) if prior_active_customers else 0
    prior_repeat_rate = float(prior_repeat_customers / prior_active_customers * 100.0) if prior_active_customers else 0.0
    prior_margin_pct = (prior_total_profit / prior_total_revenue * 100.0) if prior_total_revenue else None
    current_margin_pct = (total_profit / total_revenue * 100.0) if total_revenue else None

    at_risk_revenue_stake = _sum_numeric(merged.loc[merged["days_since_last"] >= 90, "revenue_prior_window"])

    def _delta_payload(current: Any, prior: Any, *, pct_scale: bool = False) -> Dict[str, Any]:
        current_val = _none_if_na(current)
        prior_val = _none_if_na(prior)
        try:
            c = float(current_val) if current_val is not None else None
        except Exception:
            c = None
        try:
            p = float(prior_val) if prior_val is not None else None
        except Exception:
            p = None
        if c is None:
            return {"current": None, "prior": p, "delta": None, "delta_pct": None, "status": "na"}
        if p is None:
            return {"current": c, "prior": None, "delta": None, "delta_pct": None, "status": "na"}
        delta = c - p
        if p <= 0:
            status = "new" if c > 0 else "flat"
            return {"current": c, "prior": p, "delta": delta, "delta_pct": None, "status": status}
        if (not pct_scale) and p < 500:
            return {"current": c, "prior": p, "delta": delta, "delta_pct": None, "status": "low_base"}
        delta_pct = (delta / p) * 100.0
        return {"current": c, "prior": p, "delta": delta, "delta_pct": delta_pct, "status": "normal"}

    scorecard = {
        "revenue": _delta_payload(total_revenue, prior_total_revenue),
        "profit": _delta_payload(total_profit, prior_total_profit),
        "margin_pct": _delta_payload(current_margin_pct, prior_margin_pct, pct_scale=True),
        "orders": _delta_payload(total_orders, prior_total_orders),
        "active_customers": _delta_payload(active_customers, prior_active_customers),
        "avg_order_value": _delta_payload(avg_order_value, prior_avg_order_value),
        "repeat_rate": _delta_payload(repeat_rate, prior_repeat_rate, pct_scale=True),
        "revenue_at_risk": _delta_payload(at_risk_revenue_stake, _sum_numeric(merged.loc[merged["days_since_last"] >= 180, "revenue_prior_window"])),
    }

    seg_new = merged["segment_label"] == "New"
    seg_returning = current_mask & prior_mask
    seg_reactivated = merged["segment_label"] == "Reactivated"
    growth_composition = {
        "new_count": int(seg_new.sum()),
        "returning_count": int(seg_returning.sum()),
        "reactivated_count": int(seg_reactivated.sum()),
        "new_revenue_share_pct": _safe_pct(_sum_numeric(merged.loc[seg_new, "revenue"]), total_revenue),
        "returning_revenue_share_pct": _safe_pct(_sum_numeric(merged.loc[seg_returning, "revenue"]), total_revenue),
        "reactivated_revenue_share_pct": _safe_pct(_sum_numeric(merged.loc[seg_reactivated, "revenue"]), total_revenue),
    }

    top1_share = None
    top5_share = None
    hhi = None
    if total_revenue > 0 and not active_current_df.empty:
        shares = (active_current_df["revenue"] / total_revenue).fillna(0.0)
        top1_share = float(shares.max() * 100.0)
        top5_share = float(shares.sort_values(ascending=False).head(5).sum() * 100.0)
        hhi = float((shares.pow(2).sum()) * 10000.0)

    margins = active_current_df.loc[active_current_df["revenue"] > 0, "margin_pct"].dropna()
    p10 = float(np.nanpercentile(margins, 10)) if not margins.empty else None
    p50 = float(np.nanpercentile(margins, 50)) if not margins.empty else None
    p90 = float(np.nanpercentile(margins, 90)) if not margins.empty else None
    negative_margin_mask = active_current_df["margin_pct"] < 0
    negative_margin_count = int(negative_margin_mask.sum()) if not active_current_df.empty else 0
    negative_margin_revenue = _sum_numeric(active_current_df.loc[negative_margin_mask, "revenue"]) if not active_current_df.empty else 0.0
    negative_margin_share = _safe_pct(negative_margin_revenue, total_revenue)

    tenure_days_series = (
        (reference_ts - pd.to_datetime(active_current_df.get("first_order"), errors="coerce")).dt.days.dropna()
        if not active_current_df.empty
        else pd.Series(dtype="float64")
    )
    avg_tenure_days = float(tenure_days_series.mean()) if not tenure_days_series.empty else None

    cadence_sql = f"""
        WITH orders AS (
            SELECT
                {cust_id} AS customer_id,
                {order_col} AS order_id,
                MIN(CAST({date_col} AS DATE)) AS order_date
            FROM fact
            WHERE {where_sql}
            GROUP BY 1,2
        ),
        diffs AS (
            SELECT
                DATE_DIFF(
                    'day',
                    LAG(order_date) OVER (PARTITION BY customer_id ORDER BY order_date),
                    order_date
                ) AS diff_days
            FROM orders
        )
        SELECT QUANTILE_CONT(diff_days, 0.5) AS median_days_between_orders
        FROM diffs
        WHERE diff_days IS NOT NULL AND diff_days >= 0
    """
    cadence_df = fact_store.execute_sql_df(
        cadence_sql,
        where_params,
        tag="customers.bundle.cadence",
        cache_key=None,
    )
    median_days_between_orders = None
    if not cadence_df.empty:
        median_days_between_orders = _none_if_na(cadence_df.iloc[0].get("median_days_between_orders"))
        if median_days_between_orders is not None:
            median_days_between_orders = float(median_days_between_orders)

    median_aov = None
    median_units_order = None
    median_weight_order = None
    if not active_current_df.empty:
        nonzero_orders = active_current_df[active_current_df["orders"] > 0]
        if not nonzero_orders.empty:
            median_aov = float(nonzero_orders["avg_order_value"].median())
            median_units_order = float(nonzero_orders["units_per_order"].dropna().median()) if nonzero_orders["units_per_order"].notna().any() else None
            if weight_available:
                median_weight_order = float(nonzero_orders["weight_per_order"].dropna().median()) if nonzero_orders["weight_per_order"].notna().any() else None

    at_risk_revenue = _sum_numeric(merged.loc[at_risk_mask, "revenue_last_90"])
    high_risk_customers = int((merged["churn_risk_band"] == "High").sum())
    medium_risk_customers = int((merged["churn_risk_band"] == "Medium").sum())
    low_risk_customers = int((merged["churn_risk_band"] == "Low").sum())

    cost_coverage_pct = None
    missing_cost_rows = 0
    if cost_available:
        coverage_sql = f"""
            SELECT
                COALESCE(SUM({revenue_col}), 0) AS revenue_total,
                COALESCE(SUM(CASE WHEN {cost_raw_expr} IS NULL THEN {revenue_col} ELSE 0 END), 0) AS revenue_missing_cost,
                COALESCE(COUNT(*), 0) AS row_count,
                COALESCE(SUM(CASE WHEN {cost_raw_expr} IS NULL THEN 1 ELSE 0 END), 0) AS missing_cost_rows
            FROM fact
            WHERE {where_sql}
        """
        coverage_df = fact_store.execute_sql_df(
            coverage_sql,
            where_params,
            tag="customers.bundle.cost_coverage",
            cache_key=None,
        )
        if not coverage_df.empty:
            cov_row = coverage_df.iloc[0]
            revenue_total_cov = _clean_float(cov_row.get("revenue_total"))
            revenue_missing_cov = _clean_float(cov_row.get("revenue_missing_cost"))
            missing_cost_rows = _safe_int(cov_row.get("missing_cost_rows"), 0)
            if revenue_total_cov > 0:
                cost_coverage_pct = max(0.0, min(100.0, (1.0 - revenue_missing_cov / revenue_total_cov) * 100.0))

    segment_order = ["New", "Growing", "Stable", "At Risk", "Churned", "Reactivated"]
    risk_order = ["Low", "Medium", "High"]
    risk_rows = []
    for band in risk_order:
        risk_rows.append(
            {
                "band": band,
                "customers": int((merged["churn_risk_band"] == band).sum()),
            }
        )
    segment_rows = []
    for segment_name in segment_order:
        rev_val = _sum_numeric(merged.loc[merged["segment_label"] == segment_name, "revenue"])
        segment_rows.append({"segment": segment_name, "revenue": rev_val})

    movers_rows: List[Dict[str, Any]] = []
    for row in merged.itertuples():
        curr = _clean_float(getattr(row, "revenue", 0.0))
        prev = _clean_float(getattr(row, "revenue_prior_window", 0.0))
        delta = curr - prev
        if prev <= 0.0 and curr > 0.0:
            state = "New"
        elif curr <= 0.0 and prev > 0.0:
            state = "Lost"
        elif delta > 0:
            state = "Gainer"
        elif delta < 0:
            state = "Decliner"
        else:
            state = "Flat"
        movers_rows.append(
            {
                "customer_id": getattr(row, "customer_id", None),
                "customer_name": getattr(row, "customer_name", None),
                "revenue": curr,
                "revenue_prior_window": prev,
                "delta_revenue": delta,
                "delta_revenue_pct": None if prev <= 0 else (delta / prev * 100.0),
                "status": state,
                "segment_label": getattr(row, "segment_label", None),
                "churn_risk_band": getattr(row, "churn_risk_band", None),
            }
        )
    movers_rows = sorted(
        movers_rows,
        key=lambda rec: (_clean_float(rec.get("delta_revenue")), str(rec.get("customer_id") or "")),
        reverse=True,
    )
    top_gainers = [rec for rec in movers_rows if _clean_float(rec.get("delta_revenue")) > 0][:10]
    top_decliners = sorted(
        [rec for rec in movers_rows if _clean_float(rec.get("delta_revenue")) < 0],
        key=lambda rec: _clean_float(rec.get("delta_revenue")),
    )[:10]

    active_current = int(current_mask.sum())
    active_prior = int(prior_mask.sum())
    arpa_current = (total_revenue / active_current) if active_current else 0.0
    arpa_prior = (prior_total_revenue / active_prior) if active_prior else 0.0
    delta_count_component = (active_current - active_prior) * arpa_prior
    delta_arpa_component = active_current * (arpa_current - arpa_prior)
    total_delta_revenue = total_revenue - prior_total_revenue
    decomposition = {
        "active_customers_current": active_current,
        "active_customers_prior": active_prior,
        "avg_revenue_per_active_current": arpa_current,
        "avg_revenue_per_active_prior": arpa_prior,
        "delta_revenue_total": total_delta_revenue,
        "delta_from_active_customer_count": delta_count_component,
        "delta_from_avg_revenue_per_active": delta_arpa_component,
        "delta_residual": total_delta_revenue - delta_count_component - delta_arpa_component,
    }

    new_count = growth_composition["new_count"]
    reactivated_count = growth_composition["reactivated_count"]
    new_share_pct = growth_composition["new_revenue_share_pct"] or 0.0
    returning_share_pct = growth_composition["returning_revenue_share_pct"] or 0.0
    reactivation_rate_pct = (_safe_pct(reactivated_count, active_customers) or 0.0) if active_customers else 0.0
    high_risk_frame = merged.loc[merged["churn_risk_band"] == "High"].copy()
    high_risk_frame = high_risk_frame.sort_values(["revenue_prior_window", "revenue"], ascending=[False, False])
    top_at_risk_customers: List[Dict[str, Any]] = []
    for row in high_risk_frame.head(10).itertuples():
        top_at_risk_customers.append(
            {
                "customer_id": getattr(row, "customer_id", None),
                "customer_name": getattr(row, "customer_name", None),
                "revenue_prior_window": _clean_float(getattr(row, "revenue_prior_window", 0.0)),
                "revenue": _clean_float(getattr(row, "revenue", 0.0)),
                "days_since_last": _safe_int(getattr(row, "days_since_last", 0), 0),
                "segment_label": getattr(row, "segment_label", None),
            }
        )

    health_strip = {
        "chips": [
            {"key": "active_30d", "label": "Active (30d)", "value": active_last_30},
            {"key": "new_customers", "label": "New in window", "value": new_count},
            {"key": "reactivated", "label": "Reactivated", "value": reactivated_count},
            {"key": "at_risk_90d", "label": "At risk (>=90d)", "value": at_risk_90},
            {"key": "at_risk_revenue", "label": "Revenue at risk", "value": at_risk_revenue, "format": "currency"},
            {"key": "repeat_rate", "label": "Repeat rate", "value": repeat_rate, "format": "percent"},
            {"key": "cost_coverage_pct", "label": "Cost coverage", "value": cost_coverage_pct, "format": "percent"},
            {"key": "unit_cost_gaps", "label": "Unit cost gaps", "value": missing_cost_rows},
        ],
        "narrative": (
            f"Active customers: {active_last_30}, At risk: {at_risk_90} "
            f"(Revenue at risk: ${at_risk_revenue:,.0f}). "
            f"New: {new_count} (share {new_share_pct:.1f}%)."
        ),
    }

    churn_risk_distribution = [
        {"band": "Low", "customers": low_risk_customers, "threshold": "<60 days"},
        {"band": "Medium", "customers": medium_risk_customers, "threshold": "60-89 days"},
        {"band": "High", "customers": high_risk_customers, "threshold": ">=90 days"},
    ]
    churn_risk_summary = {
        "model": "Recency risk (rule-based)",
        "thresholds": {"low_max_days": 59, "medium_min_days": 60, "medium_max_days": 89, "high_min_days": 90},
        "distribution": churn_risk_distribution,
        "top_at_risk_customers": top_at_risk_customers,
        "at_risk_customers": at_risk_90,
        "revenue_at_risk": at_risk_revenue,
    }

    seg_new_mask = merged["segment_label"] == "New"
    seg_reactivated_mask = merged["segment_label"] == "Reactivated"
    seg_at_risk_buying_mask = (merged["segment_label"] == "At Risk") & current_mask
    seg_returning_mask = current_mask & (~seg_new_mask) & (~seg_reactivated_mask) & (~seg_at_risk_buying_mask)
    rev_new = _sum_numeric(merged.loc[seg_new_mask, "revenue"])
    rev_returning = _sum_numeric(merged.loc[seg_returning_mask, "revenue"])
    rev_reactivated = _sum_numeric(merged.loc[seg_reactivated_mask, "revenue"])
    rev_at_risk_buying = _sum_numeric(merged.loc[seg_at_risk_buying_mask, "revenue"])
    rev_other = max(0.0, total_revenue - rev_new - rev_returning - rev_reactivated - rev_at_risk_buying)

    lifecycle_funnel_rows = [
        {"stage": "New", "customers": new_count},
        {"stage": "Active", "customers": active_last_30},
        {"stage": "At Risk", "customers": at_risk_90},
        {"stage": "Churned", "customers": churned_180},
    ]
    revenue_composition_rows = [
        {"segment": "New", "revenue": rev_new},
        {"segment": "Returning", "revenue": rev_returning},
        {"segment": "Reactivated", "revenue": rev_reactivated},
        {"segment": "At Risk", "revenue": rev_at_risk_buying},
        {"segment": "Other", "revenue": rev_other},
    ]

    merged["days_since_last_prior"] = (
        (prior_end_ts.normalize() - pd.to_datetime(merged.get("last_order_prior"), errors="coerce")).dt.days.astype("float64")
    )
    prior_new_mask = prior_mask & (pd.to_datetime(merged.get("first_order_prior"), errors="coerce") >= prior_start_ts)
    prior_returning_mask = prior_mask & (~prior_new_mask)
    prior_at_risk_mask = prior_mask & (merged["days_since_last_prior"] >= 90)
    segment_mix_change = [
        {
            "segment": "New",
            "customers_current": new_count,
            "customers_prior": int(prior_new_mask.sum()),
            "revenue_share_current_pct": new_share_pct,
            "revenue_share_prior_pct": _safe_pct(_sum_numeric(merged.loc[prior_new_mask, "revenue_prior_window"]), prior_total_revenue) or 0.0,
        },
        {
            "segment": "Returning",
            "customers_current": int(seg_returning_mask.sum()),
            "customers_prior": int(prior_returning_mask.sum()),
            "revenue_share_current_pct": returning_share_pct,
            "revenue_share_prior_pct": _safe_pct(_sum_numeric(merged.loc[prior_returning_mask, "revenue_prior_window"]), prior_total_revenue) or 0.0,
        },
        {
            "segment": "At Risk",
            "customers_current": at_risk_90,
            "customers_prior": int(prior_at_risk_mask.sum()),
            "revenue_share_current_pct": _safe_pct(_sum_numeric(merged.loc[at_risk_mask, "revenue"]), total_revenue) or 0.0,
            "revenue_share_prior_pct": _safe_pct(_sum_numeric(merged.loc[prior_at_risk_mask, "revenue_prior_window"]), prior_total_revenue) or 0.0,
        },
    ]

    recommended_actions: List[str] = []
    if top_at_risk_customers:
        names = [str(row.get("customer_name") or row.get("customer_id") or "") for row in top_at_risk_customers[:5]]
        recommended_actions.append(
            f"Prioritize at-risk high revenue customers: {', '.join([name for name in names if name][:5])}."
        )
    if missing_cost_rows > 0:
        recommended_actions.append(
            f"Address cost gaps affecting profit visibility ({missing_cost_rows} rows without cost)."
        )
    if top_decliners:
        names = [str(row.get("customer_name") or row.get("customer_id") or "") for row in top_decliners[:5]]
        recommended_actions.append(
            f"Review top decliners to prevent churn: {', '.join([name for name in names if name][:5])}."
        )
    if not recommended_actions:
        recommended_actions.append("No critical actions detected for this window.")

    top_gainer_name = top_gainers[0].get("customer_name") if top_gainers else None
    top_decliner_name = top_decliners[0].get("customer_name") if top_decliners else None
    executive_narrative = (
        f"Revenue {total_revenue:,.0f} ({'+' if total_delta_revenue >= 0 else ''}{total_delta_revenue:,.0f} vs prior). "
        f"NRR {((nrr or 0.0) * 100.0):.1f}% and GRR {((grr or 0.0) * 100.0):.1f}%. "
        f"Top gain: {top_gainer_name or 'n/a'}; top decline: {top_decliner_name or 'n/a'}."
    )

    kpis = {
        "revenue": total_revenue,
        "cost": total_cost,
        "profit": total_profit,
        "margin_pct": (total_profit / total_revenue * 100.0) if total_revenue else None,
        "orders": total_orders,
        "customers": active_customers,
        "customers_in_table": total_customers_in_table,
        "avg_order_value": avg_order_value,
        "repeat_rate": repeat_rate,
        "repeat_rate_avg": repeat_rate,
        "active_last_30": active_last_30,
        "new_last_90": new_count,
        "at_risk_90": at_risk_90,
        "churned_180": churned_180,
        "loyal_customers": loyal_customers,
        "loyal_share_pct": loyal_share_pct,
        "revenue_last_90_total": _sum_numeric(active_current_df.get("revenue_last_90", [])),
        "revenue_last_30_total": _sum_numeric(active_current_df.get("revenue_last_30", [])),
        "revenue_at_risk": at_risk_revenue,
        "high_risk_customers": high_risk_customers,
        "medium_risk_customers": medium_risk_customers,
        "low_risk_customers": low_risk_customers,
        "avg_churn_probability": None,
        "top_customer": top_customer,
        "window": {
            "start": window_start_ts.date().isoformat(),
            "end": window_end_ts.date().isoformat(),
            "prior_start": prior_start_ts.date().isoformat(),
            "prior_end": prior_end_ts.date().isoformat(),
        },
        "nrr": nrr,
        "grr": grr,
        "growth_composition": growth_composition,
        "top1_share_pct": top1_share,
        "top5_share_pct": top5_share,
        "hhi": hhi,
        "margin_p10_pct": p10,
        "margin_p50_pct": p50,
        "margin_p90_pct": p90,
        "negative_margin_customers": negative_margin_count,
        "negative_margin_revenue_share_pct": negative_margin_share,
        "avg_tenure_days": avg_tenure_days,
        "churn_risk_revenue_at_stake": at_risk_revenue,
        "median_days_between_orders": median_days_between_orders,
        "median_aov": median_aov,
        "median_units_per_order": median_units_order,
        "median_weight_per_order": median_weight_order,
        "cost_coverage_pct": cost_coverage_pct,
        "cost_missing_rows": missing_cost_rows,
        "reactivation_rate_pct": reactivation_rate_pct,
        "new_customer_revenue_share_pct": new_share_pct,
        "returning_customer_revenue_share_pct": returning_share_pct,
        "scorecard": scorecard,
        "executive_narrative": executive_narrative,
        "churn_risk_summary": churn_risk_summary,
    }

    table, page_totals, total_rows, total_pages = _table_payload(merged, args)

    if include_rfm:
        rfm_export_all = _is_truthy(args.get("export_all")) or _is_truthy(args.get("rfm_export_all"))
        try:
            if is_dataclass(filters):
                rfm_filters = replace(
                    filters,
                    start=None,
                    end=None,
                    preset=None,
                    complete_months_only=False,
                )
            elif isinstance(filters, dict):
                rfm_filters = dict(filters)
                rfm_filters["start"] = None
                rfm_filters["end"] = None
                rfm_filters["preset"] = None
                rfm_filters["complete_months_only"] = False
            else:
                rfm_filters = filters
    
            rfm_where_sql, rfm_where_params, _, _ = fact_store.build_where_clause(
                rfm_filters, cols, scope, apply_default_window=False
            )
            rfm_settings = _parse_rfm_settings(
                args,
                window_end_ts=window_end_ts,
                cost_available=cost_available,
                cost_coverage_pct=cost_coverage_pct,
            )
            lookback_start_ts = max(rfm_settings["lookback_start"], window_start_ts.normalize())
            lookback_end_ts = rfm_settings["lookback_end"]
            if lookback_end_ts < lookback_start_ts:
                lookback_start_ts = lookback_end_ts
            lookback_days = max(1, int((lookback_end_ts - lookback_start_ts).days) + 1)
            prior_end_lb = (lookback_start_ts - pd.Timedelta(days=1)).normalize()
            prior_start_lb = (prior_end_lb - pd.Timedelta(days=lookback_days - 1)).normalize()
    
            rfm_cost_expr = cost_raw_expr
            rfm_weight_expr = weight_expr
            rfm_qty_expr = qty_expr
            rfm_sql = f"""
                WITH base AS (
                    SELECT
                        {cust_id} AS customer_id,
                        {cust_name} AS customer_name,
                        CAST({date_col} AS DATE) AS order_date,
                        {order_col} AS order_id,
                        {revenue_col} AS revenue,
                        {rfm_cost_expr} AS cost_raw,
                        {rfm_qty_expr} AS qty,
                        {rfm_weight_expr} AS weight_lb
                    FROM fact
                    WHERE {rfm_where_sql}
                ),
                lookback AS (
                    SELECT *
                    FROM base
                    WHERE order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
                ),
                prior AS (
                    SELECT
                        customer_id,
                        SUM(revenue) AS prior_revenue,
                        COUNT(DISTINCT order_id) AS prior_frequency
                    FROM base
                    WHERE order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
                    GROUP BY 1
                ),
                first_all AS (
                    SELECT
                        customer_id,
                        MIN(order_date) AS first_order_all
                    FROM base
                    GROUP BY 1
                ),
                orders AS (
                    SELECT
                        customer_id,
                        ANY_VALUE(customer_name) AS customer_name,
                        order_id,
                        MIN(order_date) AS order_date,
                        SUM(revenue) AS order_revenue,
                        SUM(COALESCE(cost_raw, 0)) AS order_cost,
                        SUM(qty) AS order_qty,
                        SUM(weight_lb) AS order_weight
                    FROM lookback
                    GROUP BY 1, 3
                ),
                cust AS (
                    SELECT
                        customer_id,
                        ANY_VALUE(customer_name) AS customer_name,
                        MIN(order_date) AS first_order_lookback,
                        MAX(order_date) AS last_order_date,
                        COUNT(DISTINCT order_id) AS frequency,
                        SUM(order_revenue) AS revenue,
                        SUM(order_cost) AS cost,
                        SUM(order_qty) AS qty,
                        SUM(order_weight) AS weight_lb
                    FROM orders
                    GROUP BY 1
                ),
                gaps AS (
                    SELECT
                        customer_id,
                        DATE_DIFF(
                            'day',
                            LAG(order_date) OVER (PARTITION BY customer_id ORDER BY order_date),
                            order_date
                        ) AS gap_days
                    FROM orders
                ),
                gap_stats AS (
                    SELECT
                        customer_id,
                        QUANTILE_CONT(gap_days, 0.5) AS median_gap_days
                    FROM gaps
                    WHERE gap_days IS NOT NULL AND gap_days >= 0
                    GROUP BY 1
                )
                SELECT
                    c.customer_id,
                    c.customer_name,
                    c.first_order_lookback,
                    c.last_order_date,
                    c.frequency,
                    c.revenue,
                    c.cost,
                    c.qty,
                    c.weight_lb,
                    COALESCE(p.prior_revenue, 0) AS prior_revenue,
                    COALESCE(p.prior_frequency, 0) AS prior_frequency,
                    fa.first_order_all,
                    gs.median_gap_days
                FROM cust c
                LEFT JOIN prior p ON p.customer_id = c.customer_id
                LEFT JOIN first_all fa ON fa.customer_id = c.customer_id
                LEFT JOIN gap_stats gs ON gs.customer_id = c.customer_id
            """
            rfm_df = fact_store.execute_sql_df(
                rfm_sql,
                list(rfm_where_params)
                + [
                    lookback_start_ts.date().isoformat(),
                    lookback_end_ts.date().isoformat(),
                    prior_start_lb.date().isoformat(),
                    prior_end_lb.date().isoformat(),
                ],
                tag="customers.bundle.rfm_v2",
                cache_key=None,
            )
    
            if not rfm_df.empty:
                for col in ("frequency", "revenue", "cost", "qty", "weight_lb", "prior_revenue", "prior_frequency", "median_gap_days"):
                    if col in rfm_df.columns:
                        rfm_df[col] = _clean_numeric_series(rfm_df[col], default=0.0)
                for col in ("first_order_lookback", "last_order_date", "first_order_all"):
                    if col in rfm_df.columns:
                        rfm_df[col] = _clean_datetime_series(rfm_df[col])
    
                rfm_df["recency_days"] = (
                    (lookback_end_ts.normalize() - pd.to_datetime(rfm_df["last_order_date"], errors="coerce"))
                    .dt.days.astype("float64")
                )
                rfm_df["profit"] = rfm_df["revenue"] - rfm_df["cost"]
                rfm_df["avg_order_value"] = np.where(
                    rfm_df["frequency"] > 0,
                    rfm_df["revenue"] / rfm_df["frequency"],
                    0.0,
                )
                rfm_df["delta_revenue"] = rfm_df["revenue"] - rfm_df["prior_revenue"]
                rfm_df["delta_revenue_pct"] = np.where(
                    rfm_df["prior_revenue"] > 0,
                    (rfm_df["delta_revenue"] / rfm_df["prior_revenue"]) * 100.0,
                    np.nan,
                )
    
                monetary_metric = rfm_settings["monetary_metric"]
                if monetary_metric in {"profit", "gross_profit"}:
                    rfm_df["monetary_value"] = rfm_df["profit"]
                else:
                    rfm_df["monetary_value"] = rfm_df["revenue"]
    
                rfm_scores_v2 = _rfm_scores_v2(
                    rfm_df["recency_days"],
                    rfm_df["frequency"],
                    rfm_df["monetary_value"],
                    method=rfm_settings["scoring_method"],
                    recency_thresholds=rfm_settings["recency_thresholds"],
                    frequency_thresholds=rfm_settings["frequency_thresholds"],
                    monetary_thresholds=rfm_settings["monetary_thresholds"],
                )
                rfm_df = pd.concat([rfm_df.reset_index(drop=True), rfm_scores_v2.reset_index(drop=True)], axis=1)
                rfm_df["segment"] = rfm_df.apply(
                    lambda row: _rfm_segment_v2(
                        _safe_int(row.get("r_score"), 1),
                        _safe_int(row.get("f_score"), 1),
                        _safe_int(row.get("m_score"), 1),
                        row.get("recency_days"),
                    ),
                    axis=1,
                )
                rfm_df["is_reactivated"] = (
                    (rfm_df["prior_revenue"] <= 0)
                    & (pd.to_datetime(rfm_df["first_order_all"], errors="coerce") < lookback_start_ts)
                    & (rfm_df["frequency"] > 0)
                )
    
                segment_order_v2 = [
                    "Champions",
                    "Loyal",
                    "Big Spenders",
                    "Potential Loyalists",
                    "New Customers",
                    "Promising",
                    "Needs Attention",
                    "About to Sleep",
                    "At Risk",
                    "Can't Lose Them",
                    "Hibernating",
                    "Lost",
                ]
                total_rfm_revenue = _sum_numeric(rfm_df["revenue"])
                segment_summary: List[Dict[str, Any]] = []
                for seg_name in segment_order_v2:
                    seg_frame = rfm_df[rfm_df["segment"] == seg_name]
                    if seg_frame.empty:
                        segment_summary.append(
                            {
                                "segment": seg_name,
                                "customers": 0,
                                "revenue": 0.0,
                                "profit": 0.0,
                                "avg_orders": 0.0,
                                "avg_aov": 0.0,
                                "median_recency_days": None,
                                "revenue_share_pct": 0.0,
                                "delta_revenue": 0.0,
                                "playbook": _rfm_playbook_actions(seg_name),
                            }
                        )
                        continue
                    seg_revenue = _sum_numeric(seg_frame["revenue"])
                    seg_profit = _sum_numeric(seg_frame["profit"])
                    seg_delta = _sum_numeric(seg_frame["delta_revenue"])
                    segment_summary.append(
                        {
                            "segment": seg_name,
                            "customers": int(len(seg_frame)),
                            "revenue": seg_revenue,
                            "profit": seg_profit,
                            "avg_orders": float(pd.to_numeric(seg_frame["frequency"], errors="coerce").mean()),
                            "avg_aov": float(pd.to_numeric(seg_frame["avg_order_value"], errors="coerce").mean()),
                            "median_recency_days": float(pd.to_numeric(seg_frame["recency_days"], errors="coerce").median()),
                            "revenue_share_pct": (_safe_pct(seg_revenue, total_rfm_revenue) or 0.0),
                            "delta_revenue": seg_delta,
                            "playbook": _rfm_playbook_actions(seg_name),
                        }
                    )
    
                segment_playbooks: List[Dict[str, Any]] = []
                for seg_name in segment_order_v2:
                    seg_frame = rfm_df[rfm_df["segment"] == seg_name]
                    if seg_frame.empty:
                        continue
                    top_accounts = (
                        seg_frame.sort_values(["revenue", "frequency"], ascending=[False, False])
                        .head(5)
                    )
                    key_accounts: List[Dict[str, Any]] = []
                    for _, account in top_accounts.iterrows():
                        customer_id_val = str(account.get("customer_id") or "").strip()
                        key_accounts.append(
                            {
                                "customer_id": customer_id_val or None,
                                "customer_name": account.get("customer_name"),
                                "revenue": _clean_float(account.get("revenue")),
                                "url": f"/customers/drilldown/{customer_id_val}" if customer_id_val else None,
                            }
                        )
                    segment_playbooks.append(
                        {
                            "segment": seg_name,
                            "goal": _rfm_playbook_goal(seg_name),
                            "actions": _rfm_playbook_actions(seg_name),
                            "customers": int(len(seg_frame)),
                            "revenue": _sum_numeric(seg_frame["revenue"]),
                            "key_accounts": key_accounts,
                        }
                    )
    
                matrix_rows: List[Dict[str, Any]] = []
                total_rev_for_share = _sum_numeric(rfm_df["revenue"])
                for r_score in [5, 4, 3, 2, 1]:
                    row_cells: List[Dict[str, Any]] = []
                    for f_score in [1, 2, 3, 4, 5]:
                        cell_mask = (rfm_df["r_score"] == r_score) & (rfm_df["f_score"] == f_score)
                        cell_count = int(cell_mask.sum())
                        cell_revenue = _sum_numeric(rfm_df.loc[cell_mask, "revenue"])
                        row_cells.append(
                            {
                                "r_score": r_score,
                                "f_score": f_score,
                                "customers": cell_count,
                                "revenue": cell_revenue,
                                "revenue_share_pct": (_safe_pct(cell_revenue, total_rev_for_share) or 0.0),
                                "active": bool(
                                    rfm_settings.get("heat_r") == r_score and rfm_settings.get("heat_f") == f_score
                                ),
                            }
                        )
                    matrix_rows.append({"r_score": r_score, "cells": row_cells})
    
                recency_hist = _build_histogram_payload(rfm_df["recency_days"], max_bins=10)
                monetary_hist = _build_histogram_payload(
                    rfm_df["monetary_value"],
                    weights=rfm_df["revenue"],
                    max_bins=10,
                )
    
                repeat_rate_lb = (
                    float((rfm_df["frequency"] > 1).sum() / len(rfm_df) * 100.0)
                    if len(rfm_df)
                    else 0.0
                )
                at_risk_count = int(rfm_df["segment"].isin(["At Risk", "Can't Lose Them"]).sum())
                cant_lose_count = int((rfm_df["segment"] == "Can't Lose Them").sum())
                new_customers_count = int((rfm_df["segment"] == "New Customers").sum())
                reactivated_count = int(rfm_df["is_reactivated"].sum())
                at_risk_stake = _sum_numeric(
                    rfm_df.loc[rfm_df["segment"].isin(["At Risk", "Can't Lose Them"]), "prior_revenue"]
                )
                reactivation_rate = (
                    float(rfm_df["is_reactivated"].sum() / len(rfm_df) * 100.0)
                    if len(rfm_df)
                    else 0.0
                )
                top_segment_revenue = max(segment_summary, key=lambda rec: _clean_float(rec.get("revenue")), default=None)
                top_segment_growth = max(segment_summary, key=lambda rec: _clean_float(rec.get("delta_revenue")), default=None)
    
                insights = {
                    "customers_lookback": int(len(rfm_df)),
                    "revenue_lookback": _sum_numeric(rfm_df["revenue"]),
                    "repeat_rate_lookback": repeat_rate_lb,
                    "at_risk_revenue_stake": at_risk_stake,
                    "reactivation_rate": reactivation_rate,
                    "at_risk_customers": at_risk_count,
                    "cant_lose_customers": cant_lose_count,
                    "new_customers": new_customers_count,
                    "reactivated_customers": reactivated_count,
                    "top_segment_by_revenue": top_segment_revenue.get("segment") if top_segment_revenue else None,
                    "top_segment_by_growth": top_segment_growth.get("segment") if top_segment_growth else None,
                }
                risk_opportunity = {
                    "at_risk_customers": at_risk_count,
                    "at_risk_revenue_stake": at_risk_stake,
                    "cant_lose_customers": cant_lose_count,
                    "new_customers": new_customers_count,
                    "reactivated_customers": reactivated_count,
                }
    
                filtered_rfm_df = _rfm_filter_frame(rfm_df, rfm_settings)
                top_mode = rfm_settings["top_mode"]
                if top_mode == "monetary":
                    top_sorted = filtered_rfm_df.sort_values(
                        ["monetary_value", "rfm_score", "revenue"],
                        ascending=[False, False, False],
                    )
                elif top_mode == "at_risk":
                    top_sorted = filtered_rfm_df.assign(
                        _risk_rank=np.where(
                            filtered_rfm_df["segment"].isin(["Can't Lose Them", "At Risk"]),
                            0,
                            np.where(filtered_rfm_df["segment"].isin(["About to Sleep", "Hibernating", "Lost"]), 1, 2),
                        )
                    ).sort_values(
                        ["_risk_rank", "prior_revenue", "recency_days", "rfm_score"],
                        ascending=[True, False, False, False],
                    )
                else:
                    top_sorted = filtered_rfm_df.sort_values(
                        ["rfm_score", "m_score", "revenue"],
                        ascending=[False, False, False],
                    )
    
                table_sort = str(args.get("rfm_sort_by") or "rfm_score").strip().lower()
                table_dir = str(args.get("rfm_sort_dir") or "desc").strip().lower()
                asc = table_dir in {"asc", "1", "true", "up"}
                sort_map = {
                    "customer": "customer_name",
                    "segment": "segment",
                    "r": "r_score",
                    "f": "f_score",
                    "m": "m_score",
                    "rfm_score": "rfm_score",
                    "monetary": "monetary_value",
                    "revenue": "revenue",
                    "profit": "profit",
                    "frequency": "frequency",
                    "recency": "recency_days",
                    "last_order": "last_order_date",
                    "delta_revenue": "delta_revenue",
                }
                sort_col_rfm = sort_map.get(table_sort, "rfm_score")
                full_sorted = filtered_rfm_df.sort_values(
                    [sort_col_rfm, "customer_id"],
                    ascending=[asc, True],
                    na_position="last",
                )
    
                top_n = int(rfm_settings["top_n"])
                top_display = top_sorted if rfm_export_all else top_sorted.head(top_n)
    
                total_filtered = int(len(full_sorted))
                page_num = int(rfm_settings["table_page"])
                page_size_num = int(rfm_settings["table_page_size"])
                if rfm_export_all:
                    paged = full_sorted
                    page_num = 1
                    page_size_num = max(1, total_filtered)
                    total_pages_num = 1 if total_filtered else 0
                else:
                    start_idx = (page_num - 1) * page_size_num
                    end_idx = start_idx + page_size_num
                    paged = full_sorted.iloc[start_idx:end_idx]
                    total_pages_num = int(math.ceil(total_filtered / page_size_num)) if page_size_num else 0
    
                def _rfm_row_payload(row: pd.Series) -> Dict[str, Any]:
                    customer_id_val = str(row.get("customer_id") or "").strip()
                    return {
                        "customer_id": customer_id_val or row.get("customer_id"),
                        "customer_name": row.get("customer_name"),
                        "segment": row.get("segment"),
                        "r_score": _safe_int(row.get("r_score"), 1),
                        "f_score": _safe_int(row.get("f_score"), 1),
                        "m_score": _safe_int(row.get("m_score"), 1),
                        "rfm_score": _safe_int(row.get("rfm_score"), 3),
                        "monetary": _clean_float(row.get("monetary_value")),
                        "monetary_metric": monetary_metric,
                        "revenue": _clean_float(row.get("revenue")),
                        "profit": _clean_float(row.get("profit")),
                        "frequency": _safe_int(row.get("frequency"), 0),
                        "recency_days": _safe_int(row.get("recency_days"), 0),
                        "last_order_date": (
                            pd.to_datetime(row.get("last_order_date"), errors="coerce").date().isoformat()
                            if pd.notna(pd.to_datetime(row.get("last_order_date"), errors="coerce"))
                            else None
                        ),
                        "delta_revenue": _clean_float(row.get("delta_revenue")),
                        "delta_revenue_pct": _none_if_na(row.get("delta_revenue_pct")),
                        "prior_revenue": _clean_float(row.get("prior_revenue")),
                        "median_days_between_orders": _none_if_na(row.get("median_gap_days")),
                        "drilldown_url": f"/customers/drilldown/{customer_id_val}" if customer_id_val else None,
                    }
    
                top_rows_payload = [_rfm_row_payload(rec) for _, rec in top_display.iterrows()]
                full_rows_payload = [_rfm_row_payload(rec) for _, rec in paged.iterrows()]
    
                scatter_points = []
                for row in filtered_rfm_df.itertuples():
                    customer_id_val = getattr(row, "customer_id", None)
                    customer_name_val = getattr(row, "customer_name", None)
                    scatter_points.append(
                        {
                            "customer_id": customer_id_val,
                            "customer_name": customer_name_val,
                            "segment": getattr(row, "segment", None),
                            "r_score": _safe_int(getattr(row, "r_score", None), 1),
                            "f_score": _safe_int(getattr(row, "f_score", None), 1),
                            "m_score": _safe_int(getattr(row, "m_score", None), 1),
                            "rfm_score": _safe_int(getattr(row, "rfm_score", None), 3),
                            "frequency": _safe_int(getattr(row, "frequency", None), 0),
                            "recency_days": _safe_int(getattr(row, "recency_days", None), 0),
                            "monetary": _clean_float(getattr(row, "monetary_value", None)),
                            "revenue": _clean_float(getattr(row, "revenue", None)),
                            "orders": _safe_int(getattr(row, "frequency", None), 0),
                            "last_order_date": (
                                pd.to_datetime(getattr(row, "last_order_date", None), errors="coerce").date().isoformat()
                                if pd.notna(pd.to_datetime(getattr(row, "last_order_date", None), errors="coerce"))
                                else None
                            ),
                            "url": f"/customers/drilldown/{customer_id_val}",
                        }
                    )
    
                freq_median = float(pd.to_numeric(filtered_rfm_df["frequency"], errors="coerce").median()) if not filtered_rfm_df.empty else 0.0
                mon_median = float(pd.to_numeric(filtered_rfm_df["monetary_value"], errors="coerce").median()) if not filtered_rfm_df.empty else 0.0
                rec_median = float(pd.to_numeric(filtered_rfm_df["recency_days"], errors="coerce").median()) if not filtered_rfm_df.empty else 0.0
                donut_segments = [
                    {"segment": row["segment"], "count": int(row["customers"])}
                    for row in segment_summary
                    if int(row.get("customers") or 0) > 0
                ]
                donut_segments = sorted(donut_segments, key=lambda rec: rec["count"], reverse=True)
    
                legacy_top = top_sorted.head(10)
                rfm_payload = {
                    "settings": {
                        "lookback_months": int(rfm_settings["lookback_months"]),
                        "lookback_start": lookback_start_ts.date().isoformat(),
                        "lookback_end": lookback_end_ts.date().isoformat(),
                        "computed_window_note": (
                            f"Computed using last {int(rfm_settings['lookback_months'])} months ending on {lookback_end_ts.date().isoformat()}."
                        ),
                        "prior_start": prior_start_lb.date().isoformat(),
                        "prior_end": prior_end_lb.date().isoformat(),
                        "scoring_method": rfm_settings["scoring_method"],
                        "requested_monetary_metric": rfm_settings["requested_monetary_metric"],
                        "monetary_metric": monetary_metric,
                        "recency_thresholds": rfm_settings["recency_thresholds"],
                        "frequency_thresholds": rfm_settings["frequency_thresholds"],
                        "monetary_thresholds": rfm_settings["monetary_thresholds"],
                        "top_mode": top_mode,
                        "scatter_mode": rfm_settings["scatter_mode"],
                        "monetary_caveat": rfm_settings["monetary_caveat"],
                        "no_activity_policy": "excluded",
                        "params_hash": rfm_settings["params_hash"],
                    },
                    "filters": {
                        "search": rfm_settings["search"],
                        "segments": rfm_settings["segments"],
                        "at_risk_only": bool(rfm_settings["at_risk_only"]),
                        "heat_r": rfm_settings["heat_r"],
                        "heat_f": rfm_settings["heat_f"],
                        "r_min": int(rfm_settings["r_min"]),
                        "r_max": int(rfm_settings["r_max"]),
                        "f_min": int(rfm_settings["f_min"]),
                        "f_max": int(rfm_settings["f_max"]),
                        "m_min": int(rfm_settings["m_min"]),
                        "m_max": int(rfm_settings["m_max"]),
                    },
                    "insights": insights,
                    "risk_opportunity": risk_opportunity,
                    "segment_summary": segment_summary,
                    "segment_leaderboard": segment_summary,
                    "segment_playbooks": segment_playbooks,
                    "matrix": {"rows": matrix_rows, "selected_cell": {"r_score": rfm_settings["heat_r"], "f_score": rfm_settings["heat_f"]}},
                    "histograms": {
                        "recency": recency_hist,
                        "monetary": monetary_hist,
                    },
                    "segment_table": {
                        "mode": top_mode,
                        "top_n": top_n,
                        "rows": top_rows_payload,
                        "total_rows": int(len(top_sorted)),
                    },
                    "customers_table": {
                        "rows": full_rows_payload,
                        "total_rows": total_filtered,
                        "page": page_num,
                        "page_size": page_size_num,
                        "total_pages": total_pages_num,
                        "sort_by": sort_col_rfm,
                        "sort_dir": "asc" if asc else "desc",
                    },
                    "scatter_v2": {
                        "mode": rfm_settings["scatter_mode"],
                        "quadrants": {
                            "frequency_median": freq_median,
                            "monetary_median": mon_median,
                            "recency_median": rec_median,
                        },
                        "points": scatter_points,
                    },
                    "donut": donut_segments,
                    "exports": {
                        "datasets": [
                            "customers_full",
                            "top_customers",
                            "segments",
                            "segment_leaderboard",
                            "matrix_cells",
                            "heatmap_customers",
                        ],
                    },
                    # Backward compatibility keys for legacy RFM template.
                    "segments": [{"segment": row["segment"], "count": int(row["customers"])} for row in segment_summary if int(row["customers"]) > 0],
                    "top": [
                        {
                            "customer_id": row.get("customer_id"),
                            "customer_name": row.get("customer_name"),
                            "r": _safe_int(row.get("r_score"), 1),
                            "f": _safe_int(row.get("f_score"), 1),
                            "m": _safe_int(row.get("m_score"), 1),
                            "score": _safe_int(row.get("rfm_score"), 3),
                            "monetary": _clean_float(row.get("monetary")),
                            "frequency": _safe_int(row.get("frequency"), 0),
                            "segment": row.get("segment"),
                            "recency_days": _safe_int(row.get("recency_days"), 0),
                            "last_order_date": row.get("last_order_date"),
                        }
                        for row in [_rfm_row_payload(rec) for _, rec in legacy_top.iterrows()]
                    ],
                    "scatter": {
                        "frequency": [float(x) if pd.notna(x) else None for x in filtered_rfm_df["frequency"].tolist()],
                        "monetary": [float(x) if pd.notna(x) else None for x in filtered_rfm_df["monetary_value"].tolist()],
                        "labels": [
                            f"{row.customer_name or row.customer_id} (RFM={row.rfm_score})"
                            for row in filtered_rfm_df.itertuples()
                        ],
                        "scores": [int(s) for s in filtered_rfm_df["rfm_score"].tolist()],
                        "segments": [str(s) for s in filtered_rfm_df["segment"].tolist()],
                        "recency": [float(x) if pd.notna(x) else None for x in filtered_rfm_df["recency_days"].tolist()],
                    },
                    "updated_at": date.today().isoformat(),
                }
            else:
                rfm_payload = {
                    "settings": {
                        "lookback_months": int(rfm_settings["lookback_months"]),
                        "lookback_start": lookback_start_ts.date().isoformat(),
                        "lookback_end": lookback_end_ts.date().isoformat(),
                        "computed_window_note": (
                            f"Computed using last {int(rfm_settings['lookback_months'])} months ending on {lookback_end_ts.date().isoformat()}."
                        ),
                        "prior_start": prior_start_lb.date().isoformat(),
                        "prior_end": prior_end_lb.date().isoformat(),
                        "scoring_method": rfm_settings["scoring_method"],
                        "requested_monetary_metric": rfm_settings["requested_monetary_metric"],
                        "monetary_metric": rfm_settings["monetary_metric"],
                        "recency_thresholds": rfm_settings["recency_thresholds"],
                        "frequency_thresholds": rfm_settings["frequency_thresholds"],
                        "monetary_thresholds": rfm_settings["monetary_thresholds"],
                        "top_mode": rfm_settings["top_mode"],
                        "scatter_mode": rfm_settings["scatter_mode"],
                        "monetary_caveat": rfm_settings["monetary_caveat"],
                        "no_activity_policy": "excluded",
                        "params_hash": rfm_settings["params_hash"],
                    },
                    "filters": {},
                    "insights": {
                        "customers_lookback": 0,
                        "revenue_lookback": 0.0,
                        "repeat_rate_lookback": 0.0,
                        "at_risk_revenue_stake": 0.0,
                        "reactivation_rate": 0.0,
                        "at_risk_customers": 0,
                        "cant_lose_customers": 0,
                        "new_customers": 0,
                        "reactivated_customers": 0,
                        "top_segment_by_revenue": None,
                        "top_segment_by_growth": None,
                    },
                    "risk_opportunity": {
                        "at_risk_customers": 0,
                        "at_risk_revenue_stake": 0.0,
                        "cant_lose_customers": 0,
                        "new_customers": 0,
                        "reactivated_customers": 0,
                    },
                    "segment_summary": [],
                    "segment_leaderboard": [],
                    "segment_playbooks": [],
                    "matrix": {"rows": [], "selected_cell": {"r_score": None, "f_score": None}},
                    "histograms": {"recency": [], "monetary": []},
                    "segment_table": {"mode": rfm_settings["top_mode"], "top_n": int(rfm_settings["top_n"]), "rows": [], "total_rows": 0},
                    "customers_table": {"rows": [], "total_rows": 0, "page": 1, "page_size": int(rfm_settings["table_page_size"]), "total_pages": 0},
                    "scatter_v2": {
                        "mode": rfm_settings["scatter_mode"],
                        "quadrants": {"frequency_median": 0.0, "monetary_median": 0.0, "recency_median": 0.0},
                        "points": [],
                    },
                    "donut": [],
                    "exports": {
                        "datasets": [
                            "customers_full",
                            "top_customers",
                            "segments",
                            "segment_leaderboard",
                            "matrix_cells",
                            "heatmap_customers",
                        ]
                    },
                    "segments": [],
                    "top": [],
                    "scatter": {"frequency": [], "monetary": [], "labels": [], "scores": [], "segments": [], "recency": []},
                    "updated_at": date.today().isoformat(),
                }
        except Exception:
            rfm_payload = _empty_rfm_payload()
    else:
        rfm_payload = _empty_rfm_payload()

    if include_clv:
        clv_source = active_current_df.copy()
        if clv_source.empty:
            clv_source = merged.copy()
        try:
            clv_payload = _clv_payload(
                clv_source,
                args=args,
                window_start_ts=window_start_ts,
                window_end_ts=window_end_ts,
                cost_available=bool(cost_col),
                cost_coverage_pct=cost_coverage_pct,
            )
        except Exception:
            clv_payload = _empty_clv_payload(
                window_start_ts=window_start_ts,
                window_end_ts=window_end_ts,
                prior_start_ts=prior_start_ts,
                prior_end_ts=prior_end_ts,
                cost_coverage_pct=cost_coverage_pct,
            )
            (clv_payload.get("settings") or {})["monetary_caveat"] = "CLV payload generation failed; fallback to empty payload."
    else:
        clv_payload = _empty_clv_payload(
            window_start_ts=window_start_ts,
            window_end_ts=window_end_ts,
            prior_start_ts=prior_start_ts,
            prior_end_ts=prior_end_ts,
            cost_coverage_pct=cost_coverage_pct,
        )

    if include_cohorts:
        try:
            cohort_sql = f"""
            WITH base AS (
                SELECT {cust_id} AS customer_id, {date_col} AS order_date
                FROM fact
                WHERE {where_sql}
            ),
            firsts AS (
                SELECT customer_id, MIN(order_date) AS cohort_month FROM base GROUP BY 1
            ),
            activity AS (
                SELECT
                    DATE_TRUNC('month', f.cohort_month) AS cohort_month,
                    DATE_TRUNC('month', b.order_date) AS activity_month,
                    COALESCE(COUNT(DISTINCT b.customer_id), 0) AS customers_active
                FROM base b
                JOIN firsts f ON b.customer_id = f.customer_id
                GROUP BY 1,2
            )
            SELECT 'matrix' AS kind, cohort_month, activity_month, COALESCE(customers_active,0) AS customers_active, NULL::INTEGER AS cohort_size
            FROM activity
            UNION ALL
            SELECT 'size' AS kind, DATE_TRUNC('month', cohort_month), NULL, NULL, COALESCE(COUNT(*),0)::INTEGER
            FROM firsts
            GROUP BY 1,2
            ORDER BY 2,3
            """
            cohorts_df = fact_store.execute_sql_df(cohort_sql, where_params, tag="customers.bundle.cohorts", cache_key=None)
            cohorts_payload = _cohort_payload(cohorts_df)
        except Exception:
            cohorts_payload = _empty_cohorts_payload()
    else:
        cohorts_payload = _empty_cohorts_payload()

    try:
        trend_sql = f"""
            SELECT strftime('%Y-%m', {date_col}) AS month, SUM({revenue_col}) AS revenue, SUM({qty_col}) AS qty
            FROM fact
            WHERE {where_sql}
            GROUP BY 1
            ORDER BY 1
        """
        trend_df = fact_store.execute_sql_df(trend_sql, where_params, tag="customers.bundle.trend", cache_key=None)
        trend = {
            "labels": trend_df["month"].tolist() if not trend_df.empty else [],
            "revenue": [_clean_float(x) for x in trend_df.get("revenue", [])],
            "qty": [_clean_float(x) for x in trend_df.get("qty", [])],
        }
    except Exception:
        trend = {"labels": [], "revenue": [], "qty": []}

    duration_ms = int((time.perf_counter() - started) * 1000)

    payload: Dict[str, Any] = {
        "kpis": kpis,
        "executive_scorecard": scorecard,
        "executive_narrative": executive_narrative,
        "churn_risk_summary": churn_risk_summary,
        "health_strip": health_strip,
        "trend": trend,
        "charts": {
            "trend": trend,
            "churn_risk_distribution": risk_rows,
            "revenue_by_segment": segment_rows,
            "lifecycle_funnel": lifecycle_funnel_rows,
            "revenue_composition": revenue_composition_rows,
        },
        "drivers": {
            "movers": movers_rows,
            "top_gainers": top_gainers,
            "top_decliners": top_decliners,
            "decomposition": decomposition,
            "segment_mix_change": segment_mix_change,
            "recommended_actions": recommended_actions,
            "top_at_risk_customers": top_at_risk_customers,
        },
        "table": table,
        "rfm": rfm_payload,
        "cohorts": cohorts_payload,
        "clv": clv_payload,
        "definitions": _definition_payload(),
        "meta": {
            "page": table.get("page"),
            "page_size": table.get("page_size"),
            "total_rows": total_rows,
            "total_pages": total_pages,
            "sections": requested_section_list,
            "query_ms": duration_ms,
            "page_id": "customers",
            "generated_at": _utc_now_iso(),
            "window_start": window_start_ts.date().isoformat(),
            "window_end": window_end_ts.date().isoformat(),
            "prior_window_start": prior_start_ts.date().isoformat(),
            "prior_window_end": prior_end_ts.date().isoformat(),
            "rfm_params_hash": ((rfm_payload.get("settings") or {}).get("params_hash") if isinstance(rfm_payload, dict) else None),
            "clv_params_hash": ((clv_payload.get("settings") or {}).get("params_hash") if isinstance(clv_payload, dict) else None),
        },
    }
    return payload


def build_customers_drilldown(filters: Any, scope: Dict[str, Any], args: Any) -> Dict[str, Any]:
    started = time.perf_counter()
    # -- Column resolution --
    drilldown_v2 = _is_truthy(args.get("drilldown_v2"))
    drilldown_export_all = _is_truthy(args.get("export_all")) or _is_truthy(args.get("drilldown_export_all"))
    margin_target_pct = None

    cols = fact_store.list_columns()
    order_date_col = _safe_col(cols, "DateExpected", fs.CANON.date, "Date")
    date_col = order_date_col
    revenue_col = _safe_col(cols, fs.CANON.revenue, "Revenue")
    cost_col = _safe_col(cols, fs.CANON.cost, "Cost", "CostPrice")
    profit_col = _safe_col(cols, "Profit", "profit")
    qty_col = _safe_col(cols, fs.CANON.qty_units, "QuantityOrdered", "Quantity")
    weight_col = _safe_col(cols, fs.CANON.weight_lb, "ShippedLb", "Weight")
    items_col = _safe_col(cols, "ItemCount", "Items", "QuantityOrdered", "Units", "qty_units", "pack_item_count_sum")
    order_col = _safe_col(cols, fs.CANON.order_id, "OrderID")
    product_col = _safe_col(cols, fs.CANON.product_name, "ProductName", "SKU", "SkuName")
    product_id_col = _safe_col(cols, fs.CANON.product_id, "SKU")
    owner_rep_col = _safe_col(cols, "PrimarySalesRepName", "Owner", "AccountOwner", "AccountManager")
    protein_col = _safe_col(cols, "Protein", "ProteinType", "ProteinName", "Category", "ProductCategory")
    category_col = _safe_col(cols, "Category", "ProductCategory", "Protein", "ProteinType", "ProteinName")
    customer_id = str(args.get("customer_id") or args.get("id") or "")
    if not (customer_id and date_col and revenue_col and cost_col and qty_col and order_col and product_col):
        return {"error": {"message": "Required columns missing for customer drilldown"}, "meta": {"cached": False}}

    # -- Filters: filtered view + lifetime view (RBAC + statuses) --
    filt = fact_store.normalize_filters(filters)
    filt_all = replace(filt, start=None, end=None, preset=None, complete_months_only=False) if is_dataclass(filt) else filt

    where_sql_filt_scope, where_params_filt_scope, start_iso, end_iso = fact_store.build_where_clause(
        filt, cols, scope, apply_default_window=True
    )
    where_sql_filt = f"({where_sql_filt_scope}) AND {fs.CANON.customer_id} = ?"
    params_filt = list(where_params_filt_scope) + [customer_id]

    scope_where_sql, scope_where_params, _, _ = fact_store.build_where_clause(
        filt_all, cols, scope, apply_default_window=False
    )
    where_sql_all = f"({scope_where_sql}) AND {fs.CANON.customer_id} = ?"
    params_all = list(scope_where_params) + [customer_id]

    window_start_ts, window_end_ts = _coerce_window_bounds(start_iso, end_iso, date.today())
    window_days = max(int((window_end_ts - window_start_ts).days) + 1, 1)
    prior_end_ts = window_start_ts - pd.Timedelta(days=1)
    prior_start_ts = prior_end_ts - pd.Timedelta(days=window_days - 1)
    window_start_iso = window_start_ts.date().isoformat()
    window_end_iso = window_end_ts.date().isoformat()
    prior_start_iso = prior_start_ts.date().isoformat()
    prior_end_iso = prior_end_ts.date().isoformat()
    activity_reference_ts = min(window_end_ts.normalize(), _utc_today_ts_naive())
    activity_reference_iso = activity_reference_ts.date().isoformat()
    recent_30_start_iso = (activity_reference_ts - pd.Timedelta(days=29)).date().isoformat()
    recent_90_start_iso = (activity_reference_ts - pd.Timedelta(days=89)).date().isoformat()
    cross_sell_lookback_iso = (activity_reference_ts - pd.DateOffset(months=24)).date().isoformat()

    text_null = "CAST(NULL AS VARCHAR)"
    region_expr = _coalesce_text_expr(cols, fs.CANON.region, "RegionName", "Region", default=text_null)
    ship_method_expr = _coalesce_text_expr(
        cols,
        fs.CANON.ship_method,
        "ShippingMethodName",
        "ShipMethodName",
        "ShippingMethod",
        default=text_null,
    )
    sales_rep_expr = _coalesce_text_expr(
        cols,
        fs.CANON.sales_rep,
        "SalesRepName",
        "SalesRep",
        "PrimarySalesRepName",
        default=text_null,
    )
    owner_rep_expr = _coalesce_text_expr(
        cols,
        "PrimarySalesRepName",
        "Owner",
        "AccountOwner",
        "AccountManager",
        default=text_null,
    )
    city_expr = _coalesce_text_expr(cols, "City", "CustomerCity", default=text_null)
    state_expr = _coalesce_text_expr(cols, "Province", "State", "StateProvinceID", default=text_null)
    protein_expr = _coalesce_text_expr(cols, "Protein", "ProteinType", "ProteinName", "Category", "ProductCategory")
    category_expr = _coalesce_text_expr(cols, "Category", "ProductCategory", "Protein", "ProteinType", "ProteinName")
    customer_name_expr = _coalesce_text_expr(
        cols,
        fs.CANON.customer_name,
        "CustomerName",
        "Name",
        default=text_null,
    )
    weight_expr = weight_col or "NULL"
    items_expr = items_col or qty_col or "NULL"
    qty_expr = qty_col or "NULL"

    # ---------- Query 1: headline + cadence + profile (lifetime scope) ----------
    stats_sql = f"""
        WITH base_all AS (
            SELECT
                CAST({date_col} AS DATE) AS order_date,
                {order_col} AS order_id,
                {revenue_col} AS revenue,
                {cost_col} AS cost_raw,
                COALESCE({cost_col}, 0) AS cost,
                COALESCE({qty_col}, 0) AS qty,
                COALESCE({weight_expr}, 0) AS weight_lb,
                COALESCE({items_expr}, 0) AS items,
                {product_id_col or product_col} AS product_id,
                {product_col} AS product_name,
                {category_expr} AS category_name,
                {region_expr} AS region,
                {ship_method_expr} AS ship_method,
                {sales_rep_expr} AS seller_sales_rep,
                {owner_rep_expr} AS owner_sales_rep,
                {city_expr} AS city,
                {state_expr} AS state,
                {customer_name_expr} AS customer_name
            FROM fact
            WHERE {where_sql_all}
        ),
        orders AS (
            SELECT
                order_id,
                MIN(order_date) AS order_date,
                SUM(revenue) AS revenue,
                SUM(cost) AS cost,
                SUM(revenue) - SUM(cost) AS profit
            FROM base_all
            GROUP BY 1
        ),
        profile AS (
            SELECT
                MIN(order_date) AS first_order,
                MAX(order_date) AS last_order,
                COUNT(DISTINCT order_id) AS orders,
                COUNT(DISTINCT DATE_TRUNC('month', order_date)) AS months_active,
                COUNT(DISTINCT DATE_TRUNC('week', order_date)) AS weeks_active,
                DATE_DIFF('day', MIN(order_date), MAX(order_date)) AS span_days,
                COUNT(DISTINCT product_id) AS unique_products,
                COUNT(DISTINCT category_name) FILTER (WHERE category_name IS NOT NULL) AS unique_categories,
                SUM(weight_lb) AS total_weight_lb,
                SUM(items) AS total_items,
                (SELECT region FROM base_all GROUP BY region ORDER BY COUNT(*) DESC NULLS LAST LIMIT 1) AS primary_region,
                (SELECT ship_method FROM base_all GROUP BY ship_method ORDER BY COUNT(*) DESC NULLS LAST LIMIT 1) AS primary_ship,
                (
                    SELECT owner_sales_rep
                    FROM base_all
                    WHERE owner_sales_rep IS NOT NULL
                    GROUP BY owner_sales_rep
                    ORDER BY COUNT(*) DESC NULLS LAST
                    LIMIT 1
                ) AS assigned_owner_sales_rep,
                (
                    SELECT seller_sales_rep
                    FROM base_all
                    WHERE seller_sales_rep IS NOT NULL
                    GROUP BY seller_sales_rep
                    ORDER BY COUNT(*) DESC NULLS LAST
                    LIMIT 1
                ) AS dominant_seller_sales_rep,
                (SELECT city FROM base_all GROUP BY city ORDER BY COUNT(*) DESC NULLS LAST LIMIT 1) AS primary_city,
                (SELECT state FROM base_all GROUP BY state ORDER BY COUNT(*) DESC NULLS LAST LIMIT 1) AS primary_state,
                (SELECT ANY_VALUE(customer_name) FROM base_all WHERE customer_name IS NOT NULL LIMIT 1) AS customer_name
            FROM base_all
        ),
        latest_touch AS (
            SELECT
                seller_sales_rep AS last_sales_rep,
                order_date AS last_sales_rep_order_date
            FROM base_all
            WHERE order_date IS NOT NULL
              AND seller_sales_rep IS NOT NULL
            ORDER BY order_date DESC NULLS LAST, order_id DESC NULLS LAST
            LIMIT 1
        ),
        cadence AS (
            SELECT
                AVG(diff) AS avg_days,
                QUANTILE_CONT(diff, 0.5) AS median_days,
                MIN(diff) AS min_days,
                MAX(diff) AS max_days
            FROM (
                SELECT DATE_DIFF('day', LAG(order_date) OVER (ORDER BY order_date), order_date) AS diff
                FROM orders
            ) d
            WHERE diff IS NOT NULL AND diff >= 0
        ),
        recent AS (
            SELECT
                SUM(CASE WHEN order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) THEN revenue END) AS revenue_last_30,
                SUM(CASE WHEN order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) THEN revenue END) AS revenue_last_90,
                SUM(CASE WHEN order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) THEN profit END) AS profit_last_30,
                SUM(CASE WHEN order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) THEN profit END) AS profit_last_90,
                COUNT(CASE WHEN order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) THEN 1 END) AS orders_last_30,
                COUNT(CASE WHEN order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) THEN 1 END) AS orders_last_90
            FROM orders
        ),
        best_weekday AS (
            SELECT
                STRFTIME(order_date, '%A') AS weekday_label,
                SUM(revenue) AS revenue
            FROM base_all
            GROUP BY 1
            ORDER BY revenue DESC NULLS LAST
            LIMIT 1
        ),
        cost_diag AS (
            SELECT
                COUNT(*) AS total_rows,
                SUM(CASE WHEN cost_raw IS NULL THEN 1 ELSE 0 END) AS cost_missing_rows
            FROM base_all
        ),
        kpi AS (
            SELECT
                SUM(revenue) AS revenue,
                SUM(cost) AS cost,
                SUM(profit) AS profit,
                CASE WHEN SUM(revenue)=0 THEN NULL ELSE SUM(profit)/SUM(revenue)*100 END AS margin_pct,
                COUNT(*) AS orders
            FROM orders
        )
        SELECT
            kpi.revenue, kpi.cost, kpi.profit, kpi.margin_pct, kpi.orders,
            prof.first_order, prof.last_order, prof.months_active, prof.weeks_active, prof.span_days, prof.unique_products,
            prof.unique_categories, prof.total_weight_lb, prof.total_items,
            prof.primary_region, prof.primary_ship,
            COALESCE(prof.assigned_owner_sales_rep, prof.dominant_seller_sales_rep) AS owner_sales_rep,
            prof.assigned_owner_sales_rep,
            prof.dominant_seller_sales_rep,
            latest_touch.last_sales_rep,
            latest_touch.last_sales_rep_order_date,
            prof.primary_city, prof.primary_state,
            recent.revenue_last_30, recent.revenue_last_90, recent.profit_last_30, recent.profit_last_90,
            recent.orders_last_30, recent.orders_last_90,
            cadence.avg_days, cadence.median_days, cadence.min_days, cadence.max_days,
            cost_diag.total_rows, cost_diag.cost_missing_rows,
            best_weekday.weekday_label AS best_weekday,
            best_weekday.revenue AS best_weekday_revenue,
            prof.customer_name
        FROM kpi
        CROSS JOIN profile prof
        LEFT JOIN latest_touch ON 1=1
        LEFT JOIN recent ON 1=1
        LEFT JOIN cadence ON 1=1
        LEFT JOIN cost_diag ON 1=1
        LEFT JOIN best_weekday ON 1=1
    """
    stats_params: List[Any] = list(params_all) + [
        recent_30_start_iso,
        activity_reference_iso,
        recent_90_start_iso,
        activity_reference_iso,
        recent_30_start_iso,
        activity_reference_iso,
        recent_90_start_iso,
        activity_reference_iso,
        recent_30_start_iso,
        activity_reference_iso,
        recent_90_start_iso,
        activity_reference_iso,
    ]
    stats_df = fact_store.execute_sql_df(stats_sql, stats_params, tag="customers.drilldown.stats")

    # ---------- Query 1b: action center (lifetime scope) ----------
    action_sql = f"""
        WITH base_all AS (
            SELECT
                {order_col} AS order_id,
                CAST({order_date_col} AS DATE) AS order_date
            FROM fact
            WHERE {where_sql_all}
              AND {order_col} IS NOT NULL
              AND {order_date_col} IS NOT NULL
        ),
        orders_all AS (
            SELECT order_id, MIN(order_date) AS order_date
            FROM base_all
            GROUP BY 1
        )
        SELECT
            COUNT(*) FILTER (WHERE order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)) AS orders_last_30d,
            COUNT(*) FILTER (WHERE order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)) AS orders_last_90d,
            MAX(order_date) AS last_order_date
        FROM orders_all
    """
    action_params: List[Any] = list(params_all) + [
        recent_30_start_iso,
        activity_reference_iso,
        recent_90_start_iso,
        activity_reference_iso,
    ]
    action_df = fact_store.execute_sql_df(action_sql, action_params, tag="customers.drilldown.action")

    # ---------- Query 2: orders (filtered + lifetime) ----------
    orders_sql = f"""
        WITH base_filt AS (
            SELECT {order_col} AS order_id, CAST({date_col} AS DATE) AS order_date,
                   {revenue_col} AS revenue, COALESCE({cost_col},0) AS cost,
                   COALESCE({profit_col}, {revenue_col} - COALESCE({cost_col},0)) AS profit,
                   COALESCE({weight_expr},0) AS weight_lb, COALESCE({items_expr},0) AS items
            FROM fact
            WHERE {where_sql_filt}
              AND {order_col} IS NOT NULL
              AND {date_col} IS NOT NULL
        ),
        base_all AS (
            SELECT {order_col} AS order_id, CAST({date_col} AS DATE) AS order_date,
                   {revenue_col} AS revenue, COALESCE({cost_col},0) AS cost,
                   COALESCE({profit_col}, {revenue_col} - COALESCE({cost_col},0)) AS profit,
                   COALESCE({weight_expr},0) AS weight_lb, COALESCE({items_expr},0) AS items
            FROM fact
            WHERE {where_sql_all}
              AND {order_col} IS NOT NULL
              AND {date_col} IS NOT NULL
        ),
        orders_union AS (
            SELECT 'filtered' AS scope, order_id, MIN(order_date) AS order_date,
                   SUM(revenue) AS revenue, SUM(cost) AS cost, SUM(profit) AS profit,
                   SUM(weight_lb) AS weight_lb, SUM(items) AS items, COUNT(*) AS lines
            FROM base_filt GROUP BY 1,2
            UNION ALL
            SELECT 'all' AS scope, order_id, MIN(order_date) AS order_date,
                   SUM(revenue) AS revenue, SUM(cost) AS cost, SUM(profit) AS profit,
                   SUM(weight_lb) AS weight_lb, SUM(items) AS items, COUNT(*) AS lines
            FROM base_all GROUP BY 1,2
        )
        SELECT * FROM orders_union
    """
    orders_df = fact_store.execute_sql_df(orders_sql, params_filt + params_all, tag="customers.drilldown.orders")

    # ---------- Query 3: monthly series (filtered + lifetime for seasonality) ----------
    monthly_sql = f"""
        WITH base_filt AS (
            SELECT
                CAST({date_col} AS DATE) AS order_date,
                {order_col} AS order_id,
                {revenue_col} AS revenue,
                COALESCE({cost_col},0) AS cost,
                COALESCE({weight_expr},0) AS weight_lb
            FROM fact
            WHERE {where_sql_filt}
        ),
        base_all AS (
            SELECT
                CAST({date_col} AS DATE) AS order_date,
                {order_col} AS order_id,
                {revenue_col} AS revenue,
                COALESCE({cost_col},0) AS cost,
                COALESCE({weight_expr},0) AS weight_lb
            FROM fact
            WHERE {where_sql_all}
        ),
        monthly AS (
            SELECT
                   'filtered' AS scope,
                   CAST(DATE_TRUNC('month', order_date) AS DATE) AS month,
                   SUM(revenue) AS revenue, COUNT(DISTINCT order_id) AS orders,
                   SUM(cost) AS cost, SUM(weight_lb) AS weight_lb, SUM(revenue) - SUM(cost) AS profit,
                   CASE WHEN SUM(revenue)=0 THEN NULL ELSE (SUM(revenue)-SUM(cost))/SUM(revenue)*100 END AS margin_pct
            FROM base_filt
            WHERE order_date IS NOT NULL
            GROUP BY 1,2
            UNION ALL
            SELECT
                   'all' AS scope,
                   CAST(DATE_TRUNC('month', order_date) AS DATE) AS month,
                   SUM(revenue) AS revenue, COUNT(DISTINCT order_id) AS orders,
                   SUM(cost) AS cost, SUM(weight_lb) AS weight_lb, SUM(revenue) - SUM(cost) AS profit,
                   CASE WHEN SUM(revenue)=0 THEN NULL ELSE (SUM(revenue)-SUM(cost))/SUM(revenue)*100 END AS margin_pct
            FROM base_all
            WHERE order_date IS NOT NULL
            GROUP BY 1,2
        )
        SELECT * FROM monthly ORDER BY scope, month
    """
    monthly_df = fact_store.execute_sql_df(monthly_sql, params_filt + params_all, tag="customers.drilldown.monthly")

    # ---------- Query 3a: scoped revenue / weight context ----------
    scope_totals_sql = f"""
        WITH scope_base AS (
            SELECT
                {fs.CANON.customer_id} AS customer_id,
                {order_col} AS order_id,
                CAST({date_col} AS DATE) AS order_date,
                COALESCE({revenue_col}, 0) AS revenue,
                COALESCE({profit_col}, {revenue_col} - COALESCE({cost_col}, 0)) AS profit,
                COALESCE({weight_expr}, 0) AS weight_lb
            FROM fact
            WHERE {scope_where_sql}
              AND {date_col} IS NOT NULL
              AND {order_col} IS NOT NULL
        )
        SELECT
            COUNT(DISTINCT customer_id) AS customers_in_scope,
            SUM(revenue) AS revenue_lifetime_scope,
            SUM(profit) AS profit_lifetime_scope,
            SUM(weight_lb) AS weight_lifetime_scope,
            COUNT(DISTINCT CASE WHEN order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) THEN customer_id END) AS customers_in_window,
            SUM(CASE WHEN order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) THEN revenue ELSE 0 END) AS revenue_window_scope,
            SUM(CASE WHEN order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) THEN profit ELSE 0 END) AS profit_window_scope,
            SUM(CASE WHEN order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) THEN weight_lb ELSE 0 END) AS weight_window_scope,
            COUNT(DISTINCT CASE WHEN order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) THEN order_id END) AS orders_window_scope
        FROM scope_base
    """
    scope_totals_params: List[Any] = list(scope_where_params) + [
        window_start_iso,
        window_end_iso,
        window_start_iso,
        window_end_iso,
        window_start_iso,
        window_end_iso,
        window_start_iso,
        window_end_iso,
        window_start_iso,
        window_end_iso,
    ]
    scope_totals_df = fact_store.execute_sql_df(
        scope_totals_sql,
        scope_totals_params,
        tag="customers.drilldown.scope_totals",
    )

    # ---------- Query 3c: category / family mix ----------
    category_limit_sql = ""
    if not drilldown_export_all:
        category_limit_sql = "LIMIT 20"

    category_sql = f"""
        WITH base AS (
            SELECT
                COALESCE(NULLIF(CAST({category_expr} AS VARCHAR), ''), 'Unassigned') AS category,
                {product_col} AS product,
                {order_col} AS order_id,
                CAST({date_col} AS DATE) AS order_date,
                COALESCE({revenue_col}, 0) AS revenue,
                COALESCE({cost_col}, 0) AS cost,
                COALESCE({weight_expr}, 0) AS weight_lb
            FROM fact
            WHERE {where_sql_all}
              AND {order_col} IS NOT NULL
              AND {date_col} IS NOT NULL
        ),
        labeled AS (
            SELECT
                *,
                CASE
                    WHEN order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) THEN 'current'
                    WHEN order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) THEN 'prior'
                    ELSE NULL
                END AS period
            FROM base
        )
        SELECT
            category,
            SUM(CASE WHEN period = 'current' THEN revenue ELSE 0 END) AS revenue,
            SUM(CASE WHEN period = 'current' THEN cost ELSE 0 END) AS cost,
            SUM(CASE WHEN period = 'current' THEN revenue - cost ELSE 0 END) AS profit,
            SUM(CASE WHEN period = 'current' THEN weight_lb ELSE 0 END) AS weight_lb,
            COUNT(DISTINCT CASE WHEN period = 'current' THEN order_id END) AS orders,
            COUNT(DISTINCT CASE WHEN period = 'current' THEN product END) AS products,
            SUM(CASE WHEN period = 'prior' THEN revenue ELSE 0 END) AS revenue_prior,
            SUM(CASE WHEN period = 'prior' THEN weight_lb ELSE 0 END) AS weight_prior
        FROM labeled
        WHERE period IS NOT NULL
        GROUP BY category
        HAVING
            SUM(CASE WHEN period = 'current' THEN revenue ELSE 0 END) <> 0
            OR SUM(CASE WHEN period = 'prior' THEN revenue ELSE 0 END) <> 0
        ORDER BY revenue DESC NULLS LAST
        {category_limit_sql}
    """
    category_params: List[Any] = list(params_all) + [window_start_iso, window_end_iso, prior_start_iso, prior_end_iso]
    category_df = fact_store.execute_sql_df(category_sql, category_params, tag="customers.drilldown.categories")

    protein_limit_sql = category_limit_sql
    protein_sql = f"""
        WITH base AS (
            SELECT
                COALESCE(NULLIF(CAST({protein_expr} AS VARCHAR), ''), 'Unassigned') AS protein_family,
                {product_col} AS product,
                {order_col} AS order_id,
                CAST({date_col} AS DATE) AS order_date,
                COALESCE({revenue_col}, 0) AS revenue,
                COALESCE({cost_col}, 0) AS cost,
                COALESCE({weight_expr}, 0) AS weight_lb
            FROM fact
            WHERE {where_sql_all}
              AND {order_col} IS NOT NULL
              AND {date_col} IS NOT NULL
        ),
        labeled AS (
            SELECT
                *,
                CASE
                    WHEN order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) THEN 'current'
                    WHEN order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) THEN 'prior'
                    ELSE NULL
                END AS period
            FROM base
        )
        SELECT
            protein_family,
            SUM(CASE WHEN period = 'current' THEN revenue ELSE 0 END) AS revenue,
            SUM(CASE WHEN period = 'current' THEN cost ELSE 0 END) AS cost,
            SUM(CASE WHEN period = 'current' THEN revenue - cost ELSE 0 END) AS profit,
            SUM(CASE WHEN period = 'current' THEN weight_lb ELSE 0 END) AS weight_lb,
            COUNT(DISTINCT CASE WHEN period = 'current' THEN order_id END) AS orders,
            COUNT(DISTINCT CASE WHEN period = 'current' THEN product END) AS products,
            SUM(CASE WHEN period = 'prior' THEN revenue ELSE 0 END) AS revenue_prior,
            SUM(CASE WHEN period = 'prior' THEN weight_lb ELSE 0 END) AS weight_prior
        FROM labeled
        WHERE period IS NOT NULL
        GROUP BY protein_family
        HAVING
            SUM(CASE WHEN period = 'current' THEN revenue ELSE 0 END) <> 0
            OR SUM(CASE WHEN period = 'prior' THEN revenue ELSE 0 END) <> 0
        ORDER BY revenue DESC NULLS LAST
        {protein_limit_sql}
    """
    protein_params: List[Any] = list(params_all) + [window_start_iso, window_end_iso, prior_start_iso, prior_end_iso]
    protein_df = fact_store.execute_sql_df(protein_sql, protein_params, tag="customers.drilldown.protein")

    # ---------- Query 3b: opportunity highlights (lifetime) ----------
    opp_sql = f"""
        WITH base_all AS (
            SELECT
                {product_id_col or product_col} AS sku,
                {product_col} AS description,
                COALESCE({revenue_col}, 0) AS revenue,
                COALESCE({profit_col}, {revenue_col} - COALESCE({cost_col}, 0)) AS profit,
                COALESCE({cost_col}, 0) AS cost,
                COALESCE({weight_expr}, 0) AS weight_lb,
                COALESCE({items_expr}, 0) AS items,
                COALESCE({qty_expr}, 0) AS qty
            FROM fact
            WHERE {where_sql_all}
              AND {order_col} IS NOT NULL
              AND {order_date_col} IS NOT NULL
        ),
        revenue_top AS (
            SELECT sku, ANY_VALUE(description) AS description, SUM(revenue) AS revenue
            FROM base_all
            GROUP BY 1
            ORDER BY revenue DESC NULLS LAST
            LIMIT 1
        ),
        profit_top AS (
            SELECT
                sku,
                ANY_VALUE(description) AS description,
                SUM(revenue) AS revenue,
                SUM(cost) AS cost,
                SUM(profit) AS profit
            FROM base_all
            GROUP BY 1
            ORDER BY profit DESC NULLS LAST
            LIMIT 1
        ),
        weight_base AS (
            SELECT
                sku, ANY_VALUE(description) AS description,
                SUM(weight_lb) AS w_lb,
                SUM(items) AS w_items,
                SUM(qty) AS w_qty
            FROM base_all
            GROUP BY 1
        ),
        weight_pick AS (
            SELECT
                sku,
                description,
                CASE
                    WHEN w_lb > 0 THEN w_lb
                    WHEN w_items > 0 THEN w_items
                    WHEN w_qty > 0 THEN w_qty
                    ELSE 0
                END AS weight_value,
                CASE
                    WHEN w_lb > 0 THEN 'lb'
                    WHEN w_items > 0 THEN 'items'
                    WHEN w_qty > 0 THEN 'qty'
                    ELSE 'none'
                END AS weight_unit
            FROM weight_base
        ),
        pricing AS (
            SELECT
                sku,
                CASE
                    WHEN SUM(weight_lb) > 0 THEN SUM(revenue) / SUM(weight_lb)
                    WHEN SUM(w_items) > 0 THEN SUM(revenue) / SUM(w_items)
                    WHEN SUM(w_qty) > 0 THEN SUM(revenue) / SUM(w_qty)
                    ELSE NULL
                END AS unit_price
            FROM (
                SELECT sku, revenue, weight_lb, items AS w_items, qty AS w_qty
                FROM base_all
            )
            GROUP BY 1
        ),
        spread AS (
            SELECT MIN(unit_price) AS min_price, MAX(unit_price) AS max_price
            FROM pricing
            WHERE unit_price IS NOT NULL
        )
        SELECT
            (SELECT sku FROM revenue_top LIMIT 1) AS revenue_sku,
            (SELECT description FROM revenue_top LIMIT 1) AS revenue_desc,
            (SELECT revenue FROM revenue_top LIMIT 1) AS revenue_value,
            (SELECT sku FROM profit_top LIMIT 1) AS profit_sku,
            (SELECT description FROM profit_top LIMIT 1) AS profit_desc,
            (SELECT profit FROM profit_top LIMIT 1) AS profit_value,
            (SELECT revenue FROM profit_top LIMIT 1) AS profit_revenue,
            (SELECT cost FROM profit_top LIMIT 1) AS profit_cost,
            (SELECT sku FROM weight_pick ORDER BY weight_value DESC NULLS LAST LIMIT 1) AS weight_sku,
            (SELECT description FROM weight_pick ORDER BY weight_value DESC NULLS LAST LIMIT 1) AS weight_desc,
            (SELECT weight_value FROM weight_pick ORDER BY weight_value DESC NULLS LAST LIMIT 1) AS weight_value,
            (SELECT weight_unit FROM weight_pick ORDER BY weight_value DESC NULLS LAST LIMIT 1) AS weight_unit,
            (SELECT min_price FROM spread LIMIT 1) AS min_price,
            (SELECT max_price FROM spread LIMIT 1) AS max_price
    """
    opp_df = fact_store.execute_sql_df(opp_sql, params_all, tag="customers.drilldown.opportunity")
    opp_row = opp_df.iloc[0] if not opp_df.empty else None
    def _opp_val(attr: str) -> Any:
        return getattr(opp_row, attr, None) if opp_row is not None else None

    revenue_val = _opp_val("revenue_value")
    profit_val = _opp_val("profit_value")
    profit_revenue = _opp_val("profit_revenue")
    top_product = {
        "sku": _opp_val("revenue_sku"),
        "description": _opp_val("revenue_desc"),
        "value": float(revenue_val) if pd.notna(revenue_val) else 0.0,
    }
    profit_margin_pct = None
    if profit_revenue and pd.notna(profit_revenue) and profit_revenue != 0:
        try:
            profit_margin_pct = float(profit_val or 0.0) / float(profit_revenue) * 100.0
        except Exception:
            profit_margin_pct = None
    top_profit_product = {
        "sku": _opp_val("profit_sku"),
        "description": _opp_val("profit_desc"),
        "value": float(profit_val) if pd.notna(profit_val) else 0.0,
        "margin_pct": profit_margin_pct,
    }
    unit_raw = _opp_val("weight_unit")
    unit_val = unit_raw if unit_raw and unit_raw != "none" else "lb"
    top_weight_mover = {
        "sku": _opp_val("weight_sku"),
        "description": _opp_val("weight_desc"),
        "value": float(_opp_val("weight_value") or 0.0) if pd.notna(_opp_val("weight_value")) else 0.0,
        "unit": unit_val,
    }
    min_price_raw = _opp_val("min_price")
    max_price_raw = _opp_val("max_price")
    pricing_spread = None
    if pd.notna(min_price_raw) or pd.notna(max_price_raw):
        pricing_spread = {
            "min": float(min_price_raw) if pd.notna(min_price_raw) else None,
            "max": float(max_price_raw) if pd.notna(max_price_raw) else None,
        }
    opportunity = {
        "top_product": top_product,
        "top_profit_product": top_profit_product,
        "top_weight_mover": top_weight_mover,
        "pricing_spread": pricing_spread,
    }

    # ---------- Query 4: product profitability (window + prior window) ----------
    product_limit_sql = ""
    if not drilldown_export_all:
        product_limit = 250 if drilldown_v2 else 25
        product_limit_sql = f"LIMIT {int(product_limit)}"

    product_sql = f"""
        WITH base AS (
            SELECT
                COALESCE(CAST({product_id_col or product_col} AS VARCHAR), CAST({product_col} AS VARCHAR)) AS sku,
                {product_col} AS product,
                COALESCE(NULLIF(CAST({protein_expr} AS VARCHAR), ''), 'Unassigned') AS protein_family,
                COALESCE(NULLIF(CAST({category_expr} AS VARCHAR), ''), 'Unassigned') AS category,
                {order_col} AS order_id,
                CAST({date_col} AS DATE) AS order_date,
                {revenue_col} AS revenue,
                {cost_col} AS cost_raw,
                COALESCE({cost_col},0) AS cost,
                COALESCE({qty_col},0) AS qty
                {(',' + weight_col + ' AS weight') if weight_col else ', 0 AS weight'}
            FROM fact
            WHERE {where_sql_all}
              AND {product_col} IS NOT NULL
              AND {date_col} IS NOT NULL
        ),
        labeled AS (
            SELECT
                *,
                CASE
                    WHEN order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) THEN 'current'
                    WHEN order_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) THEN 'prior'
                    ELSE NULL
                END AS period
            FROM base
        )
        SELECT
            sku,
            product,
            ANY_VALUE(protein_family) AS protein_family,
            ANY_VALUE(category) AS category,
            SUM(CASE WHEN period = 'current' THEN revenue ELSE 0 END) AS revenue,
            SUM(CASE WHEN period = 'current' THEN cost ELSE 0 END) AS cost,
            SUM(CASE WHEN period = 'current' THEN (revenue - cost) ELSE 0 END) AS profit,
            CASE
                WHEN SUM(CASE WHEN period = 'current' THEN revenue ELSE 0 END) = 0 THEN NULL
                ELSE (
                    SUM(CASE WHEN period = 'current' THEN (revenue - cost) ELSE 0 END)
                    / SUM(CASE WHEN period = 'current' THEN revenue ELSE 0 END)
                ) * 100
            END AS margin_pct,
            SUM(CASE WHEN period = 'current' AND cost_raw IS NULL THEN 1 ELSE 0 END) AS cost_null_rows,
            SUM(CASE WHEN period = 'current' THEN weight ELSE 0 END) AS weight_lb,
            SUM(CASE WHEN period = 'current' THEN qty ELSE 0 END) AS qty,
            COUNT(DISTINCT CASE WHEN period = 'current' THEN order_id END) AS orders,
            COUNT(CASE WHEN period = 'current' THEN 1 ELSE NULL END) AS line_count,
            SUM(CASE WHEN period = 'prior' THEN revenue ELSE 0 END) AS revenue_prior,
            SUM(CASE WHEN period = 'prior' THEN cost ELSE 0 END) AS cost_prior,
            SUM(CASE WHEN period = 'prior' THEN (revenue - cost) ELSE 0 END) AS profit_prior,
            CASE
                WHEN SUM(CASE WHEN period = 'prior' THEN revenue ELSE 0 END) = 0 THEN NULL
                ELSE (
                    SUM(CASE WHEN period = 'prior' THEN (revenue - cost) ELSE 0 END)
                    / SUM(CASE WHEN period = 'prior' THEN revenue ELSE 0 END)
                ) * 100
            END AS margin_prior_pct
        FROM labeled
        WHERE period IS NOT NULL
        GROUP BY sku, product
        HAVING
            SUM(CASE WHEN period = 'current' THEN revenue ELSE 0 END) <> 0
            OR SUM(CASE WHEN period = 'prior' THEN revenue ELSE 0 END) <> 0
        ORDER BY profit DESC
        {product_limit_sql}
    """
    product_params: List[Any] = list(params_all) + [window_start_iso, window_end_iso, prior_start_iso, prior_end_iso]
    product_df = fact_store.execute_sql_df(product_sql, product_params, tag="customers.drilldown.products")

    # ---------- Query 5: weekday revenue (lifetime) ----------
    weekday_sql = f"""
        SELECT
            STRFTIME(CAST({date_col} AS DATE), '%w') AS weekday_num,
            STRFTIME(CAST({date_col} AS DATE), '%a') AS weekday_label,
            SUM({revenue_col}) AS revenue,
            SUM(COALESCE({weight_expr}, 0)) AS weight_lb,
            COUNT(DISTINCT {order_col}) AS orders
        FROM fact
        WHERE {where_sql_all}
        GROUP BY 1,2
        ORDER BY CAST(weekday_num AS INTEGER)
    """
    weekday_df = fact_store.execute_sql_df(weekday_sql, params_all, tag="customers.drilldown.weekday")

    # ----------------------------- Build payload ----------------------------- #
    if stats_df.empty:
        return {
            "kpis": {},
            "trend": {"labels": [], "revenue": [], "orders": [], "profit": [], "cost": [], "margin_pct": []},
            "table": {"rows": [], "page": 1, "page_size": 25, "total": 0, "sort_by": "revenue", "sort_dir": "desc"},
            "meta": {"page_id": "customer_drilldown", "entity_id": customer_id, "cached": False},
        }

    s = stats_df.iloc[0]
    action_row = action_df.iloc[0] if not action_df.empty else None
    action_orders_30 = _safe_int(action_row.get("orders_last_30d")) if action_row is not None else _safe_int(s.get("orders_last_30"))
    action_orders_90 = _safe_int(action_row.get("orders_last_90d")) if action_row is not None else _safe_int(s.get("orders_last_90"))
    last_order_raw = None
    if action_row is not None:
        last_order_raw = action_row.get("last_order_date")
    if last_order_raw is None:
        last_order_raw = s.get("last_order")
    last_order_dt = pd.to_datetime(last_order_raw) if pd.notna(last_order_raw) else None
    days_since_last = None
    try:
        if last_order_dt is not None:
            days_since_last = max(int((activity_reference_ts - last_order_dt.normalize()).days), 0)
    except Exception:
        days_since_last = None

    revenue_val = _clean_float(s.get("revenue"))
    cost_val = _clean_float(s.get("cost"))
    profit_val = _clean_float(s.get("profit"))
    orders_val = _safe_int(s.get("orders"))
    kpis = {
        "total_revenue": revenue_val,
        "total_cost": cost_val,
        "total_profit": profit_val,
        "margin_pct": None if revenue_val == 0 else _clean_float(s.get("margin_pct")),
        "total_orders": orders_val,
        "aov": (revenue_val / orders_val) if orders_val else None,
        "first_order": s.get("first_order"),
        "last_order": s.get("last_order"),
        "months_active": _safe_int(s.get("months_active")),
        "weeks_active": _safe_int(s.get("weeks_active")),
        "days_span": _safe_int(s.get("span_days")),
        "unique_products": _safe_int(s.get("unique_products")),
        "unique_categories": _safe_int(s.get("unique_categories")),
        "total_weight_lb": _clean_float(s.get("total_weight_lb")),
        "total_items": _clean_float(s.get("total_items")),
        "primary_region": s.get("primary_region"),
        "primary_shipping": s.get("primary_ship"),
        "owner_sales_rep": s.get("owner_sales_rep"),
        "assigned_owner_sales_rep": s.get("assigned_owner_sales_rep"),
        "dominant_seller_sales_rep": s.get("dominant_seller_sales_rep"),
        "historical_owner_sales_rep": None,
        "last_sales_rep": s.get("last_sales_rep") or s.get("owner_sales_rep"),
        "last_sales_rep_date": _iso_date_text(s.get("last_sales_rep_order_date") or s.get("last_order")),
        "primary_city": s.get("primary_city"),
        "primary_state": s.get("primary_state"),
        "revenue_last_30": _clean_float(s.get("revenue_last_30")),
        "revenue_last_90": _clean_float(s.get("revenue_last_90")),
        "profit_last_90": _clean_float(s.get("profit_last_90")),
        "orders_last_90": action_orders_90,
        "orders_last_30": action_orders_30,
        "avg_ticket_last_90": (_clean_float(s.get("revenue_last_90")) / action_orders_90) if action_orders_90 else None,
        "cadence_avg_days": float(s.get("avg_days") or 0.0) if pd.notna(s.get("avg_days")) else None,
        "cadence_median_days": float(s.get("median_days") or 0.0) if pd.notna(s.get("median_days")) else None,
        "cadence_min_days": float(s.get("min_days") or 0.0) if pd.notna(s.get("min_days")) else None,
        "cadence_max_days": float(s.get("max_days") or 0.0) if pd.notna(s.get("max_days")) else None,
        "cost_missing_rows": _safe_int(s.get("cost_missing_rows")),
        "cost_coverage_pct": (
            0.0
            if _safe_int(s.get("total_rows")) == 0
            else (1 - (_safe_int(s.get("cost_missing_rows")) / max(_safe_int(s.get("total_rows")), 1))) * 100.0
        ),
        "churn_risk": "High" if float(s.get("revenue_last_90") or 0) == 0 else "Medium",
        "days_since_last_order": days_since_last,
        "best_weekday": s.get("best_weekday"),
        "best_weekday_revenue": float(s.get("best_weekday_revenue") or 0.0) if pd.notna(s.get("best_weekday_revenue")) else None,
        "label": s.get("customer_name"),
    }
    for key, value in list(kpis.items()):
        kpis[key] = _none_if_na(value)

    # Opportunity highlights (lifetime scope; not filter-constrained)
    top_prod_label = top_product.get("description") or top_product.get("sku") if top_product else None
    if top_prod_label:
        kpis["top_product_label"] = top_prod_label
    kpis["top_product_revenue"] = float(top_product.get("value") or 0.0) if top_product else 0.0

    weight_label = top_weight_mover.get("description") or top_weight_mover.get("sku") if top_weight_mover else None
    if weight_label:
        kpis["top_weight_product"] = weight_label
    kpis["top_weight_lb"] = float(top_weight_mover.get("value") or 0.0) if top_weight_mover else 0.0
    if top_weight_mover and top_weight_mover.get("unit"):
        kpis["top_weight_unit"] = top_weight_mover.get("unit")

    profit_label = top_profit_product.get("description") or top_profit_product.get("sku") if top_profit_product else None
    if profit_label:
        kpis["top_profit_product"] = profit_label
    kpis["top_profit_value"] = float(top_profit_product.get("value") or 0.0) if top_profit_product else 0.0
    if top_profit_product and top_profit_product.get("margin_pct") is not None:
        kpis["top_profit_margin"] = float(top_profit_product.get("margin_pct"))

    if pricing_spread:
        kpis["price_delta_min"] = pricing_spread.get("min")
        kpis["price_delta_max"] = pricing_spread.get("max")
    else:
        kpis["price_delta_min"] = None
        kpis["price_delta_max"] = None

    # Orders-derived metrics
    orders_all_df = _scope_subset(orders_df, "all")
    orders_filt_df = _scope_subset(orders_df, "filtered")
    if orders_filt_df.empty:
        orders_filt_df = orders_all_df.copy()

    # Cadence histogram
    cadence_days: List[float] = []
    if not orders_all_df.empty:
        order_dates = sorted(pd.to_datetime(orders_all_df["order_date"].dropna()).dt.date.unique().tolist())
        diffs = []
        for i in range(1, len(order_dates)):
            delta = (order_dates[i] - order_dates[i - 1]).days
            if delta >= 0:
                diffs.append(delta)
        cadence_days = diffs
        if diffs:
            kpis["cadence_min_days"] = min(diffs)
            kpis["cadence_max_days"] = max(diffs)
            kpis["cadence_avg_days"] = sum(diffs) / len(diffs)
            kpis["cadence_median_days"] = float(pd.Series(diffs).median())
            kpis["cadence_p90_days"] = float(pd.Series(diffs).quantile(0.9))

    # Action center counts (fallback to orders frame only if action query missing)
    if action_row is None or kpis.get("orders_last_30") is None:
        order_dates_series = pd.to_datetime(orders_all_df["order_date"]).astype("datetime64[ns]")
        threshold_30 = (activity_reference_ts - pd.Timedelta(days=29)).to_datetime64()
        threshold_90 = (activity_reference_ts - pd.Timedelta(days=89)).to_datetime64()
        upper_bound = activity_reference_ts.to_datetime64()
        kpis["orders_last_30"] = int(((order_dates_series >= threshold_30) & (order_dates_series <= upper_bound)).sum())
        kpis["orders_last_90"] = int(((order_dates_series >= threshold_90) & (order_dates_series <= upper_bound)).sum())
    if not orders_all_df.empty:
        try:
            last_order_from_orders = pd.to_datetime(orders_all_df["order_date"], errors="coerce")
            last_order_from_orders = last_order_from_orders[last_order_from_orders <= activity_reference_ts]
            last_order_from_orders = last_order_from_orders.max() if not last_order_from_orders.empty else pd.NaT
            if pd.notna(last_order_from_orders):
                kpis["days_since_last_order"] = max(int((activity_reference_ts - last_order_from_orders.normalize()).days), 0)
        except Exception:
            pass

    # Window-level deltas (vs prior window)
    try:
        orders_all_tmp = orders_all_df.copy()
        orders_filt_tmp = orders_filt_df.copy()
        if not orders_all_tmp.empty:
            orders_all_tmp["order_date"] = pd.to_datetime(orders_all_tmp["order_date"], errors="coerce")
            prior_mask = (orders_all_tmp["order_date"] >= prior_start_ts) & (orders_all_tmp["order_date"] <= prior_end_ts)
            revenue_prior_window = _sum_numeric(orders_all_tmp.loc[prior_mask, "revenue"])
            orders_prior_window = int(prior_mask.sum())
            if not orders_filt_tmp.empty:
                revenue_cur_window = _sum_numeric(orders_filt_tmp.get("revenue", []))
                orders_cur_window = int(len(orders_filt_tmp.index))
            else:
                cur_mask = (orders_all_tmp["order_date"] >= window_start_ts) & (orders_all_tmp["order_date"] <= window_end_ts)
                revenue_cur_window = _sum_numeric(orders_all_tmp.loc[cur_mask, "revenue"])
                orders_cur_window = int(cur_mask.sum())
            kpis["revenue_window"] = revenue_cur_window
            kpis["revenue_prior_window"] = revenue_prior_window
            kpis["revenue_delta_window"] = revenue_cur_window - revenue_prior_window
            kpis["revenue_delta_pct_window"] = (
                None if revenue_prior_window == 0 else ((revenue_cur_window - revenue_prior_window) / revenue_prior_window) * 100.0
            )
            kpis["orders_window"] = orders_cur_window
            kpis["orders_prior_window"] = orders_prior_window
            kpis["orders_delta_window"] = orders_cur_window - orders_prior_window
    except Exception:
        pass

    # Basket stats (filtered window)
    basket_orders = len(orders_filt_df.index)
    lines_vals = _numeric_values(orders_filt_df.get("lines", []))
    weight_vals = _numeric_values(orders_filt_df.get("weight_lb", []))
    items_vals = _numeric_values(orders_filt_df.get("items", []))
    order_revenue_vals = _numeric_values(orders_filt_df.get("revenue", []))
    basket_stats = {
        "orders": basket_orders,
        "orders_lifetime": int(len(orders_all_df.index)),
        "avg_lines_per_order": _mean_numeric(lines_vals) if basket_orders else 0.0,
        "avg_weight_lb": _mean_numeric(weight_vals) if basket_orders else 0.0,
        "avg_items": _mean_numeric(items_vals) if basket_orders else 0.0,
        "median_lines_per_order": _median_numeric(lines_vals) if basket_orders else 0.0,
        "median_weight_lb": _median_numeric(weight_vals) if basket_orders else 0.0,
        "median_items": _median_numeric(items_vals) if basket_orders else 0.0,
        "median_order_value": _median_numeric(order_revenue_vals) if basket_orders else 0.0,
        "aov_stddev": _std_numeric(order_revenue_vals) if basket_orders else 0.0,
    }
    if basket_orders and kpis.get("aov") is None:
        try:
            kpis["aov"] = _sum_numeric(orders_filt_df.get("revenue", [])) / basket_orders
        except Exception:
            pass
    kpis["aov_median_order"] = basket_stats.get("median_order_value")
    total_weight_window = _sum_numeric(orders_filt_df.get("weight_lb", []))
    total_weight_lifetime = _sum_numeric(orders_all_df.get("weight_lb", []))
    kpis["asp_lb"] = (
        (_sum_numeric(orders_filt_df.get("revenue", [])) / total_weight_window)
        if total_weight_window > 0
        else None
    )
    kpis["profit_lb"] = (
        (_sum_numeric(orders_filt_df.get("profit", [])) / total_weight_window)
        if total_weight_window > 0
        else None
    )
    kpis["lifetime_weight_lb"] = total_weight_lifetime
    kpis["avg_lb_per_order"] = _safe_ratio_value(total_weight_window, basket_orders)
    kpis["avg_lb_per_order_lifetime"] = _safe_ratio_value(total_weight_lifetime, len(orders_all_df.index))
    kpis["avg_lb_per_week"] = _safe_ratio_value(total_weight_lifetime, max(kpis.get("weeks_active") or 0, 1))
    kpis["avg_lb_per_month"] = _safe_ratio_value(total_weight_lifetime, max(kpis.get("months_active") or 0, 1))

    # Monthly trend arrays (filtered scope for UI trend)
    monthly_filt = _scope_subset(monthly_df, "filtered")
    if monthly_filt.empty:
        monthly_filt = _scope_subset(monthly_df, "all")
    monthly_points: List[Dict[str, Any]] = []
    trend_build_error = False
    if not monthly_filt.empty:
        try:
            for rec in monthly_filt.to_dict(orient="records"):
                if not isinstance(rec, dict):
                    continue
                month_raw = _none_if_na(rec.get("month"))
                month_ts = pd.to_datetime(month_raw, errors="coerce")
                if pd.isna(month_ts):
                    continue
                month_ts = month_ts.to_period("M").to_timestamp()
                rev = _sum_numeric([rec.get("revenue")])
                ords = int(round(_sum_numeric([rec.get("orders")])))
                cost_v = _sum_numeric([rec.get("cost")])
                weight_v = _sum_numeric([rec.get("weight_lb")])
                profit_v = _sum_numeric([rec.get("profit")])
                margin_raw = _none_if_na(rec.get("margin_pct"))
                margin_v = None
                if rev != 0:
                    try:
                        margin_v = float(margin_raw) if margin_raw is not None else 0.0
                    except Exception:
                        margin_v = 0.0
                monthly_points.append(
                    {
                        "month": month_ts,
                        "month_label": str(month_ts.strftime("%Y-%m")),
                        "revenue": float(rev),
                        "orders": int(ords),
                        "cost": float(cost_v),
                        "weight_lb": float(weight_v),
                        "profit": float(profit_v),
                        "margin_pct": margin_v,
                    }
                )
        except Exception:
            trend_build_error = True
            try:
                current_app.logger.exception(
                    "customers.drilldown.monthly_trend_build_failed",
                    extra={"customer_id": customer_id, "window_start": window_start_iso, "window_end": window_end_iso},
                )
            except Exception:
                pass
            monthly_points = []
    monthly_points.sort(key=lambda r: r.get("month"))
    months = [str(r.get("month_label") or "") for r in monthly_points]
    monthly_revenue = [float(r.get("revenue") or 0.0) for r in monthly_points]
    monthly_orders = [int(r.get("orders") or 0) for r in monthly_points]
    monthly_cost = [float(r.get("cost") or 0.0) for r in monthly_points]
    monthly_weight = [float(r.get("weight_lb") or 0.0) for r in monthly_points]
    monthly_profit = [float(r.get("profit") or 0.0) for r in monthly_points]
    monthly_margin = [r.get("margin_pct") for r in monthly_points]

    months_count = len(monthly_revenue) if monthly_revenue else 0
    if months_count:
        kpis["revenue_per_month"] = kpis["total_revenue"] / months_count if kpis["total_revenue"] else None
        kpis["orders_per_month"] = (kpis["total_orders"] / months_count) if kpis["total_orders"] else None
    if kpis.get("revenue_last_90"):
        kpis["margin_last_90"] = (
            kpis["profit_last_90"] / kpis["revenue_last_90"] * 100.0 if kpis["revenue_last_90"] else None
        )
    # Backward-compatible aliases used by UI delta chips.
    kpis["revenue_prior_90"] = kpis.get("revenue_prior_window")
    kpis["revenue_delta_90"] = kpis.get("revenue_delta_window")
    kpis["revenue_delta_pct_90"] = kpis.get("revenue_delta_pct_window")
    kpis["orders_prior_90"] = kpis.get("orders_prior_window")
    kpis["orders_delta_90"] = kpis.get("orders_delta_window")

    # Segment + health labels
    recency_days = kpis.get("days_since_last_order")
    if recency_days is None:
        recency_band = "No activity"
        active_status = "Unknown"
    elif recency_days < 30:
        recency_band = "Active <30d"
        active_status = "Active"
    elif recency_days < 60:
        recency_band = "Warm 30-60d"
        active_status = "Warm"
    elif recency_days < 90:
        recency_band = "At risk 60-90d"
        active_status = "At risk"
    elif recency_days < 180:
        recency_band = "Churned >90d"
        active_status = "Churned"
    else:
        recency_band = "Dormant >180d"
        active_status = "Dormant"

    first_order_ts = pd.to_datetime(kpis.get("first_order"), errors="coerce")
    is_new_window = bool(pd.notna(first_order_ts) and first_order_ts >= window_start_ts and first_order_ts <= window_end_ts)
    revenue_cur_window = float(kpis.get("revenue_window") or 0.0)
    revenue_prior_window = float(kpis.get("revenue_prior_window") or 0.0)
    reactivated = False
    try:
        hist_dates = pd.to_datetime(orders_all_df.get("order_date"), errors="coerce").dropna().sort_values()
        if len(hist_dates.index) >= 2:
            latest = hist_dates.iloc[-1]
            prev = hist_dates.iloc[-2]
            gap_days = int((latest - prev).days)
            reactivated = bool(
                latest >= window_start_ts
                and latest <= window_end_ts
                and gap_days >= 90
            )
    except Exception:
        reactivated = False

    if recency_days is None:
        seg = "No Activity"
        seg_reason = "No order history is visible under current scope."
    elif recency_days >= 180:
        seg = "Churned"
        seg_reason = f"Last order {int(recency_days)} days ago."
    elif is_new_window:
        seg = "New"
        seg_reason = "First observed order occurred in the current window."
    elif reactivated:
        seg = "Reactivated"
        seg_reason = "Recent order occurred after a long inactivity gap."
    elif recency_days >= 90:
        seg = "At risk"
        seg_reason = f"No order in the past {int(recency_days)} days."
    elif revenue_prior_window > 0 and revenue_cur_window >= (revenue_prior_window * 1.10):
        seg = "Growing"
        seg_reason = "Revenue is up more than 10% vs prior window."
    else:
        seg = "Stable"
        seg_reason = "Recent activity and revenue are broadly stable."

    kpis["recency_band"] = recency_band
    kpis["active_status"] = active_status
    if recency_days is None:
        kpis["churn_risk"] = "Unknown"
    elif recency_days < 60:
        kpis["churn_risk"] = "Low"
    elif recency_days < 90:
        kpis["churn_risk"] = "Medium"
    else:
        kpis["churn_risk"] = "High"
    kpis["segment"] = seg
    kpis["segment_source"] = "rule_based"
    kpis["segment_reason"] = seg_reason
    kpis.setdefault("rfm_segment", None)
    kpis.setdefault("clv_segment", None)

    # Product table + derived tops
    product_rows: List[Dict[str, Any]] = []
    for row in product_df.itertuples():
        revenue_now = _clean_float(getattr(row, "revenue", 0.0))
        revenue_prior = _clean_float(getattr(row, "revenue_prior", 0.0))
        cost_now = _clean_float(getattr(row, "cost", 0.0))
        profit_now = _clean_float(getattr(row, "profit", 0.0))
        profit_prior = _clean_float(getattr(row, "profit_prior", 0.0))
        margin_now = None if revenue_now == 0 else _clean_float(getattr(row, "margin_pct", None))
        margin_prior = None if revenue_prior == 0 else _clean_float(getattr(row, "margin_prior_pct", None))
        weight_now = _clean_float(getattr(row, "weight_lb", 0.0))
        units_now = _clean_float(getattr(row, "qty", 0.0))
        orders_now = int(getattr(row, "orders", 0) or 0)
        line_count = int(getattr(row, "line_count", 0) or 0)
        cost_null_rows = int(getattr(row, "cost_null_rows", 0) or 0)
        cost_missing_pct = (float(cost_null_rows) / float(line_count) * 100.0) if line_count > 0 else 0.0
        delta_revenue = revenue_now - revenue_prior
        delta_revenue_pct = None if revenue_prior == 0 else (delta_revenue / revenue_prior) * 100.0
        delta_status = "new" if revenue_prior == 0 and revenue_now > 0 else ("lost" if revenue_now == 0 and revenue_prior > 0 else "existing")
        asp_lb = (revenue_now / weight_now) if weight_now > 0 else None
        cost_lb = (cost_now / weight_now) if weight_now > 0 else None
        contribution_lb = ((asp_lb - cost_lb) if (asp_lb is not None and cost_lb is not None) else None)
        asp_unit = (revenue_now / units_now) if units_now > 0 else None
        product_rows.append(
            {
                "sku": getattr(row, "sku", None),
                "product": getattr(row, "product", None),
                "protein_family": getattr(row, "protein_family", None) or "Unassigned",
                "category": getattr(row, "category", None) or "Unassigned",
                "revenue": revenue_now,
                "revenue_prior": revenue_prior,
                "delta_revenue": delta_revenue,
                "delta_revenue_pct": delta_revenue_pct,
                "delta_revenue_status": delta_status,
                "cost": cost_now,
                "effective_cost_basis": cost_now,
                "profit": profit_now,
                "profit_prior": profit_prior,
                "margin_pct": margin_now,
                "margin_prior_pct": margin_prior,
                "margin_delta_pp": (None if margin_now is None or margin_prior is None else (margin_now - margin_prior)),
                "cost_null_rows": cost_null_rows,
                "cost_missing_pct": cost_missing_pct,
                "weight_lb": weight_now,
                "units": units_now,
                "orders": orders_now,
                "line_count": line_count,
                "asp_lb": asp_lb,
                "cost_lb": cost_lb,
                "effective_cost_lb": cost_lb,
                "contribution_lb": contribution_lb,
                "asp_unit": asp_unit,
            }
        )

    protein_rows: List[Dict[str, Any]] = []
    for row in protein_df.itertuples():
        revenue_now = _clean_float(getattr(row, "revenue", 0.0))
        revenue_prior = _clean_float(getattr(row, "revenue_prior", 0.0))
        profit_now = _clean_float(getattr(row, "profit", 0.0))
        weight_now = _clean_float(getattr(row, "weight_lb", 0.0))
        margin_now = _safe_ratio_value(profit_now, revenue_now, pct=True)
        delta_revenue = revenue_now - revenue_prior
        protein_rows.append(
            {
                "family": getattr(row, "protein_family", None) or "Unassigned",
                "revenue": revenue_now,
                "revenue_prior": revenue_prior,
                "delta_revenue": delta_revenue,
                "delta_revenue_pct": _safe_ratio_value(delta_revenue, revenue_prior, pct=True),
                "profit": profit_now,
                "margin_pct": margin_now,
                "weight_lb": weight_now,
                "weight_prior": _clean_float(getattr(row, "weight_prior", 0.0)),
                "orders": int(getattr(row, "orders", 0) or 0),
                "products": int(getattr(row, "products", 0) or 0),
            }
        )

    category_rows: List[Dict[str, Any]] = []
    for row in category_df.itertuples():
        revenue_now = _clean_float(getattr(row, "revenue", 0.0))
        revenue_prior = _clean_float(getattr(row, "revenue_prior", 0.0))
        profit_now = _clean_float(getattr(row, "profit", 0.0))
        weight_now = _clean_float(getattr(row, "weight_lb", 0.0))
        margin_now = _safe_ratio_value(profit_now, revenue_now, pct=True)
        delta_revenue = revenue_now - revenue_prior
        category_rows.append(
            {
                "category": getattr(row, "category", None) or "Unassigned",
                "revenue": revenue_now,
                "revenue_prior": revenue_prior,
                "delta_revenue": delta_revenue,
                "delta_revenue_pct": _safe_ratio_value(delta_revenue, revenue_prior, pct=True),
                "profit": profit_now,
                "margin_pct": margin_now,
                "weight_lb": weight_now,
                "weight_prior": _clean_float(getattr(row, "weight_prior", 0.0)),
                "orders": int(getattr(row, "orders", 0) or 0),
                "products": int(getattr(row, "products", 0) or 0),
            }
        )

    product_rows = margin_rules.annotate_margin_rows(
        product_rows,
        protein_keys=("protein_family",),
        category_keys=("category",),
        revenue_key="revenue",
        cost_key="cost",
        profit_key="profit",
        margin_key="margin_pct",
        unit_cost_key="cost_lb",
    )
    protein_rows = margin_rules.annotate_margin_rows(
        protein_rows,
        protein_keys=("family",),
        category_keys=("family",),
        revenue_key="revenue",
        profit_key="profit",
        margin_key="margin_pct",
    )
    category_rows = margin_rules.annotate_margin_rows(
        category_rows,
        protein_keys=("category",),
        category_keys=("category",),
        revenue_key="revenue",
        profit_key="profit",
        margin_key="margin_pct",
    )
    weighted_target_margin_pct = margin_rules.weighted_target_margin_pct(product_rows)
    if weighted_target_margin_pct is not None:
        margin_target_pct = float(weighted_target_margin_pct)
    weighted_minimum_margin_pct = margin_rules.weighted_minimum_margin_pct(product_rows)
    if weighted_minimum_margin_pct is not None:
        kpis["minimum_margin_pct"] = float(weighted_minimum_margin_pct)
    if margin_target_pct is not None:
        kpis["margin_target_pct"] = float(margin_target_pct)
        kpis["target_margin_pct"] = float(margin_target_pct)

    product_sorted = sorted(product_rows, key=lambda r: r.get("revenue", 0.0), reverse=True)
    product_spend_top = [{"label": r["product"], "revenue": r.get("revenue", 0.0)} for r in product_sorted[:10]]
    product_spend_full = [{"label": r["product"], "revenue": r.get("revenue", 0.0)} for r in product_sorted]
    product_weight_sorted = sorted(
        [{"label": r["product"], "weight_lb": r.get("weight_lb", 0.0)} for r in product_rows],
        key=lambda r: r.get("weight_lb", 0.0),
        reverse=True,
    )
    product_weight_top = product_weight_sorted[:10]
    product_weight_full = product_weight_sorted
    profit_sorted = sorted(product_rows, key=lambda r: r.get("profit", 0.0), reverse=True)[:10]
    product_profit_chart = {
        "labels": [r["product"] for r in profit_sorted],
        "profit": [r.get("profit", 0.0) for r in profit_sorted],
        "margin": [r.get("margin_pct") for r in profit_sorted],
    }
    if (not kpis.get("top_product_label")) and product_spend_top:
        kpis["top_product_label"] = product_spend_top[0]["label"]
        kpis["top_product_revenue"] = product_spend_top[0]["revenue"]
    if (not kpis.get("top_weight_product")) and product_weight_top:
        kpis["top_weight_product"] = product_weight_top[0]["label"]
        kpis["top_weight_lb"] = product_weight_top[0]["weight_lb"]
    if (not kpis.get("top_profit_product")) and profit_sorted:
        kpis["top_profit_product"] = profit_sorted[0]["product"]
        kpis["top_profit_value"] = profit_sorted[0]["profit"]
        kpis["top_profit_margin"] = profit_sorted[0]["margin_pct"]

    # Weekday arrays
    weekday_labels = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    weekday_revenue_map = {str(row["weekday_label"]): float(row["revenue"] or 0.0) for _, row in weekday_df.iterrows()}
    weekday_weight_map = {str(row["weekday_label"]): float(row.get("weight_lb") or 0.0) for _, row in weekday_df.iterrows()}
    weekday_orders_map = {str(row["weekday_label"]): int(row.get("orders") or 0) for _, row in weekday_df.iterrows()}
    weekday_revenue = [weekday_revenue_map.get(lbl, 0.0) for lbl in weekday_labels]
    weekday_weight = [weekday_weight_map.get(lbl, 0.0) for lbl in weekday_labels]
    weekday_orders = [weekday_orders_map.get(lbl, 0) for lbl in weekday_labels]
    weekday_total = sum(weekday_revenue)
    if weekday_total > 0 and kpis.get("best_weekday") in weekday_labels:
        try:
            best_idx = weekday_labels.index(str(kpis.get("best_weekday")))
            kpis["best_weekday_share_pct"] = (weekday_revenue[best_idx] / weekday_total) * 100.0
        except Exception:
            kpis["best_weekday_share_pct"] = None
    weekday_weight_total = sum(weekday_weight)
    if weekday_weight_total > 0:
        best_weight_idx = max(range(len(weekday_labels)), key=lambda idx: weekday_weight[idx])
        kpis["best_weight_weekday"] = weekday_labels[best_weight_idx]
        kpis["best_weight_weekday_share_pct"] = (weekday_weight[best_weight_idx] / weekday_weight_total) * 100.0

    # Seasonality heatmap (lifetime)
    yoy_months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    monthly_all = _scope_subset(monthly_df, "all")
    ym_revenue: Dict[Tuple[int, int], float] = {}
    year_values: List[int] = []
    max_allowed_month_ts = min(window_end_ts, _utc_today_ts_naive())
    seasonality_build_error = False
    if not monthly_all.empty:
        try:
            for rec in monthly_all.to_dict(orient="records"):
                if not isinstance(rec, dict):
                    continue
                month_raw = _none_if_na(rec.get("month"))
                month_ts = pd.to_datetime(month_raw, errors="coerce")
                if pd.isna(month_ts):
                    continue
                month_ts = month_ts.to_period("M").to_timestamp()
                if month_ts > max_allowed_month_ts:
                    continue
                yr = int(month_ts.year)
                mon = int(month_ts.month)
                rev = _sum_numeric([rec.get("revenue")])
                ym_revenue[(yr, mon)] = ym_revenue.get((yr, mon), 0.0) + float(rev)
                year_values.append(yr)
        except Exception:
            seasonality_build_error = True
            try:
                current_app.logger.exception(
                    "customers.drilldown.seasonality_build_failed",
                    extra={"customer_id": customer_id, "window_start": window_start_iso, "window_end": window_end_iso},
                )
            except Exception:
                pass
            ym_revenue = {}
            year_values = []
    min_year = min(year_values) if year_values else None
    max_year = max(year_values) if year_values else None
    yoy_years: List[int] = []
    yoy_matrix: List[List[float]] = []
    if min_year is not None and max_year is not None:
        for yr in range(int(min_year), int(max_year) + 1):
            yoy_years.append(int(yr))
            row = []
            for m in range(1, 13):
                row.append(float(ym_revenue.get((yr, m), 0.0)))
            yoy_matrix.append(row)

    monthly_previous_year: List[float | None] = []
    for mp in monthly_points:
        month_ts = mp.get("month")
        if isinstance(month_ts, pd.Timestamp):
            prev_key = (int(month_ts.year) - 1, int(month_ts.month))
            if prev_key in ym_revenue:
                monthly_previous_year.append(float(ym_revenue.get(prev_key, 0.0)))
            else:
                monthly_previous_year.append(None)
        else:
            monthly_previous_year.append(None)

    monthly_revenue_lb = [
        (_safe_ratio_value(rev, wt) if (wt or 0.0) > 0 else None)
        for rev, wt in zip(monthly_revenue, monthly_weight)
    ]
    monthly_profit_lb = [
        (_safe_ratio_value(profit_v, wt) if (wt or 0.0) > 0 else None)
        for profit_v, wt in zip(monthly_profit, monthly_weight)
    ]
    monthly_revenue_rolling = _rolling_average(monthly_revenue, window=3)
    monthly_weight_rolling = _rolling_average(monthly_weight, window=3)

    # Seed cross-sell from the already-built top product rows to avoid another lifetime scan.
    xsell_top_skus = [
        str(row.get("sku")).strip()
        for row in product_sorted
        if row.get("sku") not in (None, "") and _clean_float(row.get("revenue")) > 0
    ][:3]

    # Price intelligence (peer median)
    price_table: List[Dict[str, Any]] = []
    price_delta_min = None
    price_delta_max = None
    top_price_rows = [r for r in product_sorted if r.get("revenue", 0) > 0 and str(r.get("sku") or "").strip()][:8]
    top_skus = [str(r.get("sku")).strip() for r in top_price_rows]
    primary_region = kpis.get("primary_region")

    if top_skus:
        placeholders = ", ".join("?" for _ in top_skus)
        peer_sql = f"""
            WITH peer AS (
                SELECT
                    COALESCE(CAST({product_id_col or product_col} AS VARCHAR), CAST({product_col} AS VARCHAR)) AS sku,
                    {product_col} AS product,
                    {revenue_col} AS revenue,
                    COALESCE({weight_expr},0) AS weight,
                    COALESCE({qty_expr},0) AS qty,
                    {fs.CANON.customer_id} AS customer_id,
                    {region_expr} AS region
                FROM fact
                WHERE {scope_where_sql}
                  AND CAST({date_col} AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
                  AND COALESCE(CAST({product_id_col or product_col} AS VARCHAR), CAST({product_col} AS VARCHAR)) IN ({placeholders})
                  AND {fs.CANON.customer_id} <> ?
            ),
            priced AS (
                SELECT
                    sku,
                    product,
                    CASE WHEN weight > 0 THEN revenue/weight WHEN qty > 0 THEN revenue/qty END AS unit_price,
                    region
                FROM peer
                WHERE (? IS NULL OR region = ?)
            )
            SELECT sku, ANY_VALUE(product) AS product, QUANTILE_CONT(unit_price, 0.5) AS peer_median
            FROM priced
            WHERE unit_price IS NOT NULL
            GROUP BY sku
        """
        peer_params = list(scope_where_params) + [window_start_iso, window_end_iso] + list(top_skus) + [customer_id, primary_region, primary_region]
        peer_df = fact_store.execute_sql_df(peer_sql, peer_params, tag="customers.drilldown.price_peers")
        peer_map = {str(row.sku): float(row.peer_median) for row in peer_df.itertuples() if pd.notna(row.peer_median)}

        qty_lookup = {str(row.get("sku") or ""): row.get("units") for row in product_rows}
        for r in product_rows:
            sku = str(r.get("sku") or "").strip()
            if not sku or sku not in top_skus:
                continue
            weight_val = r.get("weight_lb", 0.0) or 0.0
            qty_val = qty_lookup.get(sku)
            unit_price = None
            if weight_val > 0:
                unit_price = (r.get("revenue", 0.0) or 0.0) / weight_val
            elif qty_val and qty_val > 0:
                unit_price = (r.get("revenue", 0.0) or 0.0) / qty_val
            peer = peer_map.get(sku)
            delta_pct = None
            if unit_price is not None and peer:
                delta_pct = ((unit_price - peer) / peer) * 100.0 if peer else None
                price_delta_min = price_delta_min if price_delta_min is not None else delta_pct
                price_delta_max = price_delta_max if price_delta_max is not None else delta_pct
                if delta_pct is not None:
                    price_delta_min = min(price_delta_min, delta_pct) if price_delta_min is not None else delta_pct
                    price_delta_max = max(price_delta_max, delta_pct) if price_delta_max is not None else delta_pct
            price_table.append(
                {
                    "sku": r.get("sku"),
                    "product": r["product"],
                    "category": r.get("category"),
                    "protein_family": r.get("protein_family"),
                    "cust_unit_price": unit_price,
                    "peer_median": peer,
                    "delta_pct": delta_pct,
                    "suggest_price": peer,
                    "revenue": r.get("revenue"),
                    "weight_lb": r.get("weight_lb"),
                    "margin_pct": r.get("margin_pct"),
                    "profit_lb": r.get("contribution_lb"),
                    "price_quality_flag": (
                        "Watch"
                        if delta_pct is not None and abs(float(delta_pct)) >= 8.0
                        else "Aligned"
                    ),
                }
            )

    if kpis.get("price_delta_min") is None and price_delta_min is not None:
        kpis["price_delta_min"] = price_delta_min
    if kpis.get("price_delta_max") is None and price_delta_max is not None:
        kpis["price_delta_max"] = price_delta_max
    deltas = [float(r.get("delta_pct")) for r in price_table if r.get("delta_pct") is not None]
    if deltas:
        above_peer = [d for d in deltas if d > 5.0]
        below_peer = [d for d in deltas if d < -5.0]
        kpis["pricing_dispersion_above_peer_pct"] = (len(above_peer) / len(deltas)) * 100.0
        kpis["pricing_dispersion_below_peer_pct"] = (len(below_peer) / len(deltas)) * 100.0
        kpis["avg_abs_price_delta_pct"] = sum(abs(d) for d in deltas) / len(deltas)
    else:
        kpis["pricing_dispersion_above_peer_pct"] = None
        kpis["pricing_dispersion_below_peer_pct"] = None
        kpis["avg_abs_price_delta_pct"] = None

    # Cross-sell ideas (cached, lifetime scope with 24m lookback)
    cross_sell: List[Dict[str, Any]] = []
    cross_limit = 500 if drilldown_export_all else 5
    try:
        top_skus_key = hashlib.sha256("|".join(xsell_top_skus or []).encode("utf-8")).hexdigest()
        scope_key = hashlib.sha256(json.dumps(scope or {}, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        dataset_version = fact_store.cache_buster()
        cache_key = f"cust:{customer_id}:xsell:{top_skus_key}:scope:{scope_key}:ds:{dataset_version}:limit:{cross_limit}"

        def _build_cross_sell() -> List[Dict[str, Any]]:
            if not xsell_top_skus:
                return []
            placeholders_top = ", ".join("?" for _ in xsell_top_skus)
            cross_sql = f"""
                WITH global_lines AS (
                    SELECT
                        {order_col} AS order_id,
                        CAST({order_date_col} AS DATE) AS order_date,
                        {product_id_col or product_col} AS sku,
                        {product_col} AS description,
                        COALESCE(NULLIF(CAST({protein_expr} AS VARCHAR), ''), 'Unassigned') AS protein_family,
                        COALESCE(NULLIF(CAST({category_expr} AS VARCHAR), ''), 'Unassigned') AS category,
                        {revenue_col} AS revenue
                    FROM fact
                    WHERE {scope_where_sql}
                      AND {order_col} IS NOT NULL
                      AND {order_date_col} IS NOT NULL
                      AND {product_col} IS NOT NULL
                      AND CAST({order_date_col} AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
                ),
                cust_skus AS (
                    SELECT DISTINCT {product_id_col or product_col} AS sku
                    FROM fact
                    WHERE {where_sql_all}
                      AND {order_col} IS NOT NULL
                      AND {product_col} IS NOT NULL
                      AND CAST({order_date_col} AS DATE) <= CAST(? AS DATE)
                ),
                orders_with_top AS (
                    SELECT DISTINCT order_id
                    FROM global_lines
                    WHERE sku IN ({placeholders_top})
                ),
                global_orders AS (
                    SELECT COUNT(DISTINCT order_id) AS total_orders FROM global_lines
                ),
                candidate_base AS (
                    SELECT
                        sku,
                        COUNT(DISTINCT order_id) AS candidate_orders
                    FROM global_lines
                    WHERE sku NOT IN (SELECT sku FROM cust_skus)
                    GROUP BY 1
                ),
                cand AS (
                    SELECT
                        g.sku,
                        ANY_VALUE(g.description) AS description,
                        ANY_VALUE(g.protein_family) AS protein_family,
                        ANY_VALUE(g.category) AS category,
                        COUNT(DISTINCT g.order_id) AS co_orders,
                        SUM(g.revenue) AS revenue
                    FROM global_lines g
                    JOIN orders_with_top o ON o.order_id = g.order_id
                    WHERE g.sku NOT IN (SELECT sku FROM cust_skus)
                    GROUP BY 1
                )
                SELECT
                    c.sku,
                    c.description,
                    c.protein_family,
                    c.category,
                    c.co_orders,
                    c.revenue,
                    cb.candidate_orders,
                    go.total_orders,
                    t.total_top_orders,
                    CASE WHEN t.total_top_orders = 0 THEN NULL ELSE (c.co_orders::DOUBLE / t.total_top_orders::DOUBLE) * 100 END AS confidence_pct,
                    CASE WHEN go.total_orders = 0 THEN NULL ELSE (c.co_orders::DOUBLE / go.total_orders::DOUBLE) * 100 END AS support_pct,
                    CASE
                        WHEN cb.candidate_orders IS NULL OR cb.candidate_orders = 0 OR go.total_orders = 0 OR t.total_top_orders = 0 THEN NULL
                        ELSE (c.co_orders::DOUBLE / t.total_top_orders::DOUBLE) / (cb.candidate_orders::DOUBLE / go.total_orders::DOUBLE)
                    END AS lift
                FROM cand c
                LEFT JOIN candidate_base cb ON cb.sku = c.sku
                CROSS JOIN global_orders go
                CROSS JOIN (SELECT COUNT(*) AS total_top_orders FROM orders_with_top) t
                ORDER BY c.co_orders DESC NULLS LAST, c.revenue DESC NULLS LAST
                LIMIT {int(cross_limit)}
            """
            xs_params: List[Any] = []
            xs_params.extend(scope_where_params)
            xs_params.extend([cross_sell_lookback_iso, activity_reference_iso])
            xs_params.extend(params_all)
            xs_params.append(activity_reference_iso)
            xs_params.extend(xsell_top_skus)
            xs_df = fact_store.execute_sql_df(cross_sql, xs_params, tag="customers.drilldown.cross_sell")
            rows: List[Dict[str, Any]] = []
            for r in xs_df.itertuples():
                rows.append(
                    {
                        "product": getattr(r, "description", None) or getattr(r, "sku", None),
                        "sku": getattr(r, "sku", None),
                        "protein_family": getattr(r, "protein_family", None) or "Unassigned",
                        "category": getattr(r, "category", None) or "Unassigned",
                        "co_orders": int(getattr(r, "co_orders", 0) or 0),
                        "candidate_orders": int(getattr(r, "candidate_orders", 0) or 0),
                        "revenue": float(getattr(r, "revenue", 0.0) or 0.0),
                        "confidence_pct": (
                            float(getattr(r, "confidence_pct"))
                            if pd.notna(getattr(r, "confidence_pct", None))
                            else None
                        ),
                        "support_pct": (
                            float(getattr(r, "support_pct"))
                            if pd.notna(getattr(r, "support_pct", None))
                            else None
                        ),
                        "lift": (
                            float(getattr(r, "lift"))
                            if pd.notna(getattr(r, "lift", None))
                            else None
                        ),
                        "reason": f"Co-purchased in {int(getattr(r, 'co_orders', 0) or 0)} peer orders",
                        "explanation": (
                            f"Confidence {float(getattr(r, 'confidence_pct') or 0.0):.1f}% with "
                            f"support {float(getattr(r, 'support_pct') or 0.0):.1f}% of scoped orders."
                        ),
                        "region": kpis.get("primary_region"),
                    }
                )
            return rows

        cross_sell, _cache_hit = CROSS_SELL_CACHE.get_or_compute(cache_key, ttl=60 * 60 * 12, builder=_build_cross_sell)
    except Exception:
        cross_sell = []
    kpis["cross_sell_count"] = len(cross_sell)

    orders_export_rows: List[Dict[str, Any]] = []
    if not orders_filt_df.empty:
        try:
            order_rows = orders_filt_df.to_dict(orient="records")
            order_rows.sort(
                key=lambda r: pd.to_datetime(r.get("order_date"), errors="coerce")
                if r.get("order_date") is not None
                else pd.Timestamp.min,
                reverse=True,
            )
            for rec in order_rows:
                order_date = pd.to_datetime(rec.get("order_date"), errors="coerce")
                orders_export_rows.append(
                    {
                        "order_id": rec.get("order_id"),
                        "order_date": (order_date.date().isoformat() if pd.notna(order_date) else None),
                        "revenue": _sum_numeric([rec.get("revenue")]),
                        "cost": _sum_numeric([rec.get("cost")]),
                        "profit": _sum_numeric([rec.get("profit")]),
                        "weight_lb": _sum_numeric([rec.get("weight_lb")]),
                        "items": _sum_numeric([rec.get("items")]),
                        "lines": int(round(_sum_numeric([rec.get("lines")]))),
                    }
                )
        except Exception:
            orders_export_rows = []

    scope_row = scope_totals_df.iloc[0] if not scope_totals_df.empty else None
    scope_revenue_lifetime = _clean_float(scope_row.get("revenue_lifetime_scope")) if scope_row is not None else 0.0
    scope_profit_lifetime = _clean_float(scope_row.get("profit_lifetime_scope")) if scope_row is not None else 0.0
    scope_weight_lifetime = _clean_float(scope_row.get("weight_lifetime_scope")) if scope_row is not None else 0.0
    scope_revenue_window = _clean_float(scope_row.get("revenue_window_scope")) if scope_row is not None else 0.0
    scope_profit_window = _clean_float(scope_row.get("profit_window_scope")) if scope_row is not None else 0.0
    scope_weight_window = _clean_float(scope_row.get("weight_window_scope")) if scope_row is not None else 0.0
    customers_in_scope = _safe_int(scope_row.get("customers_in_scope")) if scope_row is not None else 0
    customers_in_window = _safe_int(scope_row.get("customers_in_window")) if scope_row is not None else 0
    orders_in_scope_window = _safe_int(scope_row.get("orders_window_scope")) if scope_row is not None else 0

    assigned_owner_name = str(kpis.get("assigned_owner_sales_rep") or "").strip() or None
    dominant_seller_name = str(kpis.get("dominant_seller_sales_rep") or "").strip() or None
    owner_name = str(kpis.get("owner_sales_rep") or "").strip() or dominant_seller_name
    historical_owner_name = dominant_seller_name if dominant_seller_name and dominant_seller_name != owner_name else None
    owner_source_label = "Dominant visible seller"
    if assigned_owner_name:
        owner_source_label = "Assigned owner field"
    elif owner_rep_col and str(owner_rep_col).strip().lower() in {"primarysalesrepname", "owner", "accountowner", "accountmanager"}:
        owner_source_label = "Assigned owner field"
    owner_detail = "Owner inferred from the dominant visible seller in RBAC-visible history."
    if owner_source_label == "Assigned owner field":
        owner_detail = "Owner comes from the assigned account-owner field visible in current scope."
    elif dominant_seller_name:
        owner_detail = f"Inferred from dominant visible seller across visible history: {dominant_seller_name}."
    if historical_owner_name:
        owner_detail = f"Current owner differs from the dominant visible seller across history: {historical_owner_name}."
    kpis["owner_sales_rep"] = owner_name
    kpis["historical_owner_sales_rep"] = historical_owner_name

    current_product_revenue_total = _sum_numeric([row.get("revenue") for row in product_rows])
    current_product_profit_total = _sum_numeric([row.get("profit") for row in product_rows])
    current_product_weight_total = _sum_numeric([row.get("weight_lb") for row in product_rows])
    top_product_share_pct = _top_n_share(product_rows, "revenue", 1)
    top5_product_share_pct = _top_n_share(product_rows, "revenue", 5)
    top_weight_share_pct = _top_n_share(product_rows, "weight_lb", 1)
    top5_weight_share_pct = _top_n_share(product_rows, "weight_lb", 5)
    top_category_share_pct = _top_n_share(category_rows, "revenue", 1)
    top5_category_share_pct = _top_n_share(category_rows, "revenue", 5)
    kpis["contribution_share_pct"] = _safe_ratio_value(kpis.get("total_revenue"), scope_revenue_lifetime, pct=True)
    kpis["window_contribution_share_pct"] = _safe_ratio_value(kpis.get("revenue_window"), scope_revenue_window, pct=True)
    kpis["profit_share_pct"] = _safe_ratio_value(kpis.get("total_profit"), scope_profit_lifetime, pct=True)
    kpis["window_profit_share_pct"] = _safe_ratio_value(current_product_profit_total, scope_profit_window, pct=True)
    kpis["weight_share_pct"] = _safe_ratio_value(kpis.get("lifetime_weight_lb"), scope_weight_lifetime, pct=True)
    kpis["window_weight_share_pct"] = _safe_ratio_value(total_weight_window, scope_weight_window, pct=True)
    kpis["top_product_share_pct"] = top_product_share_pct
    kpis["top5_product_share_pct"] = top5_product_share_pct
    kpis["top_weight_share_pct"] = top_weight_share_pct
    kpis["top5_weight_share_pct"] = top5_weight_share_pct
    kpis["top_category_share_pct"] = top_category_share_pct
    kpis["top5_category_share_pct"] = top5_category_share_pct

    low_margin_candidates = [
        row
        for row in product_rows
        if row.get("revenue", 0.0) > 0
        and row.get("margin_pct") is not None
        and row.get("target_margin_pct") is not None
        and float(row.get("margin_pct") or 0.0) < float(row.get("target_margin_pct") or 0.0)
    ]
    low_margin_candidates.sort(key=lambda r: r.get("revenue", 0.0), reverse=True)
    negative_margin_candidates = [
        row for row in product_rows if row.get("revenue", 0.0) > 0 and (row.get("margin_pct") is not None and row.get("margin_pct") < 0)
    ]
    lost_products = [
        row for row in product_rows if (row.get("revenue_prior", 0.0) or 0.0) > 0 and (row.get("revenue", 0.0) or 0.0) == 0
    ]
    lost_products.sort(key=lambda r: r.get("revenue_prior", 0.0), reverse=True)
    declining_products = [
        row
        for row in product_rows
        if (row.get("revenue", 0.0) or 0.0) > 0
        and (row.get("revenue_prior", 0.0) or 0.0) > 0
        and (row.get("delta_revenue_pct") or 0.0) <= -20.0
    ]
    declining_products.sort(key=lambda r: r.get("delta_revenue_pct") or 0.0)
    fast_growing_products = [
        row
        for row in product_rows
        if (row.get("revenue", 0.0) or 0.0) > 0
        and (
            ((row.get("revenue_prior", 0.0) or 0.0) == 0)
            or ((row.get("delta_revenue_pct") or 0.0) >= 20.0)
        )
    ]
    fast_growing_products.sort(key=lambda r: r.get("delta_revenue_pct") or 9999.0, reverse=True)
    lost_categories = [
        row for row in category_rows if (row.get("revenue_prior", 0.0) or 0.0) > 0 and (row.get("revenue", 0.0) or 0.0) == 0
    ]
    lost_categories.sort(key=lambda r: r.get("revenue_prior", 0.0), reverse=True)
    declining_categories = [
        row
        for row in category_rows
        if (row.get("revenue", 0.0) or 0.0) > 0
        and (row.get("revenue_prior", 0.0) or 0.0) > 0
        and (row.get("delta_revenue_pct") or 0.0) <= -15.0
    ]
    declining_categories.sort(key=lambda r: r.get("delta_revenue_pct") or 0.0)
    lost_proteins = [
        row for row in protein_rows if (row.get("revenue_prior", 0.0) or 0.0) > 0 and (row.get("revenue", 0.0) or 0.0) == 0
    ]
    lost_proteins.sort(key=lambda r: r.get("revenue_prior", 0.0), reverse=True)
    category_sorted = sorted(category_rows, key=lambda r: r.get("revenue", 0.0), reverse=True)
    category_weight_sorted = sorted(category_rows, key=lambda r: r.get("weight_lb", 0.0), reverse=True)
    protein_sorted = sorted(protein_rows, key=lambda r: r.get("revenue", 0.0), reverse=True)
    protein_weight_sorted = sorted(protein_rows, key=lambda r: r.get("weight_lb", 0.0), reverse=True)
    protein_revenue_total = _sum_numeric([rec.get("revenue") for rec in protein_rows])
    protein_weight_total = _sum_numeric([rec.get("weight_lb") for rec in protein_rows])
    top_protein_share_pct = _top_n_share(protein_rows, "revenue", 1)
    top5_protein_share_pct = _top_n_share(protein_rows, "revenue", 5)

    under_target_margin_exposure_pct = _safe_ratio_value(
        _sum_numeric([row.get("revenue") for row in low_margin_candidates]),
        current_product_revenue_total or kpis.get("revenue_window"),
        pct=True,
    )
    negative_margin_share_pct = _safe_ratio_value(
        _sum_numeric([row.get("revenue") for row in negative_margin_candidates]),
        current_product_revenue_total or kpis.get("revenue_window"),
        pct=True,
    )
    recoverable_margin_uplift = sum(
        max(float(row.get("target_margin_pct") or 0.0) - float(row.get("margin_pct") or 0.0), 0.0) / 100.0 * float(row.get("revenue") or 0.0)
        for row in low_margin_candidates
    )
    kpis["under_target_margin_exposure_pct"] = under_target_margin_exposure_pct
    kpis["negative_margin_share_pct"] = negative_margin_share_pct
    kpis["recoverable_margin_uplift"] = recoverable_margin_uplift

    orders_all_sorted = orders_all_df.copy()
    repeat_share_pct = None
    repeat_revenue_share_pct = None
    recent_repeat_revenue_share_pct = None
    active_days = 0
    active_weeks = 0
    if not orders_all_sorted.empty:
        orders_all_sorted["order_date"] = pd.to_datetime(orders_all_sorted["order_date"], errors="coerce")
        orders_all_sorted = orders_all_sorted.sort_values("order_date")
        active_days = int(orders_all_sorted["order_date"].dt.date.nunique())
        try:
            active_weeks = int(orders_all_sorted["order_date"].dt.to_period("W").nunique())
        except Exception:
            active_weeks = 0
        total_order_count = int(len(orders_all_sorted.index))
        if total_order_count > 0:
            repeat_share_pct = _safe_ratio_value(max(total_order_count - 1, 0), total_order_count, pct=True)
            first_order_id = orders_all_sorted.iloc[0].get("order_id")
            first_order_revenue = _sum_numeric(
                orders_all_sorted.loc[orders_all_sorted["order_id"] == first_order_id, "revenue"]
            ) if first_order_id is not None else 0.0
            repeat_revenue_share_pct = _safe_ratio_value(
                max(kpis.get("total_revenue") or 0.0, 0.0) - first_order_revenue,
                kpis.get("total_revenue") or 0.0,
                pct=True,
            )
            if first_order_id is not None and "order_id" in orders_filt_df.columns:
                recent_repeat_revenue_share_pct = _safe_ratio_value(
                    _sum_numeric(orders_filt_df.loc[orders_filt_df["order_id"] != first_order_id, "revenue"]),
                    _sum_numeric(orders_filt_df.get("revenue", [])),
                    pct=True,
                )
    kpis["repeat_share_pct"] = repeat_share_pct
    kpis["repeat_revenue_share_pct"] = repeat_revenue_share_pct
    kpis["recent_repeat_revenue_share_pct"] = recent_repeat_revenue_share_pct
    kpis["active_days"] = active_days
    kpis["active_weeks"] = active_weeks

    monthly_all_points: List[Dict[str, Any]] = []
    if not monthly_all.empty:
        try:
            for rec in monthly_all.to_dict(orient="records"):
                month_ts = pd.to_datetime(_none_if_na(rec.get("month")), errors="coerce")
                if pd.isna(month_ts):
                    continue
                monthly_all_points.append(
                    {
                        "month": month_ts.to_period("M").to_timestamp(),
                        "revenue": _sum_numeric([rec.get("revenue")]),
                        "weight_lb": _sum_numeric([rec.get("weight_lb")]),
                    }
                )
        except Exception:
            monthly_all_points = []
    monthly_all_points.sort(key=lambda rec: rec.get("month"))
    monthly_all_labels = [str(rec.get("month").strftime("%Y-%m")) for rec in monthly_all_points if rec.get("month") is not None]
    active_streak_months = _active_streak_months(monthly_all_labels)
    cadence_variability_pct = None
    if cadence_days:
        cadence_variability_pct = _safe_ratio_value(_std_numeric(cadence_days), _mean_numeric(cadence_days), pct=True)

    seasonal_month_totals: Dict[int, float] = {idx: 0.0 for idx in range(1, 13)}
    seasonal_weight_totals: Dict[int, float] = {idx: 0.0 for idx in range(1, 13)}
    for rec in monthly_all_points:
        month_ts = rec.get("month")
        if isinstance(month_ts, pd.Timestamp):
            month_no = int(month_ts.month)
            seasonal_month_totals[month_no] += float(rec.get("revenue") or 0.0)
            seasonal_weight_totals[month_no] += float(rec.get("weight_lb") or 0.0)
    nonzero_month_values = [val for val in seasonal_month_totals.values() if val > 0]
    top3_month_share_pct = None
    seasonality_strength_score = None
    best_months: List[str] = []
    worst_months: List[str] = []
    if nonzero_month_values:
        ranked_months = sorted(seasonal_month_totals.items(), key=lambda item: item[1], reverse=True)
        top3_month_share_pct = _safe_ratio_value(sum(val for _, val in ranked_months[:3]), sum(seasonal_month_totals.values()), pct=True)
        seasonality_strength_score = _clamp_score(((top3_month_share_pct or 0.0) - 25.0) * 4.0)
        best_months = [
            datetime(2000, int(month_no), 1).strftime("%b")
            for month_no, value in ranked_months[:3]
            if value > 0
        ]
        worst_months = [
            datetime(2000, int(month_no), 1).strftime("%b")
            for month_no, value in sorted(seasonal_month_totals.items(), key=lambda item: item[1])[:3]
            if value > 0
        ]

    monthly_all_revenue_vals = [float(rec.get("revenue") or 0.0) for rec in monthly_all_points if float(rec.get("revenue") or 0.0) > 0]
    monthly_cv_pct = None
    if len(monthly_all_revenue_vals) >= 2:
        monthly_cv_pct = _safe_ratio_value(float(np.std(monthly_all_revenue_vals)), float(np.mean(monthly_all_revenue_vals)), pct=True)

    margin_gap_pct = max(float(margin_target_pct or 0.0) - float(kpis.get("margin_pct") or 0.0), 0.0) if kpis.get("margin_pct") is not None else None
    kpis["margin_gap_pct"] = margin_gap_pct
    if kpis.get("margin_pct") is not None:
        margin_status = margin_rules.classify_margin_status(
            kpis.get("margin_pct"),
            kpis.get("minimum_margin_pct"),
            kpis.get("target_margin_pct"),
        )
        kpis.update(margin_status)
    pricing_quality_score = None
    if deltas:
        pricing_quality_score = _clamp_score(
            100.0
            - min(
                100.0,
                (float(kpis.get("avg_abs_price_delta_pct") or 0.0) * 4.0)
                + (float(kpis.get("pricing_dispersion_below_peer_pct") or 0.0) * 0.5)
                + (float(kpis.get("pricing_dispersion_above_peer_pct") or 0.0) * 0.25),
            )
        )
    margin_quality_score = None
    if kpis.get("margin_pct") is not None:
        margin_quality_score = _clamp_score(
            100.0
            - min(
                100.0,
                (float(margin_gap_pct or 0.0) * 3.0)
                + (float(under_target_margin_exposure_pct or 0.0) * 0.8)
                + (float(negative_margin_share_pct or 0.0)),
            )
        )

    service_rhythm_score = _clamp_score(
        100.0
        - min(70.0, float(cadence_variability_pct or 0.0) * 0.8)
        - min(25.0, max(float(recency_days or 0.0) - float(kpis.get("cadence_p90_days") or 45.0), 0.0) * 0.5)
        + min(20.0, float(active_streak_months) * 2.0)
    )
    relationship_health_score = _clamp_score(
        30.0
        + (float(repeat_revenue_share_pct or 0.0) * 0.35)
        + min(20.0, float(active_streak_months) * 4.0)
        + (10.0 if action_orders_90 > 0 else 0.0)
        + (10.0 if float(kpis.get("cost_coverage_pct") or 0.0) >= 95.0 else 0.0)
        - min(50.0, max(float(recency_days or 0.0) - float(kpis.get("cadence_p90_days") or 45.0), 0.0) * 0.8)
    )
    churn_risk_score = _clamp_score(
        min(
            100.0,
            max(float(recency_days or 0.0) - float(kpis.get("cadence_median_days") or 30.0), 0.0) * 1.2
            + (25.0 if action_orders_90 == 0 else 0.0)
            + (15.0 if _series_direction(revenue_cur_window, revenue_prior_window, threshold_pct=10.0) == "declining" else 0.0),
        )
    )
    dependency_balance_score = _clamp_score(
        100.0
        - min(
            100.0,
            (float(top5_product_share_pct or 0.0) * 0.75)
            + (float(top5_weight_share_pct or 0.0) * 0.45)
            + (float(top_category_share_pct or 0.0) * 0.35),
        )
    )
    weight_importance_score = _clamp_score(
        min(55.0, math.log1p(max(total_weight_window, total_weight_lifetime, 0.0)) * 8.0)
        + max(float(kpis.get("window_weight_share_pct") or 0.0), float(kpis.get("weight_share_pct") or 0.0)) * 6.0
    )
    forecastability_score = _clamp_score(
        100.0
        - min(80.0, float(monthly_cv_pct or 0.0) * 0.6)
        + min(20.0, float(active_streak_months) * 3.0)
        + (float(seasonality_strength_score or 0.0) * 0.2)
    )
    growth_opportunity_score = _clamp_score(
        25.0
        + min(float(len(cross_sell)) * 10.0, 25.0)
        + min(float(len(lost_products)) * 5.0, 20.0)
        + min(float(len(declining_categories)) * 4.0, 12.0)
        + (12.0 if float(relationship_health_score or 0.0) >= 55.0 else 0.0)
        + (10.0 if float(margin_quality_score or 0.0) >= 55.0 else 0.0)
        - (20.0 if float(churn_risk_score or 0.0) >= 70.0 else 0.0)
    )

    risk_posture = "Stable"
    if float(churn_risk_score or 0.0) >= 75.0 or float(recency_days or 0.0) >= 90.0:
        risk_posture = "Churn Risk"
    elif float(churn_risk_score or 0.0) >= 55.0 or float(recency_days or 0.0) >= 60.0:
        risk_posture = "At Risk"

    if kpis.get("margin_pct") is None:
        profitability_posture = "Margin Unavailable"
    elif float(negative_margin_share_pct or 0.0) > 5.0:
        profitability_posture = "Negative Margin"
    elif float(under_target_margin_exposure_pct or 0.0) >= 40.0:
        profitability_posture = "Margin Leakage"
    elif float(kpis.get("margin_pct") or 0.0) >= (float(margin_target_pct or 0.0) + 5.0):
        profitability_posture = "Healthy Margin"
    else:
        profitability_posture = "Stable Margin"

    if float(top5_product_share_pct or 0.0) >= 75.0 or float(top5_weight_share_pct or 0.0) >= 75.0:
        dependency_posture = "Concentrated"
    elif float(top5_product_share_pct or 0.0) >= 55.0 or float(top_category_share_pct or 0.0) >= 55.0:
        dependency_posture = "Moderate"
    else:
        dependency_posture = "Diversified"

    share_signal = max(
        float(kpis.get("window_contribution_share_pct") or 0.0),
        float(kpis.get("contribution_share_pct") or 0.0),
        float(kpis.get("window_weight_share_pct") or 0.0),
    )
    if share_signal >= 5.0:
        commercial_tier = "Strategic"
    elif share_signal >= 2.0:
        commercial_tier = "Core"
    elif share_signal >= 0.75:
        commercial_tier = "Growth"
    else:
        commercial_tier = "Managed"

    if not kpis.get("owner_sales_rep"):
        coverage_posture = "Low Coverage"
    elif float(recency_days or 0.0) >= max(float(kpis.get("cadence_p90_days") or 90.0), 90.0):
        coverage_posture = "Low Coverage"
    elif action_orders_90 <= 1:
        coverage_posture = "Light Coverage"
    else:
        coverage_posture = "Covered"

    if float(kpis.get("cost_coverage_pct") or 0.0) >= 98.0 and float(repeat_revenue_share_pct or 0.0) >= 75.0 and float(kpis.get("months_active") or 0.0) >= 12.0:
        trust_posture = "Established"
    elif float(kpis.get("cost_coverage_pct") or 0.0) >= 90.0 and float(kpis.get("months_active") or 0.0) >= 3.0:
        trust_posture = "Developing"
    else:
        trust_posture = "Low Visibility"

    if recency_days is None:
        lifecycle_stage = "No Activity"
    elif float(recency_days) >= 180.0:
        lifecycle_stage = "Dormant"
    elif is_new_window or float(kpis.get("months_active") or 0.0) <= 2.0 or float(kpis.get("total_orders") or 0.0) <= 2.0:
        lifecycle_stage = "New"
    elif reactivated:
        lifecycle_stage = "Reactivated"
    elif float(recency_days) >= 90.0:
        lifecycle_stage = "At Risk"
    elif float(kpis.get("months_active") or 0.0) >= 12.0 and float(repeat_share_pct or 0.0) >= 70.0:
        lifecycle_stage = "Established"
    else:
        lifecycle_stage = "Developing"

    relationship_direction = _series_direction(revenue_cur_window, revenue_prior_window, threshold_pct=10.0)
    dormancy_risk = "Low"
    if float(recency_days or 0.0) >= max(float(kpis.get("cadence_p90_days") or 90.0), 90.0):
        dormancy_risk = "High"
    elif float(recency_days or 0.0) >= max(float(kpis.get("cadence_median_days") or 45.0), 45.0):
        dormancy_risk = "Medium"
    recovery_potential = "Low"
    if lost_products and float(relationship_health_score or 0.0) >= 45.0:
        recovery_potential = "High"
    elif declining_products or lost_categories:
        recovery_potential = "Medium"

    order_weight_values = sorted(_numeric_values(orders_all_df.get("weight_lb", [])), reverse=True)
    weight_volatility_pct = _safe_ratio_value(_std_numeric(order_weight_values), _mean_numeric(order_weight_values), pct=True) if order_weight_values else None
    heavy_bucket = max(1, int(math.ceil(len(order_weight_values) * 0.2))) if order_weight_values else 0
    heavy_order_concentration_pct = _safe_ratio_value(sum(order_weight_values[:heavy_bucket]), sum(order_weight_values), pct=True) if order_weight_values else None
    operational_consistency_score = _clamp_score(
        100.0
        - min(75.0, float(weight_volatility_pct or 0.0) * 0.7)
        - min(20.0, max(float(heavy_order_concentration_pct or 0.0) - 55.0, 0.0) * 0.6)
        + min(15.0, float(service_rhythm_score or 0.0) * 0.15)
    )

    if float(relationship_health_score or 0.0) >= 70.0 and commercial_tier in {"Strategic", "Core"}:
        narrative_lead = "High-value active account"
    elif risk_posture == "Churn Risk":
        narrative_lead = "Commercially meaningful relationship showing churn risk"
    else:
        narrative_lead = "Stable commercial account"
    narrative_parts = [narrative_lead]
    if float(repeat_revenue_share_pct or 0.0) >= 70.0:
        narrative_parts.append("with strong repeat behavior")
    elif float(repeat_revenue_share_pct or 0.0) >= 40.0:
        narrative_parts.append("with developing repeat behavior")
    if profitability_posture == "Margin Leakage" and low_margin_candidates:
        top_leak = low_margin_candidates[0]
        narrative_parts.append(
            f"but meaningful margin leakage in {top_leak.get('category') or top_leak.get('product')}"
        )
    elif dependency_posture == "Concentrated" and kpis.get("top_weight_product"):
        narrative_parts.append(f"with weight concentration around {kpis.get('top_weight_product')}")
    if cross_sell and float(growth_opportunity_score or 0.0) >= 60.0:
        narrative_parts.append(
            f"and whitespace in {cross_sell[0].get('category') or cross_sell[0].get('product')}"
        )
    workspace_narrative = ", ".join(narrative_parts).strip().rstrip(".") + "."
    last_sales_rep = kpis.get("last_sales_rep") or owner_name
    last_sales_rep_date = kpis.get("last_sales_rep_date") or kpis.get("last_order")
    geography_label = ", ".join(
        [
            part
            for part in [
                kpis.get("primary_city"),
                kpis.get("primary_state"),
                kpis.get("primary_region"),
            ]
            if str(part or "").strip()
        ]
    ) or "No geography signal"
    coverage_summary = f"{coverage_posture} coverage"
    if owner_name:
        coverage_summary = f"{coverage_posture} coverage with {owner_name}"
    trust_summary = f"{trust_posture} trust"
    if float(kpis.get("cost_coverage_pct") or 0.0) > 0:
        trust_summary = f"{trust_posture} trust and {float(kpis.get('cost_coverage_pct') or 0.0):.0f}% cost coverage"

    hero_badges: List[Dict[str, Any]] = []
    def _add_badge(label: str, tone: str) -> None:
        if label not in {badge.get("label") for badge in hero_badges}:
            hero_badges.append({"label": label, "tone": tone})

    if str(kpis.get("active_status") or "").lower() == "active":
        _add_badge("Active", "positive")
    elif str(kpis.get("active_status") or "").lower() in {"warm"}:
        _add_badge("Warm", "warning")
    if risk_posture == "Stable":
        _add_badge("Stable", "positive")
    elif risk_posture == "At Risk":
        _add_badge("At Risk", "warning")
    else:
        _add_badge("Churn Risk", "danger")
    if profitability_posture == "Margin Leakage":
        _add_badge("Margin Leakage", "danger")
    elif profitability_posture == "Healthy Margin":
        _add_badge("Healthy Margin", "positive")
    if dependency_posture == "Concentrated":
        _add_badge("Concentrated", "warning")
    if relationship_direction == "declining":
        _add_badge("Declining", "danger")
    if float(growth_opportunity_score or 0.0) >= 70.0:
        _add_badge("High Potential", "positive")
    elif cross_sell:
        _add_badge("Growth Opportunity", "info")
    if coverage_posture == "Low Coverage":
        _add_badge("Low Coverage", "warning")

    key_account_flag = commercial_tier in {"Strategic", "Core"} and share_signal >= 2.0
    top_weight_products = []
    for row in sorted(product_rows, key=lambda rec: rec.get("weight_lb", 0.0), reverse=True)[:10]:
        top_weight_products.append(
            {
                **row,
                "weight_share_pct": _safe_ratio_value(row.get("weight_lb"), current_product_weight_total, pct=True),
                "revenue_share_pct": _safe_ratio_value(row.get("revenue"), current_product_revenue_total, pct=True),
            }
        )
    top_revenue_products = []
    for row in product_sorted[:10]:
        top_revenue_products.append(
            {
                **row,
                "revenue_share_pct": _safe_ratio_value(row.get("revenue"), current_product_revenue_total, pct=True),
                "weight_share_pct": _safe_ratio_value(row.get("weight_lb"), current_product_weight_total, pct=True),
            }
        )
    top_profit_products = []
    for row in sorted(product_rows, key=lambda rec: rec.get("profit", 0.0), reverse=True)[:10]:
        top_profit_products.append(
            {
                **row,
                "profit_share_pct": _safe_ratio_value(row.get("profit"), current_product_profit_total, pct=True),
            }
        )
    top_category_rows = []
    for row in category_sorted[:10]:
        top_category_rows.append(
            {
                **row,
                "revenue_share_pct": _safe_ratio_value(row.get("revenue"), _sum_numeric([rec.get("revenue") for rec in category_rows]), pct=True),
                "weight_share_pct": _safe_ratio_value(row.get("weight_lb"), _sum_numeric([rec.get("weight_lb") for rec in category_rows]), pct=True),
            }
        )

    avg_peer_attach_revenue = None
    if cross_sell:
        best_cross_sell = sorted(
            cross_sell,
            key=lambda rec: (float(rec.get("lift") or 0.0), float(rec.get("confidence_pct") or 0.0)),
            reverse=True,
        )[0]
        avg_peer_attach_revenue = _safe_ratio_value(best_cross_sell.get("revenue"), best_cross_sell.get("co_orders"))
    else:
        best_cross_sell = None

    next_best_actions: List[Dict[str, Any]] = []
    if low_margin_candidates:
        top_leak = low_margin_candidates[0]
        top_leak_target_margin_pct = float(top_leak.get("target_margin_pct") or margin_target_pct or 0.0)
        profit_uplift = max(top_leak_target_margin_pct - float(top_leak.get("margin_pct") or 0.0), 0.0) / 100.0 * float(top_leak.get("revenue") or 0.0)
        next_best_actions.append(
            _action_item(
                lane="protect_now",
                action_type="recover_margin",
                title="Recover margin on core leakage SKU",
                why=(
                    f"{top_leak.get('product')} is running at {float(top_leak.get('margin_pct') or 0.0):.1f}% margin, "
                    f"below the {top_leak_target_margin_pct:.0f}% target."
                ),
                urgency="High",
                confidence=84.0,
                owner=owner_name,
                related_products=[top_leak.get("product")],
                related_categories=[top_leak.get("category")],
                related_families=[top_leak.get("protein_family")],
                revenue_upside=float(top_leak.get("revenue") or 0.0),
                profit_upside=profit_uplift,
                margin_upside_pp=max(top_leak_target_margin_pct - float(top_leak.get("margin_pct") or 0.0), 0.0),
                scope_label="Current filter window",
                pathway_label="Open product drilldown",
                tone="danger",
                priority_score=min(98.0, 75.0 + max(top_leak_target_margin_pct - float(top_leak.get("margin_pct") or 0.0), 0.0)),
            )
        )
    if (
        recency_days is not None
        and kpis.get("cadence_median_days") is not None
        and float(recency_days) > float(kpis.get("cadence_median_days") or 0.0)
    ):
        next_best_actions.append(
            _action_item(
                lane="protect_now" if float(recency_days or 0.0) >= 90.0 else "recover_now",
                action_type="increase_cadence",
                title="Re-engage to restore normal cadence",
                why=(
                    f"Last order was {int(recency_days)} days ago versus a median cadence of "
                    f"{float(kpis.get('cadence_median_days') or 0.0):.1f} days."
                ),
                urgency="High" if float(recency_days or 0.0) >= 90.0 else "Medium",
                confidence=78.0,
                owner=owner_name,
                revenue_upside=max(float(kpis.get("revenue_prior_window") or 0.0) - float(kpis.get("revenue_window") or 0.0), 0.0) or None,
                scope_label="Visible lifetime",
                pathway_label="Open order workspace",
                tone="warning",
                priority_score=min(96.0, 60.0 + float(churn_risk_score or 0.0) * 0.4),
            )
        )
    if best_cross_sell is not None:
        next_best_actions.append(
            _action_item(
                lane="grow_now",
                action_type="cross_sell_bundle",
                title="Attach the next most probable bundle",
                why=(
                    f"{best_cross_sell.get('product')} shows {float(best_cross_sell.get('confidence_pct') or 0.0):.1f}% confidence "
                    f"and lift {float(best_cross_sell.get('lift') or 0.0):.2f} in scoped peer orders."
                ),
                urgency="Medium",
                confidence=min(95.0, float(best_cross_sell.get("confidence_pct") or 0.0)),
                owner=owner_name,
                related_products=[best_cross_sell.get("product")],
                related_categories=[best_cross_sell.get("category")],
                related_families=[best_cross_sell.get("protein_family") or best_cross_sell.get("category")],
                revenue_upside=avg_peer_attach_revenue,
                scope_label="Peer comparison",
                pathway_label="Open product drilldown",
                tone="info",
                priority_score=min(92.0, 45.0 + float(best_cross_sell.get("confidence_pct") or 0.0) * 0.4 + float(best_cross_sell.get("lift") or 0.0) * 8.0),
            )
        )
    if lost_categories:
        top_lost_category = lost_categories[0]
        next_best_actions.append(
            _action_item(
                lane="recover_now",
                action_type="reactivate_family",
                title="Reactivate a lapsed product family",
                why=(
                    f"{top_lost_category.get('category')} generated {float(top_lost_category.get('revenue_prior') or 0.0):,.0f} in the prior window "
                    "but disappeared in the current window."
                ),
                urgency="Medium",
                confidence=72.0,
                owner=owner_name,
                related_categories=[top_lost_category.get("category")],
                related_families=[top_lost_category.get("category")],
                revenue_upside=float(top_lost_category.get("revenue_prior") or 0.0),
                scope_label="Current filter window",
                pathway_label="Open family workspace",
                tone="warning",
                priority_score=76.0,
            )
        )
    if dependency_posture == "Concentrated":
        next_best_actions.append(
            _action_item(
                lane="monitor",
                action_type="reduce_concentration_risk",
                title="Reduce product concentration risk",
                why=(
                    f"Top 5 products represent {float(top5_product_share_pct or 0.0):.1f}% of current-window revenue "
                    f"and {float(top5_weight_share_pct or 0.0):.1f}% of shipped weight."
                ),
                urgency="Medium",
                confidence=70.0,
                owner=owner_name,
                related_products=[rec.get("product") for rec in top_revenue_products[:3]],
                related_families=[rec.get("protein_family") for rec in top_revenue_products[:3]],
                scope_label="Current filter window",
                pathway_label="Open dependency workspace",
                tone="warning",
                priority_score=68.0,
            )
        )
    if float(kpis.get("cost_coverage_pct") or 0.0) < 95.0:
        next_best_actions.append(
            _action_item(
                lane="monitor",
                action_type="improve_cost_coverage",
                title="Close cost coverage gaps",
                why="Cost coverage is below 95%, which weakens margin and profitability diagnostics.",
                urgency="Medium",
                confidence=92.0,
                owner=owner_name,
                scope_label="Visible lifetime",
                pathway_label="Open scoped workspace",
                tone="neutral",
                priority_score=66.0,
            )
        )
    if float(kpis.get("best_weekday_share_pct") or 0.0) >= 30.0 and kpis.get("best_weekday"):
        next_best_actions.append(
            _action_item(
                lane="grow_now",
                action_type="align_weekday_offer",
                title="Align outreach to the dominant weekday rhythm",
                why=(
                    f"{kpis.get('best_weekday')} carries {float(kpis.get('best_weekday_share_pct') or 0.0):.1f}% "
                    "of lifetime revenue, suggesting a reliable ordering pattern."
                ),
                urgency="Low",
                confidence=68.0,
                owner=owner_name,
                scope_label="Visible lifetime",
                pathway_label="Open weekday workspace",
                tone="info",
                priority_score=58.0,
            )
        )
    next_best_actions.sort(key=lambda rec: float(rec.get("priority_score") or 0.0), reverse=True)

    crm_workspace = {
        "protect_now": [rec for rec in next_best_actions if rec.get("lane") == "protect_now"],
        "grow_now": [rec for rec in next_best_actions if rec.get("lane") == "grow_now"],
        "recover_now": [rec for rec in next_best_actions if rec.get("lane") == "recover_now"],
        "monitor": [rec for rec in next_best_actions if rec.get("lane") == "monitor"],
    }

    hero = {
        "customer_name": kpis.get("label"),
        "customer_id": customer_id,
        "relationship_status": kpis.get("active_status"),
        "lifecycle_stage": lifecycle_stage,
        "segment": kpis.get("segment"),
        "risk_posture": risk_posture,
        "commercial_tier": commercial_tier,
        "profitability_posture": profitability_posture,
        "dependency_posture": dependency_posture,
        "coverage_posture": coverage_posture,
        "trust_posture": trust_posture,
        "active_window": f"{window_start_iso} to {window_end_iso}",
        "snapshot_reference_date": activity_reference_iso,
        "last_order_date": kpis.get("last_order"),
        "first_order_date": kpis.get("first_order"),
        "key_account_flag": key_account_flag,
        "owner": owner_name,
        "owner_detail": owner_detail,
        "historical_owner": historical_owner_name,
        "last_sales_rep": last_sales_rep,
        "last_sales_rep_date": last_sales_rep_date,
        "region": kpis.get("primary_region"),
        "shipping_pattern": kpis.get("primary_shipping"),
        "geography": geography_label,
        "coverage_summary": coverage_summary,
        "trust_summary": trust_summary,
        "city": kpis.get("primary_city"),
        "state": kpis.get("primary_state"),
        "badges": hero_badges,
        "narrative": workspace_narrative,
    }

    executive_scorecard = [
        {
            "key": "commercial_value",
            "title": "Commercial Value",
            "subtitle": "Lifetime value, filtered-window importance, and contribution to visible scope.",
            "metrics": [
                _metric_item(
                    "Lifetime Revenue",
                    kpis.get("total_revenue"),
                    fmt="currency",
                    detail=f"{float(kpis.get('contribution_share_pct') or 0.0):.1f}% of visible lifetime revenue",
                    scope="Visible lifetime",
                    emphasize=True,
                ),
                _metric_item(
                    "Lifetime Profit",
                    kpis.get("total_profit"),
                    fmt="currency",
                    detail=f"{float(kpis.get('profit_share_pct') or 0.0):.1f}% profit share",
                    scope="Visible lifetime",
                ),
                _metric_item(
                    "Window Revenue",
                    kpis.get("revenue_window"),
                    fmt="currency",
                    detail=f"{float(kpis.get('window_contribution_share_pct') or 0.0):.1f}% of scoped current-window revenue",
                    scope="Current filter window",
                ),
                _metric_item(
                    "Contribution Window",
                    kpis.get("window_contribution_share_pct"),
                    fmt="percent",
                    detail=f"{customers_in_window} customers active in current window",
                    scope="Scoped share",
                ),
            ],
        },
        {
            "key": "relationship_strength",
            "title": "Relationship Strength",
            "subtitle": "Recency, cadence, tenure, and repeat depth.",
            "metrics": [
                _metric_item("Days Since Last Order", kpis.get("days_since_last_order"), fmt="number", detail=kpis.get("recency_band"), scope="Visible lifetime", emphasize=True),
                _metric_item("Cadence Median", kpis.get("cadence_median_days"), fmt="number", detail=f"P90 {float(kpis.get('cadence_p90_days') or 0.0):.1f} days", suffix="days", scope="Visible lifetime"),
                _metric_item("Repeat Revenue Share", repeat_revenue_share_pct, fmt="percent", detail=f"Recent repeat revenue {float(recent_repeat_revenue_share_pct or 0.0):.1f}%", scope="Visible lifetime"),
                _metric_item("Lifecycle Stage", lifecycle_stage, fmt="text", detail=f"Active streak {active_streak_months} months", scope="Visible lifetime"),
            ],
        },
        {
            "key": "pricing_margin",
            "title": "Pricing & Margin Health",
            "subtitle": "Margin quality, pricing dispersion, and recoverable leakage.",
            "metrics": [
                _metric_item(
                    "ASP/lb",
                    kpis.get("asp_lb"),
                    fmt="currency",
                    detail=f"Profit/lb {float(kpis.get('profit_lb') or 0.0):.2f}" if kpis.get("profit_lb") is not None else None,
                    scope="Current filter window",
                    emphasize=True,
                ),
                _metric_item("Margin %", kpis.get("margin_pct"), fmt="percent", detail=profitability_posture, scope="Current filter window"),
                _metric_item(
                    "Below-Target Exposure",
                    under_target_margin_exposure_pct,
                    fmt="percent",
                    detail=f"Recoverable uplift {recoverable_margin_uplift:,.0f}",
                    scope="Current filter window",
                ),
                _metric_item(
                    "Avg Abs Price Δ",
                    kpis.get("avg_abs_price_delta_pct"),
                    fmt="percent",
                    detail=f"Below-peer exposure {float(kpis.get('pricing_dispersion_below_peer_pct') or 0.0):.1f}%",
                    scope="Peer comparison",
                ),
            ],
        },
        {
            "key": "weight_operational",
            "title": "Weight & Operational Value",
            "subtitle": "Weight moved, value per lb, and operating consistency.",
            "metrics": [
                _metric_item(
                    "Window Shipped lb",
                    total_weight_window,
                    fmt="weight",
                    detail=f"{float(kpis.get('window_weight_share_pct') or 0.0):.1f}% of scoped current-window weight",
                    scope="Current filter window",
                    emphasize=True,
                ),
                _metric_item(
                    "Lifetime Shipped lb",
                    total_weight_lifetime,
                    fmt="weight",
                    detail=f"{float(kpis.get('weight_share_pct') or 0.0):.1f}% of scoped lifetime weight",
                    scope="Visible lifetime",
                ),
                _metric_item(
                    "Avg lb / Order",
                    kpis.get("avg_lb_per_order"),
                    fmt="weight",
                    detail=f"Lifetime {float(kpis.get('avg_lb_per_order_lifetime') or 0.0):.1f}",
                    scope="Current filter window",
                ),
                _metric_item(
                    "Operational Consistency",
                    operational_consistency_score,
                    fmt="score",
                    detail=f"Volatility {float(weight_volatility_pct or 0.0):.1f}%",
                    scope="Current filter window",
                ),
            ],
        },
        {
            "key": "growth_recovery",
            "title": "Growth & Recovery",
            "subtitle": "Whitespace, lost demand, and forecast readiness.",
            "metrics": [
                _metric_item("Growth Opportunity", growth_opportunity_score, fmt="score", detail=f"{len(cross_sell)} cross-sell ideas", scope="Current relationship", emphasize=True),
                _metric_item("Lost Products", len(lost_products), fmt="number", detail=f"{len(lost_categories)} lapsed families", scope="Visible lifetime"),
                _metric_item("Declining Families", len(declining_categories), fmt="number", detail=f"{len(declining_products)} declining products", scope="Current filter window"),
                _metric_item("Forecastability", forecastability_score, fmt="score", detail=f"Seasonality strength {float(seasonality_strength_score or 0.0):.1f}", scope="Visible lifetime"),
            ],
        },
    ]

    priority_engine = {
        "focus_area": (
            "Protect relationship" if risk_posture in {"At Risk", "Churn Risk"} else
            "Recover margin" if profitability_posture in {"Margin Leakage", "Negative Margin"} else
            "Grow wallet share" if float(growth_opportunity_score or 0.0) >= 65.0 else
            "Maintain and monitor"
        ),
        "narrative": workspace_narrative,
        "scores": [
            _score_item(
                "relationship_health_score",
                "Relationship Health",
                relationship_health_score,
                f"Recency {int(recency_days or 0)}d, repeat revenue {float(repeat_revenue_share_pct or 0.0):.1f}%, active streak {active_streak_months} months.",
                implication="Higher scores imply resilient repeat demand, stronger coverage, and less near-term relationship friction.",
            ),
            _score_item(
                "churn_risk_score",
                "Churn Risk",
                churn_risk_score,
                f"Orders last 90d: {action_orders_90}, cadence median {float(kpis.get('cadence_median_days') or 0.0):.1f} days.",
                implication="Higher scores imply leadership review and proactive recovery motion should happen immediately.",
                risk=True,
            ),
            _score_item(
                "growth_opportunity_score",
                "Growth Opportunity",
                growth_opportunity_score,
                f"{len(cross_sell)} cross-sell ideas, {len(lost_products)} lapsed products, {len(declining_categories)} declining families.",
                implication="Higher scores imply more whitespace, reactivation, or attach-rate upside is commercially available.",
            ),
            _score_item(
                "pricing_quality_score",
                "Pricing Quality",
                pricing_quality_score,
                f"Average absolute peer delta {float(kpis.get('avg_abs_price_delta_pct') or 0.0):.1f}%.",
                implication="Higher scores imply pricing is more disciplined versus the visible peer set.",
            ),
            _score_item(
                "margin_quality_score",
                "Margin Quality",
                margin_quality_score,
                f"Below-target exposure {float(under_target_margin_exposure_pct or 0.0):.1f}% with uplift potential {recoverable_margin_uplift:,.0f}.",
                implication="Higher scores imply less leakage and less urgency for margin recovery action.",
            ),
            _score_item(
                "weight_importance_score",
                "Weight Importance",
                weight_importance_score,
                f"{float(total_weight_window or 0.0):,.0f} lb in window and {float(kpis.get('window_weight_share_pct') or 0.0):.1f}% of scoped weight.",
                implication="Higher scores imply this account matters more to operations and supply planning.",
            ),
            _score_item(
                "dependency_balance_score",
                "Dependency Balance",
                dependency_balance_score,
                f"Top 5 product share {float(top5_product_share_pct or 0.0):.1f}% and top category share {float(top_category_share_pct or 0.0):.1f}%.",
                implication="Higher scores imply a more balanced mix with less concentration risk.",
            ),
            _score_item(
                "service_rhythm_score",
                "Service Rhythm",
                service_rhythm_score,
                f"Cadence variability {float(cadence_variability_pct or 0.0):.1f}% and weekday concentration {float(kpis.get('best_weekday_share_pct') or 0.0):.1f}%.",
                implication="Higher scores imply the account is easier to service and plan around operationally.",
            ),
            _score_item(
                "forecastability_score",
                "Forecastability",
                forecastability_score,
                f"Seasonality strength {float(seasonality_strength_score or 0.0):.1f} with monthly CV {float(monthly_cv_pct or 0.0):.1f}%.",
                implication="Higher scores imply demand is easier to forecast and inventory against.",
            ),
        ],
    }

    trend_summary = {
        "revenue_direction": relationship_direction,
        "profit_direction": _series_direction(current_product_profit_total, _sum_numeric([row.get("profit_prior") for row in product_rows]), threshold_pct=10.0),
        "weight_direction": _series_direction(total_weight_window, _sum_numeric([row.get("weight_prior") for row in category_rows]), threshold_pct=10.0),
        "rolling_revenue": monthly_revenue_rolling,
        "rolling_weight": monthly_weight_rolling,
        "revenue_per_lb": monthly_revenue_lb,
        "profit_per_lb": monthly_profit_lb,
        "current_best_month": best_months[0] if best_months else None,
    }

    lifecycle = {
        "stage": lifecycle_stage,
        "tenure_days": kpis.get("days_span"),
        "tenure_months": kpis.get("months_active"),
        "first_order": kpis.get("first_order"),
        "last_order": kpis.get("last_order"),
        "active_days": active_days,
        "active_weeks": active_weeks,
        "active_streak_months": active_streak_months,
        "repeat_share_pct": repeat_share_pct,
        "repeat_revenue_share_pct": repeat_revenue_share_pct,
        "recent_repeat_revenue_share_pct": recent_repeat_revenue_share_pct,
        "dormancy_risk": dormancy_risk,
        "relationship_direction": relationship_direction.title(),
        "recovery_potential": recovery_potential,
        "lost_product_count": len(lost_products),
        "declining_product_count": len(declining_products),
        "lost_family_count": len(lost_categories),
        "declining_family_count": len(declining_categories),
    }

    weight_analytics = {
        "summary": {
            "total_weight_lb_window": total_weight_window,
            "total_weight_lb_lifetime": total_weight_lifetime,
            "revenue_per_lb_window": kpis.get("asp_lb"),
            "profit_per_lb_window": kpis.get("profit_lb"),
            "avg_lb_per_order": kpis.get("avg_lb_per_order"),
            "avg_lb_per_week": kpis.get("avg_lb_per_week"),
            "avg_lb_per_month": kpis.get("avg_lb_per_month"),
            "weight_share_window_pct": kpis.get("window_weight_share_pct"),
            "weight_share_lifetime_pct": kpis.get("weight_share_pct"),
            "weight_volatility_pct": weight_volatility_pct,
            "heavy_order_concentration_pct": heavy_order_concentration_pct,
            "operational_consistency_score": operational_consistency_score,
        },
        "trend": {
            "labels": months,
            "weight_lb": monthly_weight,
            "weight_lb_rolling": monthly_weight_rolling,
            "revenue_per_lb": monthly_revenue_lb,
            "profit_per_lb": monthly_profit_lb,
        },
        "weekday": {
            "labels": weekday_labels,
            "revenue": weekday_revenue,
            "weight_lb": weekday_weight,
            "orders": weekday_orders,
        },
        "top_products": top_weight_products,
        "top_categories": [
            {
                **row,
                "weight_share_pct": _safe_ratio_value(row.get("weight_lb"), _sum_numeric([rec.get("weight_lb") for rec in category_rows]), pct=True),
                "revenue_share_pct": _safe_ratio_value(row.get("revenue"), _sum_numeric([rec.get("revenue") for rec in category_rows]), pct=True),
            }
            for row in category_weight_sorted[:10]
        ],
        "narrative": (
            f"Current filter-window shipments total {float(total_weight_window or 0.0):,.0f} lb with "
            f"{float(heavy_order_concentration_pct or 0.0):.1f}% concentrated in the heaviest 20% of orders."
        ),
    }

    protein_mix_rows = [
        {
            **row,
            "family": row.get("family") or "Unassigned",
            "revenue_share_pct": _safe_ratio_value(row.get("revenue"), protein_revenue_total, pct=True),
            "weight_share_pct": _safe_ratio_value(row.get("weight_lb"), protein_weight_total, pct=True),
        }
        for row in protein_sorted[:10]
    ]
    protein_margin_watch = [
        {
            **row,
            "family": row.get("family") or "Unassigned",
            "revenue_share_pct": _safe_ratio_value(row.get("revenue"), protein_revenue_total, pct=True),
        }
        for row in sorted(
            [row for row in protein_rows if row.get("revenue") or row.get("revenue_prior")],
            key=lambda r: (float(r.get("margin_pct") if r.get("margin_pct") is not None else 999999.0), -(r.get("revenue", 0.0) or 0.0)),
        )[:10]
    ]
    protein_whitespace = [
        {
            **row,
            "family": row.get("family") or row.get("protein_family") or row.get("category") or "Unassigned",
        }
        for row in (
            lost_proteins[:6]
            + [
                row
                for row in cross_sell[:10]
                if (row.get("protein_family") or row.get("category")) not in {rec.get("family") for rec in lost_proteins[:6]}
            ][:4]
        )
    ]
    top_protein = protein_mix_rows[0] if protein_mix_rows else None
    fastest_growing_family = next((row for row in protein_mix_rows if (row.get("delta_revenue_pct") or 0.0) > 0), None)
    protein_intelligence = {
        "summary": {
            "top_family": (top_protein or {}).get("family"),
            "top_family_share_pct": (top_protein or {}).get("revenue_share_pct"),
            "top5_family_share_pct": top5_protein_share_pct,
            "family_count": len(protein_rows),
            "dependency_posture": dependency_posture,
            "dependency_balance_score": dependency_balance_score,
            "fastest_growing_family": (fastest_growing_family or {}).get("family"),
            "fastest_growing_family_delta_pct": (fastest_growing_family or {}).get("delta_revenue_pct"),
        },
        "mix": protein_mix_rows,
        "margin_watch": protein_margin_watch,
        "whitespace": protein_whitespace,
        "narrative": (
            f"{((top_protein or {}).get('family') or 'Top protein family')} represents "
            f"{float((top_protein or {}).get('revenue_share_pct') or 0.0):.1f}% of current-window revenue. "
            f"{len(protein_whitespace)} whitespace cues and {len(protein_margin_watch)} margin-watch families are visible in scope."
        ),
    }

    product_intelligence = {
        "top_revenue_products": top_revenue_products,
        "top_profit_products": top_profit_products,
        "top_weight_products": top_weight_products,
        "top_categories": top_category_rows,
        "lost_products": lost_products[:10],
        "declining_products": declining_products[:10],
        "fast_growing_products": fast_growing_products[:10],
        "margin_leakage_products": low_margin_candidates[:10],
        "whitespace_recommendations": cross_sell[:10],
        "concentration_score": dependency_balance_score,
        "narrative": (
            f"Top 5 products account for {float(top5_product_share_pct or 0.0):.1f}% of current filter-window revenue. "
            f"{len(lost_products)} products and {len(lost_categories)} families require recovery review."
        ),
    }

    price_watchlist = sorted(price_table, key=lambda rec: abs(float(rec.get("delta_pct") or 0.0)), reverse=True)
    pricing_intelligence = {
        "summary": {
            "price_quality_score": pricing_quality_score,
            "margin_quality_score": margin_quality_score,
            "under_target_margin_exposure_pct": under_target_margin_exposure_pct,
            "negative_margin_share_pct": negative_margin_share_pct,
            "recoverable_margin_uplift": recoverable_margin_uplift,
            "avg_abs_price_delta_pct": kpis.get("avg_abs_price_delta_pct"),
            "pricing_dispersion_above_peer_pct": kpis.get("pricing_dispersion_above_peer_pct"),
            "pricing_dispersion_below_peer_pct": kpis.get("pricing_dispersion_below_peer_pct"),
        },
        "price_watchlist": price_watchlist[:10],
        "low_margin_watchlist": low_margin_candidates[:10],
        "negative_margin_watchlist": negative_margin_candidates[:10],
        "narrative": (
            f"{float(under_target_margin_exposure_pct or 0.0):.1f}% of current filter-window revenue sits below the "
            f"{float(margin_target_pct or 0.0):.0f}% weighted protein-aware target margin."
        ),
    }

    ordering_rhythm = {
        "weekday_preference": kpis.get("best_weekday"),
        "weekday_share_pct": kpis.get("best_weekday_share_pct"),
        "weight_weekday_preference": kpis.get("best_weight_weekday"),
        "weight_weekday_share_pct": kpis.get("best_weight_weekday_share_pct"),
        "cadence_median_days": kpis.get("cadence_median_days"),
        "cadence_p90_days": kpis.get("cadence_p90_days"),
        "cadence_variability_pct": cadence_variability_pct,
        "service_rhythm_score": service_rhythm_score,
        "seasonality_strength_score": seasonality_strength_score,
        "best_months": best_months,
        "worst_months": worst_months,
        "narrative": (
            f"Orders cluster around {kpis.get('best_weekday') or 'the observed weekday mix'} with a "
            f"median cadence of {float(kpis.get('cadence_median_days') or 0.0):.1f} days."
        ),
    }

    concentration = {
        "top_product_share_pct": top_product_share_pct,
        "top5_product_share_pct": top5_product_share_pct,
        "top_weight_share_pct": top_weight_share_pct,
        "top5_weight_share_pct": top5_weight_share_pct,
        "top_category_share_pct": top_category_share_pct,
        "top5_category_share_pct": top5_category_share_pct,
        "dependency_posture": dependency_posture,
        "dependency_balance_score": dependency_balance_score,
        "narrative": (
            f"Top 5 products drive {float(top5_product_share_pct or 0.0):.1f}% of revenue and "
            f"{float(top5_weight_share_pct or 0.0):.1f}% of weight."
        ),
    }

    trust_coverage = {
        "owner": owner_name,
        "owner_source": owner_source_label,
        "historical_owner": historical_owner_name,
        "last_sales_rep": last_sales_rep,
        "last_sales_rep_date": last_sales_rep_date,
        "coverage_posture": coverage_posture,
        "trust_posture": trust_posture,
        "cost_coverage_pct": kpis.get("cost_coverage_pct"),
        "customers_in_scope": customers_in_scope,
        "customers_in_window": customers_in_window,
        "orders_in_scope_window": orders_in_scope_window,
        "window_reference_date": activity_reference_iso,
        "scope_note": "Window metrics reflect the active filter window and snapshot date. Lifetime metrics reflect visible history within the current RBAC and non-date filter scope.",
    }

    trend_signal_points = sum(
        1
        for revenue_v, orders_v in zip(monthly_revenue, monthly_orders)
        if _clean_float(revenue_v) > 0.0 or _safe_int(orders_v) > 0
    )
    weight_signal_points = _count_positive(monthly_weight)
    weekday_signal_points = (
        _count_positive(weekday_revenue)
        + _count_positive(weekday_weight)
        + _count_positive(weekday_orders)
    )
    seasonality_active_cells = sum(1 for row in yoy_matrix for value in row if _clean_float(value) > 0.0)
    seasonality_active_months = sum(1 for value in seasonal_month_totals.values() if _clean_float(value) > 0.0)
    top_mix_ready = bool(product_rows and current_product_revenue_total > 0.0)
    chart_states = {
        "trend": (
            _chart_state(
                "empty",
                "Trend data is unavailable for the current filter window.",
                scope="Current filter window",
            )
            if trend_build_error or trend_signal_points <= 0
            else _chart_state(
                "limited",
                "Only one active month is visible in the current filter window, so trend direction is provisional.",
                scope="Current filter window",
            )
            if trend_signal_points == 1
            else _chart_state("ready", scope="Current filter window")
        ),
        "weight_value": (
            _chart_state(
                "empty",
                "No weight-bearing rows are available in the current filter window.",
                scope="Current filter window",
            )
            if weight_signal_points <= 0
            else _chart_state(
                "limited",
                "Only one weight-bearing month is visible in the current filter window.",
                scope="Current filter window",
            )
            if weight_signal_points == 1
            else _chart_state("ready", scope="Current filter window")
        ),
        "weekday": (
            _chart_state(
                "empty",
                "This customer has insufficient weekday activity in the visible history.",
                scope="Visible lifetime",
            )
            if weekday_signal_points <= 0
            else _chart_state("ready", scope="Visible lifetime")
        ),
        "seasonality": (
            _chart_state(
                "empty",
                "Not enough history to render seasonality reliably.",
                scope="Visible lifetime",
            )
            if seasonality_build_error or seasonality_active_cells < 3 or seasonality_active_months < 2
            else _chart_state(
                "limited",
                "Seasonality is directional only because the visible history is still thin.",
                scope="Visible lifetime",
            )
            if seasonality_active_cells < 6 or len(yoy_years) < 2
            else _chart_state("ready", scope="Visible lifetime")
        ),
        "top_mix": (
            _chart_state(
                "empty",
                "No product mix is available in the current filter window.",
                scope="Current filter window",
            )
            if not top_mix_ready
            else _chart_state("ready", scope="Current filter window")
        ),
    }

    kpis["relationship_health_score"] = relationship_health_score
    kpis["churn_risk_score"] = churn_risk_score
    kpis["growth_opportunity_score"] = growth_opportunity_score
    kpis["pricing_quality_score"] = pricing_quality_score
    kpis["margin_quality_score"] = margin_quality_score
    kpis["weight_importance_score"] = weight_importance_score
    kpis["dependency_balance_score"] = dependency_balance_score
    kpis["service_rhythm_score"] = service_rhythm_score
    kpis["forecastability_score"] = forecastability_score
    kpis["commercial_tier"] = commercial_tier
    kpis["lifecycle_stage"] = lifecycle_stage
    kpis["risk_posture"] = risk_posture
    kpis["profitability_posture"] = profitability_posture
    kpis["dependency_posture"] = dependency_posture
    kpis["coverage_posture"] = coverage_posture
    kpis["trust_posture"] = trust_posture

    payload = {
        "kpis": kpis,
        "trend": {
            "labels": months,
            "revenue": monthly_revenue,
            "orders": monthly_orders,
            "weight_lb": monthly_weight,
            "profit": monthly_profit,
            "cost": monthly_cost,
            "margin_pct": monthly_margin,
            "revenue_per_lb": monthly_revenue_lb,
            "profit_per_lb": monthly_profit_lb,
            "rolling_revenue": monthly_revenue_rolling,
            "rolling_weight": monthly_weight_rolling,
            "previous_year_revenue": monthly_previous_year,
        },
        "charts": {
            "trend": {"labels": months, "revenue": monthly_revenue, "qty": monthly_orders},
            "weight": {"labels": months, "weight_lb": monthly_weight, "revenue_per_lb": monthly_revenue_lb, "profit_per_lb": monthly_profit_lb},
        },
        "table": {
            "rows": product_rows,
            "page": 1,
            "page_size": len(product_rows) or 25,
            "total": len(product_rows),
            "total_rows": len(product_rows),
            "sort_by": "profit",
            "sort_dir": "desc",
        },
        "top_spend": product_spend_top,
        "top_spend_full": product_spend_full,
        "top_weight": product_weight_top,
        "top_weight_full": product_weight_full,
        "categories": category_rows,
        "weekday": {"labels": weekday_labels, "revenue": weekday_revenue, "weight_lb": weekday_weight, "orders": weekday_orders},
        "cadence": {"days": cadence_days, "median_days": kpis.get("cadence_median_days"), "p90_days": kpis.get("cadence_p90_days")},
        "seasonality": {"months": yoy_months, "years": yoy_years, "matrix": yoy_matrix},
        "profit_mix": product_profit_chart,
        "basket": basket_stats,
        "price_table": price_table,
        "cross_sell": cross_sell,
        "cross_sell_ideas": cross_sell,
        "orders": orders_export_rows,
        "next_best_actions": next_best_actions,
        "hero": hero,
        "executive_scorecard": executive_scorecard,
        "priority_engine": priority_engine,
        "trend_summary": trend_summary,
        "lifecycle": lifecycle,
        "weight_analytics": weight_analytics,
        "product_intelligence": product_intelligence,
        "protein_intelligence": protein_intelligence,
        "pricing_intelligence": pricing_intelligence,
        "ordering_rhythm": ordering_rhythm,
        "concentration": concentration,
        "crm_workspace": crm_workspace,
        "trust_coverage": trust_coverage,
        "chart_states": chart_states,
        "top_product": top_product,
        "top_weight_mover": top_weight_mover,
        "top_profit_product": top_profit_product,
        "pricing_spread": pricing_spread,
        "orders_last_30d": kpis.get("orders_last_30"),
        "orders_last_90d": kpis.get("orders_last_90"),
        "days_since_last_order": kpis.get("days_since_last_order"),
        "profile": {
            "first_order": kpis.get("first_order"),
            "last_order": kpis.get("last_order"),
            "months_active": kpis.get("months_active"),
            "days_span": kpis.get("days_span"),
            "weeks_active": kpis.get("weeks_active"),
            "primary_region": kpis.get("primary_region"),
            "primary_shipping": kpis.get("primary_shipping"),
            "owner_sales_rep": kpis.get("owner_sales_rep"),
            "historical_owner_sales_rep": kpis.get("historical_owner_sales_rep"),
            "assigned_owner_sales_rep": kpis.get("assigned_owner_sales_rep"),
            "dominant_seller_sales_rep": kpis.get("dominant_seller_sales_rep"),
            "last_sales_rep": kpis.get("last_sales_rep"),
            "last_sales_rep_date": kpis.get("last_sales_rep_date"),
            "primary_city": kpis.get("primary_city"),
            "primary_state": kpis.get("primary_state"),
            "unique_products": kpis.get("unique_products"),
            "unique_categories": kpis.get("unique_categories"),
            "segment": kpis.get("segment"),
            "segment_reason": kpis.get("segment_reason"),
            "segment_source": kpis.get("segment_source"),
            "active_status": kpis.get("active_status"),
            "recency_band": kpis.get("recency_band"),
            "commercial_tier": commercial_tier,
            "lifecycle_stage": lifecycle_stage,
        },
        "meta": {
            "page_id": "customer_drilldown",
            "entity_id": customer_id,
            "entity_label": kpis.get("label"),
            "cached": False,
            "query_ms": int((time.perf_counter() - started) * 1000),
            "generated_at": _utc_now_iso(),
            "window_start": window_start_iso,
            "window_end": window_end_iso,
            "window_reference_date": activity_reference_iso,
            "prior_window_start": prior_start_iso,
            "prior_window_end": prior_end_iso,
            "dataset_version": str(fact_store.cache_buster()),
            "scope": {
                "is_admin": bool((scope or {}).get("is_admin")),
                "scope_mode": (scope or {}).get("scope_mode"),
                "allowed_count": (scope or {}).get("allowed_count"),
                "scope_hash": (scope or {}).get("scope_hash"),
            },
            "drilldown_v2": bool(drilldown_v2),
            "export_all": bool(drilldown_export_all),
        },
    }
    try:
        query_ms = int(((payload.get("meta") or {}).get("query_ms")) or 0)
        slow_threshold_ms = int(current_app.config.get("CUSTOMERS_DRILLDOWN_SLOW_MS", 1500))
        log_name = "customers.drilldown.slow" if query_ms >= slow_threshold_ms else "customers.drilldown.build"
        log_method = current_app.logger.warning if query_ms >= slow_threshold_ms else current_app.logger.info
        log_method(
            log_name,
            extra={
                "customer_id": customer_id,
                "query_ms": query_ms,
                "window_start": window_start_iso,
                "window_end": window_end_iso,
                "export_all": bool(drilldown_export_all),
                "drilldown_v2": bool(drilldown_v2),
                "product_rows": len(product_rows),
                "category_rows": len(category_rows),
                "cross_sell_rows": len(cross_sell),
            },
        )
    except Exception:
        pass
    return payload
