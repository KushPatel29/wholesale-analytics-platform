# app/services/analytics_utils.py
from __future__ import annotations

from typing import Any, Optional, Iterable, Dict, List, Tuple
import pandas as pd
import numpy as np
from . import fact_schema as fs

NUMERIC_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "Revenue": (
        "revenue_packs_only",
        "Revenue",
        "revenue_shipped",
        "revenue_ordered",
        "ExtRevenue",
        "ExtPrice",
        "ExtendedPrice",
        "Sales",
        "NetSales",
        "TotalSales",
        "LinesTotalPrice",
        "TotalPrice",
        "TotalRevenue",
    ),
    "Cost": (
        "cost_packs_only",
        "Cost",
        "cost_shipped",
        "cost_ordered",
        "CostPrice",
        "ExtCost",
        "ExtendedCost",
        "COGS",
        "TotalCost",
        "LineCost",
    ),
    "Profit": (
        "Profit",
        "gross_margin_shipped",
        "gross_margin_ordered",
        "GrossMargin",
        "Margin",
    ),
    "QuantityShipped": (
        "QuantityShipped",
        "Quantity_Shipped",
        "pack_item_count_sum",
        "Units",
        "QtyShipped",
    ),
    "QuantityOrdered": ("QuantityOrdered", "Quantity_Ordered", "QtyOrdered"),
}

REVENUE_CANDIDATES: Tuple[str, ...] = (
    "revenue_packs_only",
    "Revenue",
    "ExtRevenue",
    "ExtPrice",
    "ExtendedPrice",
    "Sales",
    "NetSales",
    "TotalSales",
    "LinesTotalPrice",
    "TotalPrice",
    "TotalRevenue",
    "revenue_shipped",
    "revenue_ordered",
)

COST_CANDIDATES: Tuple[str, ...] = (
    "cost_packs_only",
    "Cost",
    "ExtCost",
    "ExtendedCost",
    "COGS",
    "TotalCost",
    "LineCost",
    "ItemCost",
    "InventoryCost",
    "cost_shipped",
    "cost_ordered",
    "CostPrice",
)

COST_RATE_CANDIDATES: Tuple[str, ...] = (
    "CostPerUnit",
    "unit_cost_effective",
    "UnitCost",
    "CostPrice",
    "CostPrice_x",
    "CostPrice_y",
    "CostPrice_line",
    "StandardCost",
    "LandedCost",
)

COST_PER_LB_CANDIDATES: Tuple[str, ...] = (
    "CostPerLb",
    "CostPerLB",
    "CostPerPound",
    "CostPerPounds",
    "AvgCostPerLb",
    "AvgCostPerLB",
    "avg_cost_per_lb",
    "AverageCostPerLb",
    "CostPerWeight",
    "LandedCostPerLb",
    "LandedCostPerLB",
)

UNITS_CANDIDATES: Tuple[str, ...] = (
    "Units",
    "Qty",
    "Quantity",
    "ShippedQty",
    "ShipQty",
    "CaseQty",
    "ItemCount",
    "UnitsShipped",
    "QuantityShipped",
    "Quantity_Shipped",
    "QuantityOrdered",
    "Quantity_Ordered",
    "QtyOrdered",
    "QtyShipped",
    "UnitQty",
    "pack_item_count_sum",
)

WEIGHT_LB_CANDIDATES: Tuple[str, ...] = (
    "WeightLb",
    "ShippedLb",
    "ShipLb",
    "Lbs",
    "Pounds",
    "Weight",
    "pack_weight_lb_sum",
)

PRODUCT_ID_CANDIDATES: Tuple[str, ...] = (
    "ProductId",
    "ProductID",
    "Product Id",
    "Product_ID",
    "product_id",
    "SKU",
    "Sku",
    "SkuId",
    "SkuID",
    "ItemId",
    "ItemID",
    "ItemNumber",
    "Item",
    "ItemCode",
    "ProductCode",
)

REGION_NAME_CANDIDATES: Tuple[str, ...] = (
    "RegionName",
    "Region",
    "Region_Name",
    "Market",
    "Territory",
)

SALES_REP_ID_CANDIDATES: Tuple[str, ...] = (
    "SalesRepId",
    "SalesRepID",
    "RepId",
    "SalespersonId",
    "SalesPersonID",
    "OwnerId",
    "PrimarySalesRepId",
    "UserId",
)

SALES_REP_NAME_CANDIDATES: Tuple[str, ...] = (
    "SalesRepName",
    "SalesRep",
    "RepName",
    "Salesperson",
    "SalespersonName",
    "SalesPersonName",
    "Owner",
    "AccountOwner",
    "PrimarySalesRepName",
    "UserName",
    "User",
)
ORDER_ID_CANDIDATES: Tuple[str, ...] = (
    "OrderID",
    "OrderId",
    "OrderNo",
    "Invoice",
    "InvoiceNo",
    "ShipmentID",
    "ShipmentId",
)

CUSTOMER_ID_CANDIDATES: Tuple[str, ...] = (
    "CustomerID",
    "CustomerId",
    "CustID",
    "Customer",
    "CustomerNo",
)

PRODUCT_NAME_CANDIDATES: Tuple[str, ...] = (
    "ProductName",
    "ItemName",
    "SKUName",
    "Description",
    "Product",
    "Item",
    "SKU",
    "ProductDescription",
    "product_description",
)

REP_ID_CANDIDATES: Tuple[str, ...] = (
    "SalesRepId",
    "SalesRepID",
    "PrimarySalesRepId",
    "UserId",
    "UserID",
    "RepId",
    "RepID",
)
REP_NAME_CANDIDATES: Tuple[str, ...] = (
    "SalesRepName",
    "PrimarySalesRepName",
    "RepName",
    "UserName",
    "FullName",
    "DisplayName",
    "Name",
)

STRING_CANONICALS: Tuple[str, ...] = (
    "OrderId",
    "SalesRepId",
    "PrimarySalesRepId",
    "UserId",
    "CustomerId",
    "ProductId",
    "SupplierId",
    "RegionId",
    "SalesRepName",
    "PrimarySalesRepName",
    "CustomerName",
    "ProductName",
    "ProductDescription",
    "SupplierName",
    "RegionName",
    "ShippingMethodName",
    "OrderStatus",
)

DATE_PRIORITY_ORDER: Tuple[str, ...] = (
    "ShipDate",
    "DateShipped_line",
    "DateShipped_order",
    "DateShipped",
    "DateOrdered_line",
    "DateOrdered",
    "OrderDate",
    "InvoiceDate",
    "TxnDate",
    "Date",
)
ORDER_TS_PRIORITY: Tuple[str, ...] = ("DateOrdered_line", "DateOrdered", "Date")
SHIP_TS_PRIORITY: Tuple[str, ...] = (
    "ShipDate",
    "DateShipped_line",
    "DateShipped_order",
    "DateShipped",
)

EXPORT_PARTS: Tuple[str, ...] = (
    "trend",
    "top_customers",
    "top_products",
    "top_regions",
    "top_suppliers",
    "top_methods",
    "status_breakdown",
    "peer_ranking",
    "recent_orders",
    "kpis",
)

# Buckets used to summarize order status performance
STATUS_CLOSED_KEYWORDS: Tuple[str, ...] = (
    "ship",
    "shipp",
    "deliver",
    "complete",
    "closed",
    "fulfilled",
)
STATUS_CANCELLED_KEYWORDS: Tuple[str, ...] = (
    "cancel",
    "void",
    "reject",
    "return",
    "decline",
)

