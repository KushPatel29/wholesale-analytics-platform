"""Fast, filter-aware inventory analysis built from the canonical fact view.

The page intentionally uses one SKU-level DuckDB query.  All classifications
(ABC, stock posture, SVSI, holding cost, aging, movers, and purchase planning)
are derived from that compact result in memory, keeping the hosted demo below
the cost of another multi-query dashboard.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import math
import time
from typing import Any, Iterable

import pandas as pd

from app.core.rbac import has_permission
from app.services import fact_store


ANNUAL_CAPITAL_RATE = 0.05
ANNUAL_SERVICE_RATE = 0.01
ANNUAL_STORAGE_RATE = 0.02
ANNUAL_RISK_RATE = 0.03
ANNUAL_HOLDING_RATE = ANNUAL_CAPITAL_RATE + ANNUAL_SERVICE_RATE + ANNUAL_STORAGE_RATE + ANNUAL_RISK_RATE


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return default if math.isnan(result) or math.isinf(result) else result
    except (TypeError, ValueError):
        return default


def _optional_number(value: Any) -> float | None:
    try:
        result = float(value)
        return None if math.isnan(result) or math.isinf(result) else result
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    return int(round(_number(value)))


def _is_perishable(category: str, protein: str) -> bool:
    text = f"{category} {protein}".lower()
    return any(token in text for token in ("fresh", "produce", "dairy", "frozen", "meat", "seafood", "perishable"))


# Perishable departments run short cover by design, so one cover target across
# the chain would flag all of fresh as a problem. Named because the cover chart
# draws these as guide bands and must use the same numbers the posture rules do.
TARGET_DAYS_PERISHABLE = 14.0
TARGET_DAYS_AMBIENT = 45.0


def _target_days(row: dict[str, Any]) -> float:
    perishable = _is_perishable(str(row.get("category") or ""), str(row.get("protein") or ""))
    return TARGET_DAYS_PERISHABLE if perishable else TARGET_DAYS_AMBIENT


def _round_opt(value: Any, digits: int = 1) -> float | None:
    """Round while preserving None, so a missing value stays missing."""
    number = _optional_number(value)
    return None if number is None else round(number, digits)


def _stock_posture(row: dict[str, Any]) -> tuple[str, str, int]:
    on_hand = _number(row.get("on_hand_qty"))
    safety = _number(row.get("safety_stock_qty"))
    reorder = max(_number(row.get("reorder_point_qty")), safety)
    days = _number(row.get("days_supply"))
    target = _number(row.get("target_days"), 45.0)
    stockout = bool(row.get("is_stockout")) or on_hand <= 0
    if stockout or on_hand < safety:
        return "Critical", "critical", 4
    if on_hand <= reorder or days < target * 0.65:
        return "Reorder", "reorder", 3
    if days > target * 1.45:
        return "Excess", "excess", 2
    return "Healthy", "healthy", 1


def _age_bucket(days: int) -> str:
    if days <= 30:
        return "0-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    if days <= 180:
        return "91-180"
    if days <= 365:
        return "181-365"
    return "365+"


def _safe_text(value: Any, fallback: str = "Unknown") -> str:
    text = str(value or "").strip()
    return text or fallback


def _sku_query(where_sql: str) -> str:
    return f"""
        WITH scoped AS (
            SELECT *
            FROM fact
            WHERE {where_sql}
        )
        SELECT
            COALESCE(CAST(ProductId AS VARCHAR), CAST(SKU AS VARCHAR), 'Unknown') AS product_id,
            ARG_MAX(COALESCE(ProductName, SkuName, CAST(ProductId AS VARCHAR)), Date) AS product_name,
            ARG_MAX(COALESCE(ProductCategory, Category, 'Unassigned'), Date) AS category,
            ARG_MAX(COALESCE(ProteinName, ProteinType, 'Unassigned'), Date) AS protein,
            ARG_MAX(COALESCE(SupplierName, 'Unassigned'), Date) AS supplier,
            MIN(CAST(Date AS DATE)) AS first_movement_date,
            MAX(CAST(Date AS DATE)) AS last_movement_date,
            COUNT(DISTINCT OrderId) AS orders,
            COUNT(DISTINCT CustomerId) AS customers,
            SUM(COALESCE(pack_units_ea, QuantityShipped, 0)) AS usage_units,
            SUM(COALESCE(pack_weight_lb, ShippedLb, WeightLb, 0)) AS usage_weight_lb,
            SUM(COALESCE(Revenue, 0)) AS revenue,
            SUM(COALESCE(Cost, 0)) AS sales_cost,
            ARG_MAX(COALESCE(OnHandQty, 0), Date) AS on_hand_qty,
            ARG_MAX(COALESCE(OnHandValue, 0), Date) AS inventory_value,
            ARG_MAX(COALESCE(DaysOfSupply, 0), Date) AS days_supply,
            ARG_MAX(COALESCE(ReorderPointQty, 0), Date) AS reorder_point_qty,
            ARG_MAX(COALESCE(SafetyStockQty, 0), Date) AS safety_stock_qty,
            ARG_MAX(COALESCE(BackorderQty, 0), Date) AS backorder_qty,
            ARG_MAX(CASE WHEN IsStockout THEN 1 ELSE 0 END, Date) AS is_stockout,
            ARG_MAX(COALESCE(CostPrice, 0), Date) AS unit_cost,
            DATE_DIFF('day', MIN(CAST(Date AS DATE)), MAX(CAST(Date AS DATE))) + 1 AS observed_days
        FROM scoped
        GROUP BY 1
        ORDER BY inventory_value DESC, revenue DESC
    """


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return frame.where(pd.notna(frame), None).to_dict(orient="records") if not frame.empty else []


def _classify_rows(rows: list[dict[str, Any]], window_end: str | None) -> None:
    total_inventory = sum(_number(row.get("inventory_value")) for row in rows)
    total_usage = sum(_number(row.get("usage_units")) for row in rows)
    ranked = sorted(rows, key=lambda row: _number(row.get("inventory_value")), reverse=True)
    cumulative = 0.0

    try:
        anchor = pd.Timestamp(window_end).date() if window_end else max(pd.Timestamp(row.get("last_movement_date")).date() for row in rows)
    except Exception:
        anchor = date.today()

    for row in rows:
        observed_weeks = max(_number(row.get("observed_days"), 7.0) / 7.0, 1.0)
        row["avg_weekly_usage"] = _number(row.get("usage_units")) / observed_weeks

    usage_values = sorted(_number(row.get("avg_weekly_usage")) for row in rows)
    usage_median = usage_values[len(usage_values) // 2] if usage_values else 0.0
    value_values = sorted(_number(row.get("inventory_value")) for row in rows)
    value_median = value_values[len(value_values) // 2] if value_values else 0.0

    for row in ranked:
        average_weekly = _number(row.get("avg_weekly_usage"))
        on_hand = _number(row.get("on_hand_qty"))
        # Match the source inventory app's operating definitions: current
        # on-hand divided by average weekly usage, and 52 / WOH for turns.
        # Native days-of-supply remains the fallback for no-usage items.
        computed_woh = on_hand / average_weekly if average_weekly > 0 else None
        native_woh = _number(row.get("days_supply")) / 7.0 if _number(row.get("days_supply")) > 0 else None
        row["weeks_on_hand"] = computed_woh if computed_woh is not None else native_woh
        row["annual_turns"] = 52.0 / row["weeks_on_hand"] if _number(row.get("weeks_on_hand")) > 0 else None
        row["target_days"] = _target_days(row)
        row["target_units"] = row["avg_weekly_usage"] * row["target_days"] / 7.0
        row["suggested_buy_units"] = max(row["target_units"] - _number(row.get("on_hand_qty")), 0.0)
        unit_cost = _number(row.get("unit_cost"))
        if unit_cost <= 0 and _number(row.get("on_hand_qty")) > 0:
            unit_cost = _number(row.get("inventory_value")) / _number(row.get("on_hand_qty"))
        row["unit_cost"] = unit_cost
        row["suggested_buy_cost"] = row["suggested_buy_units"] * unit_cost
        row["holding_cost_annual"] = _number(row.get("inventory_value")) * ANNUAL_HOLDING_RATE

        posture, posture_key, priority = _stock_posture(row)
        row["posture"] = posture
        row["posture_key"] = posture_key
        row["priority"] = priority

        value = _number(row.get("inventory_value"))
        cumulative += value
        cumulative_share = cumulative / total_inventory if total_inventory else 0.0
        row["abc_class"] = "A" if cumulative_share <= 0.80 else "B" if cumulative_share <= 0.95 else "C"

        inventory_share = value / total_inventory if total_inventory else 0.0
        usage_share = _number(row.get("usage_units")) / total_usage if total_usage else 0.0
        row["svsi"] = inventory_share / usage_share if usage_share > 0 else None
        svsi = _optional_number(row.get("svsi"))
        row["svsi_label"] = "Under-covered" if svsi is not None and svsi < 0.70 else "Excess-weighted" if svsi is not None and svsi > 1.50 else "Balanced"

        high_usage = _number(row.get("avg_weekly_usage")) >= usage_median
        high_value = value >= value_median
        row["movement_quadrant"] = (
            "Core / protect" if high_usage and high_value else
            "Fast / lean" if high_usage else
            "Slow / cash tied" if high_value else
            "Tail / monitor"
        )
        try:
            age_days = max((anchor - pd.Timestamp(row.get("last_movement_date")).date()).days, 0)
        except Exception:
            age_days = 0
        row["age_days"] = age_days
        row["aging_bucket"] = _age_bucket(age_days)
        row["product_name"] = _safe_text(row.get("product_name"), row.get("product_id") or "Unknown SKU")
        row["category"] = _safe_text(row.get("category"), "Unassigned")
        row["protein"] = _safe_text(row.get("protein"), "Unassigned")
        row["supplier"] = _safe_text(row.get("supplier"), "Unassigned")


def _chart_series(rows: list[dict[str, Any]], *, total_inventory: float) -> dict[str, Any]:
    """
    The three shapes a ranked bar list cannot show.

    The page already renders ABC, movement and aging as bar lists, which is the
    right form for a composition but loses the thing each diagnostic is
    actually for:

      * ABC is a *concentration* claim - "80% of value sits in these SKUs" -
        and that is a cumulative curve, not three totals.
      * Cover is a *distribution*. A mean of 34 days hides both the SKU at 2
        days and the one at 155, which are the only two worth acting on.
      * Movement is a *relationship* between usage and value. Collapsing it to
        four quadrant counts discards both axes.

    Emitted as parallel arrays rather than a list of objects: same numbers, a
    third of the bytes, and it is the shape a plotting library wants anyway.
    Charts are frozen to SVG at build time, so none of this reaches a static
    visitor - it only has to be small enough for the live app.
    """
    ordered = sorted(rows, key=lambda row: _number(row.get("inventory_value")), reverse=True)

    # ABC: cumulative share of inventory value against SKU rank, as percentages
    # so the axis needs no client arithmetic.
    cumulative = 0.0
    cum_share: list[float] = []
    sku_share: list[float] = []
    for index, row in enumerate(ordered, start=1):
        cumulative += _number(row.get("inventory_value"))
        cum_share.append(round(cumulative / total_inventory * 100.0, 2) if total_inventory else 0.0)
        sku_share.append(round(index / len(ordered) * 100.0, 2) if ordered else 0.0)

    def _class_edge(label: str) -> int | None:
        """1-based rank of the last SKU in a class, for the band boundaries."""
        last = None
        for index, row in enumerate(ordered, start=1):
            if row.get("abc_class") == label:
                last = index
        return last

    # Cover: fixed bins so the shape is comparable between scopes, with an
    # explicit overflow bucket rather than a long empty tail.
    edges = [0, 7, 14, 21, 30, 45, 60, 90, 120]
    bins: list[dict[str, Any]] = []
    for low, high in zip(edges, edges[1:]):
        bucket = [r for r in rows if low <= _number(r.get("days_supply")) < high]
        bins.append({
            "label": f"{low}-{high}",
            "low": low,
            "high": high,
            "skus": len(bucket),
            "value": round(sum(_number(r.get("inventory_value")) for r in bucket), 2),
        })
    tail = [r for r in rows if _number(r.get("days_supply")) >= edges[-1]]
    bins.append({
        "label": f"{edges[-1]}+",
        "low": edges[-1],
        "high": None,
        "skus": len(tail),
        "value": round(sum(_number(r.get("inventory_value")) for r in tail), 2),
    })

    # Movement: one point per SKU. 300-odd points is a scatter, not a payload
    # problem, and sampling would hide exactly the outliers worth seeing.
    return {
        "abc_pareto": {
            "sku_share_pct": sku_share,
            "cum_value_pct": cum_share,
            "a_edge": _class_edge("A"),
            "b_edge": _class_edge("B"),
            "sku_count": len(ordered),
        },
        "cover_histogram": {
            "bins": bins,
            "target_perishable_days": TARGET_DAYS_PERISHABLE,
            "target_ambient_days": TARGET_DAYS_AMBIENT,
        },
        "movement": {
            "usage": [round(_number(r.get("avg_weekly_usage")), 3) for r in rows],
            "value": [round(_number(r.get("inventory_value")), 2) for r in rows],
            "days_supply": [_round_opt(r.get("days_supply")) for r in rows],
            "svsi": [_round_opt(r.get("svsi"), 2) for r in rows],
            "quadrant": [str(r.get("movement_quadrant") or "") for r in rows],
            "posture": [str(r.get("posture") or "") for r in rows],
            "label": [str(r.get("product_name") or "") for r in rows],
        },
    }


def _group_summary(rows: Iterable[dict[str, Any]], key: str, *, order: Iterable[str] | None = None) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "inventory_value": 0.0, "usage_units": 0.0})
    for row in rows:
        label = _safe_text(row.get(key))
        group = groups[label]
        group["count"] += 1
        group["inventory_value"] += _number(row.get("inventory_value"))
        group["usage_units"] += _number(row.get("usage_units"))
    labels = list(order) if order else sorted(groups, key=lambda label: groups[label]["inventory_value"], reverse=True)
    return [{"label": label, **groups[label]} for label in labels if label in groups]


def _supplier_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"skus": 0, "inventory_value": 0.0, "reorder_skus": 0, "backorders": 0.0})
    for row in rows:
        group = groups[_safe_text(row.get("supplier"), "Unassigned")]
        group["skus"] += 1
        group["inventory_value"] += _number(row.get("inventory_value"))
        group["backorders"] += _number(row.get("backorder_qty"))
        if row.get("posture_key") in {"critical", "reorder"}:
            group["reorder_skus"] += 1
    return [
        {"supplier": supplier, **values}
        for supplier, values in sorted(groups.items(), key=lambda item: item[1]["inventory_value"], reverse=True)[:8]
    ]


def _mask_financials(payload: dict[str, Any]) -> None:
    if has_permission("data.cost.view", "data.profit.view"):
        return
    financial_keys = {
        "inventory_value", "holding_cost_annual", "suggested_buy_cost", "unit_cost",
        "capital", "service", "storage", "risk", "total", "sales_cost",
    }

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key in list(value):
                if key in financial_keys:
                    value[key] = None
                else:
                    walk(value[key])
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    payload.setdefault("meta", {})["financials_masked"] = True


def build_inventory_bundle(filters: Any, scope: dict[str, Any], args: Any) -> dict[str, Any]:
    started = time.perf_counter()
    cols = fact_store.list_columns()
    required = {"ProductId", "Date", "OnHandQty", "OnHandValue", "DaysOfSupply", "ReorderPointQty", "SafetyStockQty"}
    missing = sorted(required - cols)
    if missing:
        return {
            "error": {"message": "Inventory fields are not available in this dataset."},
            "warnings": [f"Missing inventory columns: {', '.join(missing)}"],
            "meta": {"page_id": "inventory", "duration_ms": int((time.perf_counter() - started) * 1000)},
        }

    where_sql, where_params, start_iso, end_iso = fact_store.build_where_clause(
        filters, cols, scope, apply_default_window=True
    )
    frame = fact_store.execute_sql_df(_sku_query(where_sql), where_params, tag="inventory.skus")
    rows = _records(frame)
    _classify_rows(rows, end_iso)

    inventory_value = sum(_number(row.get("inventory_value")) for row in rows)
    on_hand_qty = sum(_number(row.get("on_hand_qty")) for row in rows)
    average_weekly_usage = sum(_number(row.get("avg_weekly_usage")) for row in rows)
    backorders = sum(_number(row.get("backorder_qty")) for row in rows)
    weighted_days = (
        sum(_number(row.get("days_supply")) * _number(row.get("inventory_value")) for row in rows) / inventory_value
        if inventory_value else 0.0
    )
    aggregate_weeks_on_hand = on_hand_qty / average_weekly_usage if average_weekly_usage > 0 else None
    annual_turns = 52.0 / aggregate_weeks_on_hand if _number(aggregate_weeks_on_hand) > 0 else None
    critical = [row for row in rows if row.get("posture_key") == "critical"]
    reorder = [row for row in rows if row.get("posture_key") == "reorder"]
    excess = [row for row in rows if row.get("posture_key") == "excess"]
    healthy = [row for row in rows if row.get("posture_key") == "healthy"]

    posture = _group_summary(rows, "posture", order=("Critical", "Reorder", "Healthy", "Excess"))
    abc = _group_summary(rows, "abc_class", order=("A", "B", "C"))
    aging = _group_summary(rows, "aging_bucket", order=("0-30", "31-60", "61-90", "91-180", "181-365", "365+"))
    quadrants = _group_summary(rows, "movement_quadrant", order=("Core / protect", "Fast / lean", "Slow / cash tied", "Tail / monitor"))
    charts = _chart_series(rows, total_inventory=inventory_value)

    total_holding = inventory_value * ANNUAL_HOLDING_RATE
    holding_cost = {
        "annual_rate_pct": ANNUAL_HOLDING_RATE * 100.0,
        "capital": inventory_value * ANNUAL_CAPITAL_RATE,
        "service": inventory_value * ANNUAL_SERVICE_RATE,
        "storage": inventory_value * ANNUAL_STORAGE_RATE,
        "risk": inventory_value * ANNUAL_RISK_RATE,
        "total": total_holding,
    }

    priority_rows = sorted(rows, key=lambda row: (int(row.get("priority") or 0), _number(row.get("suggested_buy_cost")), _number(row.get("revenue"))), reverse=True)
    purchase_plan = [row for row in priority_rows if _number(row.get("suggested_buy_units")) > 0][:15]
    rebalance = sorted(
        [row for row in rows if row.get("posture_key") == "excess" or (_optional_number(row.get("svsi")) or 0) > 1.5],
        key=lambda row: (_number(row.get("inventory_value")), _number(row.get("svsi"))),
        reverse=True,
    )[:15]
    demand_matrix = sorted(rows, key=lambda row: (_number(row.get("inventory_value")), _number(row.get("revenue"))), reverse=True)[:20]

    search = str(getattr(args, "get", lambda *_: "")("search") or getattr(args, "get", lambda *_: "")("q") or "").strip().lower()
    posture_filter = str(getattr(args, "get", lambda *_: "")("posture") or "").strip().lower()
    table_rows = rows
    if search:
        table_rows = [row for row in table_rows if search in f"{row.get('product_id')} {row.get('product_name')} {row.get('supplier')} {row.get('category')}".lower()]
    if posture_filter:
        table_rows = [row for row in table_rows if str(row.get("posture_key")) == posture_filter]
    sort_by = str(getattr(args, "get", lambda *_: "")("sort_by") or "priority").strip().lower()
    sort_key = {
        "inventory_value": lambda row: _number(row.get("inventory_value")),
        "days_supply": lambda row: _number(row.get("days_supply")),
        "turns": lambda row: _number(row.get("annual_turns")),
        "usage": lambda row: _number(row.get("avg_weekly_usage")),
        "name": lambda row: str(row.get("product_name") or "").lower(),
    }.get(sort_by, lambda row: (int(row.get("priority") or 0), _number(row.get("inventory_value"))))
    table_rows = sorted(table_rows, key=sort_key, reverse=sort_by != "name")
    page = max(1, _int(getattr(args, "get", lambda *_: 1)("page") or 1))
    page_size = max(10, min(_int(getattr(args, "get", lambda *_: 25)("page_size") or 25), 100))
    start_idx = (page - 1) * page_size

    strongest_reorder = purchase_plan[0] if purchase_plan else None
    largest_excess = rebalance[0] if rebalance else None
    payload: dict[str, Any] = {
        "kpis": {
            "inventory_value": inventory_value,
            "on_hand_qty": on_hand_qty,
            "sku_count": len(rows),
            "weighted_days_supply": weighted_days,
            "weeks_on_hand": aggregate_weeks_on_hand,
            "annual_turns": annual_turns,
            "critical_skus": len(critical),
            "reorder_skus": len(reorder),
            "healthy_skus": len(healthy),
            "excess_skus": len(excess),
            "stockout_skus": sum(1 for row in rows if bool(row.get("is_stockout"))),
            "backorder_units": backorders,
            "holding_cost_annual": total_holding,
            "start": start_iso,
            "end": end_iso,
        },
        "posture": posture,
        "abc": abc,
        "aging": aging,
        "charts": charts,
        "movement_quadrants": quadrants,
        "holding_cost": holding_cost,
        "suppliers": _supplier_summary(rows),
        "demand_matrix": demand_matrix,
        "purchase_plan": purchase_plan,
        "rebalancing": rebalance,
        "insights": [
            {
                "tone": "danger" if critical or reorder else "success",
                "label": "Replenishment",
                "headline": f"{len(critical) + len(reorder)} SKUs need cover review",
                "detail": f"Highest priority: {strongest_reorder.get('product_name')} ({_number(strongest_reorder.get('days_supply')):.0f} days of supply)." if strongest_reorder else "No SKU is below its cover guardrail in this scope.",
            },
            {
                "tone": "warning" if excess else "success",
                "label": "Working capital",
                "headline": f"{len(excess)} SKUs carry excess cover",
                "detail": f"Largest exposure: {largest_excess.get('product_name')} at {_number(largest_excess.get('days_supply')):.0f} days." if largest_excess else "Inventory cover is inside target bands.",
            },
            {
                "tone": "info",
                "label": "Carrying cost",
                "headline": "11% annual planning rate",
                "detail": "Capital, service, storage, and risk are shown separately so the assumption is visible and auditable.",
            },
        ],
        "table": {
            "rows": table_rows[start_idx : start_idx + page_size],
            "total": len(table_rows),
            "page": page,
            "page_size": page_size,
            "sort_by": sort_by,
        },
        "meta": {
            "page_id": "inventory",
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "formula_version": "inventory-v1",
            "holding_rate_pct": ANNUAL_HOLDING_RATE * 100.0,
            "inventory_source": "Latest observed SKU inventory fields inside the active filtered sales window",
            "svsi_definition": "SKU inventory-value share divided by SKU usage-unit share",
            "abc_definition": "A = first 80% of inventory value; B = next 15%; C = remaining 5%",
            "woh_definition": "Current on-hand units divided by average weekly usage in the active window",
            "turns_definition": "52 divided by weeks on hand",
        },
        "warnings": [],
    }
    _mask_financials(payload)
    return payload
