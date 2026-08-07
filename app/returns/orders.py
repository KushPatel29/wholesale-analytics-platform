"""Order lookup adapter for returns using the analytics fact dataset."""

from __future__ import annotations

import re
from typing import Any, Optional

import pandas as pd

from app.services import fact_store


_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


def _customer_predicate(scope: Optional[dict[str, Any]], cols: set[str]) -> tuple[str, list[Any]]:
    if scope is None:
        return "1=1", []
    return fact_store.build_scope_clause(scope, cols)


def _string_expr(column: str | None, alias: str) -> str:
    if not column:
        return f"NULL AS {alias}"
    return f"CAST({fact_store.quote_identifier(column)} AS VARCHAR) AS {alias}"


def _sum_expr(column: str | None, alias: str, default: str = "0") -> str:
    if not column:
        return f"{default} AS {alias}"
    return f"SUM(COALESCE(CAST({fact_store.quote_identifier(column)} AS DOUBLE), 0)) AS {alias}"


def _max_expr(column: str | None, alias: str, default: str = "0") -> str:
    if not column:
        return f"{default} AS {alias}"
    return f"MAX(COALESCE(CAST({fact_store.quote_identifier(column)} AS DOUBLE), 0)) AS {alias}"


def _normalize_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()


def _normalize_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _normalize_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _looks_like_weight_uom(value: Any) -> bool:
    text = _normalize_text(value).lower()
    if not text:
        return False
    return any(token in text for token in ("lb", "lbs", "pound", "weight"))


def _weight_mapping(weight_lb: float, fallback_weight_lb: float) -> tuple[float, str | None, str]:
    if weight_lb > 0:
        return round(weight_lb, 3), None, "weight_lb"
    if fallback_weight_lb > 0:
        return round(fallback_weight_lb, 3), None, "pack_weight_lb_sum"
    return 0.0, "Weight unavailable from source; review before submitting.", "missing"


def _price_per_lb_mapping(
    *,
    raw_price_per_lb: float | None,
    raw_price: float | None,
    revenue: float,
    weight_lb: float,
    billing_uom: str,
) -> tuple[float, str | None, str]:
    if raw_price_per_lb is not None and raw_price_per_lb > 0:
        return round(raw_price_per_lb, 2), None, "price_per_lb"

    derived_price = (revenue / weight_lb) if revenue > 0 and weight_lb > 0 else None
    if derived_price is not None:
        if raw_price is not None and raw_price > 0 and abs(raw_price - derived_price) <= 0.05:
            return round(raw_price, 2), None, "price"
        warning = "Price/lb derived from revenue ÷ shipped lb."
        return round(derived_price, 2), warning, "revenue_weight"

    if raw_price is not None and raw_price > 0 and _looks_like_weight_uom(billing_uom):
        return round(raw_price, 2), None, "billing_uom_price"

    return 0.0, "Price/lb unavailable from source; review before submitting.", "missing"


def _combine_mapping_warning(*messages: str | None) -> str | None:
    clean = []
    for message in messages:
        text = _normalize_text(message)
        if text and text not in clean:
            clean.append(text)
    return " ".join(clean) if clean else None


