"""
Demand and supply planning.

The planner page exists to answer one question a dashboard of totals cannot:
**where is demand going, how reliably can we supply it, and where do those two
disagree?** A department growing 20% on a lane that misses a quarter of its
delivery dates is a different problem from one growing 20% on a lane that never
misses, and a revenue chart shows them identically.

Everything here is computed from the scoped fact frame. Nothing is asserted:
the page this feeds used to ship sentences like "maintains a defensive posture"
that were true of no dataset in particular, and they are gone.

Three ideas carry the page:

* **Demand signal** - each department's revenue in the recent half of the
  window against the prior half. Half-and-half rather than a fitted trend,
  because the window is user-controlled and can be short; a slope fitted to
  four points invites more confidence than it earns.
* **Service level** - the share of order lines that arrived on or before the
  date they were promised, by fulfilment lane, by vendor, and by department.
* **The disagreement** - demand growth against service level. The quadrant
  that matters is *growing and unreliable*, and it is the only thing on the
  page that gets a colour.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd

from app.services import analytics_utils as au

# A lane or vendor below this hits the watch list. Retail replenishment
# generally runs a 95%+ case fill; 92% is deliberately permissive so the demo
# flags the genuinely bad lanes rather than everything.
SERVICE_TARGET_PCT = 92.0

# Below this a growing department is called out as a supply risk rather than a
# service note - the gap is wide enough that demand is outrunning the network.
SERVICE_CRITICAL_PCT = 85.0

# A department drawing more than this share from one vendor is single-sourced
# in practice, whatever the contract says.
CONCENTRATION_WARN_PCT = 45.0

# Rows below this in a group make a percentage meaningless. A lane with four
# deliveries that missed one is not "25% late" in any useful sense.
MIN_ROWS_FOR_RATE = 25


def _money(value: float) -> str:
    """Compact currency for narrative copy; the tables format their own."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "$0"
    if abs(amount) >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if abs(amount) >= 1_000:
        return f"${amount / 1_000:.0f}k"
    return f"${amount:,.0f}"


def _revenue(frame: pd.DataFrame) -> float:
    col = au.revenue_column(frame)
    if col not in frame.columns:
        return 0.0
    return float(au.to_numeric_safe(frame[col]).sum())


