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
        "actions": _actions(matrix, lanes, concentration),
        "thresholds": {
            "service_target_pct": SERVICE_TARGET_PCT,
            "service_critical_pct": SERVICE_CRITICAL_PCT,
            "concentration_warn_pct": CONCENTRATION_WARN_PCT,
            "min_rows_for_rate": MIN_ROWS_FOR_RATE,
        },
    }