def _records_from_df(df: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in df.to_dict(orient="records"):
        clean_row: dict[str, Any] = {}
        for key, value in row.items():
            clean_row[key] = None if pd.isna(value) else value
        rows.append(clean_row)
    return rows


def normalize_order_id(order_id: str) -> str:
    value = str(order_id or "").strip()
    if not value:
        raise ValueError("Order ID is required.")
    if len(value) > 64:
        raise ValueError("Order ID is too long.")
    if not _ORDER_ID_RE.match(value):
        raise ValueError("Order ID contains unsupported characters.")
    return value


def order_exists(order_id: str) -> bool:
    clean = normalize_order_id(order_id)
    cols = fact_store.list_columns()
    order_col = fact_store.choose_column(("OrderId", "OrderID", "InvoiceNo", "OrderNo"), cols)
    if not order_col:
        return False
    sql = f"""
        SELECT 1
        FROM fact
        WHERE LOWER(CAST({fact_store.quote_identifier(order_col)} AS VARCHAR)) = ?
        LIMIT 1
    """
    df = fact_store.execute_sql_df(sql, [clean.lower()], tag="returns.orders.exists")
    return not df.empty


def search_orders(
    *,
    order_id: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    scope: Optional[dict[str, Any]] = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    cols = fact_store.list_columns()
    order_col = fact_store.choose_column(("OrderId", "OrderID", "InvoiceNo", "OrderNo"), cols)
    if not order_col:
        return []

    customer_id_col = fact_store.choose_column(("CustomerId", "CustomerID"), cols) or order_col
    customer_name_col = fact_store.choose_column(("CustomerName", "Customer"), cols) or customer_id_col
    email_col = fact_store.choose_column(("Email", "CustomerEmail", "ShipEmail"), cols)
    phone_col = fact_store.choose_column(("Phone", "CustomerPhone", "ShipPhone"), cols)
    date_col = fact_store.choose_column(("Date", "DateExpected", "InvoiceDate"), cols)
    revenue_col = fact_store.choose_column(("Revenue", "NetSales", "Sales"), cols)

    filters: list[str] = []
    params: list[Any] = []
    if order_id:
        try:
            clean_order = normalize_order_id(order_id)
        except ValueError:
            return []
        filters.append(f"LOWER(CAST({fact_store.quote_identifier(order_col)} AS VARCHAR)) = ?")
        params.append(clean_order.lower())
    if email and email_col:
        filters.append(f"LOWER(CAST({fact_store.quote_identifier(email_col)} AS VARCHAR)) = ?")
        params.append(str(email).strip().lower())
    if phone and phone_col:
        filters.append(
            f"REPLACE(REPLACE(REPLACE(CAST({fact_store.quote_identifier(phone_col)} AS VARCHAR), '-', ''), '(', ''), ')', '') = ?"
        )
        params.append(str(phone).strip().replace("-", "").replace("(", "").replace(")", ""))
    if not filters:
        return []

    scope_sql, scope_params = _customer_predicate(scope, cols)
    select_bits = [
        f"CAST({fact_store.quote_identifier(order_col)} AS VARCHAR) AS order_id",
        f"CAST({fact_store.quote_identifier(customer_id_col)} AS VARCHAR) AS customer_id",
        f"CAST({fact_store.quote_identifier(customer_name_col)} AS VARCHAR) AS customer_name",
    ]
    group_bits = ["order_id", "customer_id", "customer_name"]
    if email_col:
        select_bits.append(f"CAST({fact_store.quote_identifier(email_col)} AS VARCHAR) AS customer_email")
        group_bits.append("customer_email")
    else:
        select_bits.append("NULL AS customer_email")
    if phone_col:
        select_bits.append(f"CAST({fact_store.quote_identifier(phone_col)} AS VARCHAR) AS customer_phone")
        group_bits.append("customer_phone")
    else:
        select_bits.append("NULL AS customer_phone")
    if date_col:
        select_bits.append(f"MAX(CAST({fact_store.quote_identifier(date_col)} AS DATE)) AS order_date")
    else:
        select_bits.append("NULL AS order_date")
    if revenue_col:
        select_bits.append(f"SUM(COALESCE(CAST({fact_store.quote_identifier(revenue_col)} AS DOUBLE), 0)) AS order_total")
    else:
        select_bits.append("0 AS order_total")
    select_bits.append("COUNT(*) AS line_count")

    where_sql = " AND ".join([f"({scope_sql})", "(" + " OR ".join(filters) + ")"])
    params_all = list(scope_params) + params
    sql = f"""
        SELECT {", ".join(select_bits)}
        FROM fact
        WHERE {where_sql}
        GROUP BY {", ".join(group_bits)}
        ORDER BY order_id DESC
    """
    if limit and int(limit) > 0:
        sql += " LIMIT ?"
        params_all.append(int(limit))

    df = fact_store.execute_sql_df(sql, params_all, tag="returns.orders.search")
    if df.empty:
        return []
    return _records_from_df(df)


def get_order_detail(order_id: str, *, scope: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    clean = normalize_order_id(order_id)
    cols = fact_store.list_columns()
    order_col = fact_store.choose_column(("OrderId", "OrderID", "InvoiceNo", "OrderNo"), cols)
    if not order_col:
        return None

    customer_id_col = fact_store.choose_column(("CustomerId", "CustomerID"), cols) or order_col
    customer_name_col = fact_store.choose_column(("CustomerName", "Customer"), cols) or customer_id_col
    email_col = fact_store.choose_column(("Email", "CustomerEmail", "ShipEmail"), cols)
    phone_col = fact_store.choose_column(("Phone", "CustomerPhone", "ShipPhone"), cols)
    ship_to_col = fact_store.choose_column(("ShipToName", "ShipTo", "ShipName", "DeliveryName"), cols)
    status_col = fact_store.choose_column(("OrderStatus", "Status", "InvoiceStatus", "ShipmentStatus"), cols)
    date_col = fact_store.choose_column(("Date", "DateExpected", "InvoiceDate", "ShipDate"), cols)

    line_id_col = fact_store.choose_column(("OrderLineId", "OrderLineID", "LineId"), cols)
    sku_col = fact_store.choose_column(("SKU", "Sku", "ProductId", "ProductID"), cols)
    product_name_col = fact_store.choose_column(("ProductName", "Product", "ItemDescription", "Description"), cols) or sku_col
    pack_col = fact_store.choose_column(("Pack", "PackSize", "UnitSize", "PackDesc"), cols)
    qty_ordered_col = fact_store.choose_column(("QuantityOrdered", "QtyOrdered", "OrderedQty", "OriginalQty", "OrderQty"), cols)
    qty_shipped_col = fact_store.choose_column(("QuantityShipped", "QtyShipped", "ShippedQty", "NetQuantity", "Qty", "Quantity", "Cases"), cols)
    weight_lb_col = fact_store.choose_column(("WeightLb", "ShippedLb", "ShipLb", "Lbs", "Pounds", "Weight", "weight_lb"), cols)
    pack_weight_lb_col = fact_store.choose_column(("pack_weight_lb_sum",), cols)
    revenue_col = fact_store.choose_column(("Revenue", "NetSales", "Sales", "LineTotal", "ExtendedPrice"), cols)
    cost_col = fact_store.choose_column(("Cost", "LineCost", "ExtendedCost", "COGS", "ProductCost"), cols)
    margin_col = fact_store.choose_column(("Margin", "GrossMargin", "Profit"), cols)
    price_per_lb_col = fact_store.choose_column(("PricePerLb", "UnitPricePerLb", "AvgSalePricePerLb"), cols)
    price_col = fact_store.choose_column(("Price", "BasePrice", "ListPrice"), cols)
    billing_uom_col = fact_store.choose_column(("BillingUOM", "UnitOfMeasure", "UOM", "UOMName", "UOM_UOMName", "UnitOfBillingId"), cols)
    category_col = fact_store.choose_column(("Category", "ProductCategory", "Department", "ProteinCategory"), cols)

    scope_sql, scope_params = _customer_predicate(scope, cols)
    select_bits = [
        f"CAST({fact_store.quote_identifier(order_col)} AS VARCHAR) AS order_id",
        f"CAST({fact_store.quote_identifier(customer_id_col)} AS VARCHAR) AS customer_id",
        f"CAST({fact_store.quote_identifier(customer_name_col)} AS VARCHAR) AS customer_name",
        _string_expr(email_col, "customer_email"),
        _string_expr(phone_col, "customer_phone"),
        _string_expr(ship_to_col, "ship_to"),
        _string_expr(status_col, "order_status"),
    ]
    group_bits = [
        "order_id",
        "customer_id",
        "customer_name",
        "customer_email",
        "customer_phone",
        "ship_to",
        "order_status",
    ]
    if date_col:
        select_bits.append(f"MAX(CAST({fact_store.quote_identifier(date_col)} AS DATE)) AS order_date")
    else:
        select_bits.append("NULL AS order_date")

    select_bits.extend(
        [
            _string_expr(line_id_col, "order_line_id"),
            _string_expr(sku_col, "sku"),
            _string_expr(product_name_col, "product_name"),
            _string_expr(pack_col, "pack"),
            _string_expr(category_col, "category"),
            _string_expr(billing_uom_col, "billing_uom"),
            _sum_expr(qty_ordered_col, "qty_ordered", default="0"),
            _sum_expr(qty_shipped_col, "qty_shipped", default="0"),
            _sum_expr(weight_lb_col, "weight_lb_source", default="0"),
            _sum_expr(pack_weight_lb_col, "pack_weight_lb_source", default="0"),
            _max_expr(price_per_lb_col, "price_per_lb_source", default="0"),
            _max_expr(price_col, "price_source", default="0"),
            _sum_expr(revenue_col, "revenue", default="0"),
            _sum_expr(cost_col, "cost", default="0"),
            _sum_expr(margin_col, "margin", default="0"),
        ]
    )
    group_bits.extend(["order_line_id", "sku", "product_name", "pack", "category", "billing_uom"])

    sql = f"""
        SELECT {", ".join(select_bits)}
        FROM fact
        WHERE ({scope_sql})
          AND LOWER(CAST({fact_store.quote_identifier(order_col)} AS VARCHAR)) = ?
        GROUP BY {", ".join(group_bits)}
        ORDER BY order_line_id NULLS LAST, sku NULLS LAST, product_name NULLS LAST
    """
    params = list(scope_params) + [clean.lower()]
    df = fact_store.execute_sql_df(sql, params, tag="returns.orders.detail")
    if df.empty:
        return None

    rows = _records_from_df(df)
    first = rows[0]
    items: list[dict[str, Any]] = []
    order_total = 0.0
    order_cost_total = 0.0
    for idx, row in enumerate(rows, start=1):
        qty_ordered = max(_normalize_float(row.get("qty_ordered")), 0.0)
        qty_shipped = max(_normalize_float(row.get("qty_shipped")), 0.0)
        if qty_ordered <= 0 and qty_shipped > 0:
            qty_ordered = qty_shipped
        source_weight_lb = max(_normalize_float(row.get("weight_lb_source")), 0.0)
        pack_weight_lb = max(_normalize_float(row.get("pack_weight_lb_source")), 0.0)
        revenue = _normalize_float(row.get("revenue"))
        cost = _normalize_float(row.get("cost"))
        margin = _normalize_float(row.get("margin"))
        if not margin and revenue and cost:
            margin = revenue - cost
        billing_uom = _normalize_text(row.get("billing_uom"))
        raw_price_per_lb = _normalize_optional_float(row.get("price_per_lb_source"))
        raw_price = _normalize_optional_float(row.get("price_source"))
        weight_lb, weight_warning, weight_source = _weight_mapping(source_weight_lb, pack_weight_lb)
        price_per_lb, price_warning, price_source = _price_per_lb_mapping(
            raw_price_per_lb=raw_price_per_lb,
            raw_price=raw_price,
            revenue=revenue,
            weight_lb=weight_lb,
            billing_uom=billing_uom,
        )
        mapping_warning = _combine_mapping_warning(price_warning, weight_warning)
        margin_pct = (margin / revenue) if revenue else None
        items.append(
            {
                "order_line_id": _normalize_text(row.get("order_line_id")) or f"{clean}:{idx}",
                "sku": _normalize_text(row.get("sku")),
                "product_name": _normalize_text(row.get("product_name")) or _normalize_text(row.get("sku")) or f"Line {idx}",
                "description": _normalize_text(row.get("product_name")) or _normalize_text(row.get("sku")) or f"Line {idx}",
                "pack": _normalize_text(row.get("pack")),
                "category": _normalize_text(row.get("category")),
                "qty_ordered": qty_ordered,
                "qty_shipped": qty_shipped,
                "weight_lb": weight_lb,
                "shipped_weight_lb": weight_lb,
                "weight_source": weight_source,
                "billing_uom": billing_uom,
                "unit_price": price_per_lb,
                "unit_price_per_lb": price_per_lb,
                "price": price_per_lb,
                "price_per_lb": price_per_lb,
                "price_source": price_source,
                "revenue": round(revenue, 2),
                "rev": round(revenue, 2),
                "cost": round(cost, 2),
                "margin": round(margin, 2),
                "margin_pct": round(margin_pct, 4) if margin_pct is not None else None,
                "credit_pct": 100.0,
                "credit_amount": round(price_per_lb * weight_lb, 2),
                "mapping_warning": mapping_warning,
                "source_price": round(raw_price, 2) if raw_price is not None else None,
                "source_price_per_lb": round(raw_price_per_lb, 2) if raw_price_per_lb is not None else None,
            }
        )
        order_total += revenue
        order_cost_total += cost

    total_margin = order_total - order_cost_total
    order_payload = {
        "order_id": _normalize_text(first.get("order_id")) or clean,
        "customer_id": _normalize_text(first.get("customer_id")),
        "customer_name": _normalize_text(first.get("customer_name")),
        "customer_email": _normalize_text(first.get("customer_email")),
        "customer_phone": _normalize_text(first.get("customer_phone")),
        "ship_to": _normalize_text(first.get("ship_to")),
        "order_status": _normalize_text(first.get("order_status")),
        "status": _normalize_text(first.get("order_status")),
        "order_date": _normalize_text(first.get("order_date")),
        "order_total": round(order_total, 2),
        "total_revenue": round(order_total, 2),
        "total_cost": round(order_cost_total, 2),
        "total_margin": round(total_margin, 2),
        "items": items,
    }
    return {"order": order_payload, "items": items}


def get_order(order_id: str, *, scope: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    detail = get_order_detail(order_id, scope=scope)
    if not detail:
        return None
    return dict(detail["order"])