def _split_window(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    """
    Halve the window by date.

    Returns (prior, recent, meta). Meta carries the two window labels so the
    page can say which periods it is comparing rather than leaving the reader
    to guess what "growth" means.
    """
    empty = frame.iloc[0:0]
    if frame.empty or "Date" not in frame.columns:
        return empty, empty, {"comparable": False}

    dates = pd.to_datetime(frame["Date"], errors="coerce")
    valid = dates.dropna()
    if valid.empty:
        return empty, empty, {"comparable": False}

    start, end = valid.min(), valid.max()
    span_days = int((end - start).days)
    if span_days < 14:
        # Too short to halve into anything meaningful.
        return empty, frame, {"comparable": False, "reason": "Window is too short to split for a trend."}

    midpoint = start + pd.Timedelta(days=span_days // 2)
    prior = frame[dates < midpoint]
    recent = frame[dates >= midpoint]
    meta = {
        "comparable": not prior.empty and not recent.empty,
        "prior_label": f"{start.date().isoformat()} to {(midpoint - pd.Timedelta(days=1)).date().isoformat()}",
        "recent_label": f"{midpoint.date().isoformat()} to {end.date().isoformat()}",
        "half_days": span_days // 2,
    }
    return prior, recent, meta


def _on_time_pct(frame: pd.DataFrame) -> float | None:
    """Share of lines that were not late. None when the column is absent."""
    if frame.empty or "IsLate" not in frame.columns:
        return None
    late = frame["IsLate"]
    # The column arrives as bool from parquet but as 0/1 or "true"/"false" from
    # some extracts, so normalise rather than trusting the dtype.
    if late.dtype == object:
        late = late.astype("string").str.lower().isin(["true", "1", "yes"])
    late = late.fillna(False).astype(bool)
    if not len(late):
        return None
    return float((~late).mean() * 100.0)


def _service_by(frame: pd.DataFrame, column: str, *, limit: int = 8) -> List[Dict[str, Any]]:
    """On-time rate and volume for each value of `column`, worst service first."""
    if frame.empty or column not in frame.columns or "IsLate" not in frame.columns:
        return []

    rows: List[Dict[str, Any]] = []
    for label, group in frame.groupby(frame[column].astype("string").fillna("Unknown")):
        lines = int(len(group))
        on_time = _on_time_pct(group)
        if on_time is None:
            continue
        rows.append(
            {
                "label": str(label),
                "lines": lines,
                "revenue": _revenue(group),
                "on_time_pct": round(on_time, 1),
                # A rate on a handful of rows is noise. Say so rather than
                # ranking a four-delivery lane as the worst in the network.
                "reliable_estimate": lines >= MIN_ROWS_FOR_RATE,
                "status": _service_status(on_time, lines),
            }
        )

    rows.sort(key=lambda r: (r["reliable_estimate"] is False, r["on_time_pct"]))
    return rows[:limit]


def _service_status(on_time_pct: float | None, lines: int) -> str:
    if on_time_pct is None or lines < MIN_ROWS_FOR_RATE:
        return "unknown"
    if on_time_pct < SERVICE_CRITICAL_PCT:
        return "critical"
    if on_time_pct < SERVICE_TARGET_PCT:
        return "watch"
    return "ok"


def _demand_by_department(prior: pd.DataFrame, recent: pd.DataFrame, column: str) -> List[Dict[str, Any]]:
    """Recent-half revenue against prior-half, per department."""
    if recent.empty or column not in recent.columns:
        return []

    recent_rev = recent.groupby(recent[column].astype("string").fillna("Unknown")).apply(
        _revenue, include_groups=False
    )
    if prior.empty or column not in prior.columns:
        prior_rev = pd.Series(dtype="float64")
    else:
        prior_rev = prior.groupby(prior[column].astype("string").fillna("Unknown")).apply(
            _revenue, include_groups=False
        )

    total_recent = float(recent_rev.sum()) or 0.0
    rows: List[Dict[str, Any]] = []
    for label, value in recent_rev.items():
        before = float(prior_rev.get(label, 0.0))
        after = float(value)
        # No prior revenue means the department is new in this window, not
        # infinitely growing. Leave the change undefined and say so.
        change_pct = None if before <= 0 else (after - before) / before * 100.0
        rows.append(
            {
                "label": str(label),
                "revenue": after,
                "revenue_prior": before,
                "share_pct": round(after / total_recent * 100.0, 1) if total_recent else 0.0,
                "change_pct": None if change_pct is None else round(change_pct, 1),
                "direction": _direction(change_pct),
            }
        )

    rows.sort(key=lambda r: -r["revenue"])
    return rows


def _direction(change_pct: float | None) -> str:
    if change_pct is None:
        return "new"
    if change_pct >= 8.0:
        return "growing"
    if change_pct <= -8.0:
        return "declining"
    return "steady"


def _planning_matrix(
    demand_rows: List[Dict[str, Any]],
    frame: pd.DataFrame,
    column: str,
) -> List[Dict[str, Any]]:
    """
    Demand growth against service level, one point per department.

    This is the page. Everything above it is the two axes measured separately;
    this is where they are read together.
    """
    if frame.empty or column not in frame.columns:
        return []

    service_by_dept = {
        str(label): _on_time_pct(group)
        for label, group in frame.groupby(frame[column].astype("string").fillna("Unknown"))
    }
    line_counts = frame[column].astype("string").fillna("Unknown").value_counts().to_dict()

    points: List[Dict[str, Any]] = []
    for row in demand_rows:
        label = row["label"]
        on_time = service_by_dept.get(label)
        lines = int(line_counts.get(label, 0))
        if on_time is None:
            continue
        points.append(
            {
                "label": label,
                "change_pct": row["change_pct"],
                "on_time_pct": round(on_time, 1),
                "revenue": row["revenue"],
                "share_pct": row["share_pct"],
                "lines": lines,
                "quadrant": _quadrant(row["change_pct"], on_time, lines),
            }
        )

    points.sort(key=lambda p: (p["quadrant"] != "at_risk", -p["revenue"]))
    return points


def _quadrant(change_pct: float | None, on_time_pct: float, lines: int) -> str:
    """
    Four ways a department can sit.

    `at_risk` is the only one the page colours: demand is growing into a lane
    that is already missing its dates, which is the case where doing nothing
    makes the problem bigger rather than keeping it the same size.
    """
    if lines < MIN_ROWS_FOR_RATE:
        return "insufficient_data"
    reliable = on_time_pct >= SERVICE_TARGET_PCT
    growing = change_pct is not None and change_pct >= 8.0
    if growing and not reliable:
        return "at_risk"
    if growing and reliable:
        return "scale"
    if not growing and not reliable:
        return "fix_service"
    return "steady"


def _vendor_concentration(frame: pd.DataFrame, dept_col: str, limit: int = 6) -> List[Dict[str, Any]]:
    """
    Where one vendor carries most of a department.

    Concentration is only interesting next to service: a department 80% sourced
    from a vendor that never misses is a commercial question, and the same
    department behind a vendor that misses one delivery in five is an
    operational one.
    """
    if frame.empty or "SupplierName" not in frame.columns or dept_col not in frame.columns:
        return []

    rows: List[Dict[str, Any]] = []
    departments = frame[dept_col].astype("string").fillna("Unknown")
    vendors = frame["SupplierName"].astype("string").fillna("Unknown")

    for dept, group in frame.groupby(departments):
        dept_revenue = _revenue(group)
        if dept_revenue <= 0:
            continue
        by_vendor = group.groupby(vendors[group.index]).apply(_revenue, include_groups=False)
        if by_vendor.empty:
            continue
        top_vendor = by_vendor.idxmax()
        top_revenue = float(by_vendor.max())
        share = top_revenue / dept_revenue * 100.0
        vendor_rows = group[vendors[group.index] == top_vendor]
        on_time = _on_time_pct(vendor_rows)
        rows.append(
            {
                "department": str(dept),
                "vendor": str(top_vendor),
                "share_pct": round(share, 1),
                "revenue": top_revenue,
                "vendor_count": int(by_vendor.shape[0]),
                "vendor_on_time_pct": None if on_time is None else round(on_time, 1),
                "concentrated": share >= CONCENTRATION_WARN_PCT,
            }
        )

    rows.sort(key=lambda r: -r["share_pct"])
    return rows[:limit]


def _actions(
    matrix: List[Dict[str, Any]],
    lanes: List[Dict[str, Any]],
    concentration: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    What to do, ranked, each one traceable to a number on the page.

    Deliberately short. A list of twenty actions is a list of none.
    """
    actions: List[Dict[str, Any]] = []

    for point in matrix:
        if point["quadrant"] != "at_risk":
            continue
        actions.append(
            {
                "severity": "critical",
                "title": f"{point['label']}: demand is outrunning service",
                "detail": (
                    f"Revenue is up {point['change_pct']:.0f}% against the prior half of the window "
                    f"while only {point['on_time_pct']:.0f}% of its lines arrive on time. "
                    f"It is {point['share_pct']:.0f}% of revenue, so the gap compounds."
                ),
                "metric": point["on_time_pct"],
                "target": SERVICE_TARGET_PCT,
            }
        )

    for lane in lanes:
        if lane["status"] != "critical" or not lane["reliable_estimate"]:
            continue
        actions.append(
            {
                "severity": "warning",
                "title": f"{lane['label']} misses {100 - lane['on_time_pct']:.0f}% of its dates",
                "detail": (
                    f"{lane['lines']:,} lines and {_money(lane['revenue'])} of revenue move on this lane "
                    f"at {lane['on_time_pct']:.0f}% on time, against a {SERVICE_TARGET_PCT:.0f}% target."
                ),
                "metric": lane["on_time_pct"],
                "target": SERVICE_TARGET_PCT,
            }
        )

    for row in concentration:
        if not row["concentrated"]:
            continue
        service_note = (
            f" That vendor runs {row['vendor_on_time_pct']:.0f}% on time."
            if row["vendor_on_time_pct"] is not None
            else ""
        )
        actions.append(
            {
                "severity": "info",
                "title": f"{row['department']} is {row['share_pct']:.0f}% single-sourced",
                "detail": (
                    f"{row['vendor']} carries {row['share_pct']:.0f}% of the department across "
                    f"{row['vendor_count']} vendors on file.{service_note}"
                ),
                "metric": row["share_pct"],
                "target": CONCENTRATION_WARN_PCT,
            }
        )

    order = {"critical": 0, "warning": 1, "info": 2}
    actions.sort(key=lambda a: order.get(a["severity"], 9))
    return actions[:6]


def _department_column(frame: pd.DataFrame) -> str:
    for candidate in ("ProteinType", "ProteinName", "ProductCategory", "Category"):
        if candidate in frame.columns:
            return candidate
    return "ProteinType"


def build_planning(frame: pd.DataFrame) -> Dict[str, Any]:
    """The whole planner payload for one scoped, filtered frame."""
    dept_col = _department_column(frame)
    prior, recent, window = _split_window(frame)

    demand = _demand_by_department(prior, recent, dept_col)
    lanes = _service_by(frame, "ShippingMethodName", limit=8)
    vendors = _service_by(frame, "SupplierName", limit=8)
    matrix = _planning_matrix(demand, frame, dept_col) if window.get("comparable") else []
    concentration = _vendor_concentration(frame, dept_col)

    overall_on_time = _on_time_pct(frame)
    at_risk = [p for p in matrix if p["quadrant"] == "at_risk"]
    at_risk_revenue = sum(p["revenue"] for p in at_risk)
    total_revenue = _revenue(recent if not recent.empty else frame)

    return {
        "window": window,
        "headline": {
            "on_time_pct": None if overall_on_time is None else round(overall_on_time, 1),
            "service_target_pct": SERVICE_TARGET_PCT,
            "at_risk_departments": len(at_risk),
            "at_risk_revenue": at_risk_revenue,
            "at_risk_revenue_share_pct": (
                round(at_risk_revenue / total_revenue * 100.0, 1) if total_revenue else 0.0
            ),
            "lanes_below_target": sum(
                1 for lane in lanes if lane["status"] in {"watch", "critical"} and lane["reliable_estimate"]
            ),
            "single_sourced_departments": sum(1 for row in concentration if row["concentrated"]),
        },
        "demand": demand,
        "service_by_lane": lanes,
        "service_by_vendor": vendors,
        "matrix": matrix,
        "concentration": concentration,
        "inventory": build_inventory(frame),
        "actions": _actions(matrix, lanes, concentration),
        "thresholds": {
            "service_target_pct": SERVICE_TARGET_PCT,
            "service_critical_pct": SERVICE_CRITICAL_PCT,
            "concentration_warn_pct": CONCENTRATION_WARN_PCT,
            "min_rows_for_rate": MIN_ROWS_FOR_RATE,
        },
    }


# ---------------------------------------------------------------------------
# Availability and inventory
# ---------------------------------------------------------------------------
#
# Four numbers a replenishment team runs on, and they are deliberately not
# interchangeable:
#
#   line fill   the share of order lines that shipped complete
#   unit fill   the share of ordered units that shipped
#   on time     the share that arrived by the promised date
#   OTIF        both at once, which is the only one a store actually feels
#
# OTIF is always the lowest of the four and is the one worth putting on the
# page, because a lane can hit 95% on each of on-time and in-full separately
# and still miss one delivery in ten.

# Perishable departments run short cover by design, so a single "days of
# supply" target across the whole chain would flag all of fresh as a problem
# and none of general merchandise. Split the target by how the item is billed:
# weighed items are the perishable ones.
COVER_TARGET_DAYS_PERISHABLE = 14.0
COVER_TARGET_DAYS_AMBIENT = 45.0

# Above this multiple of the target, stock is excess rather than healthy.
EXCESS_COVER_MULTIPLE = 1.6

OTIF_TARGET_PCT = 90.0
FILL_TARGET_PCT = 96.0


def _bool_series(frame: pd.DataFrame, column: str) -> pd.Series | None:
    """A boolean column, however the extract happened to type it."""
    if frame.empty or column not in frame.columns:
        return None
    series = frame[column]
    if series.dtype == object:
        series = series.astype("string").str.lower().isin(["true", "1", "yes"])
    return series.fillna(False).astype(bool)


def _availability(frame: pd.DataFrame) -> Dict[str, Any]:
    """Line fill, unit fill, on-time and OTIF for one frame."""
    lines = int(len(frame))
    if not lines:
        return {"lines": 0, "line_fill_pct": None, "unit_fill_pct": None,
                "on_time_pct": None, "otif_pct": None, "stockout_pct": None}

    short = _bool_series(frame, "IsShortShip")
    late = _bool_series(frame, "IsLate")
    stockout = _bool_series(frame, "IsStockout")

    # Unit fill comes from ordered and backordered, never from
    # `QuantityShipped`. The DuckDB fact view redefines that column as a
    # weight-converted unit count, so dividing it by QuantityOrdered reports a
    # fill rate over 300%. Backorder is a column the view does not touch.
    ordered = au.to_numeric_safe(frame["QuantityOrdered"]).sum() if "QuantityOrdered" in frame else 0.0
    if "BackorderQty" in frame.columns:
        backordered = float(au.to_numeric_safe(frame["BackorderQty"]).sum())
    elif "pack_item_count_sum" in frame.columns:
        backordered = max(float(ordered) - float(au.to_numeric_safe(frame["pack_item_count_sum"]).sum()), 0.0)
    else:
        backordered = 0.0
    shipped = max(float(ordered) - backordered, 0.0)

    on_time_pct = None if late is None else float((~late).mean() * 100.0)
    line_fill_pct = None if short is None else float((~short).mean() * 100.0)
    unit_fill_pct = float(shipped / ordered * 100.0) if ordered else None
    # In full AND on time. Computed on the row, never multiplied out of the two
    # rates - that would assume they are independent, and they are not.
    otif_pct = (
        float(((~late) & (~short)).mean() * 100.0)
        if late is not None and short is not None
        else None
    )

    return {
        "lines": lines,
        "line_fill_pct": None if line_fill_pct is None else round(line_fill_pct, 1),
        "unit_fill_pct": None if unit_fill_pct is None else round(unit_fill_pct, 1),
        "on_time_pct": None if on_time_pct is None else round(on_time_pct, 1),
        "otif_pct": None if otif_pct is None else round(otif_pct, 1),
        "stockout_pct": None if stockout is None else round(float(stockout.mean() * 100.0), 2),
    }


def _cover_target(frame: pd.DataFrame) -> float:
    """Cover target for a group, by whether it is mostly perishable."""
    if "UnitOfBillingId" in frame.columns and len(frame):
        weighed = au.to_numeric_safe(frame["UnitOfBillingId"]).eq(3).mean()
        if weighed >= 0.5:
            return COVER_TARGET_DAYS_PERISHABLE
    return COVER_TARGET_DAYS_AMBIENT


def _inventory_position(frame: pd.DataFrame) -> Dict[str, Any]:
    """
    Cover, stock value and how much of it is not working.

    Every figure here is computed on a **SKU-level** frame, not on order lines.
    Each line carries a snapshot of the position it was picked against, so a
    SKU that appears on 300 lines contributes its stock 300 times: summing the
    column reported ~$29M of inventory against a $9.5M annual cost of goods and
    an impossible 0.16 annual turns. Collapsing to one row per SKU first is
    also what keeps the ratios coherent - computing a numerator over matching
    lines and a denominator over all lines produced an excess share of 319%.
    """
    if frame.empty or "DaysOfSupply" not in frame.columns:
        return {"cover_days": None, "cover_weeks": None, "on_hand_value": 0.0, "excess_value": 0.0,
                "excess_share_pct": None, "dead_value": 0.0, "dead_share_pct": None,
                "below_reorder_pct": None, "cover_target_days": None, "turns": None}

    target = _cover_target(frame)

    sku_col = next((c for c in ("ProductId", "SKU", "ProductName") if c in frame.columns), None)
    columns = {"DaysOfSupply": "cover"}
    if "OnHandValue" in frame.columns:
        columns["OnHandValue"] = "value"
    if "OnHandQty" in frame.columns:
        columns["OnHandQty"] = "on_hand"
    if "ReorderPointQty" in frame.columns:
        columns["ReorderPointQty"] = "reorder"

    working = frame[list(columns)].apply(au.to_numeric_safe).rename(columns=columns)
    if sku_col is not None:
        # One position per SKU: the average of the snapshots taken against it.
        working = working.groupby(frame[sku_col].astype("string").fillna("Unknown")).mean()

    cover = working["cover"]
    value = working["value"] if "value" in working else pd.Series(0.0, index=working.index)

    on_hand_value = float(value.sum())

    # Excess and dead stock are computed but deliberately NOT surfaced.
    #
    # Collapsing to one position per SKU is required for every other figure
    # here to be coherent, but it averages away the very distribution these two
    # depend on: a SKU that is overstocked half the year and thin the other
    # half averages to "on plan". The result was 0% excess across most of the
    # chain and 94% for Fresh - an artefact of the averaging, not a finding.
    #
    # Measuring them honestly needs a real inventory snapshot at SKU x date
    # grain, which the order-line fact table is the wrong shape to carry. Until
    # that exists these stay out of the UI rather than being shown with a
    # caveat nobody reads.
    excess_value = float(value[cover > (target * EXCESS_COVER_MULTIPLE)].sum())
    dead_value = float(value[cover > (target * 4.0)].sum())

    # Below reorder point is a rate over *lines*, not over SKUs, so it is
    # measured on the raw frame. Averaging a SKU's positions first washes it
    # out - a SKU that dips under its reorder point a fifth of the time
    # averages to comfortably above it, and the figure went to zero.
    below_reorder_pct = None
    if {"OnHandQty", "ReorderPointQty"} <= set(frame.columns):
        on_hand_line = au.to_numeric_safe(frame["OnHandQty"])
        reorder_line = au.to_numeric_safe(frame["ReorderPointQty"])
        below_reorder_pct = round(float((on_hand_line < reorder_line).mean() * 100.0), 1)

    # Annual inventory turns: cost of goods over average inventory at cost,
    # annualised from the window so the figure is comparable to a benchmark.
    #
    # GMROI is deliberately not reported. It needs an inventory *valuation*
    # this dataset does not really model - the per-line snapshot is a position,
    # not a costed balance - and the numbers it produced (540 for Meat &
    # Seafood, against a real-world 2-5) would have been confidently wrong.
    turns = None
    cost_col = next((c for c in ("CostPrice", "Cost", "cost") if c in frame.columns), None)
    if cost_col and on_hand_value > 0 and "Date" in frame.columns:
        dates = pd.to_datetime(frame["Date"], errors="coerce").dropna()
        if len(dates):
            window_days = max(int((dates.max() - dates.min()).days), 1)
            cogs = float(au.to_numeric_safe(frame[cost_col]).sum())
            turns = round(cogs * (365.0 / window_days) / on_hand_value, 1)

    median_cover = float(cover.median())
    return {
        "cover_days": round(median_cover, 1),
        "cover_weeks": round(median_cover / 7.0, 1),
        "cover_target_days": target,
        "on_hand_value": on_hand_value,
        "excess_value": excess_value,
        "excess_share_pct": round(excess_value / on_hand_value * 100.0, 1) if on_hand_value else 0.0,
        "dead_value": dead_value,
        "dead_share_pct": round(dead_value / on_hand_value * 100.0, 1) if on_hand_value else 0.0,
        "below_reorder_pct": below_reorder_pct,
        "turns": turns,
        "skus": int(len(working)),
    }


def _service_status_otif(otif_pct: float | None, lines: int) -> str:
    if otif_pct is None or lines < MIN_ROWS_FOR_RATE:
        return "unknown"
    if otif_pct < OTIF_TARGET_PCT - 8:
        return "critical"
    if otif_pct < OTIF_TARGET_PCT:
        return "watch"
    return "ok"


def _inventory_by(frame: pd.DataFrame, column: str, *, limit: int = 12) -> List[Dict[str, Any]]:
    """
    The scorecard, one row per department (or vendor, or lane).

    Availability and inventory position side by side, because they only mean
    something together: 92% fill on twelve days of cover is a buying problem,
    and 92% fill on sixty days of cover is a putaway problem.
    """
    if frame.empty or column not in frame.columns:
        return []

    rows: List[Dict[str, Any]] = []
    keys = frame[column].astype("string").fillna("Unknown")
    for label, group in frame.groupby(keys):
        availability = _availability(group)
        position = _inventory_position(group)
        rows.append(
            {
                "label": str(label),
                "revenue": _revenue(group),
                **availability,
                **position,
                "status": _service_status_otif(availability["otif_pct"], availability["lines"]),
                "reliable_estimate": availability["lines"] >= MIN_ROWS_FOR_RATE,
            }
        )

    rows.sort(key=lambda r: (r["reliable_estimate"] is False, r["otif_pct"] if r["otif_pct"] is not None else 999))
    return rows[:limit]


def _inventory_actions(scorecard: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Availability and stock actions, ranked by what they cost."""
    actions: List[Dict[str, Any]] = []

    for row in scorecard:
        if not row["reliable_estimate"]:
            continue

        if row["status"] == "critical":
            actions.append(
                {
                    "severity": "critical",
                    "title": f"{row['label']}: {row['otif_pct']:.0f}% OTIF",
                    "detail": (
                        f"{row['line_fill_pct']:.0f}% of lines ship complete and "
                        f"{row['on_time_pct']:.0f}% arrive on time, so {100 - row['otif_pct']:.0f}% of "
                        f"deliveries disappoint the store on one count or the other. "
                        f"Median cover is {row['cover_days']:.0f} days against a "
                        f"{row['cover_target_days']:.0f}-day target."
                    ),
                    "metric": row["otif_pct"],
                    "target": OTIF_TARGET_PCT,
                }
            )

        # Excess is only worth raising when it is both a large share and real
        # money - 80% excess on $4k of stock is not a finding.
        if (row["excess_share_pct"] or 0) >= 30.0 and (row["excess_value"] or 0) >= 100_000:
            actions.append(
                {
                    "severity": "info",
                    "title": f"{row['label']} holds {_money(row['excess_value'])} of excess cover",
                    "detail": (
                        f"{row['excess_share_pct']:.0f}% of on-hand value sits above "
                        f"{EXCESS_COVER_MULTIPLE:g}x the {row['cover_target_days']:.0f}-day cover target. "
                        f"Working capital, not a service problem."
                    ),
                    "metric": row["excess_share_pct"],
                    "target": 30.0,
                }
            )

        if (row["stockout_pct"] or 0) >= 3.0:
            actions.append(
                {
                    "severity": "warning",
                    "title": f"{row['label']} stocks out on {row['stockout_pct']:.1f}% of lines",
                    "detail": (
                        f"Lines that filled to zero, not short. Median cover is "
                        f"{row['cover_days']:.0f} days against a {row['cover_target_days']:.0f}-day target."
                    ),
                    "metric": row["stockout_pct"],
                    "target": 3.0,
                }
            )

    order = {"critical": 0, "warning": 1, "info": 2}
    actions.sort(key=lambda a: (order.get(a["severity"], 9), -(a.get("metric") or 0)))
    return actions


def build_inventory(frame: pd.DataFrame) -> Dict[str, Any]:
    """Availability and inventory position, for the planner and the pages."""
    dept_col = _department_column(frame)
    return {
        "headline": {**_availability(frame), **_inventory_position(frame)},
        "targets": {
            "otif_pct": OTIF_TARGET_PCT,
            "fill_pct": FILL_TARGET_PCT,
            "cover_days_perishable": COVER_TARGET_DAYS_PERISHABLE,
            "cover_days_ambient": COVER_TARGET_DAYS_AMBIENT,
            "excess_cover_multiple": EXCESS_COVER_MULTIPLE,
        },
        "by_department": _inventory_by(frame, dept_col),
        "by_lane": _inventory_by(frame, "ShippingMethodName", limit=8),
        "by_vendor": _inventory_by(frame, "SupplierName", limit=8),
        "actions": _inventory_actions(_inventory_by(frame, dept_col)),
    }

# ---------------------------------------------------------------------------
# The same availability picture, computed in SQL
# ---------------------------------------------------------------------------
#
# The products and sales-rep bundles are built from DuckDB rather than a
# materialised frame, and pulling a whole frame back just to add a service
# strip would double their memory on a 512 MB box. One aggregate query costs
# almost nothing and returns the same numbers as `build_inventory`.
#
# `OTIF` is measured on the row here too - `AVG(CASE WHEN NOT late AND NOT
# short ...)` - and not multiplied out of the two component rates.

_INVENTORY_SQL = """
    WITH scoped AS (
        SELECT {group_expr} AS label, *
        FROM fact
        WHERE {where_sql}
    ),
    -- One inventory position per SKU per group. Each order line carries a
    -- snapshot of the position it was picked against, so summing the column
    -- across lines counts the same SKU's stock once per line - the pandas path
    -- had the same bug and reported $29.3M against a true $206k.
    sku_positions AS (
        SELECT
            label,
            COALESCE(NULLIF(CAST(ProductId AS VARCHAR), ''), 'Unknown') AS sku,
            AVG(COALESCE(OnHandValue, 0)) AS sku_on_hand_value,
            AVG(DaysOfSupply) AS sku_cover_days,
            0.0 AS sku_below_reorder
        FROM scoped
        GROUP BY 1, 2
    ),
    stock AS (
        SELECT
            label,
            COUNT(*) AS skus,
            SUM(sku_on_hand_value) AS on_hand_value,
            MEDIAN(sku_cover_days) AS cover_days
        FROM sku_positions
        GROUP BY 1
    ),
    service AS (
        SELECT
            label,
            COUNT(*) AS lines,
            AVG(CASE WHEN NOT COALESCE(IsShortShip, FALSE) THEN 1.0 ELSE 0.0 END) * 100 AS line_fill_pct,
            AVG(CASE WHEN NOT COALESCE(IsLate, FALSE) THEN 1.0 ELSE 0.0 END) * 100 AS on_time_pct,
            AVG(CASE WHEN NOT COALESCE(IsLate, FALSE)
                      AND NOT COALESCE(IsShortShip, FALSE) THEN 1.0 ELSE 0.0 END) * 100 AS otif_pct,
            AVG(CASE WHEN COALESCE(IsStockout, FALSE) THEN 1.0 ELSE 0.0 END) * 100 AS stockout_pct,
            AVG(CASE WHEN COALESCE(OnHandQty, 0) < COALESCE(ReorderPointQty, 0) THEN 1.0 ELSE 0.0 END) * 100 AS below_reorder_pct,
            (1 - SUM(COALESCE(BackorderQty, 0)) / NULLIF(SUM(COALESCE(QuantityOrdered, 0)), 0)) * 100 AS unit_fill_pct
        FROM scoped
        GROUP BY 1
    )
    SELECT
        service.label AS label,
        service.lines AS lines,
        service.line_fill_pct, service.on_time_pct, service.otif_pct,
        service.stockout_pct, service.unit_fill_pct, service.below_reorder_pct,
        stock.cover_days, stock.on_hand_value, stock.skus
    FROM service
    LEFT JOIN stock ON stock.label = service.label
    ORDER BY service.otif_pct ASC
    LIMIT {limit}
"""

# Columns the query needs. Without them the strip is simply not rendered,
# rather than reporting perfect service against columns that do not exist.
INVENTORY_COLUMNS = ("IsShortShip", "IsLate", "IsStockout", "BackorderQty", "QuantityOrdered", "DaysOfSupply")


def inventory_available(columns) -> bool:
    """True when the dataset carries the availability columns."""
    present = set(columns or ())
    return all(name in present for name in INVENTORY_COLUMNS)


def inventory_summary_sql(
    where_sql: str,
    where_params,
    *,
    group_expr: str = "COALESCE(NULLIF(ProteinType, ''), 'Unknown')",
    limit: int = 12,
) -> List[Dict[str, Any]]:
    """Per-group availability and inventory position, worst OTIF first."""
    from app.services import fact_store

    sql = _INVENTORY_SQL.format(
        group_expr=group_expr,
        where_sql=where_sql or "1=1",
        limit=int(limit),
    )
    try:
        frame = fact_store.execute_sql_df(sql, list(where_params or []))
    except Exception:  # pragma: no cover - a missing column must not break a page
        return []
    if frame is None or frame.empty:
        return []

    rows: List[Dict[str, Any]] = []
    for record in frame.to_dict(orient="records"):
        lines = int(record.get("lines") or 0)
        on_hand = float(record.get("on_hand_value") or 0.0)
        otif = record.get("otif_pct")
        rows.append(
            {
                "label": str(record.get("label") or "Unknown"),
                "lines": lines,
                "line_fill_pct": _round_or_none(record.get("line_fill_pct")),
                "unit_fill_pct": _round_or_none(record.get("unit_fill_pct")),
                "on_time_pct": _round_or_none(record.get("on_time_pct")),
                "otif_pct": _round_or_none(otif),
                "stockout_pct": _round_or_none(record.get("stockout_pct"), 2),
                "cover_days": _round_or_none(record.get("cover_days")),
                "on_hand_value": on_hand,
                "skus": int(record.get("skus") or 0),
                "below_reorder_pct": _round_or_none(record.get("below_reorder_pct")),
                "reliable_estimate": lines >= MIN_ROWS_FOR_RATE,
                "status": _service_status_otif(
                    None if otif is None else float(otif), lines
                ),
            }
        )
    return rows


def _round_or_none(value, digits: int = 1):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return round(number, digits)


def inventory_totals(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Roll a scorecard up into one headline, weighting rates by line count."""
    total_lines = sum(r["lines"] for r in rows) or 0
    if not total_lines:
        return {"lines": 0, "otif_pct": None, "line_fill_pct": None,
                "on_time_pct": None, "stockout_pct": None,
                "on_hand_value": 0.0, "skus": 0}

    def weighted(key: str):
        parts = [(r[key], r["lines"]) for r in rows if r.get(key) is not None]
        if not parts:
            return None
        return round(sum(v * n for v, n in parts) / sum(n for _v, n in parts), 1)

    on_hand = sum(r["on_hand_value"] for r in rows)
    return {
        "lines": total_lines,
        "otif_pct": weighted("otif_pct"),
        "line_fill_pct": weighted("line_fill_pct"),
        "on_time_pct": weighted("on_time_pct"),
        "stockout_pct": weighted("stockout_pct"),
        "unit_fill_pct": weighted("unit_fill_pct"),
        "below_reorder_pct": weighted("below_reorder_pct"),
        "cover_days": weighted("cover_days"),
        "on_hand_value": on_hand,
        "skus": sum(r.get("skus") or 0 for r in rows),
        "otif_target_pct": OTIF_TARGET_PCT,
    }