SENTINELS = {"all", "*", "__all__"}

SUPPLIER_NAME_ALIASES: Tuple[str, ...] = (
    "SupplierName",
    "Supplier Name",
    "Supplier_Name",
    "supplier_name",
    "supplier",
    "Supplier",
    "VendorName",
    "Vendor Name",
    "Vendor_Name",
    "vendor_name",
    "vendor",
    "Vendor",
)
SUPPLIER_ID_ALIASES: Tuple[str, ...] = (
    "SupplierId",
    "SupplierID",
    "Supplier Id",
    "Supplier_Id",
    "supplier_id",
    "supplierid",
    "SupplierCode",
    "suppliercode",
    "VendorId",
    "VendorID",
    "Vendor Id",
    "Vendor_Id",
    "vendor_id",
    "vendorid",
    "VendorCode",
    "vendorcode",
)


def _normalize_col_label(label: str) -> str:
    """Normalize a column label for comparison (case/whitespace/underscore agnostic)."""
    return "".join(ch for ch in str(label).lower() if ch.isalnum())


def best_column(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    """
    Return the first best matching column using exact, case-insensitive,
    then normalized (strip spaces/underscores) comparisons.
    """
    if df is None:
        return None
    cols = list(df.columns)
    lower_map: Dict[str, str] = {}
    norm_map: Dict[str, str] = {}
    for col in cols:
        lower_key = str(col).lower()
        norm_key = _normalize_col_label(col)
        if lower_key not in lower_map:
            lower_map[lower_key] = col
        if norm_key not in norm_map:
            norm_map[norm_key] = col
    for cand in candidates:
        if cand in df.columns:
            return cand
        cand_lower = str(cand).lower()
        if cand_lower in lower_map:
            return lower_map[cand_lower]
        cand_norm = _normalize_col_label(cand)
        if cand_norm in norm_map:
            return norm_map[cand_norm]
    return None


def first_notna(*values: Any) -> Any:
    for v in values:
        if v is None:
            continue
        try:
            if pd.isna(v):
                continue
        except Exception:
            pass
        return v
    return None

def find_first_column(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    """Find the first column in the DataFrame that exists in a list of candidates."""
    for col in candidates:
        if col in df.columns:
            return col
    return None


def revenue_column(df: pd.DataFrame, required: bool = False) -> Optional[str]:
    """
    Find the revenue column using robust matching.

    If `required` is True, raises ValueError when not found with the available column list.
    """
    col = fs.resolve_revenue_column(df) if df is not None else None
    if not col and df is not None:
        col = best_column(df, REVENUE_CANDIDATES)
    if required and not col:
        raise ValueError(f"Revenue column not found. Available columns: {list(df.columns) if df is not None else []}")
    return col


def region_column(df: pd.DataFrame) -> Optional[str]:
    """Find the region column."""
    return best_column(df, REGION_NAME_CANDIDATES) if df is not None else None


def normalize_datetime(
    value: Any,
) -> Any:  # Can return Series or Timestamp
    """Safely convert a value to a timezone-naive timestamp."""
    if isinstance(value, pd.Series):
        ts = pd.to_datetime(value, errors="coerce", utc=False)
        if ts.dt.tz is not None:
            try:
                ts = ts.dt.tz_localize(None)
            except Exception:
                # Already naive
                pass
        return ts

    if value is None or pd.isna(value):
        return None
    try:
        ts = pd.to_datetime(value, errors="coerce", utc=False)
        if pd.isna(ts):
            return None
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        return ts
    except (TypeError, ValueError):
        return None


def cost_column(df: pd.DataFrame) -> Optional[str]:
    """Find the cost column."""
    if df is None:
        return None
    col = fs.resolve_cost_column(df)
    if col:
        return col
    return best_column(df, COST_CANDIDATES)


def cost_rate_column(df: pd.DataFrame) -> Optional[str]:
    """Find the unit cost / cost price column."""
    if df is None:
        return None
    return fs.resolve_cost_rate_column(df)


def qty_column(df: pd.DataFrame) -> Optional[str]:
    """Find the quantity/units column (alias for units_column)."""
    return units_column(df)


def quantity_column(df: pd.DataFrame) -> Optional[str]:
    """Find the quantity/units column (alias for units_column for backward compatibility)."""
    return units_column(df)


def units_column(df: pd.DataFrame) -> Optional[str]:
    """Find the shipped/ordered units column."""
    if df is None:
        return None
    col = fs.resolve_qty_column(df)
    if col:
        return col
    return best_column(df, UNITS_CANDIDATES)


def weight_column(df: pd.DataFrame) -> Optional[str]:
    """Find the shipped weight (lbs) column."""
    return weight_lb_column(df)


def weight_lb_column(df: pd.DataFrame) -> Optional[str]:
    """Find the shipped weight (lbs) column."""
    if df is None:
        return None
    col = fs.resolve_weight_column(df)
    if col:
        return col
    return best_column(df, WEIGHT_LB_CANDIDATES)


def order_id_column(df: pd.DataFrame) -> Optional[str]:
    """Find the order id column."""
    return best_column(df, ORDER_ID_CANDIDATES) if df is not None else None


def customer_id_column(df: pd.DataFrame) -> Optional[str]:
    """Find the customer id column."""
    return best_column(df, CUSTOMER_ID_CANDIDATES) if df is not None else None


def sales_rep_id_column(df: pd.DataFrame) -> Optional[str]:
    """Find the sales rep id column."""
    return best_column(df, SALES_REP_ID_CANDIDATES) if df is not None else None


def sales_rep_name_column(df: pd.DataFrame) -> Optional[str]:
    """Find the sales rep name column."""
    return best_column(df, SALES_REP_NAME_CANDIDATES) if df is not None else None


def product_name_column(df: pd.DataFrame) -> Optional[str]:
    """Find the product name/description column."""
    return best_column(df, PRODUCT_NAME_CANDIDATES) if df is not None else None


def product_id_column(df: pd.DataFrame) -> Optional[str]:
    """Find the product id/sku column."""
    return best_column(df, PRODUCT_ID_CANDIDATES) if df is not None else None


def resolve_column(
    df: pd.DataFrame, candidates: Iterable[str], fallback: Optional[str] = None
) -> Optional[str]:
    """Find the first column in the DataFrame that exists, with a fallback."""
    for col in candidates:
        if col in df.columns:
            return col
    return fallback


def to_numeric_safe(series: pd.Series) -> pd.Series:
    """Safely convert a series to numeric, coercing errors."""
    return pd.to_numeric(series, errors="coerce")


def safe_div(numerator: Any, denominator: Any) -> Any:
    """
    Safely divide, returning None when the denominator is zero or missing.

    Handles scalars and pandas Series. For Series, returns a Series with NaN where division is unsafe.
    """
    if isinstance(numerator, pd.Series) or isinstance(denominator, pd.Series):
        num_s = to_numeric_safe(numerator) if isinstance(numerator, pd.Series) else numerator
        den_s = to_numeric_safe(denominator) if isinstance(denominator, pd.Series) else denominator
        with np.errstate(divide="ignore", invalid="ignore"):
            result = num_s / den_s
        if isinstance(result, pd.Series):
            result = result.replace([np.inf, -np.inf], np.nan)
            return result
        return None if (pd.isna(result) or np.isinf(result)) else result

    if denominator is None:
        return None
    try:
        if pd.isna(denominator) or float(denominator) == 0.0:
            return None
    except Exception:
        pass
    try:
        value = numerator / denominator
    except Exception:
        return None
    try:
        if pd.isna(value) or np.isinf(value):
            return None
    except Exception:
        pass
    return value


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    try:
        if pd.isna(out) or np.isinf(out):
            return None
    except Exception:
        pass
    return out


def safe_profit(revenue: Any, cost: Any) -> Any:
    """
    Compute profit only when cost is present and positive.

    Returns a scalar or Series; invalid values become None/NaN.
    """
    if isinstance(revenue, pd.Series) or isinstance(cost, pd.Series):
        rev_s = to_numeric_safe(revenue) if isinstance(revenue, pd.Series) else revenue
        cost_s = to_numeric_safe(cost) if isinstance(cost, pd.Series) else cost
        profit = rev_s - cost_s
        if isinstance(profit, pd.Series):
            return profit.where(cost_s > 0)
        return None

    rev_val = _as_float(revenue)
    cost_val = _as_float(cost)
    if rev_val is None or cost_val is None:
        return None
    if cost_val <= 0:
        return None
    return rev_val - cost_val


def safe_roi_pct(
    profit: Any,
    cost: Any,
    *,
    clamp_min: Optional[float] = None,
    clamp_max: Optional[float] = None,
) -> Any:
    """
    Compute ROI percent only when cost is positive.

    Returns a scalar or Series; invalid values become None/NaN.
    """
    if isinstance(profit, pd.Series) or isinstance(cost, pd.Series):
        profit_s = to_numeric_safe(profit) if isinstance(profit, pd.Series) else profit
        cost_s = to_numeric_safe(cost) if isinstance(cost, pd.Series) else cost
        valid = cost_s > 0
        with np.errstate(divide="ignore", invalid="ignore"):
            pct = profit_s / cost_s * 100.0
        if isinstance(pct, pd.Series):
            if clamp_min is not None or clamp_max is not None:
                pct = pct.clip(lower=clamp_min, upper=clamp_max)
            return pct.where(valid, np.nan)
        return None

    profit_val = _as_float(profit)
    cost_val = _as_float(cost)
    if profit_val is None or cost_val is None or cost_val <= 0:
        return None
    pct = (profit_val / cost_val) * 100.0
    if clamp_min is not None and pct < clamp_min:
        pct = clamp_min
    if clamp_max is not None and pct > clamp_max:
        pct = clamp_max
    return pct


def safe_margin_pct(
    revenue: Any,
    cost: Any,
    *,
    clamp_min: Optional[float] = -200.0,
    clamp_max: Optional[float] = 200.0,
) -> Any:
    """
    Compute margin percent with defensive checks and optional clamping.

    Returns a scalar or Series; invalid values become None/NaN.
    """
    if isinstance(revenue, pd.Series) or isinstance(cost, pd.Series):
        rev_s = to_numeric_safe(revenue) if isinstance(revenue, pd.Series) else revenue
        cost_s = to_numeric_safe(cost) if isinstance(cost, pd.Series) else cost
        valid = (rev_s > 0) & (cost_s > 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            pct = (rev_s - cost_s) / rev_s * 100.0
        if isinstance(pct, pd.Series):
            if clamp_min is not None or clamp_max is not None:
                pct = pct.clip(lower=clamp_min, upper=clamp_max)
            return pct.where(valid, np.nan)
        return None

    rev_val = _as_float(revenue)
    cost_val = _as_float(cost)
    if rev_val is None or rev_val <= 0:
        return None
    if cost_val is None or cost_val <= 0:
        return None
    pct = (rev_val - cost_val) / rev_val * 100.0
    if clamp_min is not None and pct < clamp_min:
        pct = clamp_min
    if clamp_max is not None and pct > clamp_max:
        pct = clamp_max
    return pct


def safe_uplift_pct(
    current_price: Any,
    target_price: Any,
    *,
    clamp_min: Optional[float] = -100.0,
    clamp_max: Optional[float] = 200.0,
) -> Any:
    """
    Compute uplift percent only when current + target prices are positive.

    Returns a scalar or Series; invalid values become None/NaN.
    """
    if isinstance(current_price, pd.Series) or isinstance(target_price, pd.Series):
        cur_s = to_numeric_safe(current_price) if isinstance(current_price, pd.Series) else current_price
        tgt_s = to_numeric_safe(target_price) if isinstance(target_price, pd.Series) else target_price
        valid = (cur_s > 0) & (tgt_s > 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            pct = (tgt_s - cur_s) / cur_s * 100.0
        if isinstance(pct, pd.Series):
            if clamp_min is not None or clamp_max is not None:
                pct = pct.clip(lower=clamp_min, upper=clamp_max)
            return pct.where(valid, np.nan)
        return None

    cur_val = _as_float(current_price)
    tgt_val = _as_float(target_price)
    if cur_val is None or cur_val <= 0:
        return None
    if tgt_val is None or tgt_val <= 0:
        return None
    pct = (tgt_val - cur_val) / cur_val * 100.0
    if clamp_min is not None and pct < clamp_min:
        pct = clamp_min
    if clamp_max is not None and pct > clamp_max:
        pct = clamp_max
    return pct


def _coalesce_numeric_series(df: pd.DataFrame, candidates: Iterable[str]) -> pd.Series:
    """Coalesce multiple numeric columns into a single series, preferring non-zero values."""
    if df is None or df.empty:
        return pd.Series(dtype="float64")
    idx = df.index
    out = pd.Series(np.nan, index=idx, dtype="float64")
    for cand in candidates:
        if cand not in df.columns:
            continue
        series = to_numeric_safe(df[cand])
        if out.isna().all():
            out = series
            continue
        missing = out.isna() | (out == 0)
        fill_mask = missing & series.notna() & (series != 0)
        out = out.where(~fill_mask, series)
    return out


def effective_qty_for_cost(
    units: pd.Series,
    weight: pd.Series,
    *,
    uom: Optional[pd.Series] = None,
    item_count: Optional[pd.Series] = None,
) -> pd.Series:
    """Choose quantity basis for cost calculations with weight fallback."""
    units_s = to_numeric_safe(units).fillna(0.0) if isinstance(units, pd.Series) else pd.Series(0.0)
    weight_s = to_numeric_safe(weight).fillna(0.0) if isinstance(weight, pd.Series) else pd.Series(0.0)
    item_s = to_numeric_safe(item_count).fillna(0.0) if isinstance(item_count, pd.Series) else None
    if item_s is not None:
        item_s = item_s.where(item_s > 0)
    qty = units_s.copy()
    if uom is not None:
        uom_s = pd.Series(uom, index=units_s.index).astype("string").str.strip().str.lower()
        weight_mask = uom_s.isin({"3", "lb", "lbs", "pound", "pounds", "weight"}) | uom_s.str.contains("lb|pound", na=False)
        unit_mask = uom_s.str.contains("ea|each|case|cs|unit|pack|pkg", na=False)
        weight_mask = weight_mask & ~unit_mask
        if item_s is not None:
            qty = qty.where(~unit_mask | item_s.isna(), item_s)
        qty = qty.where(weight_mask, qty)
        qty = qty.where(~weight_mask, weight_s)
    if item_s is not None:
        qty = qty.where(qty > 0, item_s)
    fallback = (qty <= 0) & (weight_s > 0)
    qty = qty.where(~fallback, weight_s)
    return qty


def resolve_cost(
    df: pd.DataFrame,
    *,
    cost_col: Optional[str] = None,
    units_col: Optional[str] = None,
    weight_col: Optional[str] = None,
    uom_col: Optional[str] = None,
    cost_rate_cols: Optional[Iterable[str]] = None,
    cost_per_lb_cols: Optional[Iterable[str]] = None,
    preserve_units: bool = False,
) -> pd.Series:
    """
    Resolve line-level cost per row with fallbacks:
    1) line cost column
    2) cost per lb * weight
    3) cost per unit * effective qty
    Missing/zero costs return NaN (not 0).
    """
    if df is None or df.empty:
        return pd.Series(dtype="float64")

    resolved_cost_col = cost_col or cost_column(df)
    units_col = units_col or units_column(df)
    weight_col = weight_col or weight_lb_column(df)
    uom_col = uom_col if uom_col and uom_col in df.columns else best_column(
        df, ("UOM_UOMShortName", "UOM_UOMName", "UOMName", "UnitOfMeasure", "UnitOfBillingId")
    )

    resolved_rate_hint = None
    if resolved_cost_col and resolved_cost_col in COST_RATE_CANDIDATES:
        resolved_rate_hint = resolved_cost_col
        resolved_cost_col = None

    total_cost_cols = []
    if resolved_cost_col:
        total_cost_cols.append(resolved_cost_col)
    for cand in COST_CANDIDATES:
        if cand not in total_cost_cols and cand in df.columns:
            if resolved_rate_hint and cand == resolved_rate_hint:
                continue
            total_cost_cols.append(cand)

    cost_rate_cols_list = list(cost_rate_cols) if cost_rate_cols else []
    if resolved_rate_hint and resolved_rate_hint not in cost_rate_cols_list:
        cost_rate_cols_list.append(resolved_rate_hint)
    if not cost_rate_cols_list:
        rate_col = cost_rate_column(df)
        if rate_col:
            cost_rate_cols_list.append(rate_col)
        cost_rate_cols_list.extend([c for c in COST_RATE_CANDIDATES if c not in cost_rate_cols_list])

    cost_per_lb_cols_list = list(cost_per_lb_cols) if cost_per_lb_cols else []
    if not cost_per_lb_cols_list:
        cost_per_lb_cols_list.extend([c for c in COST_PER_LB_CANDIDATES if c not in cost_per_lb_cols_list])

    line_cost = _coalesce_numeric_series(df, total_cost_cols) if total_cost_cols else pd.Series(np.nan, index=df.index, dtype="float64")
    cost_per_lb = _coalesce_numeric_series(df, cost_per_lb_cols_list) if cost_per_lb_cols_list else pd.Series(np.nan, index=df.index, dtype="float64")
    cost_per_unit = _coalesce_numeric_series(df, cost_rate_cols_list) if cost_rate_cols_list else pd.Series(np.nan, index=df.index, dtype="float64")

    units_s = to_numeric_safe(df[units_col]) if units_col and units_col in df.columns else pd.Series(0.0, index=df.index, dtype="float64")
    weight_s = to_numeric_safe(df[weight_col]) if weight_col and weight_col in df.columns else pd.Series(0.0, index=df.index, dtype="float64")
    uom_s = df[uom_col] if uom_col and uom_col in df.columns else None
    item_col = best_column(df, ("ItemCount", "pack_item_count_sum", "item_count", "Item_Count", "PieceCount", "pack_piece_count_sum"))
    item_s = None
    if item_col and item_col in df.columns:
        item_s = to_numeric_safe(df[item_col]).fillna(0.0)
        if units_s.abs().sum() == 0:
            units_s = item_s
        else:
            units_s = units_s.where(units_s > 0, item_s)
    preserve_units = preserve_units or (units_col and str(units_col).startswith("_qty_"))
    if preserve_units:
        qty_for_cost = units_s.copy()
        fallback_mask = (qty_for_cost <= 0) & weight_s.notna() & (weight_s > 0)
        qty_for_cost = qty_for_cost.where(~fallback_mask, weight_s)
        if qty_for_cost.dropna().empty or (qty_for_cost <= 0).all():
            qty_for_cost = effective_qty_for_cost(units_s, weight_s, uom=uom_s, item_count=item_s)
    else:
        qty_for_cost = effective_qty_for_cost(units_s, weight_s, uom=uom_s, item_count=item_s)
    weight_uom_mask = None
    if uom_s is not None:
        uom_norm = pd.Series(uom_s, index=units_s.index).astype("string").str.strip().str.lower()
        weight_uom_mask = uom_norm.isin({"3", "lb", "lbs", "pound", "pounds", "weight"}) | uom_norm.str.contains("lb|pound", na=False)
        unit_uom_mask = uom_norm.str.contains("ea|each|case|cs|unit|pack|pkg", na=False)
        weight_uom_mask = weight_uom_mask & ~unit_uom_mask

    cost = line_cost.copy()
    missing = cost.isna() | (cost <= 0)
    mp_mask = None
    if "missing_packs" in df.columns:
        try:
            mp_mask = pd.Series(df["missing_packs"], index=df.index).astype(bool)
        except Exception:
            mp_mask = None
    if mp_mask is not None:
        missing = missing & ~mp_mask
    if cost_per_lb.notna().any():
        calc_lb = cost_per_lb * weight_s
        lb_ok = weight_s > 0
        if weight_uom_mask is not None:
            lb_ok = lb_ok & weight_uom_mask
        fill_mask = missing & (cost_per_lb > 0) & lb_ok
        cost = cost.where(~fill_mask, calc_lb)
        missing = cost.isna() | (cost <= 0)
    if cost_per_unit.notna().any():
        calc_unit = cost_per_unit * qty_for_cost
        fill_mask = missing & (cost_per_unit > 0) & (qty_for_cost > 0)
        cost = cost.where(~fill_mask, calc_unit)

    if mp_mask is not None:
        cost = cost.where(~mp_mask)
    return cost.where(cost > 0)


def sum_cost(series: pd.Series) -> float:
    """Sum positive cost values, returning NaN if none are present."""
    if series is None:
        return float("nan")
    s = to_numeric_safe(series)
    valid = s > 0
    return float(s.where(valid).sum(min_count=1))


def resolve_revenue(
    df: pd.DataFrame,
    *,
    revenue_col: Optional[str] = None,
    units_col: Optional[str] = None,
    weight_col: Optional[str] = None,
    uom_col: Optional[str] = None,
    price_cols: Optional[Iterable[str]] = None,
) -> pd.Series:
    """
    Resolve line-level revenue per row with fallbacks:
    1) revenue column(s)
    2) price * effective qty (uses ItemCount for unit UOMs, weight for lb UOMs)
    Missing revenue returns NaN.
    """
    if df is None or df.empty:
        return pd.Series(dtype="float64")

    resolved_rev_col = revenue_col or revenue_column(df)
    units_col = units_col or units_column(df)
    weight_col = weight_col or weight_lb_column(df)
    uom_col = uom_col if uom_col and uom_col in df.columns else best_column(
        df, ("UOM_UOMShortName", "UOM_UOMName", "UOMName", "UnitOfMeasure", "UnitOfBillingId")
    )

    rev_cols = []
    if resolved_rev_col:
        rev_cols.append(resolved_rev_col)
    for cand in REVENUE_CANDIDATES:
        if cand in df.columns and cand not in rev_cols:
            rev_cols.append(cand)

    revenue = _coalesce_numeric_series(df, rev_cols) if rev_cols else pd.Series(np.nan, index=df.index, dtype="float64")

    price_cols_list = list(price_cols) if price_cols else []
    if not price_cols_list:
        price_cols_list = [
            "Price",
            "PricePerUnit",
            "BasePrice_x",
            "BasePrice_y",
            "BasePrice",
            "ListPrice_x",
            "ListPrice_y",
            "ListPrice",
        ]
    price_series = _coalesce_numeric_series(df, price_cols_list)
    if price_series is None:
        price_series = pd.Series(np.nan, index=df.index, dtype="float64")

    units_s = to_numeric_safe(df[units_col]) if units_col and units_col in df.columns else pd.Series(0.0, index=df.index, dtype="float64")
    weight_s = to_numeric_safe(df[weight_col]) if weight_col and weight_col in df.columns else pd.Series(0.0, index=df.index, dtype="float64")
    item_col = best_column(df, ("ItemCount", "pack_item_count_sum", "item_count", "Item_Count", "PieceCount", "pack_piece_count_sum"))
    item_s = to_numeric_safe(df[item_col]).fillna(0.0) if item_col and item_col in df.columns else None
    uom_s = df[uom_col] if uom_col and uom_col in df.columns else None
    qty = effective_qty_for_cost(units_s, weight_s, uom=uom_s, item_count=item_s)

    missing = revenue.isna() | (revenue == 0)
    mp_mask = None
    if "missing_packs" in df.columns:
        try:
            mp_mask = pd.Series(df["missing_packs"], index=df.index).astype(bool)
        except Exception:
            mp_mask = None
    if mp_mask is not None:
        missing = missing & ~mp_mask
    fill_mask = missing & (price_series > 0) & (qty > 0)
    if fill_mask.any():
        revenue = revenue.where(~fill_mask, price_series * qty)

    if mp_mask is not None:
        revenue = revenue.where(~mp_mask)
    return revenue.where(revenue > 0)

def safe_divide(numerator, denominator, default: float = 0.0) -> Any:
    """Safely divide two numbers or pandas Series, returning a default for division by zero."""
    if isinstance(numerator, pd.Series) or isinstance(denominator, pd.Series):
        if isinstance(denominator, pd.Series):
            denominator = denominator.replace(0, np.nan)
        elif denominator == 0:
            denominator = np.nan

        with np.errstate(divide="ignore", invalid="ignore"):
            result = numerator / denominator

        if isinstance(result, pd.Series):
            return result.fillna(default)
        return default if pd.isna(result) else result

    if denominator == 0:
        return default
    try:
        return numerator / denominator
    except (TypeError, ZeroDivisionError):
        return default


def safe_int(v: Any, default: int = 0) -> int:
    """Safely convert a value to an integer, returning a default on failure or if not positive."""
    try:
        i = int(v)
        return i if i >= 0 else default # Changed from >0 to >=0 to allow 0 as a valid int
    except (ValueError, TypeError):
        return default


def calculate_unit_price(df: pd.DataFrame, revenue_col: str) -> pd.Series:
    """Calculate unit price from revenue and quantity."""
    qty_col = quantity_column(df)
    if not qty_col or qty_col not in df.columns or revenue_col not in df.columns:
        return pd.Series(dtype=float, index=df.index)
    revenue = to_numeric_safe(df[revenue_col])
    quantity = to_numeric_safe(df[qty_col])
    return safe_divide(revenue, quantity)


def calculate_profit(
    df: pd.DataFrame, revenue_col: Optional[str], cost_col: Optional[str]
) -> pd.Series:
    """Calculate profit from revenue and cost."""
    if (
        not revenue_col
        or not cost_col
        or revenue_col not in df.columns
        or cost_col not in df.columns
    ):
        return pd.Series(dtype=float, index=df.index)
    revenue = to_numeric_safe(df[revenue_col])
    cost = to_numeric_safe(df[cost_col])
    return revenue - cost


def to_monthly_period(series: pd.Series) -> pd.Series:
    """Convert a series of dates to monthly periods."""
    return pd.to_datetime(series, errors="coerce").dt.to_period("M")


def calculate_aov(df: pd.DataFrame, revenue_col: Optional[str]) -> float:
    """Calculate Average Order Value (AOV)."""
    if df is None or df.empty or not revenue_col or revenue_col not in df.columns:
        return 0.0

    total_revenue = to_numeric_safe(df[revenue_col]).sum()
    total_orders = df["OrderId"].nunique() if "OrderId" in df.columns else 0

    if total_orders == 0:
        return 0.0

    return float(total_revenue / total_orders)


def calculate_pareto_80(
    df: pd.DataFrame, group_col: str, value_col: str
) -> Tuple[pd.DataFrame, float]:
    """
    Calculates Pareto analysis (80/20 rule).

    Args:
        df: DataFrame to analyze.
        group_col: The column to group by (e.g., 'CustomerId').
        value_col: The column with the value to analyze (e.g., 'Revenue').

    Returns:
        A tuple containing:
        - A DataFrame with the group, value, cumulative percentage, and a boolean
          indicating if it's in the top 80%.
        - The value at which the 80% threshold is crossed.
    """
    if (
        df is None
        or df.empty
        or group_col not in df.columns
        or value_col not in df.columns
    ):
        return pd.DataFrame(), 0.0

    grouped = df.groupby(group_col)[value_col].sum().sort_values(ascending=False)

    pareto_df = grouped.to_frame()
    pareto_df.rename(columns={value_col: "value"}, inplace=True)

    pareto_df["cumulative_sum"] = pareto_df["value"].cumsum()
    total_sum = pareto_df["value"].sum()
    pareto_df["cumulative_pct"] = pareto_df["cumulative_sum"] / total_sum

    pareto_df["is_top_80"] = pareto_df["cumulative_pct"] <= 0.8

    threshold_value = 0.0
    if (pareto_df["cumulative_pct"] >= 0.8).any():
        threshold_value = pareto_df[pareto_df["cumulative_pct"] >= 0.8]["value"].iloc[
            0
        ]

    return pareto_df, threshold_value


def profit_column(df: pd.DataFrame) -> Optional[str]:
    """Find the profit column."""
    return find_first_column(df, NUMERIC_SYNONYMS.get("Profit", ()))


def customer_name_column(df: pd.DataFrame) -> Optional[str]:
    """Find the customer name column."""
    return best_column(df, ("CustomerName", "Customer Name", "Customer_Name", "customer_name", "Name"))


def supplier_id_column(df: pd.DataFrame) -> Optional[str]:
    """Find the supplier id column using flexible matching."""
    return best_column(df, SUPPLIER_ID_ALIASES)


def supplier_name_column(df: pd.DataFrame) -> Optional[str]:
    """
    Find the supplier name column using robust heuristics.

    Falls back to supplier id when a name column is not present.
    """
    if df is None:
        return None

    name_col = best_column(df, SUPPLIER_NAME_ALIASES)
    if name_col:
        return name_col

    id_col = supplier_id_column(df)
    if id_col:
        return id_col

    return None


def supplier_column(df: pd.DataFrame) -> Optional[str]:
    """Public helper to resolve a supplier display column safely."""
    return supplier_name_column(df)


def date_column(df: pd.DataFrame) -> Optional[str]:
    """Find the date column."""
    if df is None:
        return None
    col = fs.resolve_date_column(df)
    if col:
        return col
    return best_column(df, DATE_PRIORITY_ORDER)


def customer_name_column(df: pd.DataFrame) -> Optional[str]:
    """Find the customer name column."""
    return best_column(df, ("CustomerName", "Customer Name", "Customer_Name", "customer_name", "Name"))


def column_map(df: pd.DataFrame) -> Dict[str, Optional[str]]:
    """Return a normalized column map used across analytics views."""
    return {
        "revenue": revenue_column(df),
        "cost": cost_column(df),
        "qty": qty_column(df),
        "weight": weight_column(df),
        "order_id": order_id_column(df),
        "customer_id": customer_id_column(df),
        "customer_name": customer_name_column(df),
        "product_id": product_id_column(df),
        "product_name": product_name_column(df),
        "date": date_column(df),
        "region": region_column(df),
        "supplier": supplier_column(df),
    }


def column_flags(colmap: Dict[str, Optional[str]]) -> Dict[str, bool]:
    """Boolean availability flags for templates and endpoints."""
    flags = {k: bool(v) for k, v in (colmap or {}).items()}
    flags.update(
        {
            "has_cost": bool(colmap.get("cost")),
            "has_weight": bool(colmap.get("weight")),
            "has_qty": bool(colmap.get("qty")),
        }
    )
    return flags


def missing_from_map(colmap: Dict[str, Optional[str]]) -> List[str]:
    """Human-friendly list of missing fields for banners."""
    labels = {
        "revenue": "Revenue",
        "cost": "Cost",
        "qty": "Units/Quantity",
        "weight": "Weight",
        "order_id": "Order ID",
        "customer_id": "Customer ID",
        "customer_name": "Customer Name",
        "product_id": "Product ID",
        "product_name": "Product Name",
        "date": "Date",
        "region": "Region",
        "supplier": "Supplier",
    }
    return [labels.get(k, k) for k, v in (colmap or {}).items() if not v]


def _string_series(df: pd.DataFrame, col: Optional[str]) -> pd.Series:
    """Return a normalized string series for the given column (or NA fallback)."""
    if col and col in df.columns:
        try:
            series = df[col].astype("string")
        except Exception:
            series = pd.Series(df[col], copy=False).astype("string")
        return series.str.strip()
    return pd.Series(pd.NA, index=df.index if df is not None else [], dtype="string")


def _coerce_datetime_series(df: pd.DataFrame, col: Optional[str]) -> pd.Series:
    """Best-effort datetime coercion with tz removal."""
    if col and col in df.columns:
        s = pd.to_datetime(df[col], errors="coerce", utc=False)
    else:
        s = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    try:
        return s.dt.tz_localize(None)
    except Exception:
        return s


def normalize_fact_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize a denormalized fact dataframe to canonical columns with numeric and datetime coercion.
    """
    if df is None:
        return pd.DataFrame(columns=list(vars(fs.CANON).values()) + ["Profit", "Units", "QtyBasis"])
    if df.empty:
        return df.copy()

    work = df.copy()
    idx = work.index

    # Resolve column names using schema helpers
    date_col = date_column(work)
    revenue_col = revenue_column(work)
    cost_col = cost_column(work)
    cost_rate_col = cost_rate_column(work)
    qty_col = qty_column(work)
    weight_col = weight_column(work)
    order_id_col = order_id_column(work)
    order_line_col = best_column(work, ("OrderLineId", "orderline_id", "LineId"))
    product_id_col = product_id_column(work)
    product_name_col = product_name_column(work)
    supplier_id_col = supplier_id_column(work)
    supplier_name_col_resolved = supplier_name_column(work)
    customer_id_col = customer_id_column(work)
    customer_name_col_resolved = customer_name_column(work)
    region_col = region_column(work)
    ship_method_col = best_column(work, ("ShippingMethodName", "ShippingMethodLabel", "ShipMethod_Name"))
    sales_rep_col = best_column(work, ("SalesRepName", "PrimarySalesRepName", "Owner", "UserName", "SalespersonName"))

    # Numeric cores
    qty_series = to_numeric_safe(work[qty_col]) if qty_col else pd.Series(0.0, index=idx, dtype="float64")
    weight_series = to_numeric_safe(work[weight_col]) if weight_col else pd.Series(0.0, index=idx, dtype="float64")
    item_col = best_column(work, ("ItemCount", "pack_item_count_sum", "item_count", "Item_Count", "PieceCount", "pack_piece_count_sum"))
    item_series = to_numeric_safe(work[item_col]) if item_col and item_col in work.columns else None
    uom_col = best_column(work, ("UOM_UOMShortName", "UOM_UOMName", "UOMName", "UnitOfMeasure", "UnitOfBillingId"))
    uom_series = work[uom_col] if uom_col and uom_col in work.columns else None
    base_qty = effective_qty_for_cost(qty_series, weight_series, uom=uom_series, item_count=item_series)

    mp_mask = None
    if "missing_packs" in work.columns:
        try:
            mp_mask = pd.Series(work["missing_packs"], index=idx).astype(bool)
        except Exception:
            mp_mask = None

    packs_only_mode = "revenue_packs_only" in work.columns or "cost_packs_only" in work.columns
    if packs_only_mode and revenue_col and revenue_col in work.columns:
        revenue_series = to_numeric_safe(work[revenue_col])
    else:
        revenue_series = resolve_revenue(
            work,
            revenue_col=revenue_col,
            units_col=qty_col,
            weight_col=weight_col,
        )
    if revenue_series is None or revenue_series.empty:
        revenue_series = pd.Series(np.nan, index=idx, dtype="float64")
    if mp_mask is not None:
        revenue_series = revenue_series.where(~mp_mask)
        revenue_series = revenue_series.where(mp_mask, revenue_series.fillna(0.0))
    else:
        revenue_series = revenue_series.fillna(0.0)

    if packs_only_mode and cost_col and cost_col in work.columns:
        cost_series = to_numeric_safe(work[cost_col])
    else:
        cost_series = resolve_cost(
            work,
            cost_col=cost_col,
            units_col=qty_col,
            weight_col=weight_col,
            cost_rate_cols=[c for c in [cost_rate_col] if c],
        )
    if mp_mask is not None:
        cost_series = cost_series.where(~mp_mask)
        cost_series = cost_series.where(mp_mask, cost_series.fillna(0.0))
    else:
        cost_series = cost_series.fillna(0.0)

    profit_series = revenue_series - cost_series
    if mp_mask is None:
        profit_series = profit_series.fillna(0.0)

    normalized = work.copy()
    normalized[fs.CANON.date] = _coerce_datetime_series(work, date_col)
    normalized[fs.CANON.revenue] = revenue_series
    normalized[fs.CANON.cost] = cost_series
    normalized["Profit"] = profit_series
    normalized[fs.CANON.qty_units] = base_qty.fillna(0.0)
    normalized["Qty"] = normalized[fs.CANON.qty_units]
    normalized["Units"] = normalized[fs.CANON.qty_units]
    normalized["qty_units"] = normalized[fs.CANON.qty_units]
    normalized[fs.CANON.weight_lb] = weight_series.fillna(0.0)

    # String identifiers
    normalized[fs.CANON.order_id] = _string_series(work, order_id_col)
    if order_line_col:
        normalized[fs.CANON.order_line_id] = _string_series(work, order_line_col)
    normalized[fs.CANON.product_id] = _string_series(work, product_id_col)
    normalized[fs.CANON.product_name] = _string_series(work, product_name_col)
    normalized[fs.CANON.supplier_id] = _string_series(work, supplier_id_col)
    normalized[fs.CANON.supplier_name] = _string_series(work, supplier_name_col_resolved)
    normalized[fs.CANON.customer_id] = _string_series(work, customer_id_col)
    normalized[fs.CANON.customer_name] = _string_series(work, customer_name_col_resolved)
    normalized[fs.CANON.region] = _string_series(work, region_col)
    normalized[fs.CANON.ship_method] = _string_series(work, ship_method_col)
    if sales_rep_col:
        normalized[fs.CANON.sales_rep] = _string_series(work, sales_rep_col)

    # Canonical qty/weight aliases used by frontends
    normalized["Quantity"] = normalized[fs.CANON.qty_units]
    normalized["QuantityShipped"] = normalized[fs.CANON.qty_units]
    normalized["Weight"] = normalized[fs.CANON.weight_lb]
    normalized["WeightLb"] = normalized[fs.CANON.weight_lb]

    try:
        normalized.reset_index(drop=True, inplace=True)
    except Exception:
        pass
    return normalized


def pick_first_valid_date_column(
    df: pd.DataFrame, candidates: Iterable[str]
) -> Optional[pd.Series]:
    for col in candidates:
        if col in df.columns:
            s = pd.to_datetime(df[col], errors="coerce", utc=False)
            if s.notna().any():
                try:
                    s = s.dt.tz_localize(None)
                except Exception:
                    pass
                return s
    return None

def records_to_frame(
    records: Iterable[Dict[str, Any]], columns: Optional[Iterable[str]] = None
) -> pd.DataFrame:
    data = list(records or [])
    if not data:
        cols = list(columns) if columns is not None else None
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame.from_records(data)
    if columns is not None:
        for col in columns:
            if col not in df.columns:
                df[col] = np.nan
        df = df[list(columns)]
    try:
        df = df.replace({np.nan: None})
    except Exception:
        pass
    return df


def safe_group_sum(df: pd.DataFrame, group_cols: List[str]) -> pd.DataFrame:
    """Group+sum stable against exotic dtypes (avoids NumPy sentinel pitfalls)."""
    tmp = df.copy()

    for c in group_cols:
        if c in tmp.columns:
            try:
                tmp[c] = tmp[c].astype("string").fillna("").astype(object)
            except Exception:
                tmp[c] = tmp[c].apply(lambda x: "" if pd.isna(x) else str(x))

    tmp["Revenue"] = pd.to_numeric(tmp.get("Revenue", pd.Series(dtype="float")), errors="coerce").fillna(0.0)
    if "Profit" in tmp.columns:
        tmp["Profit"] = pd.to_numeric(tmp.get("Profit"), errors="coerce").fillna(0.0)
    if "QuantityShipped" in tmp.columns:
        tmp["QuantityShipped"] = pd.to_numeric(tmp.get("QuantityShipped"), errors="coerce").fillna(0.0)

    grouped = tmp.groupby(group_cols, dropna=False)

    rev = grouped["Revenue"].sum()
    prof = grouped["Profit"].sum() if "Profit" in tmp.columns else None
    ords = grouped["OrderId"].nunique() if "OrderId" in tmp.columns else None
    units = grouped["QuantityShipped"].sum() if "QuantityShipped" in tmp.columns else None

    records: List[Dict[str, Any]] = []
    for key, r in rev.items():
        key_tuple = key if isinstance(key, tuple) else (key,)
        row: Dict[str, Any] = {}
        for i, col in enumerate(group_cols):
            val = key_tuple[i] if i < len(key_tuple) else None
            row[col] = None if (val is None or (isinstance(val, str) and val == "")) else val
        row["Revenue"] = float(r or 0.0)
        if prof is not None:
            row["Profit"] = float(prof.get(key, 0.0) or 0.0)
        if ords is not None:
            row["Orders"] = int(ords.get(key, 0) or 0)
        if units is not None:
            row["Units"] = float(units.get(key, 0.0) or 0.0)
        records.append(row)

    out_df = pd.DataFrame.from_records(records)
    for c in group_cols:
        if c not in out_df.columns:
            out_df[c] = None
    return out_df

def leaderboard(
    df: pd.DataFrame, id_col: Optional[str], name_col: Optional[str], limit: int
) -> List[Dict[str, Any]]:
    if df.empty:
        return []
    group_cols: List[str] = []
    if id_col and id_col in df.columns:
        group_cols.append(id_col)
    if name_col and name_col in df.columns and name_col not in group_cols:
        group_cols.append(name_col)
    if not group_cols:
        return []
    agg = safe_group_sum(df, group_cols).sort_values("Revenue", ascending=False).head(limit)
    out: List[Dict[str, Any]] = []
    for _, row in agg.iterrows():
        rid = str(row[id_col]).strip() if id_col and id_col in row and pd.notna(row[id_col]) else None
        label = None
        if name_col and name_col in row and pd.notna(row[name_col]):
            label = str(row[name_col]).strip()
        label = label or rid or "Unknown"
        out.append(
            {
                "id": rid,
                "name": label,
                "revenue": float(row.get("Revenue", 0) or 0.0),
                "profit": float(row.get("Profit", 0) or 0.0),
                "orders": int(row.get("Orders", 0) or 0),
                "units": float(row.get("Units", 0) or 0.0),
            }
        )
    return out


def trend(df: pd.DataFrame) -> List[Dict[str, Any]]:
    if df.empty:
        return []
    series = pick_first_valid_date_column(df, DATE_PRIORITY_ORDER)
    if series is None:
        return []
    tmp = df.copy()
    tmp["_trend_date"] = series

    agg: Dict[Any, Dict[str, float]] = {}
    for _, row in tmp.iterrows():
        d = row.get("_trend_date")
        if d is None or (hasattr(d, "tz") and pd.isna(d)) or pd.isna(d):
            continue
        try:
            d_key = d.date() if isinstance(d, pd.Timestamp) else d
        except Exception:
            d_key = d
        rec = agg.setdefault(d_key, {"Revenue": 0.0, "Profit": 0.0})
        rec["Revenue"] += float(row.get("Revenue") or 0.0)
        rec["Profit"] += float(row.get("Profit") or 0.0)

    rows: List[Dict[str, Any]] = []
    for d_key in sorted(agg.keys()):
        rec = agg[d_key]
        d_str = d_key.isoformat() if hasattr(d_key, "isoformat") else str(d_key)
        revenue = float(rec["Revenue"])
        profit = float(rec["Profit"])
        if revenue == 0.0 and profit == 0.0:
            continue
        rows.append({"date": d_str, "Revenue": revenue, "Profit": profit})
    return rows


def status_breakdown(df: pd.DataFrame) -> List[Dict[str, Any]]:
    if df.empty or "OrderStatus" not in df.columns:
        return []
    tmp = df.copy()
    tmp["OrderStatus"] = tmp["OrderStatus"].astype("string").fillna("").astype(object)
    tmp["Revenue"] = pd.to_numeric(tmp.get("Revenue", pd.Series(dtype="float")), errors="coerce").fillna(0.0)

    agg: Dict[str, Dict[str, Any]] = {}
    for _, row in tmp.iterrows():
        status_raw = row.get("OrderStatus")
        status = ""
        if status_raw is not None and pd.notna(status_raw):
            status = str(status_raw).strip().lower()
        if not status:
            status = "unknown"
        rec = agg.setdefault(status, {"Revenue": 0.0, "Orders_set": set()})
        rec["Revenue"] += float(row.get("Revenue") or 0.0)
        oid = row.get("OrderId")
        if oid is not None and not pd.isna(oid):
            rec["Orders_set"].add(oid)

    out_list: List[Dict[str, Any]] = []
    for k, v in agg.items():
        clean = k.replace("_", " ").strip()
        label = clean.title() if clean else "Unknown"
        out_list.append(
            {
                "status": k,
                "label": label,
                "revenue": float(v["Revenue"]),
                "orders": int(len(v["Orders_set"])),
            }
        )
    out_list.sort(key=lambda r: r["revenue"], reverse=True)
    return out_list


def status_summary(rows: Iterable[Dict[str, Any]]) -> Dict[str, float]:
    """Aggregate open/closed/cancelled counts and revenue for status cards."""
    summary = {
        "total_orders": 0,
        "total_revenue": 0.0,
        "open_orders": 0,
        "open_revenue": 0.0,
        "closed_orders": 0,
        "closed_revenue": 0.0,
        "cancelled_orders": 0,
        "cancelled_revenue": 0.0,
    }
    for row in rows or []:
        orders = int(row.get("orders") or 0)
        revenue = float(row.get("revenue") or 0.0)
        status = (row.get("status") or "").strip().lower()
        bucket = "open"
        if any(keyword in status for keyword in STATUS_CANCELLED_KEYWORDS):
            bucket = "cancelled"
        elif any(keyword in status for keyword in STATUS_CLOSED_KEYWORDS):
            bucket = "closed"
        summary["total_orders"] += orders
        summary["total_revenue"] += revenue
        summary[f"{bucket}_orders"] += orders
        summary[f"{bucket}_revenue"] += revenue

    total_orders = summary["total_orders"]
    total_revenue = summary["total_revenue"]

    def _ratio(part: float, whole: float) -> float:
        try:
            return float(part / whole) if whole else 0.0
        except Exception:
            return 0.0

    summary["open_ratio"] = _ratio(summary["open_orders"], total_orders)
    summary["closed_ratio"] = _ratio(summary["closed_orders"], total_orders)
    summary["cancelled_ratio"] = _ratio(summary["cancelled_orders"], total_orders)
    summary["open_revenue_ratio"] = _ratio(summary["open_revenue"], total_revenue)
    summary["closed_revenue_ratio"] = _ratio(summary["closed_revenue"], total_revenue)
    summary["cancelled_revenue_ratio"] = _ratio(summary["cancelled_revenue"], total_revenue)

    for key in list(summary.keys()):
        value = summary[key]
        if isinstance(value, float):
            summary[key] = float(value)
    return summary


def safe_round(value: Any, decimals: int = 0) -> Any:
    """Safely round a numeric value, returning the original value on failure."""
    if value is None:
        return None
    try:
        return round(float(value), decimals)
    except (ValueError, TypeError):
        return value


def recent_orders(df: pd.DataFrame, active_rep_id: Optional[str], limit: int = 20) -> List[Dict[str, Any]]:
    if df.empty:
        return []
    tmp = df.copy()
    order_ts = pick_first_valid_date_column(tmp, ORDER_TS_PRIORITY)
    ship_ts = pick_first_valid_date_column(tmp, SHIP_TS_PRIORITY)
    tmp["_order_ts"] = order_ts
    tmp["_ship_ts"] = ship_ts

    sort_cols: List[str] = []
    ascending: List[bool] = []
    if order_ts is not None:
        sort_cols.append("_order_ts"); ascending.append(False)
    if ship_ts is not None:
        sort_cols.append("_ship_ts");  ascending.append(False)
    if not sort_cols and "Revenue" in tmp.columns:
        sort_cols.append("Revenue");   ascending.append(False)

    tmp = tmp.sort_values(sort_cols, ascending=ascending).head(limit)

    rep_token = str(active_rep_id).strip().lower() if active_rep_id else None
    rows: List[Dict[str, Any]] = []
    for _, row in tmp.iterrows():
        ordered_at = row.get("_order_ts")
        shipped_at = row.get("_ship_ts")
        rep_match = False
        if rep_token:
            for col in ("RepId", "SalesRepId", "PrimarySalesRepId", "UserId"):
                try:
                    if col in row and pd.notna(row[col]) and str(row[col]).strip().lower() == rep_token:
                        rep_match = True
                        break
                except Exception:
                    continue

        rows.append(
            {
                "order_id": str(first_notna(row.get("OrderId")) or ""),
                "customer": str(first_notna(row.get("CustomerName"), row.get("CustomerId"), "Unknown") or ""),
                "region": str(first_notna(row.get("RegionName"), row.get("RegionId"), "") or ""),
                "revenue": float(row.get("Revenue", 0) or 0.0),
                "profit": float(row.get("Profit", 0) or 0.0),
                "units": float(row.get("QuantityShipped", 0) or 0.0),
                "status": str(first_notna(row.get("OrderStatus"), "") or ""),
                "ordered_at": ordered_at.date().isoformat()
                if isinstance(ordered_at, pd.Timestamp) and pd.notna(ordered_at)
                else None,
                "shipped_at": shipped_at.date().isoformat()
                if isinstance(shipped_at, pd.Timestamp) and pd.notna(shipped_at)
                else None,
                "can_edit": rep_match,
            }
        )
    return rows


def safe_sum(series: Optional[pd.Series]) -> float:
    """Stable float sum avoiding NumPy sentinels."""
    if series is None:
        return 0.0
    s = pd.to_numeric(series, errors="coerce")
    total = 0.0
    for v in s:
        try:
            if pd.isna(v):
                continue
        except Exception:
            continue
        try:
            total += float(v)
        except Exception:
            continue
    return total


def kpis(df: pd.DataFrame) -> Optional[Dict[str, float]]:
    if df.empty:
        return None

    revenue_total = safe_sum(df.get("Revenue"))
    profit_total = safe_sum(df.get("Profit"))
    orders = int(df["OrderId"].dropna().nunique()) if "OrderId" in df.columns else 0
    avg_order = (revenue_total / orders) if orders else 0.0
    customers = int(df["CustomerId"].dropna().nunique()) if "CustomerId" in df.columns else 0
    units = safe_sum(df.get("QuantityShipped")) if "QuantityShipped" in df.columns else 0.0
    margin_pct = (profit_total / revenue_total * 100.0) if revenue_total else 0.0
    products = int(df["ProductId"].dropna().nunique()) if "ProductId" in df.columns else 0
    regions = int(df["RegionId"].dropna().nunique()) if "RegionId" in df.columns else 0

    order_dates = pick_first_valid_date_column(df, ORDER_TS_PRIORITY)
    ship_dates = pick_first_valid_date_column(df, SHIP_TS_PRIORITY)
    avg_ship_days = 0.0
    if order_dates is not None and ship_dates is not None:
        delta = (ship_dates - order_dates).dt.days.dropna()
        if not delta.empty:
            avg_ship_days = float(delta[delta >= 0].mean())

    return dict(
        revenue=revenue_total,
        profit=profit_total,
        orders=orders,
        avg_order=avg_order,
        customers=customers,
        units=units,
        margin_pct=margin_pct,
        products=products,
        regions=regions,
        avg_ship_days=avg_ship_days,
    )


def calculate_rolling_average(series: pd.Series, window: int = 3, min_periods: int = 1) -> pd.Series:
    """Calculate rolling average."""
    if series is None or series.empty:
        return pd.Series(dtype=float)
    return series.rolling(window=window, min_periods=min_periods).mean()


def calculate_yoy_growth(current_val: float, previous_val: float) -> Optional[float]:
    """Calculate Year-over-Year growth percentage."""
    if not previous_val or pd.isna(previous_val) or pd.isna(current_val):
        return None
    try:
        return ((current_val - previous_val) / abs(previous_val)) * 100.0
    except ZeroDivisionError:
        return None
