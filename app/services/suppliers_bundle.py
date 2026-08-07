from __future__ import annotations

import copy
from calendar import monthrange
from dataclasses import replace as dc_replace
import hashlib
import json
import math
import os
import time
from datetime import date, datetime as datetime, timedelta
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import pandas as pd
from flask import current_app

from app.core.cache_manager import TTLValueCache
from app.services import fact_schema as fs
from app.services import fact_store
from app.services import filters_service
from app.services import margin_rules
from app.services import presentation

TOP_N_DEFAULT = 15
TOP_N_MAX = 5000
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 50000
SUPPLIER_PRODUCTS_EXPORT_MAX_ROWS = int(os.getenv("SUPPLIER_PRODUCTS_EXPORT_MAX_ROWS", "250000"))
SUPPLIER_PRODUCTS_EXPORT_CHUNK_SIZE = int(os.getenv("SUPPLIER_PRODUCTS_EXPORT_CHUNK_SIZE", "10000"))

KPI_CHART_TTL_SECONDS = int(os.getenv("SUPPLIERS_KPI_CHART_TTL_SECONDS", "900"))
TABLE_TTL_SECONDS = int(os.getenv("SUPPLIERS_TABLE_TTL_SECONDS", "300"))
DRILLDOWN_TTL_SECONDS = int(os.getenv("SUPPLIERS_DRILLDOWN_TTL_SECONDS", "600"))

_KPI_CHART_CACHE = TTLValueCache(maxsize=256)
_TABLE_CACHE = TTLValueCache(maxsize=512)
_DRILLDOWN_CACHE = TTLValueCache(maxsize=256)
_ANALYTICS_CACHE = TTLValueCache(maxsize=256)


class SupplierProductsExportLimitError(ValueError):
    def __init__(self, *, row_count: int, max_rows: int) -> None:
        self.row_count = int(row_count)
        self.max_rows = int(max_rows)
        super().__init__(
            f"Export is too large ({self.row_count:,} rows). "
            f"Please narrow filters to {self.max_rows:,} rows or fewer."
        )

DATE_CANDIDATES: tuple[str, ...] = (
    "DateShipped_line",
    "DateShipped_order",
    "ShipDate",
    "DateExpected_line",
    "DateExpected_order",
    "DateOrdered_line",
    "DateOrdered_order",
    fs.CANON.date,
    "Date",
)

SUPPLIER_ID_CANDIDATES: tuple[str, ...] = (
    fs.CANON.supplier_id,
    "SupplierId",
    "SupplierID",
    "Supplier",
)
SUPPLIER_NAME_CANDIDATES: tuple[str, ...] = (
    fs.CANON.supplier_name,
    "SupplierName",
    "Name as SupplierName",
    "Supplier",
    "ShortName",
)

REVENUE_CANDIDATES: tuple[str, ...] = (
    fs.CANON.revenue,
    "Revenue",
    "revenue_shipped",
    "revenue_ordered",
    "LinesTotalPrice",
    "OrderTotalPrice",
    "TotalRevenue",
)

COST_TOTAL_CANDIDATES: tuple[str, ...] = (
    fs.CANON.cost,
    "Cost",
    "ExtCost",
    "ExtendedCost",
    "COGS",
    "TotalCost",
    "LineCost",
    "cost_shipped",
    "cost_ordered",
    "pack_cost_total",
)

COST_PER_UNIT_CANDIDATES: tuple[str, ...] = (
    "CostPerUnit",
    "CostPrice",
    "CostPrice_orderline",
    "CostPrice_po",
    "CostPrice_product",
    "CostPrice_x",
    "CostPrice_y",
    "unit_cost_effective",
    "pack_unit_cost_effective",
    "UnitCost",
)

COST_PER_LB_CANDIDATES: tuple[str, ...] = (
    "CostPerLb",
    "CostPerLB",
    "CostPerPound",
    "CostPerPounds",
    "AvgCostPerLb",
    "AvgCostPerLB",
    "avg_cost_per_lb",
    "AverageCostPerLb",
    "LandedCostPerLb",
    "LandedCostPerLB",
)

QTY_CANDIDATES: tuple[str, ...] = (
    "ShippedItems",
    fs.CANON.qty_units,
    "qty_units",
    "QuantityShipped",
    "QuantityOrdered",
    "Qty",
    "Quantity",
    "Units",
    "ItemCount",
    "pack_item_count_sum",
)

WEIGHT_CANDIDATES: tuple[str, ...] = (
    fs.CANON.weight_lb,
    "WeightLb",
    "Weight",
    "weight_lb",
    "pack_weight_lb_sum",
)

ORDER_ID_CANDIDATES: tuple[str, ...] = (
    fs.CANON.order_id,
    "OrderId",
    "OrderID",
)

PRODUCT_ID_CANDIDATES: tuple[str, ...] = (
    fs.CANON.product_id,
    "ProductId",
    "ProductID",
    "SKU",
)
PRODUCT_NAME_CANDIDATES: tuple[str, ...] = (
    fs.CANON.product_name,
    "ProductName",
    "Name as ProductName",
    "ProductDescription",
    "Description",
    "SkuName",
    "SKUName",
)

CUSTOMER_ID_CANDIDATES: tuple[str, ...] = (
    fs.CANON.customer_id,
    "CustomerId",
    "CustomerID",
)
CUSTOMER_NAME_CANDIDATES: tuple[str, ...] = (
    fs.CANON.customer_name,
    "CustomerName",
    "Name as CustomerName",
)

REGION_CANDIDATES: tuple[str, ...] = (
    fs.CANON.region,
    "RegionName",
    "Region",
    "RegionId",
)

SHIP_METHOD_CANDIDATES: tuple[str, ...] = (
    fs.CANON.ship_method,
    "ShippingMethodName",
    "ShippingMethod",
    "ShipMethodName",
    "ShipperName",
)

SALES_REP_CANDIDATES: tuple[str, ...] = (
    fs.CANON.sales_rep,
    "SalesRepName",
    "PrimarySalesRepName",
    "SalesRep",
    "SalesRepId",
    "SalesRepID",
)

PROTEIN_CANDIDATES: tuple[str, ...] = (
    "Protein",
    "ProteinType",
    "ProteinName",
    "Category",
    "ProductCategory",
)

CATEGORY_CANDIDATES: tuple[str, ...] = (
    "Category",
    "ProductCategory",
    "Protein",
    "ProteinType",
    "ProteinName",
)


