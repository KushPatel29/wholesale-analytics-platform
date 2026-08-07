from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd

from app.services import fact_schema as fs
from app.services import fact_store
from app.services import filters_service
from app.services import margin_rules
from app.services import salesrep_ownership


TOP_N_DEFAULT = 15
TABLE_PAGE_SIZE_DEFAULT = 25
TABLE_PAGE_SIZES = {25, 50, 100}
RISK_TOP_CUSTOMER_THRESHOLD = 0.30
RISK_MOM_PROFIT_DOWN_THRESHOLD = -15.0
_REVIEW_REP_LABEL = "Needs Review"
_UNASSIGNED_REP_LABEL = "Unassigned / Needs Review"
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_TECHNICAL_REP_RE = re.compile(r"^[A-Za-z]{1,6}[-_ ]?\d[\w-]*$")
_SAFE_REP_BUCKET_ALIASES = {
    "unassigned": _UNASSIGNED_REP_LABEL,
    "unassigned / needs review": _UNASSIGNED_REP_LABEL,
    "needs mapping": _REVIEW_REP_LABEL,
    "needs review": _REVIEW_REP_LABEL,
    "unknown rep": _REVIEW_REP_LABEL,
}
_SAFE_REP_BUCKETS = {value.lower() for value in _SAFE_REP_BUCKET_ALIASES.values()}
_REP_DIRECTORY_LOCK = threading.RLock()
_REP_DIRECTORY_CACHE: dict[str, dict[str, str]] = {}

TXN_REP_ID_CANDIDATES: Tuple[str, ...] = (
    "SalesRepId",
    "SalesRepID",
    "RepId",
    "RepID",
    "SalesRepUserId",
    "SalesRepUserID",
    "RepUserId",
    "RepUserID",
    "UserId",
    "UserID",
)

TXN_REP_NAME_CANDIDATES: Tuple[str, ...] = (
    "SalesRepName",
    "SalesRep",
    "RepName",
    "SalespersonName",
    "SalesPersonName",
    "UserName",
    "DisplayName",
)

OWNER_REP_ID_CANDIDATES: Tuple[str, ...] = (
    "PrimarySalesRepId",
    "PrimarySalesRepID",
    "PrimarySalesRepId_x",
    "PrimarySalesRepId_y",
    "PrimarySalesRepID_x",
    "PrimarySalesRepID_y",
    "PrimarySalesRepUserId",
    "PrimarySalesRepUserID",
    "AccountOwnerId",
    "OwnerId",
    "AccountManagerId",
)

OWNER_REP_NAME_CANDIDATES: Tuple[str, ...] = (
    "PrimarySalesRepName",
    "PrimarySalesRepName_x",
    "PrimarySalesRepName_y",
    "AccountOwner",
    "Owner",
    "AccountManager",
    "PrimaryOwnerName",
)

REP_ID_CANDIDATES: Tuple[str, ...] = (
    "SalesRepId",
    "SalesRepID",
    "PrimarySalesRepId",
    "PrimarySalesRepID",
    "RepId",
    "RepID",
    "UserId",
    "UserID",
    "RepUserId",
    "RepUserID",
    "SalesRepUserId",
    "SalesRepUserID",
)

REP_NAME_CANDIDATES: Tuple[str, ...] = (
    "SalesRepName",
    "PrimarySalesRepName",
    "SalesRep",
    "RepName",
    "SalespersonName",
    "SalesPersonName",
    "Owner",
    "AccountOwner",
    "UserName",
    "User",
    "FullName",
    "DisplayName",
)

ORDER_ID_CANDIDATES: Tuple[str, ...] = (
    "OrderId",
    "OrderID",
    "OrderNo",
    "Invoice",
    "InvoiceNo",
    "ShipmentID",
    "ShipmentId",
)

CUSTOMER_ID_CANDIDATES: Tuple[str, ...] = (
    "CustomerId",
    "CustomerID",
    "CustomerNo",
    "Customer",
    "CustID",
)

CUSTOMER_NAME_CANDIDATES: Tuple[str, ...] = (
    "CustomerName",
    "Customer",
)

PRODUCT_ID_CANDIDATES: Tuple[str, ...] = (
    "ProductId",
    "ProductID",
    "SKU",
    "Sku",
    "ItemId",
    "ItemID",
    "Item",
)

PRODUCT_NAME_CANDIDATES: Tuple[str, ...] = (
    "ProductName",
    "Product",
    "Description",
    "ItemName",
)

PROTEIN_CANDIDATES: Tuple[str, ...] = (
    "Protein",
    "ProteinType",
    "ProteinName",
    "Category",
    "ProductCategory",
)

CATEGORY_CANDIDATES: Tuple[str, ...] = (
    "Category",
    "ProductCategory",
    "Protein",
    "ProteinType",
    "ProteinName",
)

TERRITORY_ID_CANDIDATES: Tuple[str, ...] = (
    "TerritoryId",
    "TerritoryID",
    "CustomerTerritoryId",
    "RegionId",
    "RegionID",
)

TERRITORY_NAME_CANDIDATES: Tuple[str, ...] = (
    "TerritoryName",
    "Territory",
    "CustomerTerritoryName",
    "RegionName",
    "Region",
)


def _norm_col(name: str) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _safe_col(cols: set[str], *candidates: str) -> str | None:
    if not cols:
        return None
    lower_map = {str(c).lower(): c for c in cols}
    norm_map = {_norm_col(str(c)): c for c in cols}
    for cand in candidates:
        if not cand:
            continue
        if cand in cols:
            return cand
        key = str(cand).lower()
        if key in lower_map:
            return lower_map[key]
        norm_key = _norm_col(str(cand))
        if norm_key in norm_map:
            return norm_map[norm_key]
    return None


def _present_cols(cols: set[str], candidates: Sequence[str]) -> list[str]:
    if not cols:
        return []
    lower_map = {str(c).lower(): c for c in cols}
    norm_map = {_norm_col(str(c)): c for c in cols}
    present: list[str] = []
    for cand in candidates:
        if not cand:
            continue
        if cand in cols:
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
    return list(dict.fromkeys(present))


def _quote(col: str) -> str:
    return fact_store.quote_identifier(col)


def _coalesce_expr(cols: set[str], candidates: Sequence[str], default: str = "0") -> str:
    present = _present_cols(cols, candidates)
    if not present:
        return default
    inner = ", ".join([_quote(c) for c in present] + [default])
    return f"COALESCE({inner})"


def _string_expr(col: str) -> str:
    return f"NULLIF(TRIM(CAST({_quote(col)} AS VARCHAR)), '')"


def _coalesce_exprs(exprs: Sequence[str], default: str) -> str:
    if not exprs:
        return default
    inner = ", ".join([*exprs, default])
    return f"COALESCE({inner})"


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "null", "nan"}:
        return ""
    return text


def _rep_directory_csv_path() -> Path:
    return Path(__file__).resolve().parents[1] / "core" / "userid.csv"


def _rep_directory_cache_key() -> str:
    parts = [str(fact_store.cache_buster() or "0")]
    csv_path = _rep_directory_csv_path()
    try:
        stat = csv_path.stat()
        parts.append(f"{csv_path}:{int(stat.st_mtime)}:{int(stat.st_size)}")
    except Exception:
        parts.append(str(csv_path))
    return "|".join(parts)


def _csv_rep_directory() -> dict[str, str]:
    csv_path = _rep_directory_csv_path()
    if not csv_path.exists():
        return {}
    try:
        df = pd.read_csv(csv_path, encoding="utf-8-sig", dtype="string")
    except Exception:
        return {}

    cols = list(df.columns)
    lower_map = {str(col).strip().lower(): col for col in cols}
    user_id_col = lower_map.get("userid") or lower_map.get("user_id")
    first_col = lower_map.get("firstname") or lower_map.get("first_name")
    last_col = lower_map.get("lastname") or lower_map.get("last_name")
    full_col = lower_map.get("fullname") or lower_map.get("full_name")
    if not user_id_col:
        return {}

    out: dict[str, str] = {}
    for row in df.to_dict(orient="records"):
        rep_id = _clean_text(row.get(user_id_col))
        if not rep_id:
            continue
        full_name = _clean_text(row.get(full_col))
        if not full_name:
            first = _clean_text(row.get(first_col))
            last = _clean_text(row.get(last_col))
            full_name = " ".join(part for part in (first, last) if part).strip()
        if full_name and not _is_technical_rep_identifier(full_name):
            out[rep_id] = full_name
    return out


def _fact_rep_directory() -> dict[str, str]:
    cols = fact_store.list_columns()
    rep_id_expr = _coalesce_text_expr(cols, TXN_REP_ID_CANDIDATES, "NULL::VARCHAR")
    rep_name_expr = _coalesce_text_expr(cols, TXN_REP_NAME_CANDIDATES, "NULL::VARCHAR")
    owner_id_expr = _coalesce_text_expr(cols, OWNER_REP_ID_CANDIDATES, "NULL::VARCHAR")
    owner_name_expr = _coalesce_text_expr(cols, OWNER_REP_NAME_CANDIDATES, "NULL::VARCHAR")
    if rep_id_expr == "NULL::VARCHAR" and owner_id_expr == "NULL::VARCHAR":
        return {}

    sql = f"""
        WITH rep_candidates AS (
            SELECT
                {rep_id_expr} AS rep_id,
                {rep_name_expr} AS rep_name
            FROM fact
            UNION ALL
            SELECT
                {owner_id_expr} AS rep_id,
                {owner_name_expr} AS rep_name
            FROM fact
        )
        SELECT
            rep_id,
            ANY_VALUE(rep_name) AS rep_name
        FROM rep_candidates
        WHERE rep_id IS NOT NULL
          AND rep_name IS NOT NULL
          AND rep_name <> ''
          AND rep_name <> rep_id
        GROUP BY 1
    """
    try:
        df = fact_store.get_duckdb_conn().execute(sql).df()
    except Exception:
        return {}
    work = _normalize_frame(df)
    if work.empty:
        return {}
    out: dict[str, str] = {}
    for row in work.to_dict(orient="records"):
        rep_id = _clean_text(row.get("rep_id"))
        rep_name = _clean_text(row.get("rep_name"))
        if rep_id and rep_name and not _is_technical_rep_identifier(rep_name):
            out[rep_id] = rep_name
    return out


def _rep_directory() -> dict[str, str]:
    cache_key = _rep_directory_cache_key()
    with _REP_DIRECTORY_LOCK:
        cached = _REP_DIRECTORY_CACHE.get(cache_key)
        if cached is not None:
            return dict(cached)

    mapping = _csv_rep_directory()
    mapping.update(_fact_rep_directory())
    with _REP_DIRECTORY_LOCK:
        _REP_DIRECTORY_CACHE.clear()
        _REP_DIRECTORY_CACHE[cache_key] = dict(mapping)
    return mapping


