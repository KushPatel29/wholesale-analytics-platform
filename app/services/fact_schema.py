# app/services/fact_schema.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional
import pandas as pd


@dataclass(frozen=True)
class FactColumns:
    date: str = "Date"
    order_id: str = "OrderId"
    order_line_id: str = "OrderLineId"
    product_id: str = "ProductId"
    product_name: str = "ProductName"
    supplier_id: str = "SupplierId"
    supplier_name: str = "SupplierName"
    customer_id: str = "CustomerId"
    customer_name: str = "CustomerName"
    region: str = "RegionName"
    ship_method: str = "ShippingMethodName"
    sales_rep: str = "SalesRepName"
    protein_group: str = "ProteinGroup"
    yield_pct: str = "YieldPct"
    is_catch_weight: bool = "IsCatchWeight"
    revenue: str = "Revenue"
    cost: str = "Cost"
    qty_units: str = "QuantityShipped"
    weight_lb: str = "WeightLb"


CANON = FactColumns()

PROTEIN_CANDIDATES: tuple[str, ...] = (
    "ProteinGroup",
    "ProteinCategory",
    "MeatType",
    "Species",
    "Protein",
    "CategoryName",
    "SubCategoryName",
)

YIELD_CANDIDATES: tuple[str, ...] = (
    "YieldPct",
    "Yield",
    "ProcessingYield",
    "ShrinkagePct",
    "RecoveryPct",
)

CATCH_WEIGHT_CANDIDATES: tuple[str, ...] = (
    "IsCatchWeight",
    "CatchWeight",
    "VariableWeight",
    "IsVariableWeight",
)

REVENUE_CANDIDATES: tuple[str, ...] = (
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
    "OrderTotalPrice",
)

COST_TOTAL_CANDIDATES: tuple[str, ...] = (
    "cost_packs_only",
    "Cost",
    "ExtCost",
    "ExtendedCost",
    "COGS",
    "TotalCost",
    "LineCost",
    "cost_shipped",
    "cost_ordered",
    "Cost_line",
    "Cost_x",
    "Cost_y",
)

COST_RATE_CANDIDATES: tuple[str, ...] = (
    "CostPrice",
    "CostPerUnit",
    "unit_cost_effective",
    "UnitCost",
    "CostPrice_x",
    "CostPrice_y",
    "CostPrice_line",
)

QTY_CANDIDATES: tuple[str, ...] = (
    "QuantityShipped",
    "QuantityOrdered",
    "Qty",
    "QtyShipped",
    "QtyOrdered",
    "Units",
    "Quantity",
    "ShippedQty",
    "ShipQty",
    "CaseQty",
    "ItemCount",
    "UnitsShipped",
    "Quantity_Shipped",
    "Quantity_Ordered",
    "UnitQty",
    "pack_item_count_sum",
)

WEIGHT_CANDIDATES: tuple[str, ...] = (
    "WeightLb",
    "ShippedLb",
    "ShipLb",
    "Lbs",
    "Pounds",
    "Weight",
    "weight_lb",
    "pack_weight_lb_sum",
)

DATE_CANDIDATES: tuple[str, ...] = (
    "Date",
    "ShipDate",
    "DateShipped",
    "DateShipped_line",
    "DateShipped_order",
    "DateExpected",
    "DateOrdered",
    "OrderDate",
    "InvoiceDate",
    "TxnDate",
)


def _normalize_col_label(label: str) -> str:
    """Normalize a column label for comparison (case/whitespace/underscore agnostic)."""
    return "".join(ch for ch in str(label).lower() if ch.isalnum())


def best_column(df: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    """
    Return the first best matching column using exact, case-insensitive,
    then normalized (strip spaces/underscores) comparisons.
    Suffix variants like `_x`/`_y` are also tolerated.
    """
    if df is None:
        return None
    cols = list(df.columns)
    lower_map: dict[str, str] = {}
    norm_map: dict[str, str] = {}
    for col in cols:
        lower_key = str(col).lower()
        norm_key = _normalize_col_label(col)
        if lower_key not in lower_map:
            lower_map[lower_key] = col
        if norm_key not in norm_map:
            norm_map[norm_key] = col
        # Allow suffix-insensitive lookups for pandas merge artifacts
        if norm_key.endswith("x") or norm_key.endswith("y"):
            base_norm = norm_key[:-1]
            norm_map.setdefault(base_norm, col)
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


def resolve_revenue_column(df: pd.DataFrame) -> Optional[str]:
    return best_column(df, REVENUE_CANDIDATES)


def resolve_cost_column(df: pd.DataFrame) -> Optional[str]:
    return best_column(df, COST_TOTAL_CANDIDATES)


def resolve_cost_rate_column(df: pd.DataFrame) -> Optional[str]:
    return best_column(df, COST_RATE_CANDIDATES)


def resolve_qty_column(df: pd.DataFrame) -> Optional[str]:
    return best_column(df, QTY_CANDIDATES)


def resolve_weight_column(df: pd.DataFrame) -> Optional[str]:
    return best_column(df, WEIGHT_CANDIDATES)


def resolve_date_column(df: pd.DataFrame) -> Optional[str]:
    return best_column(df, DATE_CANDIDATES)


def resolve_protein_column(df: pd.DataFrame) -> Optional[str]:
    return best_column(df, PROTEIN_CANDIDATES)


def resolve_yield_column(df: pd.DataFrame) -> Optional[str]:
    return best_column(df, YIELD_CANDIDATES)


def resolve_catch_weight_column(df: pd.DataFrame) -> Optional[str]:
    return best_column(df, CATCH_WEIGHT_CANDIDATES)