def _hash_payload(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _scope_summary(scope: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "role": scope.get("role"),
        "user_id": scope.get("user_id"),
        "scope_mode": scope.get("scope_mode"),
        "scope_hash": scope.get("scope_hash"),
        "permissions_version": scope.get("permissions_version"),
    }


def _cache_key(
    kind: str,
    dataset_version: str,
    scope_summary: Mapping[str, Any],
    filter_hash: str,
    extras: Mapping[str, Any] | None = None,
) -> str:
    payload = {
        "kind": kind,
        "dataset_version": dataset_version,
        "scope": dict(scope_summary),
        "filter_hash": filter_hash,
        "extras": dict(extras or {}),
    }
    return _hash_payload(payload)


def _clone(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def _norm_col(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _safe_col(cols: set[str], *candidates: str) -> str | None:
    if not cols:
        return None
    lower_map = {str(c).lower(): c for c in cols}
    norm_map = {_norm_col(str(c)): c for c in cols}
    for cand in candidates:
        if cand and cand in cols:
            return cand
        key = str(cand).lower()
        if key in lower_map:
            return lower_map[key]
        norm_key = _norm_col(str(cand))
        if norm_key in norm_map:
            return norm_map[norm_key]
    return None


def _quote(col: str) -> str:
    return fact_store.quote_identifier(col)


def _to_list(val: Any) -> list:
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, tuple):
        return list(val)
    try:
        import numpy as np  # type: ignore

        if isinstance(val, np.ndarray):
            return val.tolist()
    except Exception:
        pass
    return [val]


def _struct_list(val: Any) -> list[dict]:
    out: list[dict] = []
    for item in _to_list(val):
        if item is None:
            continue
        if isinstance(item, dict):
            out.append(item)
            continue
        try:
            out.append(dict(item))
            continue
        except Exception:
            pass
        try:
            out.append(item._asdict())  # type: ignore[attr-defined]
            continue
        except Exception:
            pass
        out.append({})
    return out


def _clean_float(val: Any, default: float = 0.0) -> float:
    try:
        fval = float(val)
        if math.isnan(fval):
            return default
        return fval
    except Exception:
        return default


def _clean_optional_float(val: Any) -> float | None:
    try:
        if val is None:
            return None
        fval = float(val)
        if math.isnan(fval):
            return None
        return fval
    except Exception:
        return None


def _clean_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except Exception:
        return default


def _date_to_iso(val: Any) -> str | None:
    if val in (None, ""):
        return None
    try:
        ts = pd.to_datetime(val, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.date().isoformat()
    except Exception:
        try:
            return val.date().isoformat()  # type: ignore[attr-defined]
        except Exception:
            try:
                return val.isoformat()  # type: ignore[attr-defined]
            except Exception:
                return str(val)


def _execute_sql_df_with_fallback(sql: str, params: Sequence[Any], *, tag: str) -> pd.DataFrame:
    """
    DuckDB -> pandas conversion can intermittently raise `_NoValueType` errors
    under certain test/runtime stacks. Fallback to fetchall() in that case.
    """
    try:
        return fact_store.execute_sql_df(sql, params, tag=tag)
    except TypeError as exc:
        if "_NoValueType" not in str(exc):
            raise
        conn = fact_store.get_conn()
        cursor = conn.execute(sql, list(params))
        cols = [meta[0] for meta in (cursor.description or [])]
        rows = cursor.fetchall()
        return pd.DataFrame(rows, columns=cols)


def _parse_top_n(args: Any, default: int = TOP_N_DEFAULT) -> int:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    try:
        raw = getter("topN") or getter("top_n") or getter("top") or default
        n = int(raw)
        if n < 0:
            return default
        return min(n, TOP_N_MAX)
    except Exception:
        return default


def _parse_bool_arg(args: Any, keys: Sequence[str], default: bool = False) -> bool:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    for key in keys:
        raw = getter(key)
        if raw is None:
            continue
        if isinstance(raw, bool):
            return raw
        token = str(raw).strip().lower()
        if token in {"1", "true", "yes", "on", "y"}:
            return True
        if token in {"0", "false", "no", "off", "n"}:
            return False
    return default


def _suppliers_v2_requested(args: Any) -> bool:
    return _parse_bool_arg(args, ("suppliers_v2", "v2"), default=False)


def _args_getlist(args: Any, key: str) -> list[str]:
    if hasattr(args, "getlist"):
        try:
            vals = args.getlist(key)
            return [str(v).strip() for v in vals if str(v).strip()]
        except Exception:
            pass
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    raw = getter(key)
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        out: list[str] = []
        for item in raw:
            token = str(item).strip()
            if token:
                out.append(token)
        return out
    token = str(raw).strip()
    if not token:
        return []
    if "," in token:
        return [part.strip() for part in token.split(",") if part.strip()]
    return [token]


def _normalize_segment_token(value: Any) -> str:
    token = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "marginrisk": "margin_risk",
        "datarisk": "data_risk",
        "longtail": "long_tail",
        "all_suppliers": "all",
        "all": "all",
    }
    return aliases.get(token, token)


def _parse_segment_filters(args: Any) -> list[str]:
    values = _args_getlist(args, "segment")
    values.extend(_args_getlist(args, "segments"))
    seen: set[str] = set()
    out: list[str] = []
    for val in values:
        token = _normalize_segment_token(val)
        if not token or token == "all" or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _parse_quick_filter(args: Any) -> str:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    raw = (
        getter("quick_filter")
        or getter("quick_filters")
        or getter("segment_quick")
        or getter("segment")
        or "all"
    )
    token = _normalize_segment_token(raw)
    return token or "all"


def _coerce_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        parsed = pd.to_datetime(value, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.date()
    except Exception:
        return None


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _month_end(value: date) -> date:
    return value.replace(day=monthrange(value.year, value.month)[1])


def _shift_months(value: date, months: int) -> date:
    month_idx = (value.month - 1) + int(months)
    year = value.year + (month_idx // 12)
    month = (month_idx % 12) + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


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
            "history_start": None,
            "current_days": 0,
            "prior_days": 0,
            "terminal_period_incomplete": False,
            "is_partial_period": False,
            "current_label": "Current filtered window",
            "prior_label": "Prior comparable window",
            "current_short_label": "Current window",
            "prior_short_label": "Prior comparable",
            "comparison_label": "Current window vs prior comparable window",
            "window_label": "Live filters",
            "current_window_label": "Current filtered window",
            "prior_window_label": "Prior comparable window",
            "note": "Comparisons follow the active filtered window.",
            "trajectory_note": "Trajectory shows the active filtered window only.",
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
            f"Trend shows the active filtered window. The latest month is partial, so deltas use "
            f"{_window_label(prior_start, prior_end)} rather than a full prior month."
        )
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
        trajectory_note = "Trend uses completed periods from the active filtered window."
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
        trajectory_note = "Trend shows the active filtered window; deltas use the prior matched-days comparison."

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
    }


def _filters_with_window(filters: Any, start_iso: str | None, end_iso: str | None) -> Any:
    if not start_iso and not end_iso:
        return filters
    try:
        start_val = pd.to_datetime(start_iso, errors="coerce") if start_iso else None
        end_val = pd.to_datetime(end_iso, errors="coerce") if end_iso else None
        return dc_replace(filters, start=start_val, end=end_val)
    except Exception:
        return filters


def _pagination(args: Any, default_size: int = DEFAULT_PAGE_SIZE, max_size: int = MAX_PAGE_SIZE) -> Tuple[int, int]:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    try:
        page = max(1, int(getter("page", 1)))
    except Exception:
        page = 1
    try:
        size = int(getter("page_size") or getter("per_page") or default_size)
    except Exception:
        size = default_size
    size = max(1, min(size, max_size))
    return page, size


def _sort_params(args: Any) -> Tuple[str, str]:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    sort_by_raw = str(getter("sort") or getter("sort_by") or "revenue").strip().lower()
    sort_dir_raw = str(getter("sort_dir") or getter("direction") or "desc").strip().lower()
    sort_dir = "ASC" if sort_dir_raw in {"asc", "ascending", "up", "1"} else "DESC"
    mapping = {
        "supplier": "supplier_name",
        "supplier_name": "supplier_name",
        "suppliername": "supplier_name",
        "name": "supplier_name",
        "segment_label": "segment_label",
        "segment": "segment_label",
        "risk_band": "risk_band",
        "risk": "risk_band",
        "supplier_id": "supplier_id",
        "supplierid": "supplier_id",
        "revenue": "revenue",
        "revenue_current": "revenue_current",
        "revenue_prior": "revenue_prior",
        "revenue_prior_window": "revenue_prior",
        "delta_revenue": "delta_revenue",
        "delta_revenue_pct": "delta_revenue_pct",
        "cost": "cost",
        "cost_current": "cost",
        "profit": "profit",
        "profit_current": "profit",
        "profit_prior": "profit_prior",
        "delta_profit": "delta_profit",
        "margin": "margin_pct",
        "margin_pct": "margin_pct",
        "marginpct": "margin_pct",
        "margin_prior": "margin_prior_pct",
        "margin_prior_pct": "margin_prior_pct",
        "delta_margin_pp": "delta_margin_pp",
        "roi": "roi_pct",
        "roi_pct": "roi_pct",
        "roipct": "roi_pct",
        "units": "units",
        "qty": "units",
        "weight": "weight_lb",
        "weight_lb": "weight_lb",
        "weightlb": "weight_lb",
        "contribution_per_lb": "contribution_per_lb",
        "missing_cost_pct": "missing_cost_pct",
        "missing_cost_revenue": "missing_cost_revenue_current",
        "cost_coverage_pct": "cost_coverage_pct",
        "customers": "customers",
        "avg_sale_price_per_lb": "avg_sale_price_per_lb",
        "avgsalepriceperlb": "avg_sale_price_per_lb",
        "avg_cost_per_unit": "avg_cost_per_unit",
        "avgcostperunit": "avg_cost_per_unit",
        "avg_cost_per_lb": "avg_cost_per_lb",
        "avgcostperlb": "avg_cost_per_lb",
        "profit_per_unit": "profit_per_unit",
        "profitperunit": "profit_per_unit",
        "profit_per_lb": "profit_per_lb",
        "profitperlb": "profit_per_lb",
        "products": "products",
        "orders": "orders",
        "top_protein": "top_protein",
        "topprotein": "top_protein",
        "top_protein_share_pct": "top_protein_share_pct",
        "protein_share": "top_protein_share_pct",
        "top_sku_share_pct": "top_sku_share_pct",
        "sku_concentration": "top_sku_share_pct",
        "days_since_last_order": "days_since_last_order",
        "last_sold": "last_sold",
        "lastsold": "last_sold",
    }
    sort_by = mapping.get(sort_by_raw, "revenue")
    return sort_by, sort_dir


def _extract_search(args: Any) -> str:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    return str(getter("search") or getter("q") or "").strip()


def _parse_protein_filter(args: Any) -> str:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    raw = getter("protein") or getter("protein_family") or getter("family") or ""
    return str(raw or "").strip()


def _coalesce_text_expr(cols: set[str], candidates: Sequence[str], default: str) -> str:
    parts: list[str] = []
    for cand in candidates:
        if cand in cols:
            qc = _quote(cand)
            parts.append(f"NULLIF(CAST({qc} AS VARCHAR), '')")
    if not parts:
        return default
    parts.append(default)
    if len(parts) == 1:
        return parts[0]
    return f"COALESCE({', '.join(parts)})"


def _coalesce_num_expr(cols: set[str], candidates: Sequence[str], default: str = "0") -> str:
    parts: list[str] = []
    for cand in candidates:
        if cand in cols:
            qc = _quote(cand)
            parts.append(f"CAST({qc} AS DOUBLE)")
    if not parts:
        return default
    parts.append(default)
    if len(parts) == 1:
        return parts[0]
    return f"COALESCE({', '.join(parts)})"


def _coalesce_nonzero_num_expr(cols: set[str], candidates: Sequence[str], default: str = "0") -> str:
    parts: list[str] = []
    for cand in candidates:
        if cand in cols:
            qc = _quote(cand)
            parts.append(f"NULLIF(CAST({qc} AS DOUBLE), 0)")
    if not parts:
        return default
    parts.append(default)
    if len(parts) == 1:
        return parts[0]
    return f"COALESCE({', '.join(parts)})"


def _coalesce_date_expr(cols: set[str], candidates: Sequence[str], default: str = "NULL::DATE") -> str:
    parts: list[str] = []
    for cand in candidates:
        if cand in cols:
            qc = _quote(cand)
            parts.append(f"CAST({qc} AS DATE)")
    if not parts:
        return default
    parts.append(default)
    if len(parts) == 1:
        return parts[0]
    return f"COALESCE({', '.join(parts)})"


def _required_columns(cols: set[str]) -> Dict[str, Any]:
    date_col = _safe_col(cols, fs.CANON.date, "Date")
    date_candidates = [c for c in DATE_CANDIDATES if c in cols]
    if date_col and date_col not in date_candidates:
        date_candidates.append(date_col)

    supplier_id_candidates = [c for c in SUPPLIER_ID_CANDIDATES if c in cols]
    supplier_name_candidates = [c for c in SUPPLIER_NAME_CANDIDATES if c in cols]
    revenue_candidates = [c for c in REVENUE_CANDIDATES if c in cols]

    order_candidates = [c for c in ORDER_ID_CANDIDATES if c in cols]
    product_id_candidates = [c for c in PRODUCT_ID_CANDIDATES if c in cols]
    product_name_candidates = [c for c in PRODUCT_NAME_CANDIDATES if c in cols]
    customer_id_candidates = [c for c in CUSTOMER_ID_CANDIDATES if c in cols]
    customer_name_candidates = [c for c in CUSTOMER_NAME_CANDIDATES if c in cols]
    region_candidates = [c for c in REGION_CANDIDATES if c in cols]
    ship_method_candidates = [c for c in SHIP_METHOD_CANDIDATES if c in cols]
    sales_rep_candidates = [c for c in SALES_REP_CANDIDATES if c in cols]
    protein_candidates = [c for c in PROTEIN_CANDIDATES if _safe_col(cols, c)]
    category_candidates = [c for c in CATEGORY_CANDIDATES if _safe_col(cols, c)]

    qty_candidates = [c for c in QTY_CANDIDATES if c in cols]
    weight_candidates = [c for c in WEIGHT_CANDIDATES if c in cols]

    cost_total_candidates = [c for c in COST_TOTAL_CANDIDATES if c in cols]
    cost_per_unit_candidates = [c for c in COST_PER_UNIT_CANDIDATES if c in cols]
    cost_per_lb_candidates = [c for c in COST_PER_LB_CANDIDATES if c in cols]

    return {
        "date": date_col,
        "date_candidates": date_candidates,
        "supplier_id_candidates": supplier_id_candidates,
        "supplier_name_candidates": supplier_name_candidates,
        "revenue_candidates": revenue_candidates,
        "order_candidates": order_candidates,
        "product_id_candidates": product_id_candidates,
        "product_name_candidates": product_name_candidates,
        "customer_id_candidates": customer_id_candidates,
        "customer_name_candidates": customer_name_candidates,
        "region_candidates": region_candidates,
        "ship_method_candidates": ship_method_candidates,
        "sales_rep_candidates": sales_rep_candidates,
        "protein_candidates": list(dict.fromkeys(protein_candidates)),
        "category_candidates": list(dict.fromkeys(category_candidates)),
        "qty_candidates": qty_candidates,
        "weight_candidates": weight_candidates,
        "cost_total_candidates": cost_total_candidates,
        "cost_per_unit_candidates": cost_per_unit_candidates,
        "cost_per_lb_candidates": cost_per_lb_candidates,
    }


def _supplier_id_expr(cols: set[str], cols_map: Mapping[str, Any]) -> str:
    supplier_ids = list(cols_map.get("supplier_id_candidates") or [])
    supplier_names = list(cols_map.get("supplier_name_candidates") or [])
    candidates = supplier_ids + [c for c in supplier_names if c not in supplier_ids]
    return _coalesce_text_expr(cols, candidates, "'Unknown Supplier'")


def _supplier_name_expr(cols: set[str], cols_map: Mapping[str, Any], supplier_id_expr: str) -> str:
    supplier_ids = list(cols_map.get("supplier_id_candidates") or [])
    supplier_names = list(cols_map.get("supplier_name_candidates") or [])
    candidates = supplier_names + [c for c in supplier_ids if c not in supplier_names]
    return _coalesce_text_expr(cols, candidates, supplier_id_expr)


def _scoped_cte(cols: set[str], cols_map: Mapping[str, Any], where_sql: str) -> str:
    order_date_expr = _coalesce_date_expr(cols, cols_map.get("date_candidates") or (), "NULL::DATE")
    supplier_id_expr = _supplier_id_expr(cols, cols_map)
    supplier_name_expr = _supplier_name_expr(cols, cols_map, supplier_id_expr)

    order_id_expr = _coalesce_text_expr(cols, cols_map.get("order_candidates") or (), "NULL::VARCHAR")
    product_id_expr = _coalesce_text_expr(cols, cols_map.get("product_id_candidates") or (), "NULL::VARCHAR")
    product_name_expr = _coalesce_text_expr(
        cols,
        (cols_map.get("product_name_candidates") or ()) + (cols_map.get("product_id_candidates") or ()),
        product_id_expr,
    )
    customer_id_expr = _coalesce_text_expr(cols, cols_map.get("customer_id_candidates") or (), "NULL::VARCHAR")
    customer_name_expr = _coalesce_text_expr(
        cols,
        (cols_map.get("customer_name_candidates") or ()) + (cols_map.get("customer_id_candidates") or ()),
        customer_id_expr,
    )
    region_expr = _coalesce_text_expr(cols, cols_map.get("region_candidates") or (), "NULL::VARCHAR")
    ship_method_expr = _coalesce_text_expr(cols, cols_map.get("ship_method_candidates") or (), "NULL::VARCHAR")
    sales_rep_expr = _coalesce_text_expr(cols, cols_map.get("sales_rep_candidates") or (), "NULL::VARCHAR")
    protein_base_expr = _coalesce_text_expr(cols, cols_map.get("protein_candidates") or (), "NULL::VARCHAR")
    category_base_expr = _coalesce_text_expr(cols, cols_map.get("category_candidates") or (), "NULL::VARCHAR")
    protein_expr = f"COALESCE({protein_base_expr}, {category_base_expr}, 'Unassigned')"
    category_expr = f"COALESCE({category_base_expr}, {protein_base_expr}, 'Unassigned')"

    # Prefer non-zero candidates so synthetic zero-filled columns do not shadow real revenue fields.
    revenue_expr = _coalesce_nonzero_num_expr(cols, cols_map.get("revenue_candidates") or (), "0")
    units_expr = _coalesce_nonzero_num_expr(cols, cols_map.get("qty_candidates") or (), "0")
    weight_expr = _coalesce_num_expr(cols, cols_map.get("weight_candidates") or (), "0")

    cost_total_expr = _coalesce_nonzero_num_expr(cols, cols_map.get("cost_total_candidates") or (), "NULL::DOUBLE")
    cost_per_unit_expr = _coalesce_nonzero_num_expr(cols, cols_map.get("cost_per_unit_candidates") or (), "NULL::DOUBLE")
    cost_per_lb_expr = _coalesce_nonzero_num_expr(cols, cols_map.get("cost_per_lb_candidates") or (), "NULL::DOUBLE")
    base_cost_expr = """
                COALESCE(
                    cost_total,
                    cost_per_lb * NULLIF(weight_lb, 0),
                    cost_per_unit * NULLIF(units, 0)
                )
    """
    effective_cost_expr = margin_rules.sql_effective_cost_expr(base_cost_expr, "weight_lb", "units", fallback="NULL::DOUBLE")

    return f"""
        scoped_base AS (
            SELECT
                CAST({order_date_expr} AS DATE) AS order_date,
                CAST({supplier_id_expr} AS VARCHAR) AS supplier_id,
                CAST({supplier_name_expr} AS VARCHAR) AS supplier_name,
                CAST({order_id_expr} AS VARCHAR) AS order_id,
                CAST({product_id_expr} AS VARCHAR) AS product_id,
                CAST({product_name_expr} AS VARCHAR) AS product_name,
                CAST({customer_id_expr} AS VARCHAR) AS customer_id,
                CAST({customer_name_expr} AS VARCHAR) AS customer_name,
                CAST({region_expr} AS VARCHAR) AS region,
                CAST({ship_method_expr} AS VARCHAR) AS shipping_method,
                CAST({sales_rep_expr} AS VARCHAR) AS sales_rep,
                CAST({protein_expr} AS VARCHAR) AS protein_family,
                CAST({category_expr} AS VARCHAR) AS product_category,
                CAST({revenue_expr} AS DOUBLE) AS revenue,
                CAST({units_expr} AS DOUBLE) AS units,
                CAST({weight_expr} AS DOUBLE) AS weight_lb,
                {cost_total_expr} AS cost_total,
                {cost_per_lb_expr} AS cost_per_lb,
                {cost_per_unit_expr} AS cost_per_unit
            FROM fact
            WHERE {where_sql}
        ),
        scoped AS (
            SELECT
                order_date,
                supplier_id,
                supplier_name,
                order_id,
                product_id,
                product_name,
                customer_id,
                customer_name,
                region,
                shipping_method,
                sales_rep,
                COALESCE(NULLIF(protein_family, ''), 'Unassigned') AS protein_family,
                COALESCE(NULLIF(product_category, ''), COALESCE(NULLIF(protein_family, ''), 'Unassigned')) AS product_category,
                revenue,
                units,
                weight_lb,
                {base_cost_expr} AS base_cost,
                ({effective_cost_expr}) AS cost
            FROM scoped_base
        )
    """


def _supplier_rollup_cte() -> str:
    return """
        supplier_rollup AS (
            SELECT
                supplier_id,
                supplier_name,
                SUM(revenue) AS revenue,
                SUM(units) AS units,
                SUM(weight_lb) AS weight_lb,
                SUM(cost) AS cost_sum,
                SUM(CASE WHEN cost IS NULL THEN 0 ELSE 1 END) AS cost_count,
                COUNT(DISTINCT CASE WHEN order_id IS NOT NULL AND order_id <> '' THEN order_id END) AS orders,
                COUNT(DISTINCT CASE WHEN product_id IS NOT NULL AND product_id <> '' THEN product_id END) AS products,
                COUNT(DISTINCT CASE WHEN customer_id IS NOT NULL AND customer_id <> '' THEN customer_id END) AS customers,
                MAX(order_date) AS last_sold
            FROM scoped
            GROUP BY 1,2
        ),
        supplier_enriched AS (
            SELECT
                supplier_id,
                supplier_name,
                revenue,
                CASE WHEN cost_count > 0 THEN cost_sum ELSE NULL END AS cost,
                CASE WHEN cost_count > 0 THEN revenue - cost_sum ELSE NULL END AS profit,
                CASE WHEN revenue > 0 AND cost_count > 0 THEN (revenue - cost_sum) / revenue * 100 ELSE NULL END AS margin_pct,
                CASE WHEN cost_count > 0 AND cost_sum > 0 THEN (revenue - cost_sum) / cost_sum * 100 ELSE NULL END AS roi_pct,
                units,
                weight_lb,
                CASE WHEN weight_lb > 0 THEN revenue / NULLIF(weight_lb, 0) ELSE NULL END AS avg_sale_price_per_lb,
                CASE WHEN cost_count > 0 AND units > 0 THEN cost_sum / NULLIF(units, 0) ELSE NULL END AS avg_cost_per_unit,
                CASE WHEN cost_count > 0 AND weight_lb > 0 THEN cost_sum / NULLIF(weight_lb, 0) ELSE NULL END AS avg_cost_per_lb,
                CASE WHEN cost_count > 0 AND units > 0 THEN (revenue - cost_sum) / NULLIF(units, 0) ELSE NULL END AS profit_per_unit,
                CASE WHEN cost_count > 0 AND weight_lb > 0 THEN (revenue - cost_sum) / NULLIF(weight_lb, 0) ELSE NULL END AS profit_per_lb,
                orders,
                products,
                customers,
                last_sold
            FROM supplier_rollup
        )
    """


def _table_order_expr(sort_by: str) -> str:
    mapping = {
        "supplier_id": "supplier_id",
        "supplier_name": "LOWER(supplier_name)",
        "revenue": "revenue",
        "cost": "cost",
        "profit": "profit",
        "margin_pct": "margin_pct",
        "roi_pct": "roi_pct",
        "units": "units",
        "weight_lb": "weight_lb",
        "avg_sale_price_per_lb": "avg_sale_price_per_lb",
        "avg_cost_per_unit": "avg_cost_per_unit",
        "avg_cost_per_lb": "avg_cost_per_lb",
        "profit_per_unit": "profit_per_unit",
        "profit_per_lb": "profit_per_lb",
        "products": "products",
        "orders": "orders",
        "last_sold": "last_sold",
    }
    return mapping.get(sort_by, "revenue")


def _supplier_product_sort_params(args: Any) -> Tuple[str, str]:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    sort_by_raw = str(getter("sort") or getter("sort_by") or "revenue").strip().lower()
    sort_dir_raw = str(getter("sort_dir") or getter("direction") or "desc").strip().lower()
    sort_dir = "ASC" if sort_dir_raw in {"asc", "ascending", "up", "1"} else "DESC"
    mapping = {
        "product": "product_name",
        "product_name": "product_name",
        "productname": "product_name",
        "name": "product_name",
        "product_id": "product_id",
        "productid": "product_id",
        "sku": "product_id",
        "revenue": "revenue",
        "cost": "cost",
        "profit": "profit",
        "margin": "margin_pct",
        "margin_pct": "margin_pct",
        "marginpct": "margin_pct",
        "units": "units",
        "qty": "units",
        "weight": "weight_lb",
        "weight_lb": "weight_lb",
        "weightlb": "weight_lb",
        "orders": "orders",
        "customers": "customers",
        "avg_sale_price": "avg_sale_price",
        "avgsaleprice": "avg_sale_price",
        "avg_cost_per_unit": "avg_cost_per_unit",
        "avgcostperunit": "avg_cost_per_unit",
    }
    sort_by = mapping.get(sort_by_raw, "revenue")
    return sort_by, sort_dir


def _supplier_product_order_expr(sort_by: str) -> str:
    mapping = {
        "product_id": "LOWER(COALESCE(product_id, ''))",
        "product_name": "LOWER(COALESCE(product_name, ''))",
        "revenue": "revenue",
        "cost": "cost",
        "profit": "profit",
        "margin_pct": "margin_pct",
        "units": "units",
        "weight_lb": "weight_lb",
        "orders": "orders",
        "customers": "customers",
        "avg_sale_price": "avg_sale_price",
        "avg_cost_per_unit": "avg_cost_per_unit",
    }
    return mapping.get(sort_by, "revenue")


def _supplier_products_sql(
    scoped_supplier_cte: str,
    sort_by: str,
    sort_dir: str,
    search: str,
    *,
    paginate: bool,
    page_size: int,
    offset: int,
) -> tuple[str, List[Any]]:
    order_expr = _supplier_product_order_expr(sort_by)
    search_sql = ""
    search_params: list[Any] = []
    if search:
        search_sql = """
            WHERE
                LOWER(COALESCE(product_name, '')) LIKE ?
                OR LOWER(COALESCE(product_id, '')) LIKE ?
        """
        token = f"%{search.lower()}%"
        search_params.extend([token, token])

    limit_sql = ""
    limit_params: list[Any] = []
    if paginate:
        limit_sql = "LIMIT ? OFFSET ?"
        limit_params.extend([page_size, offset])

    sql = f"""
        WITH
        {scoped_supplier_cte},
        prod_rollup AS (
            SELECT
                product_id,
                product_name,
                SUM(revenue) AS revenue,
                SUM(cost) AS cost_sum,
                SUM(CASE WHEN cost IS NULL THEN 0 ELSE 1 END) AS cost_count,
                SUM(units) AS units,
                SUM(weight_lb) AS weight_lb,
                COUNT(DISTINCT CASE WHEN order_id IS NOT NULL AND order_id <> '' THEN order_id END) AS orders,
                COUNT(DISTINCT CASE WHEN customer_id IS NOT NULL AND customer_id <> '' THEN customer_id END) AS customers
            FROM scoped_supplier
            WHERE product_id IS NOT NULL AND product_id <> ''
            GROUP BY 1,2
        ),
        enriched AS (
            SELECT
                product_id,
                product_name,
                revenue,
                CASE WHEN cost_count > 0 THEN cost_sum ELSE NULL END AS cost,
                CASE WHEN cost_count > 0 THEN revenue - cost_sum ELSE NULL END AS profit,
                CASE WHEN revenue > 0 AND cost_count > 0 THEN (revenue - cost_sum) / revenue * 100 ELSE NULL END AS margin_pct,
                units,
                weight_lb,
                orders,
                customers,
                CASE WHEN units > 0 THEN revenue / NULLIF(units, 0) ELSE NULL END AS avg_sale_price,
                CASE WHEN cost_count > 0 AND units > 0 THEN cost_sum / NULLIF(units, 0) ELSE NULL END AS avg_cost_per_unit
            FROM prod_rollup
        ),
        filtered AS (
            SELECT
                product_id,
                product_name,
                revenue,
                cost,
                profit,
                margin_pct,
                units,
                weight_lb,
                orders,
                customers,
                avg_sale_price,
                avg_cost_per_unit
            FROM enriched
            {search_sql}
        )
        SELECT
            product_id,
            product_name,
            revenue,
            cost,
            profit,
            margin_pct,
            units,
            weight_lb,
            orders,
            customers,
            avg_sale_price,
            avg_cost_per_unit
        FROM filtered
        ORDER BY {order_expr} {sort_dir} NULLS LAST, LOWER(COALESCE(product_name, product_id, ''))
        {limit_sql}
    """
    return sql, search_params + limit_params


def _supplier_products_count_sql(scoped_supplier_cte: str, search: str) -> tuple[str, List[Any]]:
    search_sql = ""
    search_params: list[Any] = []
    if search:
        search_sql = """
            WHERE
                LOWER(COALESCE(product_name, '')) LIKE ?
                OR LOWER(COALESCE(product_id, '')) LIKE ?
        """
        token = f"%{search.lower()}%"
        search_params.extend([token, token])

    sql = f"""
        WITH
        {scoped_supplier_cte},
        prod_rollup AS (
            SELECT
                product_id,
                product_name
            FROM scoped_supplier
            WHERE product_id IS NOT NULL AND product_id <> ''
            GROUP BY 1,2
        )
        SELECT COUNT(*) AS total_rows
        FROM prod_rollup
        {search_sql}
    """
    return sql, search_params


def _filter_hash(filters: Any, scope: Mapping[str, Any], dataset_version: str) -> tuple[str, Dict[str, Any]]:
    filters_json = filters_service.canonical_json(filters)
    scope_summary = _scope_summary(scope)
    payload = {
        "filters": json.loads(filters_json),
        "scope": scope_summary,
        "dataset_version": dataset_version,
    }
    return _hash_payload(payload), scope_summary


def _kpis_charts_sql(scoped_cte: str, top_n: int) -> str:
    return f"""
        WITH
        {scoped_cte},
        {_supplier_rollup_cte()},
        totals AS (
            SELECT
                SUM(revenue) AS total_revenue,
                SUM(cost) AS total_cost_sum,
                SUM(CASE WHEN cost IS NULL THEN 0 ELSE 1 END) AS total_cost_count,
                SUM(units) AS total_units,
                SUM(weight_lb) AS total_weight_lb,
                COUNT(DISTINCT CASE WHEN order_id IS NOT NULL AND order_id <> '' THEN order_id END) AS total_orders,
                COUNT(DISTINCT CASE WHEN product_id IS NOT NULL AND product_id <> '' THEN product_id END) AS total_products,
                COUNT(DISTINCT CASE WHEN customer_id IS NOT NULL AND customer_id <> '' THEN customer_id END) AS total_customers,
                COUNT(DISTINCT CASE WHEN supplier_id IS NOT NULL AND supplier_id <> '' THEN supplier_id END) AS supplier_count
            FROM scoped
        ),
        monthly_rollup AS (
            SELECT
                date_trunc('month', order_date) AS month_start,
                SUM(revenue) AS revenue,
                SUM(cost) AS cost_sum,
                SUM(CASE WHEN cost IS NULL THEN 0 ELSE 1 END) AS cost_count,
                SUM(units) AS units,
                SUM(weight_lb) AS weight_lb,
                COUNT(DISTINCT CASE WHEN order_id IS NOT NULL AND order_id <> '' THEN order_id END) AS orders
            FROM scoped
            GROUP BY 1
        ),
        monthly_last12 AS (
            SELECT * FROM monthly_rollup ORDER BY month_start DESC LIMIT 12
        ),
        monthly_sorted AS (
            SELECT
                strftime('%Y-%m', month_start) AS month,
                revenue,
                CASE WHEN cost_count > 0 THEN cost_sum ELSE NULL END AS cost,
                CASE WHEN cost_count > 0 THEN revenue - cost_sum ELSE NULL END AS profit,
                CASE WHEN revenue > 0 AND cost_count > 0 THEN (revenue - cost_sum) / revenue * 100 ELSE NULL END AS margin_pct,
                units,
                weight_lb,
                orders,
                month_start
            FROM monthly_last12
            ORDER BY month_start
        ),
        top_suppliers AS (
            SELECT *
            FROM supplier_enriched
            ORDER BY revenue DESC, supplier_name
            LIMIT {top_n if top_n > 0 else TOP_N_MAX}
        ),
        concentration AS (
            SELECT
                CASE
                    WHEN t.total_revenue > 0 THEN (
                        SELECT SUM(POWER(sr.revenue / t.total_revenue, 2)) * 10000
                        FROM supplier_rollup sr
                    )
                    ELSE NULL
                END AS hhi,
                CASE
                    WHEN t.total_revenue > 0 THEN (
                        SELECT SUM(revenue) / t.total_revenue * 100
                        FROM (SELECT revenue FROM supplier_rollup ORDER BY revenue DESC LIMIT 1)
                    )
                    ELSE NULL
                END AS top1_share,
                CASE
                    WHEN t.total_revenue > 0 THEN (
                        SELECT SUM(revenue) / t.total_revenue * 100
                        FROM (SELECT revenue FROM supplier_rollup ORDER BY revenue DESC LIMIT 5)
                    )
                    ELSE NULL
                END AS top5_share
            FROM totals t
        )
        SELECT
            t.total_revenue,
            CASE WHEN t.total_cost_count > 0 THEN t.total_cost_sum ELSE NULL END AS total_cost,
            CASE WHEN t.total_cost_count > 0 THEN t.total_revenue - t.total_cost_sum ELSE NULL END AS total_profit,
            t.supplier_count,
            t.total_orders,
            t.total_products,
            t.total_customers,
            t.total_units,
            t.total_weight_lb,
            CASE WHEN t.total_orders > 0 THEN t.total_revenue / NULLIF(t.total_orders, 0) ELSE 0 END AS avg_order_value,
            CASE
                WHEN t.total_revenue > 0 AND t.total_cost_count > 0 THEN (t.total_revenue - t.total_cost_sum) / t.total_revenue * 100
                ELSE NULL
            END AS avg_margin_pct,
            c.hhi,
            c.top1_share,
            c.top5_share,
            (SELECT list(struct_pack(
                month:=month,
                revenue:=revenue,
                cost:=cost,
                profit:=profit,
                margin_pct:=margin_pct,
                units:=units,
                weight_lb:=weight_lb,
                orders:=orders
            )) FROM monthly_sorted) AS monthly,
            (SELECT list(struct_pack(
                supplier_id:=supplier_id,
                supplier_name:=supplier_name,
                revenue:=revenue,
                cost:=cost,
                profit:=profit,
                margin_pct:=margin_pct,
                roi_pct:=roi_pct,
                units:=units,
                weight_lb:=weight_lb,
                avg_sale_price_per_lb:=avg_sale_price_per_lb,
                avg_cost_per_unit:=avg_cost_per_unit,
                avg_cost_per_lb:=avg_cost_per_lb,
                profit_per_unit:=profit_per_unit,
                profit_per_lb:=profit_per_lb,
                orders:=orders,
                products:=products,
                customers:=customers,
                last_sold:=last_sold
            )) FROM top_suppliers) AS top_suppliers
        FROM totals t
        CROSS JOIN concentration c
    """


def _parse_kpis_charts_row(row: Mapping[str, Any], start_iso: str | None, end_iso: str | None, top_n: int) -> Dict[str, Any]:
    monthly = _struct_list(row.get("monthly"))
    trend_labels = [str(item.get("month")) for item in monthly if item.get("month") is not None]
    trend_revenue = [_clean_float(item.get("revenue")) for item in monthly]
    trend_cost = [_clean_optional_float(item.get("cost")) for item in monthly]
    trend_profit = [_clean_optional_float(item.get("profit")) for item in monthly]
    trend_margin = [_clean_optional_float(item.get("margin_pct")) for item in monthly]

    top_list = _struct_list(row.get("top_suppliers"))
    top_rows: list[dict[str, Any]] = []
    for s in top_list:
        supplier_id = s.get("supplier_id")
        supplier_name = s.get("supplier_name") or supplier_id or "Unknown Supplier"
        top_rows.append(
            {
                "supplier_id": supplier_id,
                "supplier_name": supplier_name,
                "key": supplier_id,
                "label": supplier_name,
                "revenue": _clean_float(s.get("revenue")),
                "cost": _clean_optional_float(s.get("cost")),
                "profit": _clean_optional_float(s.get("profit")),
                "margin_pct": _clean_optional_float(s.get("margin_pct")),
                "roi_pct": _clean_optional_float(s.get("roi_pct")),
                "units": _clean_float(s.get("units")),
                "weight_lb": _clean_float(s.get("weight_lb")),
                "avg_sale_price_per_lb": _clean_optional_float(s.get("avg_sale_price_per_lb")),
                "avg_cost_per_unit": _clean_optional_float(s.get("avg_cost_per_unit")),
                "avg_cost_per_lb": _clean_optional_float(s.get("avg_cost_per_lb")),
                "profit_per_unit": _clean_optional_float(s.get("profit_per_unit")),
                "profit_per_lb": _clean_optional_float(s.get("profit_per_lb")),
                "orders": _clean_int(s.get("orders")),
                "products": _clean_int(s.get("products")),
                "customers": _clean_int(s.get("customers")),
                "last_sold": _date_to_iso(s.get("last_sold")),
            }
        )

    top_labels = [str(s.get("supplier_name") or s.get("supplier_id") or "Unknown Supplier") for s in top_rows]
    top_values = [_clean_float(s.get("revenue")) for s in top_rows]

    kpis = {
        "total_revenue": _clean_float(row.get("total_revenue")),
        "total_cost": _clean_optional_float(row.get("total_cost")),
        "total_profit": _clean_optional_float(row.get("total_profit")),
        "total_suppliers": _clean_int(row.get("supplier_count")),
        "total_orders": _clean_int(row.get("total_orders")),
        "total_products": _clean_int(row.get("total_products")),
        "total_customers": _clean_int(row.get("total_customers")),
        "total_units": _clean_float(row.get("total_units")),
        "total_weight_lb": _clean_float(row.get("total_weight_lb")),
        "avg_order_value": _clean_float(row.get("avg_order_value")),
        "avg_margin_pct": _clean_optional_float(row.get("avg_margin_pct")),
        "concentration_hhi": _clean_optional_float(row.get("hhi")),
        "concentration_top1_share": _clean_optional_float(row.get("top1_share")),
        "concentration_top5_share": _clean_optional_float(row.get("top5_share")),
        "start": start_iso,
        "end": end_iso,
    }

    trend_payload = {
        "labels": trend_labels,
        "revenue": trend_revenue,
        "cost": trend_cost,
        "profit": trend_profit,
        "margin_pct": trend_margin,
    }

    charts_payload = {
        "trend_12m": dict(trend_payload),
        "top_suppliers": {
            "labels": top_labels,
            "values": top_values,
            "rows": top_rows,
            "top_n": top_n,
        },
    }

    return {"kpis": kpis, "trend": trend_payload, "charts": charts_payload}


def _suppliers_table_sql(
    scoped_cte: str,
    sort_by: str,
    sort_dir: str,
    search: str,
    *,
    paginate: bool,
    page_size: int,
    offset: int,
) -> tuple[str, List[Any]]:
    order_expr = _table_order_expr(sort_by)
    search_sql = ""
    search_params: list[Any] = []
    if search:
        search_sql = """
            WHERE
                LOWER(COALESCE(supplier_name, '')) LIKE ?
                OR LOWER(COALESCE(supplier_id, '')) LIKE ?
        """
        token = f"%{search.lower()}%"
        search_params.extend([token, token])

    limit_sql = ""
    limit_params: list[Any] = []
    if paginate:
        limit_sql = "LIMIT ? OFFSET ?"
        limit_params.extend([page_size, offset])

    sql = f"""
        WITH
        {scoped_cte},
        {_supplier_rollup_cte()},
        filtered AS (
            SELECT
                supplier_id,
                supplier_name,
                revenue,
                cost,
                profit,
                margin_pct,
                roi_pct,
                units,
                weight_lb,
                avg_sale_price_per_lb,
                avg_cost_per_unit,
                avg_cost_per_lb,
                profit_per_unit,
                profit_per_lb,
                products,
                orders,
                customers,
                last_sold
            FROM supplier_enriched
            {search_sql}
        )
        SELECT
            supplier_id,
            supplier_name,
            revenue,
            cost,
            profit,
            margin_pct,
            roi_pct,
            units,
            weight_lb,
            avg_sale_price_per_lb,
            avg_cost_per_unit,
            avg_cost_per_lb,
            profit_per_unit,
            profit_per_lb,
            products,
            orders,
            customers,
            last_sold,
            COUNT(*) OVER() AS total_groups
        FROM filtered
        ORDER BY {order_expr} {sort_dir} NULLS LAST
        {limit_sql}
    """
    return sql, search_params + limit_params


def _table_payload_from_df(
    table_df: pd.DataFrame,
    *,
    page_num: int,
    page_size: int,
    sort_by: str,
    sort_dir: str,
    search: str,
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    total_groups = 0
    if not table_df.empty:
        total_groups = int(table_df.iloc[0].get("total_groups", len(table_df)))
        for _, r in table_df.iterrows():
            supplier_id = r.get("supplier_id")
            supplier_name = r.get("supplier_name") or supplier_id or "Unknown Supplier"
            last_sold = _date_to_iso(r.get("last_sold"))
            row = {
                "supplier_id": supplier_id,
                "supplier_name": supplier_name,
                "key": supplier_id,
                "label": supplier_name,
                "revenue": _clean_float(r.get("revenue")),
                "cost": _clean_optional_float(r.get("cost")),
                "profit": _clean_optional_float(r.get("profit")),
                "margin_pct": _clean_optional_float(r.get("margin_pct")),
                "roi_pct": _clean_optional_float(r.get("roi_pct")),
                "units": _clean_float(r.get("units")),
                "weight_lb": _clean_float(r.get("weight_lb")),
                "avg_sale_price_per_lb": _clean_optional_float(r.get("avg_sale_price_per_lb")),
                "avg_cost_per_unit": _clean_optional_float(r.get("avg_cost_per_unit")),
                "avg_cost_per_lb": _clean_optional_float(r.get("avg_cost_per_lb")),
                "profit_per_unit": _clean_optional_float(r.get("profit_per_unit")),
                "profit_per_lb": _clean_optional_float(r.get("profit_per_lb")),
                "products": _clean_int(r.get("products")),
                "orders": _clean_int(r.get("orders")),
                "customers": _clean_int(r.get("customers")),
                "last_sold": last_sold,
                # Legacy aliases for templates that still reference old keys
                "SupplierId": supplier_id,
                "SupplierName": supplier_name,
                "Revenue": _clean_float(r.get("revenue")),
                "Cost": _clean_optional_float(r.get("cost")),
                "Profit": _clean_optional_float(r.get("profit")),
                "MarginPct": _clean_optional_float(r.get("margin_pct")),
                "ROIPct": _clean_optional_float(r.get("roi_pct")),
                "Units": _clean_float(r.get("units")),
                "WeightLb": _clean_float(r.get("weight_lb")),
                "AvgSalePricePerLb": _clean_optional_float(r.get("avg_sale_price_per_lb")),
                "AvgCostPerUnit": _clean_optional_float(r.get("avg_cost_per_unit")),
                "AvgCostPerLb": _clean_optional_float(r.get("avg_cost_per_lb")),
                "ProfitPerUnit": _clean_optional_float(r.get("profit_per_unit")),
                "ProfitPerLb": _clean_optional_float(r.get("profit_per_lb")),
                "Products": _clean_int(r.get("products")),
                "Orders": _clean_int(r.get("orders")),
                "LastSold": last_sold,
            }
            rows.append(row)

    return {
        "rows": rows,
        "page": page_num,
        "page_size": page_size,
        "total": total_groups,
        "total_rows": total_groups,
        "sort_by": sort_by,
        "sort_dir": sort_dir.lower(),
        "search": search,
    }


def _supplier_v2_stats_sql(scoped_cte: str) -> str:
    return f"""
        WITH
        {scoped_cte},
        window_params AS (
            SELECT
                CAST(? AS DATE) AS curr_start,
                CAST(? AS DATE) AS curr_end,
                CAST(? AS DATE) AS prior_start,
                CAST(? AS DATE) AS prior_end
        ),
        supplier_window AS (
            SELECT
                s.supplier_id,
                s.supplier_name,
                MIN(s.order_date) AS first_order_date,
                MAX(s.order_date) AS last_order_date,
                SUM(CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end THEN s.revenue ELSE 0 END) AS revenue_current,
                SUM(CASE WHEN s.order_date BETWEEN p.prior_start AND p.prior_end THEN s.revenue ELSE 0 END) AS revenue_prior,
                SUM(CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end THEN s.units ELSE 0 END) AS units_current,
                SUM(CASE WHEN s.order_date BETWEEN p.prior_start AND p.prior_end THEN s.units ELSE 0 END) AS units_prior,
                SUM(CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end THEN s.weight_lb ELSE 0 END) AS weight_current,
                SUM(CASE WHEN s.order_date BETWEEN p.prior_start AND p.prior_end THEN s.weight_lb ELSE 0 END) AS weight_prior,
                SUM(CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end AND s.cost IS NOT NULL THEN s.revenue ELSE 0 END) AS cost_covered_revenue_current,
                SUM(CASE WHEN s.order_date BETWEEN p.prior_start AND p.prior_end AND s.cost IS NOT NULL THEN s.revenue ELSE 0 END) AS cost_covered_revenue_prior,
                SUM(CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end AND s.cost IS NULL THEN s.revenue ELSE 0 END) AS missing_cost_revenue_current,
                SUM(CASE WHEN s.order_date BETWEEN p.prior_start AND p.prior_end AND s.cost IS NULL THEN s.revenue ELSE 0 END) AS missing_cost_revenue_prior,
                SUM(CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end AND s.cost IS NOT NULL THEN s.cost ELSE 0 END) AS cost_current_sum,
                SUM(CASE WHEN s.order_date BETWEEN p.prior_start AND p.prior_end AND s.cost IS NOT NULL THEN s.cost ELSE 0 END) AS cost_prior_sum,
                SUM(CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end AND s.cost IS NOT NULL THEN s.units ELSE 0 END) AS units_covered_current,
                SUM(CASE WHEN s.order_date BETWEEN p.prior_start AND p.prior_end AND s.cost IS NOT NULL THEN s.units ELSE 0 END) AS units_covered_prior,
                SUM(CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end AND s.cost IS NOT NULL THEN s.weight_lb ELSE 0 END) AS weight_covered_current,
                SUM(CASE WHEN s.order_date BETWEEN p.prior_start AND p.prior_end AND s.cost IS NOT NULL THEN s.weight_lb ELSE 0 END) AS weight_covered_prior,
                SUM(CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end AND s.cost IS NOT NULL THEN 1 ELSE 0 END) AS cost_known_rows_current,
                SUM(CASE WHEN s.order_date BETWEEN p.prior_start AND p.prior_end AND s.cost IS NOT NULL THEN 1 ELSE 0 END) AS cost_known_rows_prior,
                SUM(CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end THEN CASE WHEN s.cost IS NULL THEN 1 ELSE 0 END ELSE 0 END) AS cost_missing_rows_current,
                SUM(CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end THEN 1 ELSE 0 END) AS rows_current,
                COUNT(DISTINCT CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end AND s.order_id IS NOT NULL AND s.order_id <> '' THEN s.order_id END) AS orders_current,
                COUNT(DISTINCT CASE WHEN s.order_date BETWEEN p.prior_start AND p.prior_end AND s.order_id IS NOT NULL AND s.order_id <> '' THEN s.order_id END) AS orders_prior,
                COUNT(DISTINCT CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end AND s.product_id IS NOT NULL AND s.product_id <> '' THEN s.product_id END) AS products_current,
                COUNT(DISTINCT CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end AND s.customer_id IS NOT NULL AND s.customer_id <> '' THEN s.customer_id END) AS customers_current,
                COUNT(DISTINCT CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end AND s.protein_family IS NOT NULL AND s.protein_family <> '' THEN s.protein_family END) AS proteins_current,
                COUNT(DISTINCT CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end AND s.product_category IS NOT NULL AND s.product_category <> '' THEN s.product_category END) AS categories_current
            FROM scoped s
            CROSS JOIN window_params p
            GROUP BY 1,2
        )
        SELECT
            supplier_id,
            supplier_name,
            first_order_date,
            last_order_date,
            revenue_current,
            revenue_prior,
            units_current,
            units_prior,
            weight_current,
            weight_prior,
            cost_covered_revenue_current,
            cost_covered_revenue_prior,
            missing_cost_revenue_current,
            missing_cost_revenue_prior,
            CASE WHEN cost_known_rows_current > 0 THEN cost_current_sum ELSE NULL END AS cost_current,
            CASE WHEN cost_known_rows_prior > 0 THEN cost_prior_sum ELSE NULL END AS cost_prior,
            units_covered_current,
            units_covered_prior,
            weight_covered_current,
            weight_covered_prior,
            cost_missing_rows_current,
            rows_current,
            orders_current,
            orders_prior,
            products_current,
            customers_current,
            proteins_current,
            categories_current
        FROM supplier_window
        WHERE revenue_current > 0
           OR revenue_prior > 0
           OR orders_current > 0
           OR orders_prior > 0
    """


def _normalize_v2_supplier_frame(frame: pd.DataFrame, *, window_end_iso: str | None) -> pd.DataFrame:
    base_cols = [
        "supplier_id",
        "supplier_name",
        "first_order_date",
        "last_order_date",
        "revenue_current",
        "revenue_prior",
        "units_current",
        "units_prior",
        "weight_current",
        "weight_prior",
        "cost_covered_revenue_current",
        "cost_covered_revenue_prior",
        "missing_cost_revenue_current",
        "missing_cost_revenue_prior",
        "cost_current",
        "cost_prior",
        "units_covered_current",
        "units_covered_prior",
        "weight_covered_current",
        "weight_covered_prior",
        "cost_missing_rows_current",
        "rows_current",
        "orders_current",
        "orders_prior",
        "products_current",
        "customers_current",
        "proteins_current",
        "categories_current",
    ]
    if frame is None or frame.empty:
        empty = pd.DataFrame(columns=base_cols)
        empty["segment_label"] = pd.Series(dtype="object")
        empty["segment_key"] = pd.Series(dtype="object")
        return empty

    df = frame.copy()
    for col in base_cols:
        if col not in df.columns:
            df[col] = pd.NA

    numeric_zero_cols = [
        "revenue_current",
        "revenue_prior",
        "units_current",
        "units_prior",
        "weight_current",
        "weight_prior",
        "cost_covered_revenue_current",
        "cost_covered_revenue_prior",
        "missing_cost_revenue_current",
        "missing_cost_revenue_prior",
        "units_covered_current",
        "units_covered_prior",
        "weight_covered_current",
        "weight_covered_prior",
        "cost_missing_rows_current",
        "rows_current",
        "orders_current",
        "orders_prior",
        "products_current",
        "customers_current",
        "proteins_current",
        "categories_current",
    ]
    for col in numeric_zero_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    df["cost_current"] = pd.to_numeric(df["cost_current"], errors="coerce")
    df["cost_prior"] = pd.to_numeric(df["cost_prior"], errors="coerce")

    df["supplier_id"] = df["supplier_id"].astype("string")
    fallback_name = df["supplier_id"].fillna("Unknown Supplier")
    df["supplier_name"] = df["supplier_name"].astype("string").fillna(fallback_name)

    first_dates = pd.to_datetime(df["first_order_date"], errors="coerce")
    last_dates = pd.to_datetime(df["last_order_date"], errors="coerce")
    df["first_order_date"] = first_dates.dt.date.astype("string")
    df["last_order_date"] = last_dates.dt.date.astype("string")

    df["profit_current"] = (df["cost_covered_revenue_current"] - df["cost_current"]).where(df["cost_current"].notna())
    df["profit_prior"] = (df["cost_covered_revenue_prior"] - df["cost_prior"]).where(df["cost_prior"].notna())
    df["delta_revenue"] = df["revenue_current"] - df["revenue_prior"]
    df["delta_profit"] = (df["profit_current"] - df["profit_prior"]).where(
        df["profit_current"].notna() & df["profit_prior"].notna()
    )

    df["margin_pct"] = ((df["profit_current"] / df["cost_covered_revenue_current"]) * 100.0).where(
        df["cost_covered_revenue_current"] > 0
    )
    df["margin_prior_pct"] = ((df["profit_prior"] / df["cost_covered_revenue_prior"]) * 100.0).where(
        df["cost_covered_revenue_prior"] > 0
    )
    df["delta_margin_pp"] = (df["margin_pct"] - df["margin_prior_pct"]).where(
        df["margin_pct"].notna() & df["margin_prior_pct"].notna()
    )
    df["roi_pct"] = ((df["profit_current"] / df["cost_current"]) * 100.0).where(df["cost_current"] > 0)

    delta_pct_raw = ((df["delta_revenue"] / df["revenue_prior"]) * 100.0).where(df["revenue_prior"] > 0)
    df["delta_revenue_pct_raw"] = delta_pct_raw
    df["low_base_warning"] = (df["revenue_prior"] > 0) & (df["revenue_prior"] < 500.0)
    df["delta_revenue_pct"] = delta_pct_raw.where(~df["low_base_warning"])

    status = pd.Series(["flat"] * len(df), index=df.index, dtype="object")
    new_mask = (df["revenue_prior"] <= 0) & (df["revenue_current"] > 0)
    lost_mask = (df["revenue_current"] <= 0) & (df["revenue_prior"] > 0)
    up_mask = (df["delta_revenue"] > 0) & (df["revenue_prior"] > 0)
    down_mask = (df["delta_revenue"] < 0) & (df["revenue_prior"] > 0)
    status.loc[new_mask] = "new"
    status.loc[lost_mask] = "lost"
    status.loc[up_mask & ~new_mask & ~lost_mask] = "up"
    status.loc[down_mask & ~new_mask & ~lost_mask] = "down"
    status.loc[df["low_base_warning"] & status.isin(["up", "down", "flat"])] = "low_base"
    df["delta_revenue_status"] = status
    label_map = {
        "new": "New",
        "lost": "Lost",
        "up": "Up",
        "down": "Down",
        "flat": "Flat",
        "low_base": "Low base",
    }
    df["delta_revenue_label"] = df["delta_revenue_status"].map(label_map).fillna("Flat")

    df["aov"] = (df["revenue_current"] / df["orders_current"]).where(df["orders_current"] > 0)
    df["avg_sale_price_per_lb"] = (df["revenue_current"] / df["weight_current"]).where(df["weight_current"] > 0)
    df["avg_cost_per_unit"] = (df["cost_current"] / df["units_covered_current"]).where(
        df["cost_current"].notna() & (df["units_covered_current"] > 0)
    )
    df["avg_cost_per_lb"] = (df["cost_current"] / df["weight_covered_current"]).where(
        df["cost_current"].notna() & (df["weight_covered_current"] > 0)
    )
    df["contribution_per_lb"] = (df["profit_current"] / df["weight_covered_current"]).where(
        df["profit_current"].notna() & (df["weight_covered_current"] > 0)
    )
    df["missing_cost_pct"] = ((df["missing_cost_revenue_current"] / df["revenue_current"]) * 100.0).where(
        df["revenue_current"] > 0
    )
    df["cost_coverage_pct"] = ((df["cost_covered_revenue_current"] / df["revenue_current"]) * 100.0).where(
        df["revenue_current"] > 0
    )
    df["cost_coverage_row_pct"] = (
        ((df["rows_current"] - df["cost_missing_rows_current"]) / df["rows_current"]) * 100.0
    ).where(df["rows_current"] > 0)

    window_end = pd.to_datetime(window_end_iso, errors="coerce") if window_end_iso else pd.NaT
    if pd.notna(window_end):
        last_dt = pd.to_datetime(df["last_order_date"], errors="coerce")
        days_since = (window_end.normalize() - last_dt).dt.days
        df["days_since_last_order"] = days_since
    else:
        df["days_since_last_order"] = pd.NA

    risk_band = pd.Series(["Unknown"] * len(df), index=df.index, dtype="object")
    days = pd.to_numeric(df["days_since_last_order"], errors="coerce")
    risk_band = risk_band.where(~(days < 60), "Low")
    risk_band = risk_band.where(~((days >= 60) & (days < 90)), "Medium")
    risk_band = risk_band.where(~(days >= 90), "High")
    df["risk_band"] = risk_band
    df["at_risk_flag"] = (df["risk_band"] == "High").astype(int)

    df["revenue"] = df["revenue_current"]
    df["revenue_prior_window"] = df["revenue_prior"]
    df["cost"] = df["cost_current"]
    df["profit"] = df["profit_current"]
    df["units"] = df["units_current"]
    df["weight_lb"] = df["weight_current"]
    df["orders"] = df["orders_current"]
    df["orders_prior_window"] = df["orders_prior"]
    df["products"] = df["products_current"]
    df["customers"] = df["customers_current"]
    df["proteins"] = df["proteins_current"]
    df["categories"] = df["categories_current"]
    df["last_sold"] = df["last_order_date"]
    return df


def _assign_supplier_segments(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        out = frame.copy() if frame is not None else pd.DataFrame()
        out["segment_label"] = pd.Series(dtype="object")
        out["segment_key"] = pd.Series(dtype="object")
        return out

    out = frame.copy()
    if "target_margin_pct" not in out.columns:
        if "cost_current" in out.columns:
            out["effective_cost_basis"] = pd.to_numeric(out.get("cost_current"), errors="coerce")
        out = margin_rules.annotate_margin_frame(
            out,
            protein_col="top_protein",
            category_col="top_protein",
            revenue_col="revenue_current",
            cost_col="cost_current" if "cost_current" in out.columns else "profit_current",
            profit_col="profit_current",
            margin_col="margin_pct",
        )
    revenue_positive = pd.to_numeric(out["revenue_current"], errors="coerce")
    revenue_positive = revenue_positive[revenue_positive > 0]
    strategic_cutoff = float(revenue_positive.quantile(0.80)) if not revenue_positive.empty else 0.0
    median_cutoff = float(revenue_positive.quantile(0.50)) if not revenue_positive.empty else 0.0
    growth_cutoff_pct = 20.0

    labels: list[str] = []
    reasons: list[str] = []
    actions: list[str] = []
    for _, row in out.iterrows():
        rev = _clean_float(row.get("revenue_current"))
        margin = _clean_optional_float(row.get("margin_pct"))
        delta_pct = _clean_optional_float(row.get("delta_revenue_pct_raw"))
        missing_cost_pct = _clean_optional_float(row.get("missing_cost_pct")) or 0.0
        cost_coverage_pct = _clean_optional_float(row.get("cost_coverage_pct")) or 0.0
        top_protein_share = _clean_optional_float(row.get("top_protein_share_pct")) or 0.0
        top_sku_share = _clean_optional_float(row.get("top_sku_share_pct")) or 0.0
        row_target_margin_pct = _clean_optional_float(row.get("target_margin_pct"))
        is_concentrated = top_protein_share >= 65.0 or top_sku_share >= 35.0

        if missing_cost_pct >= 20.0 or cost_coverage_pct < 80.0:
            labels.append("Data risk")
            reasons.append("Cost coverage is too weak for reliable supplier margin interpretation.")
            actions.append("Review data risk")
            continue
        if row_target_margin_pct is None:
            labels.append("Data risk")
            reasons.append("Protein/category mapping is incomplete, so supplier target margin cannot be evaluated reliably.")
            actions.append("Review data risk")
            continue
        if rev >= strategic_cutoff and (margin is None or margin >= row_target_margin_pct):
            labels.append("Strategic")
            reasons.append("Revenue scale is concentrated here and current margin remains at or above target.")
            actions.append("Protect strategic suppliers")
            continue
        if rev >= max(median_cutoff, 1.0) and margin is not None and margin < row_target_margin_pct:
            labels.append("Margin risk")
            if is_concentrated:
                reasons.append("Meaningful revenue is sitting below target margin and concentrated in a narrow mix.")
            else:
                reasons.append("Meaningful revenue is sitting below target margin under the active window.")
            actions.append("Recover margin")
            continue
        if rev > 0 and delta_pct is not None and delta_pct >= growth_cutoff_pct:
            labels.append("Growth")
            reasons.append("Revenue is growing faster than the comparable period and remains commercially relevant.")
            actions.append("Expand growth suppliers")
            continue
        labels.append("Long tail")
        reasons.append("Lower-revenue supplier with narrower impact under the current scope.")
        actions.append("Rationalize long tail")

    out["segment_label"] = labels
    out["segment_reason"] = reasons
    out["action_bucket"] = actions
    out["segment_key"] = out["segment_label"].map(
        {
            "Strategic": "strategic",
            "Growth": "growth",
            "Margin risk": "margin_risk",
            "Data risk": "data_risk",
            "Long tail": "long_tail",
        }
    ).fillna("long_tail")
    return out


def _supplier_protein_mix_sql(scoped_cte: str) -> str:
    return f"""
        WITH
        {scoped_cte},
        window_params AS (
            SELECT
                CAST(? AS DATE) AS curr_start,
                CAST(? AS DATE) AS curr_end,
                CAST(? AS DATE) AS prior_start,
                CAST(? AS DATE) AS prior_end
        )
        SELECT
            s.supplier_id,
            s.supplier_name,
            COALESCE(NULLIF(s.protein_family, ''), 'Unassigned') AS protein_family,
            COALESCE(NULLIF(s.product_category, ''), COALESCE(NULLIF(s.protein_family, ''), 'Unassigned')) AS product_category,
            SUM(CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end THEN s.revenue ELSE 0 END) AS revenue_current,
            SUM(CASE WHEN s.order_date BETWEEN p.prior_start AND p.prior_end THEN s.revenue ELSE 0 END) AS revenue_prior,
            SUM(CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end THEN s.weight_lb ELSE 0 END) AS weight_current,
            SUM(CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end AND s.cost IS NOT NULL THEN s.revenue ELSE 0 END) AS cost_covered_revenue_current,
            SUM(CASE WHEN s.order_date BETWEEN p.prior_start AND p.prior_end AND s.cost IS NOT NULL THEN s.revenue ELSE 0 END) AS cost_covered_revenue_prior,
            SUM(CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end AND s.cost IS NOT NULL THEN s.cost ELSE 0 END) AS cost_current,
            SUM(CASE WHEN s.order_date BETWEEN p.prior_start AND p.prior_end AND s.cost IS NOT NULL THEN s.cost ELSE 0 END) AS cost_prior,
            COUNT(DISTINCT CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end AND s.product_id IS NOT NULL AND s.product_id <> '' THEN s.product_id END) AS sku_count,
            COUNT(DISTINCT CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end AND s.customer_id IS NOT NULL AND s.customer_id <> '' THEN s.customer_id END) AS customer_count,
            COUNT(DISTINCT CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end AND s.order_id IS NOT NULL AND s.order_id <> '' THEN s.order_id END) AS order_count
        FROM scoped s
        CROSS JOIN window_params p
        GROUP BY 1,2,3,4
        HAVING revenue_current > 0 OR revenue_prior > 0
    """


def _supplier_product_concentration_sql(scoped_cte: str) -> str:
    return f"""
        WITH
        {scoped_cte},
        window_params AS (
            SELECT
                CAST(? AS DATE) AS curr_start,
                CAST(? AS DATE) AS curr_end
        )
        SELECT
            s.supplier_id,
            s.supplier_name,
            s.product_id,
            s.product_name,
            SUM(CASE WHEN s.order_date BETWEEN p.curr_start AND p.curr_end THEN s.revenue ELSE 0 END) AS revenue_current
        FROM scoped s
        CROSS JOIN window_params p
        GROUP BY 1,2,3,4
        HAVING revenue_current > 0
    """


def _enrich_supplier_mix_frame(
    frame: pd.DataFrame,
    protein_df: pd.DataFrame,
    product_df: pd.DataFrame,
) -> pd.DataFrame:
    if frame is None or frame.empty:
        return frame.copy() if frame is not None else pd.DataFrame()

    out = frame.copy()
    default_list: list[str] = []
    for col, default in (
        ("top_protein", "Unassigned"),
        ("top_category", "Unassigned"),
        ("top_protein_share_pct", None),
        ("protein_family_count", 0),
        ("protein_hhi", None),
        ("top_sku_share_pct", None),
        ("skus_for_80_pct", None),
        ("top_sku", None),
        ("top_sku_name", None),
        ("protein_dependency_posture", "Balanced"),
        ("margin_risk_family", None),
        ("growth_family", None),
    ):
        if col not in out.columns:
            out[col] = default
    if "protein_families" not in out.columns:
        out["protein_families"] = pd.Series([default_list.copy() for _ in range(len(out.index))], index=out.index, dtype="object")

    supplier_index = {str(rec): idx for idx, rec in zip(out.index, out["supplier_id"].astype("string").fillna(""))}

    if protein_df is not None and not protein_df.empty:
        mix = protein_df.copy()
        for col in (
            "revenue_current",
            "revenue_prior",
            "weight_current",
            "cost_covered_revenue_current",
            "cost_covered_revenue_prior",
            "cost_current",
            "cost_prior",
            "sku_count",
            "customer_count",
            "order_count",
        ):
            mix[col] = pd.to_numeric(mix.get(col), errors="coerce").fillna(0.0)
        category_rank = (
            mix.groupby(["supplier_id", "protein_family", "product_category"], dropna=False)
            .agg(revenue_current=("revenue_current", "sum"))
            .reset_index()
            .sort_values(
                ["supplier_id", "protein_family", "revenue_current"],
                ascending=[True, True, False],
                na_position="last",
            )
            .groupby(["supplier_id", "protein_family"], dropna=False)
            .head(1)
            .loc[:, ["supplier_id", "protein_family", "product_category"]]
            .rename(columns={"product_category": "lead_category"})
        )
        family_mix = (
            mix.groupby(["supplier_id", "supplier_name", "protein_family"], dropna=False)
            .agg(
                revenue_current=("revenue_current", "sum"),
                revenue_prior=("revenue_prior", "sum"),
                weight_current=("weight_current", "sum"),
                cost_covered_revenue_current=("cost_covered_revenue_current", "sum"),
                cost_covered_revenue_prior=("cost_covered_revenue_prior", "sum"),
                cost_current=("cost_current", "sum"),
                cost_prior=("cost_prior", "sum"),
                sku_count=("sku_count", "sum"),
                customer_count=("customer_count", "sum"),
                order_count=("order_count", "sum"),
            )
            .reset_index()
            .merge(category_rank, on=["supplier_id", "protein_family"], how="left")
        )
        family_mix["profit_current"] = (
            family_mix["cost_covered_revenue_current"] - family_mix["cost_current"]
        ).where(family_mix["cost_covered_revenue_current"] > 0)
        family_mix["margin_pct"] = (
            (family_mix["profit_current"] / family_mix["cost_covered_revenue_current"]) * 100.0
        ).where(family_mix["cost_covered_revenue_current"] > 0)
        family_mix["delta_revenue"] = family_mix["revenue_current"] - family_mix["revenue_prior"]
        for supplier_id, group in family_mix.groupby("supplier_id", dropna=False):
            idx = supplier_index.get(str(supplier_id or ""))
            if idx is None:
                continue
            ranked = group.sort_values(["revenue_current", "weight_current"], ascending=[False, False], na_position="last")
            top = ranked.iloc[0]
            total_revenue = float(pd.to_numeric(group["revenue_current"], errors="coerce").fillna(0.0).sum())
            shares = (
                pd.to_numeric(group["revenue_current"], errors="coerce").fillna(0.0) / total_revenue
                if total_revenue > 0
                else pd.Series([0.0] * len(group.index), index=group.index, dtype="float64")
            )
            ranked = ranked.assign(_share=shares.loc[ranked.index].values)
            hhi = float((ranked["_share"] ** 2).sum() * 10000.0) if total_revenue > 0 else None
            top_share = float(ranked["_share"].iloc[0] * 100.0) if total_revenue > 0 and not ranked.empty else None
            family_names = list(
                dict.fromkeys(
                    [
                        str(val)
                        for val in ranked["protein_family"].astype("string").fillna("Unassigned").tolist()
                        if str(val).strip()
                    ]
                )
            )
            low_margin = ranked[ranked["margin_pct"].notna()].sort_values(["margin_pct", "revenue_current"], ascending=[True, False])
            growth = ranked.sort_values(["delta_revenue", "revenue_current"], ascending=[False, False], na_position="last")
            posture = "Balanced"
            if top_share is not None and top_share >= 70.0:
                posture = "Highly concentrated"
            elif top_share is not None and top_share >= 50.0:
                posture = "Focused"
            out.at[idx, "top_protein"] = str(top.get("protein_family") or "Unassigned")
            out.at[idx, "top_category"] = str(top.get("lead_category") or top.get("protein_family") or "Unassigned")
            out.at[idx, "top_protein_share_pct"] = _clean_optional_float(top_share)
            out.at[idx, "protein_family_count"] = int(ranked["protein_family"].astype("string").replace("", pd.NA).dropna().nunique())
            out.at[idx, "protein_hhi"] = _clean_optional_float(hhi)
            out.at[idx, "protein_families"] = family_names
            out.at[idx, "protein_dependency_posture"] = posture
            out.at[idx, "margin_risk_family"] = (
                str(low_margin.iloc[0].get("protein_family")) if not low_margin.empty else None
            )
            out.at[idx, "growth_family"] = (
                str(growth.iloc[0].get("protein_family")) if not growth.empty and _clean_float(growth.iloc[0].get("delta_revenue")) > 0 else None
            )

    if product_df is not None and not product_df.empty:
        prod = product_df.copy()
        prod["revenue_current"] = pd.to_numeric(prod.get("revenue_current"), errors="coerce").fillna(0.0)
        for supplier_id, group in prod.groupby("supplier_id", dropna=False):
            idx = supplier_index.get(str(supplier_id or ""))
            if idx is None:
                continue
            ranked = group.sort_values("revenue_current", ascending=False, na_position="last")
            total_revenue = float(ranked["revenue_current"].sum())
            if total_revenue <= 0 or ranked.empty:
                continue
            top = ranked.iloc[0]
            shares = ranked["revenue_current"] / total_revenue
            skus_for_80 = _items_for_cumulative_share(ranked["revenue_current"], 80.0)
            out.at[idx, "top_sku_share_pct"] = _clean_optional_float(float(shares.iloc[0] * 100.0))
            out.at[idx, "skus_for_80_pct"] = skus_for_80
            out.at[idx, "top_sku"] = top.get("product_id")
            out.at[idx, "top_sku_name"] = top.get("product_name") or top.get("product_id")

    return out


def _apply_v2_table_filters(
    frame: pd.DataFrame,
    *,
    search: str,
    quick_filter: str,
    segments: Sequence[str],
    protein_filter: str,
) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=list(frame.columns) if frame is not None else [])

    out = frame.copy()
    if search:
        token = search.lower()
        names = out["supplier_name"].astype("string").fillna("").str.lower()
        ids = out["supplier_id"].astype("string").fillna("").str.lower()
        out = out[names.str.contains(token, na=False) | ids.str.contains(token, na=False)]

    seg_tokens = {_normalize_segment_token(v) for v in segments if _normalize_segment_token(v)}
    if seg_tokens:
        out = out[out["segment_key"].isin(seg_tokens)]

    protein_token = str(protein_filter or "").strip().lower()
    if protein_token:
        def _protein_match(value: Any) -> bool:
            if isinstance(value, (list, tuple, set)):
                return any(str(item or "").strip().lower() == protein_token for item in value)
            return str(value or "").strip().lower() == protein_token

        families = out.get("protein_families")
        if families is not None:
            out = out[families.apply(_protein_match)]
        else:
            out = out[out.get("top_protein", pd.Series(dtype="object")).astype("string").str.lower() == protein_token]

    quick = _normalize_segment_token(quick_filter)
    if quick and quick != "all":
        if quick in {"strategic", "growth", "margin_risk", "data_risk", "long_tail"}:
            out = out[out["segment_key"] == quick]
        elif quick == "at_risk":
            out = out[out["at_risk_flag"] == 1]
        elif quick == "new":
            out = out[out["delta_revenue_status"] == "new"]
        elif quick == "lost":
            out = out[out["delta_revenue_status"] == "lost"]
        elif quick == "high_concentration":
            out = out[
                (pd.to_numeric(out.get("top_protein_share_pct"), errors="coerce") >= 60.0)
                | (pd.to_numeric(out.get("top_sku_share_pct"), errors="coerce") >= 35.0)
                | (pd.to_numeric(out.get("protein_hhi"), errors="coerce") >= 3000.0)
            ]
        elif quick == "missing_cost":
            out = out[
                (pd.to_numeric(out.get("missing_cost_revenue_current"), errors="coerce") > 0)
                | (pd.to_numeric(out.get("cost_coverage_pct"), errors="coerce") < 100.0)
            ]
        elif quick == "below_target_margin":
            out = out[
                (pd.to_numeric(out.get("revenue_current"), errors="coerce") > 0)
                & (
                    pd.to_numeric(out.get("margin_pct"), errors="coerce")
                    < pd.to_numeric(out.get("target_margin_pct"), errors="coerce")
                )
            ]

    return out


def _sort_v2_frame(frame: pd.DataFrame, sort_by: str, sort_dir: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=list(frame.columns) if frame is not None else [])

    out = frame.copy()
    col = sort_by if sort_by in out.columns else "revenue_current"
    asc = str(sort_dir or "DESC").upper() == "ASC"

    if col in {
        "supplier_name",
        "supplier_id",
        "segment_label",
        "segment_key",
        "risk_band",
        "delta_revenue_status",
        "top_protein",
    }:
        out["__sort__"] = out[col].astype("string").fillna("").str.lower()
    else:
        out["__sort__"] = pd.to_numeric(out[col], errors="coerce")

    out = out.sort_values(
        by=["__sort__", "supplier_name"],
        ascending=[asc, True],
        na_position="last",
        kind="mergesort",
    ).drop(columns=["__sort__"])
    return out


def _v2_row_payload(row: Mapping[str, Any]) -> Dict[str, Any]:
    supplier_id = row.get("supplier_id")
    supplier_name = row.get("supplier_name") or supplier_id or "Unknown Supplier"
    revenue = _clean_float(row.get("revenue_current"))
    revenue_prior = _clean_float(row.get("revenue_prior"))
    profit = _clean_optional_float(row.get("profit_current"))
    profit_prior = _clean_optional_float(row.get("profit_prior"))
    margin_pct = _clean_optional_float(row.get("margin_pct"))
    margin_prior = _clean_optional_float(row.get("margin_prior_pct"))
    row_out: Dict[str, Any] = {
        "supplier_id": supplier_id,
        "supplier_name": supplier_name,
        "key": supplier_id,
        "label": supplier_name,
        "revenue_current": revenue,
        "revenue_prior": revenue_prior,
        "delta_revenue": _clean_float(row.get("delta_revenue")),
        "delta_revenue_pct": _clean_optional_float(row.get("delta_revenue_pct")),
        "delta_revenue_pct_raw": _clean_optional_float(row.get("delta_revenue_pct_raw")),
        "delta_revenue_status": row.get("delta_revenue_status"),
        "delta_revenue_label": row.get("delta_revenue_label"),
        "low_base_warning": bool(row.get("low_base_warning")),
        "cost": _clean_optional_float(row.get("cost_current")),
        "profit": profit,
        "profit_prior": profit_prior,
        "delta_profit": _clean_optional_float(row.get("delta_profit")),
        "margin_pct": margin_pct,
        "margin_prior_pct": margin_prior,
        "delta_margin_pp": _clean_optional_float(row.get("delta_margin_pp")),
        "minimum_margin_pct": _clean_optional_float(row.get("minimum_margin_pct")),
        "target_margin_pct": _clean_optional_float(row.get("target_margin_pct")),
        "target_gap_pct_points": _clean_optional_float(row.get("target_gap_pct_points")),
        "target_status": row.get("target_status"),
        "status_key": row.get("status_key"),
        "status_color": row.get("status_color"),
        "roi_pct": _clean_optional_float(row.get("roi_pct")),
        "units": _clean_float(row.get("units_current")),
        "weight_lb": _clean_float(row.get("weight_current")),
        "avg_sale_price_per_lb": _clean_optional_float(row.get("avg_sale_price_per_lb")),
        "avg_cost_per_unit": _clean_optional_float(row.get("avg_cost_per_unit")),
        "avg_cost_per_lb": _clean_optional_float(row.get("avg_cost_per_lb")),
        "contribution_per_lb": _clean_optional_float(row.get("contribution_per_lb")),
        "products": _clean_int(row.get("products_current")),
        "orders": _clean_int(row.get("orders_current")),
        "orders_prior": _clean_int(row.get("orders_prior")),
        "customers": _clean_int(row.get("customers_current")),
        "aov": _clean_optional_float(row.get("aov")),
        "first_order_date": _date_to_iso(row.get("first_order_date")),
        "last_order_date": _date_to_iso(row.get("last_order_date")),
        "last_sold": _date_to_iso(row.get("last_order_date")),
        "days_since_last_order": _clean_int(row.get("days_since_last_order"), default=-1),
        "risk_band": row.get("risk_band") or "Unknown",
        "at_risk_flag": _clean_int(row.get("at_risk_flag"), default=0),
        "segment_label": row.get("segment_label") or "Long tail",
        "segment_key": row.get("segment_key") or "long_tail",
        "segment_reason": row.get("segment_reason"),
        "action_bucket": row.get("action_bucket"),
        "missing_cost_pct": _clean_optional_float(row.get("missing_cost_pct")),
        "cost_coverage_pct": _clean_optional_float(row.get("cost_coverage_pct")),
        "cost_coverage_row_pct": _clean_optional_float(row.get("cost_coverage_row_pct")),
        "cost_covered_revenue_current": _clean_float(row.get("cost_covered_revenue_current")),
        "cost_covered_revenue_prior": _clean_float(row.get("cost_covered_revenue_prior")),
        "missing_cost_revenue_current": _clean_float(row.get("missing_cost_revenue_current")),
        "missing_cost_revenue_prior": _clean_float(row.get("missing_cost_revenue_prior")),
        "proteins": _clean_int(row.get("proteins_current")),
        "categories": _clean_int(row.get("categories_current")),
        "top_protein": row.get("top_protein") or "Unassigned",
        "top_category": row.get("top_category") or row.get("top_protein") or "Unassigned",
        "top_protein_share_pct": _clean_optional_float(row.get("top_protein_share_pct")),
        "protein_family_count": _clean_int(row.get("protein_family_count")),
        "protein_hhi": _clean_optional_float(row.get("protein_hhi")),
        "protein_dependency_posture": row.get("protein_dependency_posture") or "Balanced",
        "margin_risk_family": row.get("margin_risk_family"),
        "growth_family": row.get("growth_family"),
        "top_sku_share_pct": _clean_optional_float(row.get("top_sku_share_pct")),
        "skus_for_80_pct": _clean_int(row.get("skus_for_80_pct")),
        "top_sku": row.get("top_sku"),
        "top_sku_name": row.get("top_sku_name"),
        "rows_current": _clean_int(row.get("rows_current")),
        "cost_missing_rows_current": _clean_int(row.get("cost_missing_rows_current")),
        # Legacy aliases expected by v1 template/JS.
        "SupplierId": supplier_id,
        "SupplierName": supplier_name,
        "Revenue": revenue,
        "RevenuePriorWindow": revenue_prior,
        "DeltaRevenue": _clean_float(row.get("delta_revenue")),
        "DeltaRevenuePct": _clean_optional_float(row.get("delta_revenue_pct")),
        "DeltaRevenueStatus": row.get("delta_revenue_status"),
        "DeltaRevenueLabel": row.get("delta_revenue_label"),
        "Cost": _clean_optional_float(row.get("cost_current")),
        "Profit": profit,
        "ProfitPrior": profit_prior,
        "DeltaProfit": _clean_optional_float(row.get("delta_profit")),
        "MarginPct": margin_pct,
        "MarginPriorPct": margin_prior,
        "DeltaMarginPct": _clean_optional_float(row.get("delta_margin_pp")),
        "ROIPct": _clean_optional_float(row.get("roi_pct")),
        "Units": _clean_float(row.get("units_current")),
        "WeightLb": _clean_float(row.get("weight_current")),
        "AvgSalePricePerLb": _clean_optional_float(row.get("avg_sale_price_per_lb")),
        "AvgCostPerUnit": _clean_optional_float(row.get("avg_cost_per_unit")),
        "AvgCostPerLb": _clean_optional_float(row.get("avg_cost_per_lb")),
        "ContributionPerLb": _clean_optional_float(row.get("contribution_per_lb")),
        "Products": _clean_int(row.get("products_current")),
        "Orders": _clean_int(row.get("orders_current")),
        "OrdersPrior": _clean_int(row.get("orders_prior")),
        "Customers": _clean_int(row.get("customers_current")),
        "AOV": _clean_optional_float(row.get("aov")),
        "FirstOrderDate": _date_to_iso(row.get("first_order_date")),
        "LastOrderDate": _date_to_iso(row.get("last_order_date")),
        "LastSold": _date_to_iso(row.get("last_order_date")),
        "DaysSinceLastOrder": _clean_int(row.get("days_since_last_order"), default=-1),
        "RiskBand": row.get("risk_band") or "Unknown",
        "SegmentLabel": row.get("segment_label") or "Long tail",
        "MissingCostPct": _clean_optional_float(row.get("missing_cost_pct")),
        "CostCoveragePct": _clean_optional_float(row.get("cost_coverage_pct")),
        "MissingCostRevenue": _clean_float(row.get("missing_cost_revenue_current")),
        "CostCoveredRevenue": _clean_float(row.get("cost_covered_revenue_current")),
        "TopProtein": row.get("top_protein") or "Unassigned",
        "TopProteinSharePct": _clean_optional_float(row.get("top_protein_share_pct")),
        "TopSkuSharePct": _clean_optional_float(row.get("top_sku_share_pct")),
        "ProteinFamilyCount": _clean_int(row.get("protein_family_count")),
        "ProteinDependencyPosture": row.get("protein_dependency_posture") or "Balanced",
    }
    return row_out


def _v2_table_payload_from_frame(
    frame: pd.DataFrame,
    args: Any,
    *,
    paginate: bool,
) -> tuple[Dict[str, Any], pd.DataFrame]:
    page_num, page_size = _pagination(args)
    sort_by, sort_dir = _sort_params(args)
    search = _extract_search(args)
    quick_filter = _parse_quick_filter(args)
    segments = _parse_segment_filters(args)
    protein_filter = _parse_protein_filter(args)
    export_all = _parse_bool_arg(args, ("export_all", "all_rows"), default=False)

    filtered = _apply_v2_table_filters(
        frame,
        search=search,
        quick_filter=quick_filter,
        segments=segments,
        protein_filter=protein_filter,
    )
    sorted_df = _sort_v2_frame(filtered, sort_by, sort_dir)

    total_rows = int(len(sorted_df.index))
    if paginate and not export_all:
        offset = (page_num - 1) * page_size
        page_df = sorted_df.iloc[offset : offset + page_size]
    else:
        page_num = 1
        page_df = sorted_df

    rows = [_v2_row_payload(rec) for rec in page_df.to_dict(orient="records")]
    payload = {
        "rows": rows,
        "page": page_num,
        "page_size": page_size,
        "total": total_rows,
        "total_rows": total_rows,
        "sort_by": sort_by,
        "sort_dir": sort_dir.lower(),
        "search": search,
        "quick_filter": quick_filter,
        "protein_filter": protein_filter,
        "segments": list(segments),
        "summary": _table_summary_from_frame(sorted_df),
    }
    return payload, sorted_df


def _segment_summary_from_frame(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    grouped = (
        frame.groupby("segment_label", dropna=False)
        .agg(
            suppliers=("supplier_id", "nunique"),
            revenue=("revenue_current", "sum"),
            revenue_prior=("revenue_prior", "sum"),
            profit=("profit_current", "sum"),
            avg_margin_pct=("margin_pct", "mean"),
            avg_orders=("orders_current", "mean"),
            median_days_since_last=("days_since_last_order", "median"),
            cost_coverage_pct=("cost_coverage_pct", "mean"),
            top_examples=("supplier_name", lambda s: ", ".join([str(v) for v in s.head(3).tolist() if str(v).strip()])),
        )
        .reset_index()
    )
    total_revenue = float(grouped["revenue"].sum()) if not grouped.empty else 0.0
    grouped["share_pct"] = (grouped["revenue"] / total_revenue * 100.0) if total_revenue > 0 else 0.0
    grouped["delta_revenue"] = grouped["revenue"] - grouped["revenue_prior"]
    grouped["delta_revenue_pct"] = ((grouped["delta_revenue"] / grouped["revenue_prior"]) * 100.0).where(
        grouped["revenue_prior"] > 0
    )
    order_map = {
        "Strategic": 1,
        "Growth": 2,
        "Margin risk": 3,
        "Data risk": 4,
        "Long tail": 5,
    }
    grouped["segment_order"] = grouped["segment_label"].map(order_map).fillna(99)
    grouped = grouped.sort_values(["segment_order", "revenue"], ascending=[True, False])
    key_map = {
        "Strategic": "strategic",
        "Growth": "growth",
        "Margin risk": "margin_risk",
        "Data risk": "data_risk",
        "Long tail": "long_tail",
    }
    rows: list[dict[str, Any]] = []
    for _, rec in grouped.iterrows():
        rows.append(
            {
                "segment": rec.get("segment_label"),
                "segment_key": key_map.get(rec.get("segment_label"), "long_tail"),
                "suppliers": _clean_int(rec.get("suppliers")),
                "revenue": _clean_float(rec.get("revenue")),
                "revenue_prior": _clean_float(rec.get("revenue_prior")),
                "delta_revenue": _clean_float(rec.get("delta_revenue")),
                "delta_revenue_pct": _clean_optional_float(rec.get("delta_revenue_pct")),
                "profit": _clean_optional_float(rec.get("profit")),
                "avg_margin_pct": _clean_optional_float(rec.get("avg_margin_pct")),
                "avg_orders": _clean_optional_float(rec.get("avg_orders")),
                "median_days_since_last": _clean_optional_float(rec.get("median_days_since_last")),
                "cost_coverage_pct": _clean_optional_float(rec.get("cost_coverage_pct")),
                "examples": rec.get("top_examples"),
                "share_pct": _clean_optional_float(rec.get("share_pct")),
            }
        )
    return rows


def _movers_payload_from_frame(frame: pd.DataFrame) -> Dict[str, Any]:
    if frame is None or frame.empty:
        return {"rows": [], "top_gainers": [], "top_decliners": [], "profit_movers": [], "margin_decliners": []}
    cols = [
        "supplier_id",
        "supplier_name",
        "revenue_current",
        "revenue_prior",
        "delta_revenue",
        "delta_revenue_pct",
        "delta_revenue_status",
        "delta_revenue_label",
        "segment_label",
        "margin_pct",
        "delta_profit",
        "delta_margin_pp",
        "top_protein",
    ]
    movers = frame.loc[:, [c for c in cols if c in frame.columns]].copy()
    movers = movers.sort_values("delta_revenue", ascending=False, na_position="last")

    def _rows(df: pd.DataFrame) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            out.append(
                {
                    "supplier_id": row.get("supplier_id"),
                    "supplier_name": row.get("supplier_name"),
                    "revenue_current": _clean_float(row.get("revenue_current")),
                    "revenue_prior": _clean_float(row.get("revenue_prior")),
                    "delta_revenue": _clean_float(row.get("delta_revenue")),
                    "delta_revenue_pct": _clean_optional_float(row.get("delta_revenue_pct")),
                    "delta_revenue_status": row.get("delta_revenue_status"),
                    "delta_revenue_label": row.get("delta_revenue_label"),
                    "segment_label": row.get("segment_label"),
                    "margin_pct": _clean_optional_float(row.get("margin_pct")),
                    "delta_profit": _clean_optional_float(row.get("delta_profit")),
                    "delta_margin_pp": _clean_optional_float(row.get("delta_margin_pp")),
                    "top_protein": row.get("top_protein") or "Unassigned",
                }
            )
        return out

    top_gainers_df = movers.sort_values("delta_revenue", ascending=False, na_position="last").head(10)
    top_decliners_df = movers.sort_values("delta_revenue", ascending=True, na_position="last").head(10)
    profit_movers_df = movers.sort_values("delta_profit", ascending=False, na_position="last").head(10)
    margin_decliners_df = movers.sort_values("delta_margin_pp", ascending=True, na_position="last").head(10)
    return {
        "rows": _rows(movers),
        "top_gainers": _rows(top_gainers_df),
        "top_decliners": _rows(top_decliners_df),
        "profit_movers": _rows(profit_movers_df),
        "margin_decliners": _rows(margin_decliners_df),
    }


def _risk_payload_from_frame(frame: pd.DataFrame) -> Dict[str, Any]:
    if frame is None or frame.empty:
        return {"margin_leakage": [], "data_risk": [], "summary": {}}

    scoped_frame = frame.copy()
    if "cost_current" in scoped_frame.columns:
        scoped_frame["effective_cost_basis"] = pd.to_numeric(scoped_frame.get("cost_current"), errors="coerce")
    scoped_frame = margin_rules.annotate_margin_frame(
        scoped_frame,
        protein_col="top_protein",
        category_col="top_protein",
        revenue_col="revenue_current",
        cost_col="cost_current" if "cost_current" in frame.columns else "profit_current",
        profit_col="profit_current",
        margin_col="margin_pct",
    )
    weighted_target_margin_pct = margin_rules.weighted_target_margin_pct(
        scoped_frame.to_dict(orient="records"),
        revenue_key="revenue_current",
    )
    margin_target_pct = float(weighted_target_margin_pct) if weighted_target_margin_pct is not None else None
    margin_df = scoped_frame.copy()
    margin_df = margin_df[
        (pd.to_numeric(margin_df["revenue_current"], errors="coerce") > 0)
        & (pd.to_numeric(margin_df["margin_pct"], errors="coerce") < pd.to_numeric(margin_df["target_margin_pct"], errors="coerce"))
    ].copy()
    margin_df["profit_uplift_target"] = pd.to_numeric(margin_df.get("profit_uplift_to_target"), errors="coerce").fillna(0.0)
    margin_df = margin_df.sort_values(["profit_uplift_target", "revenue_current"], ascending=[False, False])

    data_risk_df = frame.copy()
    data_risk_df = data_risk_df[pd.to_numeric(data_risk_df["missing_cost_pct"], errors="coerce") >= 10.0].copy()
    data_risk_df = data_risk_df.sort_values(["missing_cost_revenue_current", "missing_cost_pct"], ascending=[False, False])

    def _margin_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            out.append(
                {
                    "supplier_id": row.get("supplier_id"),
                    "supplier_name": row.get("supplier_name"),
                    "revenue": _clean_float(row.get("revenue_current")),
                    "profit": _clean_optional_float(row.get("profit_current")),
                    "margin_pct": _clean_optional_float(row.get("margin_pct")),
                    "target_margin_pct": _clean_optional_float(row.get("target_margin_pct")),
                    "profit_uplift_target": _clean_optional_float(row.get("profit_uplift_target")),
                    "top_protein": row.get("top_protein") or "Unassigned",
                    "status_key": row.get("status_key"),
                    "target_status": row.get("target_status"),
                }
            )
        return out

    def _risk_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            out.append(
                {
                    "supplier_id": row.get("supplier_id"),
                    "supplier_name": row.get("supplier_name"),
                    "revenue": _clean_float(row.get("revenue_current")),
                    "missing_cost_revenue": _clean_float(row.get("missing_cost_revenue_current")),
                    "missing_cost_pct": _clean_optional_float(row.get("missing_cost_pct")),
                    "cost_coverage_pct": _clean_optional_float(row.get("cost_coverage_pct")),
                    "missing_rows": _clean_int(row.get("cost_missing_rows_current")),
                    "rows_current": _clean_int(row.get("rows_current")),
                    "top_protein": row.get("top_protein") or "Unassigned",
                }
            )
        return out

    margin_rows = _margin_rows(margin_df)
    data_risk_rows = _risk_rows(data_risk_df)
    total_uplift = sum(_clean_float(r.get("profit_uplift_target")) for r in margin_rows)
    missing_cost_revenue = _clean_float(
        pd.to_numeric(frame.get("missing_cost_revenue_current"), errors="coerce").fillna(0.0).sum()
    ) if frame is not None and not frame.empty else 0.0
    return {
        "margin_leakage": margin_rows,
        "data_risk": data_risk_rows,
        "summary": {
            "margin_risk_suppliers": len(margin_rows),
            "data_risk_suppliers": len(data_risk_rows),
            "target_margin_pct": margin_target_pct,
            "profit_uplift_target": total_uplift,
            "margin_risk_revenue": _clean_float(pd.to_numeric(margin_df["revenue_current"], errors="coerce").fillna(0.0).sum()) if not margin_df.empty else 0.0,
            "missing_cost_revenue": missing_cost_revenue,
        },
    }


def _concentration_payload_from_frame(frame: pd.DataFrame) -> Dict[str, Any]:
    if frame is None or frame.empty:
        return {
            "hhi": None,
            "top1_share": None,
            "top5_share": None,
            "top10_share": None,
            "profit_hhi": None,
            "profit_top1_share": None,
            "profit_top5_share": None,
            "profit_top10_share": None,
            "suppliers_for_80_pct": None,
            "level": "unknown",
            "pareto_rows": [],
        }
    conc = frame.copy()
    conc = conc[pd.to_numeric(conc["revenue_current"], errors="coerce") > 0].copy()
    if conc.empty:
        return {
            "hhi": None,
            "top1_share": None,
            "top5_share": None,
            "top10_share": None,
            "profit_hhi": None,
            "profit_top1_share": None,
            "profit_top5_share": None,
            "profit_top10_share": None,
            "suppliers_for_80_pct": None,
            "level": "unknown",
            "pareto_rows": [],
        }
    conc = conc.sort_values("revenue_current", ascending=False).reset_index(drop=True)
    total_revenue = float(conc["revenue_current"].sum())
    if total_revenue <= 0:
        return {
            "hhi": None,
            "top1_share": None,
            "top5_share": None,
            "top10_share": None,
            "profit_hhi": None,
            "profit_top1_share": None,
            "profit_top5_share": None,
            "profit_top10_share": None,
            "suppliers_for_80_pct": None,
            "level": "unknown",
            "pareto_rows": [],
        }

    conc["share_pct"] = (conc["revenue_current"] / total_revenue) * 100.0
    conc["cumulative_share_pct"] = conc["share_pct"].cumsum()
    hhi = _hhi_from_values(conc["revenue_current"])
    top1_share = _top_n_share_pct(conc["revenue_current"], 1)
    top5_share = _top_n_share_pct(conc["revenue_current"], 5)
    top10_share = _top_n_share_pct(conc["revenue_current"], 10)
    suppliers_for_80_pct = _items_for_cumulative_share(conc["revenue_current"], 80.0)
    positive_profit = pd.to_numeric(conc.get("profit_current"), errors="coerce").fillna(0.0)
    positive_profit = positive_profit[positive_profit > 0]
    profit_hhi = _hhi_from_values(positive_profit)
    profit_top1_share = _top_n_share_pct(positive_profit, 1)
    profit_top5_share = _top_n_share_pct(positive_profit, 5)
    profit_top10_share = _top_n_share_pct(positive_profit, 10)

    if (hhi or 0.0) >= 2500:
        level = "high"
    elif (hhi or 0.0) >= 1500:
        level = "medium"
    else:
        level = "low"

    pareto_rows: list[dict[str, Any]] = []
    for idx, row in conc.head(30).iterrows():
        pareto_rows.append(
            {
                "rank": _clean_int(idx + 1),
                "supplier_id": row.get("supplier_id"),
                "supplier_name": row.get("supplier_name"),
                "revenue": _clean_float(row.get("revenue_current")),
                "share_pct": _clean_optional_float(row.get("share_pct")),
                "cumulative_share_pct": _clean_optional_float(row.get("cumulative_share_pct")),
            }
        )

    return {
        "hhi": hhi,
        "top1_share": top1_share,
        "top5_share": top5_share,
        "top10_share": top10_share,
        "profit_hhi": profit_hhi,
        "profit_top1_share": profit_top1_share,
        "profit_top5_share": profit_top5_share,
        "profit_top10_share": profit_top10_share,
        "suppliers_for_80_pct": suppliers_for_80_pct,
        "level": level,
        "pareto_rows": pareto_rows,
    }


def _safe_ratio_value(numerator: Any, denominator: Any, *, pct: bool = False) -> float | None:
    num = _clean_optional_float(numerator)
    den = _clean_optional_float(denominator)
    if num is None or den in (None, 0):
        return None
    ratio = num / den
    return ratio * 100.0 if pct else ratio


def _top_n_share_pct(values: pd.Series | Sequence[Any], top_n: int) -> float | None:
    series = pd.to_numeric(pd.Series(values), errors="coerce").fillna(0.0)
    series = series[series > 0].sort_values(ascending=False).reset_index(drop=True)
    if series.empty:
        return None
    total = float(series.sum())
    if total <= 0:
        return None
    return float(series.head(max(1, int(top_n))).sum() / total * 100.0)


def _hhi_from_values(values: pd.Series | Sequence[Any]) -> float | None:
    series = pd.to_numeric(pd.Series(values), errors="coerce").fillna(0.0)
    series = series[series > 0]
    if series.empty:
        return None
    total = float(series.sum())
    if total <= 0:
        return None
    shares = series / total
    return float((shares.pow(2).sum()) * 10000.0)


def _items_for_cumulative_share(values: pd.Series | Sequence[Any], target_pct: float = 80.0) -> int:
    series = pd.to_numeric(pd.Series(values), errors="coerce").fillna(0.0)
    series = series[series > 0].sort_values(ascending=False).reset_index(drop=True)
    if series.empty:
        return 0
    total = float(series.sum())
    if total <= 0:
        return 0
    cumulative = ((series / total) * 100.0).cumsum()
    reached = cumulative >= float(target_pct)
    if bool(reached.any()):
        return int(reached.idxmax()) + 1
    return int(len(series.index))


def _protein_payload_from_frame(
    frame: pd.DataFrame,
    protein_df: pd.DataFrame,
) -> Dict[str, Any]:
    if protein_df is None or protein_df.empty:
        return {
            "summary": {
                "family_count": 0,
                "top_family": None,
                "top_family_share_pct": None,
                "concentration_hhi": None,
                "top_supplier": None,
            },
            "mix": [],
            "mix_shift": [],
            "margin_watch": [],
            "supplier_dependency": [],
            "narrative": "Protein intelligence is unavailable for the current supplier scope.",
        }

    mix = protein_df.copy()
    for col in (
        "revenue_current",
        "revenue_prior",
        "weight_current",
        "cost_covered_revenue_current",
        "cost_covered_revenue_prior",
        "cost_current",
        "cost_prior",
        "sku_count",
        "customer_count",
        "order_count",
    ):
        mix[col] = pd.to_numeric(mix.get(col), errors="coerce").fillna(0.0)
    mix["profit_current"] = (mix["cost_covered_revenue_current"] - mix["cost_current"]).where(mix["cost_covered_revenue_current"] > 0)
    mix["margin_pct"] = ((mix["profit_current"] / mix["cost_covered_revenue_current"]) * 100.0).where(
        mix["cost_covered_revenue_current"] > 0
    )
    mix["effective_cost_basis"] = pd.to_numeric(mix.get("cost_current"), errors="coerce")
    mix = margin_rules.annotate_margin_frame(
        mix,
        protein_col="protein_family",
        category_col="product_category",
        revenue_col="revenue_current",
        cost_col="cost_current",
        profit_col="profit_current",
        margin_col="margin_pct",
    )
    mix["share_current"] = pd.Series(dtype="float64")
    mix["share_prior"] = pd.Series(dtype="float64")
    mix["share_delta_pp"] = pd.Series(dtype="float64")

    category_rank = (
        mix.groupby(["protein_family", "product_category"], dropna=False)
        .agg(revenue=("revenue_current", "sum"))
        .reset_index()
        .sort_values(["protein_family", "revenue"], ascending=[True, False], na_position="last")
        .groupby("protein_family", dropna=False)
        .head(1)
        .loc[:, ["protein_family", "product_category"]]
        .rename(columns={"product_category": "lead_category"})
    )

    family = (
        mix.groupby(["protein_family"], dropna=False)
        .agg(
            revenue=("revenue_current", "sum"),
            revenue_prior=("revenue_prior", "sum"),
            weight_lb=("weight_current", "sum"),
            cost_covered_revenue=("cost_covered_revenue_current", "sum"),
            profit=("profit_current", "sum"),
            sku_count=("sku_count", "sum"),
            customer_count=("customer_count", "sum"),
            supplier_count=("supplier_id", "nunique"),
        )
        .reset_index()
    )
    family = family.merge(category_rank, on="protein_family", how="left")
    total_revenue = float(pd.to_numeric(family["revenue"], errors="coerce").fillna(0.0).sum())
    total_revenue_prior = float(pd.to_numeric(family["revenue_prior"], errors="coerce").fillna(0.0).sum())
    if total_revenue > 0:
        family["share_current"] = family["revenue"] / total_revenue * 100.0
    else:
        family["share_current"] = 0.0
    if total_revenue_prior > 0:
        family["share_prior"] = family["revenue_prior"] / total_revenue_prior * 100.0
    else:
        family["share_prior"] = 0.0
    family["share_delta_pp"] = family["share_current"] - family["share_prior"]
    family["margin_pct"] = ((family["profit"] / family["cost_covered_revenue"]) * 100.0).where(
        family["cost_covered_revenue"] > 0
    )
    family["cost"] = pd.to_numeric(family["revenue"], errors="coerce") - pd.to_numeric(family["profit"], errors="coerce")
    family["effective_cost_basis"] = pd.to_numeric(family.get("cost"), errors="coerce")
    family = margin_rules.annotate_margin_frame(
        family,
        protein_col="protein_family",
        category_col="lead_category",
        revenue_col="revenue",
        cost_col="cost",
        profit_col="profit",
        margin_col="margin_pct",
    )

    supplier_rank = (
        mix.sort_values(["protein_family", "revenue_current"], ascending=[True, False], na_position="last")
        .groupby("protein_family", dropna=False)
        .head(1)
        .loc[:, ["protein_family", "supplier_id", "supplier_name", "revenue_current"]]
        .rename(columns={"revenue_current": "top_supplier_revenue"})
    )
    family = family.merge(supplier_rank, on="protein_family", how="left")
    family["top_supplier_share_pct"] = family.apply(
        lambda row: _safe_ratio_value(row.get("top_supplier_revenue"), row.get("revenue"), pct=True), axis=1
    )
    family["below_target_supplier_count"] = family["protein_family"].map(
        mix[
            (pd.to_numeric(mix["revenue_current"], errors="coerce") > 0)
            & (
                pd.to_numeric(mix["margin_pct"], errors="coerce")
                < pd.to_numeric(mix["target_margin_pct"], errors="coerce")
            )
        ]
        .groupby("protein_family", dropna=False)["supplier_id"]
        .nunique()
        .to_dict()
    ).fillna(0).astype(int)

    family_sorted = family.sort_values(["revenue", "profit"], ascending=[False, False], na_position="last")
    top_family = family_sorted.iloc[0].to_dict() if not family_sorted.empty else {}
    concentration_hhi = (
        float((((family_sorted["revenue"] / total_revenue) ** 2).sum()) * 10000.0)
        if total_revenue > 0 and not family_sorted.empty
        else None
    )

    mix_rows = []
    for _, row in family_sorted.head(8).iterrows():
        mix_rows.append(
            {
                "family": row.get("protein_family") or "Unassigned",
                "category": row.get("lead_category") or row.get("protein_family") or "Unassigned",
                "revenue": _clean_float(row.get("revenue")),
                "revenue_prior": _clean_float(row.get("revenue_prior")),
                "profit": _clean_optional_float(row.get("profit")),
                "weight_lb": _clean_float(row.get("weight_lb")),
                "margin_pct": _clean_optional_float(row.get("margin_pct")),
                "minimum_margin_pct": _clean_optional_float(row.get("minimum_margin_pct")),
                "target_margin_pct": _clean_optional_float(row.get("target_margin_pct")),
                "target_status": row.get("target_status"),
                "status_key": row.get("status_key"),
                "share_current": _clean_optional_float(row.get("share_current")),
                "share_prior": _clean_optional_float(row.get("share_prior")),
                "share_delta_pp": _clean_optional_float(row.get("share_delta_pp")),
                "supplier_count": _clean_int(row.get("supplier_count")),
                "sku_count": _clean_int(row.get("sku_count")),
                "customer_count": _clean_int(row.get("customer_count")),
                "top_supplier_id": row.get("supplier_id"),
                "top_supplier_name": row.get("supplier_name"),
                "top_supplier_share_pct": _clean_optional_float(row.get("top_supplier_share_pct")),
            }
        )

    mix_shift = [
        {
            "family": row.get("protein_family") or "Unassigned",
            "category": row.get("lead_category") or row.get("protein_family") or "Unassigned",
            "revenue": _clean_float(row.get("revenue")),
            "share_current": _clean_optional_float(row.get("share_current")),
            "share_prior": _clean_optional_float(row.get("share_prior")),
            "share_delta_pp": _clean_optional_float(row.get("share_delta_pp")),
        }
        for _, row in family.reindex(family["share_delta_pp"].abs().sort_values(ascending=False).index).head(6).iterrows()
    ]

    margin_watch = [
        {
            "family": row.get("protein_family") or "Unassigned",
            "category": row.get("lead_category") or row.get("protein_family") or "Unassigned",
            "revenue": _clean_float(row.get("revenue")),
            "margin_pct": _clean_optional_float(row.get("margin_pct")),
            "minimum_margin_pct": _clean_optional_float(row.get("minimum_margin_pct")),
            "target_margin_pct": _clean_optional_float(row.get("target_margin_pct")),
            "target_status": row.get("target_status"),
            "status_key": row.get("status_key"),
            "share_current": _clean_optional_float(row.get("share_current")),
            "below_target_supplier_count": _clean_int(row.get("below_target_supplier_count")),
            "top_supplier_name": row.get("supplier_name"),
        }
        for _, row in family.sort_values(["margin_pct", "revenue"], ascending=[True, False], na_position="last").head(6).iterrows()
    ]

    supplier_dependency = []
    if frame is not None and not frame.empty:
        dep = frame.copy()
        dep = dep[pd.to_numeric(dep["revenue_current"], errors="coerce") > 0]
        dep = dep.sort_values(["top_protein_share_pct", "revenue_current"], ascending=[False, False], na_position="last")
        for _, row in dep.head(8).iterrows():
            supplier_dependency.append(
                {
                    "supplier_id": row.get("supplier_id"),
                    "supplier_name": row.get("supplier_name"),
                    "family": row.get("top_protein") or "Unassigned",
                    "share_pct": _clean_optional_float(row.get("top_protein_share_pct")),
                    "revenue": _clean_float(row.get("revenue_current")),
                    "posture": row.get("protein_dependency_posture") or "Balanced",
                    "margin_risk_family": row.get("margin_risk_family"),
                }
            )

    positive_shift = next((row for row in mix_shift if _clean_float(row.get("share_delta_pp")) > 0), None)
    negative_shift = next(
        (item for item in sorted(mix_shift, key=lambda item: _clean_float(item.get("share_delta_pp"))) if _clean_float(item.get("share_delta_pp")) < 0),
        None,
    )
    strongest_dependency = next(
        (row for row in supplier_dependency if _clean_float(row.get("share_pct")) >= 60.0),
        supplier_dependency[0] if supplier_dependency else None,
    )
    focus_cards = [
        {
            "key": "top_family",
            "tone": "premium",
            "label": "Largest protein family",
            "value": top_family.get("protein_family") or "Unassigned",
            "note": (
                f"{float(top_family.get('share_current') or 0.0):.1f}% of supplier revenue · "
                f"top supplier {top_family.get('supplier_name') or 'n/a'}"
            ),
        },
        {
            "key": "mix_shift",
            "tone": "growth" if positive_shift else "watch",
            "label": "Largest mix move",
            "value": (positive_shift or negative_shift or {}).get("family") or "Stable mix",
            "note": (
                f"Mix {'up' if _clean_float((positive_shift or negative_shift or {}).get('share_delta_pp')) >= 0 else 'down'} "
                f"{abs(_clean_float((positive_shift or negative_shift or {}).get('share_delta_pp'))):.1f} pp"
                if (positive_shift or negative_shift)
                else "No material mix shift in the visible protein set."
            ),
        },
        {
            "key": "margin_watch",
            "tone": "risk",
            "label": "Margin watch",
            "value": (margin_watch[0] if margin_watch else {}).get("family") or "No family flagged",
            "note": (
                f"{_clean_int((margin_watch[0] if margin_watch else {}).get('below_target_supplier_count'))} suppliers below target margin"
                if margin_watch
                else "No protein-family margin pressure detected."
            ),
        },
        {
            "key": "dependency",
            "tone": "focus",
            "label": "Dependency watch",
            "value": (strongest_dependency or {}).get("supplier_name") or "Balanced mix",
            "note": (
                f"{_clean_float((strongest_dependency or {}).get('share_pct')):.1f}% tied to "
                f"{(strongest_dependency or {}).get('family') or 'the top family'}"
                if strongest_dependency
                else "No supplier is materially dependent on a single protein family."
            ),
        },
    ]

    narrative = (
        f"{(top_family.get('protein_family') or 'Top protein')} represents "
        f"{float(top_family.get('share_current') or 0.0):.1f}% of supplier revenue in scope. "
        f"{len(margin_watch)} protein families are carrying margin pressure and "
        f"{len([row for row in supplier_dependency if (row.get('share_pct') or 0.0) >= 60.0])} suppliers are highly protein-dependent."
    )

    return {
        "summary": {
            "family_count": int(family["protein_family"].astype("string").replace("", pd.NA).dropna().nunique()),
            "top_family": top_family.get("protein_family"),
            "top_family_share_pct": _clean_optional_float(top_family.get("share_current")),
            "concentration_hhi": _clean_optional_float(concentration_hhi),
            "top_supplier": top_family.get("supplier_name"),
        },
        "mix": mix_rows,
        "mix_shift": mix_shift,
        "margin_watch": margin_watch,
        "supplier_dependency": supplier_dependency,
        "focus_cards": focus_cards,
        "narrative": narrative,
    }


def _dependency_payload_from_frame(frame: pd.DataFrame, concentration: Mapping[str, Any]) -> Dict[str, Any]:
    if frame is None or frame.empty:
        return {"summary": {}, "concentrated_suppliers": []}
    working = frame.copy()
    working = working[pd.to_numeric(working["revenue_current"], errors="coerce") > 0]
    concentrated = working[
        (pd.to_numeric(working["top_protein_share_pct"], errors="coerce") >= 60.0)
        | (pd.to_numeric(working["top_sku_share_pct"], errors="coerce") >= 35.0)
    ].copy()
    concentrated = concentrated.sort_values(["revenue_current", "top_protein_share_pct"], ascending=[False, False], na_position="last")
    diversified = working[
        (pd.to_numeric(working.get("protein_family_count"), errors="coerce") >= 3.0)
        & (pd.to_numeric(working.get("top_protein_share_pct"), errors="coerce") < 50.0)
    ].copy()
    high_dependency_suppliers = int(concentrated["supplier_id"].nunique()) if not concentrated.empty else 0
    rows = [
        {
            "supplier_id": row.get("supplier_id"),
            "supplier_name": row.get("supplier_name"),
            "revenue": _clean_float(row.get("revenue_current")),
            "top_protein": row.get("top_protein") or "Unassigned",
            "top_protein_share_pct": _clean_optional_float(row.get("top_protein_share_pct")),
            "top_sku_share_pct": _clean_optional_float(row.get("top_sku_share_pct")),
            "posture": row.get("protein_dependency_posture") or "Balanced",
            "segment_label": row.get("segment_label"),
        }
        for _, row in concentrated.head(8).iterrows()
    ]
    return {
        "summary": {
            "hhi": concentration.get("hhi"),
            "top1_share": concentration.get("top1_share"),
            "top5_share": concentration.get("top5_share"),
            "top10_share": concentration.get("top10_share"),
            "profit_hhi": concentration.get("profit_hhi"),
            "profit_top1_share": concentration.get("profit_top1_share"),
            "profit_top5_share": concentration.get("profit_top5_share"),
            "profit_top10_share": concentration.get("profit_top10_share"),
            "suppliers_for_80_pct": concentration.get("suppliers_for_80_pct"),
            "high_dependency_suppliers": high_dependency_suppliers,
            "diversified_suppliers": int(diversified["supplier_id"].nunique()) if not diversified.empty else 0,
            "median_protein_family_count": _clean_optional_float(
                pd.to_numeric(working.get("protein_family_count"), errors="coerce").median()
            ),
        },
        "concentrated_suppliers": rows,
    }


def _actions_payload_from_frame(frame: pd.DataFrame, risk_payload: Mapping[str, Any]) -> Dict[str, Any]:
    if frame is None or frame.empty:
        return {"cards": []}

    def _bucket(label: str, title: str, quick_filter: str, subset: pd.DataFrame, note: str) -> dict[str, Any]:
        revenue = float(pd.to_numeric(subset.get("revenue_current"), errors="coerce").fillna(0.0).sum()) if not subset.empty else 0.0
        profit = pd.to_numeric(subset.get("profit_current"), errors="coerce").sum(min_count=1) if not subset.empty else None
        examples = ", ".join([str(v) for v in subset["supplier_name"].head(3).tolist() if str(v).strip()]) if not subset.empty else ""
        return {
            "key": quick_filter,
            "label": label,
            "title": title,
            "supplier_count": int(subset["supplier_id"].nunique()) if not subset.empty else 0,
            "revenue": _clean_float(revenue),
            "profit": _clean_optional_float(profit),
            "quick_filter": quick_filter,
            "note": note,
            "examples": examples,
            "tone": {
                "strategic": "healthy",
                "below_target_margin": "risk",
                "growth": "growth",
                "missing_cost": "watch",
                "long_tail": "neutral",
            }.get(quick_filter, "neutral"),
        }

    strategic = frame[frame["segment_key"] == "strategic"]
    growth = frame[frame["segment_key"] == "growth"]
    margin_risk = frame[
        (
            pd.to_numeric(frame["margin_pct"], errors="coerce")
            < pd.to_numeric(frame.get("target_margin_pct"), errors="coerce")
        )
        & (pd.to_numeric(frame["revenue_current"], errors="coerce") > 0)
    ]
    data_risk = frame[pd.to_numeric(frame["missing_cost_revenue_current"], errors="coerce") > 0]
    long_tail = frame[frame["segment_key"] == "long_tail"]
    cards = [
        _bucket("Protect", "Protect strategic suppliers", "strategic", strategic, "Highest-scale suppliers that are already at or above target margin."),
        _bucket("Recover", "Recover margin", "below_target_margin", margin_risk, "Revenue currently below the supplier margin target."),
        _bucket("Expand", "Expand growth suppliers", "growth", growth, "Suppliers outgrowing the comparable window."),
        _bucket("Review", "Review data-risk suppliers", "missing_cost", data_risk, "Revenue with missing cost or weak cost coverage."),
        _bucket("Rationalize", "Rationalize long tail", "long_tail", long_tail, "Low-impact suppliers where mix simplification may help."),
    ]
    cards[1]["uplift_target"] = ((risk_payload.get("summary") or {}).get("profit_uplift_target"))
    return {"cards": cards}


def _portfolio_posture_payload(
    frame: pd.DataFrame,
    *,
    concentration: Mapping[str, Any],
    dependency: Mapping[str, Any],
    risk_payload: Mapping[str, Any],
    protein_payload: Mapping[str, Any],
    active_30d: int,
    at_risk_count: int,
    new_suppliers: int,
    lost_suppliers: int,
    cost_coverage_pct: float | None,
    missing_cost_revenue: float,
) -> Dict[str, Any]:
    if frame is None or frame.empty:
        return {"cards": [], "narrative": "No supplier posture is available for the current scope."}

    top_family = (protein_payload.get("summary") or {}).get("top_family") or "Top family"
    top_family_share = _clean_optional_float((protein_payload.get("summary") or {}).get("top_family_share_pct"))
    high_dependency = _clean_int((dependency.get("summary") or {}).get("high_dependency_suppliers"))
    diversified = _clean_int((dependency.get("summary") or {}).get("diversified_suppliers"))
    margin_risk_suppliers = _clean_int((risk_payload.get("summary") or {}).get("margin_risk_suppliers"))
    margin_risk_revenue = _clean_float((risk_payload.get("summary") or {}).get("margin_risk_revenue"))
    profit_top5_share = _clean_optional_float(concentration.get("profit_top5_share"))
    profit_hhi = _clean_optional_float(concentration.get("profit_hhi"))
    protein_value = f"{top_family} {top_family_share:.1f}%" if top_family_share is not None else str(top_family)
    coverage_value = f"{cost_coverage_pct:.1f}% coverage" if cost_coverage_pct is not None else "Coverage unavailable"

    cards = [
        {
            "key": "concentration",
            "tone": "focus",
            "label": "Concentration posture",
            "value": f"Top 5 {float(concentration.get('top5_share') or 0.0):.1f}%",
            "note": (
                f"Revenue is {'narrow' if float(concentration.get('top5_share') or 0.0) >= 70.0 else 'reasonably spread'} "
                f"across suppliers. Covered profit top 5 share is "
                f"{'n/a' if profit_top5_share is None else f'{profit_top5_share:.1f}%'}."
            ),
            "meta": f"HHI {int(profit_hhi or concentration.get('hhi') or 0)} · {int(concentration.get('suppliers_for_80_pct') or 0)} suppliers drive 80%",
        },
        {
            "key": "activity",
            "tone": "growth",
            "label": "Activity rhythm",
            "value": f"{active_30d} active in 30d",
            "note": f"{at_risk_count} suppliers inactive for 90+ days. New/lost signals are {new_suppliers}/{lost_suppliers} in the comparable window.",
            "meta": "Use this to separate portfolio noise from real supplier drift.",
        },
        {
            "key": "protein",
            "tone": "premium",
            "label": "Protein posture",
            "value": protein_value,
            "note": f"{high_dependency} suppliers are highly dependent on one family, while {diversified} are meaningfully diversified across proteins.",
            "meta": "Protein concentration highlights supplier exposure and negotiation leverage.",
        },
        {
            "key": "trust",
            "tone": "watch" if (cost_coverage_pct or 0.0) < 95.0 else "healthy",
            "label": "Trust & margin pressure",
            "value": coverage_value,
            "note": f"{margin_risk_suppliers} suppliers sit below target margin across ${margin_risk_revenue:,.0f} of revenue.",
            "meta": f"Missing-cost revenue is ${missing_cost_revenue:,.0f}.",
        },
    ]
    narrative = (
        f"Supplier exposure is {'broad' if float(concentration.get('top5_share') or 0.0) < 70.0 else 'concentrated'}, "
        f"with {top_family} leading protein mix and {margin_risk_suppliers} suppliers requiring active commercial follow-up."
    )
    return {"cards": cards, "narrative": narrative}


def _table_summary_from_frame(frame: pd.DataFrame) -> Dict[str, Any]:
    if frame is None or frame.empty:
        return {
            "supplier_count": 0,
            "revenue": 0.0,
            "profit": None,
            "missing_cost_revenue": 0.0,
            "avg_margin_pct": None,
        }
    profit_series = pd.to_numeric(frame.get("profit_current"), errors="coerce")
    return {
        "supplier_count": int(frame["supplier_id"].nunique()) if "supplier_id" in frame.columns else int(len(frame.index)),
        "revenue": _clean_float(pd.to_numeric(frame.get("revenue_current"), errors="coerce").fillna(0.0).sum()),
        "profit": _clean_optional_float(profit_series.sum(min_count=1)),
        "missing_cost_revenue": _clean_float(pd.to_numeric(frame.get("missing_cost_revenue_current"), errors="coerce").fillna(0.0).sum()),
        "avg_margin_pct": _clean_optional_float(pd.to_numeric(frame.get("margin_pct"), errors="coerce").mean()),
    }


def _suppliers_narrative(
    *,
    total_revenue: float,
    revenue_prior: float,
    top_gainers: Sequence[Mapping[str, Any]],
    top_decliners: Sequence[Mapping[str, Any]],
    margin_shift_supplier: str | None,
) -> str:
    delta = total_revenue - revenue_prior
    direction = "up" if delta >= 0 else "down"
    pct = (abs(delta) / revenue_prior * 100.0) if revenue_prior > 0 else None
    gainers_txt = ", ".join([str(r.get("supplier_name")) for r in top_gainers[:2] if r.get("supplier_name")]) or "n/a"
    decliners_txt = ", ".join([str(r.get("supplier_name")) for r in top_decliners[:2] if r.get("supplier_name")]) or "n/a"
    if pct is not None:
        base = f"Revenue {direction} by ${abs(delta):,.0f} ({pct:.1f}%) vs prior window."
    else:
        base = f"Revenue {direction} by ${abs(delta):,.0f} vs prior window."
    margin_txt = f" Margin shift is most visible for {margin_shift_supplier}." if margin_shift_supplier else ""
    return f"{base} Top gainers: {gainers_txt}. Top decliners: {decliners_txt}.{margin_txt}".strip()


def _suppliers_v2_base_frame(
    filters: Any,
    scope: Dict[str, Any],
    cols: set[str],
    cols_map: Mapping[str, Any],
    dataset_version: str,
    filter_hash: str,
    scope_summary: Mapping[str, Any],
    start_iso: str | None,
    end_iso: str | None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, Dict[str, Any], bool]:
    comparison = _build_comparison_window(start_iso, end_iso)
    curr_start = str(comparison.get("current_start") or start_iso or "")
    curr_end = str(comparison.get("current_end") or start_iso or "")
    prior_start = str(comparison.get("prior_start") or curr_start)
    prior_end = str(comparison.get("prior_end") or curr_start)
    history_start = str(comparison.get("history_start") or prior_start)

    cache_key = _cache_key(
        "suppliers.v2.base",
        dataset_version,
        scope_summary,
        filter_hash,
        {
            "curr_start": curr_start,
            "curr_end": curr_end,
            "prior_start": prior_start,
            "prior_end": prior_end,
            "history_start": history_start,
        },
    )

    def _build() -> Dict[str, Any]:
        expanded_filters = _filters_with_window(filters, history_start, curr_end)
        where_sql, params, _, _ = fact_store.build_where_clause(
            expanded_filters,
            cols,
            scope,
            apply_default_window=True,
        )
        scoped_cte = _scoped_cte(cols, cols_map, where_sql)
        sql = _supplier_v2_stats_sql(scoped_cte)
        sql_params = list(params) + [curr_start, curr_end, prior_start, prior_end]
        frame = _execute_sql_df_with_fallback(sql, sql_params, tag="suppliers.v2.base")
        normalized = _normalize_v2_supplier_frame(frame, window_end_iso=curr_end)
        protein_df = _execute_sql_df_with_fallback(
            _supplier_protein_mix_sql(scoped_cte),
            list(params) + [curr_start, curr_end, prior_start, prior_end],
            tag="suppliers.v2.protein_mix",
        )
        product_df = _execute_sql_df_with_fallback(
            _supplier_product_concentration_sql(scoped_cte),
            list(params) + [curr_start, curr_end],
            tag="suppliers.v2.product_mix",
        )
        enriched = _enrich_supplier_mix_frame(normalized, protein_df, product_df)
        segmented = _assign_supplier_segments(enriched)
        return {
            "rows": segmented.to_dict(orient="records"),
            "protein_rows": protein_df.to_dict(orient="records"),
            "product_rows": product_df.to_dict(orient="records"),
            "comparison": comparison,
        }

    payload, hit = _ANALYTICS_CACHE.get_or_compute(cache_key, TABLE_TTL_SECONDS, _build)
    payload = _clone(payload) if isinstance(payload, dict) else {"rows": [], "protein_rows": [], "product_rows": [], "comparison": comparison}
    frame = pd.DataFrame.from_records(payload.get("rows") or [])
    protein_df = pd.DataFrame.from_records(payload.get("protein_rows") or [])
    product_df = pd.DataFrame.from_records(payload.get("product_rows") or [])
    comparison_meta = payload.get("comparison") or comparison
    return frame, protein_df, product_df, comparison_meta, bool(hit)


def _build_suppliers_bundle_v2(
    filters: Any,
    scope: Dict[str, Any],
    args: Any,
    *,
    cols: set[str],
    cols_map: Mapping[str, Any],
    dataset_version: str,
    filter_hash: str,
    scope_summary: Mapping[str, Any],
    where_sql: str,
    params: Sequence[Any],
    start_iso: str | None,
    end_iso: str | None,
    scoped_cte: str,
) -> Dict[str, Any]:
    build_started = time.perf_counter()
    top_n = _parse_top_n(args)
    kpi_cache_key = _cache_key(
        "suppliers.kpis_charts.v2",
        dataset_version,
        scope_summary,
        filter_hash,
        {"top_n": top_n},
    )

    def _build_kpis_charts() -> Dict[str, Any]:
        sql = _kpis_charts_sql(scoped_cte, top_n)
        df = _execute_sql_df_with_fallback(sql, params, tag="suppliers.kpis_charts.v2")
        row = df.iloc[0].to_dict() if not df.empty else {}
        return _parse_kpis_charts_row(row, start_iso, end_iso, top_n)

    kpi_payload, kpi_hit = _KPI_CHART_CACHE.get_or_compute(kpi_cache_key, KPI_CHART_TTL_SECONDS, _build_kpis_charts)
    kpi_payload = _clone(kpi_payload)

    base_frame, protein_df, product_df, window_meta, base_hit = _suppliers_v2_base_frame(
        filters,
        scope,
        cols,
        cols_map,
        dataset_version,
        filter_hash,
        scope_summary,
        start_iso,
        end_iso,
    )

    table_payload, filtered_table_df = _v2_table_payload_from_frame(base_frame, args, paginate=True)
    concentration = _concentration_payload_from_frame(base_frame)
    segments_summary = _segment_summary_from_frame(base_frame)
    movers_payload = _movers_payload_from_frame(base_frame)
    risk_payload = _risk_payload_from_frame(base_frame)
    protein_intelligence = _protein_payload_from_frame(base_frame, protein_df)
    dependency_payload = _dependency_payload_from_frame(base_frame, concentration)
    actions_payload = _actions_payload_from_frame(base_frame, risk_payload)

    total_revenue = _clean_float(base_frame.get("revenue_current", pd.Series(dtype="float64")).sum()) if not base_frame.empty else 0.0
    total_revenue_prior = _clean_float(base_frame.get("revenue_prior", pd.Series(dtype="float64")).sum()) if not base_frame.empty else 0.0
    covered_revenue = _clean_float(base_frame.get("cost_covered_revenue_current", pd.Series(dtype="float64")).sum()) if not base_frame.empty else 0.0
    covered_revenue_prior = _clean_float(base_frame.get("cost_covered_revenue_prior", pd.Series(dtype="float64")).sum()) if not base_frame.empty else 0.0
    missing_cost_revenue = _clean_float(base_frame.get("missing_cost_revenue_current", pd.Series(dtype="float64")).sum()) if not base_frame.empty else 0.0
    total_profit = _clean_optional_float(base_frame.get("profit_current", pd.Series(dtype="float64")).sum()) if not base_frame.empty else None
    total_profit_prior = _clean_optional_float(base_frame.get("profit_prior", pd.Series(dtype="float64")).sum()) if not base_frame.empty else None

    margin_pct = ((total_profit / covered_revenue) * 100.0) if (total_profit is not None and covered_revenue > 0) else None
    margin_prior_pct = (
        ((total_profit_prior / covered_revenue_prior) * 100.0)
        if (total_profit_prior is not None and covered_revenue_prior > 0)
        else None
    )
    margin_delta_pp = (margin_pct - margin_prior_pct) if (margin_pct is not None and margin_prior_pct is not None) else None

    rows_current = _clean_float(base_frame.get("rows_current", pd.Series(dtype="float64")).sum()) if not base_frame.empty else 0.0
    cost_missing_rows = _clean_float(base_frame.get("cost_missing_rows_current", pd.Series(dtype="float64")).sum()) if not base_frame.empty else 0.0
    cost_coverage_pct = (covered_revenue / total_revenue * 100.0) if total_revenue > 0 else None
    cost_coverage_row_pct = ((rows_current - cost_missing_rows) / rows_current * 100.0) if rows_current > 0 else None

    active_suppliers = int((pd.to_numeric(base_frame.get("revenue_current"), errors="coerce") > 0).sum()) if not base_frame.empty else 0
    suppliers_total = int(base_frame["supplier_id"].nunique()) if not base_frame.empty and "supplier_id" in base_frame.columns else 0
    active_30d = int((pd.to_numeric(base_frame.get("days_since_last_order"), errors="coerce") <= 30).sum()) if not base_frame.empty else 0
    at_risk_mask = pd.to_numeric(base_frame.get("at_risk_flag"), errors="coerce") == 1 if not base_frame.empty else pd.Series(dtype="bool")
    at_risk_count = int(at_risk_mask.sum()) if not base_frame.empty else 0
    revenue_at_risk = _clean_float(base_frame.loc[at_risk_mask, "revenue_current"].sum()) if not base_frame.empty else 0.0
    missing_cost_supplier_count = int((pd.to_numeric(base_frame.get("missing_cost_revenue_current"), errors="coerce") > 0).sum()) if not base_frame.empty else 0
    new_suppliers = int((base_frame.get("delta_revenue_status", pd.Series(dtype="object")) == "new").sum()) if not base_frame.empty else 0
    lost_suppliers = int((base_frame.get("delta_revenue_status", pd.Series(dtype="object")) == "lost").sum()) if not base_frame.empty else 0
    strategic_suppliers = int((base_frame.get("segment_key", pd.Series(dtype="object")) == "strategic").sum()) if not base_frame.empty else 0
    long_tail_suppliers = int((base_frame.get("segment_key", pd.Series(dtype="object")) == "long_tail").sum()) if not base_frame.empty else 0
    portfolio_posture = _portfolio_posture_payload(
        base_frame,
        concentration=concentration,
        dependency=dependency_payload,
        risk_payload=risk_payload,
        protein_payload=protein_intelligence,
        active_30d=active_30d,
        at_risk_count=at_risk_count,
        new_suppliers=new_suppliers,
        lost_suppliers=lost_suppliers,
        cost_coverage_pct=cost_coverage_pct,
        missing_cost_revenue=missing_cost_revenue,
    )

    margin_shift_supplier = None
    if not base_frame.empty and "delta_margin_pp" in base_frame.columns:
        margin_sorted = base_frame.copy()
        margin_sorted["abs_margin_delta"] = pd.to_numeric(margin_sorted["delta_margin_pp"], errors="coerce").abs()
        margin_sorted = margin_sorted.sort_values("abs_margin_delta", ascending=False, na_position="last")
        if not margin_sorted.empty:
            margin_shift_supplier = margin_sorted.iloc[0].get("supplier_name")

    top_gain = (movers_payload.get("top_gainers") or [None])[0] or {}
    top_decline = (movers_payload.get("top_decliners") or [None])[0] or {}
    top_family = ((protein_intelligence.get("summary") or {}).get("top_family"))
    top_family_share = ((protein_intelligence.get("summary") or {}).get("top_family_share_pct"))
    table_margin_rows = table_payload.get("rows") or []
    minimum_margin_pct = margin_rules.weighted_minimum_margin_pct(table_margin_rows, revenue_key="revenue_current")
    target_margin_pct = margin_rules.weighted_target_margin_pct(table_margin_rows, revenue_key="revenue_current")
    margin_status = margin_rules.classify_margin_status(margin_pct, minimum_margin_pct, target_margin_pct)
    revenue_delta = total_revenue - total_revenue_prior
    revenue_delta_pct = ((revenue_delta / total_revenue_prior) * 100.0) if total_revenue_prior > 0 else None
    direction = "up" if revenue_delta >= 0 else "down"
    delta_txt = (
        f"Revenue is {direction} ${abs(revenue_delta):,.0f} ({abs(revenue_delta_pct):.1f}%) versus {window_meta.get('prior_short_label') or 'the prior comparable window'}."
        if revenue_delta_pct is not None
        else f"Revenue is {direction} ${abs(revenue_delta):,.0f} under the active window."
    )
    protein_txt = (
        f" {top_family} leads protein mix at {float(top_family_share or 0.0):.1f}% of supplier revenue."
        if top_family
        else ""
    )
    trust_txt = (
        f" Cost coverage is {float(cost_coverage_pct or 0.0):.1f}% of revenue."
        if cost_coverage_pct is not None
        else ""
    )
    movers_txt = ""
    if top_gain.get("supplier_name") or top_decline.get("supplier_name"):
        movers_txt = (
            f" Biggest gain: {top_gain.get('supplier_name') or 'n/a'}."
            f" Biggest decline: {top_decline.get('supplier_name') or 'n/a'}."
        )
    margin_txt = f" Margin deterioration is most visible for {margin_shift_supplier}." if margin_shift_supplier else ""
    narrative = f"{delta_txt}{protein_txt}{trust_txt}{movers_txt}{margin_txt}".strip()
    coverage_status = (
        "Healthy"
        if (cost_coverage_pct or 0.0) >= 95.0
        else "Watch"
        if (cost_coverage_pct or 0.0) >= 85.0
        else "Risk"
    )

    kpis = dict(kpi_payload.get("kpis", {}) or {})
    kpis.update(
        {
            "total_revenue": total_revenue,
            "total_revenue_prior": total_revenue_prior,
            "revenue_delta": revenue_delta,
            "revenue_delta_pct": revenue_delta_pct,
            "total_profit": total_profit,
            "total_profit_prior": total_profit_prior,
            "profit_delta": (total_profit - total_profit_prior)
            if (total_profit is not None and total_profit_prior is not None)
            else None,
            "margin_pct": margin_pct,
            "minimum_margin_pct": minimum_margin_pct,
            "target_margin_pct": target_margin_pct,
            "target_gap_pct_points": None if margin_pct is None or target_margin_pct is None else (margin_pct - target_margin_pct),
            "margin_prior_pct": margin_prior_pct,
            "margin_delta_pp": margin_delta_pp,
            "cost_coverage_pct": cost_coverage_pct,
            "cost_coverage_row_pct": cost_coverage_row_pct,
            "cost_missing_rows": _clean_int(cost_missing_rows),
            "missing_cost_revenue": missing_cost_revenue,
            "missing_cost_supplier_count": missing_cost_supplier_count,
            "active_suppliers": active_suppliers,
            "active_suppliers_30d": active_30d,
            "suppliers_total": suppliers_total,
            "at_risk_suppliers": at_risk_count,
            "revenue_at_risk": revenue_at_risk,
            "new_suppliers": new_suppliers,
            "lost_suppliers": lost_suppliers,
            "strategic_suppliers": strategic_suppliers,
            "long_tail_suppliers": long_tail_suppliers,
            "concentration_hhi": concentration.get("hhi"),
            "concentration_top1_share": concentration.get("top1_share"),
            "concentration_top5_share": concentration.get("top5_share"),
            "concentration_top10_share": concentration.get("top10_share"),
            "profit_concentration_hhi": concentration.get("profit_hhi"),
            "profit_concentration_top1_share": concentration.get("profit_top1_share"),
            "profit_concentration_top5_share": concentration.get("profit_top5_share"),
            "suppliers_for_80_pct": concentration.get("suppliers_for_80_pct"),
            "top_protein_family": top_family,
            "top_protein_share_pct": top_family_share,
            "window": {
                "start": window_meta.get("current_start") or start_iso,
                "end": window_meta.get("current_end") or end_iso,
                "prior_start": window_meta.get("prior_start"),
                "prior_end": window_meta.get("prior_end"),
                "current_label": window_meta.get("current_label"),
                "prior_label": window_meta.get("prior_label"),
                "comparison_label": window_meta.get("comparison_label"),
            },
            "narrative": narrative,
            **margin_status,
        }
    )

    charts = dict(kpi_payload.get("charts", {}) or {})
    charts["concentration_pareto"] = concentration.get("pareto_rows") or []
    charts["revenue_profit_trend"] = charts.get("trend_12m", {})
    charts["protein_mix"] = protein_intelligence.get("mix") or []
    charts["segment_mix"] = segments_summary

    payload = {
        "kpis": kpis,
        "trend": kpi_payload.get("trend", {}),
        "charts": charts,
        "table": table_payload,
        "segments": {
            "summary": segments_summary,
        },
        "movers": movers_payload,
        "risk_opportunities": risk_payload,
        "protein_intelligence": protein_intelligence,
        "dependency": dependency_payload,
        "actions": actions_payload,
        "portfolio_posture": portfolio_posture,
        "executive_summary": {
            "narrative": narrative,
            "comparison": window_meta,
            "coverage_status": coverage_status,
            "active_suppliers_30d": active_30d,
            "active_suppliers": active_suppliers,
            "suppliers_total": suppliers_total,
            "at_risk_suppliers": at_risk_count,
            "revenue_at_risk": revenue_at_risk,
            "cost_coverage_pct": cost_coverage_pct,
            "missing_cost_revenue": missing_cost_revenue,
            "top_gainers": (movers_payload.get("top_gainers") or [])[:3],
            "top_decliners": (movers_payload.get("top_decliners") or [])[:3],
            "top_protein_family": top_family,
            "top_protein_share_pct": top_family_share,
        },
        "meta": {
            "page_id": "suppliers",
            "suppliers_v2": True,
            "top_n": top_n,
            "window_start": window_meta.get("current_start") or start_iso,
            "window_end": window_meta.get("current_end") or end_iso,
            "prior_window_start": window_meta.get("prior_start"),
            "prior_window_end": window_meta.get("prior_end"),
            "comparison": window_meta,
            "cache_parts": {
                "kpis_charts": bool(kpi_hit),
                "analytics": bool(base_hit),
                "table": False,
            },
            "table_rows_after_filters": int(len(filtered_table_df.index)),
            "protein_rows": int(len(protein_df.index)),
            "product_mix_rows": int(len(product_df.index)),
        },
    }
    try:
        current_app.logger.info(
            "suppliers.bundle.completed",
            extra={
                "page": "suppliers",
                "window_method": window_meta.get("method"),
                "scope": dict(scope_summary),
                "table_rows_after_filters": int(len(filtered_table_df.index)),
                "cache_parts": payload["meta"]["cache_parts"],
                "duration_ms": round((time.perf_counter() - build_started) * 1000.0, 2),
            },
        )
    except Exception:
        pass
    return payload


def build_suppliers_bundle(filters: Any, scope: Dict[str, Any], args: Any) -> Dict[str, Any]:
    cols = fact_store.list_columns()
    cols_map = _required_columns(cols)
    missing = []
    if not cols_map.get("date"):
        missing.append("date")
    if not cols_map.get("revenue_candidates"):
        missing.append("revenue")
    if not (cols_map.get("supplier_id_candidates") or cols_map.get("supplier_name_candidates")):
        missing.append("supplier_id/supplier_name")
    if missing:
        return {
            "error": {"message": "Required columns missing for suppliers bundle: " + ", ".join(missing)},
            "meta": {"cached": False},
        }

    dataset_version = fact_store.cache_buster()
    filter_hash, scope_summary = _filter_hash(filters, scope, dataset_version)

    where_sql, params, start_iso, end_iso = fact_store.build_where_clause(
        filters, cols, scope, apply_default_window=True
    )
    scoped_cte = _scoped_cte(cols, cols_map, where_sql)

    if _suppliers_v2_requested(args):
        return _build_suppliers_bundle_v2(
            filters,
            scope,
            args,
            cols=cols,
            cols_map=cols_map,
            dataset_version=dataset_version,
            filter_hash=filter_hash,
            scope_summary=scope_summary,
            where_sql=where_sql,
            params=params,
            start_iso=start_iso,
            end_iso=end_iso,
            scoped_cte=scoped_cte,
        )

    top_n = _parse_top_n(args)
    kpi_cache_key = _cache_key(
        "suppliers.kpis_charts",
        dataset_version,
        scope_summary,
        filter_hash,
        {"top_n": top_n},
    )

    def _build_kpis_charts() -> Dict[str, Any]:
        sql = _kpis_charts_sql(scoped_cte, top_n)
        df = fact_store.execute_sql_df(sql, params, tag="suppliers.kpis_charts")
        row = df.iloc[0].to_dict() if not df.empty else {}
        return _parse_kpis_charts_row(row, start_iso, end_iso, top_n)

    kpi_payload, kpi_hit = _KPI_CHART_CACHE.get_or_compute(kpi_cache_key, KPI_CHART_TTL_SECONDS, _build_kpis_charts)
    kpi_payload = _clone(kpi_payload)

    page_num, page_size = _pagination(args)
    sort_by, sort_dir = _sort_params(args)
    search = _extract_search(args)
    offset = (page_num - 1) * page_size

    table_cache_key = _cache_key(
        "suppliers.table",
        dataset_version,
        scope_summary,
        filter_hash,
        {
            "page": page_num,
            "page_size": page_size,
            "sort_by": sort_by,
            "sort_dir": sort_dir,
            "search": search,
        },
    )

    def _build_table() -> Dict[str, Any]:
        sql, table_params = _suppliers_table_sql(
            scoped_cte,
            sort_by,
            sort_dir,
            search,
            paginate=True,
            page_size=page_size,
            offset=offset,
        )
        df = fact_store.execute_sql_df(sql, params + table_params, tag="suppliers.table")
        return _table_payload_from_df(
            df,
            page_num=page_num,
            page_size=page_size,
            sort_by=sort_by,
            sort_dir=sort_dir,
            search=search,
        )

    table_payload, table_hit = _TABLE_CACHE.get_or_compute(table_cache_key, TABLE_TTL_SECONDS, _build_table)
    table_payload = _clone(table_payload)

    payload = {
        "kpis": kpi_payload.get("kpis", {}),
        "trend": kpi_payload.get("trend", {}),
        "charts": kpi_payload.get("charts", {}),
        "table": table_payload,
        "meta": {
            "page_id": "suppliers",
            "top_n": top_n,
            "cache_parts": {"kpis_charts": bool(kpi_hit), "table": bool(table_hit)},
        },
    }
    return payload


def build_suppliers_table_frame(filters: Any, scope: Dict[str, Any], args: Any) -> tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Export helper: returns the full filtered + sorted supplier table without pagination.
    This reuses the same DuckDB SQL generator used by the on-page table.
    """
    cols = fact_store.list_columns()
    cols_map = _required_columns(cols)
    if not cols_map.get("date") or not cols_map.get("revenue_candidates"):
        return pd.DataFrame(), {"total_rows": 0}

    if _suppliers_v2_requested(args):
        dataset_version = fact_store.cache_buster()
        filter_hash, scope_summary = _filter_hash(filters, scope, dataset_version)
        where_sql, params, start_iso, end_iso = fact_store.build_where_clause(
            filters, cols, scope, apply_default_window=True
        )
        scoped_cte = _scoped_cte(cols, cols_map, where_sql)
        _ = params  # explicit for parity with v1 window resolver
        base_frame, _protein_df, _product_df, window_meta, _cache_hit = _suppliers_v2_base_frame(
            filters,
            scope,
            cols,
            cols_map,
            dataset_version,
            filter_hash,
            scope_summary,
            start_iso,
            end_iso,
        )
        table_payload, _filtered = _v2_table_payload_from_frame(base_frame, args, paginate=False)
        frame = pd.DataFrame.from_records(table_payload.get("rows") or [])
        rename_map = {
            "supplier_name": "Supplier",
            "supplier_id": "Supplier ID",
            "segment_label": "Segment",
            "risk_band": "Risk Band",
            "revenue_current": "Revenue",
            "revenue_prior": "Revenue (Prior Window)",
            "delta_revenue": "Delta Revenue",
            "delta_revenue_pct": "Delta Revenue %",
            "delta_revenue_label": "Delta Status",
            "cost": "Cost",
            "profit": "Profit",
            "profit_prior": "Profit (Prior Window)",
            "delta_profit": "Delta Profit",
            "margin_pct": "Margin %",
            "margin_prior_pct": "Margin % (Prior Window)",
            "delta_margin_pp": "Delta Margin (pp)",
            "orders": "Orders",
            "orders_prior": "Orders (Prior Window)",
            "customers": "Customers",
            "products": "Products",
            "units": "Units",
            "weight_lb": "Weight (lb)",
            "aov": "AOV",
            "avg_sale_price_per_lb": "ASP / lb",
            "avg_cost_per_lb": "Avg Cost / lb",
            "contribution_per_lb": "Contribution / lb",
            "missing_cost_pct": "Missing Cost %",
            "missing_cost_revenue_current": "Missing Cost Revenue",
            "cost_coverage_pct": "Cost Coverage %",
            "cost_coverage_row_pct": "Cost Coverage % (Rows)",
            "top_protein": "Top Protein",
            "top_protein_share_pct": "Top Protein Share %",
            "protein_dependency_posture": "Protein Dependency",
            "top_sku_share_pct": "Top SKU Share %",
            "protein_family_count": "Protein Families",
            "first_order_date": "First Order Date",
            "last_order_date": "Last Order Date",
            "days_since_last_order": "Days Since Last Order",
        }
        if not frame.empty:
            frame = frame.rename(columns={k: v for k, v in rename_map.items() if k in frame.columns})
        meta = {
            "total_rows": int(table_payload.get("total_rows") or 0),
            "sort_by": table_payload.get("sort_by"),
            "sort_dir": table_payload.get("sort_dir"),
            "search": table_payload.get("search"),
            "quick_filter": table_payload.get("quick_filter"),
            "protein_filter": table_payload.get("protein_filter"),
            "segments": table_payload.get("segments") or [],
            "start": window_meta.get("current_start") or start_iso,
            "end": window_meta.get("current_end") or end_iso,
            "prior_start": window_meta.get("prior_start"),
            "prior_end": window_meta.get("prior_end"),
        }
        return frame, meta

    where_sql, params, start_iso, end_iso = fact_store.build_where_clause(
        filters, cols, scope, apply_default_window=True
    )
    scoped_cte = _scoped_cte(cols, cols_map, where_sql)

    sort_by, sort_dir = _sort_params(args)
    search = _extract_search(args)
    sql, table_params = _suppliers_table_sql(
        scoped_cte,
        sort_by,
        sort_dir,
        search,
        paginate=False,
        page_size=0,
        offset=0,
    )
    df = fact_store.execute_sql_df(sql, params + table_params, tag="suppliers.export.table")
    if "total_groups" in df.columns:
        df = df.drop(columns=["total_groups"])
    if "last_sold" in df.columns:
        last = pd.to_datetime(df["last_sold"], errors="coerce")
        df["last_sold"] = last.dt.date.astype("string")

    rename_map = {
        "supplier_name": "Supplier",
        "supplier_id": "Supplier ID",
        "revenue": "Revenue",
        "cost": "Cost",
        "profit": "Profit",
        "margin_pct": "Margin %",
        "roi_pct": "ROI %",
        "units": "Units",
        "weight_lb": "Weight (lb)",
        "avg_sale_price_per_lb": "Avg Sale Price / lb",
        "avg_cost_per_unit": "Avg Cost / Unit",
        "avg_cost_per_lb": "Avg Cost / lb",
        "profit_per_unit": "Profit / Unit",
        "profit_per_lb": "Profit / lb",
        "products": "Products",
        "orders": "Orders",
        "customers": "Customers",
        "last_sold": "Last Sold",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    meta = {
        "total_rows": int(len(df)),
        "sort_by": sort_by,
        "sort_dir": sort_dir.lower(),
        "search": search,
        "start": start_iso,
        "end": end_iso,
    }
    return df, meta


def build_suppliers_export_dataset(
    filters: Any,
    scope: Dict[str, Any],
    args: Any,
    dataset: str,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    token = str(dataset or "table").strip().lower()
    aliases = {
        "suppliers": "table",
        "overview": "table",
        "mover": "movers",
        "segment": "segments",
        "segment_summary": "segments",
        "data_quality_risk": "risk",
        "margin_leakage": "risk",
        "pareto": "concentration",
        "proteins": "protein",
        "protein_mix": "protein",
        "action": "actions",
        "watchlist": "actions",
    }
    token = aliases.get(token, token)

    if token == "table" and not _suppliers_v2_requested(args):
        return build_suppliers_table_frame(filters, scope, args)

    cols = fact_store.list_columns()
    cols_map = _required_columns(cols)
    if not cols_map.get("date") or not cols_map.get("revenue_candidates"):
        return pd.DataFrame(), {"total_rows": 0, "dataset": token}

    dataset_version = fact_store.cache_buster()
    filter_hash, scope_summary = _filter_hash(filters, scope, dataset_version)
    where_sql, params, start_iso, end_iso = fact_store.build_where_clause(
        filters, cols, scope, apply_default_window=True
    )
    scoped_cte = _scoped_cte(cols, cols_map, where_sql)
    _ = params
    _ = scoped_cte
    base_frame, protein_df, _product_df, window_meta, _cache_hit = _suppliers_v2_base_frame(
        filters,
        scope,
        cols,
        cols_map,
        dataset_version,
        filter_hash,
        scope_summary,
        start_iso,
        end_iso,
    )

    if token == "table":
        table_payload, _filtered = _v2_table_payload_from_frame(base_frame, args, paginate=False)
        frame = pd.DataFrame.from_records(table_payload.get("rows") or [])
        total_rows = int(table_payload.get("total_rows") or len(frame.index))
        meta = {
            "dataset": token,
            "total_rows": total_rows,
            "sort_by": table_payload.get("sort_by"),
            "sort_dir": table_payload.get("sort_dir"),
            "search": table_payload.get("search"),
            "quick_filter": table_payload.get("quick_filter"),
            "protein_filter": table_payload.get("protein_filter"),
            "segments": table_payload.get("segments") or [],
            "start": window_meta.get("current_start") or start_iso,
            "end": window_meta.get("current_end") or end_iso,
            "prior_start": window_meta.get("prior_start"),
            "prior_end": window_meta.get("prior_end"),
        }
        return frame, meta

    if token == "movers":
        movers_payload = _movers_payload_from_frame(base_frame)
        frame = pd.DataFrame.from_records(movers_payload.get("rows") or [])
        meta = {
            "dataset": token,
            "total_rows": int(len(frame.index)),
            "start": window_meta.get("current_start") or start_iso,
            "end": window_meta.get("current_end") or end_iso,
            "prior_start": window_meta.get("prior_start"),
            "prior_end": window_meta.get("prior_end"),
        }
        return frame, meta

    if token == "segments":
        rows = _segment_summary_from_frame(base_frame)
        frame = pd.DataFrame.from_records(rows)
        meta = {
            "dataset": token,
            "total_rows": int(len(frame.index)),
            "start": window_meta.get("current_start") or start_iso,
            "end": window_meta.get("current_end") or end_iso,
            "prior_start": window_meta.get("prior_start"),
            "prior_end": window_meta.get("prior_end"),
        }
        return frame, meta

    if token == "concentration":
        conc = _concentration_payload_from_frame(base_frame)
        frame = pd.DataFrame.from_records(conc.get("pareto_rows") or [])
        meta = {
            "dataset": token,
            "total_rows": int(len(frame.index)),
            "start": window_meta.get("current_start") or start_iso,
            "end": window_meta.get("current_end") or end_iso,
            "prior_start": window_meta.get("prior_start"),
            "prior_end": window_meta.get("prior_end"),
        }
        return frame, meta

    if token == "protein":
        protein_payload = _protein_payload_from_frame(base_frame, protein_df)
        frame = pd.DataFrame.from_records(protein_payload.get("mix") or [])
        meta = {
            "dataset": token,
            "total_rows": int(len(frame.index)),
            "start": window_meta.get("current_start") or start_iso,
            "end": window_meta.get("current_end") or end_iso,
            "prior_start": window_meta.get("prior_start"),
            "prior_end": window_meta.get("prior_end"),
        }
        return frame, meta

    if token == "risk":
        risk_payload = _risk_payload_from_frame(base_frame)
        margin_df = pd.DataFrame.from_records(risk_payload.get("margin_leakage") or [])
        if not margin_df.empty:
            margin_df.insert(0, "risk_type", "margin_leakage")
        data_df = pd.DataFrame.from_records(risk_payload.get("data_risk") or [])
        if not data_df.empty:
            data_df.insert(0, "risk_type", "data_quality")
        frame = pd.concat([margin_df, data_df], ignore_index=True, sort=False) if (not margin_df.empty or not data_df.empty) else pd.DataFrame()
        meta = {
            "dataset": token,
            "total_rows": int(len(frame.index)),
            "start": window_meta.get("current_start") or start_iso,
            "end": window_meta.get("current_end") or end_iso,
            "prior_start": window_meta.get("prior_start"),
            "prior_end": window_meta.get("prior_end"),
        }
        return frame, meta

    if token == "actions":
        frame = pd.DataFrame.from_records(_actions_payload_from_frame(base_frame, _risk_payload_from_frame(base_frame)).get("cards") or [])
        meta = {
            "dataset": token,
            "total_rows": int(len(frame.index)),
            "start": window_meta.get("current_start") or start_iso,
            "end": window_meta.get("current_end") or end_iso,
            "prior_start": window_meta.get("prior_start"),
            "prior_end": window_meta.get("prior_end"),
        }
        return frame, meta

    raise ValueError(f"Unsupported suppliers export dataset: {dataset}")


def build_suppliers_export_metadata_frame(
    filters: Any,
    *,
    dataset: str,
    dataset_version: str,
    meta: Mapping[str, Any],
    args: Any,
) -> pd.DataFrame:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    rows = [
        {"field": "generated_at_utc", "value": pd.Timestamp.utcnow().isoformat()},
        {"field": "dataset", "value": str(dataset)},
        {"field": "dataset_version", "value": str(dataset_version or "")},
        {"field": "window_start", "value": str(meta.get("start") or "")},
        {"field": "window_end", "value": str(meta.get("end") or "")},
        {"field": "prior_window_start", "value": str(meta.get("prior_start") or "")},
        {"field": "prior_window_end", "value": str(meta.get("prior_end") or "")},
        {"field": "filters_json", "value": filters_service.canonical_json(filters)},
        {"field": "search", "value": str(meta.get("search") or getter("search") or "")},
        {"field": "quick_filter", "value": str(meta.get("quick_filter") or getter("quick_filter") or "all")},
        {"field": "protein_filter", "value": str(meta.get("protein_filter") or getter("protein") or "")},
        {"field": "segments", "value": ",".join(meta.get("segments") or _parse_segment_filters(args))},
        {"field": "sort_by", "value": str(meta.get("sort_by") or getter("sort") or "")},
        {"field": "sort_dir", "value": str(meta.get("sort_dir") or getter("sort_dir") or "")},
        {"field": "total_rows", "value": str(meta.get("total_rows") or 0)},
    ]
    return pd.DataFrame(rows)


def _scoped_supplier_cte(scoped_cte: str) -> str:
    return f"""
        {scoped_cte},
        scoped_supplier AS (
            SELECT * FROM scoped WHERE supplier_id = ?
        )
    """


def _drilldown_summary_sql(scoped_supplier_cte: str) -> str:
    return f"""
        WITH
        {scoped_supplier_cte},
        monthly_rollup AS (
            SELECT
                date_trunc('month', order_date) AS month_start,
                SUM(revenue) AS revenue,
                SUM(cost) AS cost_sum,
                SUM(CASE WHEN cost IS NULL THEN 0 ELSE 1 END) AS cost_count,
                SUM(units) AS units,
                SUM(weight_lb) AS weight_lb,
                COUNT(DISTINCT CASE WHEN order_id IS NOT NULL AND order_id <> '' THEN order_id END) AS orders
            FROM scoped_supplier
            GROUP BY 1
        ),
        monthly_last24 AS (
            SELECT * FROM monthly_rollup ORDER BY month_start DESC LIMIT 24
        ),
        monthly_sorted AS (
            SELECT
                strftime('%Y-%m', month_start) AS month,
                revenue,
                CASE WHEN cost_count > 0 THEN cost_sum ELSE NULL END AS cost,
                CASE WHEN cost_count > 0 THEN revenue - cost_sum ELSE NULL END AS profit,
                CASE WHEN revenue > 0 AND cost_count > 0 THEN (revenue - cost_sum) / revenue * 100 ELSE NULL END AS margin_pct,
                units,
                weight_lb,
                orders,
                month_start
            FROM monthly_last24
            ORDER BY month_start
        )
        SELECT
            MIN(supplier_name) AS supplier_name,
            SUM(revenue) AS revenue,
            CASE WHEN SUM(CASE WHEN cost IS NULL THEN 0 ELSE 1 END) > 0 THEN SUM(cost) ELSE NULL END AS cost,
            CASE
                WHEN SUM(CASE WHEN cost IS NULL THEN 0 ELSE 1 END) > 0 THEN SUM(revenue) - SUM(cost)
                ELSE NULL
            END AS profit,
            CASE
                WHEN SUM(revenue) > 0 AND SUM(CASE WHEN cost IS NULL THEN 0 ELSE 1 END) > 0 THEN (SUM(revenue) - SUM(cost)) / SUM(revenue) * 100
                ELSE NULL
            END AS margin_pct,
            CASE
                WHEN SUM(CASE WHEN cost IS NULL THEN 0 ELSE 1 END) > 0 AND SUM(cost) > 0 THEN (SUM(revenue) - SUM(cost)) / SUM(cost) * 100
                ELSE NULL
            END AS roi_pct,
            SUM(units) AS units,
            SUM(weight_lb) AS weight_lb,
            COUNT(DISTINCT CASE WHEN order_id IS NOT NULL AND order_id <> '' THEN order_id END) AS orders,
            COUNT(DISTINCT CASE WHEN product_id IS NOT NULL AND product_id <> '' THEN product_id END) AS products,
            COUNT(DISTINCT CASE WHEN customer_id IS NOT NULL AND customer_id <> '' THEN customer_id END) AS customers,
            MAX(order_date) AS last_sold,
            (SELECT list(struct_pack(
                month:=month,
                revenue:=revenue,
                cost:=cost,
                profit:=profit,
                margin_pct:=margin_pct,
                units:=units,
                weight_lb:=weight_lb,
                orders:=orders
            )) FROM monthly_sorted) AS monthly
        FROM scoped_supplier
    """


def _drilldown_products_sql(scoped_supplier_cte: str, top_n: int) -> str:
    return f"""
        WITH
        {scoped_supplier_cte},
        prod_rollup AS (
            SELECT
                product_id,
                product_name,
                SUM(revenue) AS revenue,
                SUM(cost) AS cost_sum,
                SUM(CASE WHEN cost IS NULL THEN 0 ELSE 1 END) AS cost_count,
                SUM(units) AS units,
                SUM(weight_lb) AS weight_lb,
                COUNT(DISTINCT CASE WHEN order_id IS NOT NULL AND order_id <> '' THEN order_id END) AS orders,
                COUNT(DISTINCT CASE WHEN customer_id IS NOT NULL AND customer_id <> '' THEN customer_id END) AS customers
            FROM scoped_supplier
            WHERE product_id IS NOT NULL AND product_id <> ''
            GROUP BY 1,2
        ),
        totals AS (
            SELECT SUM(revenue) AS total_revenue FROM prod_rollup
        ),
        enriched AS (
            SELECT
                product_id,
                product_name,
                revenue,
                CASE WHEN cost_count > 0 THEN cost_sum ELSE NULL END AS cost,
                CASE WHEN cost_count > 0 THEN revenue - cost_sum ELSE NULL END AS profit,
                CASE WHEN revenue > 0 AND cost_count > 0 THEN (revenue - cost_sum) / revenue * 100 ELSE NULL END AS margin_pct,
                units,
                weight_lb,
                orders,
                customers,
                CASE WHEN units > 0 THEN revenue / NULLIF(units, 0) ELSE NULL END AS avg_sale_price,
                CASE WHEN cost_count > 0 AND units > 0 THEN cost_sum / NULLIF(units, 0) ELSE NULL END AS avg_cost_per_unit
            FROM prod_rollup
        ),
        ranked AS (
            SELECT * FROM enriched ORDER BY revenue DESC, product_name LIMIT {top_n if top_n > 0 else TOP_N_MAX}
        ),
        conc AS (
            SELECT
                CASE
                    WHEN t.total_revenue > 0 THEN (
                        SELECT SUM(POWER(pr.revenue / t.total_revenue, 2)) * 10000
                        FROM prod_rollup pr
                    )
                    ELSE NULL
                END AS hhi,
                CASE
                    WHEN t.total_revenue > 0 THEN (
                        SELECT SUM(revenue) / t.total_revenue * 100
                        FROM (SELECT revenue FROM prod_rollup ORDER BY revenue DESC LIMIT 1)
                    )
                    ELSE NULL
                END AS top1_share,
                CASE
                    WHEN t.total_revenue > 0 THEN (
                        SELECT SUM(revenue) / t.total_revenue * 100
                        FROM (SELECT revenue FROM prod_rollup ORDER BY revenue DESC LIMIT 5)
                    )
                    ELSE NULL
                END AS top5_share
            FROM totals t
        )
        SELECT
            (SELECT list(struct_pack(
                product_id:=product_id,
                product_name:=product_name,
                revenue:=revenue,
                cost:=cost,
                profit:=profit,
                margin_pct:=margin_pct,
                units:=units,
                weight_lb:=weight_lb,
                orders:=orders,
                customers:=customers,
                avg_sale_price:=avg_sale_price,
                avg_cost_per_unit:=avg_cost_per_unit
            )) FROM ranked) AS top_products,
            (SELECT list(
                CASE WHEN units > 0 THEN revenue / NULLIF(units, 0) ELSE NULL END
            ) FROM scoped_supplier WHERE units > 0) AS unit_prices,
            (SELECT
                struct_pack(
                    p10:=quantile_cont(CASE WHEN units > 0 THEN revenue / NULLIF(units, 0) ELSE NULL END, 0.10),
                    p50:=quantile_cont(CASE WHEN units > 0 THEN revenue / NULLIF(units, 0) ELSE NULL END, 0.50),
                    p90:=quantile_cont(CASE WHEN units > 0 THEN revenue / NULLIF(units, 0) ELSE NULL END, 0.90)
                )
             FROM scoped_supplier) AS unit_price_stats,
            (SELECT
                struct_pack(
                    p10:=quantile_cont(CASE WHEN revenue > 0 AND cost IS NOT NULL THEN (revenue - cost) / revenue * 100 ELSE NULL END, 0.10),
                    p50:=quantile_cont(CASE WHEN revenue > 0 AND cost IS NOT NULL THEN (revenue - cost) / revenue * 100 ELSE NULL END, 0.50),
                    p90:=quantile_cont(CASE WHEN revenue > 0 AND cost IS NOT NULL THEN (revenue - cost) / revenue * 100 ELSE NULL END, 0.90)
                )
             FROM scoped_supplier) AS margin_stats,
            (SELECT struct_pack(hhi:=hhi, top1_share:=top1_share, top5_share:=top5_share) FROM conc) AS concentration
    """


def _drilldown_customers_sql(scoped_supplier_cte: str, top_n: int) -> str:
    return f"""
        WITH
        {scoped_supplier_cte},
        cust_rollup AS (
            SELECT
                customer_id,
                customer_name,
                SUM(revenue) AS revenue,
                SUM(cost) AS cost_sum,
                SUM(CASE WHEN cost IS NULL THEN 0 ELSE 1 END) AS cost_count,
                COUNT(DISTINCT CASE WHEN order_id IS NOT NULL AND order_id <> '' THEN order_id END) AS orders,
                COUNT(DISTINCT CASE WHEN product_id IS NOT NULL AND product_id <> '' THEN product_id END) AS products
            FROM scoped_supplier
            WHERE customer_id IS NOT NULL AND customer_id <> ''
            GROUP BY 1,2
        ),
        ranked AS (
            SELECT * FROM cust_rollup ORDER BY revenue DESC, customer_name LIMIT {top_n if top_n > 0 else TOP_N_MAX}
        ),
        totals AS (
            SELECT SUM(revenue) AS total_revenue FROM cust_rollup
        ),
        conc AS (
            SELECT
                CASE
                    WHEN t.total_revenue > 0 THEN (
                        SELECT SUM(POWER(cr.revenue / t.total_revenue, 2)) * 10000
                        FROM cust_rollup cr
                    )
                    ELSE NULL
                END AS hhi,
                CASE
                    WHEN t.total_revenue > 0 THEN (
                        SELECT SUM(revenue) / t.total_revenue * 100
                        FROM (SELECT revenue FROM cust_rollup ORDER BY revenue DESC LIMIT 1)
                    )
                    ELSE NULL
                END AS top1_share,
                CASE
                    WHEN t.total_revenue > 0 THEN (
                        SELECT SUM(revenue) / t.total_revenue * 100
                        FROM (SELECT revenue FROM cust_rollup ORDER BY revenue DESC LIMIT 5)
                    )
                    ELSE NULL
                END AS top5_share
            FROM totals t
        )
        SELECT
            (SELECT list(struct_pack(
                customer_id:=customer_id,
                customer_name:=customer_name,
                revenue:=revenue,
                cost:=CASE WHEN cost_count > 0 THEN cost_sum ELSE NULL END,
                profit:=CASE WHEN cost_count > 0 THEN revenue - cost_sum ELSE NULL END,
                margin_pct:=CASE WHEN revenue > 0 AND cost_count > 0 THEN (revenue - cost_sum) / revenue * 100 ELSE NULL END,
                orders:=orders,
                products:=products
            )) FROM ranked) AS top_customers,
            (SELECT struct_pack(hhi:=hhi, top1_share:=top1_share, top5_share:=top5_share) FROM conc) AS concentration
    """


def _drilldown_detail_rows_sql(scoped_supplier_cte: str) -> str:
    return f"""
        WITH
        {scoped_supplier_cte}
        SELECT
            CAST(order_date AS DATE) AS order_date,
            strftime('%Y-%m', date_trunc('month', order_date)) AS month,
            supplier_id,
            supplier_name,
            order_id,
            product_id,
            product_name,
            customer_id,
            customer_name,
            region,
            shipping_method,
            sales_rep,
            protein_family,
            product_category,
            revenue,
            cost,
            units,
            weight_lb,
            CASE WHEN units > 0 THEN revenue / NULLIF(units, 0) ELSE NULL END AS unit_price,
            CASE WHEN weight_lb > 0 THEN revenue / NULLIF(weight_lb, 0) ELSE NULL END AS asp_lb,
            CASE WHEN revenue > 0 AND cost IS NOT NULL THEN (revenue - cost) / revenue * 100 ELSE NULL END AS margin_pct
        FROM scoped_supplier
        WHERE order_date IS NOT NULL
        ORDER BY order_date ASC
    """


def _series_quantile(values: pd.Series, q: float) -> float | None:
    if values is None or values.empty:
        return None
    s = pd.to_numeric(values, errors="coerce").dropna()
    if s.empty:
        return None
    try:
        return float(s.quantile(q))
    except Exception:
        return None


def _clamp(value: float | None, lo: float = 0.0, hi: float = 100.0) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except Exception:
        return None
    if num < lo:
        return lo
    if num > hi:
        return hi
    return num


def _hhi_and_shares(frame: pd.DataFrame, value_col: str) -> Dict[str, Any]:
    if frame is None or frame.empty or value_col not in frame.columns:
        return {"hhi": None, "top1_share": None, "top5_share": None, "top10_share": None}
    vals = pd.to_numeric(frame[value_col], errors="coerce").fillna(0.0)
    vals = vals[vals > 0]
    if vals.empty:
        return {"hhi": None, "top1_share": None, "top5_share": None, "top10_share": None}
    vals = vals.sort_values(ascending=False).reset_index(drop=True)
    total = float(vals.sum())
    if total <= 0:
        return {"hhi": None, "top1_share": None, "top5_share": None, "top10_share": None}
    shares = vals / total
    return {
        "hhi": float((shares.pow(2).sum()) * 10000.0),
        "top1_share": float(shares.head(1).sum() * 100.0),
        "top5_share": float(shares.head(5).sum() * 100.0),
        "top10_share": float(shares.head(10).sum() * 100.0),
    }


def _health_label(score: float | None) -> str:
    if score is None:
        return "Watch"
    if score >= 70:
        return "Strong"
    if score >= 45:
        return "Watch"
    return "Risk"


def _month_labels(series: pd.Series) -> pd.Series:
    dt = pd.to_datetime(series, errors="coerce")
    return dt.dt.to_period("M").astype("string")


def _display_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.lower() in {"nan", "none", "null", "<na>"}:
        return None
    return text


def _supplier_product_label(product_id: Any, product_name: Any) -> str:
    sku_text = _display_text(product_id)
    name_text = _display_text(product_name)
    if sku_text and name_text:
        return presentation.format_product_label(sku_text, name_text)
    if sku_text:
        return presentation.format_product_label(sku_text, "Unnamed Product")
    if name_text:
        return name_text
    return "Unknown Product"


def _product_display_fields(product_id: Any, product_name: Any) -> Dict[str, str]:
    sku_text = _display_text(product_id)
    name_for_display = _display_text(product_name) or ("Unnamed Product" if sku_text else None)
    full_label = _supplier_product_label(sku_text, name_for_display)
    return {
        "display_name": full_label,
        "display_name_short": presentation.compact_product_label(sku_text, name_for_display, max_length=56, fallback=full_label),
        "display_name_axis": presentation.compact_product_label(sku_text, name_for_display, max_length=34, fallback=full_label),
    }


def _rolling(values: Sequence[float], window_size: int = 3) -> list[float | None]:
    out: list[float | None] = []
    if not values:
        return out
    s = pd.Series(list(values), dtype="float64")
    roll = s.rolling(window=window_size, min_periods=1).mean()
    for val in roll.tolist():
        if pd.isna(val):
            out.append(None)
        else:
            out.append(float(val))
    return out


def _supplier_drilldown_v2_payload(
    *,
    supplier_id: str,
    supplier_name: str,
    detail_df: pd.DataFrame,
    kpis: Mapping[str, Any],
    top_n: int,
    start_iso: str | None,
    end_iso: str | None,
    prior_start_iso: str | None,
    prior_end_iso: str | None,
) -> Dict[str, Any]:
    empty_payload = {
        "window": {
            "start": start_iso,
            "end": end_iso,
            "prior_start": prior_start_iso,
            "prior_end": prior_end_iso,
        },
        "header": {
            "narrative": "Supplier detail will populate when scoped supplier activity is available for the active filter window.",
            "coverage_note": "Cost coverage and protein mapping update from the active scoped rows.",
            "top_gainer": None,
            "top_decliner": None,
            "top_protein": None,
            "concentration_posture": "No data",
            "data_notice": "No scoped supplier rows were returned for the current filters.",
        },
        "scorecard": {
            "supplier_id": supplier_id,
            "supplier_name": supplier_name,
            "total_revenue": _clean_float(kpis.get("revenue")),
            "total_profit": _clean_optional_float(kpis.get("profit")),
            "gross_margin_pct": _clean_optional_float(kpis.get("margin_pct")),
            "orders": _clean_int(kpis.get("orders")),
            "units": _clean_float(kpis.get("units")),
            "weight_lb": _clean_float(kpis.get("weight_lb")),
            "active_skus": _clean_int(kpis.get("products")),
            "active_customers": _clean_int(kpis.get("customers")),
            "asp_lb": None,
            "asp_lb_delta_pct": None,
            "last_sold": None,
            "cost_coverage_pct": None,
            "revenue_delta_mom": None,
            "revenue_delta_mom_pct": None,
            "margin_volatility": None,
            "customer_hhi": None,
            "sku_hhi": None,
            "customer_top1_share": None,
            "customer_top5_share": None,
            "sku_top1_share": None,
            "sku_top5_share": None,
            "top_protein": None,
            "top_protein_share_pct": None,
            "top_category": None,
            "top_category_share_pct": None,
            "top_sku": None,
            "top_sku_share_pct": None,
            "protein_breadth": 0,
            "category_breadth": 0,
            "mapped_protein_share_pct": None,
            "customers_for_80_pct": None,
            "skus_for_80_pct": None,
            "days_since_last_order": _clean_int(kpis.get("days_since_last_order")),
            "missing_cost_revenue": None,
            "below_target_revenue": None,
            "health_score": None,
            "health_label": "Watch",
            "health_formula": "Weighted score: margin 25%, stability 20%, growth 20%, concentration 20%, coverage 15%.",
            "cost_missing_rows": 0,
            "rows_total": 0,
            "no_data": True,
        },
        "trend": {
            "labels": [],
            "revenue": [],
            "profit": [],
            "margin_pct": [],
            "orders": [],
            "rolling_revenue_3m": [],
            "rolling_profit_3m": [],
            "rolling_margin_3m": [],
            "narrative": "Monthly trend will appear once the active scope includes supplier history.",
        },
        "mix": {
            "top_products_revenue": [],
            "top_products_profit": [],
            "top_customers": [],
            "customer_concentration": {"hhi": None, "top1_share": None, "top5_share": None, "top10_share": None},
            "product_concentration": {"hhi": None, "top1_share": None, "top5_share": None, "top10_share": None, "skus_for_80_pct": None},
            "narrative": "Concentration diagnostics populate from visible SKU and customer spend.",
        },
        "protein": {
            "rows": [],
            "category_rows": [],
            "focus_cards": [],
            "narrative": "Protein and category exposure will appear once mapped supplier rows are available.",
            "category_narrative": "Category breadth is unavailable for the current supplier scope.",
        },
        "pricing": {
            "asp_lb_stats": {"p10": None, "p50": None, "p90": None},
            "margin_stats": {"p10": None, "p50": None, "p90": None},
            "asp_lb_samples": [],
            "margin_samples": [],
            "guardrails": {"high_outliers": 0, "low_outliers": 0},
            "price_velocity": [],
            "elasticity": {"correlation": None, "slope": None, "method": "indicative", "insufficient_variation": True},
            "outliers": [],
            "narrative": "Pricing diagnostics require scoped supplier rows with shipped weight and cost coverage.",
        },
        "customers": {
            "top_rows": [],
            "decliners": [],
            "summary": {
                "customer_count": 0,
                "decliner_count": 0,
                "repeat_customer_revenue_share_pct": None,
                "new_customer_revenue_share_pct": None,
                "returning_customer_revenue_share_pct": None,
                "top_customer_name": None,
                "top_customer_share_pct": None,
                "customers_for_80_pct": None,
            },
            "narrative": "Customer reach and dependence will populate once the supplier has scoped customer activity.",
        },
        "products_table": {"rows": [], "total_rows": 0},
        "opportunities": {"margin_at_risk": [], "pricing_fixes": [], "promote_candidates": [], "data_quality_fixes": []},
        "playbook": {
            "goal": "Stabilize and grow supplier contribution",
            "actions": [],
            "cards": [],
            "narrative": "Action guidance appears when revenue, margin, and trust signals are available together.",
        },
        "notices": [],
    }

    if detail_df is None or detail_df.empty:
        return empty_payload

    df = detail_df.copy()
    df["order_date"] = pd.to_datetime(df.get("order_date"), errors="coerce")
    df = df[df["order_date"].notna()].copy()
    if df.empty:
        return empty_payload
    df["month"] = _month_labels(df["order_date"])
    df["revenue"] = pd.to_numeric(df.get("revenue"), errors="coerce").fillna(0.0)
    df["cost"] = pd.to_numeric(df.get("cost"), errors="coerce")
    df["units"] = pd.to_numeric(df.get("units"), errors="coerce").fillna(0.0)
    df["weight_lb"] = pd.to_numeric(df.get("weight_lb"), errors="coerce").fillna(0.0)
    df["unit_price"] = pd.to_numeric(df.get("unit_price"), errors="coerce")
    df["asp_lb"] = pd.to_numeric(df.get("asp_lb"), errors="coerce")
    df["margin_pct"] = pd.to_numeric(df.get("margin_pct"), errors="coerce")
    df["profit"] = (df["revenue"] - df["cost"]).where(df["cost"].notna())
    for col in ("product_id", "product_name", "customer_id", "customer_name", "protein_family", "product_category"):
        if col not in df.columns:
            df[col] = pd.Series(pd.NA, index=df.index, dtype="string")
        df[col] = df[col].astype("string").str.strip().replace({"nan": pd.NA, "None": pd.NA, "null": pd.NA, "<NA>": pd.NA})
    df["product_name"] = df["product_name"].fillna(pd.Series(["Unnamed Product"] * len(df.index), index=df.index, dtype="string")).replace({"": "Unnamed Product"})
    df["protein_family"] = df["protein_family"].fillna("Unassigned").replace({"": "Unassigned"})
    df["product_category"] = df["product_category"].where(
        df["product_category"].notna() & (df["product_category"] != ""),
        df["protein_family"],
    )
    df["product_label"] = df.apply(lambda row: _supplier_product_label(row.get("product_id"), row.get("product_name")), axis=1)

    curr_start = pd.to_datetime(start_iso, errors="coerce")
    curr_end = pd.to_datetime(end_iso, errors="coerce")
    prior_start = pd.to_datetime(prior_start_iso, errors="coerce")
    prior_end = pd.to_datetime(prior_end_iso, errors="coerce")
    if pd.isna(curr_start):
        curr_start = df["order_date"].min()
    if pd.isna(curr_end):
        curr_end = df["order_date"].max()
    curr_mask = (df["order_date"] >= curr_start) & (df["order_date"] <= curr_end)
    prior_mask = (df["order_date"] >= prior_start) & (df["order_date"] <= prior_end) if pd.notna(prior_start) and pd.notna(prior_end) else pd.Series(False, index=df.index)

    curr = df[curr_mask].copy()
    prior = df[prior_mask].copy()
    if curr.empty:
        return empty_payload

    month_rollup = (
        curr.groupby("month", dropna=False)
        .agg(
            revenue=("revenue", "sum"),
            profit=("profit", "sum"),
            orders=("order_id", lambda s: s.astype("string").replace("", pd.NA).dropna().nunique()),
            cost_known=("cost", lambda s: s.notna().sum()),
            cost_sum=("cost", "sum"),
        )
        .reset_index()
        .sort_values("month")
    )
    month_rollup["margin_pct"] = ((month_rollup["profit"] / month_rollup["revenue"]) * 100.0).where(month_rollup["revenue"] > 0)
    trend_labels = [str(v) for v in month_rollup["month"].tolist() if str(v)]
    trend_revenue = [_clean_float(v) for v in month_rollup["revenue"].tolist()]
    trend_profit = [_clean_optional_float(v) for v in month_rollup["profit"].tolist()]
    trend_margin = [_clean_optional_float(v) for v in month_rollup["margin_pct"].tolist()]
    trend_orders = [_clean_int(v) for v in month_rollup["orders"].tolist()]

    mom_delta = None
    mom_delta_pct = None
    if len(trend_revenue) >= 2:
        mom_delta = trend_revenue[-1] - trend_revenue[-2]
        mom_delta_pct = (mom_delta / trend_revenue[-2] * 100.0) if trend_revenue[-2] > 0 else None

    product_cols = ["product_id", "product_name"]
    product_rollup = (
        curr.groupby(product_cols, dropna=False)
        .agg(
            revenue=("revenue", "sum"),
            cost_sum=("cost", "sum"),
            cost_known_rows=("cost", lambda s: s.notna().sum()),
            protein_family=("protein_family", _mode_text),
            product_category=("product_category", _mode_text),
            orders=("order_id", lambda s: s.astype("string").replace("", pd.NA).dropna().nunique()),
            units=("units", "sum"),
            weight_lb=("weight_lb", "sum"),
            customers=("customer_id", lambda s: s.astype("string").replace("", pd.NA).dropna().nunique()),
            last_order=("order_date", "max"),
        )
        .reset_index()
    )
    prior_product = (
        prior.groupby("product_id", dropna=False)
        .agg(revenue_prior=("revenue", "sum"), units_prior=("units", "sum"), weight_prior=("weight_lb", "sum"))
        .reset_index()
    )
    if not product_rollup.empty:
        product_rollup = product_rollup.merge(prior_product, on="product_id", how="left")
    else:
        product_rollup["revenue_prior"] = pd.Series(dtype="float64")
        product_rollup["units_prior"] = pd.Series(dtype="float64")
        product_rollup["weight_prior"] = pd.Series(dtype="float64")
    product_rollup["revenue_prior"] = pd.to_numeric(product_rollup.get("revenue_prior"), errors="coerce").fillna(0.0)
    product_rollup["units_prior"] = pd.to_numeric(product_rollup.get("units_prior"), errors="coerce").fillna(0.0)
    product_rollup["weight_prior"] = pd.to_numeric(product_rollup.get("weight_prior"), errors="coerce").fillna(0.0)
    product_rollup["cost"] = product_rollup["cost_sum"].where(product_rollup["cost_known_rows"] > 0)
    product_rollup["profit"] = (product_rollup["revenue"] - product_rollup["cost"]).where(product_rollup["cost"].notna())
    product_rollup["margin_pct"] = ((product_rollup["profit"] / product_rollup["revenue"]) * 100.0).where(product_rollup["revenue"] > 0)
    product_rollup["asp_lb"] = (product_rollup["revenue"] / product_rollup["weight_lb"]).where(product_rollup["weight_lb"] > 0)
    product_rollup["asp_lb_prior"] = (product_rollup["revenue_prior"] / product_rollup["weight_prior"]).where(product_rollup["weight_prior"] > 0)
    product_rollup["asp_lb_delta_pct"] = (
        (product_rollup["asp_lb"] - product_rollup["asp_lb_prior"]) / product_rollup["asp_lb_prior"] * 100.0
    ).where(product_rollup["asp_lb_prior"] > 0)
    product_rollup["delta_revenue"] = product_rollup["revenue"] - product_rollup["revenue_prior"]
    total_product_revenue = float(product_rollup["revenue"].sum()) if not product_rollup.empty else 0.0
    if total_product_revenue > 0:
        product_rollup["revenue_share_pct"] = product_rollup["revenue"] / total_product_revenue * 100.0
    else:
        product_rollup["revenue_share_pct"] = pd.Series([None] * len(product_rollup), index=product_rollup.index, dtype="float64")
    product_rollup["last_sold"] = pd.to_datetime(product_rollup["last_order"], errors="coerce").dt.date.astype("string")
    product_rollup["protein_family"] = product_rollup["protein_family"].fillna("Unassigned").replace({"": "Unassigned"})
    product_rollup["product_category"] = product_rollup["product_category"].fillna(product_rollup["protein_family"])

    customer_rollup = (
        curr.groupby(["customer_id", "customer_name"], dropna=False)
        .agg(
            revenue=("revenue", "sum"),
            cost_sum=("cost", "sum"),
            cost_known_rows=("cost", lambda s: s.notna().sum()),
            orders=("order_id", lambda s: s.astype("string").replace("", pd.NA).dropna().nunique()),
            units=("units", "sum"),
            last_order=("order_date", "max"),
        )
        .reset_index()
    )
    prior_customer = (
        prior.groupby("customer_id", dropna=False)
        .agg(revenue_prior=("revenue", "sum"))
        .reset_index()
    )
    if not customer_rollup.empty:
        customer_rollup = customer_rollup.merge(prior_customer, on="customer_id", how="left")
    else:
        customer_rollup["revenue_prior"] = pd.Series(dtype="float64")
    customer_rollup["revenue_prior"] = pd.to_numeric(customer_rollup.get("revenue_prior"), errors="coerce").fillna(0.0)
    customer_rollup["cost"] = customer_rollup["cost_sum"].where(customer_rollup["cost_known_rows"] > 0)
    customer_rollup["profit"] = (customer_rollup["revenue"] - customer_rollup["cost"]).where(customer_rollup["cost"].notna())
    customer_rollup["margin_pct"] = ((customer_rollup["profit"] / customer_rollup["revenue"]) * 100.0).where(customer_rollup["revenue"] > 0)
    customer_rollup["delta_revenue"] = customer_rollup["revenue"] - customer_rollup["revenue_prior"]
    customer_rollup["last_order"] = pd.to_datetime(customer_rollup["last_order"], errors="coerce")
    customer_rollup["last_order_date"] = customer_rollup["last_order"].dt.date.astype("string")

    customer_conc = _hhi_and_shares(customer_rollup, "revenue")
    product_conc = _hhi_and_shares(product_rollup, "revenue")
    customer_conc["customers_for_80_pct"] = _items_for_cumulative_share(customer_rollup["revenue"], 80.0)
    product_conc["skus_for_80_pct"] = _items_for_cumulative_share(product_rollup["revenue"], 80.0)

    protein_rollup = (
        curr.groupby("protein_family", dropna=False)
        .agg(
            revenue=("revenue", "sum"),
            cost_sum=("cost", "sum"),
            cost_known_rows=("cost", lambda s: s.notna().sum()),
            units=("units", "sum"),
            weight_lb=("weight_lb", "sum"),
            orders=("order_id", lambda s: s.astype("string").replace("", pd.NA).dropna().nunique()),
            sku_count=("product_id", lambda s: s.astype("string").replace("", pd.NA).dropna().nunique()),
            customer_count=("customer_id", lambda s: s.astype("string").replace("", pd.NA).dropna().nunique()),
        )
        .reset_index()
    )
    prior_protein = (
        prior.groupby("protein_family", dropna=False)
        .agg(revenue_prior=("revenue", "sum"))
        .reset_index()
    )
    protein_rollup = protein_rollup.merge(prior_protein, on="protein_family", how="left")
    protein_rollup["revenue_prior"] = pd.to_numeric(protein_rollup.get("revenue_prior"), errors="coerce").fillna(0.0)
    protein_rollup["cost"] = protein_rollup["cost_sum"].where(protein_rollup["cost_known_rows"] > 0)
    protein_rollup["profit"] = (protein_rollup["revenue"] - protein_rollup["cost"]).where(protein_rollup["cost"].notna())
    protein_rollup["margin_pct"] = ((protein_rollup["profit"] / protein_rollup["revenue"]) * 100.0).where(protein_rollup["revenue"] > 0)
    total_protein_revenue = float(pd.to_numeric(protein_rollup["revenue"], errors="coerce").fillna(0.0).sum())
    total_prior_protein_revenue = float(pd.to_numeric(protein_rollup["revenue_prior"], errors="coerce").fillna(0.0).sum())
    if total_protein_revenue > 0:
        protein_rollup["revenue_share_pct"] = (protein_rollup["revenue"] / total_protein_revenue) * 100.0
    else:
        protein_rollup["revenue_share_pct"] = pd.Series(float("nan"), index=protein_rollup.index, dtype="float64")
    if total_prior_protein_revenue > 0:
        protein_rollup["prior_share_pct"] = (protein_rollup["revenue_prior"] / total_prior_protein_revenue) * 100.0
    else:
        protein_rollup["prior_share_pct"] = pd.Series(float("nan"), index=protein_rollup.index, dtype="float64")
    protein_rollup["mix_shift_pp"] = protein_rollup["revenue_share_pct"] - protein_rollup["prior_share_pct"]
    protein_rollup["revenue_delta"] = protein_rollup["revenue"] - protein_rollup["revenue_prior"]

    protein_category_rollup = (
        curr.groupby(["protein_family", "product_category"], dropna=False)["revenue"]
        .sum()
        .reset_index()
        .sort_values(["protein_family", "revenue"], ascending=[True, False])
    )
    lead_category_by_protein = (
        protein_category_rollup.groupby("protein_family", dropna=False)
        .head(1)[["protein_family", "product_category"]]
        .rename(columns={"product_category": "lead_category"})
    )
    protein_rollup = protein_rollup.merge(lead_category_by_protein, on="protein_family", how="left")
    protein_rollup["lead_category"] = protein_rollup["lead_category"].fillna(protein_rollup["protein_family"])

    category_rollup = (
        curr.groupby("product_category", dropna=False)
        .agg(
            revenue=("revenue", "sum"),
            cost_sum=("cost", "sum"),
            cost_known_rows=("cost", lambda s: s.notna().sum()),
            units=("units", "sum"),
            weight_lb=("weight_lb", "sum"),
            orders=("order_id", lambda s: s.astype("string").replace("", pd.NA).dropna().nunique()),
            sku_count=("product_id", lambda s: s.astype("string").replace("", pd.NA).dropna().nunique()),
            customer_count=("customer_id", lambda s: s.astype("string").replace("", pd.NA).dropna().nunique()),
        )
        .reset_index()
    )
    prior_category = (
        prior.groupby("product_category", dropna=False)
        .agg(revenue_prior=("revenue", "sum"))
        .reset_index()
    )
    category_rollup = category_rollup.merge(prior_category, on="product_category", how="left")
    category_rollup["revenue_prior"] = pd.to_numeric(category_rollup.get("revenue_prior"), errors="coerce").fillna(0.0)
    category_rollup["cost"] = category_rollup["cost_sum"].where(category_rollup["cost_known_rows"] > 0)
    category_rollup["profit"] = (category_rollup["revenue"] - category_rollup["cost"]).where(category_rollup["cost"].notna())
    category_rollup["margin_pct"] = ((category_rollup["profit"] / category_rollup["revenue"]) * 100.0).where(category_rollup["revenue"] > 0)
    total_category_revenue = float(pd.to_numeric(category_rollup["revenue"], errors="coerce").fillna(0.0).sum())
    total_prior_category_revenue = float(pd.to_numeric(category_rollup["revenue_prior"], errors="coerce").fillna(0.0).sum())
    if total_category_revenue > 0:
        category_rollup["revenue_share_pct"] = (category_rollup["revenue"] / total_category_revenue) * 100.0
    else:
        category_rollup["revenue_share_pct"] = pd.Series(float("nan"), index=category_rollup.index, dtype="float64")
    if total_prior_category_revenue > 0:
        category_rollup["prior_share_pct"] = (category_rollup["revenue_prior"] / total_prior_category_revenue) * 100.0
    else:
        category_rollup["prior_share_pct"] = pd.Series(float("nan"), index=category_rollup.index, dtype="float64")
    category_rollup["mix_shift_pp"] = category_rollup["revenue_share_pct"] - category_rollup["prior_share_pct"]
    category_rollup["revenue_delta"] = category_rollup["revenue"] - category_rollup["revenue_prior"]

    asp_lb_samples = curr["asp_lb"].dropna()
    margins = curr["margin_pct"].dropna()
    asp_lb_stats = {
        "p10": _series_quantile(asp_lb_samples, 0.10),
        "p50": _series_quantile(asp_lb_samples, 0.50),
        "p90": _series_quantile(asp_lb_samples, 0.90),
    }
    margin_stats = {
        "p10": _series_quantile(margins, 0.10),
        "p50": _series_quantile(margins, 0.50),
        "p90": _series_quantile(margins, 0.90),
    }
    high_cut = (asp_lb_stats["p90"] * 1.15) if asp_lb_stats["p90"] is not None else None
    low_cut = (asp_lb_stats["p10"] / 1.15) if asp_lb_stats["p10"] not in {None, 0} else None

    peer_median = asp_lb_stats["p50"]
    outliers_rows: list[dict[str, Any]] = []
    high_outliers = 0
    low_outliers = 0
    for _, row in product_rollup.sort_values("revenue", ascending=False).iterrows():
        asp_lb = _clean_optional_float(row.get("asp_lb"))
        if asp_lb is None:
            continue
        is_high = bool(high_cut is not None and asp_lb > high_cut)
        is_low = bool(low_cut is not None and asp_lb < low_cut)
        if not (is_high or is_low):
            continue
        if is_high:
            high_outliers += 1
        if is_low:
            low_outliers += 1
        delta_peer = ((asp_lb - peer_median) / peer_median * 100.0) if peer_median and peer_median > 0 else None
        display = _product_display_fields(row.get("product_id"), row.get("product_name"))
        outliers_rows.append(
            {
                "product_id": row.get("product_id"),
                "product_name": row.get("product_name") or row.get("product_id"),
                "asp_lb": asp_lb,
                "peer_median": _clean_optional_float(peer_median),
                "delta_pct_vs_peer": _clean_optional_float(delta_peer),
                "revenue": _clean_float(row.get("revenue")),
                "margin_pct": _clean_optional_float(row.get("margin_pct")),
                "last_sold": _date_to_iso(row.get("last_sold")),
                "outlier_type": "high" if is_high else "low",
                **display,
            }
        )

    window_days = max(int((curr_end - curr_start).days) + 1 if pd.notna(curr_end) and pd.notna(curr_start) else 30, 1)
    window_months = max(window_days / 30.0, 1.0)
    product_rollup["velocity_units_per_month"] = product_rollup["units"] / window_months
    price_velocity_rows: list[dict[str, Any]] = []
    for _, row in product_rollup.iterrows():
        display = _product_display_fields(row.get("product_id"), row.get("product_name"))
        price_velocity_rows.append(
            {
                "product_id": row.get("product_id"),
                "product_name": row.get("product_name") or row.get("product_id"),
                "asp_lb": _clean_optional_float(row.get("asp_lb")),
                "velocity": _clean_optional_float(row.get("velocity_units_per_month")),
                "revenue_share_pct": _clean_optional_float(row.get("revenue_share_pct")),
                "margin_pct": _clean_optional_float(row.get("margin_pct")),
                **display,
            }
        )

    pv = pd.DataFrame.from_records(price_velocity_rows)
    elasticity = {
        "correlation": None,
        "slope": None,
        "method": "indicative",
        "insufficient_variation": True,
    }
    if not pv.empty:
        pv["asp_lb"] = pd.to_numeric(pv["asp_lb"], errors="coerce")
        pv["velocity"] = pd.to_numeric(pv["velocity"], errors="coerce")
        pv = pv.dropna(subset=["asp_lb", "velocity"])
        if len(pv.index) >= 3 and pv["asp_lb"].nunique() > 1 and pv["velocity"].nunique() > 1:
            corr = pv["asp_lb"].corr(pv["velocity"])
            var_x = float(pv["asp_lb"].var(ddof=0))
            cov_xy = float(((pv["asp_lb"] - pv["asp_lb"].mean()) * (pv["velocity"] - pv["velocity"].mean())).mean())
            slope = (cov_xy / var_x) if var_x > 0 else None
            elasticity = {
                "correlation": _clean_optional_float(corr),
                "slope": _clean_optional_float(slope),
                "method": "linear_proxy",
                "insufficient_variation": False,
            }

    product_rollup["effective_cost_basis"] = pd.to_numeric(product_rollup.get("cost"), errors="coerce")
    if "cost_lb" in product_rollup.columns:
        product_rollup["effective_cost_lb"] = pd.to_numeric(product_rollup.get("cost_lb"), errors="coerce")
    product_rollup = margin_rules.annotate_margin_frame(
        product_rollup,
        protein_col="protein_family",
        category_col="product_category",
        revenue_col="revenue",
        cost_col="cost",
        profit_col="profit",
        margin_col="margin_pct",
        unit_cost_col="cost_lb" if "cost_lb" in product_rollup.columns else "margin_pct",
    )
    _target_margin = margin_rules.weighted_target_margin_pct(product_rollup.to_dict(orient="records"))
    margin_at_risk = product_rollup[
        (pd.to_numeric(product_rollup["revenue"], errors="coerce") > 0)
        & (pd.to_numeric(product_rollup["margin_pct"], errors="coerce") < pd.to_numeric(product_rollup["target_margin_pct"], errors="coerce"))
        & (product_rollup["cost"].notna())
    ].copy()
    margin_at_risk["target_profit"] = pd.to_numeric(margin_at_risk.get("target_profit"), errors="coerce")
    margin_at_risk["uplift_to_target"] = pd.to_numeric(margin_at_risk.get("profit_uplift_to_target"), errors="coerce").fillna(0.0)
    margin_at_risk = margin_at_risk.sort_values(["uplift_to_target", "revenue"], ascending=[False, False])

    declining_customers = customer_rollup[customer_rollup["delta_revenue"] < 0].copy()
    declining_customers = declining_customers.sort_values("delta_revenue", ascending=True)

    customer_behavior = (
        curr.groupby("customer_id", dropna=False)
        .agg(
            revenue=("revenue", "sum"),
            order_count=("order_id", lambda s: s.astype("string").replace("", pd.NA).dropna().nunique()),
        )
        .reset_index()
    )
    if not customer_behavior.empty:
        customer_behavior = customer_behavior.merge(
            prior_customer.rename(columns={"revenue_prior": "revenue_prior_customer"}),
            on="customer_id",
            how="left",
        )
    customer_behavior["revenue_prior_customer"] = pd.to_numeric(
        customer_behavior.get("revenue_prior_customer"), errors="coerce"
    ).fillna(0.0)
    total_customer_revenue = float(pd.to_numeric(customer_behavior.get("revenue"), errors="coerce").fillna(0.0).sum())
    if total_customer_revenue > 0:
        customer_rollup["revenue_share_pct"] = (customer_rollup["revenue"] / total_customer_revenue) * 100.0
    else:
        customer_rollup["revenue_share_pct"] = pd.Series(float("nan"), index=customer_rollup.index, dtype="float64")
    repeat_revenue = float(
        pd.to_numeric(
            customer_behavior.loc[pd.to_numeric(customer_behavior.get("order_count"), errors="coerce").fillna(0) >= 2, "revenue"],
            errors="coerce",
        ).fillna(0.0).sum()
    ) if not customer_behavior.empty else 0.0
    new_revenue = float(
        pd.to_numeric(
            customer_behavior.loc[pd.to_numeric(customer_behavior.get("revenue_prior_customer"), errors="coerce").fillna(0) <= 0, "revenue"],
            errors="coerce",
        ).fillna(0.0).sum()
    ) if not customer_behavior.empty else 0.0
    repeat_share_pct = (repeat_revenue / total_customer_revenue * 100.0) if total_customer_revenue > 0 else None
    new_share_pct = (new_revenue / total_customer_revenue * 100.0) if total_customer_revenue > 0 else None
    returning_share_pct = (100.0 - new_share_pct) if new_share_pct is not None else None

    product_monthly = (
        curr.groupby(["product_id", "month"], dropna=False)
        .agg(revenue=("revenue", "sum"))
        .reset_index()
    )
    prod_vol_map: dict[str, float] = {}
    if not product_monthly.empty:
        for pid, grp in product_monthly.groupby("product_id"):
            vals = pd.to_numeric(grp["revenue"], errors="coerce").dropna()
            mean = float(vals.mean()) if not vals.empty else 0.0
            std = float(vals.std(ddof=0)) if not vals.empty else 0.0
            cv = (std / mean) if mean > 0 else 0.0
            prod_vol_map[str(pid)] = cv

    product_customer_rollup = (
        curr.groupby(["product_id", "customer_id", "customer_name"], dropna=False)
        .agg(revenue=("revenue", "sum"))
        .reset_index()
    )
    top_customer_share_by_product: dict[str, float] = {}
    top_customer_name_by_product: dict[str, str] = {}
    if not product_customer_rollup.empty:
        for pid, grp in product_customer_rollup.groupby("product_id", dropna=False):
            total_rev = float(pd.to_numeric(grp["revenue"], errors="coerce").fillna(0.0).sum())
            if total_rev <= 0:
                continue
            ranked = grp.sort_values("revenue", ascending=False).head(1)
            top_row = ranked.iloc[0]
            top_customer_share_by_product[str(pid)] = float(float(top_row.get("revenue") or 0.0) / total_rev * 100.0)
            top_customer_name_by_product[str(pid)] = str(top_row.get("customer_name") or top_row.get("customer_id") or "")

    products_rows: list[dict[str, Any]] = []
    for _, row in product_rollup.sort_values("revenue", ascending=False).iterrows():
        margin = _clean_optional_float(row.get("margin_pct"))
        row_target_margin = _clean_optional_float(row.get("target_margin_pct"))
        tags: list[str] = []
        if _clean_float(row.get("revenue_share_pct")) >= 10.0:
            tags.append("Top SKU")
        if margin is not None and row_target_margin is not None and margin < row_target_margin:
            tags.append("Below target margin")
        if margin is None:
            tags.append("Fix cost mapping")
        pid = str(row.get("product_id") or "")
        if prod_vol_map.get(pid, 0.0) >= 1.0:
            tags.append("High volatility")
        if top_customer_share_by_product.get(pid, 0.0) >= 50.0:
            tags.append("Customer concentration")
        asp_lb = _clean_optional_float(row.get("asp_lb"))
        if high_cut is not None and asp_lb is not None and asp_lb > high_cut:
            tags.append("Price outlier")
        if low_cut is not None and asp_lb is not None and asp_lb < low_cut:
            tags.append("Price outlier")
        display = _product_display_fields(row.get("product_id"), row.get("product_name"))
        products_rows.append(
            {
                "sku": row.get("product_id"),
                "product_id": row.get("product_id"),
                "product_name": row.get("product_name") or row.get("product_id"),
                "protein_family": row.get("protein_family") or "Unassigned",
                "product_category": row.get("product_category") or row.get("protein_family") or "Unassigned",
                "customers": _clean_int(row.get("customers")),
                "revenue": _clean_float(row.get("revenue")),
                "profit": _clean_optional_float(row.get("profit")),
                "margin_pct": margin,
                "target_margin_pct": row_target_margin,
                "minimum_margin_pct": _clean_optional_float(row.get("minimum_margin_pct")),
                "minimum_price_lb": _clean_optional_float(row.get("minimum_price")),
                "target_price_lb": _clean_optional_float(row.get("target_price")),
                "asp_lb_gap_to_min": _clean_optional_float(row.get("min_price_gap")),
                "asp_lb_gap_to_target": _clean_optional_float(row.get("target_price_gap")),
                "target_achievement_pct": _clean_optional_float(row.get("target_achievement_pct")),
                "target_status": row.get("target_status"),
                "status_key": row.get("status_key"),
                "price_band_status": row.get("price_band_status"),
                "status_color": row.get("status_color"),
                "orders": _clean_int(row.get("orders")),
                "units": _clean_float(row.get("units")),
                "weight_lb": _clean_float(row.get("weight_lb")),
                "asp_lb": asp_lb,
                "cost_lb": _clean_optional_float(row.get("cost_lb")),
                "asp_lb_delta_pct": _clean_optional_float(row.get("asp_lb_delta_pct")),
                "revenue_share_pct": _clean_optional_float(row.get("revenue_share_pct")),
                "top_customer_share_pct": _clean_optional_float(top_customer_share_by_product.get(pid)),
                "top_customer_name": top_customer_name_by_product.get(pid),
                "last_sold": _date_to_iso(row.get("last_sold")),
                "delta_revenue": _clean_float(row.get("delta_revenue")),
                "tags": tags,
                **display,
            }
        )

    top_products_revenue = products_rows[: max(15, top_n if top_n > 0 else 15)]
    top_products_profit = sorted(
        [r for r in products_rows if r.get("profit") is not None],
        key=lambda rec: float(rec.get("profit") or 0.0),
        reverse=True,
    )[:15]

    customers_top_rows: list[dict[str, Any]] = []
    for _, row in customer_rollup.sort_values("revenue", ascending=False).iterrows():
        customers_top_rows.append(
            {
                "customer_id": row.get("customer_id"),
                "customer_name": row.get("customer_name") or row.get("customer_id"),
                "revenue": _clean_float(row.get("revenue")),
                "profit": _clean_optional_float(row.get("profit")),
                "margin_pct": _clean_optional_float(row.get("margin_pct")),
                "orders": _clean_int(row.get("orders")),
                "revenue_share_pct": _clean_optional_float(row.get("revenue_share_pct")),
                "last_order_date": _date_to_iso(row.get("last_order_date")),
                "delta_revenue": _clean_optional_float(row.get("delta_revenue")),
            }
        )
    top_customer_row = customers_top_rows[0] if customers_top_rows else {}

    decline_rows = []
    for _, row in declining_customers.head(15).iterrows():
        decline_rows.append(
            {
                "customer_id": row.get("customer_id"),
                "customer_name": row.get("customer_name") or row.get("customer_id"),
                "revenue_current": _clean_float(row.get("revenue")),
                "revenue_prior": _clean_float(row.get("revenue_prior")),
                "delta_revenue": _clean_float(row.get("delta_revenue")),
                "last_order_date": _date_to_iso(row.get("last_order_date")),
            }
        )

    protein_rows = []
    for _, row in protein_rollup.sort_values("revenue", ascending=False).iterrows():
        protein_rows.append(
            {
                "protein_family": row.get("protein_family") or "Unassigned",
                "lead_category": row.get("lead_category") or row.get("protein_family") or "Unassigned",
                "revenue": _clean_float(row.get("revenue")),
                "revenue_prior": _clean_float(row.get("revenue_prior")),
                "revenue_delta": _clean_optional_float(row.get("revenue_delta")),
                "revenue_share_pct": _clean_optional_float(row.get("revenue_share_pct")),
                "mix_shift_pp": _clean_optional_float(row.get("mix_shift_pp")),
                "profit": _clean_optional_float(row.get("profit")),
                "margin_pct": _clean_optional_float(row.get("margin_pct")),
                "weight_lb": _clean_float(row.get("weight_lb")),
                "orders": _clean_int(row.get("orders")),
                "sku_count": _clean_int(row.get("sku_count")),
                "customer_count": _clean_int(row.get("customer_count")),
            }
        )

    category_rows = []
    for _, row in category_rollup.sort_values("revenue", ascending=False).iterrows():
        category_rows.append(
            {
                "product_category": row.get("product_category") or "Unassigned",
                "revenue": _clean_float(row.get("revenue")),
                "revenue_prior": _clean_float(row.get("revenue_prior")),
                "revenue_delta": _clean_optional_float(row.get("revenue_delta")),
                "revenue_share_pct": _clean_optional_float(row.get("revenue_share_pct")),
                "mix_shift_pp": _clean_optional_float(row.get("mix_shift_pp")),
                "profit": _clean_optional_float(row.get("profit")),
                "margin_pct": _clean_optional_float(row.get("margin_pct")),
                "sku_count": _clean_int(row.get("sku_count")),
                "customer_count": _clean_int(row.get("customer_count")),
            }
        )

    top_protein_row = protein_rows[0] if protein_rows else {}
    top_category_row = category_rows[0] if category_rows else {}

    pricing_fixes = []
    data_quality_fixes = []
    promote_candidates = []
    velocity_median = _series_quantile(product_rollup["velocity_units_per_month"], 0.50)
    for row in products_rows:
        margin = row.get("margin_pct")
        row_target_margin = _clean_optional_float(row.get("target_margin_pct"))
        velocity = _clean_optional_float(
            product_rollup.loc[
                pd.Series(product_rollup["product_id"]).astype("string") == str(row.get("product_id")),
                "velocity_units_per_month",
            ].iloc[0]
        ) if not product_rollup.empty else None
        if (
            margin is not None
            and row_target_margin is not None
            and margin < row_target_margin
            and velocity is not None
            and velocity_median is not None
            and velocity >= velocity_median
        ):
            pricing_fixes.append(
                {
                    "product_id": row.get("product_id"),
                    "product_name": row.get("product_name"),
                    "revenue": row.get("revenue"),
                    "margin_pct": margin,
                    "velocity": velocity,
                    "suggested_action": "Fix margin",
                    "reason": "High velocity SKU below target margin.",
                    "display_name": row.get("display_name"),
                    "target_margin_pct": row_target_margin,
                    "status_key": row.get("status_key"),
                }
            )
        if "Price outlier" in (row.get("tags") or []):
            pricing_fixes.append(
                {
                    "product_id": row.get("product_id"),
                    "product_name": row.get("product_name"),
                    "revenue": row.get("revenue"),
                    "margin_pct": margin,
                    "velocity": velocity,
                    "suggested_action": "Review price",
                    "reason": "ASP/lb outside guardrail band.",
                    "display_name": row.get("display_name"),
                }
            )
        if (
            margin is not None
            and row_target_margin is not None
            and margin >= row_target_margin
            and velocity is not None
            and velocity_median is not None
            and velocity < velocity_median
        ):
            promote_candidates.append(
                {
                    "product_id": row.get("product_id"),
                    "product_name": row.get("product_name"),
                    "revenue": row.get("revenue"),
                    "margin_pct": margin,
                    "velocity": velocity,
                    "suggested_action": "Promote",
                    "reason": "High-margin SKU with lower velocity.",
                    "display_name": row.get("display_name"),
                    "target_margin_pct": row_target_margin,
                    "status_key": row.get("status_key"),
                }
            )
        if row.get("margin_pct") is None:
            data_quality_fixes.append(
                {
                    "product_id": row.get("product_id"),
                    "product_name": row.get("product_name"),
                    "revenue": row.get("revenue"),
                    "suggested_action": "Fix cost mapping",
                    "reason": "Cost missing; margin cannot be computed.",
                    "display_name": row.get("display_name"),
                    "status_key": row.get("status_key"),
                }
            )

    top_sku_row = products_rows[0] if products_rows else {}
    missing_cost_revenue = float(curr.loc[curr["cost"].isna(), "revenue"].sum()) if "cost" in curr.columns else 0.0
    below_target_revenue = float(pd.to_numeric(margin_at_risk.get("revenue"), errors="coerce").fillna(0.0).sum()) if not margin_at_risk.empty else 0.0
    mapped_protein_revenue = float(
        curr.loc[curr["protein_family"].astype("string").replace("", pd.NA).fillna("Unassigned") != "Unassigned", "revenue"].sum()
    )
    mapped_protein_share_pct = (mapped_protein_revenue / float(curr["revenue"].sum()) * 100.0) if float(curr["revenue"].sum()) > 0 else None
    concentration_posture = "Concentrated" if (product_conc.get("top5_share") or 0.0) >= 70.0 else "Balanced"
    protein_posture = "Protein concentrated" if (_clean_optional_float(top_protein_row.get("revenue_share_pct")) or 0.0) >= 65.0 else "Protein diversified"

    protein_focus_cards = [
        {
            "title": "Top protein family",
            "value": str(top_protein_row.get("protein_family") or "—"),
            "meta": f"{_clean_optional_float(top_protein_row.get('revenue_share_pct')):.1f}% of revenue" if top_protein_row.get("revenue_share_pct") is not None else "No mapped protein revenue",
            "tone": "accent",
        },
        {
            "title": "Largest mix shift",
            "value": str(max(protein_rows, key=lambda row: abs(float(row.get('mix_shift_pp') or 0.0))).get("protein_family") if protein_rows else "—"),
            "meta": (
                f"{_clean_optional_float(max(protein_rows, key=lambda row: abs(float(row.get('mix_shift_pp') or 0.0))).get('mix_shift_pp')):+.1f} pp vs prior"
                if protein_rows and max(protein_rows, key=lambda row: abs(float(row.get('mix_shift_pp') or 0.0))).get("mix_shift_pp") is not None
                else "No prior comparison"
            ),
            "tone": "warn" if protein_rows else "accent",
        },
        {
            "title": "Margin watch",
            "value": str(
                min(
                    [row for row in protein_rows if row.get("margin_pct") is not None],
                    key=lambda row: float(row.get("margin_pct") or 0.0),
                    default={"protein_family": "—"},
                ).get("protein_family")
            ),
            "meta": (
                f"{_clean_optional_float(min([row for row in protein_rows if row.get('margin_pct') is not None], key=lambda row: float(row.get('margin_pct') or 0.0), default={'margin_pct': None}).get('margin_pct')):.1f}% margin"
                if any(row.get("margin_pct") is not None for row in protein_rows)
                else "Margin unavailable"
            ),
            "tone": "risk" if any(row.get("margin_pct") is not None for row in protein_rows) else "accent",
        },
        {
            "title": "Dependence posture",
            "value": protein_posture,
            "meta": (
                f"{_clean_optional_float(top_sku_row.get('revenue_share_pct')):.1f}% top SKU share"
                if top_sku_row.get("revenue_share_pct") is not None
                else "SKU breadth unavailable"
            ),
            "tone": "warn" if protein_posture == "Protein concentrated" else "good",
        },
    ]

    playbook_cards = []
    if below_target_revenue > 0:
        playbook_cards.append(
            {
                "title": "Recover margin",
                "body": "Below-target mix is material in the active window. Prioritize pricing, cost review, and negotiation on the most exposed SKUs first.",
                "exposure": f"${below_target_revenue:,.0f} exposed",
                "tone": "risk",
                "target": "pricing",
                "tag": "Below target margin",
            }
        )
    if missing_cost_revenue > 0:
        playbook_cards.append(
            {
                "title": "Review missing cost",
                "body": "Cost gaps are suppressing trustworthy margin interpretation. Fix mapping before escalating commercial conclusions.",
                "exposure": f"${missing_cost_revenue:,.0f} without cost",
                "tone": "watch",
                "target": "pricing",
                "tag": "Fix cost mapping",
            }
        )
    if (_clean_optional_float(top_protein_row.get("revenue_share_pct")) or 0.0) >= 65.0:
        playbook_cards.append(
            {
                "title": "Diversify protein exposure",
                "body": f"{top_protein_row.get('protein_family') or 'One protein family'} is carrying a disproportionate share of supplier revenue.",
                "exposure": f"{float(top_protein_row.get('revenue_share_pct') or 0.0):.1f}% of supplier revenue",
                "tone": "warn",
                "target": "protein",
                "protein_family": top_protein_row.get("protein_family"),
            }
        )
    if decline_rows:
        playbook_cards.append(
            {
                "title": "Re-engage declining customers",
                "body": "Recent customer declines are visible inside this supplier relationship. Review service, pricing, and substitution risk with account owners.",
                "exposure": f"{len(decline_rows)} declining customers",
                "tone": "risk",
                "target": "customers",
            }
        )
    if not playbook_cards and (top_customer_row.get("revenue_share_pct") or 0.0) < 35.0 and (top_sku_row.get("revenue_share_pct") or 0.0) < 20.0:
        playbook_cards.append(
            {
                "title": "Protect healthy breadth",
                "body": "Revenue is spread across customers and SKUs without a single concentration spike. Protect service and keep commercial discipline steady.",
                "exposure": f"{float(top_customer_row.get('revenue_share_pct') or 0.0):.1f}% top customer share",
                "tone": "good",
                "target": "overview",
            }
        )

    margin_pct = _clean_optional_float(kpis.get("margin_pct"))
    cost_known_rows = int(curr["cost"].notna().sum())
    rows_total = int(len(curr.index))
    cost_coverage_pct = (cost_known_rows / rows_total * 100.0) if rows_total > 0 else None
    margin_volatility = _series_quantile(month_rollup["margin_pct"].dropna().diff().abs(), 0.50)
    if margin_volatility is None:
        margin_volatility = float(pd.to_numeric(month_rollup["margin_pct"], errors="coerce").std(ddof=0)) if len(month_rollup.index) > 1 else None

    asp_lb = (float(curr["revenue"].sum()) / float(curr["weight_lb"].sum())) if float(curr["weight_lb"].sum()) > 0 else None
    asp_lb_prior = (
        float(prior["revenue"].sum()) / float(prior["weight_lb"].sum())
        if not prior.empty and float(prior["weight_lb"].sum()) > 0
        else None
    )
    asp_lb_delta_pct = (
        ((asp_lb - asp_lb_prior) / asp_lb_prior * 100.0)
        if asp_lb is not None and asp_lb_prior and asp_lb_prior > 0
        else None
    )

    growth_component = _clamp(50.0 + (mom_delta_pct or 0.0) * 2.0)
    margin_component = _clamp((margin_pct or 0.0) / 35.0 * 100.0)
    stability_component = _clamp(100.0 - ((margin_volatility or 0.0) * 3.0))
    hhi_vals = [v for v in [customer_conc.get("hhi"), product_conc.get("hhi")] if v is not None]
    hhi_avg = float(sum(hhi_vals) / len(hhi_vals)) if hhi_vals else 1800.0
    concentration_component = _clamp(100.0 - max(hhi_avg - 1200.0, 0.0) / 30.0)
    coverage_component = _clamp(cost_coverage_pct if cost_coverage_pct is not None else 0.0)
    health_score = _clamp(
        (0.25 * (margin_component or 0.0))
        + (0.20 * (stability_component or 0.0))
        + (0.20 * (growth_component or 0.0))
        + (0.20 * (concentration_component or 0.0))
        + (0.15 * (coverage_component or 0.0))
    )
    health_label = _health_label(health_score)

    delta_revenue_total = _clean_float(curr["revenue"].sum()) - _clean_float(prior["revenue"].sum())
    delta_revenue_total_pct = (delta_revenue_total / _clean_float(prior["revenue"].sum()) * 100.0) if _clean_float(prior["revenue"].sum()) > 0 else None

    lifecycle = "Stable"
    if _clean_float(prior["revenue"].sum()) <= 0 and _clean_float(curr["revenue"].sum()) > 0:
        lifecycle = "New"
    elif delta_revenue_total_pct is not None and delta_revenue_total_pct >= 20.0:
        lifecycle = "Growth"
    elif delta_revenue_total_pct is not None and delta_revenue_total_pct <= -20.0:
        lifecycle = "Decline"

    top_customer_share_pct = _clean_optional_float(top_customer_row.get("revenue_share_pct"))
    top_protein_share_pct = _clean_optional_float(top_protein_row.get("revenue_share_pct"))
    top_category_share_pct = _clean_optional_float(top_category_row.get("revenue_share_pct"))
    top_sku_share_pct = _clean_optional_float(top_sku_row.get("revenue_share_pct"))

    notices: list[dict[str, Any]] = []
    if cost_coverage_pct is not None and cost_coverage_pct < 85.0:
        notices.append(
            {
                "tone": "warn",
                "title": "Cost coverage watch",
                "message": f"Only {cost_coverage_pct:.1f}% of scoped rows have cost coverage. Margin signals are directional until mapping improves.",
            }
        )
    if mapped_protein_share_pct is not None and mapped_protein_share_pct < 90.0:
        notices.append(
            {
                "tone": "accent",
                "title": "Protein mapping gap",
                "message": f"{mapped_protein_share_pct:.1f}% of supplier revenue has mapped protein/category context in the active window.",
            }
        )

    header_narrative = (
        f"Revenue is {_clean_float(curr['revenue'].sum()):,.0f} in the active window, "
        f"{'up' if (delta_revenue_total_pct or 0.0) >= 0 else 'down'} "
        f"{abs(delta_revenue_total_pct or 0.0):.1f}% vs the prior matched window. "
        f"{top_protein_row.get('protein_family') or 'The leading protein family'} drives {top_protein_share_pct or 0.0:.1f}% of visible revenue, "
        f"while cost coverage sits at {cost_coverage_pct or 0.0:.1f}%."
    )
    trend_narrative = (
        f"Latest visible month moved {mom_delta_pct or 0.0:+.1f}% vs the prior month, with median margin volatility of {margin_volatility or 0.0:.1f} points."
        if trend_labels
        else "Trend history is too sparse for a reliable monthly momentum read."
    )
    mix_narrative = (
        f"Customer concentration is {customer_conc.get('top5_share') or 0.0:.1f}% in the top five accounts and SKU concentration is {product_conc.get('top5_share') or 0.0:.1f}% in the top five SKUs."
    )
    protein_narrative = (
        f"{top_protein_row.get('protein_family') or 'No mapped protein'} is the anchor family at {top_protein_share_pct or 0.0:.1f}% of supplier revenue. "
        f"{max(protein_rows, key=lambda row: abs(float(row.get('mix_shift_pp') or 0.0))).get('protein_family') if protein_rows else 'No protein family'} shows the largest mix move versus prior."
    )
    category_narrative = (
        f"{top_category_row.get('product_category') or 'No mapped category'} is the leading category at {top_category_share_pct or 0.0:.1f}% of supplier revenue."
    )
    customer_narrative = (
        f"{top_customer_row.get('customer_name') or 'No customer'} carries {top_customer_share_pct or 0.0:.1f}% of supplier revenue. "
        f"{len(decline_rows)} customers are down versus prior."
    )
    pricing_narrative = (
        f"{len(outliers_rows)} price outliers and {len(margin_at_risk.index)} below-target SKUs are visible in the active window."
    )
    actions = [card.get("body") for card in playbook_cards if card.get("body")]
    playbook_narrative = (
        f"{len(playbook_cards)} action lanes are open: margin recovery, concentration control, data trust, and customer recovery."
        if playbook_cards
        else "No immediate action lanes are flagged beyond routine supplier monitoring."
    )
    scorecard_minimum_margin_pct = margin_rules.weighted_minimum_margin_pct(products_rows)
    scorecard_target_margin_pct = margin_rules.weighted_target_margin_pct(products_rows)
    scorecard_margin_status = margin_rules.classify_margin_status(
        margin_pct,
        scorecard_minimum_margin_pct,
        scorecard_target_margin_pct,
    )

    return {
        "window": {
            "start": start_iso,
            "end": end_iso,
            "prior_start": prior_start_iso,
            "prior_end": prior_end_iso,
        },
        "header": {
            "narrative": header_narrative,
            "coverage_note": notices[0]["message"] if notices else f"Cost coverage is {cost_coverage_pct or 0.0:.1f}% across the active supplier scope.",
            "top_gainer": max(products_rows, key=lambda row: float(row.get("delta_revenue") or 0.0), default={}).get("display_name"),
            "top_decliner": min(products_rows, key=lambda row: float(row.get("delta_revenue") or 0.0), default={}).get("display_name"),
            "top_protein": top_protein_row.get("protein_family"),
            "concentration_posture": concentration_posture,
            "data_notice": notices[0]["title"] if notices else "Coverage and protein mapping are healthy enough for commercial interpretation.",
        },
        "scorecard": {
            "supplier_id": supplier_id,
            "supplier_name": supplier_name,
            "total_revenue": _clean_float(curr["revenue"].sum()),
            "total_profit": _clean_optional_float(curr["profit"].sum()) if curr["profit"].notna().any() else None,
            "gross_margin_pct": margin_pct,
            "minimum_margin_pct": _clean_optional_float(scorecard_minimum_margin_pct),
            "target_margin_pct": _clean_optional_float(scorecard_target_margin_pct),
            "target_gap_pct_points": (
                _clean_optional_float(margin_pct - scorecard_target_margin_pct)
                if margin_pct is not None and scorecard_target_margin_pct is not None
                else None
            ),
            "orders": _clean_int(curr["order_id"].astype("string").replace("", pd.NA).dropna().nunique()),
            "units": _clean_float(curr["units"].sum()),
            "weight_lb": _clean_float(curr["weight_lb"].sum()),
            "active_skus": _clean_int(curr["product_id"].astype("string").replace("", pd.NA).dropna().nunique()),
            "active_customers": _clean_int(curr["customer_id"].astype("string").replace("", pd.NA).dropna().nunique()),
            "asp_lb": _clean_optional_float(asp_lb),
            "asp_lb_delta_pct": _clean_optional_float(asp_lb_delta_pct),
            "last_sold": _date_to_iso(curr["order_date"].max()),
            "cost_coverage_pct": _clean_optional_float(cost_coverage_pct),
            "revenue_delta_mom": _clean_optional_float(mom_delta),
            "revenue_delta_mom_pct": _clean_optional_float(mom_delta_pct),
            "margin_volatility": _clean_optional_float(margin_volatility),
            "customer_hhi": _clean_optional_float(customer_conc.get("hhi")),
            "sku_hhi": _clean_optional_float(product_conc.get("hhi")),
            "customer_top1_share": _clean_optional_float(customer_conc.get("top1_share")),
            "customer_top5_share": _clean_optional_float(customer_conc.get("top5_share")),
            "sku_top1_share": _clean_optional_float(product_conc.get("top1_share")),
            "sku_top5_share": _clean_optional_float(product_conc.get("top5_share")),
            "top_protein": top_protein_row.get("protein_family"),
            "top_protein_share_pct": top_protein_share_pct,
            "top_category": top_category_row.get("product_category"),
            "top_category_share_pct": top_category_share_pct,
            "top_sku": top_sku_row.get("display_name"),
            "top_sku_share_pct": top_sku_share_pct,
            "protein_breadth": _clean_int(len(protein_rows)),
            "category_breadth": _clean_int(len(category_rows)),
            "mapped_protein_share_pct": _clean_optional_float(mapped_protein_share_pct),
            "customers_for_80_pct": _clean_int(customer_conc.get("customers_for_80_pct")),
            "skus_for_80_pct": _clean_int(product_conc.get("skus_for_80_pct")),
            "days_since_last_order": _clean_int(kpis.get("days_since_last_order")),
            "missing_cost_revenue": _clean_optional_float(missing_cost_revenue),
            "below_target_revenue": _clean_optional_float(below_target_revenue),
            "health_score": _clean_optional_float(health_score),
            "health_label": health_label,
            "health_formula": "Weighted score: margin 25%, stability 20%, growth 20%, concentration 20%, coverage 15%.",
            "cost_missing_rows": rows_total - cost_known_rows,
            "rows_total": rows_total,
            "lifecycle": lifecycle,
            "classification": "Concentrated" if (product_conc.get("top5_share") or 0.0) >= 70.0 else "Diversified",
            "revenue_delta_window": _clean_optional_float(delta_revenue_total),
            "revenue_delta_window_pct": _clean_optional_float(delta_revenue_total_pct),
            "supplier_share_pct": None,
            "no_data": False,
            **scorecard_margin_status,
        },
        "trend": {
            "labels": trend_labels,
            "revenue": trend_revenue,
            "profit": trend_profit,
            "margin_pct": trend_margin,
            "orders": trend_orders,
            "rolling_revenue_3m": _rolling(trend_revenue, 3),
            "rolling_profit_3m": _rolling([v or 0.0 for v in trend_profit], 3),
            "rolling_margin_3m": _rolling([v or 0.0 for v in trend_margin], 3),
            "narrative": trend_narrative,
        },
        "mix": {
            "top_products_revenue": top_products_revenue[:15],
            "top_products_profit": top_products_profit,
            "top_customers": customers_top_rows[:15],
            "customer_concentration": customer_conc,
            "product_concentration": product_conc,
            "narrative": mix_narrative,
        },
        "protein": {
            "rows": protein_rows[:12],
            "category_rows": category_rows[:12],
            "focus_cards": protein_focus_cards,
            "narrative": protein_narrative,
            "category_narrative": category_narrative,
        },
        "pricing": {
            "asp_lb_stats": asp_lb_stats,
            "margin_stats": margin_stats,
            "asp_lb_samples": [float(v) for v in asp_lb_samples.dropna().head(5000).tolist()],
            "margin_samples": [float(v) for v in margins.dropna().head(5000).tolist()],
            "guardrails": {"high_outliers": high_outliers, "low_outliers": low_outliers},
            "price_velocity": price_velocity_rows,
            "elasticity": elasticity,
            "outliers": outliers_rows,
            "narrative": pricing_narrative,
        },
        "customers": {
            "top_rows": customers_top_rows,
            "decliners": decline_rows,
            "summary": {
                "customer_count": int(customer_rollup["customer_id"].astype("string").replace("", pd.NA).dropna().nunique()),
                "decliner_count": int(len(decline_rows)),
                "repeat_customer_revenue_share_pct": _clean_optional_float(repeat_share_pct),
                "new_customer_revenue_share_pct": _clean_optional_float(new_share_pct),
                "returning_customer_revenue_share_pct": _clean_optional_float(returning_share_pct),
                "top_customer_name": top_customer_row.get("customer_name"),
                "top_customer_share_pct": top_customer_share_pct,
                "customers_for_80_pct": _clean_int(customer_conc.get("customers_for_80_pct")),
            },
            "narrative": customer_narrative,
        },
        "products_table": {
            "rows": products_rows,
            "total_rows": len(products_rows),
        },
        "opportunities": {
            "margin_at_risk": [
                {
                    "product_id": r.get("product_id"),
                    "product_name": r.get("product_name") or r.get("product_id"),
                    "revenue": _clean_float(r.get("revenue")),
                    "profit": _clean_optional_float(r.get("profit")),
                    "margin_pct": _clean_optional_float(r.get("margin_pct")),
                    "target_margin_pct": _clean_optional_float(r.get("target_margin_pct")),
                    "minimum_margin_pct": _clean_optional_float(r.get("minimum_margin_pct")),
                    "target_status": r.get("target_status"),
                    "status_key": r.get("status_key"),
                    "uplift_to_target": _clean_optional_float(r.get("uplift_to_target")),
                    "last_sold": _date_to_iso(r.get("last_sold")),
                    "asp_lb": _clean_optional_float(r.get("asp_lb")),
                    **_product_display_fields(r.get("product_id"), r.get("product_name")),
                }
                for _, r in margin_at_risk.head(200).iterrows()
            ],
            "pricing_fixes": pricing_fixes[:200],
            "promote_candidates": promote_candidates[:200],
            "data_quality_fixes": data_quality_fixes[:200],
        },
        "playbook": {
            "goal": "Protect margin, reduce concentration risk, and grow stable volume",
            "actions": actions,
            "cards": playbook_cards,
            "narrative": playbook_narrative,
        },
        "notices": notices,
    }


def build_suppliers_drilldown(supplier_id: str, filters: Any, scope: Dict[str, Any], args: Any) -> Dict[str, Any]:
    # DEV NOTE (audit summary):
    # - Drilldown data source: canonical fact_store DuckDB `fact` view via `_scoped_cte` + `_scoped_supplier_cte`.
    # - Scope enforcement helper: access_policy.enforce_entity_access(...) in bundle_service before this builder runs;
    #   scoped SQL is applied by fact_store.build_where_clause(..., scope=...).
    # - Caching: `_DRILLDOWN_CACHE` keyed by dataset_version + scope_hash + filters hash + supplier_id + v2 args.
    cols = fact_store.list_columns()
    cols_map = _required_columns(cols)
    missing = []
    if not cols_map.get("date"):
        missing.append("date")
    if not cols_map.get("revenue_candidates"):
        missing.append("revenue")
    if not (cols_map.get("supplier_id_candidates") or cols_map.get("supplier_name_candidates")):
        missing.append("supplier_id/supplier_name")
    if missing:
        return {
            "error": {"message": "Required columns missing for suppliers drilldown: " + ", ".join(missing)},
            "meta": {"cached": False},
        }

    dataset_version = fact_store.cache_buster()
    filter_hash, scope_summary = _filter_hash(filters, scope, dataset_version)
    top_n = _parse_top_n(args, default=25)
    drilldown_v2 = _parse_bool_arg(args, ("supplier_drilldown_v2", "drilldown_v2", "v2"), default=False)

    cache_key = _cache_key(
        "suppliers.drilldown",
        dataset_version,
        scope_summary,
        filter_hash,
        {"supplier_id": str(supplier_id), "top_n": top_n, "drilldown_v2": drilldown_v2},
    )

    def _build_drilldown() -> Dict[str, Any]:
        where_sql, params, start_iso, end_iso = fact_store.build_where_clause(
            filters, cols, scope, apply_default_window=True
        )
        window = _build_comparison_window(start_iso, end_iso)
        prior_start_iso = window.get("prior_start")
        prior_end_iso = window.get("prior_end")
        scoped_cte = _scoped_cte(cols, cols_map, where_sql)
        scoped_supplier_cte = _scoped_supplier_cte(scoped_cte)
        params_with_supplier = list(params) + [supplier_id]

        summary_sql = _drilldown_summary_sql(scoped_supplier_cte)
        products_sql = _drilldown_products_sql(scoped_supplier_cte, top_n)
        customers_sql = _drilldown_customers_sql(scoped_supplier_cte, top_n)
        detail_sql = None
        detail_params_with_supplier: list[Any] = []
        if drilldown_v2:
            detail_filters = _filters_with_window(filters, prior_start_iso or start_iso, end_iso)
            detail_where_sql, detail_params, _, _ = fact_store.build_where_clause(
                detail_filters, cols, scope, apply_default_window=True
            )
            detail_scoped_cte = _scoped_cte(cols, cols_map, detail_where_sql)
            detail_scoped_supplier_cte = _scoped_supplier_cte(detail_scoped_cte)
            detail_sql = _drilldown_detail_rows_sql(detail_scoped_supplier_cte)
            detail_params_with_supplier = list(detail_params) + [supplier_id]

        summary_df = fact_store.execute_sql_df(summary_sql, params_with_supplier, tag="suppliers.drilldown.summary")
        products_df = fact_store.execute_sql_df(products_sql, params_with_supplier, tag="suppliers.drilldown.products")
        customers_df = fact_store.execute_sql_df(customers_sql, params_with_supplier, tag="suppliers.drilldown.customers")
        detail_df = (
            _execute_sql_df_with_fallback(detail_sql, detail_params_with_supplier, tag="suppliers.drilldown.detail")
            if detail_sql
            else pd.DataFrame()
        )

        summary_row = summary_df.iloc[0].to_dict() if not summary_df.empty else {}
        monthly = _struct_list(summary_row.get("monthly"))
        trend_labels = [str(item.get("month")) for item in monthly if item.get("month") is not None]
        trend_revenue = [_clean_float(item.get("revenue")) for item in monthly]
        trend_cost = [_clean_optional_float(item.get("cost")) for item in monthly]
        trend_profit = [_clean_optional_float(item.get("profit")) for item in monthly]
        trend_margin = [_clean_optional_float(item.get("margin_pct")) for item in monthly]

        products_row = products_df.iloc[0].to_dict() if not products_df.empty else {}
        top_products = _struct_list(products_row.get("top_products"))
        unit_prices = _to_list(products_row.get("unit_prices"))
        unit_price_stats = products_row.get("unit_price_stats") or {}
        margin_stats = products_row.get("margin_stats") or {}
        prod_conc = products_row.get("concentration") or {}

        customers_row = customers_df.iloc[0].to_dict() if not customers_df.empty else {}
        top_customers = _struct_list(customers_row.get("top_customers"))
        cust_conc = customers_row.get("concentration") or {}

        supplier_name = summary_row.get("supplier_name") or str(supplier_id)
        last_sold_iso = _date_to_iso(summary_row.get("last_sold"))
        days_since_last_order = None
        if last_sold_iso:
            try:
                last_dt = date.fromisoformat(last_sold_iso)
                days_since_last_order = (date.today() - last_dt).days
            except Exception:
                days_since_last_order = None

        kpis = {
            "supplier_id": str(supplier_id),
            "supplier_name": supplier_name,
            "revenue": _clean_float(summary_row.get("revenue")),
            "cost": _clean_optional_float(summary_row.get("cost")),
            "profit": _clean_optional_float(summary_row.get("profit")),
            "margin_pct": _clean_optional_float(summary_row.get("margin_pct")),
            "roi_pct": _clean_optional_float(summary_row.get("roi_pct")),
            "orders": _clean_int(summary_row.get("orders")),
            "products": _clean_int(summary_row.get("products")),
            "customers": _clean_int(summary_row.get("customers")),
            "units": _clean_float(summary_row.get("units")),
            "weight_lb": _clean_float(summary_row.get("weight_lb")),
            "last_sold": last_sold_iso,
            "days_since_last_order": days_since_last_order,
            "start": start_iso,
            "end": end_iso,
        }

        product_metrics = [
            {
                "product_id": p.get("product_id"),
                "sku": p.get("product_id"),
                "product_name": p.get("product_name") or p.get("product_id"),
                **_product_display_fields(p.get("product_id"), p.get("product_name")),
                "revenue": _clean_float(p.get("revenue")),
                "cost": _clean_optional_float(p.get("cost")),
                "profit": _clean_optional_float(p.get("profit")),
                "margin_pct": _clean_optional_float(p.get("margin_pct")),
                "units": _clean_float(p.get("units")),
                "weight_lb": _clean_float(p.get("weight_lb")),
                "orders": _clean_int(p.get("orders")),
                "customers": _clean_int(p.get("customers")),
                "avg_sale_price": _clean_optional_float(p.get("avg_sale_price")),
                "avg_cost_per_unit": _clean_optional_float(p.get("avg_cost_per_unit")),
            }
            for p in top_products
        ]

        top_prod_labels = [_product_display_fields(p.get("product_id"), p.get("product_name")).get("display_name_short") for p in top_products]
        top_prod_values = [_clean_float(p.get("revenue")) for p in top_products]
        top_cust_labels = [str(c.get("customer_name") or c.get("customer_id")) for c in top_customers]
        top_cust_values = [_clean_float(c.get("revenue")) for c in top_customers]

        payload = {
            "kpis": kpis,
            "trend": {
                "labels": trend_labels,
                "revenue": trend_revenue,
                "cost": trend_cost,
                "profit": trend_profit,
                "margin_pct": trend_margin,
            },
            "charts": {
                "trend": {
                    "labels": trend_labels,
                    "revenue": trend_revenue,
                    "cost": trend_cost,
                    "profit": trend_profit,
                    "margin_pct": trend_margin,
                },
                "top_products": {
                    "labels": top_prod_labels,
                    "values": top_prod_values,
                    "rows": top_products,
                    "concentration": prod_conc or {},
                },
                "top_customers": {
                    "labels": top_cust_labels,
                    "values": top_cust_values,
                    "rows": top_customers,
                    "concentration": cust_conc or {},
                },
                "unit_price": {
                    "values": unit_prices,
                    "stats": unit_price_stats or {},
                },
                "margin_stats": margin_stats or {},
            },
            "table": {
                "rows": product_metrics,
                "page": 1,
                "page_size": len(product_metrics),
                "total": len(product_metrics),
                "total_rows": len(product_metrics),
                "sort_by": "revenue",
                "sort_dir": "desc",
            },
            "meta": {
                "page_id": "supplier_drilldown",
                "entity_id": str(supplier_id),
                "entity_label": supplier_name,
                "top_n": top_n,
                "window_start": start_iso,
                "window_end": end_iso,
                "prior_window_start": prior_start_iso,
                "prior_window_end": prior_end_iso,
            },
        }
        if drilldown_v2:
            supplier_v2 = _supplier_drilldown_v2_payload(
                supplier_id=str(supplier_id),
                supplier_name=str(supplier_name),
                detail_df=detail_df,
                kpis=kpis,
                top_n=top_n,
                start_iso=start_iso,
                end_iso=end_iso,
                prior_start_iso=prior_start_iso,
                prior_end_iso=prior_end_iso,
            )
            payload["supplier_v2"] = supplier_v2
            payload["v2"] = supplier_v2
        return payload

    drilldown_payload, cache_hit = _DRILLDOWN_CACHE.get_or_compute(cache_key, DRILLDOWN_TTL_SECONDS, _build_drilldown)
    drilldown_payload = _clone(drilldown_payload)
    drilldown_payload.setdefault("meta", {})
    drilldown_payload["meta"].setdefault("cache_parts", {})
    drilldown_payload["meta"]["cache_parts"]["drilldown"] = bool(cache_hit)
    return drilldown_payload


def build_supplier_products_frame(
    supplier_id: str,
    filters: Any,
    scope: Dict[str, Any],
    args: Any,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Export helper for supplier products.
    Uses the same scoped filters/window as drilldown, but does not apply top-N
    or pagination so exports always include all matching products.
    """
    cols = fact_store.list_columns()
    cols_map = _required_columns(cols)
    missing: list[str] = []
    if not cols_map.get("date"):
        missing.append("date")
    if not cols_map.get("revenue_candidates"):
        missing.append("revenue")
    if not (cols_map.get("supplier_id_candidates") or cols_map.get("supplier_name_candidates")):
        missing.append("supplier_id/supplier_name")
    if not cols_map.get("product_id_candidates"):
        missing.append("product_id")
    if missing:
        return pd.DataFrame(), {
            "supplier_id": str(supplier_id),
            "total_rows": 0,
            "missing_columns": missing,
        }

    where_sql, params, start_iso, end_iso = fact_store.build_where_clause(
        filters, cols, scope, apply_default_window=True
    )
    scoped_cte = _scoped_cte(cols, cols_map, where_sql)
    scoped_supplier_cte = _scoped_supplier_cte(scoped_cte)
    base_params = list(params) + [supplier_id]

    sort_by, sort_dir = _supplier_product_sort_params(args)
    search = _extract_search(args)

    count_sql, count_params = _supplier_products_count_sql(scoped_supplier_cte, search)
    count_df = _execute_sql_df_with_fallback(
        count_sql,
        base_params + count_params,
        tag="suppliers.products_export.count",
    )
    total_rows_raw = count_df.iloc[0].get("total_rows") if not count_df.empty else 0
    total_rows = _clean_int(total_rows_raw, default=0)
    if total_rows > SUPPLIER_PRODUCTS_EXPORT_MAX_ROWS:
        raise SupplierProductsExportLimitError(
            row_count=total_rows,
            max_rows=SUPPLIER_PRODUCTS_EXPORT_MAX_ROWS,
        )

    if total_rows <= 0:
        df = pd.DataFrame(
            columns=[
                "product_id",
                "product_name",
                "revenue",
                "cost",
                "profit",
                "margin_pct",
                "units",
                "weight_lb",
                "orders",
                "customers",
                "avg_sale_price",
                "avg_cost_per_unit",
            ]
        )
    else:
        chunk_size = max(1000, min(SUPPLIER_PRODUCTS_EXPORT_CHUNK_SIZE, MAX_PAGE_SIZE))
        chunks: list[pd.DataFrame] = []
        offset = 0
        while offset < total_rows:
            sql, query_params = _supplier_products_sql(
                scoped_supplier_cte,
                sort_by,
                sort_dir,
                search,
                paginate=True,
                page_size=min(chunk_size, total_rows - offset),
                offset=offset,
            )
            chunk = _execute_sql_df_with_fallback(
                sql,
                base_params + query_params,
                tag="suppliers.products_export.rows",
            )
            if chunk.empty:
                break
            chunks.append(chunk)
            fetched = len(chunk.index)
            if fetched <= 0:
                break
            offset += fetched
        df = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()

    if "total_groups" in df.columns:
        df = df.drop(columns=["total_groups"])

    rename_map = {
        "product_id": "Product ID",
        "product_name": "Product",
        "revenue": "Revenue",
        "cost": "Cost",
        "profit": "Profit",
        "margin_pct": "Margin %",
        "units": "Units",
        "weight_lb": "Weight (lb)",
        "orders": "Orders",
        "customers": "Customers",
        "avg_sale_price": "Avg Sale Price",
        "avg_cost_per_unit": "Avg Cost / Unit",
    }
    export_order = [
        "product_id",
        "product_name",
        "revenue",
        "cost",
        "profit",
        "margin_pct",
        "units",
        "weight_lb",
        "orders",
        "customers",
        "avg_sale_price",
        "avg_cost_per_unit",
    ]
    existing_order = [c for c in export_order if c in df.columns]
    if existing_order:
        ordered_data = {col: df[col].tolist() for col in existing_order}
        df = pd.DataFrame(ordered_data)
    else:
        df = pd.DataFrame(columns=export_order)
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
    meta = {
        "supplier_id": str(supplier_id),
        "total_rows": int(len(df.index)),
        "sort_by": sort_by,
        "sort_dir": sort_dir.lower(),
        "search": search,
        "start": start_iso,
        "end": end_iso,
    }
    return df, meta


def _mode_text(series: pd.Series) -> str | None:
    if series is None:
        return None
    values = series.astype("string").str.strip().replace({"": pd.NA}).dropna()
    if values.empty:
        return None
    mode_vals = values.mode(dropna=True)
    if not mode_vals.empty:
        return str(mode_vals.iloc[0])
    return str(values.iloc[0])


def build_supplier_products_vs_customers_export_frame(
    supplier_id: str,
    filters: Any,
    scope: Dict[str, Any],
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    output_cols = [
        "SupplierId",
        "SupplierName",
        "SKU",
        "Product",
        "ProductName",
        "ProteinFamily",
        "ProductCategory",
        "CustomerId",
        "CustomerName",
        "Region",
        "Orders",
        "Units",
        "WeightLb",
        "Revenue",
        "Cost",
        "Profit",
        "MarginPct",
        "ASP/lb",
        "FirstOrderDate",
        "LastOrderDate",
        "RevenueShareWithinSupplier",
        "RevenueShareWithinProduct",
        "CustomerRankForProduct",
        "ProductRankWithinSupplier",
        "ShippingMethod",
        "SalesRep",
        "ActiveFilterStart",
        "ActiveFilterEnd",
    ]

    cols = fact_store.list_columns()
    cols_map = _required_columns(cols)
    missing: list[str] = []
    if not cols_map.get("date"):
        missing.append("date")
    if not cols_map.get("revenue_candidates"):
        missing.append("revenue")
    if not (cols_map.get("supplier_id_candidates") or cols_map.get("supplier_name_candidates")):
        missing.append("supplier_id/supplier_name")
    if not cols_map.get("product_id_candidates"):
        missing.append("product_id")
    if not cols_map.get("customer_id_candidates"):
        missing.append("customer_id")
    if missing:
        return pd.DataFrame(columns=output_cols), {
            "dataset": "products_vs_customers",
            "supplier_id": str(supplier_id),
            "total_rows": 0,
            "missing_columns": missing,
            "start": None,
            "end": None,
            "prior_start": None,
            "prior_end": None,
        }

    where_sql, params, start_iso, end_iso = fact_store.build_where_clause(
        filters, cols, scope, apply_default_window=True
    )
    scoped_cte = _scoped_cte(cols, cols_map, where_sql)
    scoped_supplier_cte = _scoped_supplier_cte(scoped_cte)
    detail_sql = _drilldown_detail_rows_sql(scoped_supplier_cte)
    detail_df = _execute_sql_df_with_fallback(
        detail_sql,
        list(params) + [supplier_id],
        tag="suppliers.drilldown.export.products_vs_customers",
    )

    if detail_df is None or detail_df.empty:
        return pd.DataFrame(columns=output_cols), {
            "dataset": "products_vs_customers",
            "supplier_id": str(supplier_id),
            "total_rows": 0,
            "start": start_iso,
            "end": end_iso,
            "prior_start": None,
            "prior_end": None,
        }

    detail = detail_df.copy()
    detail["order_date"] = pd.to_datetime(detail.get("order_date"), errors="coerce")
    detail = detail[detail["order_date"].notna()].copy()
    if detail.empty:
        return pd.DataFrame(columns=output_cols), {
            "dataset": "products_vs_customers",
            "supplier_id": str(supplier_id),
            "total_rows": 0,
            "start": start_iso,
            "end": end_iso,
            "prior_start": None,
            "prior_end": None,
        }

    for col in ("revenue", "cost", "units", "weight_lb"):
        detail[col] = pd.to_numeric(detail.get(col), errors="coerce")
    detail["revenue"] = detail["revenue"].fillna(0.0)
    detail["units"] = detail["units"].fillna(0.0)
    detail["weight_lb"] = detail["weight_lb"].fillna(0.0)

    def _text_col(name: str, default: str = "") -> pd.Series:
        raw = detail.get(name)
        if raw is None:
            return pd.Series([default] * len(detail.index), index=detail.index, dtype="string")
        return raw.astype("string").str.strip().replace({"nan": pd.NA, "None": pd.NA})

    detail["supplier_id"] = _text_col("supplier_id", str(supplier_id)).fillna(str(supplier_id))
    detail["supplier_name"] = _text_col("supplier_name", str(supplier_id)).fillna(str(supplier_id))
    detail["product_id"] = _text_col("product_id").fillna("")
    detail["product_name"] = _text_col("product_name").fillna(detail["product_id"])
    detail["customer_id"] = _text_col("customer_id").fillna("")
    detail["customer_name"] = _text_col("customer_name").fillna(detail["customer_id"])
    detail["region"] = _text_col("region", "Unspecified").fillna("Unspecified")
    detail["shipping_method"] = _text_col("shipping_method").fillna("")
    detail["sales_rep"] = _text_col("sales_rep").fillna("")
    detail["protein_family"] = _text_col("protein_family", "Unassigned").fillna("Unassigned")
    detail["product_category"] = _text_col("product_category").fillna(detail["protein_family"])

    group_cols = [
        "supplier_id",
        "supplier_name",
        "product_id",
        "product_name",
        "protein_family",
        "product_category",
        "customer_id",
        "customer_name",
        "region",
    ]
    grouped = (
        detail.groupby(group_cols, dropna=False)
        .agg(
            Orders=("order_id", lambda s: s.astype("string").replace({"": pd.NA}).dropna().nunique()),
            Units=("units", "sum"),
            WeightLb=("weight_lb", "sum"),
            Revenue=("revenue", "sum"),
            _CostSum=("cost", "sum"),
            _CostRows=("cost", lambda s: s.notna().sum()),
            FirstOrderDate=("order_date", "min"),
            LastOrderDate=("order_date", "max"),
            ShippingMethod=("shipping_method", _mode_text),
            SalesRep=("sales_rep", _mode_text),
        )
        .reset_index()
    )

    grouped["Cost"] = grouped["_CostSum"].where(grouped["_CostRows"] > 0)
    grouped["Profit"] = (grouped["Revenue"] - grouped["Cost"]).where(grouped["Cost"].notna())
    grouped["MarginPct"] = ((grouped["Profit"] / grouped["Revenue"]) * 100.0).where(grouped["Revenue"] > 0)
    grouped["ASP/lb"] = (grouped["Revenue"] / grouped["WeightLb"]).where(grouped["WeightLb"] > 0)
    grouped["Product"] = grouped.apply(
        lambda row: presentation.format_product_label(row.get("product_id"), row.get("product_name")),
        axis=1,
    )

    supplier_revenue = float(pd.to_numeric(grouped["Revenue"], errors="coerce").fillna(0.0).sum())
    if supplier_revenue > 0:
        grouped["RevenueShareWithinSupplier"] = grouped["Revenue"] / supplier_revenue * 100.0
    else:
        grouped["RevenueShareWithinSupplier"] = None

    product_keys = ["product_id", "product_name"]
    product_revenue = grouped.groupby(product_keys, dropna=False)["Revenue"].transform("sum")
    grouped["RevenueShareWithinProduct"] = ((grouped["Revenue"] / product_revenue) * 100.0).where(product_revenue > 0)
    grouped["CustomerRankForProduct"] = (
        grouped.groupby(product_keys, dropna=False)["Revenue"].rank(method="dense", ascending=False).astype("Int64")
    )
    product_rank = (
        grouped.groupby(product_keys, dropna=False)["Revenue"]
        .sum()
        .sort_values(ascending=False)
        .reset_index()
    )
    product_rank["ProductRankWithinSupplier"] = (
        product_rank["Revenue"].rank(method="dense", ascending=False).astype("Int64")
    )
    grouped = grouped.merge(product_rank[product_keys + ["ProductRankWithinSupplier"]], on=product_keys, how="left")

    grouped["FirstOrderDate"] = pd.to_datetime(grouped["FirstOrderDate"], errors="coerce").dt.date.astype("string")
    grouped["LastOrderDate"] = pd.to_datetime(grouped["LastOrderDate"], errors="coerce").dt.date.astype("string")
    grouped["ActiveFilterStart"] = str(start_iso or "")
    grouped["ActiveFilterEnd"] = str(end_iso or "")

    grouped = grouped.rename(
        columns={
            "supplier_id": "SupplierId",
            "supplier_name": "SupplierName",
            "product_id": "SKU",
            "product_name": "ProductName",
            "protein_family": "ProteinFamily",
            "product_category": "ProductCategory",
            "customer_id": "CustomerId",
            "customer_name": "CustomerName",
            "region": "Region",
        }
    )
    grouped["Orders"] = pd.to_numeric(grouped["Orders"], errors="coerce").fillna(0).astype(int)
    grouped["CustomerRankForProduct"] = pd.to_numeric(grouped["CustomerRankForProduct"], errors="coerce").astype("Int64")
    grouped["ProductRankWithinSupplier"] = pd.to_numeric(grouped["ProductRankWithinSupplier"], errors="coerce").astype("Int64")
    grouped["ShippingMethod"] = grouped["ShippingMethod"].astype("string").fillna("")
    grouped["SalesRep"] = grouped["SalesRep"].astype("string").fillna("")

    for col in output_cols:
        if col not in grouped.columns:
            grouped[col] = None

    frame = grouped[output_cols].sort_values(
        by=["ProductRankWithinSupplier", "CustomerRankForProduct", "Revenue", "CustomerName"],
        ascending=[True, True, False, True],
        na_position="last",
        kind="stable",
    )
    frame = frame.reset_index(drop=True)
    meta = {
        "dataset": "products_vs_customers",
        "supplier_id": str(supplier_id),
        "supplier_name": (
            str(frame["SupplierName"].iloc[0])
            if not frame.empty and "SupplierName" in frame.columns and pd.notna(frame["SupplierName"].iloc[0])
            else None
        ),
        "total_rows": int(len(frame.index)),
        "start": start_iso,
        "end": end_iso,
        "prior_start": None,
        "prior_end": None,
    }
    return frame, meta


def build_supplier_drilldown_export_dataset(
    supplier_id: str,
    filters: Any,
    scope: Dict[str, Any],
    args: Any,
    dataset: str,
) -> tuple[pd.DataFrame, Dict[str, Any]]:
    token = str(dataset or "products").strip().lower()
    aliases = {
        "table": "products",
        "product_table": "products",
        "product": "products",
        "customer": "customers",
        "products_customers": "products_vs_customers",
        "products-vs-customers": "products_vs_customers",
        "product_customer": "products_vs_customers",
        "product_vs_customer": "products_vs_customers",
        "outlier": "pricing_outliers",
        "pricing": "pricing_outliers",
        "margin": "margin_risk",
        "margin_at_risk": "margin_risk",
        "trend": "monthly_series",
        "monthly": "monthly_series",
        "kpis": "summary",
    }
    token = aliases.get(token, token)

    args_payload: Dict[str, Any] = {}
    if hasattr(args, "to_dict"):
        try:
            args_payload = args.to_dict(flat=True)  # type: ignore[assignment]
        except Exception:
            args_payload = {}
    elif isinstance(args, Mapping):
        args_payload = {str(k): v for k, v in args.items()}
    args_payload["supplier_drilldown_v2"] = "1"
    args_payload["drilldown_v2"] = "1"
    args_payload["top_n"] = str(TOP_N_MAX)
    payload = build_suppliers_drilldown(str(supplier_id), filters, scope, args_payload)
    supplier_v2 = payload.get("supplier_v2") or payload.get("v2") or {}
    if not isinstance(supplier_v2, dict):
        supplier_v2 = {}
    window = supplier_v2.get("window") if isinstance(supplier_v2.get("window"), dict) else {}
    score = supplier_v2.get("scorecard") if isinstance(supplier_v2.get("scorecard"), dict) else {}

    if token == "products":
        frame = pd.DataFrame.from_records(((supplier_v2.get("products_table") or {}).get("rows") or []))
    elif token == "products_vs_customers":
        frame, meta = build_supplier_products_vs_customers_export_frame(str(supplier_id), filters, scope)
        return frame, meta
    elif token == "customers":
        frame = pd.DataFrame.from_records(((supplier_v2.get("customers") or {}).get("top_rows") or []))
    elif token == "pricing_outliers":
        frame = pd.DataFrame.from_records(((supplier_v2.get("pricing") or {}).get("outliers") or []))
    elif token == "margin_risk":
        frame = pd.DataFrame.from_records(((supplier_v2.get("opportunities") or {}).get("margin_at_risk") or []))
    elif token == "monthly_series":
        trend = supplier_v2.get("trend") or {}
        frame = pd.DataFrame(
            {
                "month": trend.get("labels") or [],
                "revenue": trend.get("revenue") or [],
                "profit": trend.get("profit") or [],
                "margin_pct": trend.get("margin_pct") or [],
                "orders": trend.get("orders") or [],
            }
        )
    elif token == "summary":
        frame = pd.DataFrame([score]) if score else pd.DataFrame()
    else:
        raise ValueError(f"Unsupported suppliers drilldown export dataset: {dataset}")

    if token == "products":
        rename_map = {
            "product_id": "SKU",
            "sku": "SKU",
            "display_name": "Product",
            "protein_family": "ProteinFamily",
            "product_category": "ProductCategory",
            "revenue": "Revenue",
            "profit": "Profit",
            "margin_pct": "MarginPct",
            "orders": "Orders",
            "customers": "Customers",
            "units": "Units",
            "weight_lb": "WeightLb",
            "asp_lb": "ASP/lb",
            "asp_lb_delta_pct": "ASP/lb DeltaPct",
            "revenue_share_pct": "RevenueSharePct",
            "last_sold": "LastSold",
            "tags": "Tags",
        }
        order = [
            "SKU",
            "Product",
            "ProteinFamily",
            "ProductCategory",
            "Revenue",
            "Profit",
            "MarginPct",
            "Orders",
            "Customers",
            "Units",
            "WeightLb",
            "ASP/lb",
            "ASP/lb DeltaPct",
            "RevenueSharePct",
            "LastSold",
            "Tags",
        ]
        if "tags" in frame.columns:
            frame["tags"] = frame["tags"].apply(
                lambda values: " | ".join(str(v) for v in values if str(v).strip()) if isinstance(values, list) else values
            )
        frame = frame.rename(columns={k: v for k, v in rename_map.items() if k in frame.columns})
        existing = [col for col in order if col in frame.columns]
        frame = frame[existing] if existing else frame
    elif token == "customers":
        rename_map = {
            "customer_id": "CustomerId",
            "customer_name": "CustomerName",
            "revenue": "Revenue",
            "profit": "Profit",
            "margin_pct": "MarginPct",
            "orders": "Orders",
            "last_order_date": "LastOrderDate",
            "delta_revenue": "DeltaRevenue",
        }
        frame = frame.rename(columns={k: v for k, v in rename_map.items() if k in frame.columns})
    elif token == "pricing_outliers":
        rename_map = {
            "product_id": "SKU",
            "display_name": "Product",
            "asp_lb": "ASP/lb",
            "peer_median": "PeerMedianASP/lb",
            "delta_pct_vs_peer": "DeltaPctVsPeer",
            "revenue": "Revenue",
            "margin_pct": "MarginPct",
            "last_sold": "LastSold",
            "outlier_type": "OutlierType",
        }
        order = ["SKU", "Product", "ASP/lb", "PeerMedianASP/lb", "DeltaPctVsPeer", "Revenue", "MarginPct", "LastSold", "OutlierType"]
        frame = frame.rename(columns={k: v for k, v in rename_map.items() if k in frame.columns})
        existing = [col for col in order if col in frame.columns]
        frame = frame[existing] if existing else frame
    elif token == "margin_risk":
        rename_map = {
            "product_id": "SKU",
            "display_name": "Product",
            "revenue": "Revenue",
            "profit": "Profit",
            "margin_pct": "MarginPct",
            "minimum_margin_pct": "MinimumMarginPct",
            "target_margin_pct": "TargetMarginPct",
            "uplift_to_target": "UpliftToTarget",
            "asp_lb": "ASP/lb",
            "minimum_price_lb": "MinimumPriceLb",
            "target_price_lb": "TargetPriceLb",
            "asp_lb_gap_to_target": "GapToTargetLb",
            "target_status": "Status",
            "last_sold": "LastSold",
        }
        order = ["SKU", "Product", "Revenue", "Profit", "MarginPct", "MinimumMarginPct", "TargetMarginPct", "UpliftToTarget", "ASP/lb", "MinimumPriceLb", "TargetPriceLb", "GapToTargetLb", "Status", "LastSold"]
        frame = frame.rename(columns={k: v for k, v in rename_map.items() if k in frame.columns})
        existing = [col for col in order if col in frame.columns]
        frame = frame[existing] if existing else frame
    elif token == "monthly_series":
        rename_map = {
            "month": "Month",
            "revenue": "Revenue",
            "profit": "Profit",
            "margin_pct": "MarginPct",
            "orders": "Orders",
        }
        frame = frame.rename(columns={k: v for k, v in rename_map.items() if k in frame.columns})
    elif token == "summary":
        rename_map = {
            "supplier_id": "SupplierId",
            "supplier_name": "SupplierName",
            "total_revenue": "Revenue",
            "total_profit": "Profit",
            "gross_margin_pct": "MarginPct",
            "orders": "Orders",
            "units": "Units",
            "weight_lb": "WeightLb",
            "active_skus": "ActiveSkus",
            "active_customers": "ActiveCustomers",
            "asp_lb": "ASP/lb",
            "asp_lb_delta_pct": "ASP/lb DeltaPct",
            "last_sold": "LastSold",
            "cost_coverage_pct": "CostCoveragePct",
            "revenue_delta_mom": "RevenueDeltaMoM",
            "revenue_delta_mom_pct": "RevenueDeltaMoMPct",
            "margin_volatility": "MarginVolatility",
            "customer_hhi": "CustomerHHI",
            "sku_hhi": "SkuHHI",
            "health_score": "HealthScore",
            "health_label": "HealthLabel",
            "lifecycle": "Lifecycle",
            "classification": "Classification",
            "revenue_delta_window": "RevenueDeltaWindow",
            "revenue_delta_window_pct": "RevenueDeltaWindowPct",
        }
        frame = frame.rename(columns={k: v for k, v in rename_map.items() if k in frame.columns})

    meta = {
        "dataset": token,
        "supplier_id": str(supplier_id),
        "supplier_name": score.get("supplier_name"),
        "total_rows": int(len(frame.index)),
        "start": window.get("start"),
        "end": window.get("end"),
        "prior_start": window.get("prior_start"),
        "prior_end": window.get("prior_end"),
    }
    return frame, meta