def _normalize_rep_bucket_label(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    return _SAFE_REP_BUCKET_ALIASES.get(text.lower())


def _is_technical_rep_identifier(value: Any) -> bool:
    text = _clean_text(value)
    if not text:
        return False
    if _normalize_rep_bucket_label(text):
        return False
    lowered = text.lower()
    if lowered in _SAFE_REP_BUCKETS:
        return False
    if "@" in text or "/" in text or "\\" in text:
        return True
    if _UUID_RE.fullmatch(text):
        return True
    if " " not in text and any(ch.isdigit() for ch in text) and _TECHNICAL_REP_RE.fullmatch(text):
        return True
    return " " not in text and len(text) >= 12 and re.fullmatch(r"[A-Za-z0-9_-]+", text) is not None


def _business_rep_name(name: Any, fallback_id: Any = None, *, default: str = _REVIEW_REP_LABEL) -> str:
    primary = _clean_text(name)
    fallback = _clean_text(fallback_id)
    directory = _rep_directory()
    for candidate in (primary, fallback):
        if not candidate:
            continue
        normalized = _normalize_rep_bucket_label(candidate)
        if normalized:
            return normalized
        mapped = directory.get(candidate)
        normalized = _normalize_rep_bucket_label(mapped)
        if normalized:
            return normalized
        if mapped and not _is_technical_rep_identifier(mapped):
            return mapped
        if not _is_technical_rep_identifier(candidate):
            return candidate
    return default


def _business_rep_reference(name: Any, fallback_id: Any = None, *, default: str = _REVIEW_REP_LABEL) -> Dict[str, str | None]:
    rep_id = _clean_text(fallback_id)
    raw_name = _clean_text(name)
    if not rep_id and raw_name and _is_technical_rep_identifier(raw_name):
        rep_id = raw_name
    return {
        "rep_id": rep_id or None,
        "rep_name": _business_rep_name(raw_name, rep_id, default=default),
    }


def _business_rep_csv(value: Any) -> str:
    raw = _clean_text(value)
    if not raw:
        return ""
    cleaned: list[str] = []
    for part in [item.strip() for item in raw.split(",") if str(item).strip()]:
        label = _business_rep_name(part, part, default="")
        if label and label not in cleaned:
            cleaned.append(label)
    return ", ".join(cleaned)


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


def _all_time_requested(args: Any) -> bool:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    raw = getter("all_time") or getter("full_history") or getter("no_window") or getter("export_all")
    if raw is None:
        return False
    try:
        return str(raw).strip().lower() in {"1", "true", "yes", "on", "all"}
    except Exception:
        return False


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


def _clean_int(val: Any, default: int = 0) -> int:
    try:
        return int(val)
    except Exception:
        return default


def _pagination(args: Any, default_size: int = TABLE_PAGE_SIZE_DEFAULT, max_size: int = 100) -> Tuple[int, int]:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    try:
        page = max(1, int(getter("page", 1)))
    except Exception:
        page = 1
    try:
        size = int(getter("page_size") or getter("per_page") or default_size)
    except Exception:
        size = default_size
    if size not in TABLE_PAGE_SIZES:
        size = default_size
    size = max(1, min(size, max_size))
    return page, size


def _sort_params(args: Any) -> Tuple[str, str]:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    sort_raw = str(getter("sort") or getter("sort_by") or "revenue").strip().lower()
    dir_raw = str(getter("dir") or getter("sort_dir") or getter("direction") or "desc").strip().lower()
    mapping = {
        "rep": "rep_name",
        "name": "rep_name",
        "rep_name": "rep_name",
        "label": "rep_name",
        "revenue": "revenue",
        "profit": "profit",
        "margin_dollar": "profit",
        "margin$": "profit",
        "margin_amount": "profit",
        "margin": "margin_pct",
        "margin_pct": "margin_pct",
        "orders": "orders",
        "customers": "customers",
        "active_customers": "active_customers",
        "weight": "weight_lb",
        "weight_lb": "weight_lb",
        "units": "units",
        "qty": "units",
        "asp": "asp",
        "asp_lb": "asp_lb",
        "avg_order_value": "avg_order_value",
        "revenue_per_customer": "revenue_per_customer",
        "momentum": "momentum_pct",
        "top_customer_share": "top_customer_share",
        "top_5_customer_share": "top_5_customer_share",
        "concentration": "customer_hhi",
        "hhi": "customer_hhi",
        "mom_revenue_pct": "mom_revenue_pct",
        "mom_profit_pct": "mom_profit_pct",
        "yoy_revenue_pct": "yoy_revenue_pct",
        "yoy_profit_pct": "yoy_profit_pct",
        "ownership_delta": "ownership_delta_revenue",
        "ownership_delta_revenue": "ownership_delta_revenue",
        "current_owned_customers": "current_owned_customers",
        "inherited_customers": "inherited_customers",
        "gained_customers": "gained_customers",
        "lost_customers": "lost_customers",
        "territory_count": "territory_count",
        "replaced_rep_count": "replaced_rep_count",
        "top_territory_revenue": "top_territory_revenue",
        "transferred_in_revenue": "transferred_in_revenue",
        "top_customer_revenue": "top_customer_revenue",
    }
    sort_by = mapping.get(sort_raw, "revenue")
    sort_dir = "asc" if dir_raw in {"asc", "ascending", "up", "1"} else "desc"
    return sort_by, sort_dir


def _search_term(args: Any) -> str:
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    raw = getter("search") or getter("q") or ""
    return str(raw).strip().lower()


def _apply_search_filter(df, search_term: str):
    if df is None or getattr(df, "empty", True) or not search_term:
        return df
    work = _normalize_frame(df)
    if work.empty:
        return work
    rep_series = work.apply(
        lambda row: _business_rep_name(row.get("rep_name"), row.get("rep_key") or row.get("rep_id"), default=""),
        axis=1,
    )
    territory_names = work.get("top_territory_name", pd.Series(dtype="string")).fillna("").astype(str).str.lower()
    top_customers = work.get("top_customer_name", pd.Series(dtype="string")).fillna("").astype(str).str.lower()
    mask = (
        rep_series.fillna("").astype(str).str.lower().str.contains(search_term, na=False)
        | territory_names.str.contains(search_term, na=False)
        | top_customers.str.contains(search_term, na=False)
    )
    return work.loc[mask]


def _normalize_frame(df):
    if df is None:
        return pd.DataFrame()
    try:
        records = df.to_dict(orient="records")
        if records:
            return pd.DataFrame.from_records(records)
        cols = list(getattr(df, "columns", []))
        return pd.DataFrame(columns=cols)
    except Exception:
        try:
            return df.reset_index(drop=True).copy()
        except Exception:
            return df


def _rollup_records(source: Any) -> List[Dict[str, Any]]:
    if source is None:
        return []
    if isinstance(source, list):
        return [dict(item) for item in source if isinstance(item, dict)]
    try:
        rows = source.to_dict(orient="records")
    except Exception:
        return []
    return [dict(item) for item in rows]


def _sanitize_rollup_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    rep_id = rec.get("rep_id") or rec.get("rep_key")
    rep_name = _business_rep_name(rec.get("rep_name"), rep_id)
    top_protein_family = rec.get("top_protein_family")
    margin_rule = margin_rules.resolve_margin_rule(top_protein_family, top_protein_family)
    minimum_margin_pct = _clean_optional(rec.get("minimum_margin_pct"))
    target_margin_pct = _clean_optional(rec.get("target_margin_pct"))
    if margin_rule.get("mapped"):
        if minimum_margin_pct is None:
            minimum_margin_pct = _clean_optional(margin_rule.get("min_gross_margin_pct"))
        if target_margin_pct is None:
            target_margin_pct = _clean_optional(margin_rule.get("target_gross_margin_pct"))
    status = margin_rules.classify_margin_status(rec.get("margin_pct"), minimum_margin_pct, target_margin_pct)
    revenue = _clean_float(rec.get("revenue"))
    transferred_in_revenue = _clean_optional(rec.get("transferred_in_revenue"))
    inherited_customers = _clean_int(rec.get("inherited_customers"))
    active_customers = _clean_int(rec.get("active_customers"))
    healthy_customers = _clean_int(rec.get("healthy_customers"))
    direct_revenue = _clean_optional(rec.get("direct_revenue"))
    if direct_revenue is None and transferred_in_revenue is not None:
        direct_revenue = revenue - transferred_in_revenue
    direct_profit = _clean_optional(rec.get("direct_profit"))
    direct_weight_lb = _clean_optional(rec.get("direct_weight_lb"))
    direct_margin_pct = None
    if direct_revenue not in (None, 0) and direct_profit is not None:
        direct_margin_pct = (_clean_float(direct_profit) / _clean_float(direct_revenue)) * 100.0
    inherited_revenue_share = None
    if revenue > 0 and transferred_in_revenue is not None:
        inherited_revenue_share = (transferred_in_revenue / revenue) * 100.0
    return {
        "rep_id": rep_id,
        "rep_name": rep_name,
        "rep_key": rec.get("rep_key") or rec.get("rep_id"),
        "revenue": revenue,
        "cost": _clean_optional(rec.get("cost")),
        "profit": _clean_optional(rec.get("profit")),
        "prior_revenue": _clean_optional(rec.get("prior_revenue")),
        "prior_profit": _clean_optional(rec.get("prior_profit")),
        "yoy_revenue": _clean_optional(rec.get("yoy_revenue")),
        "yoy_profit": _clean_optional(rec.get("yoy_profit")),
        "margin_pct": _clean_optional(rec.get("margin_pct")),
        "orders": _clean_int(rec.get("orders")),
        "customers": _clean_int(rec.get("customers")),
        "units": _clean_float(rec.get("units")),
        "weight_lb": _clean_float(rec.get("weight_lb")),
        "asp": _clean_optional(rec.get("asp")),
        "asp_lb": _clean_optional(rec.get("asp_lb")),
        "last_order_date": rec.get("last_order_date"),
        "mom_revenue_delta": _clean_optional(rec.get("mom_revenue_delta")),
        "yoy_revenue_delta": _clean_optional(rec.get("yoy_revenue_delta")),
        "avg_order_value": _clean_optional(rec.get("avg_order_value")),
        "revenue_per_customer": _clean_optional(rec.get("revenue_per_customer")),
        "top_customer_share": _clean_optional(rec.get("top_customer_share")),
        "top_5_customer_share": _clean_optional(rec.get("top_5_customer_share")),
        "top_customer_name": rec.get("top_customer_name"),
        "top_customer_revenue": _clean_optional(rec.get("top_customer_revenue")),
        "customer_hhi": _clean_optional(rec.get("customer_hhi")),
        "momentum_pct": _clean_optional(rec.get("momentum_pct")),
        "mom_revenue_pct": _clean_optional(rec.get("mom_revenue_pct")),
        "mom_profit_pct": _clean_optional(rec.get("mom_profit_pct")),
        "mom_margin_pct": _clean_optional(rec.get("mom_margin_pct")),
        "yoy_revenue_pct": _clean_optional(rec.get("yoy_revenue_pct")),
        "yoy_profit_pct": _clean_optional(rec.get("yoy_profit_pct")),
        "yoy_margin_delta": _clean_optional(rec.get("yoy_margin_delta")),
        "minimum_margin_pct": minimum_margin_pct,
        "target_margin_pct": target_margin_pct,
        "target_gap_pct_points": (
            None
            if _clean_optional(rec.get("margin_pct")) is None or target_margin_pct is None
            else _clean_float(rec.get("margin_pct")) - target_margin_pct
        ),
        "active_customers": active_customers,
        "healthy_customers": _clean_int(rec.get("healthy_customers")),
        "direct_customers": max(active_customers - inherited_customers, 0),
        "current_owned_customers": _clean_int(rec.get("current_owned_customers")),
        "inherited_customers": inherited_customers,
        "direct_revenue": _clean_optional(direct_revenue),
        "direct_profit": direct_profit,
        "direct_weight_lb": direct_weight_lb,
        "direct_margin_pct": _clean_optional(direct_margin_pct),
        "inherited_revenue_share_pct": _clean_optional(inherited_revenue_share),
        "gained_customers": _clean_int(rec.get("gained_customers")),
        "lost_customers": _clean_int(rec.get("lost_customers")),
        "territory_count": _clean_int(rec.get("territory_count")),
        "unassigned_customers": _clean_int(rec.get("unassigned_customers")),
        "replaced_rep_count": _clean_int(rec.get("replaced_rep_count")),
        "replaced_rep_names": _business_rep_csv(rec.get("replaced_rep_names")),
        "top_territory_name": rec.get("top_territory_name"),
        "top_territory_revenue": _clean_optional(rec.get("top_territory_revenue")),
        "transferred_in_revenue": _clean_optional(rec.get("transferred_in_revenue")),
        "transferred_out_revenue": _clean_optional(rec.get("transferred_out_revenue")),
        "transfer_revenue": _clean_optional(rec.get("transfer_revenue")),
        "unassigned_revenue": _clean_optional(rec.get("unassigned_revenue")),
        "current_owner_revenue": _clean_optional(rec.get("current_owner_revenue")),
        "historical_revenue": _clean_optional(rec.get("historical_revenue")),
        "ownership_delta_revenue": _clean_optional(rec.get("ownership_delta_revenue")),
        "ownership_delta_pct": _clean_optional(rec.get("ownership_delta_pct")),
        "top_protein_family": rec.get("top_protein_family"),
        "top_protein_revenue": _clean_float(rec.get("top_protein_revenue")),
        "leakage_revenue": _clean_float(rec.get("leakage_revenue")),
        "leakage_count": _clean_int(rec.get("leakage_count")),
        "overdue_customers": _clean_int(rec.get("overdue_customers")),
        "protein_penetration": [
            {
                "family": str(p.get("protein_family")),
                "customers": _clean_int(p.get("customer_count")),
                "penetration_pct": (
                    (_clean_int(p.get("customer_count")) / healthy_customers * 100.0)
                    if healthy_customers > 0
                    else 0.0
                ),
            }
            for p in _struct_list(rec.get("protein_stats"))
        ],
        # Pass through quartile if already computed by build_salesreps_bundle

        "revenue_quartile": _clean_int(rec.get("revenue_quartile", 0)),
        "quartile_label": rec.get("quartile_label", ""),
        **status,
        **_compute_health_score(rec, active_customers, inherited_customers),
    }


def _compute_health_score(rec: Dict[str, Any], active_customers: int, inherited_customers: int) -> Dict[str, Any]:
    """Compute a 0–100 Health Score for a rep row from four components."""
    mom_revenue_pct = _clean_optional(rec.get("mom_revenue_pct"))
    margin_pct = _clean_optional(rec.get("margin_pct"))
    top_customer_share = _clean_optional(rec.get("top_customer_share"))
    gained = _clean_int(rec.get("gained_customers"))
    lost = _clean_int(rec.get("lost_customers"))

    # Component 1 — Revenue Momentum (25 pts)
    if mom_revenue_pct is None:
        c1 = 15  # neutral when unavailable
    elif mom_revenue_pct >= 5:
        c1 = 25
    elif mom_revenue_pct >= 0:
        c1 = 15
    elif mom_revenue_pct >= -10:
        c1 = 8
    else:
        c1 = 0

    # Component 2 — Margin Health (25 pts)
    if margin_pct is None:
        c2 = 0
    elif margin_pct >= 32:
        c2 = 25
    elif margin_pct >= 27:
        c2 = 18
    elif margin_pct >= 20:
        c2 = 10
    else:
        c2 = 0

    # Component 3 — Customer Retention (25 pts)
    # prev_customers = active_customers - gained + lost
    prev_customers = max(active_customers - gained + lost, 0)
    if prev_customers > 0:
        retained = max(prev_customers - lost, 0)
        retention_rate = retained / prev_customers
        if retention_rate >= 0.85:
            c3 = 25
        elif retention_rate >= 0.70:
            c3 = 15
        elif retention_rate >= 0.50:
            c3 = 8
        else:
            c3 = 0
    else:
        c3 = 15  # neutral when retention data is not available

    # Component 4 — Concentration Risk (25 pts)
    if top_customer_share is None:
        c4 = 15  # neutral when unavailable
    elif top_customer_share <= 0.20:
        c4 = 25
    elif top_customer_share <= 0.30:
        c4 = 18
    elif top_customer_share <= 0.40:
        c4 = 10
    else:
        c4 = 0

    health_score = c1 + c2 + c3 + c4
    if health_score >= 80:
        health_label = "Excellent"
        health_color = "#198754"
    elif health_score >= 60:
        health_label = "Good"
        health_color = "#0d6efd"
    elif health_score >= 40:
        health_label = "Fair"
        health_color = "#fd7e14"
    else:
        health_label = "At Risk"
        health_color = "#dc3545"

    return {
        "health_score": health_score,
        "health_label": health_label,
        "health_color": health_color,
        "health_components": {
            "momentum": c1,
            "margin": c2,
            "retention": c3,
            "concentration": c4,
        },
    }


def _salesrep_lost_accounts(customers_records: List[Dict[str, Any]], ref_date: Any) -> List[Dict[str, Any]]:
    """
    Return high-priority targets for follow-up based on:
    1. SILENT: No orders in the last 30 days (reactive).
    2. OVERDUE: Orders missed based on historical pulse (predictive).
    
    Includes a priority_score (0-100) based on revenue volume and churn probability.
    Sorted by priority_score DESC, top 100 returned.
    """
    targets: List[Dict[str, Any]] = []
    ref_dt = pd.to_datetime(ref_date, errors="coerce") if ref_date is not None else None

    for row in customers_records:
        rev_last = _clean_float(row.get("revenue") if "revenue" in row else row.get("revenue_last_30"))
        rev_prev = _clean_float(row.get("prior_revenue") if "prior_revenue" in row else row.get("revenue_prev_30"))
        is_overdue = bool(row.get("is_overdue", False))
        
        last_order_raw = row.get("last_order_date") or row.get("last_sale_date")
        last_order_str = str(last_order_raw)[:10] if last_order_raw else None
        days_since: int | None = None
        if ref_dt is not None and last_order_str:
            lod = pd.to_datetime(last_order_str, errors="coerce")
            if not pd.isna(lod):
                days_since = int((ref_dt - lod).days)

        # Condition 1: Lost (Silent > 30 days)
        is_silent = (rev_last == 0.0 and rev_prev > 0.0)
        
        if is_silent or is_overdue:
            targets.append({
                "customer_id": row.get("customer_id"),
                "customer_name": row.get("customer_name") or row.get("customer_id"),
                "account_owner_name": row.get("account_owner_name") or row.get("last_sales_rep_name"),
                "account_owner_id": row.get("account_owner_id") or row.get("last_sales_rep_id"),
                "territory_name": row.get("territory_name"),
                "revenue_prev_30": rev_prev,
                "last_order_date": last_order_str,
                "days_since_order": days_since,
                "is_overdue": is_overdue,
                "is_silent": is_silent,
                "historical_proteins": row.get("historical_proteins", []),
                "avg_days": _clean_float(row.get("avg_days_between_orders")),
                "customer_phone": row.get("customer_phone"),
                "customer_email": row.get("customer_email"),
            })

    if not targets:
        return []

    max_rev = max((r['revenue_prev_30'] for r in targets), default=1.0)
    
    for r in targets:
        # Volume weight (0-60 pts)
        rev_score = (r['revenue_prev_30'] / max_rev) * 60
        
        # Probability weight (0-40 pts)
        prob_score = 0.0
        if r['is_overdue']:
            prob_score = 40.0
            r['urgency_label'] = "Predictive"
            avg = r['avg_days'] or 7.0
            deviation = (r['days_since_order'] or 0) / avg
            r['opportunity_reason'] = f"Overdue: {deviation:.1f}x normal pulse ({int(avg)}d cycle)"
        elif r['is_silent']:
            prob_score = 25.0
            r['urgency_label'] = "Silent"
            r['opportunity_reason'] = f"No activity in 30 days"
            
        r['priority_score'] = round(rev_score + prob_score, 1)

    targets.sort(key=lambda r: r.get("priority_score", 0.0), reverse=True)
    return targets[:100]


def _sort_rollup_records(records: List[Dict[str, Any]], sort_by: str, sort_dir: str) -> List[Dict[str, Any]]:
    if not records:
        return []
    ascending = sort_dir == "asc"
    token = sort_by or "revenue"

    def _name(rec: Dict[str, Any]) -> str:
        return _business_rep_name(rec.get("rep_name"), rec.get("rep_id") or rec.get("rep_key"), default="").strip().lower()

    if token == "rep_name":
        return sorted(records, key=lambda rec: (_name(rec), str(rec.get("rep_id") or "")), reverse=not ascending)

    def _metric(rec: Dict[str, Any]) -> float:
        val = rec.get(token)
        return _clean_float(val)

    return sorted(
        records,
        key=lambda rec: (_metric(rec), _name(rec)),
        reverse=not ascending,
    )


def _sort_rollup_df(df, sort_by: str, sort_dir: str):
    if df is None or getattr(df, "empty", True):
        return df
    work = _normalize_frame(df)
    if sort_by not in work.columns and sort_by != "rep_name":
        sort_by = "revenue"
    ascending = sort_dir == "asc"

    records = work.to_dict(orient="records")

    def _name(rec: Dict[str, Any]) -> str:
        return _business_rep_name(rec.get("rep_name"), rec.get("rep_key") or rec.get("rep_id"), default="").strip().lower()

    if sort_by == "rep_name":
        records = sorted(records, key=lambda rec: (_name(rec), str(rec.get("rep_key") or "")), reverse=not ascending)
        return pd.DataFrame.from_records(records, columns=work.columns)

    def _metric_key(rec: Dict[str, Any]) -> tuple[int, float, str]:
        raw = rec.get(sort_by)
        missing = raw is None
        try:
            metric = float(raw)
            if math.isnan(metric):
                missing = True
                metric = 0.0
        except Exception:
            missing = True
            metric = 0.0
        if not ascending:
            metric *= -1
        return (1 if missing else 0, metric, _name(rec))

    records = sorted(records, key=_metric_key)
    return pd.DataFrame.from_records(records, columns=work.columns)


def _required_columns(cols: set[str]) -> Dict[str, str | None]:
    date_col = _safe_col(cols, fs.CANON.date, *fs.DATE_CANDIDATES)
    revenue_col = _safe_col(cols, fs.CANON.revenue, *fs.REVENUE_CANDIDATES)
    order_col = _safe_col(cols, fs.CANON.order_id, *ORDER_ID_CANDIDATES)
    customer_col = _safe_col(cols, fs.CANON.customer_id, *CUSTOMER_ID_CANDIDATES)
    customer_name_col = _safe_col(cols, fs.CANON.customer_name, *CUSTOMER_NAME_CANDIDATES)
    product_col = _safe_col(cols, fs.CANON.product_id, *PRODUCT_ID_CANDIDATES)
    product_name_col = _safe_col(cols, fs.CANON.product_name, *PRODUCT_NAME_CANDIDATES)
    protein_col = _safe_col(cols, *PROTEIN_CANDIDATES)
    category_col = _safe_col(cols, *CATEGORY_CANDIDATES)
    territory_id_col = _safe_col(cols, *TERRITORY_ID_CANDIDATES)
    territory_name_col = _safe_col(cols, *TERRITORY_NAME_CANDIDATES)
    missing_packs_col = _safe_col(cols, "missing_packs")
    phone_expr = _coalesce_text_expr(cols, ("Phone", "CustomerPhone", "ship_phone", "Telephone"))
    email_expr = _coalesce_text_expr(cols, ("Email", "CustomerEmail", "ship_email", "EmailAddress"))
    cost_expr = _coalesce_expr(cols, (fs.CANON.cost, *fs.COST_TOTAL_CANDIDATES, "CostPrice"), "NULL")
    qty_expr = _coalesce_expr(cols, (fs.CANON.qty_units, *fs.QTY_CANDIDATES, "ShippedItems"), "0")
    weight_expr = _coalesce_expr(cols, (fs.CANON.weight_lb, *fs.WEIGHT_CANDIDATES), "0")
    lat_expr = _coalesce_expr(cols, ("DeliveryLat", "Lat", "Latitude", "ship_lat"), "NULL::DOUBLE")
    lng_expr = _coalesce_expr(cols, ("DeliveryLong", "DeliveryLng", "Lng", "Longitude", "ship_lng"), "NULL::DOUBLE")
    return {
        "date": date_col,
        "revenue": revenue_col,
        "order": order_col,
        "customer": customer_col,
        "customer_name": customer_name_col,
        "product": product_col,
        "product_name": product_name_col,
        "protein": protein_col,
        "category": category_col,
        "territory_id": territory_id_col,
        "territory_name": territory_name_col,
        "missing_packs": missing_packs_col,
        "phone_expr": phone_expr,
        "email_expr": email_expr,
        "cost_expr": cost_expr,
        "qty_expr": qty_expr,
        "weight_expr": weight_expr,
        "lat_expr": lat_expr,
        "lng_expr": lng_expr,
    }


def _coalesce_text_expr(cols: set[str], candidates: Sequence[str], default: str = "NULL::VARCHAR") -> str:
    present = _present_cols(cols, candidates)
    exprs = [_string_expr(col) for col in present]
    return _coalesce_exprs(exprs, default)


def _rep_pair_exprs(
    cols: set[str],
    id_candidates: Sequence[str],
    name_candidates: Sequence[str],
    *,
    default: str = "'Unassigned / Needs Review'",
) -> Tuple[str, str]:
    id_expr = _coalesce_text_expr(cols, id_candidates, default)
    name_expr = _coalesce_text_expr(cols, name_candidates, id_expr)
    return id_expr, name_expr


def _transaction_rep_exprs(cols: set[str]) -> Tuple[str, str]:
    return _rep_pair_exprs(cols, TXN_REP_ID_CANDIDATES, TXN_REP_NAME_CANDIDATES)


def _current_owner_exprs(cols: set[str]) -> Tuple[str, str]:
    owner_id_col = _safe_col(cols, *OWNER_REP_ID_CANDIDATES)
    owner_name_col = _safe_col(cols, *OWNER_REP_NAME_CANDIDATES)
    if owner_id_col or owner_name_col:
        return _rep_pair_exprs(cols, OWNER_REP_ID_CANDIDATES, OWNER_REP_NAME_CANDIDATES)
    return _transaction_rep_exprs(cols)


def _sql_date_literal(value: str | None) -> str:
    if not value:
        return ""
    escaped = str(value).replace("'", "''")
    return f"DATE '{escaped}'"


def _window_predicate(column: str, start_iso: str | None, end_iso: str | None, *, default_true: bool) -> str:
    parts: list[str] = []
    if start_iso:
        parts.append(f"{column} >= {_sql_date_literal(start_iso)}")
    if end_iso:
        parts.append(f"{column} < {_sql_date_literal(end_iso)}")
    if not parts:
        return "1=1" if default_true else "0=1"
    return " AND ".join(parts)


def _window_context(filters: Any, cols: set[str], scope: Dict[str, Any]) -> Dict[str, Any]:
    normalized = filters_service.normalize_filters(filters)
    _current_where, _current_params, start_iso, end_iso = fact_store.build_where_clause(
        normalized,
        cols,
        scope,
        apply_default_window=True,
    )
    scope_filters = replace(
        normalized,
        start=None,
        end=None,
        preset=None,
        complete_months_only=False,
    )
    scope_where, scope_params, _, _ = fact_store.build_where_clause(
        scope_filters,
        cols,
        scope,
        apply_default_window=False,
    )

    start_ts = pd.to_datetime(start_iso, errors="coerce") if start_iso else None
    end_ts = pd.to_datetime(end_iso, errors="coerce") if end_iso else None
    prior_start_iso = None
    prior_end_iso = None
    yoy_start_iso = None
    yoy_end_iso = None
    base_start_iso = start_iso
    base_end_iso = end_iso

    if start_ts is not None and end_ts is not None and pd.notna(start_ts) and pd.notna(end_ts):
        window_days = max(int((end_ts - start_ts).days), 1)
        prior_start = start_ts - pd.Timedelta(days=window_days)
        prior_end = start_ts
        yoy_start = start_ts - pd.DateOffset(years=1)
        yoy_end = end_ts - pd.DateOffset(years=1)
        prior_start_iso = prior_start.date().isoformat()
        prior_end_iso = prior_end.date().isoformat()
        yoy_start_iso = yoy_start.date().isoformat()
        yoy_end_iso = yoy_end.date().isoformat()
        base_start = min(start_ts, prior_start, yoy_start)
        base_end = end_ts
        base_start_iso = base_start.date().isoformat()
        base_end_iso = base_end.date().isoformat()
    elif start_ts is not None and pd.notna(start_ts):
        base_start_iso = start_ts.date().isoformat()

    current_display_end = None
    if end_ts is not None and pd.notna(end_ts):
        try:
            current_display_end = (end_ts - pd.Timedelta(days=1)).date().isoformat()
        except Exception:
            current_display_end = end_iso

    return {
        "scope_where_sql": scope_where,
        "scope_params": list(scope_params),
        "window_start": start_iso,
        "window_end_exclusive": end_iso,
        "window_end_display": current_display_end or end_iso,
        "prior_start": prior_start_iso,
        "prior_end_exclusive": prior_end_iso,
        "yoy_start": yoy_start_iso,
        "yoy_end_exclusive": yoy_end_iso,
        "base_start": base_start_iso,
        "base_end_exclusive": base_end_iso,
    }


def _attributed_salesrep_ctes(
    cols: set[str],
    cols_map: Dict[str, str | None],
    scope_where_sql: str,
    controls: salesrep_ownership.AttributionControls,
    windows: Dict[str, Any],
    *,
    bridge_available: bool,
    successor_available: bool,
) -> str:
    date_col = cols_map.get("date")
    revenue_col = cols_map.get("revenue")
    order_col = cols_map.get("order")
    customer_col = cols_map.get("customer")
    if not all([date_col, revenue_col, order_col, customer_col]):
        return ""

    customer_name_col = cols_map.get("customer_name")
    product_col = cols_map.get("product")
    product_name_col = cols_map.get("product_name")
    protein_col = cols_map.get("protein")
    category_col = cols_map.get("category")
    territory_id_col = cols_map.get("territory_id")
    territory_name_col = cols_map.get("territory_name")
    cost_expr = cols_map.get("cost_expr") or "NULL"
    qty_expr = cols_map.get("qty_expr") or "0"
    weight_expr = cols_map.get("weight_expr") or "0"
    missing_packs_col = cols_map.get("missing_packs")
    missing_packs_expr = f"CAST({_quote(missing_packs_col)} AS BOOLEAN)" if missing_packs_col else "NULL::BOOLEAN"

    txn_rep_id_expr, txn_rep_name_expr = _transaction_rep_exprs(cols)
    owner_rep_id_expr, owner_rep_name_expr = _current_owner_exprs(cols)

    order_expr = _string_expr(order_col)
    customer_expr = _string_expr(customer_col)
    customer_name_expr = _string_expr(customer_name_col) if customer_name_col else customer_expr
    product_expr = _string_expr(product_col) if product_col else "NULL::VARCHAR"
    product_name_expr = _string_expr(product_name_col) if product_name_col else product_expr
    protein_expr = _string_expr(protein_col) if protein_col else "NULL::VARCHAR"
    category_expr = _string_expr(category_col) if category_col else protein_expr
    territory_id_expr = _string_expr(territory_id_col) if territory_id_col else "NULL::VARCHAR"
    territory_name_expr = _string_expr(territory_name_col) if territory_name_col else territory_id_expr

    base_predicate = _window_predicate(
        "CAST(order_date AS DATE)",
        windows.get("base_start"),
        windows.get("base_end_exclusive"),
        default_true=True,
    )
    current_predicate = _window_predicate(
        "order_date",
        windows.get("window_start"),
        windows.get("window_end_exclusive"),
        default_true=True,
    )
    prior_predicate = _window_predicate(
        "order_date",
        windows.get("prior_start"),
        windows.get("prior_end_exclusive"),
        default_true=False,
    )
    yoy_predicate = _window_predicate(
        "order_date",
        windows.get("yoy_start"),
        windows.get("yoy_end_exclusive"),
        default_true=False,
    )

    roster_clause = "1=1"
    if controls.attribution_mode == salesrep_ownership.ATTRIBUTION_HISTORICAL and controls.roster_mode == salesrep_ownership.ROSTER_CURRENT_ONLY:
        roster_clause = (
            "NOT EXISTS (SELECT 1 FROM current_roster) "
            "OR historical_rep_id IN (SELECT rep_id FROM current_roster) "
            "OR historical_rep_name IN (SELECT rep_name FROM current_roster) "
            "OR COALESCE(historical_rep_id, historical_rep_name) = 'Unassigned / Needs Review'"
        )

    transfer_clause = "1=1"
    if controls.transfer_only:
        transfer_clause = "ownership_changed = 1 OR owner_missing = 1"

    attributed_rep_id_expr = "historical_rep_id"
    attributed_rep_name_expr = "historical_rep_name"
    if controls.is_current_owner_mode:
        if controls.roster_mode == salesrep_ownership.ROSTER_CURRENT_ONLY:
            attributed_rep_id_expr = (
                "CASE WHEN current_owner_active = FALSE THEN 'Unassigned / Needs Review' ELSE current_owner_id END"
            )
            attributed_rep_name_expr = (
                "CASE WHEN current_owner_active = FALSE THEN 'Unassigned / Needs Review' ELSE current_owner_name END"
            )
        else:
            attributed_rep_id_expr = "current_owner_id"
            attributed_rep_name_expr = "current_owner_name"

    bridge_base_source = f"""
            SELECT
                customer_id,
                territory_id,
                territory_name,
                rep_id,
                rep_name,
                prior_rep_id,
                prior_rep_name,
                CAST(assignment_start_date AS DATE) AS assignment_start_date,
                CAST(assignment_end_date AS DATE) AS assignment_end_date,
                is_current,
                ownership_type,
                rep_is_active,
                mapping_confidence,
                dq_status
            FROM {salesrep_ownership.BRIDGE_VIEW_NAME}
    """
    if not bridge_available:
        bridge_base_source = """
            SELECT
                CAST(NULL AS VARCHAR) AS customer_id,
                CAST(NULL AS VARCHAR) AS territory_id,
                CAST(NULL AS VARCHAR) AS territory_name,
                CAST(NULL AS VARCHAR) AS rep_id,
                CAST(NULL AS VARCHAR) AS rep_name,
                CAST(NULL AS VARCHAR) AS prior_rep_id,
                CAST(NULL AS VARCHAR) AS prior_rep_name,
                CAST(NULL AS DATE) AS assignment_start_date,
                CAST(NULL AS DATE) AS assignment_end_date,
                CAST(NULL AS BOOLEAN) AS is_current,
                CAST(NULL AS VARCHAR) AS ownership_type,
                CAST(NULL AS BOOLEAN) AS rep_is_active,
                CAST(NULL AS VARCHAR) AS mapping_confidence,
                CAST(NULL AS VARCHAR) AS dq_status
            WHERE 1=0
        """

    successor_base_source = f"""
            SELECT
                customer_id,
                territory_id,
                territory_name,
                prior_rep_id,
                prior_rep_name,
                successor_rep_id,
                successor_rep_name,
                CAST(effective_start_date AS DATE) AS effective_start_date,
                CAST(effective_end_date AS DATE) AS effective_end_date,
                is_current,
                successor_rep_is_active,
                mapping_confidence,
                dq_status
            FROM {salesrep_ownership.SUCCESSION_VIEW_NAME}
    """
    if not successor_available:
        successor_base_source = """
            SELECT
                CAST(NULL AS VARCHAR) AS customer_id,
                CAST(NULL AS VARCHAR) AS territory_id,
                CAST(NULL AS VARCHAR) AS territory_name,
                CAST(NULL AS VARCHAR) AS prior_rep_id,
                CAST(NULL AS VARCHAR) AS prior_rep_name,
                CAST(NULL AS VARCHAR) AS successor_rep_id,
                CAST(NULL AS VARCHAR) AS successor_rep_name,
                CAST(NULL AS DATE) AS effective_start_date,
                CAST(NULL AS DATE) AS effective_end_date,
                CAST(NULL AS BOOLEAN) AS is_current,
                CAST(NULL AS BOOLEAN) AS successor_rep_is_active,
                CAST(NULL AS VARCHAR) AS mapping_confidence,
                CAST(NULL AS VARCHAR) AS dq_status
            WHERE 1=0
        """

    effective_cost_expr = margin_rules.sql_effective_cost_expr(
        f"CAST({cost_expr} AS DOUBLE)",
        f"CAST({weight_expr} AS DOUBLE)",
        f"CAST({qty_expr} AS DOUBLE)",
        fallback="NULL::DOUBLE",
    )
    minimum_margin_expr = margin_rules.sql_margin_rule_expr(protein_expr, category_expr, "min_gross_margin_pct")
    target_margin_expr = margin_rules.sql_margin_rule_expr(protein_expr, category_expr, "target_gross_margin_pct")
    phone_expr = cols_map.get("phone_expr") or "NULL::VARCHAR"
    email_expr = cols_map.get("email_expr") or "NULL::VARCHAR"
    lat_expr = cols_map.get("lat_expr") or "NULL::DOUBLE"
    lng_expr = cols_map.get("lng_expr") or "NULL::DOUBLE"

    return f"""
        fact_scope AS (
            SELECT
                ROW_NUMBER() OVER () AS fact_row_id,
                CAST({_quote(date_col)} AS DATE) AS order_date,
                {order_expr} AS order_id,
                {customer_expr} AS customer_id,
                {customer_name_expr} AS customer_name,
                {product_expr} AS product_id,
                {product_name_expr} AS product_name,
                {protein_expr} AS protein_family,
                {category_expr} AS category_name,
                {territory_id_expr} AS fact_territory_id,
                {territory_name_expr} AS fact_territory_name,
                {phone_expr} AS customer_phone,
                {email_expr} AS customer_email,
                {lat_expr} AS DeliveryLat,
                {lng_expr} AS DeliveryLong,
                {txn_rep_id_expr} AS transaction_rep_id,
                {txn_rep_name_expr} AS transaction_rep_name,
                {owner_rep_id_expr} AS owner_rep_id_fact,
                {owner_rep_name_expr} AS owner_rep_name_fact,
                CAST({_quote(revenue_col)} AS DOUBLE) AS revenue,
                CAST({cost_expr} AS DOUBLE) AS base_cost,
                ({effective_cost_expr}) AS cost,
                CASE
                    WHEN ({effective_cost_expr}) IS NULL THEN NULL
                    ELSE CAST({_quote(revenue_col)} AS DOUBLE) - ({effective_cost_expr})
                END AS profit,
                CAST({qty_expr} AS DOUBLE) AS units,
                CAST({weight_expr} AS DOUBLE) AS weight_lb,
                ({minimum_margin_expr}) AS minimum_margin_pct_rule,
                ({target_margin_expr}) AS target_margin_pct_rule,
                {missing_packs_expr} AS missing_packs
            FROM fact
            WHERE {scope_where_sql}
        ),
        fact_base AS (
            SELECT *
            FROM fact_scope
            WHERE {base_predicate}
        ),
        fact_owner_profile AS (
            SELECT
                customer_id,
                COUNT(
                    DISTINCT COALESCE(
                        owner_rep_id_fact,
                        owner_rep_name_fact
                    )
                ) AS owner_variant_count
            FROM fact_scope
            WHERE customer_id IS NOT NULL
              AND COALESCE(owner_rep_id_fact, owner_rep_name_fact) IS NOT NULL
            GROUP BY 1
        ),
        fact_owner_ranked AS (
            SELECT
                customer_id,
                owner_rep_id_fact,
                owner_rep_name_fact,
                fact_territory_id,
                fact_territory_name,
                COALESCE(fop.owner_variant_count, 0) AS owner_variant_count,
                ROW_NUMBER() OVER (
                    PARTITION BY customer_id
                    ORDER BY order_date DESC NULLS LAST, order_id DESC NULLS LAST, fact_row_id DESC
                ) AS rn
            FROM fact_scope
            LEFT JOIN fact_owner_profile fop USING (customer_id)
            WHERE customer_id IS NOT NULL
              AND (owner_rep_id_fact IS NOT NULL OR owner_rep_name_fact IS NOT NULL)
        ),
        fact_owner_current AS (
            SELECT
                customer_id,
                owner_rep_id_fact AS rep_id,
                owner_rep_name_fact AS rep_name,
                fact_territory_id AS territory_id,
                fact_territory_name AS territory_name,
                owner_variant_count
            FROM fact_owner_ranked
            WHERE rn = 1
        ),
        customer_last_sale_ranked AS (
            SELECT
                customer_id,
                transaction_rep_id,
                transaction_rep_name,
                order_date,
                order_id,
                ROW_NUMBER() OVER (
                    PARTITION BY customer_id
                    ORDER BY order_date DESC NULLS LAST, order_id DESC NULLS LAST, fact_row_id DESC
                ) AS rn
            FROM fact_scope
            WHERE customer_id IS NOT NULL
        ),
        customer_last_sale AS (
            SELECT
                customer_id,
                transaction_rep_id AS last_sales_rep_id,
                transaction_rep_name AS last_sales_rep_name,
                order_date AS last_sale_date
            FROM customer_last_sale_ranked
            WHERE rn = 1
        ),
        bridge_base_raw AS (
{bridge_base_source}
        ),
        bridge_base AS (
            SELECT
                customer_id,
                territory_id,
                territory_name,
                rep_id,
                rep_name,
                COALESCE(
                    prior_rep_id,
                    LAG(rep_id) OVER (
                        PARTITION BY customer_id
                        ORDER BY
                            COALESCE(assignment_start_date, DATE '1900-01-01'),
                            COALESCE(assignment_end_date, DATE '2999-12-31'),
                            rep_id
                    )
                ) AS prior_rep_id,
                COALESCE(
                    prior_rep_name,
                    LAG(rep_name) OVER (
                        PARTITION BY customer_id
                        ORDER BY
                            COALESCE(assignment_start_date, DATE '1900-01-01'),
                            COALESCE(assignment_end_date, DATE '2999-12-31'),
                            rep_id
                    )
                ) AS prior_rep_name,
                assignment_start_date,
                assignment_end_date,
                is_current,
                ownership_type,
                rep_is_active,
                COALESCE(mapping_confidence, 'ownership_history') AS mapping_confidence,
                COALESCE(dq_status, 'ok') AS dq_status
            FROM bridge_base_raw
        ),
        bridge_current_ranked AS (
            SELECT
                customer_id,
                territory_id,
                territory_name,
                rep_id,
                rep_name,
                prior_rep_id,
                prior_rep_name,
                ownership_type,
                rep_is_active,
                mapping_confidence,
                dq_status,
                ROW_NUMBER() OVER (
                    PARTITION BY customer_id
                    ORDER BY
                        COALESCE(is_current, FALSE) DESC,
                        COALESCE(assignment_end_date, DATE '2999-12-31') DESC,
                        COALESCE(assignment_start_date, DATE '1900-01-01') DESC,
                        rep_id
                ) AS rn
            FROM bridge_base
            WHERE customer_id IS NOT NULL
              AND (
                  COALESCE(is_current, FALSE)
                  OR assignment_end_date IS NULL
                  OR assignment_end_date >= CURRENT_DATE
              )
        ),
        bridge_current AS (
            SELECT
                customer_id,
                territory_id,
                territory_name,
                rep_id,
                rep_name,
                prior_rep_id,
                prior_rep_name,
                ownership_type,
                rep_is_active,
                mapping_confidence,
                dq_status
            FROM bridge_current_ranked
            WHERE rn = 1
        ),
        successor_base AS (
{successor_base_source}
        ),
        successor_customer_ranked AS (
            SELECT
                customer_id,
                territory_id,
                territory_name,
                prior_rep_id,
                prior_rep_name,
                successor_rep_id,
                successor_rep_name,
                successor_rep_is_active,
                COALESCE(mapping_confidence, 'customer_succession') AS mapping_confidence,
                COALESCE(dq_status, 'ok') AS dq_status,
                ROW_NUMBER() OVER (
                    PARTITION BY customer_id
                    ORDER BY
                        COALESCE(is_current, FALSE) DESC,
                        COALESCE(effective_end_date, DATE '2999-12-31') DESC,
                        COALESCE(effective_start_date, DATE '1900-01-01') DESC,
                        successor_rep_id
                ) AS rn
            FROM successor_base
            WHERE customer_id IS NOT NULL
              AND (
                  COALESCE(is_current, FALSE)
                  OR effective_end_date IS NULL
                  OR effective_end_date >= CURRENT_DATE
              )
        ),
        successor_customer_current AS (
            SELECT
                customer_id,
                territory_id,
                territory_name,
                prior_rep_id,
                prior_rep_name,
                successor_rep_id,
                successor_rep_name,
                successor_rep_is_active,
                mapping_confidence,
                dq_status
            FROM successor_customer_ranked
            WHERE rn = 1
        ),
        successor_territory_ranked AS (
            SELECT
                territory_id,
                territory_name,
                prior_rep_id,
                prior_rep_name,
                successor_rep_id,
                successor_rep_name,
                successor_rep_is_active,
                COALESCE(mapping_confidence, 'territory_succession') AS mapping_confidence,
                COALESCE(dq_status, 'ok') AS dq_status,
                ROW_NUMBER() OVER (
                    PARTITION BY territory_id
                    ORDER BY
                        COALESCE(is_current, FALSE) DESC,
                        COALESCE(effective_end_date, DATE '2999-12-31') DESC,
                        COALESCE(effective_start_date, DATE '1900-01-01') DESC,
                        successor_rep_id
                ) AS rn
            FROM successor_base
            WHERE territory_id IS NOT NULL
              AND (
                  COALESCE(is_current, FALSE)
                  OR effective_end_date IS NULL
                  OR effective_end_date >= CURRENT_DATE
              )
        ),
        successor_territory_current AS (
            SELECT
                territory_id,
                territory_name,
                prior_rep_id,
                prior_rep_name,
                successor_rep_id,
                successor_rep_name,
                successor_rep_is_active,
                mapping_confidence,
                dq_status
            FROM successor_territory_ranked
            WHERE rn = 1
        ),
        successor_rep_id_ranked AS (
            SELECT
                prior_rep_id,
                prior_rep_name,
                successor_rep_id,
                successor_rep_name,
                successor_rep_is_active,
                COALESCE(mapping_confidence, 'rep_succession') AS mapping_confidence,
                COALESCE(dq_status, 'ok') AS dq_status,
                ROW_NUMBER() OVER (
                    PARTITION BY prior_rep_id
                    ORDER BY
                        COALESCE(is_current, FALSE) DESC,
                        COALESCE(effective_end_date, DATE '2999-12-31') DESC,
                        COALESCE(effective_start_date, DATE '1900-01-01') DESC,
                        successor_rep_id
                ) AS rn
            FROM successor_base
            WHERE prior_rep_id IS NOT NULL
        ),
        successor_rep_current_id AS (
            SELECT
                prior_rep_id,
                prior_rep_name,
                successor_rep_id,
                successor_rep_name,
                successor_rep_is_active,
                mapping_confidence,
                dq_status
            FROM successor_rep_id_ranked
            WHERE rn = 1
        ),
        successor_rep_name_ranked AS (
            SELECT
                prior_rep_id,
                prior_rep_name,
                successor_rep_id,
                successor_rep_name,
                successor_rep_is_active,
                COALESCE(mapping_confidence, 'rep_succession') AS mapping_confidence,
                COALESCE(dq_status, 'ok') AS dq_status,
                ROW_NUMBER() OVER (
                    PARTITION BY prior_rep_name
                    ORDER BY
                        COALESCE(is_current, FALSE) DESC,
                        COALESCE(effective_end_date, DATE '2999-12-31') DESC,
                        COALESCE(effective_start_date, DATE '1900-01-01') DESC,
                        successor_rep_id
                ) AS rn
            FROM successor_base
            WHERE prior_rep_name IS NOT NULL
        ),
        successor_rep_current_name AS (
            SELECT
                prior_rep_id,
                prior_rep_name,
                successor_rep_id,
                successor_rep_name,
                successor_rep_is_active,
                mapping_confidence,
                dq_status
            FROM successor_rep_name_ranked
            WHERE rn = 1
        ),
        current_owner_map AS (
            SELECT
                COALESCE(b.customer_id, f.customer_id) AS customer_id,
                COALESCE(
                    b.rep_id,
                    sc.successor_rep_id,
                    st.successor_rep_id,
                    sri.successor_rep_id,
                    srn.successor_rep_id,
                    f.rep_id
                ) AS current_owner_id,
                COALESCE(
                    b.rep_name,
                    sc.successor_rep_name,
                    st.successor_rep_name,
                    sri.successor_rep_name,
                    srn.successor_rep_name,
                    f.rep_name,
                    b.rep_id,
                    sc.successor_rep_id,
                    st.successor_rep_id,
                    sri.successor_rep_id,
                    srn.successor_rep_id,
                    f.rep_id
                ) AS current_owner_name,
                COALESCE(b.prior_rep_id, sc.prior_rep_id, st.prior_rep_id, sri.prior_rep_id, srn.prior_rep_id, NULL) AS prior_rep_id,
                COALESCE(
                    b.prior_rep_name,
                    sc.prior_rep_name,
                    st.prior_rep_name,
                    sri.prior_rep_name,
                    srn.prior_rep_name,
                    b.prior_rep_id,
                    sc.prior_rep_id,
                    st.prior_rep_id,
                    sri.prior_rep_id,
                    srn.prior_rep_id
                ) AS prior_rep_name,
                COALESCE(b.territory_id, sc.territory_id, st.territory_id, f.territory_id) AS territory_id,
                COALESCE(
                    b.territory_name,
                    sc.territory_name,
                    st.territory_name,
                    f.territory_name,
                    b.territory_id,
                    sc.territory_id,
                    st.territory_id,
                    f.territory_id
                ) AS territory_name,
                COALESCE(b.ownership_type, 'account_owner') AS ownership_type,
                COALESCE(
                    b.rep_is_active,
                    sc.successor_rep_is_active,
                    st.successor_rep_is_active,
                    sri.successor_rep_is_active,
                    srn.successor_rep_is_active
                ) AS current_owner_active,
                COALESCE(
                    b.mapping_confidence,
                    sc.mapping_confidence,
                    st.mapping_confidence,
                    sri.mapping_confidence,
                    srn.mapping_confidence,
                    CASE
                        WHEN f.customer_id IS NOT NULL AND COALESCE(f.owner_variant_count, 0) <= 1 THEN 'fact_customer_snapshot'
                        WHEN f.customer_id IS NOT NULL THEN 'fact_fallback'
                        ELSE 'unassigned'
                    END
                ) AS mapping_confidence,
                COALESCE(
                    b.dq_status,
                    sc.dq_status,
                    st.dq_status,
                    sri.dq_status,
                    srn.dq_status,
                    CASE
                        WHEN f.customer_id IS NOT NULL AND COALESCE(f.owner_variant_count, 0) <= 1 THEN 'ok'
                        WHEN f.customer_id IS NOT NULL THEN 'fact_owner_only'
                        ELSE 'needs_review'
                    END
                ) AS dq_status,
                CASE
                    WHEN b.customer_id IS NOT NULL THEN 'ownership_bridge'
                    WHEN sc.customer_id IS NOT NULL THEN 'customer_succession'
                    WHEN st.territory_id IS NOT NULL THEN 'territory_succession'
                    WHEN sri.successor_rep_id IS NOT NULL OR sri.successor_rep_name IS NOT NULL THEN 'rep_succession'
                    WHEN srn.successor_rep_id IS NOT NULL OR srn.successor_rep_name IS NOT NULL THEN 'rep_succession'
                    WHEN f.customer_id IS NOT NULL AND COALESCE(f.owner_variant_count, 0) <= 1 THEN 'fact_customer_snapshot'
                    WHEN f.customer_id IS NOT NULL THEN 'fact_current_owner'
                    ELSE 'unassigned'
                END AS owner_source
            FROM fact_owner_current f
            FULL OUTER JOIN bridge_current b USING (customer_id)
            LEFT JOIN successor_customer_current sc
              ON sc.customer_id = COALESCE(b.customer_id, f.customer_id)
            LEFT JOIN successor_territory_current st
              ON st.territory_id = COALESCE(b.territory_id, f.territory_id)
            LEFT JOIN successor_rep_current_id sri
              ON sri.prior_rep_id = COALESCE(b.prior_rep_id, f.rep_id, b.rep_id)
            LEFT JOIN successor_rep_current_name srn
              ON srn.prior_rep_name = COALESCE(b.prior_rep_name, f.rep_name, b.rep_name)
        ),
        history_bridge_ranked AS (
            SELECT
                fb.fact_row_id,
                sh.rep_id,
                sh.rep_name,
                ROW_NUMBER() OVER (
                    PARTITION BY fb.fact_row_id
                    ORDER BY
                        COALESCE(sh.assignment_start_date, DATE '1900-01-01') DESC,
                        COALESCE(sh.assignment_end_date, DATE '2999-12-31') DESC,
                        sh.rep_id
                ) AS rn
            FROM fact_base fb
            JOIN bridge_base sh
              ON sh.customer_id = fb.customer_id
             AND COALESCE(sh.mapping_confidence, '') <> 'fact_owner_snapshot'
             AND fb.order_date >= COALESCE(sh.assignment_start_date, DATE '1900-01-01')
             AND fb.order_date < COALESCE(sh.assignment_end_date + INTERVAL 1 DAY, DATE '2999-12-31')
        ),
        history_bridge AS (
            SELECT
                fact_row_id,
                rep_id,
                rep_name
            FROM history_bridge_ranked
            WHERE rn = 1
        ),
        enriched AS (
            SELECT
                fb.*,
                COALESCE(hb.rep_id, fb.transaction_rep_id, fb.transaction_rep_name, 'Unassigned / Needs Review') AS historical_rep_id,
                COALESCE(hb.rep_name, fb.transaction_rep_name, hb.rep_id, fb.transaction_rep_id, 'Unassigned / Needs Review') AS historical_rep_name,
                COALESCE(com.current_owner_id, fb.owner_rep_id_fact, fb.owner_rep_name_fact, 'Unassigned / Needs Review') AS current_owner_id,
                COALESCE(com.current_owner_name, fb.owner_rep_name_fact, fb.owner_rep_id_fact, 'Unassigned / Needs Review') AS current_owner_name,
                COALESCE(
                    com.prior_rep_id,
                    CASE
                        WHEN COALESCE(hb.rep_id, fb.transaction_rep_id, fb.transaction_rep_name)
                             IS DISTINCT FROM COALESCE(com.current_owner_id, fb.owner_rep_id_fact, fb.owner_rep_name_fact)
                        THEN COALESCE(hb.rep_id, fb.transaction_rep_id)
                        ELSE NULL
                    END
                ) AS prior_rep_id,
                COALESCE(
                    com.prior_rep_name,
                    CASE
                        WHEN COALESCE(hb.rep_id, fb.transaction_rep_id, fb.transaction_rep_name)
                             IS DISTINCT FROM COALESCE(com.current_owner_id, fb.owner_rep_id_fact, fb.owner_rep_name_fact)
                        THEN COALESCE(hb.rep_name, fb.transaction_rep_name, hb.rep_id, fb.transaction_rep_id)
                        ELSE NULL
                    END
                ) AS prior_rep_name,
                COALESCE(com.territory_id, fb.fact_territory_id) AS territory_id,
                COALESCE(com.territory_name, fb.fact_territory_name, com.territory_id, fb.fact_territory_id) AS territory_name,
                COALESCE(com.owner_source, 'unassigned') AS owner_source,
                COALESCE(com.mapping_confidence, CASE
                    WHEN COALESCE(com.current_owner_id, fb.owner_rep_id_fact, fb.owner_rep_name_fact) IS NULL THEN 'needs_review'
                    WHEN com.owner_source = 'ownership_bridge' THEN 'ownership_history'
                    WHEN com.owner_source = 'fact_current_owner' THEN 'fact_fallback'
                    ELSE 'needs_review'
                END) AS mapping_confidence,
                COALESCE(com.dq_status, CASE
                    WHEN COALESCE(com.current_owner_id, fb.owner_rep_id_fact, fb.owner_rep_name_fact) IS NULL THEN 'needs_review'
                    WHEN com.owner_source = 'fact_current_owner' THEN 'fact_owner_only'
                    ELSE 'ok'
                END) AS dq_status,
                com.current_owner_active,
                cls.last_sales_rep_id,
                cls.last_sales_rep_name,
                cls.last_sale_date,
                CASE
                    WHEN COALESCE(com.current_owner_id, fb.owner_rep_id_fact, fb.owner_rep_name_fact) IS NULL THEN 1
                    ELSE 0
                END AS owner_missing,
                CASE
                    WHEN COALESCE(hb.rep_id, fb.transaction_rep_id, fb.transaction_rep_name) IS DISTINCT FROM COALESCE(com.current_owner_id, fb.owner_rep_id_fact, fb.owner_rep_name_fact)
                    THEN 1 ELSE 0
                END AS ownership_changed
            FROM fact_base fb
            LEFT JOIN history_bridge hb ON hb.fact_row_id = fb.fact_row_id
            LEFT JOIN current_owner_map com ON com.customer_id = fb.customer_id
            LEFT JOIN customer_last_sale cls ON cls.customer_id = fb.customer_id
        ),
        current_roster AS (
            SELECT DISTINCT
                current_owner_id AS rep_id,
                current_owner_name AS rep_name
            FROM enriched
            WHERE current_owner_id IS NOT NULL
              AND current_owner_name IS NOT NULL
              AND current_owner_name <> 'Unassigned / Needs Review'
        ),
        attributed_base AS (
            SELECT
                enriched.*,
                {attributed_rep_id_expr} AS rep_key,
                {attributed_rep_name_expr} AS rep_name,
                CASE WHEN {current_predicate} THEN 1 ELSE 0 END AS is_current_window,
                CASE WHEN {prior_predicate} THEN 1 ELSE 0 END AS is_prior_window,
                CASE WHEN {yoy_predicate} THEN 1 ELSE 0 END AS is_yoy_window,
                CASE
                    WHEN COALESCE(current_owner_id, '') <> ''
                         AND COALESCE(current_owner_name, '') <> ''
                         AND COALESCE(current_owner_id, current_owner_name) <> COALESCE(historical_rep_id, historical_rep_name)
                    THEN 1 ELSE 0
                END AS inherited_flag,
                -- Margin Leakage Detection (God Level)
                CASE 
                    WHEN {current_predicate} AND profit IS NOT NULL AND revenue > 0 AND minimum_margin_pct_rule IS NOT NULL
                         AND (profit / revenue * 100.0) < minimum_margin_pct_rule
                    THEN 1 ELSE 0
                END AS is_margin_leakage
            FROM enriched
            WHERE ({roster_clause})
              AND ({transfer_clause})
        ),
        -- 📈 Predictive Churn: Custom Pulse Detection
        customer_pulse AS (
            SELECT 
                customer_id,
                AVG(diff) AS avg_days_between_orders,
                COUNT(*) AS order_count
            FROM (
                SELECT 
                    customer_id,
                    order_date - LAG(order_date) OVER (PARTITION BY customer_id ORDER BY order_date) AS diff
                FROM fact_scope
                WHERE customer_id IS NOT NULL
            )
            GROUP BY 1
            HAVING COUNT(*) >= 3
        ),
        -- 🍖 Protein Whitespace: Species Penetration Map
        protein_customer_map AS (
            SELECT DISTINCT
                rep_key,
                customer_id,
                protein_family
            FROM attributed_base
            WHERE is_current_window = 1 AND protein_family IS NOT NULL AND protein_family <> 'Unassigned'
        ),
        rep_protein_stats AS (
            SELECT 
                rep_key,
                protein_family,
                COUNT(DISTINCT customer_id) AS customer_count
            FROM protein_customer_map
            GROUP BY 1, 2
        )
    """


def _ownership_bridge_meta_fallback(message: str) -> salesrep_ownership.OwnershipBridgeMeta:
    return salesrep_ownership.OwnershipBridgeMeta(
        available=False,
        source=None,
        rows=0,
        dropped_rows=0,
        overlapping_current_assignments=0,
        bridge_kind=None,
        warnings=(message,),
    )


def _successor_meta_fallback(message: str) -> salesrep_ownership.SuccessorMapMeta:
    return salesrep_ownership.SuccessorMapMeta(
        available=False,
        source=None,
        rows=0,
        dropped_rows=0,
        scoped_rows=0,
        warnings=(message,),
    )


def _salesrep_attribution_context(filters: Any, scope: Dict[str, Any], args: Any) -> Dict[str, Any]:
    cols = fact_store.list_columns()
    cols_map = _required_columns(cols)
    missing = [k for k in ("date", "revenue", "order", "customer") if not cols_map.get(k)]
    if missing:
        return {
            "error": {
                "message": f"Required columns missing for salesreps attribution: {', '.join(missing)}"
            }
        }

    controls = salesrep_ownership.parse_attribution_controls(args)
    try:
        bridge_meta = salesrep_ownership.register_bridge_view()
        if bridge_meta.available:
            fact_store.get_duckdb_conn().execute(
                f"SELECT 1 FROM {salesrep_ownership.BRIDGE_VIEW_NAME} LIMIT 1"
            ).fetchall()
    except Exception as exc:
        bridge_meta = _ownership_bridge_meta_fallback(
            f"Ownership history bridge could not be registered; current-owner rollups fall back to fact owner fields ({exc})."
        )
    try:
        successor_meta = salesrep_ownership.register_successor_view()
        if successor_meta.available:
            fact_store.get_duckdb_conn().execute(
                f"SELECT 1 FROM {salesrep_ownership.SUCCESSION_VIEW_NAME} LIMIT 1"
            ).fetchall()
    except Exception as exc:
        successor_meta = _successor_meta_fallback(
            f"Sales rep succession map could not be registered; successor overrides are unavailable ({exc})."
        )

    windows = _window_context(filters, cols, scope)
    cte_sql = _attributed_salesrep_ctes(
        cols,
        cols_map,
        windows.get("scope_where_sql") or "1=1",
        controls,
        windows,
        bridge_available=bool(bridge_meta.available),
        successor_available=bool(successor_meta.available),
    )
    if not cte_sql:
        return {"error": {"message": "Salesreps attribution query could not be built"}}

    return {
        "cols": cols,
        "cols_map": cols_map,
        "controls": controls,
        "bridge_meta": bridge_meta,
        "successor_meta": successor_meta,
        "windows": windows,
        "cte_sql": cte_sql,
        "params": list(windows.get("scope_params") or []),
    }


def _salesrep_rep_context(rep_id: str, filters: Any, scope: Dict[str, Any], args: Any) -> Dict[str, Any]:
    context = _salesrep_attribution_context(filters, scope, args)
    if context.get("error"):
        return context

    cte_sql = f"""
        {context["cte_sql"]},
        rep_scope AS (
            SELECT *
            FROM attributed_base
            WHERE LOWER(COALESCE(rep_key, '')) = LOWER(?)
               OR LOWER(COALESCE(rep_name, '')) = LOWER(?)
        ),
        scoped AS (
            SELECT *
            FROM rep_scope
            WHERE is_current_window = 1
        )
    """
    params = list(context.get("params") or []) + [rep_id, rep_id]
    context = dict(context)
    context["cte_sql"] = cte_sql
    context["params"] = params
    context["rep_id"] = rep_id
    return context


def _rep_exprs(cols: set[str]) -> Tuple[str, str]:
    rep_id_cols = _present_cols(cols, REP_ID_CANDIDATES)
    rep_name_cols = _present_cols(cols, REP_NAME_CANDIDATES)
    rep_id_exprs = [_string_expr(c) for c in rep_id_cols]
    rep_name_exprs = [_string_expr(c) for c in rep_name_cols]
    default = "'Unassigned'"
    rep_key_expr = _coalesce_exprs(rep_id_exprs + rep_name_exprs, default)
    rep_name_expr = _coalesce_exprs(rep_name_exprs + rep_id_exprs, rep_key_expr)
    return rep_key_expr, rep_name_expr


def _scoped_sql(cols_map: Dict[str, str | None], where_sql: str, rep_key_expr: str, rep_name_expr: str) -> str:
    date_col = cols_map.get("date")
    revenue_col = cols_map.get("revenue")
    order_col = cols_map.get("order")
    customer_col = cols_map.get("customer")
    if not all([date_col, revenue_col, order_col, customer_col]):
        return ""
    customer_name = cols_map.get("customer_name")
    product_col = cols_map.get("product")
    product_name = cols_map.get("product_name")
    cost_expr = cols_map.get("cost_expr") or "NULL"
    qty_expr = cols_map.get("qty_expr") or "0"
    weight_expr = cols_map.get("weight_expr") or "0"
    missing_packs_col = cols_map.get("missing_packs")
    missing_packs_expr = f"CAST({_quote(missing_packs_col)} AS BOOLEAN)" if missing_packs_col else "NULL::BOOLEAN"
    phone_expr = cols_map.get("phone_expr") or "NULL::VARCHAR"
    email_expr = cols_map.get("email_expr") or "NULL::VARCHAR"

    order_expr = _string_expr(order_col)
    customer_expr = _string_expr(customer_col)
    customer_name_expr = _string_expr(customer_name) if customer_name else customer_expr
    product_expr = _string_expr(product_col) if product_col else "NULL::VARCHAR"
    product_name_expr = _string_expr(product_name) if product_name else product_expr
    territory_id_col = cols_map.get("territory_id")
    territory_name_col = cols_map.get("territory_name")
    territory_id_expr = _string_expr(territory_id_col) if territory_id_col else "NULL::VARCHAR"
    territory_name_expr = _string_expr(territory_name_col) if territory_name_col else territory_id_expr

    return f"""
        SELECT
            {rep_key_expr} AS rep_key,
            {rep_name_expr} AS rep_name,
            CAST({_quote(date_col)} AS DATE) AS order_date,
            {order_expr} AS order_id,
            {customer_expr} AS customer_id,
            {customer_name_expr} AS customer_name,
            {product_expr} AS product_id,
            {product_name_expr} AS product_name,
            {territory_id_expr} AS territory_id,
            {territory_name_expr} AS territory_name,
            CAST({_quote(revenue_col)} AS DOUBLE) AS revenue,
            CAST({cost_expr} AS DOUBLE) AS cost,
            CAST({qty_expr} AS DOUBLE) AS units,
            CAST({weight_expr} AS DOUBLE) AS weight_lb,
            {missing_packs_expr} AS missing_packs,
            {phone_expr} AS customer_phone,
            {email_expr} AS customer_email
        FROM fact
        WHERE {where_sql}
    """


def _rollup_sql(cte_sql: str) -> str:
    return f"""
        WITH
        {cte_sql},
        rep_totals AS (
            SELECT
                rep_key,
                ANY_VALUE(rep_name) AS rep_name,
                SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END) AS revenue,
                SUM(CASE WHEN is_current_window = 1 THEN cost END) AS cost,
                SUM(CASE WHEN is_current_window = 1 THEN profit END) AS profit,
                SUM(CASE WHEN is_current_window = 1 AND is_margin_leakage = 1 THEN revenue ELSE 0 END) AS leakage_revenue,
                COUNT(CASE WHEN is_current_window = 1 AND is_margin_leakage = 1 THEN 1 END) AS leakage_count,
                SUM(CASE WHEN is_current_window = 1 AND revenue > 0 AND minimum_margin_pct_rule IS NOT NULL THEN revenue * minimum_margin_pct_rule ELSE 0 END) AS minimum_margin_revenue_sum,
                SUM(CASE WHEN is_current_window = 1 AND revenue > 0 AND minimum_margin_pct_rule IS NOT NULL THEN revenue ELSE 0 END) AS minimum_margin_revenue_weight,
                SUM(CASE WHEN is_current_window = 1 AND revenue > 0 AND target_margin_pct_rule IS NOT NULL THEN revenue * target_margin_pct_rule ELSE 0 END) AS target_margin_revenue_sum,
                SUM(CASE WHEN is_current_window = 1 AND revenue > 0 AND target_margin_pct_rule IS NOT NULL THEN revenue ELSE 0 END) AS target_margin_revenue_weight,
                SUM(CASE WHEN is_prior_window = 1 THEN revenue ELSE 0 END) AS prior_revenue,
                SUM(CASE WHEN is_prior_window = 1 THEN profit END) AS prior_profit,
                SUM(CASE WHEN is_yoy_window = 1 THEN revenue ELSE 0 END) AS yoy_revenue,
                SUM(CASE WHEN is_yoy_window = 1 THEN profit END) AS yoy_profit,
                COUNT(DISTINCT CASE WHEN is_current_window = 1 THEN order_id END) AS orders,
                COUNT(DISTINCT CASE WHEN is_current_window = 1 THEN customer_id END) AS customers,
                SUM(CASE WHEN is_current_window = 1 THEN units ELSE 0 END) AS units,
                SUM(CASE WHEN is_current_window = 1 THEN weight_lb ELSE 0 END) AS weight_lb,
                SUM(CASE WHEN is_current_window = 1 AND cost IS NULL THEN 1 ELSE 0 END) AS cost_null_rows,
                COUNT(CASE WHEN is_current_window = 1 THEN 1 END) AS row_count,
                MAX(CASE WHEN is_current_window = 1 THEN order_date END) AS last_order_date,
                SUM(CASE WHEN is_current_window = 1 AND ownership_changed = 1 THEN revenue ELSE 0 END) AS transfer_revenue,
                SUM(CASE WHEN is_current_window = 1 AND owner_missing = 1 THEN revenue ELSE 0 END) AS unassigned_revenue,
                SUM(CASE WHEN is_current_window = 1 AND current_owner_id = rep_key AND inherited_flag = 1 THEN revenue ELSE 0 END) AS transferred_in_revenue,
                SUM(CASE WHEN is_current_window = 1 AND historical_rep_id = rep_key AND ownership_changed = 1 THEN revenue ELSE 0 END) AS transferred_out_revenue,
                SUM(CASE WHEN is_current_window = 1 AND current_owner_id = rep_key AND inherited_flag = 0 AND owner_missing = 0 THEN revenue ELSE 0 END) AS direct_revenue,
                SUM(CASE WHEN is_current_window = 1 AND current_owner_id = rep_key AND inherited_flag = 0 AND owner_missing = 0 THEN profit END) AS direct_profit,
                SUM(CASE WHEN is_current_window = 1 AND current_owner_id = rep_key AND inherited_flag = 0 AND owner_missing = 0 THEN weight_lb ELSE 0 END) AS direct_weight_lb,
                SUM(CASE WHEN is_current_window = 1 AND current_owner_id = rep_key THEN revenue ELSE 0 END) AS current_owner_revenue,
                SUM(CASE WHEN is_current_window = 1 AND current_owner_id = rep_key THEN profit END) AS current_owner_profit,
                SUM(CASE WHEN is_current_window = 1 AND historical_rep_id = rep_key THEN revenue ELSE 0 END) AS historical_revenue,
                SUM(CASE WHEN is_current_window = 1 AND historical_rep_id = rep_key THEN profit END) AS historical_profit
            FROM attributed_base
            GROUP BY rep_key
        ),
        protein_penetration AS (
            SELECT 
                rep_key,
                LIST(STRUCT_PACK(protein_family := protein_family, customer_count := customer_count)) AS protein_stats
            FROM rep_protein_stats
            GROUP BY 1
        ),
        customer_rollup AS (
            SELECT
                ab.rep_key,
                ab.customer_id,
                ANY_VALUE(ab.customer_name) AS customer_name,
                MAX(ab.current_owner_id) AS current_owner_id,
                MAX(ab.current_owner_name) AS current_owner_name,
                MAX(ab.historical_rep_id) AS historical_rep_id,
                MAX(ab.historical_rep_name) AS historical_rep_name,
                MAX(ab.prior_rep_id) AS prior_rep_id,
                MAX(ab.prior_rep_name) AS prior_rep_name,
                MAX(ab.territory_name) AS territory_name,
                MAX(ab.customer_phone) AS customer_phone,
                MAX(ab.customer_email) AS customer_email,
                MAX(ab.owner_missing) AS owner_missing,
                MAX(ab.inherited_flag) AS inherited_flag,
                LIST(DISTINCT ab.protein_family) FILTER (WHERE ab.protein_family IS NOT NULL AND ab.protein_family <> 'Unassigned') AS historical_proteins,
                SUM(CASE WHEN ab.is_current_window = 1 THEN ab.revenue ELSE 0 END) AS revenue,
                SUM(CASE WHEN ab.is_prior_window = 1 THEN ab.revenue ELSE 0 END) AS prior_revenue,
                SUM(CASE WHEN ab.is_yoy_window = 1 THEN ab.revenue ELSE 0 END) AS yoy_revenue,
                MAX(ab.order_date) AS last_order_date,
                MAX(cp.avg_days_between_orders) AS avg_days_between_orders,
                CASE 
                    WHEN MAX(ab.order_date) IS NOT NULL AND MAX(cp.avg_days_between_orders) > 0
                    THEN (CURRENT_DATE - MAX(ab.order_date)) > (MAX(cp.avg_days_between_orders) * 1.5) + 2
                    ELSE FALSE
                END AS is_overdue
            FROM attributed_base ab
            LEFT JOIN customer_pulse cp ON cp.customer_id = ab.customer_id
            WHERE ab.customer_id IS NOT NULL AND ab.customer_id <> ''
            GROUP BY 1, 2
        ),
        customer_stats AS (
            SELECT
                rep_key,
                COUNT(DISTINCT CASE WHEN revenue > 0 OR prior_revenue > 0 THEN customer_id END) AS active_customers,
                COUNT(DISTINCT CASE WHEN revenue > 0 THEN customer_id END) AS healthy_customers,
                COUNT(DISTINCT CASE WHEN revenue > 0 AND prior_revenue = 0 THEN customer_id END) AS gained_customers,
                COUNT(DISTINCT CASE WHEN revenue = 0 AND prior_revenue > 0 THEN customer_id END) AS lost_customers,
                COUNT(DISTINCT CASE WHEN is_overdue THEN customer_id END) AS overdue_customers,
                COUNT(DISTINCT CASE WHEN revenue > 0 AND current_owner_id = rep_key THEN customer_id END) AS current_owned_customers,
                COUNT(DISTINCT CASE WHEN revenue > 0 AND current_owner_id = rep_key AND inherited_flag = 1 THEN customer_id END) AS inherited_customers,
                COUNT(DISTINCT CASE WHEN revenue > 0 AND owner_missing = 1 THEN customer_id END) AS unassigned_customers
            FROM customer_rollup
            GROUP BY rep_key
        ),
        customer_ranked AS (
            SELECT
                rep_key,
                customer_id,
                customer_name,
                revenue,
                SUM(revenue) OVER (PARTITION BY rep_key) AS rep_total_revenue,
                ROW_NUMBER() OVER (PARTITION BY rep_key ORDER BY revenue DESC, customer_id) AS rn
            FROM customer_rollup
            WHERE revenue > 0
        ),
        concentration AS (
            SELECT
                rep_key,
                MAX(CASE WHEN rn = 1 AND rep_total_revenue > 0 THEN revenue / rep_total_revenue ELSE NULL END) AS top_customer_share,
                SUM(CASE WHEN rn <= 5 AND rep_total_revenue > 0 THEN revenue / rep_total_revenue ELSE 0 END) AS top_5_customer_share,
                SUM(CASE WHEN rep_total_revenue > 0 THEN POWER(revenue / rep_total_revenue, 2) ELSE 0 END) AS hhi,
                MAX(CASE WHEN rn = 1 THEN customer_name END) AS top_customer_name,
                MAX(CASE WHEN rn = 1 THEN revenue END) AS top_customer_revenue
            FROM customer_ranked
            GROUP BY rep_key
        ),
        territory_ranked AS (
            SELECT
                rep_key,
                COALESCE(territory_name, 'Unassigned') AS territory_name,
                SUM(revenue) AS revenue,
                ROW_NUMBER() OVER (
                    PARTITION BY rep_key
                    ORDER BY SUM(revenue) DESC, COALESCE(territory_name, 'Unassigned')
                ) AS rn
            FROM customer_rollup
            WHERE revenue > 0
            GROUP BY 1, 2
        ),
        territory_summary AS (
            SELECT
                rep_key,
                COUNT(DISTINCT territory_name) AS territory_count,
                MAX(CASE WHEN rn = 1 THEN territory_name END) AS top_territory_name,
                MAX(CASE WHEN rn = 1 THEN revenue END) AS top_territory_revenue
            FROM territory_ranked
            GROUP BY rep_key
        ),
        replacement_summary AS (
            SELECT
                rep_key,
                COUNT(DISTINCT COALESCE(prior_rep_id, prior_rep_name, historical_rep_id, historical_rep_name)) AS replaced_rep_count,
                STRING_AGG(
                    DISTINCT COALESCE(prior_rep_name, prior_rep_id, historical_rep_name, historical_rep_id),
                    ', '
                    ORDER BY COALESCE(prior_rep_name, prior_rep_id, historical_rep_name, historical_rep_id)
                ) AS replaced_rep_names
            FROM customer_rollup
            WHERE revenue > 0
              AND inherited_flag = 1
              AND COALESCE(prior_rep_id, prior_rep_name, historical_rep_id, historical_rep_name) IS NOT NULL
            GROUP BY rep_key
        ),
        protein_rollup AS (
            SELECT
                rep_key,
                COALESCE(protein_family, category_name, 'Unassigned') AS protein_family,
                SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END) AS revenue
            FROM attributed_base
            GROUP BY 1, 2
        ),
        top_protein AS (
            SELECT
                rep_key,
                protein_family,
                revenue,
                ROW_NUMBER() OVER (PARTITION BY rep_key ORDER BY revenue DESC, protein_family) AS rn
            FROM protein_rollup
            WHERE revenue > 0
        )
        SELECT
            rt.rep_key,
            rt.rep_name,
            rt.revenue,
            rt.cost,
            rt.profit,
            rt.leakage_revenue,
            rt.leakage_count,
            CASE
                WHEN rt.minimum_margin_revenue_weight > 0
                THEN rt.minimum_margin_revenue_sum / NULLIF(rt.minimum_margin_revenue_weight, 0)
                ELSE NULL
            END AS minimum_margin_pct,
            CASE
                WHEN rt.target_margin_revenue_weight > 0
                THEN rt.target_margin_revenue_sum / NULLIF(rt.target_margin_revenue_weight, 0)
                ELSE NULL
            END AS target_margin_pct,
            rt.prior_revenue,
            rt.prior_profit,
            rt.yoy_revenue,
            rt.yoy_profit,
            CASE WHEN rt.revenue > 0 AND rt.profit IS NOT NULL THEN (rt.profit / rt.revenue) * 100 ELSE NULL END AS margin_pct,
            rt.orders,
            COALESCE(cs.active_customers, 0) AS customers,
            COALESCE(cs.active_customers, 0) AS active_customers,
            COALESCE(cs.healthy_customers, 0) AS healthy_customers,
            COALESCE(cs.gained_customers, 0) AS gained_customers,
            COALESCE(cs.lost_customers, 0) AS lost_customers,
            COALESCE(cs.overdue_customers, 0) AS overdue_customers,
            COALESCE(cs.current_owned_customers, 0) AS current_owned_customers,
            COALESCE(cs.inherited_customers, 0) AS inherited_customers,
            COALESCE(cs.unassigned_customers, 0) AS unassigned_customers,
            rt.units,
            rt.weight_lb,
            CASE WHEN rt.units > 0 THEN rt.revenue / NULLIF(rt.units, 0) ELSE NULL END AS asp,
            CASE WHEN rt.weight_lb > 0 THEN rt.revenue / NULLIF(rt.weight_lb, 0) ELSE NULL END AS asp_lb,
            CASE WHEN rt.orders > 0 THEN rt.revenue / NULLIF(rt.orders, 0) ELSE NULL END AS avg_order_value,
            CASE WHEN COALESCE(cs.active_customers, 0) > 0 THEN rt.revenue / NULLIF(COALESCE(cs.active_customers, 0), 0) ELSE NULL END AS revenue_per_customer,
            rt.last_order_date,
            rt.revenue - rt.prior_revenue AS mom_revenue_delta,
            rt.revenue - rt.yoy_revenue AS yoy_revenue_delta,
            rt.transfer_revenue,
            rt.unassigned_revenue,
            rt.transferred_in_revenue,
            rt.transferred_out_revenue,
            rt.direct_revenue,
            rt.direct_profit,
            rt.direct_weight_lb,
            rt.current_owner_revenue,
            rt.current_owner_profit,
            rt.historical_revenue,
            rt.historical_profit,
            rt.current_owner_profit - rt.historical_profit AS ownership_delta_revenue,
            CASE WHEN rt.historical_revenue > 0 THEN ((rt.current_owner_revenue - rt.historical_revenue) / rt.historical_revenue) * 100 ELSE NULL END AS ownership_delta_pct,
            ts.territory_count,
            ts.top_territory_name,
            ts.top_territory_revenue,
            rs.replaced_rep_count,
            rs.replaced_rep_names,
            tp.protein_family AS top_protein_family,
            tp.revenue AS top_protein_revenue,
            pp.protein_stats AS protein_stats,
            CASE WHEN rt.prior_revenue > 0 THEN ((rt.revenue - rt.prior_revenue) / rt.prior_revenue) * 100 ELSE NULL END AS mom_revenue_pct,
            CASE WHEN rt.yoy_revenue > 0 THEN ((rt.revenue - rt.yoy_revenue) / rt.yoy_revenue) * 100 ELSE NULL END AS yoy_revenue_pct,
            CASE WHEN rt.prior_profit IS NOT NULL AND rt.profit IS NOT NULL AND rt.prior_profit <> 0 THEN ((rt.profit - rt.prior_profit) / ABS(rt.prior_profit)) * 100 ELSE NULL END AS mom_profit_pct,
            conc.top_customer_share AS top_customer_share,
            conc.top_5_customer_share AS top_5_customer_share,
            conc.top_customer_name AS top_customer_name,
            conc.top_customer_revenue AS top_customer_revenue,
            conc.hhi AS customer_hhi,
            CASE WHEN rt.prior_revenue > 0 THEN (rt.revenue - rt.prior_revenue) / rt.prior_revenue * 100 ELSE NULL END AS momentum_pct,
            CASE WHEN rt.prior_revenue > 0 THEN (rt.revenue - rt.prior_revenue) / rt.prior_revenue * 100 ELSE NULL END AS mom_revenue_pct,
            CASE
                WHEN rt.prior_profit IS NULL OR rt.prior_profit = 0 THEN NULL
                ELSE (rt.profit - rt.prior_profit) / ABS(rt.prior_profit) * 100
            END AS mom_profit_pct,
            CASE
                WHEN rt.prior_revenue > 0 AND rt.prior_profit IS NOT NULL AND rt.profit IS NOT NULL
                THEN ((rt.profit / NULLIF(rt.revenue, 0)) * 100) - ((rt.prior_profit / NULLIF(rt.prior_revenue, 0)) * 100)
                ELSE NULL
            END AS mom_margin_pct,
            CASE WHEN rt.yoy_revenue > 0 THEN (rt.revenue - rt.yoy_revenue) / rt.yoy_revenue * 100 ELSE NULL END AS yoy_revenue_pct,
            CASE
                WHEN rt.yoy_profit IS NULL OR rt.yoy_profit = 0 THEN NULL
                ELSE (rt.profit - rt.yoy_profit) / ABS(rt.yoy_profit) * 100
            END AS yoy_profit_pct,
            CASE
                WHEN rt.yoy_revenue > 0 AND rt.yoy_profit IS NOT NULL AND rt.profit IS NOT NULL
                THEN ((rt.profit / NULLIF(rt.revenue, 0)) * 100) - ((rt.yoy_profit / NULLIF(rt.yoy_revenue, 0)) * 100)
                ELSE NULL
            END AS yoy_margin_delta,
            COALESCE(cs.current_owned_customers, 0) AS current_owned_customers,
            COALESCE(cs.inherited_customers, 0) AS inherited_customers,
            COALESCE(cs.gained_customers, 0) AS gained_customers,
            COALESCE(cs.lost_customers, 0) AS lost_customers,
            COALESCE(ts.territory_count, 0) AS territory_count,
            ts.top_territory_name,
            ts.top_territory_revenue,
            COALESCE(cs.unassigned_customers, 0) AS unassigned_customers,
            COALESCE(rs.replaced_rep_count, 0) AS replaced_rep_count,
            rs.replaced_rep_names,
            rt.transferred_in_revenue,
            rt.transferred_out_revenue,
            rt.transfer_revenue,
            rt.unassigned_revenue,
            rt.direct_revenue,
            rt.direct_profit,
            rt.direct_weight_lb,
            rt.current_owner_revenue,
            rt.current_owner_profit,
            rt.historical_revenue,
            rt.historical_profit,
            (rt.current_owner_revenue - rt.historical_revenue) AS ownership_delta_revenue,
            CASE
                WHEN rt.historical_revenue = 0 THEN NULL
                ELSE (rt.current_owner_revenue - rt.historical_revenue) / ABS(rt.historical_revenue) * 100
            END AS ownership_delta_pct,
            rt.cost_null_rows,
            rt.row_count,
            tp.protein_family AS top_protein_family,
            tp.revenue AS top_protein_revenue
        FROM rep_totals rt
        LEFT JOIN concentration conc ON conc.rep_key = rt.rep_key
        LEFT JOIN customer_stats cs ON cs.rep_key = rt.rep_key
        LEFT JOIN territory_summary ts ON ts.rep_key = rt.rep_key
        LEFT JOIN replacement_summary rs ON rs.rep_key = rt.rep_key
        LEFT JOIN top_protein tp ON tp.rep_key = rt.rep_key AND tp.rn = 1
        LEFT JOIN protein_penetration pp ON pp.rep_key = rt.rep_key
        WHERE COALESCE(rt.revenue, 0) <> 0
           OR COALESCE(rt.current_owner_revenue, 0) <> 0
           OR COALESCE(rt.historical_revenue, 0) <> 0
    """


def _kpis_sql(cte_sql: str) -> str:
    return f"""
        WITH
        {cte_sql},
        customer_rollup AS (
            SELECT
                ab.customer_id,
                MAX(ab.owner_missing) AS owner_missing,
                MAX(ab.inherited_flag) AS inherited_flag,
                SUM(CASE WHEN ab.is_current_window = 1 THEN ab.revenue ELSE 0 END) AS revenue,
                CASE 
                    WHEN MAX(ab.order_date) IS NOT NULL AND MAX(cp.avg_days_between_orders) > 0
                    THEN (CURRENT_DATE - MAX(ab.order_date)) > (MAX(cp.avg_days_between_orders) * 1.5) + 2
                    ELSE FALSE
                END AS is_overdue
            FROM attributed_base ab
            LEFT JOIN customer_pulse cp ON cp.customer_id = ab.customer_id
            WHERE ab.customer_id IS NOT NULL AND ab.customer_id <> ''
            GROUP BY 1
        )
        SELECT
            SUM(CASE WHEN ab.is_current_window = 1 THEN ab.revenue ELSE 0 END) AS revenue,
            SUM(CASE WHEN ab.is_current_window = 1 THEN ab.cost END) AS cost,
            SUM(CASE WHEN ab.is_current_window = 1 THEN ab.profit END) AS profit,
            SUM(CASE WHEN ab.is_current_window = 1 AND ab.is_margin_leakage = 1 THEN ab.revenue ELSE 0 END) AS leakage_revenue,
            COUNT(DISTINCT CASE WHEN cr.is_overdue THEN cr.customer_id END) AS overdue_customers,
            CASE
                WHEN SUM(CASE WHEN ab.is_current_window = 1 AND ab.revenue > 0 AND ab.minimum_margin_pct_rule IS NOT NULL THEN ab.revenue ELSE 0 END) > 0
                THEN SUM(CASE WHEN ab.is_current_window = 1 AND ab.revenue > 0 AND ab.minimum_margin_pct_rule IS NOT NULL THEN ab.revenue * ab.minimum_margin_pct_rule ELSE 0 END)
                    / NULLIF(SUM(CASE WHEN ab.is_current_window = 1 AND ab.revenue > 0 AND ab.minimum_margin_pct_rule IS NOT NULL THEN ab.revenue ELSE 0 END), 0)
                ELSE NULL
            END AS minimum_margin_pct,
            CASE
                WHEN SUM(CASE WHEN ab.is_current_window = 1 AND ab.revenue > 0 AND ab.target_margin_pct_rule IS NOT NULL THEN ab.revenue ELSE 0 END) > 0
                THEN SUM(CASE WHEN ab.is_current_window = 1 AND ab.revenue > 0 AND ab.target_margin_pct_rule IS NOT NULL THEN ab.revenue * ab.target_margin_pct_rule ELSE 0 END)
                    / NULLIF(SUM(CASE WHEN ab.is_current_window = 1 AND ab.revenue > 0 AND ab.target_margin_pct_rule IS NOT NULL THEN ab.revenue ELSE 0 END), 0)
                ELSE NULL
            END AS target_margin_pct,
            CASE
                WHEN SUM(CASE WHEN ab.is_current_window = 1 THEN ab.revenue ELSE 0 END) > 0
                THEN SUM(CASE WHEN ab.is_current_window = 1 THEN ab.profit END) / NULLIF(SUM(CASE WHEN ab.is_current_window = 1 THEN ab.revenue ELSE 0 END), 0) * 100
                ELSE NULL
            END AS margin_pct,
            COUNT(DISTINCT CASE WHEN ab.is_current_window = 1 THEN ab.order_id END) AS orders,
            COUNT(DISTINCT CASE WHEN ab.is_current_window = 1 THEN ab.customer_id END) AS customers,
            COUNT(DISTINCT CASE WHEN ab.is_current_window = 1 THEN ab.rep_key END) AS active_reps,
            COUNT(DISTINCT CASE WHEN ab.is_current_window = 1 AND ab.owner_source = 'ownership_bridge' THEN ab.customer_id END) AS bridge_customers,
            COUNT(DISTINCT CASE WHEN ab.is_current_window = 1 AND ab.owner_source = 'fact_customer_snapshot' THEN ab.customer_id END) AS snapshot_customers,
            COUNT(DISTINCT CASE WHEN ab.is_current_window = 1 AND ab.owner_source = 'fact_current_owner' THEN ab.customer_id END) AS fact_fallback_customers,
            SUM(CASE WHEN ab.is_current_window = 1 THEN ab.units ELSE 0 END) AS units,
            SUM(CASE WHEN ab.is_current_window = 1 THEN ab.weight_lb ELSE 0 END) AS weight_lb,
            CASE
                WHEN COUNT(DISTINCT CASE WHEN ab.is_current_window = 1 THEN ab.order_id END) > 0
                THEN SUM(CASE WHEN ab.is_current_window = 1 THEN ab.revenue ELSE 0 END) / NULLIF(COUNT(DISTINCT CASE WHEN ab.is_current_window = 1 THEN ab.order_id END), 0)
                ELSE NULL
            END AS avg_order_value,
            CASE
                WHEN COUNT(DISTINCT CASE WHEN ab.is_current_window = 1 THEN ab.customer_id END) > 0
                THEN SUM(CASE WHEN ab.is_current_window = 1 THEN ab.revenue ELSE 0 END) / NULLIF(COUNT(DISTINCT CASE WHEN ab.is_current_window = 1 THEN ab.customer_id END), 0)
                ELSE NULL
            END AS revenue_per_customer,
            CASE WHEN SUM(CASE WHEN ab.is_current_window = 1 THEN ab.units ELSE 0 END) > 0 THEN SUM(CASE WHEN ab.is_current_window = 1 THEN ab.revenue ELSE 0 END) / NULLIF(SUM(CASE WHEN ab.is_current_window = 1 THEN ab.units ELSE 0 END), 0) ELSE NULL END AS asp,
            CASE WHEN SUM(CASE WHEN ab.is_current_window = 1 THEN ab.weight_lb ELSE 0 END) > 0 THEN SUM(CASE WHEN ab.is_current_window = 1 THEN ab.revenue ELSE 0 END) / NULLIF(SUM(CASE WHEN ab.is_current_window = 1 THEN ab.weight_lb ELSE 0 END), 0) ELSE NULL END AS asp_lb,
            SUM(CASE WHEN ab.is_current_window = 1 AND ab.cost IS NULL THEN 1 ELSE 0 END) AS cost_null_rows,
            SUM(CASE WHEN ab.is_current_window = 1 AND COALESCE(ab.missing_packs, FALSE) THEN 1 ELSE 0 END) AS missing_packs_rows,
            COUNT(CASE WHEN ab.is_current_window = 1 THEN 1 END) AS total_rows,
            MIN(CASE WHEN ab.is_current_window = 1 THEN ab.order_date END) AS date_min,
            MAX(CASE WHEN ab.is_current_window = 1 THEN ab.order_date END) AS date_max,
            SUM(CASE WHEN ab.is_prior_window = 1 THEN ab.revenue ELSE 0 END) AS prior_revenue,
            SUM(CASE WHEN ab.is_prior_window = 1 THEN ab.profit END) AS prior_profit,
            SUM(CASE WHEN ab.is_yoy_window = 1 THEN ab.revenue ELSE 0 END) AS yoy_revenue,
            SUM(CASE WHEN ab.is_yoy_window = 1 THEN ab.profit END) AS yoy_profit,
            CASE
                WHEN SUM(CASE WHEN ab.is_prior_window = 1 THEN ab.revenue ELSE 0 END) > 0
                THEN (
                    SUM(CASE WHEN ab.is_current_window = 1 THEN ab.revenue ELSE 0 END)
                    - SUM(CASE WHEN ab.is_prior_window = 1 THEN ab.revenue ELSE 0 END)
                ) / NULLIF(SUM(CASE WHEN ab.is_prior_window = 1 THEN ab.revenue ELSE 0 END), 0) * 100
                ELSE NULL
            END AS revenue_mom_pct,
            CASE
                WHEN SUM(CASE WHEN ab.is_prior_window = 1 THEN ab.profit END) IS NULL OR SUM(CASE WHEN ab.is_prior_window = 1 THEN ab.profit END) = 0
                THEN NULL
                ELSE (
                    SUM(CASE WHEN ab.is_current_window = 1 THEN ab.profit END)
                    - SUM(CASE WHEN ab.is_prior_window = 1 THEN ab.profit END)
                ) / ABS(SUM(CASE WHEN ab.is_prior_window = 1 THEN ab.profit END)) * 100
            END AS profit_mom_pct,
            CASE
                WHEN SUM(CASE WHEN ab.is_prior_window = 1 THEN ab.revenue ELSE 0 END) > 0
                     AND SUM(CASE WHEN ab.is_prior_window = 1 THEN ab.profit END) IS NOT NULL
                     AND SUM(CASE WHEN ab.is_current_window = 1 THEN ab.profit END) IS NOT NULL
                THEN (
                    (SUM(CASE WHEN ab.is_current_window = 1 THEN ab.profit END) / NULLIF(SUM(CASE WHEN ab.is_current_window = 1 THEN ab.revenue ELSE 0 END), 0)) * 100
                    - (SUM(CASE WHEN ab.is_prior_window = 1 THEN ab.profit END) / NULLIF(SUM(CASE WHEN ab.is_prior_window = 1 THEN ab.revenue ELSE 0 END), 0)) * 100
                )
                ELSE NULL
            END AS margin_mom_pct,
            CASE
                WHEN SUM(CASE WHEN ab.is_yoy_window = 1 THEN ab.revenue ELSE 0 END) > 0
                THEN (
                    SUM(CASE WHEN ab.is_current_window = 1 THEN ab.revenue ELSE 0 END)
                    - SUM(CASE WHEN ab.is_yoy_window = 1 THEN ab.revenue ELSE 0 END)
                ) / NULLIF(SUM(CASE WHEN ab.is_yoy_window = 1 THEN ab.revenue ELSE 0 END), 0) * 100
                ELSE NULL
            END AS revenue_yoy_pct,
            CASE
                WHEN SUM(CASE WHEN ab.is_yoy_window = 1 THEN ab.profit END) IS NULL OR SUM(CASE WHEN ab.is_yoy_window = 1 THEN ab.profit END) = 0
                THEN NULL
                ELSE (
                    SUM(CASE WHEN ab.is_current_window = 1 THEN ab.profit END)
                    - SUM(CASE WHEN ab.is_yoy_window = 1 THEN ab.profit END)
                ) / ABS(SUM(CASE WHEN ab.is_yoy_window = 1 THEN ab.profit END)) * 100
            END AS profit_yoy_pct,
            CASE
                WHEN SUM(CASE WHEN ab.is_yoy_window = 1 THEN ab.revenue ELSE 0 END) > 0
                     AND SUM(CASE WHEN ab.is_yoy_window = 1 THEN ab.profit END) IS NOT NULL
                     AND SUM(CASE WHEN ab.is_current_window = 1 THEN ab.profit END) IS NOT NULL
                THEN (
                    (SUM(CASE WHEN ab.is_current_window = 1 THEN ab.profit END) / NULLIF(SUM(CASE WHEN ab.is_current_window = 1 THEN ab.revenue ELSE 0 END), 0)) * 100
                    - (SUM(CASE WHEN ab.is_yoy_window = 1 THEN ab.profit END) / NULLIF(SUM(CASE WHEN ab.is_yoy_window = 1 THEN ab.revenue ELSE 0 END), 0)) * 100
                )
                ELSE NULL
            END AS margin_yoy_delta,
            COUNT(DISTINCT CASE WHEN cr.revenue > 0 THEN cr.customer_id END) AS active_customers,
            COUNT(DISTINCT CASE WHEN cr.revenue > 0 AND cr.inherited_flag = 0 AND cr.owner_missing = 0 THEN cr.customer_id END) AS direct_customers,
            COUNT(DISTINCT CASE WHEN cr.revenue > 0 AND cr.inherited_flag = 1 THEN cr.customer_id END) AS inherited_customers,
            COUNT(DISTINCT CASE WHEN cr.revenue > 0 AND cr.inherited_flag = 1 THEN cr.customer_id END) AS transferred_in_customers,
            COUNT(DISTINCT CASE WHEN cr.revenue > 0 AND cr.owner_missing = 0 AND cr.inherited_flag = 0 THEN cr.customer_id END) AS current_direct_customers,
            COUNT(DISTINCT CASE WHEN cr.revenue > 0 AND cr.owner_missing = 1 THEN cr.customer_id END) AS unassigned_customers,
            SUM(CASE WHEN ab.is_current_window = 1 AND cr.inherited_flag = 0 AND cr.owner_missing = 0 THEN ab.revenue ELSE 0 END) AS direct_revenue,
            SUM(CASE WHEN ab.is_current_window = 1 AND cr.inherited_flag = 1 THEN ab.revenue ELSE 0 END) AS inherited_revenue,
            SUM(CASE WHEN ab.is_current_window = 1 AND cr.owner_missing = 1 THEN ab.revenue ELSE 0 END) AS unassigned_revenue
        FROM attributed_base ab
        LEFT JOIN customer_rollup cr USING (customer_id)
    """


def _trend_sql(cte_sql: str, top_n: int) -> str:
    return f"""
        WITH
        {cte_sql},
        rep_totals AS (
            SELECT
                rep_key,
                ANY_VALUE(rep_name) AS rep_name,
                SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END) AS revenue,
                SUM(CASE WHEN is_current_window = 1 THEN profit END) AS profit,
                SUM(CASE WHEN is_current_window = 1 THEN weight_lb ELSE 0 END) AS weight_lb,
                COUNT(DISTINCT CASE WHEN is_current_window = 1 THEN customer_id END) AS customers,
                CASE
                    WHEN SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END) > 0
                         AND SUM(CASE WHEN is_current_window = 1 THEN profit END) IS NOT NULL
                    THEN SUM(CASE WHEN is_current_window = 1 THEN profit END)
                        / NULLIF(SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END), 0) * 100
                    ELSE NULL
                END AS margin_pct
            FROM attributed_base
            GROUP BY rep_key
        ),
        rep_candidates AS (
            SELECT rep_key
            FROM (
                SELECT
                    rep_key,
                    ROW_NUMBER() OVER (ORDER BY revenue DESC, rep_key) AS revenue_rank,
                    ROW_NUMBER() OVER (ORDER BY COALESCE(profit, -1000000000000.0) DESC, rep_key) AS profit_rank,
                    ROW_NUMBER() OVER (ORDER BY weight_lb DESC, rep_key) AS weight_rank,
                    ROW_NUMBER() OVER (ORDER BY customers DESC, rep_key) AS customer_rank,
                    ROW_NUMBER() OVER (ORDER BY COALESCE(margin_pct, -1000000000000.0) DESC, rep_key) AS margin_rank
                FROM rep_totals
            ) ranked
            WHERE revenue_rank <= {top_n}
               OR profit_rank <= {top_n}
               OR weight_rank <= {top_n}
               OR customer_rank <= {top_n}
               OR margin_rank <= {top_n}
        ),
        trend_rows AS (
            SELECT
                DATE_TRUNC('month', order_date) AS aligned_month,
                rep_key,
                rep_name,
                CAST(revenue AS DOUBLE) AS revenue_current,
                NULL::DOUBLE AS revenue_yoy,
                CAST(profit AS DOUBLE) AS profit_current,
                NULL::DOUBLE AS profit_yoy,
                CAST(weight_lb AS DOUBLE) AS weight_current,
                NULL::DOUBLE AS weight_yoy,
                customer_id AS current_customer_id,
                NULL::VARCHAR AS yoy_customer_id,
                CASE WHEN inherited_flag = 1 THEN CAST(revenue AS DOUBLE) ELSE NULL::DOUBLE END AS inherited_revenue_current,
                CASE WHEN inherited_flag = 0 AND owner_missing = 0 THEN CAST(revenue AS DOUBLE) ELSE NULL::DOUBLE END AS direct_revenue_current,
                CASE WHEN inherited_flag = 1 THEN customer_id ELSE NULL::VARCHAR END AS inherited_customer_id_current,
                CASE WHEN inherited_flag = 0 AND owner_missing = 0 THEN customer_id ELSE NULL::VARCHAR END AS direct_customer_id_current,
                order_date AS current_order_date,
                NULL::DATE AS yoy_order_date
            FROM attributed_base
            WHERE is_current_window = 1
            UNION ALL
            SELECT
                DATE_TRUNC('month', order_date + INTERVAL 1 YEAR) AS aligned_month,
                rep_key,
                rep_name,
                NULL::DOUBLE AS revenue_current,
                CAST(revenue AS DOUBLE) AS revenue_yoy,
                NULL::DOUBLE AS profit_current,
                CAST(profit AS DOUBLE) AS profit_yoy,
                NULL::DOUBLE AS weight_current,
                CAST(weight_lb AS DOUBLE) AS weight_yoy,
                NULL::VARCHAR AS current_customer_id,
                customer_id AS yoy_customer_id,
                NULL::DOUBLE AS inherited_revenue_current,
                NULL::DOUBLE AS direct_revenue_current,
                NULL::VARCHAR AS inherited_customer_id_current,
                NULL::VARCHAR AS direct_customer_id_current,
                NULL::DATE AS current_order_date,
                order_date AS yoy_order_date
            FROM attributed_base
            WHERE is_yoy_window = 1
        ),
        rep_trend AS (
            SELECT
                aligned_month,
                rep_key,
                ANY_VALUE(rep_name) AS rep_name,
                SUM(revenue_current) AS revenue,
                SUM(revenue_yoy) AS revenue_yoy,
                SUM(profit_current) AS profit,
                SUM(profit_yoy) AS profit_yoy,
                SUM(weight_current) AS weight_lb,
                SUM(weight_yoy) AS weight_lb_yoy,
                COUNT(DISTINCT current_customer_id) AS customers,
                COUNT(DISTINCT yoy_customer_id) AS customers_yoy,
                SUM(inherited_revenue_current) AS inherited_revenue,
                SUM(direct_revenue_current) AS direct_revenue,
                COUNT(DISTINCT inherited_customer_id_current) AS inherited_customers,
                COUNT(DISTINCT direct_customer_id_current) AS direct_customers,
                COUNT(DISTINCT current_order_date) AS observed_days,
                COUNT(DISTINCT yoy_order_date) AS observed_days_yoy
            FROM trend_rows
            WHERE rep_key IN (SELECT rep_key FROM rep_candidates)
            GROUP BY 1, 2
        ),
        monthly_compare AS (
            SELECT
                aligned_month,
                SUM(revenue_current) AS revenue,
                SUM(revenue_yoy) AS revenue_yoy,
                SUM(profit_current) AS profit,
                SUM(profit_yoy) AS profit_yoy,
                SUM(weight_current) AS weight_lb,
                SUM(weight_yoy) AS weight_lb_yoy,
                COUNT(DISTINCT current_customer_id) AS customers,
                COUNT(DISTINCT yoy_customer_id) AS customers_yoy,
                SUM(inherited_revenue_current) AS inherited_revenue,
                SUM(direct_revenue_current) AS direct_revenue,
                COUNT(DISTINCT current_order_date) AS observed_days,
                COUNT(DISTINCT yoy_order_date) AS observed_days_yoy
            FROM trend_rows
            GROUP BY 1
        )
        SELECT
            'rep_trend' AS dataset,
            strftime('%Y-%m', aligned_month) AS bucket,
            rep_key,
            rep_name,
            revenue,
            revenue_yoy AS comparison_value,
            profit,
            profit_yoy AS comparison_profit,
            weight_lb,
            weight_lb_yoy AS comparison_weight_lb,
            direct_revenue AS direct_value,
            inherited_revenue AS inherited_value,
            CAST(customers AS DOUBLE) AS customers_value,
            CAST(customers_yoy AS DOUBLE) AS comparison_customers_value,
            CAST(direct_customers AS DOUBLE) AS direct_customers_value,
            CAST(inherited_customers AS DOUBLE) AS inherited_customers_value,
            CAST(observed_days AS DOUBLE) AS observed_days_value,
            CAST(observed_days_yoy AS DOUBLE) AS comparison_observed_days_value
        FROM rep_trend
        UNION ALL
        SELECT
            'monthly_compare' AS dataset,
            strftime('%Y-%m', aligned_month) AS bucket,
            NULL::VARCHAR AS rep_key,
            NULL::VARCHAR AS rep_name,
            revenue,
            revenue_yoy AS comparison_value,
            profit,
            profit_yoy AS comparison_profit,
            weight_lb,
            weight_lb_yoy AS comparison_weight_lb,
            direct_revenue AS direct_value,
            inherited_revenue AS inherited_value,
            CAST(customers AS DOUBLE) AS customers_value,
            CAST(customers_yoy AS DOUBLE) AS comparison_customers_value,
            NULL::DOUBLE AS direct_customers_value,
            NULL::DOUBLE AS inherited_customers_value,
            CAST(observed_days AS DOUBLE) AS observed_days_value,
            CAST(observed_days_yoy AS DOUBLE) AS comparison_observed_days_value
        FROM monthly_compare
        ORDER BY dataset, bucket, rep_key
    """


def _analysis_sql(cte_sql: str) -> str:
    return f"""
        WITH
        {cte_sql},
        customer_scope AS (
            SELECT
                ab.customer_id,
                ANY_VALUE(customer_name) AS customer_name,
                ANY_VALUE(current_owner_id) AS current_owner_id,
                ANY_VALUE(current_owner_name) AS current_owner_name,
                ANY_VALUE(prior_rep_id) AS prior_rep_id,
                ANY_VALUE(prior_rep_name) AS prior_rep_name,
                ANY_VALUE(last_sales_rep_id) AS last_sales_rep_id,
                ANY_VALUE(last_sales_rep_name) AS last_sales_rep_name,
                ANY_VALUE(territory_name) AS territory_name,
                ANY_VALUE(owner_source) AS owner_source,
                ANY_VALUE(mapping_confidence) AS mapping_confidence,
                MAX(current_owner_active) AS current_owner_active,
                MAX(owner_missing) AS owner_missing,
                MAX(ownership_changed) AS ownership_changed,
                MAX(inherited_flag) AS inherited_flag,
                MAX(
                    CASE
                        WHEN dq_status IS NULL OR dq_status = '' THEN CASE WHEN owner_missing = 1 THEN 'needs_review' ELSE 'ok' END
                        ELSE dq_status
                    END
                ) AS dq_status,
                SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END) AS revenue,
                SUM(CASE WHEN is_prior_window = 1 THEN revenue ELSE 0 END) AS prior_revenue,
                SUM(CASE WHEN is_yoy_window = 1 THEN revenue ELSE 0 END) AS yoy_revenue,
                SUM(
                    CASE
                        WHEN is_current_window = 1
                             AND LOWER(COALESCE(protein_family, category_name, '')) LIKE '%beef%'
                        THEN revenue
                        ELSE 0
                    END
                ) AS beef_revenue,
                SUM(
                    CASE
                        WHEN is_current_window = 1
                             AND (
                                 LOWER(COALESCE(protein_family, category_name, '')) LIKE '%poultry%'
                                 OR LOWER(COALESCE(protein_family, category_name, '')) LIKE '%chicken%'
                                 OR LOWER(COALESCE(protein_family, category_name, '')) LIKE '%turkey%'
                             )
                        THEN revenue
                        ELSE 0
                    END
                ) AS poultry_revenue,
                SUM(
                    CASE
                        WHEN is_current_window = 1
                             AND (
                                 LOWER(COALESCE(protein_family, category_name, '')) LIKE '%pork%'
                                 OR LOWER(COALESCE(protein_family, category_name, '')) LIKE '%ham%'
                                 OR LOWER(COALESCE(protein_family, category_name, '')) LIKE '%bacon%'
                             )
                        THEN revenue
                        ELSE 0
                    END
                ) AS pork_revenue,
                SUM(CASE WHEN ab.is_current_window = 1 THEN ab.profit END) AS profit,
                SUM(CASE WHEN ab.is_yoy_window = 1 THEN ab.profit END) AS yoy_profit,
                COUNT(DISTINCT CASE WHEN ab.is_current_window = 1 THEN ab.order_id END) AS orders,
                MAX(ab.order_date) AS last_order_date,
                MAX(ab.DeliveryLat) AS delivery_lat,
                MAX(ab.DeliveryLong) AS delivery_lng,
                MAX(cp.avg_days_between_orders) AS avg_days_between_orders,
                CASE 
                    WHEN MAX(ab.order_date) IS NOT NULL AND MAX(cp.avg_days_between_orders) > 0
                    THEN (CURRENT_DATE - MAX(ab.order_date)) > (MAX(cp.avg_days_between_orders) * 1.5) + 2
                    ELSE FALSE
                END AS is_overdue,
                -- 🎯 God Level Opportunity Score: (Protein Gap + Momentum Loss) * Account Size
                (
                    (CASE WHEN SUM(CASE WHEN ab.is_current_window = 1 AND LOWER(COALESCE(protein_family, category_name, '')) LIKE '%beef%' THEN revenue ELSE 0 END) = 0 AND SUM(CASE WHEN is_prior_window = 1 THEN revenue ELSE 0 END) > 0 THEN 20 ELSE 0 END) +
                    (CASE WHEN SUM(CASE WHEN ab.is_current_window = 1 AND (LOWER(COALESCE(protein_family, category_name, '')) LIKE '%poultry%' OR LOWER(COALESCE(protein_family, category_name, '')) LIKE '%chicken%' OR LOWER(COALESCE(protein_family, category_name, '')) LIKE '%turkey%') THEN revenue ELSE 0 END) = 0 AND SUM(CASE WHEN is_prior_window = 1 THEN revenue ELSE 0 END) > 0 THEN 15 ELSE 0 END) +
                    (CASE WHEN SUM(CASE WHEN ab.is_current_window = 1 AND (LOWER(COALESCE(protein_family, category_name, '')) LIKE '%pork%' OR LOWER(COALESCE(protein_family, category_name, '')) LIKE '%ham%' OR LOWER(COALESCE(protein_family, category_name, '')) LIKE '%bacon%') THEN revenue ELSE 0 END) = 0 AND SUM(CASE WHEN is_prior_window = 1 THEN revenue ELSE 0 END) > 0 THEN 15 ELSE 0 END) +
                    (CASE WHEN SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END) < SUM(CASE WHEN is_prior_window = 1 THEN revenue ELSE 0 END) THEN 10 ELSE 0 END)
                ) * (LOG(NULLIF(SUM(CASE WHEN is_prior_window = 1 THEN revenue ELSE 0 END), 0)) + 1) AS opportunity_score,
                LIST(DISTINCT ab.protein_family) FILTER (WHERE ab.protein_family IS NOT NULL AND ab.protein_family <> 'Unassigned') AS historical_proteins
            FROM attributed_base ab
            LEFT JOIN customer_pulse cp ON cp.customer_id = ab.customer_id
            WHERE ab.customer_id IS NOT NULL AND ab.customer_id <> ''
            GROUP BY 1
        ),
        top_customers AS (
            SELECT
                *,
                ROW_NUMBER() OVER (ORDER BY revenue DESC, customer_id) AS rn
            FROM customer_scope
            WHERE revenue > 0
        ),
        customer_movers_pos AS (
            SELECT
                *,
                ROW_NUMBER() OVER (ORDER BY (revenue - prior_revenue) DESC, customer_id) AS rn
            FROM customer_scope
            WHERE revenue <> 0 OR prior_revenue <> 0
        ),
        customer_movers_neg AS (
            SELECT
                *,
                ROW_NUMBER() OVER (ORDER BY (revenue - prior_revenue) ASC, customer_id) AS rn
            FROM customer_scope
            WHERE revenue <> 0 OR prior_revenue <> 0
        ),
        protein_scope AS (
            SELECT
                COALESCE(protein_family, category_name, 'Unassigned') AS protein_family,
                SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END) AS revenue,
                SUM(CASE WHEN is_current_window = 1 THEN profit END) AS profit,
                SUM(CASE WHEN is_yoy_window = 1 THEN revenue ELSE 0 END) AS yoy_revenue,
                SUM(CASE WHEN is_yoy_window = 1 THEN profit END) AS yoy_profit,
                SUM(CASE WHEN is_current_window = 1 THEN weight_lb ELSE 0 END) AS weight_lb
            FROM attributed_base
            GROUP BY 1
        ),
        protein_ranked AS (
            SELECT
                *,
                CASE
                    WHEN revenue > 0 AND profit IS NOT NULL THEN (profit / revenue) * 100
                    ELSE NULL
                END AS margin_pct,
                ROW_NUMBER() OVER (ORDER BY revenue DESC, protein_family) AS rn
            FROM protein_scope
            WHERE revenue > 0
        ),
        transfer_pairs AS (
            SELECT
                COALESCE(current_owner_id, current_owner_name, 'Unassigned / Needs Review') AS owner_key,
                COALESCE(current_owner_name, current_owner_id, 'Unassigned / Needs Review') AS owner_name,
                COALESCE(prior_rep_id, prior_rep_name, historical_rep_id, historical_rep_name, 'Unassigned / Needs Review') AS prior_key,
                COALESCE(prior_rep_name, prior_rep_id, historical_rep_name, historical_rep_id, 'Unassigned / Needs Review') AS prior_name,
                STRING_AGG(
                    DISTINCT COALESCE(territory_name, territory_id, 'Unassigned'),
                    ', '
                    ORDER BY COALESCE(territory_name, territory_id, 'Unassigned')
                ) AS territory_names,
                COUNT(DISTINCT CASE WHEN is_current_window = 1 AND inherited_flag = 1 THEN customer_id END) AS customer_count,
                SUM(CASE WHEN is_current_window = 1 AND inherited_flag = 1 THEN revenue ELSE 0 END) AS revenue,
                MIN(CASE WHEN is_current_window = 1 AND inherited_flag = 1 THEN order_date END) AS first_order_date,
                MAX(CASE WHEN is_current_window = 1 AND inherited_flag = 1 THEN order_date END) AS last_order_date
            FROM attributed_base
            WHERE current_owner_id IS NOT NULL OR current_owner_name IS NOT NULL
            GROUP BY 1, 2, 3, 4
        ),
        transfer_ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (ORDER BY revenue DESC, owner_name, prior_name) AS rn
            FROM transfer_pairs
            WHERE revenue > 0
              AND prior_name <> 'Unassigned / Needs Review'
        ),
        territory_scope AS (
            SELECT
                COALESCE(territory_name, territory_id, 'Unassigned') AS territory_name,
                COUNT(DISTINCT CASE WHEN is_current_window = 1 THEN customer_id END) AS customer_count,
                COUNT(DISTINCT CASE WHEN is_current_window = 1 AND inherited_flag = 1 THEN customer_id END) AS inherited_customer_count,
                SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END) AS revenue,
                SUM(CASE WHEN is_current_window = 1 AND inherited_flag = 1 THEN revenue ELSE 0 END) AS inherited_revenue
            FROM attributed_base
            GROUP BY 1
        ),
        territory_ranked AS (
            SELECT
                *,
                CASE
                    WHEN SUM(revenue) OVER () > 0 THEN revenue / NULLIF(SUM(revenue) OVER (), 0) * 100
                    ELSE NULL
                END AS revenue_share_pct,
                ROW_NUMBER() OVER (ORDER BY revenue DESC, territory_name) AS rn
            FROM territory_scope
            WHERE revenue > 0
        ),
        dq_scope AS (
            SELECT
                CASE
                    WHEN owner_missing = 1 THEN 'unassigned'
                    WHEN current_owner_active = FALSE THEN 'inactive_current_owner'
                    WHEN owner_source = 'fact_current_owner' THEN 'fact_fallback'
                    WHEN dq_status IS NOT NULL AND dq_status <> '' THEN dq_status
                    ELSE 'ok'
                END AS dq_bucket,
                COUNT(DISTINCT CASE WHEN is_current_window = 1 THEN customer_id END) AS customer_count,
                SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END) AS revenue
            FROM attributed_base
            GROUP BY 1
        )
        SELECT
            'top_customer' AS dataset,
            customer_id AS key,
            customer_name AS label,
            current_owner_name AS secondary_label,
            revenue AS metric_1,
            profit AS metric_2,
            revenue - yoy_revenue AS metric_3,
            CASE WHEN yoy_revenue > 0 THEN (revenue - yoy_revenue) / yoy_revenue * 100 ELSE NULL END AS metric_4,
            CASE WHEN prior_revenue > 0 THEN (revenue - prior_revenue) / prior_revenue * 100 ELSE NULL END AS metric_5,
            CAST(orders AS DOUBLE) AS metric_6,
            beef_revenue AS metric_7,
            poultry_revenue AS metric_8,
            pork_revenue AS metric_9,
            CASE WHEN is_overdue THEN 1.0 ELSE 0.0 END AS metric_10,
            avg_days_between_orders AS metric_11,
            delivery_lat AS metric_12,
            delivery_lng AS metric_13,
            opportunity_score AS metric_14,
            territory_name AS text_1,
            current_owner_id AS text_2,
            strftime('%Y-%m-%d', last_order_date) AS last_order_date,
            array_to_string(historical_proteins, ', ') AS text_3
        FROM top_customers
        WHERE rn <= 200
        UNION ALL
        SELECT
            'customer_mover_up' AS dataset,
            customer_id AS key,
            customer_name AS label,
            current_owner_name AS secondary_label,
            revenue - prior_revenue AS metric_1,
            revenue AS metric_2,
            CASE WHEN prior_revenue > 0 THEN (revenue - prior_revenue) / prior_revenue * 100 ELSE NULL END AS metric_3,
            yoy_revenue AS metric_4,
            CAST(orders AS DOUBLE) AS metric_5,
            NULL::DOUBLE AS metric_6,
            NULL::DOUBLE AS metric_7,
            NULL::DOUBLE AS metric_8,
            NULL::DOUBLE AS metric_9,
            NULL::DOUBLE AS metric_10,
            NULL::DOUBLE AS metric_11,
            NULL::DOUBLE AS metric_12,
            NULL::DOUBLE AS metric_13,
            NULL::DOUBLE AS metric_14,
            territory_name AS text_1,
            current_owner_id AS text_2,
            NULL::VARCHAR AS last_order_date,
            NULL::VARCHAR AS text_3
        FROM customer_movers_pos
        WHERE rn <= 8
        UNION ALL
        SELECT
            'customer_mover_down' AS dataset,
            customer_id AS key,
            customer_name AS label,
            current_owner_name AS secondary_label,
            revenue - prior_revenue AS metric_1,
            revenue AS metric_2,
            CASE WHEN prior_revenue > 0 THEN (revenue - prior_revenue) / prior_revenue * 100 ELSE NULL END AS metric_3,
            yoy_revenue AS metric_4,
            CAST(orders AS DOUBLE) AS metric_5,
            NULL::DOUBLE AS metric_6,
            NULL::DOUBLE AS metric_7,
            NULL::DOUBLE AS metric_8,
            NULL::DOUBLE AS metric_9,
            NULL::DOUBLE AS metric_10,
            NULL::DOUBLE AS metric_11,
            NULL::DOUBLE AS metric_12,
            NULL::DOUBLE AS metric_13,
            NULL::DOUBLE AS metric_14,
            territory_name AS text_1,
            current_owner_id AS text_2,
            NULL::VARCHAR AS last_order_date,
            NULL::VARCHAR AS text_3
        FROM customer_movers_neg
        WHERE rn <= 8
        UNION ALL
        SELECT
            'protein' AS dataset,
            protein_family AS key,
            protein_family AS label,
            NULL::VARCHAR AS secondary_label,
            revenue AS metric_1,
            profit AS metric_2,
            margin_pct AS metric_3,
            revenue - yoy_revenue AS metric_4,
            weight_lb AS metric_5,
            NULL::DOUBLE AS metric_6,
            NULL::DOUBLE AS metric_7,
            NULL::DOUBLE AS metric_8,
            NULL::DOUBLE AS metric_9,
            NULL::DOUBLE AS metric_10,
            NULL::DOUBLE AS metric_11,
            NULL::DOUBLE AS metric_12,
            NULL::DOUBLE AS metric_13,
            NULL::DOUBLE AS metric_14,
            NULL::VARCHAR AS text_1,
            NULL::VARCHAR AS text_2,
            NULL::VARCHAR AS last_order_date,
            NULL::VARCHAR AS text_3
        FROM protein_ranked
        WHERE rn <= 10
        UNION ALL
        SELECT
            'transfer_pair' AS dataset,
            owner_key AS key,
            owner_name AS label,
            prior_name AS secondary_label,
            revenue AS metric_1,
            CAST(customer_count AS DOUBLE) AS metric_2,
            NULL::DOUBLE AS metric_3,
            NULL::DOUBLE AS metric_4,
            NULL::DOUBLE AS metric_5,
            NULL::DOUBLE AS metric_6,
            NULL::DOUBLE AS metric_7,
            NULL::DOUBLE AS metric_8,
            NULL::DOUBLE AS metric_9,
            NULL::DOUBLE AS metric_10,
            NULL::DOUBLE AS metric_11,
            NULL::DOUBLE AS metric_12,
            NULL::DOUBLE AS metric_13,
            NULL::DOUBLE AS metric_14,
            territory_names AS text_1,
            CASE
                WHEN first_order_date IS NULL OR last_order_date IS NULL THEN NULL
                ELSE strftime('%Y-%m-%d', first_order_date) || ' to ' || strftime('%Y-%m-%d', last_order_date)
            END AS text_2,
            NULL::VARCHAR AS last_order_date,
            NULL::VARCHAR AS text_3
        FROM transfer_ranked
        WHERE rn <= 10
        UNION ALL
        SELECT
            'territory' AS dataset,
            territory_name AS key,
            territory_name AS label,
            NULL::VARCHAR AS secondary_label,
            revenue AS metric_1,
            CAST(customer_count AS DOUBLE) AS metric_2,
            revenue_share_pct AS metric_3,
            inherited_revenue AS metric_4,
            CAST(inherited_customer_count AS DOUBLE) AS metric_5,
            NULL::DOUBLE AS metric_6,
            NULL::DOUBLE AS metric_7,
            NULL::DOUBLE AS metric_8,
            NULL::DOUBLE AS metric_9,
            NULL::DOUBLE AS metric_10,
            NULL::DOUBLE AS metric_11,
            NULL::DOUBLE AS metric_12,
            NULL::DOUBLE AS metric_13,
            NULL::DOUBLE AS metric_14,
            NULL::VARCHAR AS text_1,
            NULL::VARCHAR AS text_2,
            NULL::VARCHAR AS last_order_date,
            NULL::VARCHAR AS text_3
        FROM territory_ranked
        WHERE rn <= 6
        UNION ALL
        SELECT
            'lost_account' AS dataset,
            customer_id AS key,
            customer_name AS label,
            current_owner_name AS secondary_label,
            prior_revenue AS metric_1,
            revenue AS metric_2,
            NULL::DOUBLE AS metric_3,
            NULL::DOUBLE AS metric_4,
            NULL::DOUBLE AS metric_5,
            NULL::DOUBLE AS metric_6,
            NULL::DOUBLE AS metric_7,
            NULL::DOUBLE AS metric_8,
            NULL::DOUBLE AS metric_9,
            CASE WHEN is_overdue THEN 1.0 ELSE 0.0 END AS metric_10,
            avg_days_between_orders AS metric_11,
            delivery_lat AS metric_12,
            delivery_lng AS metric_13,
            opportunity_score AS metric_14,
            territory_name AS text_1,
            current_owner_id AS text_2,
            strftime('%Y-%m-%d', last_order_date) AS last_order_date,
            array_to_string(historical_proteins, ', ') AS text_3
        FROM customer_scope
        WHERE revenue = 0 AND prior_revenue > 0
        UNION ALL
        SELECT
            'dq' AS dataset,
            dq_bucket AS key,
            dq_bucket AS label,
            NULL::VARCHAR AS secondary_label,
            revenue AS metric_1,
            CAST(customer_count AS DOUBLE) AS metric_2,
            NULL::DOUBLE AS metric_3,
            NULL::DOUBLE AS metric_4,
            NULL::DOUBLE AS metric_5,
            NULL::DOUBLE AS metric_6,
            NULL::DOUBLE AS metric_7,
            NULL::DOUBLE AS metric_8,
            NULL::DOUBLE AS metric_9,
            NULL::DOUBLE AS metric_10,
            NULL::DOUBLE AS metric_11,
            NULL::DOUBLE AS metric_12,
            NULL::DOUBLE AS metric_13,
            NULL::DOUBLE AS metric_14,
            NULL::VARCHAR AS text_1,
            NULL::VARCHAR AS text_2,
            NULL::VARCHAR AS last_order_date,
            NULL::VARCHAR AS text_3
        FROM dq_scope
        WHERE COALESCE(customer_count, 0) > 0 OR COALESCE(revenue, 0) <> 0
        ORDER BY dataset, metric_1 DESC NULLS LAST, label
    """


def _build_table_rows(df, page: int, page_size: int, sort_by: str, sort_dir: str) -> Tuple[List[Dict[str, Any]], int, int, int]:
    records = _rollup_records(df)
    if not records:
        return [], 0, 0, 0

    work = [_sanitize_rollup_record(rec) for rec in records]
    sorted_rows = _sort_rollup_records(work, sort_by, sort_dir)
    total_rows = len(sorted_rows)
    total_pages = max(1, math.ceil(total_rows / page_size)) if total_rows else 0
    offset = (page - 1) * page_size
    if offset >= total_rows:
        offset = 0
        page = 1
    page_rows = sorted_rows[offset : offset + page_size]

    rows: List[Dict[str, Any]] = []
    for rec in page_rows:
        rows.append(
            {
                "rep_id": rec.get("rep_id"),
                "rep_name": rec.get("rep_name"),
                "key": rec.get("rep_key") or rec.get("rep_id"),
                "label": rec.get("rep_name") or rec.get("rep_key") or rec.get("rep_id"),
                "revenue": _clean_float(rec.get("revenue", 0.0)),
                "cost": _clean_optional(rec.get("cost")),
                "profit": _clean_optional(rec.get("profit")),
                "prior_revenue": _clean_optional(rec.get("prior_revenue")),
                "prior_profit": _clean_optional(rec.get("prior_profit")),
                "yoy_revenue": _clean_optional(rec.get("yoy_revenue")),
                "yoy_profit": _clean_optional(rec.get("yoy_profit")),
                "margin_pct": _clean_optional(rec.get("margin_pct")),
                "orders": _clean_int(rec.get("orders", 0)),
                "customers": _clean_int(rec.get("customers", 0)),
                "units": _clean_float(rec.get("units", 0.0)),
                "weight_lb": _clean_float(rec.get("weight_lb", 0.0)),
                "asp": _clean_optional(rec.get("asp")),
                "asp_lb": _clean_optional(rec.get("asp_lb")),
                "avg_order_value": _clean_optional(rec.get("avg_order_value")),
                "revenue_per_customer": _clean_optional(rec.get("revenue_per_customer")),
                "top_customer_share": _clean_optional(rec.get("top_customer_share")),
                "top_5_customer_share": _clean_optional(rec.get("top_5_customer_share")),
                "top_customer_name": rec.get("top_customer_name"),
                "top_customer_revenue": _clean_optional(rec.get("top_customer_revenue")),
                "top_customer_share_pct": _clean_optional(rec.get("top_customer_share") * 100.0)
                if rec.get("top_customer_share") is not None
                else None,
                "top_5_customer_share_pct": _clean_optional(rec.get("top_5_customer_share") * 100.0)
                if rec.get("top_5_customer_share") is not None
                else None,
                "customer_hhi": _clean_optional(rec.get("customer_hhi")),
                "momentum_pct": _clean_optional(rec.get("momentum_pct")),
                "mom_revenue_pct": _clean_optional(rec.get("mom_revenue_pct")),
                "mom_profit_pct": _clean_optional(rec.get("mom_profit_pct")),
                "mom_margin_pct": _clean_optional(rec.get("mom_margin_pct")),
                "yoy_revenue_pct": _clean_optional(rec.get("yoy_revenue_pct")),
                "yoy_profit_pct": _clean_optional(rec.get("yoy_profit_pct")),
                "yoy_margin_delta": _clean_optional(rec.get("yoy_margin_delta")),
                "active_customers": _clean_int(rec.get("active_customers")),
                "healthy_customers": _clean_int(rec.get("healthy_customers")),
                "direct_customers": _clean_int(rec.get("direct_customers")),
                "current_owned_customers": _clean_int(rec.get("current_owned_customers")),
                "inherited_customers": _clean_int(rec.get("inherited_customers")),
                "direct_revenue": _clean_optional(rec.get("direct_revenue")),
                "inherited_revenue_share_pct": _clean_optional(rec.get("inherited_revenue_share_pct")),
                "rank_change": _clean_optional(rec.get("rank_change")),
                "gained_customers": _clean_int(rec.get("gained_customers")),
                "lost_customers": _clean_int(rec.get("lost_customers")),
                "territory_count": _clean_int(rec.get("territory_count")),
                "top_territory_name": rec.get("top_territory_name"),
                "top_territory_revenue": _clean_optional(rec.get("top_territory_revenue")),
                "unassigned_customers": _clean_int(rec.get("unassigned_customers")),
                "replaced_rep_count": _clean_int(rec.get("replaced_rep_count")),
                "replaced_rep_names": rec.get("replaced_rep_names"),
                "transferred_in_revenue": _clean_optional(rec.get("transferred_in_revenue")),
                "transferred_out_revenue": _clean_optional(rec.get("transferred_out_revenue")),
                "transfer_revenue": _clean_optional(rec.get("transfer_revenue")),
                "unassigned_revenue": _clean_optional(rec.get("unassigned_revenue")),
                "current_owner_revenue": _clean_optional(rec.get("current_owner_revenue")),
                "historical_revenue": _clean_optional(rec.get("historical_revenue")),
                "ownership_delta_revenue": _clean_optional(rec.get("ownership_delta_revenue")),
                "ownership_delta_pct": _clean_optional(rec.get("ownership_delta_pct")),
                "top_protein_family": rec.get("top_protein_family"),
                "top_protein_revenue": _clean_optional(rec.get("top_protein_revenue")),
                # Health Score (Task 2A)
                "health_score": _clean_int(rec.get("health_score", 0)),
                "health_label": rec.get("health_label", "At Risk"),
                "health_color": rec.get("health_color", "#dc3545"),
                "health_components": rec.get("health_components"),
                # Quartile Ranking (Task 2B) — set in build_salesreps_bundle after this call
                "revenue_quartile": _clean_int(rec.get("revenue_quartile", 0)),
                "quartile_label": rec.get("quartile_label", ""),
            }
        )
    return rows, total_rows, total_pages, page


def _what_changed_insight(kpis: Dict[str, Any], rollup_df) -> List[str]:
    rows = [_sanitize_rollup_record(rec) for rec in _rollup_records(rollup_df)]
    if not rows:
        return ["No sales rep activity for the selected filters."]

    insights = []

    # 1. Top Growth Rep
    growth_reps = [r for r in rows if r.get("mom_revenue_pct") is not None]
    if growth_reps:
        top_growth = max(growth_reps, key=lambda r: r["mom_revenue_pct"])
        if top_growth["mom_revenue_pct"] > 0:
            insights.append(f"Top Growth Rep: {top_growth['rep_name']} (+{top_growth['mom_revenue_pct']:.1f}% MoM)")
        else:
            insights.append("Top Growth Rep: None (all declining or flat)")
    else:
        insights.append("Top Growth Rep: Insufficient data")

    # 2. Top Margin Risk
    # In this advanced version, we have health scores and classifying margin status.
    # Let's use the classified status if available.
    margin_risks = [r for r in rows if r.get("margin_pct") is not None and r["margin_pct"] < 27]
    if margin_risps := [r for r in margin_risks if r.get("revenue") > 5000]:
        worst_margin = min(margin_risps, key=lambda r: r["margin_pct"])
        insights.append(f"Top Margin Risk: {worst_margin['rep_name']} ({worst_margin['margin_pct']:.1f}%)")
    else:
        insights.append("Top Margin Risk: None (all key accounts above 27%)")

    # 3. Silent High-Value Accounts
    # Use momentum_pct decline > 20% for large accounts
    silent_reps = sum(1 for r in rows if (r.get("momentum_pct") or 0) < -20 and (r.get("revenue") or 0) > 10000)
    insights.append(f"Silent High-Value Accounts: {silent_reps} rep portfolios showing >20% momentum decline")

    return insights


def _risk_flags(rollup_df) -> List[Dict[str, Any]]:
    rows = [_sanitize_rollup_record(rec) for rec in _rollup_records(rollup_df)]
    if not rows:
        return []
    top_customer_count = sum(
        1 for row in rows if _clean_float(row.get("top_customer_share")) > RISK_TOP_CUSTOMER_THRESHOLD
    )
    low_margin_count = sum(
        1
        for row in rows
        if (
            _clean_optional(row.get("margin_pct")) is not None
            and _clean_optional(row.get("target_margin_pct")) is not None
            and _clean_float(row.get("margin_pct"))
            < _clean_float(row.get("target_margin_pct"))
        )
    )
    profit_down_count = sum(
        1
        for row in rows
        if (_clean_optional(row.get("mom_profit_pct")) is not None and _clean_float(row.get("mom_profit_pct")) < RISK_MOM_PROFIT_DOWN_THRESHOLD)
    )
    return [
        {
            "key": "top_customer_concentration",
            "severity": "high" if top_customer_count > 0 else "ok",
            "count": top_customer_count,
            "label": f"Top customer share > {int(RISK_TOP_CUSTOMER_THRESHOLD * 100)}%",
        },
        {
            "key": "low_margin",
            "severity": "medium" if low_margin_count > 0 else "ok",
            "count": low_margin_count,
            "label": "Margin below mapped protein target",
        },
        {
            "key": "profit_decline",
            "severity": "high" if profit_down_count > 0 else "ok",
            "count": profit_down_count,
            "label": f"Profit down MoM worse than {abs(RISK_MOM_PROFIT_DOWN_THRESHOLD):.0f}%",
        },
    ]


def _build_analysis_sections(analysis_df, rollup_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    records = _rollup_records(analysis_df)
    sections: Dict[str, Any] = {
        "top_customers": [],
        "customer_movers": {"up": [], "down": []},
        "proteins": [],
        "replacement_pairs": [],
        "territories": [],
        "lost_accounts": [],
        "data_quality": [],
    }
    for rec in records:
        dataset = str(rec.get("dataset") or "").strip().lower()
        key = rec.get("key")
        label = rec.get("label") or key
        secondary = rec.get("secondary_label")
        metric_1 = _clean_optional(rec.get("metric_1"))
        metric_2 = _clean_optional(rec.get("metric_2"))
        metric_3 = _clean_optional(rec.get("metric_3"))
        metric_4 = _clean_optional(rec.get("metric_4"))
        metric_5 = _clean_optional(rec.get("metric_5"))
        metric_6 = _clean_optional(rec.get("metric_6"))
        metric_7 = _clean_optional(rec.get("metric_7"))
        metric_8 = _clean_optional(rec.get("metric_8"))
        metric_9 = _clean_optional(rec.get("metric_9"))
        metric_10 = _clean_optional(rec.get("metric_10"))
        metric_11 = _clean_optional(rec.get("metric_11"))
        metric_12 = _clean_optional(rec.get("metric_12"))
        metric_13 = _clean_optional(rec.get("metric_13"))
        metric_14 = _clean_optional(rec.get("metric_14"))
        text_1 = rec.get("text_1")
        text_2 = rec.get("text_2")
        text_3 = rec.get("text_3")
        if dataset == "top_customer":
            owner_ref = _business_rep_reference(secondary, text_2)
            sections["top_customers"].append(
                {
                    "customer_id": key,
                    "customer_name": label,
                    "account_owner_id": owner_ref["rep_id"],
                    "account_owner_name": owner_ref["rep_name"],
                    "revenue": metric_1,
                    "profit": metric_2,
                    "yoy_delta_revenue": metric_3,
                    "yoy_revenue_pct": metric_4,
                    "mom_revenue_pct": metric_5,
                    "vs_prior_pct": metric_5,
                    "orders": _clean_int(metric_6),
                    "beef_revenue": metric_7,
                    "poultry_revenue": metric_8,
                    "pork_revenue": metric_9,
                    "is_overdue": bool(metric_10),
                    "avg_days_between_orders": metric_11,
                    "delivery_lat": metric_12,
                    "delivery_lng": metric_13,
                    "opportunity_score": metric_14,
                    "territory_name": text_1,
                    "last_order_date": rec.get("last_order_date"),
                    "historical_proteins": [p.strip() for p in (text_3 or "").split(",") if p.strip()],
                }
            )
        elif dataset == "lost_account":
            owner_ref = _business_rep_reference(secondary, text_2)
            sections["lost_accounts"].append(
                {
                    "customer_id": key,
                    "customer_name": label,
                    "account_owner_id": owner_ref["rep_id"],
                    "account_owner_name": owner_ref["rep_name"],
                    "revenue_prev_30": metric_1,
                    "revenue_last_30": metric_2,
                    "is_overdue": bool(metric_10),
                    "avg_days_between_orders": metric_11,
                    "delivery_lat": metric_12,
                    "delivery_lng": metric_13,
                    "opportunity_score": metric_14,
                    "territory_name": text_1,
                    "last_order_date": rec.get("last_order_date"),
                    "historical_proteins": [p.strip() for p in (text_3 or "").split(",") if p.strip()],
                }
            )
        elif dataset == "customer_mover_up":
            owner_ref = _business_rep_reference(secondary, text_2)
            sections["customer_movers"]["up"].append(
                {
                    "customer_id": key,
                    "customer_name": label,
                    "account_owner_id": owner_ref["rep_id"],
                    "account_owner_name": owner_ref["rep_name"],
                    "delta_revenue": metric_1,
                    "revenue": metric_2,
                    "delta_pct": metric_3,
                    "territory_name": text_1,
                    "yoy_revenue": metric_4,
                }
            )
        elif dataset == "customer_mover_down":
            owner_ref = _business_rep_reference(secondary, text_2)
            sections["customer_movers"]["down"].append(
                {
                    "customer_id": key,
                    "customer_name": label,
                    "account_owner_id": owner_ref["rep_id"],
                    "account_owner_name": owner_ref["rep_name"],
                    "delta_revenue": metric_1,
                    "revenue": metric_2,
                    "delta_pct": metric_3,
                    "territory_name": text_1,
                    "yoy_revenue": metric_4,
                }
            )
        elif dataset == "protein":
            sections["proteins"].append(
                {
                    "protein_family": label,
                    "revenue": metric_1,
                    "profit": metric_2,
                    "margin_pct": metric_3,
                    "yoy_delta_revenue": metric_4,
                    "weight_lb": metric_5,
                }
            )
        elif dataset == "transfer_pair":
            owner_ref = _business_rep_reference(label, key, default=_UNASSIGNED_REP_LABEL)
            prior_ref = _business_rep_reference(secondary)
            sections["replacement_pairs"].append(
                {
                    "current_owner_key": owner_ref["rep_id"] or key,
                    "current_owner_name": owner_ref["rep_name"],
                    "prior_rep_key": prior_ref["rep_id"],
                    "prior_rep_name": prior_ref["rep_name"],
                    "inherited_revenue": metric_1,
                    "customer_count": _clean_int(metric_2),
                    "territories": text_1,
                    "time_window": text_2,
                }
            )
        elif dataset == "territory":
            sections["territories"].append(
                {
                    "territory_name": label,
                    "revenue": metric_1,
                    "customer_count": _clean_int(metric_2),
                    "revenue_share_pct": metric_3,
                    "inherited_revenue": metric_4,
                    "inherited_customer_count": _clean_int(metric_5),
                }
            )
        elif dataset == "dq":
            bucket_name = str(label or "").strip().lower()
            if bucket_name in {
                "",
                "ok",
                "customer_history",
                "territory_history",
                "ownership_history",
                "succession_map",
            }:
                continue
            sections["data_quality"].append(
                {
                    "bucket": label,
                    "revenue": metric_1,
                    "customer_count": _clean_int(metric_2),
                }
            )

    replacement_names = sorted(
        {
            str(row.get("prior_rep_name")).strip()
            for row in sections["replacement_pairs"]
            if str(row.get("prior_rep_name") or "").strip()
        }
    )
    sections["proteins"] = margin_rules.annotate_margin_rows(
        sections["proteins"],
        protein_keys=("protein_family",),
        category_keys=("protein_family",),
        revenue_key="revenue",
        profit_key="profit",
        margin_key="margin_pct",
    )
    top_rep = rollup_rows[0] if rollup_rows else {}
    sections["portfolio"] = {
        "visible_rep_count": len(rollup_rows),
        "top_rep_name": _business_rep_name(top_rep.get("rep_name"), top_rep.get("rep_id")),
        "top_rep_revenue": top_rep.get("revenue"),
        "top_rep_direct_revenue": top_rep.get("direct_revenue"),
        "top_rep_inherited_revenue": top_rep.get("transferred_in_revenue"),
        "top_rep_territory_count": _clean_int(top_rep.get("territory_count")),
        "top_rep_replaced_rep_count": _clean_int(top_rep.get("replaced_rep_count")),
        "top_rep_replaced_rep_names": _business_rep_csv(top_rep.get("replaced_rep_names")),
        "territory_count": len(sections["territories"]),
        "territories": sections["territories"][:3],
        "replacement_pairs": sections["replacement_pairs"][:6],
        "replacement_names": replacement_names,
    }
    return sections


def _month_bucket_sort_key(value: Any) -> tuple[int, str]:
    token = str(value or "").strip()
    if not token:
        return (10**9, "")
    try:
        period = pd.Period(token, freq="M")
        return (int(period.ordinal), token)
    except Exception:
        return (10**9, token)


def _build_rank_change_map(rows: List[Dict[str, Any]]) -> Dict[str, int | None]:
    current_rank = {
        str(row.get("rep_id") or row.get("rep_key") or ""): idx + 1
        for idx, row in enumerate(_sort_rollup_records(rows, "revenue", "desc"))
        if row.get("rep_id") or row.get("rep_key")
    }
    prior_rank = {
        str(row.get("rep_id") or row.get("rep_key") or ""): idx + 1
        for idx, row in enumerate(
            sorted(
                rows,
                key=lambda row: (_clean_float(row.get("prior_revenue")), _business_rep_name(row.get("rep_name"), row.get("rep_id"))),
                reverse=True,
            )
        )
        if row.get("rep_id") or row.get("rep_key")
    }
    changes: Dict[str, int | None] = {}
    for rep_id, rank in current_rank.items():
        prev = prior_rank.get(rep_id)
        changes[rep_id] = (prev - rank) if prev else None
    return changes


def _top_row_by(rows: List[Dict[str, Any]], key: str, *, reverse: bool = True) -> Dict[str, Any] | None:
    ranked = [
        row for row in rows
        if _clean_optional(row.get(key)) is not None
    ]
    if not ranked:
        return None
    return sorted(ranked, key=lambda row: _clean_float(row.get(key)), reverse=reverse)[0]


def _salesrep_page_insights(kpis: Dict[str, Any], rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    highest_growth = _top_row_by(rows, "mom_revenue_pct", reverse=True)
    biggest_drag = _top_row_by(rows, "yoy_revenue_pct", reverse=False)
    highest_inherited = _top_row_by(rows, "inherited_revenue_share_pct", reverse=True)
    largest_concentration = _top_row_by(rows, "top_customer_share", reverse=True)
    best_margin = _top_row_by(
        [row for row in rows if _clean_float(row.get("revenue")) > 0],
        "margin_pct",
        reverse=True,
    )
    chips: List[Dict[str, Any]] = []

    def _append_chip(key: str, label: str, row: Dict[str, Any] | None, metric_key: str, formatter: str) -> None:
        if not row:
            return
        rep_name = _business_rep_name(row.get("rep_name"), row.get("rep_id"))
        metric_val = _clean_optional(row.get(metric_key))
        if metric_val is None:
            return
        if formatter == "pct":
            value = metric_val
            display = f"{metric_val:+.1f}%"
        elif formatter == "share":
            value = metric_val
            display = f"{metric_val:.1f}%"
        else:
            value = metric_val
            display = f"${metric_val:,.0f}"
        chips.append(
            {
                "key": key,
                "label": label,
                "rep_id": row.get("rep_id"),
                "rep_name": rep_name,
                "metric_key": metric_key,
                "metric_value": value,
                "display_value": display,
            }
        )

    _append_chip("highest_growth_rep", "Highest Growth Rep", highest_growth, "mom_revenue_pct", "pct")
    _append_chip("biggest_yoy_drag", "Biggest YoY Drag", biggest_drag, "yoy_revenue_pct", "pct")
    _append_chip("highest_inherited_exposure", "Highest Inherited Exposure", highest_inherited, "inherited_revenue_share_pct", "share")
    _append_chip("largest_concentration_risk", "Largest Concentration Risk", largest_concentration, "top_customer_share", "share")
    _append_chip("best_margin_performer", "Best Margin Performer", best_margin, "margin_pct", "share")

    narrative_parts: List[str] = []
    if highest_growth:
        narrative_parts.append(
            f"{_business_rep_name(highest_growth.get('rep_name'), highest_growth.get('rep_id'))} is the strongest MoM gainer"
        )
    if biggest_drag:
        narrative_parts.append(
            f"{_business_rep_name(biggest_drag.get('rep_name'), biggest_drag.get('rep_id'))} is the biggest YoY drag"
        )
    risky_count = sum(1 for row in rows if _clean_float(row.get("top_customer_share")) > RISK_TOP_CUSTOMER_THRESHOLD)
    if risky_count:
        narrative_parts.append(f"{risky_count} rep(s) are above the top-customer concentration threshold")

    return {
        "chips": chips[:5],
        "narrative": ". ".join(narrative_parts) if narrative_parts else (kpis.get("what_changed") or ""),
        "strongest_signal": chips[0] if chips else None,
        "weakest_signal": next((chip for chip in chips if chip.get("key") == "biggest_yoy_drag"), None),
    }


def build_salesreps_bundle(filters: Any, scope: Dict[str, Any], args: Any) -> Dict[str, Any]:
    started = time.perf_counter()
    context = _salesrep_attribution_context(filters, scope, args)
    if context.get("error"):
        return {"error": context["error"], "meta": {"cached": False}}

    controls: salesrep_ownership.AttributionControls = context["controls"]
    bridge_meta: salesrep_ownership.OwnershipBridgeMeta = context["bridge_meta"]
    successor_meta: salesrep_ownership.SuccessorMapMeta = context["successor_meta"]
    windows = context["windows"]
    cte_sql = context["cte_sql"]
    params = list(context["params"])
    start_iso = windows.get("window_start")
    end_iso = windows.get("window_end_display") or windows.get("window_end_exclusive")

    page_num, page_size = _pagination(args)
    sort_by, sort_dir = _sort_params(args)
    search_term = _search_term(args)
    getter = args.get if hasattr(args, "get") else (lambda _k, _d=None: None)
    metric_token = str(getter("metric") or "revenue").strip().lower()
    if hasattr(args, "get"):
        try:
            top_n = int(args.get("topN") or args.get("top_n") or TOP_N_DEFAULT)
        except Exception:
            top_n = TOP_N_DEFAULT
    else:
        top_n = TOP_N_DEFAULT
    top_n = max(5, min(top_n, 25))

    kpis_df = fact_store.execute_sql_df(_kpis_sql(cte_sql), params, tag="salesreps.kpis")
    rollup_df = fact_store.execute_sql_df(_rollup_sql(cte_sql), params, tag="salesreps.rollup")
    trend_df = fact_store.execute_sql_df(_trend_sql(cte_sql, top_n), params, tag="salesreps.trend")
    analysis_df = fact_store.execute_sql_df(_analysis_sql(cte_sql), params, tag="salesreps.analysis")
    kpis_df = _normalize_frame(kpis_df)
    rollup_df = _normalize_frame(rollup_df)
    trend_df = _normalize_frame(trend_df)
    analysis_df = _normalize_frame(analysis_df)

    last_refresh = (
        (fact_store.get_meta() or {}).get("last_refresh_utc")
        or (fact_store.get_meta() or {}).get("watermark_dt")
        or (fact_store.get_meta() or {}).get("watermark")
        or None
    )

    base_meta = {
        "page_id": "salesreps",
        "window_start": start_iso,
        "window_end": end_iso,
        "window_end_exclusive": windows.get("window_end_exclusive"),
        "prior_start": windows.get("prior_start"),
        "prior_end_exclusive": windows.get("prior_end_exclusive"),
        "yoy_start": windows.get("yoy_start"),
        "yoy_end_exclusive": windows.get("yoy_end_exclusive"),
        "units_label": "Units",
        "asp_label": "ASP",
        "asp_lb_label": "ASP / lb",
        "metric": metric_token,
        "search": search_term,
        "attribution": controls.as_dict(),
        "ownership_bridge": bridge_meta.as_dict(),
        "ownership_succession": successor_meta.as_dict(),
        "last_refresh": last_refresh,
        "risk_thresholds": {
            "top_customer_share": RISK_TOP_CUSTOMER_THRESHOLD,
            "margin_pct": None,
            "mom_profit_down_pct": RISK_MOM_PROFIT_DOWN_THRESHOLD,
        },
    }
    warnings = list(bridge_meta.warnings) + list(successor_meta.warnings)

    if rollup_df.empty and kpis_df.empty:
        payload = {
            "kpis": {
                "what_changed": "No sales rep activity for the selected filters.",
                "last_refresh": last_refresh,
            },
            "trend": {
                "labels": [],
                "series": [],
                "detail": [],
                "monthly_compare": {
                    "labels": [],
                    "revenue": [],
                    "revenue_yoy": [],
                    "profit": [],
                    "profit_yoy": [],
                    "weight_lb": [],
                    "weight_lb_yoy": [],
                    "detail": [],
                },
            },
            "charts": {
                "trend": {"labels": [], "series": [], "detail": []},
                "monthly_compare": {
                    "labels": [],
                    "revenue": [],
                    "revenue_yoy": [],
                    "profit": [],
                    "profit_yoy": [],
                    "weight_lb": [],
                    "weight_lb_yoy": [],
                    "detail": [],
                },
            },
            "table": {"rows": [], "page": page_num, "page_size": page_size, "total_rows": 0, "total_pages": 0},
            "risk_flags": [],
            "analysis": {},
            "benchmarks": {"avg_revenue": None, "avg_profit": None, "avg_margin_pct": None, "avg_health_index_pct": None, "avg_asp_lb": None, "avg_orders": None, "avg_customers": None},
            "warnings": warnings,
            "meta": {
                **base_meta,
                "ownership_snapshot": {"available": False, "rows": 0, "source": None},
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "warning_count": len(warnings),
            },
        }
        return payload

    krow = kpis_df.iloc[0] if not kpis_df.empty else {}
    revenue = _clean_float(krow.get("revenue"))
    cost = _clean_optional(krow.get("cost"))
    profit = _clean_optional(krow.get("profit"))
    margin_pct = _clean_optional(krow.get("margin_pct"))
    minimum_margin_pct = _clean_optional(krow.get("minimum_margin_pct"))
    target_margin_pct = _clean_optional(krow.get("target_margin_pct"))
    orders = _clean_int(krow.get("orders"))
    customers = _clean_int(krow.get("customers"))
    active_reps = _clean_int(krow.get("active_reps"))
    units = _clean_float(krow.get("units"))
    weight_lb = _clean_float(krow.get("weight_lb"))
    asp = _clean_optional(krow.get("asp"))
    asp_lb = _clean_optional(krow.get("asp_lb"))
    revenue_mom_pct = _clean_optional(krow.get("revenue_mom_pct"))
    profit_mom_pct = _clean_optional(krow.get("profit_mom_pct"))
    margin_mom_pct = _clean_optional(krow.get("margin_mom_pct"))
    revenue_yoy_pct = _clean_optional(krow.get("revenue_yoy_pct"))
    profit_yoy_pct = _clean_optional(krow.get("profit_yoy_pct"))
    margin_yoy_delta = _clean_optional(krow.get("margin_yoy_delta"))
    cost_null_rows = _clean_int(krow.get("cost_null_rows"))
    missing_packs_rows = _clean_int(krow.get("missing_packs_rows"))
    total_rows = _clean_int(krow.get("total_rows"))
    cost_coverage_pct = None
    packs_coverage_pct = None
    if total_rows:
        cost_coverage_pct = (1 - (cost_null_rows / total_rows)) * 100.0
        packs_coverage_pct = (1 - (missing_packs_rows / total_rows)) * 100.0
    kpi_margin_status = margin_rules.classify_margin_status(margin_pct, minimum_margin_pct, target_margin_pct)

    kpis = {
        "revenue": revenue,
        "cost": cost,
        "profit": profit,
        "margin_pct": margin_pct,
        "minimum_margin_pct": minimum_margin_pct,
        "target_margin_pct": target_margin_pct,
        "target_gap_pct_points": None if margin_pct is None or target_margin_pct is None else (margin_pct - target_margin_pct),
        "orders": orders,
        "customers": customers,
        "active_reps": active_reps,
        "bridge_customers": _clean_int(krow.get("bridge_customers")),
        "snapshot_customers": _clean_int(krow.get("snapshot_customers")),
        "fact_fallback_customers": _clean_int(krow.get("fact_fallback_customers")),
        "units": units,
        "weight_lb": weight_lb,
        "asp": asp,
        "asp_lb": asp_lb,
        "avg_order_value": _clean_optional(krow.get("avg_order_value")),
        "revenue_per_customer": _clean_optional(krow.get("revenue_per_customer")),
        "revenue_mom_pct": revenue_mom_pct,
        "profit_mom_pct": profit_mom_pct,
        "margin_mom_pct": margin_mom_pct,
        "revenue_yoy_pct": revenue_yoy_pct,
        "profit_yoy_pct": profit_yoy_pct,
        "margin_yoy_delta": margin_yoy_delta,
        "active_customers": _clean_int(krow.get("active_customers")),
        "direct_customers": _clean_int(krow.get("direct_customers")),
        "inherited_customers": _clean_int(krow.get("inherited_customers")),
        "transferred_in_customers": _clean_int(krow.get("transferred_in_customers")),
        "unassigned_customers": _clean_int(krow.get("unassigned_customers")),
        "direct_revenue": _clean_optional(krow.get("direct_revenue")),
        "inherited_revenue": _clean_optional(krow.get("inherited_revenue")),
        "unassigned_revenue": _clean_optional(krow.get("unassigned_revenue")),
        "cost_coverage_pct": cost_coverage_pct,
        "packs_coverage_pct": packs_coverage_pct,
        "attribution_mode": controls.attribution_mode,
        "roster_mode": controls.roster_mode,
        "transfer_only": bool(controls.transfer_only),
        "last_refresh": last_refresh,
        "start": start_iso,
        "end": end_iso,
        **kpi_margin_status,
    }

    rep_trend_df = trend_df.loc[trend_df["dataset"] == "rep_trend"].copy() if "dataset" in trend_df.columns else pd.DataFrame()
    monthly_compare_df = (
        trend_df.loc[trend_df["dataset"] == "monthly_compare"].copy() if "dataset" in trend_df.columns else pd.DataFrame()
    )

    trend_labels = sorted(
        {str(bucket) for bucket in rep_trend_df.get("bucket", pd.Series(dtype="string")).tolist()} if not rep_trend_df.empty else [],
        key=_month_bucket_sort_key,
    )
    series_map: Dict[str, Dict[str, float]] = {}
    rep_names: Dict[str, str] = {}
    trend_detail: List[Dict[str, Any]] = []
    if not rep_trend_df.empty:
        for r in rep_trend_df.itertuples():
            rep_key = getattr(r, "rep_key", None)
            month = str(getattr(r, "bucket", ""))
            if rep_key is None or not month:
                continue
            series_map.setdefault(rep_key, {})[month] = _clean_float(getattr(r, "revenue", 0.0))
            rep_names[rep_key] = _business_rep_name(getattr(r, "rep_name", None), rep_key)
            trend_detail.append(
                {
                    "bucket": month,
                    "rep_id": rep_key,
                    "rep_name": _business_rep_name(getattr(r, "rep_name", None), rep_key),
                    "revenue": _clean_float(getattr(r, "revenue", 0.0)),
                    "revenue_yoy": _clean_optional(getattr(r, "comparison_value", None)),
                    "profit": _clean_optional(getattr(r, "profit", None)),
                    "profit_yoy": _clean_optional(getattr(r, "comparison_profit", None)),
                    "weight_lb": _clean_float(getattr(r, "weight_lb", 0.0)),
                    "weight_lb_yoy": _clean_optional(getattr(r, "comparison_weight_lb", None)),
                    "direct_revenue": _clean_optional(getattr(r, "direct_value", None)),
                    "inherited_revenue": _clean_optional(getattr(r, "inherited_value", None)),
                    "customers": _clean_int(getattr(r, "customers_value", 0)),
                    "customers_yoy": _clean_optional(getattr(r, "comparison_customers_value", None)),
                    "direct_customers": _clean_optional(getattr(r, "direct_customers_value", None)),
                    "inherited_customers": _clean_optional(getattr(r, "inherited_customers_value", None)),
                    "observed_days": _clean_int(getattr(r, "observed_days_value", 0)),
                    "observed_days_yoy": _clean_optional(getattr(r, "comparison_observed_days_value", None)),
                }
            )

    trend_series: List[Dict[str, Any]] = []
    for rep_key, points in series_map.items():
        trend_series.append(
            {
                "rep_id": rep_key,
                "rep_name": _business_rep_name(rep_names.get(rep_key), rep_key),
                "revenue": [points.get(label, 0.0) for label in trend_labels],
            }
        )

    monthly_compare_labels: List[str] = []
    monthly_compare = {
        "labels": [],
        "revenue": [],
        "revenue_yoy": [],
        "profit": [],
        "profit_yoy": [],
        "weight_lb": [],
        "weight_lb_yoy": [],
        "detail": [],
    }
    if not monthly_compare_df.empty:
        monthly_compare_df = monthly_compare_df.assign(
            _bucket_sort=monthly_compare_df.get("bucket", pd.Series(dtype="string")).map(_month_bucket_sort_key)
        ).sort_values("_bucket_sort").drop(columns=["_bucket_sort"], errors="ignore").reset_index(drop=True)
        monthly_compare_labels = [str(v) for v in monthly_compare_df["bucket"].tolist()]
        monthly_compare = {
            "labels": monthly_compare_labels,
            "revenue": [_clean_float(v) for v in monthly_compare_df.get("revenue", pd.Series(dtype="float")).tolist()],
            "revenue_yoy": [_clean_float(v) for v in monthly_compare_df.get("comparison_value", pd.Series(dtype="float")).tolist()],
            "profit": [_clean_optional(v) for v in monthly_compare_df.get("profit", pd.Series(dtype="float")).tolist()],
            "profit_yoy": [_clean_optional(v) for v in monthly_compare_df.get("comparison_profit", pd.Series(dtype="float")).tolist()],
            "weight_lb": [_clean_float(v) for v in monthly_compare_df.get("weight_lb", pd.Series(dtype="float")).tolist()],
            "weight_lb_yoy": [
                _clean_float(v)
                for v in monthly_compare_df.get("comparison_weight_lb", pd.Series(dtype="float")).tolist()
            ],
            "detail": [
                {
                    "bucket": str(row.get("bucket") or ""),
                    "revenue": _clean_float(row.get("revenue")),
                    "revenue_yoy": _clean_optional(row.get("comparison_value")),
                    "profit": _clean_optional(row.get("profit")),
                    "profit_yoy": _clean_optional(row.get("comparison_profit")),
                    "weight_lb": _clean_float(row.get("weight_lb")),
                    "weight_lb_yoy": _clean_optional(row.get("comparison_weight_lb")),
                    "direct_revenue": _clean_optional(row.get("direct_value")),
                    "inherited_revenue": _clean_optional(row.get("inherited_value")),
                    "customers": _clean_int(row.get("customers_value")),
                    "customers_yoy": _clean_optional(row.get("comparison_customers_value")),
                    "observed_days": _clean_int(row.get("observed_days_value")),
                    "observed_days_yoy": _clean_optional(row.get("comparison_observed_days_value")),
                }
                for row in monthly_compare_df.to_dict(orient="records")
            ],
        }

    rollup_rows = [_sanitize_rollup_record(rec) for rec in _rollup_records(rollup_df)]
    rank_change_map = _build_rank_change_map(rollup_rows)
    for row in rollup_rows:
        rep_id = str(row.get("rep_id") or row.get("rep_key") or "")
        row["rank_change"] = rank_change_map.get(rep_id)
    analysis = _build_analysis_sections(
        analysis_df,
        _sort_rollup_records(rollup_rows, "revenue", "desc"),
    )
    analysis["insights"] = _salesrep_page_insights(kpis, rollup_rows)
    analysis["ownership_breakdown"] = {
        "direct_revenue": kpis.get("direct_revenue"),
        "inherited_revenue": kpis.get("inherited_revenue"),
        "direct_customers": kpis.get("direct_customers"),
        "inherited_customers": kpis.get("inherited_customers"),
        "transferred_in_customers": kpis.get("transferred_in_customers"),
        "unassigned_customers": kpis.get("unassigned_customers"),
    }
    charts: Dict[str, Any] = {}
    if rollup_rows:
        top_reps = _sort_rollup_records(rollup_rows, "revenue", "desc")[:10]
        charts["top_reps"] = [
            {
                "rep_id": row.get("rep_id"),
                "rep_name": row.get("rep_name"),
                "revenue": row.get("revenue"),
                "direct_revenue": row.get("direct_revenue"),
                "direct_profit": row.get("direct_profit"),
                "direct_weight_lb": row.get("direct_weight_lb"),
                "direct_margin_pct": row.get("direct_margin_pct"),
                "direct_customers": row.get("direct_customers"),
                "inherited_revenue": row.get("transferred_in_revenue"),
                "profit": row.get("profit"),
                "margin_pct": row.get("margin_pct"),
                "orders": row.get("orders"),
                "customers": row.get("customers"),
                "weight_lb": row.get("weight_lb"),
                "rank_change": row.get("rank_change"),
            }
            for row in top_reps
        ]

        charts["scatter"] = [
            {
                "rep_id": row.get("rep_id"),
                "rep_name": row.get("rep_name"),
                "customers": row.get("customers"),
                "orders": row.get("orders"),
                "revenue": row.get("revenue"),
                "profit": row.get("profit"),
                "margin_pct": row.get("margin_pct"),
                "rank_change": row.get("rank_change"),
            }
            for row in rollup_rows
        ]

        charts["concentration"] = [
            {
                "rep_id": row.get("rep_id"),
                "rep_name": row.get("rep_name"),
                "top_customer_share": row.get("top_customer_share"),
                "top_5_customer_share": row.get("top_5_customer_share"),
                "top_customer_name": row.get("top_customer_name"),
                "top_customer_revenue": row.get("top_customer_revenue"),
                "customer_hhi": row.get("customer_hhi"),
            }
            for row in rollup_rows
        ]

        charts["profit_vs_revenue"] = [
            {
                "rep_id": row.get("rep_id"),
                "rep_name": row.get("rep_name"),
                "revenue": row.get("revenue"),
                "profit": row.get("profit"),
                "margin_pct": row.get("margin_pct"),
            }
            for row in rollup_rows
        ]

        asp_leaders = [row for row in _sort_rollup_records(rollup_rows, "asp", "desc") if row.get("asp") is not None][:10]
        charts["asp_leaders"] = [
            {
                "rep_id": row.get("rep_id"),
                "rep_name": row.get("rep_name"),
                "asp": row.get("asp"),
                "revenue": row.get("revenue"),
            }
            for row in asp_leaders
        ]

        margin_rank = [
            row
            for row in _sort_rollup_records(rollup_rows, "margin_pct", "desc")
            if row.get("margin_pct") is not None
        ][:10]
        charts["margin_ranking"] = [
            {
                "rep_id": row.get("rep_id"),
                "rep_name": row.get("rep_name"),
                "margin_pct": row.get("margin_pct"),
                "revenue": row.get("revenue"),
                "rank_change": row.get("rank_change"),
            }
            for row in margin_rank
        ]

        pareto_rows: List[Dict[str, Any]] = []
        sorted_rev = _sort_rollup_records(rollup_rows, "revenue", "desc")
        total_rev = sum(_clean_float(row.get("revenue")) for row in sorted_rev)
        cumulative = 0.0
        for row in sorted_rev:
            rev = _clean_float(row.get("revenue"))
            cumulative += rev
            pareto_rows.append(
                {
                    "rep_id": row.get("rep_id"),
                    "rep_name": row.get("rep_name"),
                    "revenue": rev,
                    "cumulative_pct": (cumulative / total_rev * 100.0) if total_rev > 0 else None,
                }
            )
        charts["pareto"] = pareto_rows

        ownership_rank = sorted(
            rollup_rows,
            key=lambda row: abs(_clean_float(row.get("ownership_delta_revenue"))),
            reverse=True,
        )[:10]
        charts["ownership_delta"] = [
            {
                "rep_id": row.get("rep_id"),
                "rep_name": row.get("rep_name"),
                "historical_revenue": row.get("historical_revenue"),
                "current_owner_revenue": row.get("current_owner_revenue"),
                "ownership_delta_revenue": row.get("ownership_delta_revenue"),
                "ownership_delta_pct": row.get("ownership_delta_pct"),
            }
            for row in ownership_rank
            if any(
                _clean_optional(row.get(field)) is not None
                for field in ("historical_revenue", "current_owner_revenue", "ownership_delta_revenue")
            )
        ]

        transfer_rank = sorted(
            rollup_rows,
            key=lambda row: max(
                abs(_clean_float(row.get("transferred_in_revenue"))),
                abs(_clean_float(row.get("transferred_out_revenue"))),
            ),
            reverse=True,
        )[:10]
        charts["transfers"] = [
            {
                "rep_id": row.get("rep_id"),
                "rep_name": row.get("rep_name"),
                "transferred_in_revenue": row.get("transferred_in_revenue"),
                "transferred_out_revenue": row.get("transferred_out_revenue"),
                "direct_revenue": row.get("direct_revenue"),
                "inherited_customers": row.get("inherited_customers"),
                "gained_customers": row.get("gained_customers"),
                "lost_customers": row.get("lost_customers"),
            }
            for row in transfer_rank
        ]

        charts["protein_leaders"] = [
            {
                "rep_id": row.get("rep_id"),
                "rep_name": row.get("rep_name"),
                "protein_family": row.get("top_protein_family"),
                "revenue": row.get("top_protein_revenue"),
            }
            for row in rollup_rows
            if row.get("top_protein_family")
        ]

    charts["rep_trend_detail"] = trend_detail
    charts["monthly_compare"] = monthly_compare

    territory_trend_df = trend_df.loc[trend_df["dataset"] == "territory_trend"].copy() if "dataset" in trend_df.columns else pd.DataFrame()
    territory_series_map = {}
    territory_rep_counts = {}
    if not territory_trend_df.empty:
        for r in territory_trend_df.itertuples():
            t_name = getattr(r, "rep_key", None)
            month = str(getattr(r, "bucket", ""))
            if t_name is None or not month:
                continue
            territory_series_map.setdefault(t_name, {})[month] = {
                "revenue": _clean_float(getattr(r, "revenue", 0.0)),
                "revenue_yoy": _clean_optional(getattr(r, "comparison_value", None)),
            }
            territory_rep_counts[t_name] = _clean_int(getattr(r, "rep_name", 0))

    territory_series = []
    for t_name, points in territory_series_map.items():
        territory_series.append({
            "territory_name": t_name,
            "rep_count": territory_rep_counts.get(t_name, 0),
            "revenue": [points.get(label, {}).get("revenue", 0.0) for label in trend_labels],
            "revenue_yoy": [points.get(label, {}).get("revenue_yoy") for label in trend_labels],
            "has_prior_year": any(points.get(label, {}).get("revenue_yoy") is not None for label in trend_labels),
        })
    
    territory_trend = {
        "labels": trend_labels,
        "series": territory_series
    }
    analysis["territory_trend"] = territory_trend

    table_source = rollup_rows
    if search_term:
        table_source = [
            row
            for row in rollup_rows
            if (
                search_term in str(row.get("rep_name") or "").strip().lower()
                or search_term in str(row.get("top_customer_name") or "").strip().lower()
                or search_term in str(row.get("top_territory_name") or "").strip().lower()
            )
        ]
    # Compute performance quartile ranking by revenue across all reps
    if rollup_rows:
        revenue_sorted = sorted(rollup_rows, key=lambda r: _clean_float(r.get("revenue")))
        n = len(revenue_sorted)
        for idx, row in enumerate(revenue_sorted):
            # ntile(4): Q1=bottom 25%, Q4=top 25%
            quartile = min(4, max(1, math.ceil((idx + 1) / n * 4)))
            row["revenue_quartile"] = quartile
            if quartile == 4:
                row["quartile_label"] = "Top 25%"
            elif quartile == 3:
                row["quartile_label"] = "Mid-High"
            elif quartile == 2:
                row["quartile_label"] = "Mid-Low"
            else:
                row["quartile_label"] = "Bottom 25%"

    # Compute team benchmarks (revenue-weighted where applicable)
    # Revenue-weighted margin (not simple avg)
    total_revenue_for_bench = sum(_clean_float(r.get("revenue")) for r in rollup_rows)
    if total_revenue_for_bench > 0:
        avg_margin_pct = sum(
            _clean_float(r.get("revenue")) * (_clean_optional(r.get("margin_pct")) or 0.0)
            for r in rollup_rows
        ) / total_revenue_for_bench
        avg_health_index_pct = sum(
            _clean_float(r.get("revenue")) * (_clean_float(r.get("health_score", 0.0)))
            for r in rollup_rows
        ) / total_revenue_for_bench
    else:
        avg_margin_pct = None
        avg_health_index_pct = None

    benchmarks: Dict[str, Any] = {
        "avg_revenue": (total_revenue_for_bench / len(rollup_rows)) if rollup_rows else None,
        "avg_profit": (
            sum(_clean_float(r.get("profit")) for r in rollup_rows) / len(rollup_rows)
            if rollup_rows else None
        ),
        "avg_margin_pct": avg_margin_pct,  # Revenue-weighted margin (not simple avg)
        "avg_health_index_pct": avg_health_index_pct,  # Revenue-weighted health
        "avg_asp_lb": (
            sum(_clean_float(r.get("asp_lb")) for r in rollup_rows if r.get("asp_lb") is not None) / max(
                sum(1 for r in rollup_rows if r.get("asp_lb") is not None), 1
            ) if rollup_rows else None
        ),
        "avg_orders": (
            sum(_clean_float(r.get("orders")) for r in rollup_rows) / len(rollup_rows)
            if rollup_rows else None
        ),
        "avg_customers": (
            sum(_clean_float(r.get("customers")) for r in rollup_rows) / len(rollup_rows)
            if rollup_rows else None
        ),
    }

    rows, total_rows, total_pages, page_num = _build_table_rows(table_source, page_num, page_size, sort_by, sort_dir)
    total_reps = len(rollup_rows)
    kpis["active_reps"] = max(_clean_int(kpis.get("active_reps"), 0), total_reps)
    kpis["territory_count"] = len(analysis.get("territories") or [])
    kpis["replaced_rep_count"] = len(analysis.get("portfolio", {}).get("replacement_names") or [])
    kpis["transferred_accounts_count"] = _clean_int(kpis.get("inherited_customers"))
    kpis["transferred_in_revenue"] = sum(_clean_float(row.get("transferred_in_revenue")) for row in rollup_rows)
    kpis["transferred_out_revenue"] = sum(_clean_float(row.get("transferred_out_revenue")) for row in rollup_rows)
    kpis["current_owner_revenue"] = sum(_clean_float(row.get("current_owner_revenue")) for row in rollup_rows)
    kpis["historical_revenue"] = sum(_clean_float(row.get("historical_revenue")) for row in rollup_rows)
    if kpis.get("direct_revenue") is None:
        kpis["direct_revenue"] = max(
            _clean_float(kpis.get("revenue")) - _clean_float(kpis.get("inherited_revenue")),
            0.0,
        )
    kpis["what_changed"] = _what_changed_insight(kpis, rollup_df)
    kpis["largest_gained_accounts_count"] = sum(1 for row in rollup_rows if _clean_int(row.get("gained_customers")) > 0)
    kpis["largest_lost_accounts_count"] = sum(1 for row in rollup_rows if _clean_int(row.get("lost_customers")) > 0)
    risk_flags = _risk_flags(rollup_df)
    dq_rows = analysis.get("data_quality") or []
    snapshot_customers = _clean_int(kpis.get("snapshot_customers"))
    fact_fallback_customers = sum(
        _clean_int(item.get("customer_count"))
        for item in dq_rows
        if str(item.get("bucket") or "").strip().lower() == "fact_fallback"
    )
    inactive_owner_customers = sum(
        _clean_int(item.get("customer_count"))
        for item in dq_rows
        if str(item.get("bucket") or "").strip().lower() == "inactive_current_owner"
    )
    mapped_customers = max(
        _clean_int(kpis.get("active_customers"))
        - _clean_int(kpis.get("unassigned_customers"))
        - fact_fallback_customers
        - inactive_owner_customers,
        0,
    )
    if _clean_int(kpis.get("active_customers")) > 0:
        kpis["ownership_coverage_pct"] = (mapped_customers / _clean_int(kpis.get("active_customers"))) * 100.0
    else:
        kpis["ownership_coverage_pct"] = None
    if snapshot_customers > 0:
        warnings = [
            msg
            for msg in warnings
            if "ownership history bridge not configured" not in str(msg or "").strip().lower()
        ]
    if fact_fallback_customers > 0 and controls.is_current_owner_mode:
        warnings.append(
            f"{fact_fallback_customers} customer(s) are still using fact-row owner fallback; load ownership history for director-grade current-owner rollups."
        )
    if inactive_owner_customers > 0:
        warnings.append(
            f"{inactive_owner_customers} customer(s) are assigned to inactive current owners and were routed to Unassigned / Needs Review."
        )
    if _clean_int(kpis.get("unassigned_customers")) > 0:
        risk_flags.append(
            {
                "key": "unassigned_customers",
                "severity": "high",
                "count": _clean_int(kpis.get("unassigned_customers")),
                "label": "Customers missing current owner mapping",
            }
        )
        warnings.append(
            f"{_clean_int(kpis.get('unassigned_customers'))} customer(s) are in the Unassigned / Needs Review bucket."
        )

    duration_ms = int((time.perf_counter() - started) * 1000)
    meta = {
        **base_meta,
        "ownership_snapshot": {
            "available": bool(snapshot_customers > 0),
            "rows": snapshot_customers,
            "source": "fact_customer_snapshot" if snapshot_customers > 0 else None,
        },
        "packs_coverage": {
            "total_orderlines": total_rows,
            "has_packs_orderlines": max(total_rows - missing_packs_rows, 0),
            "missing_packs_orderlines": missing_packs_rows,
            "packs_coverage_pct": packs_coverage_pct,
        },
        "date_min": krow.get("date_min"),
        "date_max": krow.get("date_max"),
        "elapsed_ms": duration_ms,
        "has_margin": margin_pct is not None,
        "warning_count": len(dict.fromkeys(warnings)),
    }

    payload = {
        "kpis": kpis,
        "trend": {
            "labels": trend_labels,
            "series": trend_series,
            "detail": trend_detail,
            "monthly_compare": monthly_compare,
        },
        "charts": {
            "trend": {"labels": trend_labels, "series": trend_series, "detail": trend_detail},
            **charts,
        },
        "lost_accounts": analysis.get("lost_accounts", []),
        "table": {
            "rows": rows,
            "page": page_num,
            "page_size": page_size,
            "total_rows": total_rows,
            "total_pages": total_pages,
            "sort_by": sort_by,
            "sort_dir": sort_dir,
            "search": search_term,
            "all_rows": len(table_source),
        },
        "risk_flags": risk_flags,
        "analysis": analysis,
        "benchmarks": benchmarks,
        "warnings": list(dict.fromkeys(warnings)),
        "meta": meta,
    }
    return payload


def build_efficiency_payload(filters: Any, scope: Dict[str, Any], args: Any) -> Dict[str, Any]:
    # Reuse the main rollup but only return the eff chart part
    payload = build_salesreps_bundle(filters, scope, args)
    return {"eff": payload.get("charts", {}).get("eff"), "meta": payload.get("meta")}


def build_salesreps_drilldown(rep_id: str, filters: Any, scope: Dict[str, Any], args: Any) -> Dict[str, Any]:
    context = _salesrep_rep_context(rep_id, filters, scope, args)
    if context.get("error"):
        return {"error": context["error"], "meta": {"cached": False}}

    controls: salesrep_ownership.AttributionControls = context["controls"]
    bridge_meta: salesrep_ownership.OwnershipBridgeMeta = context["bridge_meta"]
    successor_meta: salesrep_ownership.SuccessorMapMeta = context["successor_meta"]
    windows = context["windows"]
    scoped_sql = context["cte_sql"]
    params_rep = context["params"]
    start_iso = windows.get("window_start")
    end_iso = windows.get("window_end_display") or windows.get("window_end_exclusive")

    at_risk_days = _clean_int(args.get("at_risk_days") if hasattr(args, "get") else None, 45)
    at_risk_days = max(7, min(at_risk_days, 365))

    summary_df = _normalize_frame(_salesrep_summary_frame(scoped_sql, params_rep))
    customers_df = _normalize_frame(_salesrep_customers_frame(scoped_sql, params_rep))
    products_df = _normalize_frame(_salesrep_products_frame(scoped_sql, params_rep))

    if summary_df.empty:
        return {
            "kpis": {},
            "trend": {"monthly": {"labels": [], "revenue": [], "orders": [], "profit": [], "margin_pct": []}, "weekly": {"labels": [], "revenue": [], "orders": [], "profit": [], "margin_pct": []}},
            "charts": {},
            "table": {"rows": [], "page": 1, "page_size": 0, "total": 0},
            "meta": {"page_id": "salesrep_drilldown", "entity_id": rep_id},
        }

    srow = summary_df.iloc[0]
    revenue = _clean_float(srow.get("revenue"))
    cost = _clean_optional(srow.get("cost"))
    profit = _clean_optional(srow.get("profit"))
    margin_pct = _clean_optional(srow.get("margin_pct"))
    orders = _clean_int(srow.get("orders"))
    customers = _clean_int(srow.get("customers"))
    units = _clean_float(srow.get("units"))
    weight_lb = _clean_float(srow.get("weight_lb"))
    asp = _clean_optional(srow.get("asp"))
    asp_lb = _clean_optional(srow.get("asp_lb"))
    rep_name = _business_rep_name(srow.get("rep_name"), rep_id)
    cost_null_rows = _clean_int(srow.get("cost_null_rows"))
    total_rows = _clean_int(srow.get("total_rows"))
    cost_coverage_pct = ((1 - (cost_null_rows / total_rows)) * 100.0) if total_rows else None
    ref_date = pd.to_datetime(srow.get("ref_date"), errors="coerce")

    trend_labels = [str(x) for x in _to_list(srow.get("trend_labels"))]
    trend_revenue = [_clean_float(x, 0.0) for x in _to_list(srow.get("trend_revenue"))]
    trend_orders = [_clean_int(x, 0) for x in _to_list(srow.get("trend_orders"))]
    trend_profit = [_clean_optional(x) for x in _to_list(srow.get("trend_profit"))]
    trend_margin = [_clean_optional(x) for x in _to_list(srow.get("trend_margin"))]
    trend_customers = [_clean_int(x, 0) for x in _to_list(srow.get("trend_customers"))]
    trend_units = [_clean_float(x, 0.0) for x in _to_list(srow.get("trend_units"))]

    def _rolling(values: List[Any], window: int) -> List[float | None]:
        out: List[float | None] = []
        for idx in range(len(values)):
            segment = [v for v in values[max(0, idx - window + 1) : idx + 1] if v is not None]
            out.append((sum(segment) / len(segment)) if segment else None)
        return out

    trend_monthly = {
        "labels": trend_labels,
        "revenue": trend_revenue,
        "orders": trend_orders,
        "profit": trend_profit,
        "margin_pct": trend_margin,
        "customers": trend_customers,
        "units": trend_units,
        "rolling_revenue_3m": _rolling(trend_revenue, 3),
        "rolling_profit_3m": _rolling([_clean_optional(v) for v in trend_profit], 3),
    }

    trend_weekly = {
        "labels": [str(v) for v in _to_list(srow.get("trend_week_labels"))],
        "revenue": [_clean_float(v, 0.0) for v in _to_list(srow.get("trend_week_revenue"))],
        "orders": [_clean_int(v, 0) for v in _to_list(srow.get("trend_week_orders"))],
        "profit": [_clean_optional(v) for v in _to_list(srow.get("trend_week_profit"))],
        "margin_pct": [_clean_optional(v) for v in _to_list(srow.get("trend_week_margin"))],
    }
    trend_weekly["rolling_revenue_4w"] = _rolling(trend_weekly["revenue"], 4)
    trend_compare = {
        "labels": [str(v) for v in _to_list(srow.get("compare_labels"))],
        "revenue": [_clean_float(v, 0.0) for v in _to_list(srow.get("compare_revenue"))],
        "revenue_yoy": [_clean_float(v, 0.0) for v in _to_list(srow.get("compare_revenue_yoy"))],
        "profit": [_clean_optional(v) for v in _to_list(srow.get("compare_profit"))],
        "profit_yoy": [_clean_optional(v) for v in _to_list(srow.get("compare_profit_yoy"))],
        "weight_lb": [_clean_float(v, 0.0) for v in _to_list(srow.get("compare_weight_lb"))],
        "weight_lb_yoy": [_clean_float(v, 0.0) for v in _to_list(srow.get("compare_weight_lb_yoy"))],
    }
    trend_monthly["revenue_yoy"] = list(trend_compare["revenue_yoy"])
    trend_monthly["profit_yoy"] = list(trend_compare["profit_yoy"])
    trend_monthly["weight_lb_yoy"] = list(trend_compare["weight_lb_yoy"])

    customers_df = customers_df.copy()
    products_df = products_df.copy()

    if "revenue" in customers_df:
        customers_df = customers_df.sort_values(["revenue", "customer_id"], ascending=[False, True]).reset_index(drop=True)
    if "revenue" in products_df:
        products_df = products_df.sort_values(["revenue", "product_id"], ascending=[False, True]).reset_index(drop=True)

    total_customer_revenue = float(customers_df["revenue"].sum()) if (not customers_df.empty and "revenue" in customers_df) else 0.0
    top_customer_share = None
    top5_customer_share = None
    customer_hhi = None
    if total_customer_revenue > 0 and not customers_df.empty:
        shares = (customers_df["revenue"] / total_customer_revenue).astype(float)
        top_customer_share = float(shares.iloc[0]) if len(shares.index) else None
        top5_customer_share = float(shares.head(5).sum())
        customer_hhi = float((shares.pow(2).sum()))

    top_product_share = None
    if not products_df.empty and "revenue" in products_df:
        total_product_revenue = float(products_df["revenue"].sum())
        if total_product_revenue > 0:
            top_product_share = float(products_df["revenue"].max() / total_product_revenue)

    def _mom_fields(values: List[Any]) -> tuple[float | None, float | None]:
        if len(values) < 2:
            return None, None
        curr = _clean_optional(values[-1])
        prev = _clean_optional(values[-2])
        if curr is None or prev is None:
            return curr, None
        if prev == 0:
            return curr, None
        return curr, ((curr - prev) / abs(prev)) * 100.0

    _, revenue_mom_pct = _mom_fields(trend_revenue)
    _, profit_mom_pct = _mom_fields([_clean_optional(v) for v in trend_profit])
    margin_mom_pct = None
    if len(trend_margin) >= 2 and trend_margin[-1] is not None and trend_margin[-2] is not None:
        margin_mom_pct = float(trend_margin[-1]) - float(trend_margin[-2])
    active_customers_prev = _clean_int(srow.get("active_customers_prev")) if srow.get("active_customers_prev") is not None else None
    active_customers_curr = _clean_int(srow.get("active_customers_curr")) if srow.get("active_customers_curr") is not None else None
    active_customers_delta = (active_customers_curr - active_customers_prev) if (active_customers_curr is not None and active_customers_prev is not None) else None

    def _yoy(series_values: List[Any]) -> float | None:
        if not trend_labels:
            return None
        latest_label = trend_labels[-1]
        try:
            latest_dt = pd.to_datetime(f"{latest_label}-01", errors="coerce")
            if pd.isna(latest_dt):
                return None
            yoy_label = (latest_dt - pd.DateOffset(years=1)).strftime("%Y-%m")
            idx_map = {lbl: idx for idx, lbl in enumerate(trend_labels)}
            if yoy_label not in idx_map:
                return None
            curr = _clean_optional(series_values[-1])
            prev = _clean_optional(series_values[idx_map[yoy_label]])
            if curr is None or prev is None or prev == 0:
                return None
            return ((curr - prev) / abs(prev)) * 100.0
        except Exception:
            return None

    revenue_yoy_pct = _yoy(trend_revenue)
    profit_yoy_pct = _yoy([_clean_optional(v) for v in trend_profit])
    margin_yoy_pct = _yoy([_clean_optional(v) for v in trend_margin])

    customers_records = customers_df.to_dict(orient="records") if not customers_df.empty else []
    products_records = products_df.to_dict(orient="records") if not products_df.empty else []

    for row in customers_records:
        row["margin_pct"] = _clean_optional(row.get("margin_pct"))
        row["mom_revenue_delta"] = _clean_optional(row.get("mom_revenue_delta"))
        row["mom_revenue_pct"] = _clean_optional(row.get("mom_revenue_pct"))
        row["yoy_revenue"] = _clean_optional(row.get("yoy_revenue"))
        row["yoy_revenue_pct"] = _clean_optional(row.get("yoy_revenue_pct"))
        row["revenue"] = _clean_float(row.get("revenue"))
        row["profit"] = _clean_optional(row.get("profit"))
        row["orders"] = _clean_int(row.get("orders"))
        row["weight_lb"] = _clean_float(row.get("weight_lb"))
        row["asp_lb"] = _clean_optional(row.get("asp_lb"))
        row["last_order_date"] = str(row.get("last_order_date"))[:10] if row.get("last_order_date") is not None else None
        row["last_sale_date"] = str(row.get("last_sale_date"))[:10] if row.get("last_sale_date") is not None else None
        row["customer_id"] = row.get("customer_id")
        row["customer_name"] = row.get("customer_name") or row.get("customer_id")
        row["account_owner_name"] = _business_rep_name(
            row.get("account_owner_name"),
            row.get("account_owner_id"),
        )
        row["last_sales_rep_name"] = _business_rep_name(
            row.get("last_sales_rep_name"),
            row.get("last_sales_rep_id"),
        )
        row["owner_missing"] = _clean_int(row.get("owner_missing"))
        row["inherited_flag"] = _clean_int(row.get("inherited_flag"))

    for row in products_records:
        row["margin_pct"] = _clean_optional(row.get("margin_pct"))
        row["mom_revenue_delta"] = _clean_optional(row.get("mom_revenue_delta"))
        row["mom_revenue_pct"] = _clean_optional(row.get("mom_revenue_pct"))
        row["price_change_pct"] = _clean_optional(row.get("price_change_pct"))
        row["yoy_revenue"] = _clean_optional(row.get("yoy_revenue"))
        row["yoy_revenue_pct"] = _clean_optional(row.get("yoy_revenue_pct"))
        row["revenue"] = _clean_float(row.get("revenue"))
        row["profit"] = _clean_optional(row.get("profit"))
        row["orders"] = _clean_int(row.get("orders"))
        row["weight_lb"] = _clean_float(row.get("weight_lb"))
        row["asp_lb"] = _clean_optional(row.get("asp_lb"))
        row["cost"] = (
            row["revenue"] - row["profit"]
            if row.get("revenue") is not None and row.get("profit") is not None
            else None
        )
        row["effective_cost_basis"] = row.get("cost")
        row["cost_lb"] = (
            (row["cost"] / row["weight_lb"])
            if row.get("cost") is not None and row.get("weight_lb") not in (None, 0)
            else None
        )
        row["effective_cost_lb"] = row.get("cost_lb")
        row["current_unit_price"] = row.get("asp_lb")
        row["last_order_date"] = str(row.get("last_order_date"))[:10] if row.get("last_order_date") is not None else None
        row["product_id"] = row.get("product_id")
        row["product_name"] = row.get("product_name") or row.get("product_id")
        row["protein_family"] = row.get("protein_family") or row.get("category_name")
        row["volatility"] = None

    gainers_customers = sorted(customers_records, key=lambda r: _clean_float(r.get("mom_revenue_delta")), reverse=True)[:10]
    decliners_customers = sorted(customers_records, key=lambda r: _clean_float(r.get("mom_revenue_delta")))[:10]
    gainers_products = sorted(products_records, key=lambda r: _clean_float(r.get("mom_revenue_delta")), reverse=True)[:10]
    decliners_products = sorted(products_records, key=lambda r: _clean_float(r.get("mom_revenue_delta")))[:10]

    at_risk_rows: List[Dict[str, Any]] = []
    if ref_date is not None:
        for row in customers_records:
            lod = pd.to_datetime(row.get("last_order_date"), errors="coerce")
            if pd.isna(lod):
                continue
            days_since_last = int((ref_date - lod).days)
            if days_since_last <= at_risk_days:
                continue
            row_out = dict(row)
            row_out["days_since_last_order"] = days_since_last
            row_out["prior_period_revenue"] = _clean_optional(row.get("revenue_prev_30"))
            at_risk_rows.append(row_out)
    at_risk_rows = sorted(
        at_risk_rows,
        key=lambda r: (_clean_float(r.get("prior_period_revenue")), _clean_float(r.get("revenue"))),
        reverse=True,
    )[:200]

    products_records = margin_rules.annotate_margin_rows(
        products_records,
        protein_keys=("protein_family", "category_name", "top_protein_family"),
        category_keys=("category_name", "protein_family"),
        revenue_key="revenue",
        cost_key="cost",
        profit_key="profit",
        margin_key="margin_pct",
        unit_cost_key="cost_lb",
        unit_price_key="asp_lb",
    )
    margin_risk_rows: List[Dict[str, Any]] = []
    for row in products_records:
        m = _clean_optional(row.get("margin_pct"))
        p = _clean_optional(row.get("profit"))
        rev = _clean_float(row.get("revenue"))
        target_margin_pct = _clean_optional(row.get("target_margin_pct"))
        if m is None and p is None:
            continue
        if (m is not None and target_margin_pct is not None and m < target_margin_pct) or (p is not None and p < 0):
            leakage = None
            if m is not None and target_margin_pct is not None:
                leakage = max(((target_margin_pct - m) / 100.0) * rev, 0.0)
            row_out = dict(row)
            row_out["leakage_to_target"] = leakage
            row_out["negative_margin_flag"] = 1 if (p is not None and p < 0) else 0
            margin_risk_rows.append(row_out)
    margin_risk_rows = sorted(
        margin_risk_rows,
        key=lambda r: (_clean_float(r.get("leakage_to_target")), _clean_float(r.get("revenue"))),
        reverse=True,
    )[:200]

    below_target_count = sum(
        1
        for r in margin_risk_rows
        if _clean_optional(r.get("margin_pct")) is not None
        and _clean_optional(r.get("target_margin_pct")) is not None
        and _clean_float(r.get("margin_pct")) < _clean_float(r.get("target_margin_pct"))
    )
    negative_margin_count = sum(1 for r in margin_risk_rows if _clean_int(r.get("negative_margin_flag")) == 1)
    below_target_revenue = sum(
        _clean_float(r.get("revenue"))
        for r in margin_risk_rows
        if _clean_optional(r.get("margin_pct")) is not None
        and _clean_optional(r.get("target_margin_pct")) is not None
        and _clean_float(r.get("margin_pct")) < _clean_float(r.get("target_margin_pct"))
    )
    negative_margin_revenue = sum(_clean_float(r.get("revenue")) for r in margin_risk_rows if _clean_int(r.get("negative_margin_flag")) == 1)

    rev_prev = trend_revenue[-2] if len(trend_revenue) >= 2 else None
    rev_curr = trend_revenue[-1] if len(trend_revenue) >= 1 else None
    units_prev = trend_units[-2] if len(trend_units) >= 2 else None
    units_curr = trend_units[-1] if len(trend_units) >= 1 else None
    price_impact = None
    volume_impact = None
    mix_impact = None
    total_change = None
    if (
        rev_prev is not None
        and rev_curr is not None
        and units_prev is not None
        and units_curr is not None
        and units_prev > 0
    ):
        asp_prev = rev_prev / units_prev if units_prev else None
        asp_curr = rev_curr / units_curr if units_curr else None
        if asp_prev is not None and asp_curr is not None:
            price_impact = (asp_curr - asp_prev) * units_prev
            volume_impact = (units_curr - units_prev) * asp_prev
            total_change = rev_curr - rev_prev
            mix_impact = total_change - price_impact - volume_impact

    top_customer_drivers = sorted(customers_records, key=lambda r: abs(_clean_float(r.get("mom_revenue_delta"))), reverse=True)[:3]
    top_product_drivers = sorted(products_records, key=lambda r: abs(_clean_float(r.get("mom_revenue_delta"))), reverse=True)[:3]
    driver_names = [d.get("customer_name") or d.get("customer_id") for d in top_customer_drivers] + [d.get("product_name") or d.get("product_id") for d in top_product_drivers]
    driver_names = [str(x) for x in driver_names if x][:3]
    if revenue_mom_pct is None:
        what_changed = "MoM change unavailable for this filter window."
    else:
        direction = "up" if revenue_mom_pct >= 0 else "down"
        what_changed = f"Revenue {direction} {abs(revenue_mom_pct):.1f}% MoM"
        if driver_names:
            what_changed += f" driven by {', '.join(driver_names)}."
        else:
            what_changed += "."

    risk_flags = [
        {
            "key": "top_customer_concentration",
            "severity": "high" if (_clean_float(top_customer_share) > 0.25) else "ok",
            "count": 1 if (_clean_float(top_customer_share) > 0.25) else 0,
            "label": "Top customer share > 25%",
        },
        {
            "key": "margin_below_target_skus",
            "severity": "medium" if below_target_count > 0 else "ok",
            "count": below_target_count,
            "label": "SKUs below protein target margin",
        },
        {
            "key": "negative_margin_skus",
            "severity": "high" if negative_margin_count > 0 else "ok",
            "count": negative_margin_count,
            "label": "Negative margin SKUs",
        },
    ]
    warnings = list(bridge_meta.warnings) + list(successor_meta.warnings)
    snapshot_customers = _clean_int(srow.get("snapshot_customers"))
    if snapshot_customers > 0:
        warnings = [
            msg
            for msg in warnings
            if "ownership history bridge not configured" not in str(msg or "").strip().lower()
        ]
    if _clean_int(srow.get("unassigned_customers")) > 0:
        risk_flags.append(
            {
                "key": "unassigned_customers",
                "severity": "high",
                "count": _clean_int(srow.get("unassigned_customers")),
                "label": "Customers missing current owner mapping",
            }
        )
        warnings.append(
            f"{_clean_int(srow.get('unassigned_customers'))} customer(s) are in the Unassigned / Needs Review bucket."
        )

    manifest_meta = fact_store.get_meta() or {}
    last_refresh = (
        manifest_meta.get("last_refresh_utc")
        or manifest_meta.get("watermark_dt")
        or manifest_meta.get("watermark")
        or None
    )

    kpis = {
        "rep_id": rep_id,
        "rep_name": rep_name,
        "revenue": revenue,
        "cost": cost,
        "profit": profit,
        "margin_pct": margin_pct,
        "orders": orders,
        "customers": customers,
        "bridge_customers": _clean_int(srow.get("bridge_customers")),
        "snapshot_customers": snapshot_customers,
        "fact_fallback_customers": _clean_int(srow.get("fact_fallback_customers")),
        "units": units,
        "weight_lb": weight_lb,
        "asp": asp,
        "asp_lb": asp_lb,
        "orders_last_30": _clean_int(srow.get("orders_last_30")),
        "orders_last_90": _clean_int(srow.get("orders_last_90")),
        "revenue_last_30": _clean_float(srow.get("revenue_last_30")),
        "revenue_last_90": _clean_float(srow.get("revenue_last_90")),
        "momentum_pct": None,
        "profit_last_30": None
        if srow.get("cost_last_30") is None
        else _clean_float(srow.get("revenue_last_30")) - _clean_float(srow.get("cost_last_30")),
        "profit_last_90": None
        if srow.get("cost_last_90") is None
        else _clean_float(srow.get("revenue_last_90")) - _clean_float(srow.get("cost_last_90")),
        "days_since_last_order": srow.get("days_since_last"),
        "top_customer_share": _clean_optional(top_customer_share),
        "top5_customer_share": _clean_optional(top5_customer_share),
        "top_product_share": _clean_optional(top_product_share),
        "customer_hhi": _clean_optional(customer_hhi),
        "cost_coverage_pct": cost_coverage_pct,
        "revenue_mom_pct": revenue_mom_pct,
        "profit_mom_pct": profit_mom_pct,
        "margin_mom_pct": margin_mom_pct,
        "revenue_yoy_pct": revenue_yoy_pct,
        "profit_yoy_pct": profit_yoy_pct,
        "margin_yoy_pct": margin_yoy_pct,
        "current_owned_customers": _clean_int(srow.get("current_owned_customers")),
        "inherited_customers": _clean_int(srow.get("inherited_customers")),
        "gained_customers": _clean_int(srow.get("gained_customers")),
        "lost_customers": _clean_int(srow.get("lost_customers")),
        "unassigned_customers": _clean_int(srow.get("unassigned_customers")),
        "inherited_revenue": _clean_optional(srow.get("inherited_revenue")),
        "transferred_in_revenue": _clean_optional(srow.get("transferred_in_revenue")),
        "transferred_out_revenue": _clean_optional(srow.get("transferred_out_revenue")),
        "current_owner_revenue": _clean_optional(srow.get("current_owner_revenue")),
        "historical_revenue": _clean_optional(srow.get("historical_revenue")),
        "ownership_delta_revenue": _clean_optional(srow.get("current_owner_revenue")) - _clean_optional(srow.get("historical_revenue"))
        if _clean_optional(srow.get("current_owner_revenue")) is not None and _clean_optional(srow.get("historical_revenue")) is not None
        else None,
        "active_customers_curr": active_customers_curr,
        "active_customers_prev": active_customers_prev,
        "active_customers_delta": active_customers_delta,
        "avg_order_value": (revenue / orders) if orders else None,
        "revenue_per_customer": (revenue / customers) if customers else None,
        "below_target_margin_skus": below_target_count,
        "below_target_margin_revenue": below_target_revenue,
        "negative_margin_skus": negative_margin_count,
        "negative_margin_revenue": negative_margin_revenue,
        "attribution_mode": controls.attribution_mode,
        "roster_mode": controls.roster_mode,
        "transfer_only": bool(controls.transfer_only),
        "last_refresh": last_refresh,
        "what_changed": what_changed,
        "start": start_iso,
        "end": end_iso,
    }
    try:
        rev_prev_90 = _clean_float(srow.get("revenue_prev_90"))
        if rev_prev_90 > 0:
            kpis["momentum_pct"] = (kpis["revenue_last_90"] - rev_prev_90) / rev_prev_90 * 100.0
    except Exception:
        pass

    table_rows = [
        {
            "key": c.get("customer_id"),
            "label": c.get("customer_name") or c.get("customer_id"),
            "customer_id": c.get("customer_id"),
            "customer_name": c.get("customer_name") or c.get("customer_id"),
            "revenue": _clean_float(c.get("revenue")),
            "profit": _clean_optional(c.get("profit")),
            "margin_pct": _clean_optional(c.get("margin_pct")),
            "orders": _clean_int(c.get("orders")),
            "weight_lb": _clean_float(c.get("weight_lb")),
            "asp_lb": _clean_optional(c.get("asp_lb")),
            "mom_revenue_delta": _clean_optional(c.get("mom_revenue_delta")),
            "mom_revenue_pct": _clean_optional(c.get("mom_revenue_pct")),
            "yoy_revenue_pct": _clean_optional(c.get("yoy_revenue_pct")),
            "account_owner_name": c.get("account_owner_name"),
            "last_sales_rep_name": c.get("last_sales_rep_name"),
            "inherited_flag": _clean_int(c.get("inherited_flag")),
            "owner_missing": _clean_int(c.get("owner_missing")),
            "last_order_date": c.get("last_order_date"),
        }
        for c in customers_records[:100]
    ]

    # Rolling 30-day window: last 30 days vs days 31–60 prior (equal-length windows for fair comparison)
    lost_accounts = _salesrep_lost_accounts(customers_records, ref_date)

    payload = {
        "kpis": kpis,
        "trend": {
            "monthly": trend_monthly,
            "weekly": trend_weekly,
            "monthly_compare": trend_compare,
            "default_grain": "monthly",
        },
        "table": {"rows": table_rows, "page": 1, "page_size": len(table_rows), "total": len(table_rows)},
        "tables": {
            "customers": customers_records[:250],
            "products": products_records[:250],
            "at_risk_customers": at_risk_rows,
            "margin_risk_products": margin_risk_rows,
            "movers_customers": {
                "gainers": gainers_customers,
                "decliners": decliners_customers,
            },
            "movers_products": {
                "gainers": gainers_products,
                "decliners": decliners_products,
            },
        },
        "lost_accounts": lost_accounts,
        "decomposition": {
            "price_impact": price_impact,
            "volume_impact": volume_impact,
            "mix_impact": mix_impact,
            "total_change": total_change,
            "methodology": "Approximation using latest vs prior month ASP and units.",
        },
        "charts": {
            "trend": trend_monthly,
            "trend_weekly": trend_weekly,
            "monthly_compare": trend_compare,
            "top_customers": customers_records[:20],
            "top_customers_profit": sorted(customers_records, key=lambda r: (_clean_float(r.get("profit")), _clean_float(r.get("revenue"))), reverse=True)[:20],
            "top_products": products_records[:20],
            "worst_products": sorted(products_records, key=lambda r: (_clean_float(r.get("profit")), _clean_float(r.get("revenue"))))[:20],
            "mix": products_records[:20],
            "ownership_compare": {
                "historical_revenue": _clean_optional(srow.get("historical_revenue")),
                "current_owner_revenue": _clean_optional(srow.get("current_owner_revenue")),
                "transferred_in_revenue": _clean_optional(srow.get("transferred_in_revenue")),
                "transferred_out_revenue": _clean_optional(srow.get("transferred_out_revenue")),
            },
            "concentration": {
                "top_customer_share": _clean_optional(top_customer_share),
                "top5_customer_share": _clean_optional(top5_customer_share),
                "customer_hhi": _clean_optional(customer_hhi),
            },
        },
        "risk_flags": risk_flags,
        "warnings": list(dict.fromkeys(warnings)),
        "insights": {
            "what_changed": what_changed,
            "drivers": {
                "customers": top_customer_drivers,
                "products": top_product_drivers,
            },
        },
        "meta": {
            "page_id": "salesrep_drilldown",
            "entity_id": rep_id,
            "entity_label": rep_name,
            "window_start": start_iso,
            "window_end": end_iso,
            "last_refresh": last_refresh,
            "attribution": controls.as_dict(),
            "ownership_bridge": bridge_meta.as_dict(),
            "ownership_succession": successor_meta.as_dict(),
            "ownership_snapshot": {
                "available": bool(snapshot_customers > 0),
                "rows": snapshot_customers,
                "source": "fact_customer_snapshot" if snapshot_customers > 0 else None,
            },
            "warning_count": len(dict.fromkeys(warnings)),
            "risk_thresholds": {
                "top_customer_share": 0.25,
                "margin_pct": None,
                "at_risk_days": at_risk_days,
            },
            "dataset_version": fact_store.cache_buster(),
        },
    }
    return payload


def _sanitize_business_rep_columns(df, columns: Sequence[tuple[str, str | None]]) -> pd.DataFrame:
    work = _normalize_frame(df)
    if work.empty:
        return work
    work = work.copy()
    for name_col, id_col in columns:
        if name_col not in work.columns and (not id_col or id_col not in work.columns):
            continue
        work[name_col] = work.apply(
            lambda row: _business_rep_name(
                row.get(name_col),
                row.get(id_col) if id_col else None,
            ),
            axis=1,
        )
    return work


def _sanitize_salesrep_business_dataset(df, dataset: str) -> pd.DataFrame:
    work = _normalize_frame(df)
    if work.empty:
        return work
    token = str(dataset or "").strip().lower()
    work = work.copy()

    if token in {"summary", "all"} and "rep_name" in work.columns:
        work["rep_name"] = work.apply(
            lambda row: _business_rep_name(row.get("rep_name"), row.get("rep_id") or row.get("rep_key")),
            axis=1,
        )

    if token in {"customers"}:
        work = _sanitize_business_rep_columns(
            work,
            (
                ("account_owner_name", "account_owner_id"),
                ("last_sales_rep_name", "last_sales_rep_id"),
            ),
        )
        work = work.drop(columns=["account_owner_id", "last_sales_rep_id"], errors="ignore")

    if token in {"history", "all_history"}:
        work = _sanitize_business_rep_columns(
            work,
            (
                ("rep_name", "rep_key"),
                ("historical_rep_name", "historical_rep_id"),
                ("current_owner_name", "current_owner_id"),
                ("transaction_rep_name", "transaction_rep_id"),
                ("last_sales_rep_name", "last_sales_rep_id"),
            ),
        )
        work = work.drop(
            columns=[
                "rep_key",
                "historical_rep_id",
                "current_owner_id",
                "transaction_rep_id",
                "last_sales_rep_id",
            ],
            errors="ignore",
        )

    return work


def build_salesreps_export_frame(filters: Any, scope: Dict[str, Any], args: Any):
    context = _salesrep_attribution_context(filters, scope, args)
    if context.get("error"):
        raise RuntimeError(context["error"]["message"])

    rollup_df = fact_store.execute_sql_df(
        _rollup_sql(context["cte_sql"]),
        context["params"],
        tag="salesreps.export.rollup",
    )
    rollup_df = _normalize_frame(rollup_df)
    if rollup_df.empty:
        return rollup_df

    search_term = _search_term(args)
    sort_by, sort_dir = _sort_params(args)

    work = _apply_search_filter(rollup_df, search_term)
    work = _sort_rollup_df(work, sort_by, sort_dir)
    if work is None or work.empty:
        return work

    work = _sanitize_salesrep_business_dataset(work, "summary").copy()
    if "replaced_rep_names" in work.columns:
        work["replaced_rep_names"] = work["replaced_rep_names"].map(_business_rep_csv)
    for src, dst in (
        ("margin_pct", "margin_pct_export"),
        ("top_customer_share", "top_customer_share_pct_export"),
        ("top_5_customer_share", "top_5_customer_share_pct_export"),
        ("mom_revenue_pct", "mom_revenue_pct_export"),
        ("mom_profit_pct", "mom_profit_pct_export"),
    ):
        if src in work.columns:
            if src in {"top_customer_share", "top_5_customer_share"}:
                work[dst] = work[src] * 100.0
            else:
                work[dst] = work[src]
        else:
            work[dst] = None

    ordered = [
        ("rep_name", "Rep Name"),
        ("revenue", "Revenue"),
        ("profit", "Profit"),
        ("margin_pct_export", "Margin %"),
        ("orders", "Orders"),
        ("customers", "Customers"),
        ("active_customers", "Active Customers"),
        ("current_owned_customers", "Current Owned Customers"),
        ("inherited_customers", "Inherited Customers"),
        ("gained_customers", "Gained Customers"),
        ("lost_customers", "Lost Customers"),
        ("transferred_in_revenue", "Transferred In Revenue"),
        ("transferred_out_revenue", "Transferred Out Revenue"),
        ("current_owner_revenue", "Current Owner Revenue"),
        ("historical_revenue", "Historical Revenue"),
        ("ownership_delta_revenue", "Ownership Delta Revenue"),
        ("weight_lb", "Weight (lb)"),
        ("units", "Units"),
        ("asp_lb", "ASP/LB"),
        ("asp", "ASP"),
        ("avg_order_value", "Avg Order Value"),
        ("revenue_per_customer", "Revenue Per Customer"),
        ("top_territory_name", "Top Territory"),
        ("replaced_rep_names", "Replaced Reps"),
        ("top_protein_family", "Top Protein"),
        ("top_protein_revenue", "Top Protein Revenue"),
        ("top_customer_share_pct_export", "Top Customer %"),
        ("top_customer_name", "Top Customer Name"),
        ("top_customer_revenue", "Top Customer Revenue"),
        ("mom_revenue_pct_export", "MoM Revenue %"),
        ("mom_profit_pct_export", "MoM Profit %"),
        ("top_5_customer_share_pct_export", "Top 5 Customer %"),
        ("customer_hhi", "Concentration HHI"),
    ]
    export_df = work.reindex(columns=[src for src, _ in ordered]).rename(columns={src: dst for src, dst in ordered})
    return export_df


def build_salesrep_history_frame(rep_id: str, filters: Any, scope: Dict[str, Any], args: Any):
    scoped_sql, params_rep, _, _ = _salesrep_export_context(rep_id, filters, scope, args)
    return _sanitize_salesrep_business_dataset(_salesrep_history_frame(scoped_sql, params_rep), "history")


def _salesrep_export_context(
    rep_id: str,
    filters: Any,
    scope: Dict[str, Any],
    args: Any | None = None,
) -> tuple[str, list[Any], str | None, str | None]:
    context = _salesrep_rep_context(rep_id, filters, scope, args or {})
    if context.get("error"):
        raise RuntimeError(context["error"]["message"])
    windows = context["windows"]
    return (
        context["cte_sql"],
        list(context["params"]),
        windows.get("window_start"),
        windows.get("window_end_display") or windows.get("window_end_exclusive"),
    )


def _salesrep_summary_frame(scoped_sql: str, params_rep: list[Any]) -> Any:
    sql = f"""
        WITH
        {scoped_sql},
        ref AS (
            SELECT COALESCE(MAX(order_date), CURRENT_DATE) AS ref_date FROM scoped
        ),
        customer_rollup AS (
            SELECT
                customer_id,
                ANY_VALUE(customer_name) AS customer_name,
                ANY_VALUE(current_owner_id) AS current_owner_id,
                ANY_VALUE(current_owner_name) AS current_owner_name,
                ANY_VALUE(last_sales_rep_id) AS last_sales_rep_id,
                ANY_VALUE(last_sales_rep_name) AS last_sales_rep_name,
                MAX(last_sale_date) AS last_sale_date,
                MAX(owner_missing) AS owner_missing,
                MAX(inherited_flag) AS inherited_flag,
                SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END) AS revenue,
                SUM(CASE WHEN is_prior_window = 1 THEN revenue ELSE 0 END) AS prior_revenue,
                SUM(CASE WHEN is_yoy_window = 1 THEN revenue ELSE 0 END) AS yoy_revenue
            FROM rep_scope
            WHERE customer_id IS NOT NULL AND customer_id <> ''
            GROUP BY 1
        ),
        summary AS (
            SELECT
                MIN(rep_name) AS rep_name,
                SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END) AS revenue,
                SUM(CASE WHEN is_current_window = 1 THEN cost END) AS cost,
                SUM(CASE WHEN is_current_window = 1 THEN profit END) AS profit,
                CASE
                    WHEN SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END) > 0
                         AND SUM(CASE WHEN is_current_window = 1 THEN profit END) IS NOT NULL
                    THEN SUM(CASE WHEN is_current_window = 1 THEN profit END)
                        / NULLIF(SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END), 0) * 100
                    ELSE NULL
                END AS margin_pct,
                COUNT(DISTINCT CASE WHEN is_current_window = 1 THEN order_id END) AS orders,
                COUNT(DISTINCT CASE WHEN is_current_window = 1 THEN customer_id END) AS customers,
                COUNT(DISTINCT CASE WHEN is_current_window = 1 THEN product_id END) AS products,
                SUM(CASE WHEN is_current_window = 1 THEN units ELSE 0 END) AS units,
                SUM(CASE WHEN is_current_window = 1 THEN weight_lb ELSE 0 END) AS weight_lb,
                CASE
                    WHEN SUM(CASE WHEN is_current_window = 1 THEN units ELSE 0 END) > 0
                    THEN SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END)
                        / NULLIF(SUM(CASE WHEN is_current_window = 1 THEN units ELSE 0 END), 0)
                    ELSE NULL
                END AS asp,
                CASE
                    WHEN SUM(CASE WHEN is_current_window = 1 THEN weight_lb ELSE 0 END) > 0
                    THEN SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END)
                        / NULLIF(SUM(CASE WHEN is_current_window = 1 THEN weight_lb ELSE 0 END), 0)
                    ELSE NULL
                END AS asp_lb,
                MIN(CASE WHEN is_current_window = 1 THEN order_date END) AS first_order_date,
                MAX(CASE WHEN is_current_window = 1 THEN order_date END) AS last_order_date,
                SUM(CASE WHEN is_current_window = 1 AND order_date > ref.ref_date - INTERVAL 30 DAY THEN revenue ELSE 0 END) AS revenue_last_30,
                SUM(CASE WHEN is_current_window = 1 AND order_date > ref.ref_date - INTERVAL 90 DAY THEN revenue ELSE 0 END) AS revenue_last_90,
                SUM(CASE WHEN is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 90 DAY AND order_date > ref.ref_date - INTERVAL 180 DAY THEN revenue ELSE 0 END) AS revenue_prev_90,
                SUM(CASE WHEN is_current_window = 1 AND order_date > ref.ref_date - INTERVAL 30 DAY THEN cost ELSE 0 END) AS cost_last_30,
                SUM(CASE WHEN is_current_window = 1 AND order_date > ref.ref_date - INTERVAL 90 DAY THEN cost ELSE 0 END) AS cost_last_90,
                COUNT(DISTINCT CASE WHEN is_current_window = 1 AND order_date > ref.ref_date - INTERVAL 30 DAY THEN order_id END) AS orders_last_30,
                COUNT(DISTINCT CASE WHEN is_current_window = 1 AND order_date > ref.ref_date - INTERVAL 90 DAY THEN order_id END) AS orders_last_90,
                SUM(CASE WHEN is_current_window = 1 AND cost IS NULL THEN 1 ELSE 0 END) AS cost_null_rows,
                COUNT(CASE WHEN is_current_window = 1 THEN 1 END) AS total_rows,
                DATE_DIFF('day', MAX(CASE WHEN is_current_window = 1 THEN order_date END), MAX(ref.ref_date)) AS days_since_last,
                SUM(CASE WHEN is_prior_window = 1 THEN revenue ELSE 0 END) AS prior_revenue,
                SUM(CASE WHEN is_prior_window = 1 THEN profit END) AS prior_profit,
                SUM(CASE WHEN is_yoy_window = 1 THEN revenue ELSE 0 END) AS yoy_revenue,
                SUM(CASE WHEN is_yoy_window = 1 THEN profit END) AS yoy_profit,
                SUM(CASE WHEN is_current_window = 1 AND current_owner_id = rep_key THEN revenue ELSE 0 END) AS current_owner_revenue,
                SUM(CASE WHEN is_current_window = 1 AND historical_rep_id = rep_key THEN revenue ELSE 0 END) AS historical_revenue,
                SUM(CASE WHEN is_current_window = 1 AND current_owner_id = rep_key AND inherited_flag = 1 THEN revenue ELSE 0 END) AS transferred_in_revenue,
                SUM(CASE WHEN is_current_window = 1 AND historical_rep_id = rep_key AND ownership_changed = 1 THEN revenue ELSE 0 END) AS transferred_out_revenue,
                COUNT(DISTINCT CASE WHEN is_current_window = 1 AND current_owner_id = rep_key THEN customer_id END) AS current_owned_customers,
                COUNT(DISTINCT CASE WHEN is_current_window = 1 AND owner_missing = 1 THEN customer_id END) AS unassigned_customers,
                COUNT(DISTINCT CASE WHEN is_current_window = 1 AND owner_source = 'ownership_bridge' THEN customer_id END) AS bridge_customers,
                COUNT(DISTINCT CASE WHEN is_current_window = 1 AND owner_source = 'fact_customer_snapshot' THEN customer_id END) AS snapshot_customers,
                COUNT(DISTINCT CASE WHEN is_current_window = 1 AND owner_source = 'fact_current_owner' THEN customer_id END) AS fact_fallback_customers
            FROM rep_scope, ref
        ),
        portfolio AS (
            SELECT
                COUNT(DISTINCT CASE WHEN revenue > 0 THEN customer_id END) AS active_customers_curr,
                COUNT(DISTINCT CASE WHEN prior_revenue > 0 THEN customer_id END) AS active_customers_prev,
                COUNT(DISTINCT CASE WHEN revenue > 0 AND prior_revenue = 0 THEN customer_id END) AS gained_customers,
                COUNT(DISTINCT CASE WHEN revenue = 0 AND prior_revenue > 0 THEN customer_id END) AS lost_customers,
                COUNT(DISTINCT CASE WHEN revenue > 0 AND inherited_flag = 1 THEN customer_id END) AS inherited_customers,
                SUM(CASE WHEN revenue > 0 AND inherited_flag = 1 THEN revenue ELSE 0 END) AS inherited_revenue
            FROM customer_rollup
        ),
        monthly AS (
            SELECT
                DATE_TRUNC('month', order_date) AS month_start,
                SUM(revenue) AS revenue,
                SUM(cost) AS cost,
                SUM(profit) AS profit,
                CASE WHEN SUM(revenue) > 0 AND SUM(profit) IS NOT NULL THEN SUM(profit) / NULLIF(SUM(revenue), 0) * 100 ELSE NULL END AS margin_pct,
                COUNT(DISTINCT order_id) AS orders,
                COUNT(DISTINCT customer_id) AS customers,
                SUM(units) AS units,
                SUM(weight_lb) AS weight_lb
            FROM scoped
            GROUP BY 1
            ORDER BY 1
        ),
        weekly AS (
            SELECT
                DATE_TRUNC('week', order_date) AS week_start,
                SUM(revenue) AS revenue,
                SUM(cost) AS cost,
                SUM(profit) AS profit,
                CASE WHEN SUM(revenue) > 0 AND SUM(profit) IS NOT NULL THEN SUM(profit) / NULLIF(SUM(revenue), 0) * 100 ELSE NULL END AS margin_pct,
                COUNT(DISTINCT order_id) AS orders,
                COUNT(DISTINCT customer_id) AS customers,
                SUM(units) AS units,
                SUM(weight_lb) AS weight_lb
            FROM scoped
            GROUP BY 1
            ORDER BY 1
        ),
        monthly_compare_rows AS (
            SELECT
                DATE_TRUNC('month', order_date) AS aligned_month,
                CAST(revenue AS DOUBLE) AS revenue_current,
                NULL::DOUBLE AS revenue_yoy,
                CAST(profit AS DOUBLE) AS profit_current,
                NULL::DOUBLE AS profit_yoy,
                CAST(weight_lb AS DOUBLE) AS weight_current,
                NULL::DOUBLE AS weight_lb_yoy
            FROM rep_scope
            WHERE is_current_window = 1
            UNION ALL
            SELECT
                DATE_TRUNC('month', order_date + INTERVAL 1 YEAR) AS aligned_month,
                NULL::DOUBLE AS revenue_current,
                CAST(revenue AS DOUBLE) AS revenue_yoy,
                NULL::DOUBLE AS profit_current,
                CAST(profit AS DOUBLE) AS profit_yoy,
                NULL::DOUBLE AS weight_current,
                CAST(weight_lb AS DOUBLE) AS weight_lb_yoy
            FROM rep_scope
            WHERE is_yoy_window = 1
        ),
        monthly_compare AS (
            SELECT
                aligned_month,
                SUM(revenue_current) AS revenue,
                SUM(revenue_yoy) AS revenue_yoy,
                SUM(profit_current) AS profit,
                SUM(profit_yoy) AS profit_yoy,
                SUM(weight_current) AS weight_lb,
                SUM(weight_lb_yoy) AS weight_lb_yoy
            FROM monthly_compare_rows
            GROUP BY 1
            ORDER BY 1
        )
        SELECT
            summary.*,
            portfolio.*,
            (SELECT list(strftime('%Y-%m', month_start)) FROM monthly) AS trend_labels,
            (SELECT list(revenue) FROM monthly) AS trend_revenue,
            (SELECT list(orders) FROM monthly) AS trend_orders,
            (SELECT list(profit) FROM monthly) AS trend_profit,
            (SELECT list(margin_pct) FROM monthly) AS trend_margin,
            (SELECT list(customers) FROM monthly) AS trend_customers,
            (SELECT list(units) FROM monthly) AS trend_units,
            (SELECT list(strftime('%Y-%m-%d', week_start)) FROM weekly) AS trend_week_labels,
            (SELECT list(revenue) FROM weekly) AS trend_week_revenue,
            (SELECT list(orders) FROM weekly) AS trend_week_orders,
            (SELECT list(profit) FROM weekly) AS trend_week_profit,
            (SELECT list(margin_pct) FROM weekly) AS trend_week_margin,
            (SELECT list(strftime('%Y-%m', aligned_month)) FROM monthly_compare) AS compare_labels,
            (SELECT list(revenue) FROM monthly_compare) AS compare_revenue,
            (SELECT list(revenue_yoy) FROM monthly_compare) AS compare_revenue_yoy,
            (SELECT list(profit) FROM monthly_compare) AS compare_profit,
            (SELECT list(profit_yoy) FROM monthly_compare) AS compare_profit_yoy,
            (SELECT list(weight_lb) FROM monthly_compare) AS compare_weight_lb,
            (SELECT list(weight_lb_yoy) FROM monthly_compare) AS compare_weight_lb_yoy,
            (SELECT ref_date FROM ref) AS ref_date
        FROM summary
        CROSS JOIN portfolio
        LIMIT 1
    """
    return fact_store.execute_sql_df(sql, params_rep, tag="salesreps.export.summary")


def _salesrep_trend_frame(scoped_sql: str, params_rep: list[Any]) -> Any:
    sql = f"""
        WITH
        {scoped_sql}
        SELECT
            strftime('%Y-%m', order_date) AS month,
            SUM(revenue) AS revenue,
            SUM(profit) AS profit,
            CASE WHEN SUM(revenue) > 0 AND SUM(profit) IS NOT NULL THEN SUM(profit) / NULLIF(SUM(revenue), 0) * 100 ELSE NULL END AS margin_pct,
            COUNT(DISTINCT order_id) AS orders,
            COUNT(DISTINCT customer_id) AS customers,
            COUNT(DISTINCT product_id) AS products,
            SUM(units) AS units,
            SUM(weight_lb) AS weight_lb
        FROM scoped
        GROUP BY 1
        ORDER BY 1
    """
    return fact_store.execute_sql_df(sql, params_rep, tag="salesreps.export.trend")


def _salesrep_customers_frame(scoped_sql: str, params_rep: list[Any]) -> Any:
    sql = f"""
        WITH
        {scoped_sql},
        ref AS (
            SELECT COALESCE(MAX(order_date), CURRENT_DATE) AS ref_date FROM scoped
        )
        SELECT
            customer_id,
            ANY_VALUE(customer_name) AS customer_name,
            ANY_VALUE(current_owner_id) AS account_owner_id,
            ANY_VALUE(current_owner_name) AS account_owner_name,
            ANY_VALUE(last_sales_rep_id) AS last_sales_rep_id,
            ANY_VALUE(last_sales_rep_name) AS last_sales_rep_name,
            SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END) AS revenue,
            SUM(CASE WHEN is_current_window = 1 THEN profit END) AS profit,
            CASE
                WHEN SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END) > 0
                     AND SUM(CASE WHEN is_current_window = 1 THEN profit END) IS NOT NULL
                THEN SUM(CASE WHEN is_current_window = 1 THEN profit END)
                    / NULLIF(SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END), 0) * 100
                ELSE NULL
            END AS margin_pct,
            COUNT(DISTINCT CASE WHEN is_current_window = 1 THEN order_id END) AS orders,
            COUNT(DISTINCT CASE WHEN is_current_window = 1 THEN product_id END) AS products,
            SUM(CASE WHEN is_current_window = 1 THEN units ELSE 0 END) AS units,
            SUM(CASE WHEN is_current_window = 1 THEN weight_lb ELSE 0 END) AS weight_lb,
            CASE
                WHEN SUM(CASE WHEN is_current_window = 1 THEN weight_lb ELSE 0 END) > 0
                THEN SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END)
                    / NULLIF(SUM(CASE WHEN is_current_window = 1 THEN weight_lb ELSE 0 END), 0)
                ELSE NULL
            END AS asp_lb,
            MAX(CASE WHEN is_current_window = 1 THEN order_date END) AS last_order_date,
            MAX(last_sale_date) AS last_sale_date,
            MAX(owner_source) AS owner_source,
            MAX(owner_missing) AS owner_missing,
            MAX(inherited_flag) AS inherited_flag,
            SUM(CASE WHEN is_yoy_window = 1 THEN revenue ELSE 0 END) AS yoy_revenue,
            SUM(CASE WHEN is_current_window = 1 AND order_date > ref.ref_date - INTERVAL 30 DAY THEN revenue ELSE 0 END) AS revenue_last_30,
            SUM(CASE WHEN is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY THEN revenue ELSE 0 END) AS revenue_prev_30,
            (
                SUM(CASE WHEN is_current_window = 1 AND order_date > ref.ref_date - INTERVAL 30 DAY THEN revenue ELSE 0 END)
                - SUM(CASE WHEN is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY THEN revenue ELSE 0 END)
            ) AS mom_revenue_delta,
            CASE
                WHEN SUM(CASE WHEN is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY THEN revenue ELSE 0 END) > 0
                THEN (
                    SUM(CASE WHEN is_current_window = 1 AND order_date > ref.ref_date - INTERVAL 30 DAY THEN revenue ELSE 0 END)
                    - SUM(CASE WHEN is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY THEN revenue ELSE 0 END)
                )
                / NULLIF(SUM(CASE WHEN is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY THEN revenue ELSE 0 END), 0) * 100
                ELSE NULL
            END AS mom_revenue_pct,
            CASE
                WHEN SUM(CASE WHEN is_yoy_window = 1 THEN revenue ELSE 0 END) > 0
                THEN (
                    SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END)
                    - SUM(CASE WHEN is_yoy_window = 1 THEN revenue ELSE 0 END)
                ) / NULLIF(SUM(CASE WHEN is_yoy_window = 1 THEN revenue ELSE 0 END), 0) * 100
                ELSE NULL
            END AS yoy_revenue_pct
        FROM rep_scope, ref
        WHERE customer_id IS NOT NULL AND customer_id <> ''
        GROUP BY 1
        ORDER BY revenue DESC NULLS LAST, customer_id
    """
    return fact_store.execute_sql_df(sql, params_rep, tag="salesreps.export.customers")


def _salesrep_products_frame(scoped_sql: str, params_rep: list[Any]) -> Any:
    sql = f"""
        WITH
        {scoped_sql},
        ref AS (
            SELECT COALESCE(MAX(order_date), CURRENT_DATE) AS ref_date FROM scoped
        )
        SELECT
            CAST(product_id AS VARCHAR) AS product_id,
            CAST(MIN(product_name) AS VARCHAR) AS product_name,
            CAST(MIN(protein_family) AS VARCHAR) AS protein_family,
            CAST(MIN(category_name) AS VARCHAR) AS category_name,
            CAST(SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END) AS DOUBLE) AS revenue,
            CAST(SUM(CASE WHEN is_current_window = 1 THEN profit END) AS DOUBLE) AS profit,
            CAST(CASE
                WHEN SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END) > 0
                     AND SUM(CASE WHEN is_current_window = 1 THEN profit END) IS NOT NULL
                THEN SUM(CASE WHEN is_current_window = 1 THEN profit END)
                    / NULLIF(SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END), 0) * 100
                ELSE NULL
            END AS DOUBLE) AS margin_pct,
            CAST(COUNT(DISTINCT CASE WHEN is_current_window = 1 THEN order_id END) AS BIGINT) AS orders,
            CAST(COUNT(DISTINCT CASE WHEN is_current_window = 1 THEN customer_id END) AS BIGINT) AS customers,
            CAST(SUM(CASE WHEN is_current_window = 1 THEN units ELSE 0 END) AS DOUBLE) AS units,
            CAST(SUM(CASE WHEN is_current_window = 1 THEN weight_lb ELSE 0 END) AS DOUBLE) AS weight_lb,
            CAST(CASE
                WHEN SUM(CASE WHEN is_current_window = 1 THEN weight_lb ELSE 0 END) > 0
                THEN SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END)
                    / NULLIF(SUM(CASE WHEN is_current_window = 1 THEN weight_lb ELSE 0 END), 0)
                ELSE NULL
            END AS DOUBLE) AS asp_lb,
            CAST(MAX(CASE WHEN is_current_window = 1 THEN order_date END) AS DATE) AS last_order_date,
            CAST(SUM(CASE WHEN is_yoy_window = 1 THEN revenue ELSE 0 END) AS DOUBLE) AS yoy_revenue,
            CAST(SUM(CASE WHEN is_current_window = 1 AND order_date > ref.ref_date - INTERVAL 30 DAY THEN revenue ELSE 0 END) AS DOUBLE) AS revenue_last_30,
            CAST(SUM(CASE WHEN is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY THEN revenue ELSE 0 END) AS DOUBLE) AS revenue_prev_30,
            CAST((
                SUM(CASE WHEN is_current_window = 1 AND order_date > ref.ref_date - INTERVAL 30 DAY THEN revenue ELSE 0 END)
                - SUM(CASE WHEN is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY THEN revenue ELSE 0 END)
            ) AS DOUBLE) AS mom_revenue_delta,
            CAST(CASE
                WHEN SUM(CASE WHEN is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY THEN revenue ELSE 0 END) > 0
                THEN (
                    SUM(CASE WHEN is_current_window = 1 AND order_date > ref.ref_date - INTERVAL 30 DAY THEN revenue ELSE 0 END)
                    - SUM(CASE WHEN is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY THEN revenue ELSE 0 END)
                )
                / NULLIF(SUM(CASE WHEN is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY THEN revenue ELSE 0 END), 0) * 100
                ELSE NULL
            END AS DOUBLE) AS mom_revenue_pct,
            CAST(CASE WHEN SUM(weight_lb) FILTER (WHERE is_current_window = 1 AND order_date > ref.ref_date - INTERVAL 30 DAY) > 0
                 THEN SUM(revenue) FILTER (WHERE is_current_window = 1 AND order_date > ref.ref_date - INTERVAL 30 DAY)
                      / NULLIF(SUM(weight_lb) FILTER (WHERE is_current_window = 1 AND order_date > ref.ref_date - INTERVAL 30 DAY), 0)
                 ELSE NULL
            END AS DOUBLE) AS asp_lb_last_30,
            CAST(CASE WHEN SUM(weight_lb) FILTER (WHERE is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY) > 0
                 THEN SUM(revenue) FILTER (WHERE is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY)
                      / NULLIF(SUM(weight_lb) FILTER (WHERE is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY), 0)
                 ELSE NULL
            END AS DOUBLE) AS asp_lb_prev_30,
            CAST(CASE
                WHEN SUM(weight_lb) FILTER (WHERE is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY) > 0
                     AND SUM(revenue) FILTER (WHERE is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY) > 0
                THEN (
                    (
                        SUM(revenue) FILTER (WHERE is_current_window = 1 AND order_date > ref.ref_date - INTERVAL 30 DAY)
                        / NULLIF(SUM(weight_lb) FILTER (WHERE is_current_window = 1 AND order_date > ref.ref_date - INTERVAL 30 DAY), 0)
                    ) - (
                        SUM(revenue) FILTER (WHERE is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY)
                        / NULLIF(SUM(weight_lb) FILTER (WHERE is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY), 0)
                    )
                ) / NULLIF(
                    SUM(revenue) FILTER (WHERE is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY)
                    / NULLIF(SUM(weight_lb) FILTER (WHERE is_current_window = 1 AND order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY), 0),
                    0
                ) * 100
                ELSE NULL
            END AS DOUBLE) AS price_change_pct,
            CAST(CASE
                WHEN SUM(CASE WHEN is_yoy_window = 1 THEN revenue ELSE 0 END) > 0
                THEN (
                    SUM(CASE WHEN is_current_window = 1 THEN revenue ELSE 0 END)
                    - SUM(CASE WHEN is_yoy_window = 1 THEN revenue ELSE 0 END)
                ) / NULLIF(SUM(CASE WHEN is_yoy_window = 1 THEN revenue ELSE 0 END), 0) * 100
                ELSE NULL
            END AS DOUBLE) AS yoy_revenue_pct
        FROM rep_scope, ref
        WHERE product_id IS NOT NULL AND product_id <> ''
        GROUP BY 1
        ORDER BY revenue DESC NULLS LAST, product_id
    """
    return fact_store.execute_sql_df(sql, params_rep, tag="salesreps.export.products")


def _salesrep_movers_customers_frame(scoped_sql: str, params_rep: list[Any]) -> Any:
    sql = f"""
        WITH
        {scoped_sql},
        ref AS (
            SELECT COALESCE(MAX(order_date), CURRENT_DATE) AS ref_date FROM scoped
        ),
        customer_rollup AS (
            SELECT
                customer_id,
                ANY_VALUE(customer_name) AS customer_name,
                SUM(CASE WHEN order_date > ref.ref_date - INTERVAL 30 DAY THEN revenue ELSE 0 END) AS revenue_last_30,
                SUM(CASE WHEN order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY THEN revenue ELSE 0 END) AS revenue_prev_30
            FROM scoped, ref
            WHERE customer_id IS NOT NULL AND customer_id <> ''
            GROUP BY 1
        )
        SELECT
            customer_id,
            customer_name,
            revenue_last_30,
            revenue_prev_30,
            revenue_last_30 - revenue_prev_30 AS delta_revenue,
            CASE WHEN revenue_prev_30 > 0 THEN (revenue_last_30 - revenue_prev_30) / NULLIF(revenue_prev_30, 0) * 100 ELSE NULL END AS delta_revenue_pct
        FROM customer_rollup
        WHERE revenue_last_30 <> 0 OR revenue_prev_30 <> 0
        ORDER BY delta_revenue DESC NULLS LAST, customer_id
    """
    return fact_store.execute_sql_df(sql, params_rep, tag="salesreps.export.movers_customers")


def _salesrep_movers_products_frame(scoped_sql: str, params_rep: list[Any]) -> Any:
    sql = f"""
        WITH
        {scoped_sql},
        ref AS (
            SELECT COALESCE(MAX(order_date), CURRENT_DATE) AS ref_date FROM scoped
        ),
        product_rollup AS (
            SELECT
                product_id,
                ANY_VALUE(product_name) AS product_name,
                SUM(CASE WHEN order_date > ref.ref_date - INTERVAL 30 DAY THEN revenue ELSE 0 END) AS revenue_last_30,
                SUM(CASE WHEN order_date <= ref.ref_date - INTERVAL 30 DAY AND order_date > ref.ref_date - INTERVAL 60 DAY THEN revenue ELSE 0 END) AS revenue_prev_30
            FROM scoped, ref
            WHERE product_id IS NOT NULL AND product_id <> ''
            GROUP BY 1
        )
        SELECT
            product_id,
            product_name,
            revenue_last_30,
            revenue_prev_30,
            revenue_last_30 - revenue_prev_30 AS delta_revenue,
            CASE WHEN revenue_prev_30 > 0 THEN (revenue_last_30 - revenue_prev_30) / NULLIF(revenue_prev_30, 0) * 100 ELSE NULL END AS delta_revenue_pct
        FROM product_rollup
        WHERE revenue_last_30 <> 0 OR revenue_prev_30 <> 0
        ORDER BY delta_revenue DESC NULLS LAST, product_id
    """
    return fact_store.execute_sql_df(sql, params_rep, tag="salesreps.export.movers_products")


def _salesrep_margin_risk_frame(scoped_sql: str, params_rep: list[Any]) -> Any:
    sql = f"""
        WITH
        {scoped_sql},
        product_rollup AS (
            SELECT
                product_id,
                ANY_VALUE(product_name) AS product_name,
                ANY_VALUE(protein_family) AS protein_family,
                ANY_VALUE(category_name) AS category_name,
                SUM(revenue) AS revenue,
                SUM(profit) AS profit,
                CASE WHEN SUM(revenue) > 0 AND SUM(profit) IS NOT NULL THEN SUM(profit) / NULLIF(SUM(revenue), 0) * 100 ELSE NULL END AS margin_pct,
                COUNT(DISTINCT order_id) AS orders,
                SUM(weight_lb) AS weight_lb,
                CASE WHEN SUM(weight_lb) > 0 THEN SUM(revenue) / NULLIF(SUM(weight_lb), 0) ELSE NULL END AS asp_lb
            FROM scoped
            WHERE product_id IS NOT NULL AND product_id <> ''
            GROUP BY 1
        )
        SELECT
            product_id,
            product_name,
            protein_family,
            category_name,
            revenue,
            profit,
            margin_pct,
            orders,
            weight_lb,
            asp_lb,
            CASE WHEN profit IS NOT NULL AND profit < 0 THEN 1 ELSE 0 END AS negative_margin_flag
        FROM product_rollup
        WHERE margin_pct IS NOT NULL OR (profit IS NOT NULL AND profit < 0)
        ORDER BY revenue DESC NULLS LAST, product_id
    """
    frame = fact_store.execute_sql_df(sql, params_rep, tag="salesreps.export.margin_risk")
    if frame.empty:
        return frame
    frame["cost"] = (
        pd.to_numeric(frame.get("revenue"), errors="coerce")
        - pd.to_numeric(frame.get("profit"), errors="coerce")
    )
    frame["effective_cost_basis"] = pd.to_numeric(frame.get("cost"), errors="coerce")
    frame["cost_lb"] = np.where(
        pd.to_numeric(frame.get("weight_lb"), errors="coerce") > 0,
        pd.to_numeric(frame.get("cost"), errors="coerce")
        / pd.to_numeric(frame.get("weight_lb"), errors="coerce"),
        np.nan,
    )
    frame["effective_cost_lb"] = pd.to_numeric(frame.get("cost_lb"), errors="coerce")
    frame = margin_rules.annotate_margin_frame(
        frame,
        protein_col="protein_family",
        category_col="category_name",
        revenue_col="revenue",
        cost_col="cost",
        profit_col="profit",
        margin_col="margin_pct",
        unit_cost_col="cost_lb",
        unit_price_col="asp_lb",
    )
    frame["leakage_to_target"] = pd.to_numeric(frame.get("profit_uplift_to_target"), errors="coerce").fillna(0.0)
    frame = frame[
        (
            pd.to_numeric(frame.get("margin_pct"), errors="coerce")
            < pd.to_numeric(frame.get("target_margin_pct"), errors="coerce")
        )
        | (pd.to_numeric(frame.get("profit"), errors="coerce") < 0)
    ].copy()
    return frame.sort_values(["leakage_to_target", "revenue"], ascending=[False, False])


def _salesrep_at_risk_customers_frame(
    scoped_sql: str, params_rep: list[Any], inactivity_days: int = 45
) -> Any:
    inactivity_days = max(7, min(int(inactivity_days), 365))
    sql = f"""
        WITH
        {scoped_sql},
        ref AS (
            SELECT COALESCE(MAX(order_date), CURRENT_DATE) AS ref_date FROM scoped
        ),
        customer_rollup AS (
            SELECT
                customer_id,
                ANY_VALUE(customer_name) AS customer_name,
                MAX(order_date) AS last_order_date,
                SUM(revenue) AS revenue,
                SUM(CASE WHEN order_date <= ref.ref_date - INTERVAL 90 DAY AND order_date > ref.ref_date - INTERVAL 180 DAY THEN revenue ELSE 0 END) AS prior_period_revenue,
                COUNT(DISTINCT order_id) AS orders
            FROM scoped, ref
            WHERE customer_id IS NOT NULL AND customer_id <> ''
            GROUP BY 1
        )
        SELECT
            customer_id,
            customer_name,
            last_order_date,
            DATE_DIFF('day', last_order_date, (SELECT ref_date FROM ref)) AS days_since_last_order,
            revenue,
            prior_period_revenue,
            orders
        FROM customer_rollup
        WHERE last_order_date < (SELECT ref_date FROM ref) - INTERVAL {inactivity_days} DAY
        ORDER BY prior_period_revenue DESC NULLS LAST, revenue DESC NULLS LAST, customer_id
    """
    return fact_store.execute_sql_df(sql, params_rep, tag="salesreps.export.at_risk")


def _salesrep_history_frame(scoped_sql: str, params_rep: list[Any]) -> Any:
    sql = f"""
        WITH
        {scoped_sql}
        SELECT
            rep_key,
            rep_name,
            historical_rep_id,
            historical_rep_name,
            current_owner_id,
            current_owner_name,
            transaction_rep_id,
            transaction_rep_name,
            last_sales_rep_id,
            last_sales_rep_name,
            last_sale_date,
            territory_id,
            territory_name,
            owner_source,
            owner_missing,
            ownership_changed,
            order_date,
            order_id,
            customer_id,
            customer_name,
            product_id,
            product_name,
            protein_family,
            category_name,
            revenue,
            cost,
            profit,
            units,
            weight_lb,
            missing_packs
        FROM scoped
        ORDER BY order_date DESC, order_id
    """
    return fact_store.execute_sql_df(sql, params_rep, tag="salesreps.export.history")


def _salesrep_mix_frame(products_df: Any) -> Any:
    if products_df is None or products_df.empty:
        return products_df
    mix = products_df.copy()
    revenue_series = mix.get("revenue")
    total_revenue = float(revenue_series.sum()) if revenue_series is not None else 0.0
    if total_revenue > 0:
        mix["share_pct"] = (mix["revenue"] / total_revenue) * 100.0
    else:
        mix["share_pct"] = None
    return mix


def _salesrep_metadata_frame(
    rep_id: str,
    summary_df: Any,
    filters: Any,
    dataset_version: str,
    export_type: str,
) -> pd.DataFrame:
    rep_name = _business_rep_name(None, rep_id)
    try:
        if summary_df is not None and not summary_df.empty:
            rep_name = _business_rep_name(summary_df.iloc[0].get("rep_name"), rep_id)
    except Exception:
        rep_name = _business_rep_name(None, rep_id)
    start = getattr(filters, "start", None)
    end = getattr(filters, "end", None)
    generated_at = pd.Timestamp.utcnow().isoformat()
    return pd.DataFrame(
        [
            {"key": "rep_name", "value": rep_name},
            {"key": "export_type", "value": export_type},
            {"key": "window_start", "value": str(start) if start is not None else ""},
            {"key": "window_end", "value": str(end) if end is not None else ""},
            {"key": "generated_at_utc", "value": generated_at},
            {"key": "dataset_version", "value": str(dataset_version or "")},
            {"key": "filters_json", "value": filters_service.canonical_json(filters)},
        ]
    )


def build_salesrep_export_metadata_frame(
    rep_id: str,
    filters: Any,
    scope: Dict[str, Any],
    export_type: str,
):
    scoped_sql, params_rep, _, _ = _salesrep_export_context(rep_id, filters, scope)
    summary_df = _salesrep_summary_frame(scoped_sql, params_rep)
    return _salesrep_metadata_frame(
        rep_id=rep_id,
        summary_df=summary_df if summary_df is not None else pd.DataFrame(),
        filters=filters,
        dataset_version=fact_store.cache_buster(),
        export_type=export_type,
    )


def build_salesrep_export_dataset(rep_id: str, filters: Any, scope: Dict[str, Any], args: Any, dataset: str):
    token = str(dataset or "all").strip().lower()
    aliases = {
        "customer": "customers",
        "cust": "customers",
        "product": "products",
        "prod": "products",
        "product_mix": "mix",
        "orders": "history",
        "summary_all": "summary",
        "movers_customer": "movers_customers",
        "movers_product": "movers_products",
        "margin_leakage": "margin_risk",
        "risk_margin": "margin_risk",
        "risk_at_risk": "at_risk",
        "atrisk": "at_risk",
    }
    token = aliases.get(token, token)
    scoped_sql, params_rep, _, _ = _salesrep_export_context(rep_id, filters, scope, args)
    inactivity_days = _clean_int(args.get("at_risk_days") if hasattr(args, "get") else None, 45)

    if token == "summary":
        return _sanitize_salesrep_business_dataset(_salesrep_summary_frame(scoped_sql, params_rep), token)
    if token == "trend":
        return _sanitize_salesrep_business_dataset(_salesrep_trend_frame(scoped_sql, params_rep), token)
    if token == "customers":
        return _sanitize_salesrep_business_dataset(_salesrep_customers_frame(scoped_sql, params_rep), token)
    if token == "products":
        return _sanitize_salesrep_business_dataset(_salesrep_products_frame(scoped_sql, params_rep), token)
    if token == "mix":
        products_df = _salesrep_products_frame(scoped_sql, params_rep)
        return _sanitize_salesrep_business_dataset(_salesrep_mix_frame(products_df), token)
    if token in {"history", "all_history"}:
        return _sanitize_salesrep_business_dataset(_salesrep_history_frame(scoped_sql, params_rep), token)
    if token == "movers_customers":
        return _sanitize_salesrep_business_dataset(_salesrep_movers_customers_frame(scoped_sql, params_rep), token)
    if token == "movers_products":
        return _sanitize_salesrep_business_dataset(_salesrep_movers_products_frame(scoped_sql, params_rep), token)
    if token == "margin_risk":
        return _sanitize_salesrep_business_dataset(_salesrep_margin_risk_frame(scoped_sql, params_rep), token)
    if token == "at_risk":
        return _sanitize_salesrep_business_dataset(
            _salesrep_at_risk_customers_frame(scoped_sql, params_rep, inactivity_days=inactivity_days),
            token,
        )

    raise ValueError(f"Unsupported export dataset: {dataset}")


def build_salesrep_export_sheets(rep_id: str, filters: Any, scope: Dict[str, Any], args: Any, include_history: bool = False):
    scoped_sql, params_rep, _, _ = _salesrep_export_context(rep_id, filters, scope, args)
    summary_df = _sanitize_salesrep_business_dataset(_salesrep_summary_frame(scoped_sql, params_rep), "summary")
    trend_df = _sanitize_salesrep_business_dataset(_salesrep_trend_frame(scoped_sql, params_rep), "trend")
    customers_df = _sanitize_salesrep_business_dataset(_salesrep_customers_frame(scoped_sql, params_rep), "customers")
    products_df = _sanitize_salesrep_business_dataset(_salesrep_products_frame(scoped_sql, params_rep), "products")
    mix_df = _sanitize_salesrep_business_dataset(_salesrep_mix_frame(products_df), "mix")
    movers_customers_df = _sanitize_salesrep_business_dataset(_salesrep_movers_customers_frame(scoped_sql, params_rep), "movers_customers")
    movers_products_df = _sanitize_salesrep_business_dataset(_salesrep_movers_products_frame(scoped_sql, params_rep), "movers_products")
    margin_risk_df = _sanitize_salesrep_business_dataset(_salesrep_margin_risk_frame(scoped_sql, params_rep), "margin_risk")
    inactivity_days = _clean_int(args.get("at_risk_days") if hasattr(args, "get") else None, 45)
    at_risk_df = _sanitize_salesrep_business_dataset(
        _salesrep_at_risk_customers_frame(scoped_sql, params_rep, inactivity_days=inactivity_days),
        "at_risk",
    )
    metadata_df = _salesrep_metadata_frame(
        rep_id,
        summary_df,
        filters,
        dataset_version=fact_store.cache_buster(),
        export_type="all",
    )

    sheets = {
        "Metadata": metadata_df,
        "Summary": summary_df,
        "Trend": trend_df,
        "Customers": customers_df,
        "Products": products_df,
        "Mix": mix_df,
        "Movers_Customers": movers_customers_df,
        "Movers_Products": movers_products_df,
        "Margin_Risk": margin_risk_df,
        "At_Risk_Customers": at_risk_df,
    }
    if include_history:
        sheets["History"] = _sanitize_salesrep_business_dataset(_salesrep_history_frame(scoped_sql, params_rep), "history")
    return sheets
