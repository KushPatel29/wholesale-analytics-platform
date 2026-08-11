"""
One definition of every metric that appears on more than one page.

The demo shipped these contradictions, all visible on screen at the same time:

    Customers   health bar `Active (30d): 0`  beside KPI card `ACTIVE CUSTOMERS 80`
    Customers   `Revenue at risk: $0.00`, card subtext `$19,187.00 (26.9%)`,
                and a panel listing five at-risk accounts totalling ~$79k
    Customers   `Repeat rate: 100.0%`
    Overview    `HHI 197`   Suppliers `HHI (Market Conc.) 378.0`
                Regions `Revenue Concentration 1,933`
    Customers   margin 21.1%  vs  Suppliers 20.5%, on identical revenue and AOV

None of these are arithmetic mistakes. Each page had its own implementation of
the same idea, and the implementations disagreed - about the reference date,
about the denominator, about the scale. So the fix is not to correct a formula,
it is to have one formula.

Everything here is pure: values in, value out, no query and no request context.
That keeps it testable and keeps the aggregation where it already lives.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Optional, Sequence

# ─────────────────────────────────────────────────────────────────────────────
# Recency thresholds
# ─────────────────────────────────────────────────────────────────────────────
# Days since a customer's last order. These are the definitions; the reference
# date they are measured from is `comparison.effective_today()`, which is the
# data cutoff rather than the wall clock.
#
# Measuring from the wall clock is what produced `Active (30d): 0`: the dataset
# stopped five weeks before the server's today, so no customer could possibly
# fall inside a 30-day window, and the health bar reported an empty book beside
# a KPI card that counted 80 active accounts.
ACTIVE_WITHIN_DAYS = 30
AT_RISK_AFTER_DAYS = 60
CHURNED_AFTER_DAYS = 120

# ─────────────────────────────────────────────────────────────────────────────
# Materiality floor for growth percentages
# ─────────────────────────────────────────────────────────────────────────────
# A growth percentage against a near-zero base is not a large number, it is a
# meaningless one. The demo reported +2261.5% for an account whose prior period
# held $13,078 across five orders, and +896.5% at company level.
#
# Below either floor the percentage is suppressed and the reason is rendered
# instead - `New` when there was no prior activity at all, `n/a` when there was
# too little to divide by.
MATERIALITY_MIN_PRIOR_REVENUE = 5_000.0
MATERIALITY_MIN_PRIOR_ORDERS = 10


@dataclass(frozen=True)
class GrowthResult:
    """A growth figure, or an explicit reason there isn't one."""

    pct: Optional[float]
    label: str
    # "ok" | "new" | "immaterial" | "no_prior"
    reason: str

    @property
    def is_shown(self) -> bool:
        return self.pct is not None

    def as_dict(self) -> dict[str, Any]:
        return {"pct": self.pct, "label": self.label, "reason": self.reason}


def growth_pct(
    current: Any,
    prior: Any,
    *,
    prior_orders: Any = None,
    min_prior_value: float = MATERIALITY_MIN_PRIOR_REVENUE,
    min_prior_orders: int = MATERIALITY_MIN_PRIOR_ORDERS,
) -> GrowthResult:
    """
    Period-over-period growth, or an honest refusal.

    `prior_orders` is optional and only checked when supplied: a revenue base
    can look material while resting on two orders, and two orders is not a
    trend. Pass it wherever the count is to hand.
    """
    current_value = _to_float(current)
    prior_value = _to_float(prior)

    if current_value is None or prior_value is None:
        return GrowthResult(None, "—", "no_prior")

    if prior_value <= 0:
        # No prior activity at all is a fact worth stating, not a divide-by-zero.
        if current_value > 0:
            return GrowthResult(None, "New", "new")
        return GrowthResult(None, "—", "no_prior")

    if prior_value < float(min_prior_value):
        return GrowthResult(None, "n/a", "immaterial")

    if prior_orders is not None:
        count = _to_float(prior_orders)
        if count is not None and count < float(min_prior_orders):
            return GrowthResult(None, "n/a", "immaterial")

    pct = (current_value - prior_value) / abs(prior_value) * 100.0
    return GrowthResult(pct, f"{pct:+.1f}%", "ok")


# ─────────────────────────────────────────────────────────────────────────────
# Customer lifecycle
# ─────────────────────────────────────────────────────────────────────────────
def days_since(last_order: Any, reference: date) -> Optional[int]:
    parsed = _to_date(last_order)
    if parsed is None:
        return None
    return (reference - parsed).days


def customer_status(last_order: Any, reference: date) -> str:
    """
    `active` | `at_risk` | `churned` | `unknown`.

    One ladder, measured from one reference date. Every page that shows a
    lifecycle segment, a health bar, an RFM band or a churn-risk list reads
    this, so the four of them cannot disagree about what "active" means.
    """
    elapsed = days_since(last_order, reference)
    if elapsed is None:
        return "unknown"
    if elapsed <= ACTIVE_WITHIN_DAYS:
        return "active"
    if elapsed <= AT_RISK_AFTER_DAYS:
        return "at_risk"
    if elapsed <= CHURNED_AFTER_DAYS:
        return "at_risk"
    return "churned"


def is_active(last_order: Any, reference: date) -> bool:
    return customer_status(last_order, reference) == "active"


def is_at_risk(last_order: Any, reference: date) -> bool:
    return customer_status(last_order, reference) == "at_risk"


def is_churned(last_order: Any, reference: date) -> bool:
    return customer_status(last_order, reference) == "churned"


def revenue_at_risk(rows: Iterable[Mapping[str, Any]], reference: date) -> float:
    """
    Revenue belonging to accounts currently classified `at_risk`.

    The demo gave three different answers to this on one screen - $0.00 in the
    health bar, $19,187.00 in the card subtext, and ~$79k in the panel below -
    because each was computed separately against a different recency rule.
    """
    total = 0.0
    for row in rows or ():
        if is_at_risk(row.get("last_order_date") or row.get("LastOrderDate"), reference):
            total += _to_float(row.get("revenue") or row.get("Revenue")) or 0.0
    return total


def repeat_rate(order_counts: Sequence[Any]) -> Optional[float]:
    """
    Share of customers with more than one order, as a percentage.

    Reported `100.0%` on the demo, which is implausible on its face and was:
    the denominator was customers *in the window who had ordered*, so a
    single-order customer counted in both halves of the fraction. Anyone with
    at least one order is the denominator; anyone with two or more is the
    numerator.
    """
    counts = [c for c in (_to_float(v) for v in order_counts or ()) if c is not None]
    ordering = [c for c in counts if c >= 1]
    if not ordering:
        return None
    repeat = sum(1 for c in ordering if c >= 2)
    return repeat / len(ordering) * 100.0


# ─────────────────────────────────────────────────────────────────────────────
# Profitability
# ─────────────────────────────────────────────────────────────────────────────
def margin_pct(revenue: Any, cost: Any = None, *, profit: Any = None) -> Optional[float]:
    """
    Gross margin as a percentage of revenue.

    Give it cost or profit, not both interpretations. The Customers and
    Suppliers pages reported 21.1% and 20.5% on identical revenue: one divided
    profit by revenue, the other averaged the per-row margin percentages, which
    weights a $12 line the same as a $120,000 one. This is the revenue-weighted
    definition, which is the only one that adds up.
    """
    total_revenue = _to_float(revenue)
    if total_revenue is None or abs(total_revenue) < 1e-9:
        return None
    if profit is not None:
        total_profit = _to_float(profit)
    else:
        total_cost = _to_float(cost)
        total_profit = None if total_cost is None else total_revenue - total_cost
    if total_profit is None:
        return None
    return total_profit / total_revenue * 100.0


# ─────────────────────────────────────────────────────────────────────────────
# Concentration
# ─────────────────────────────────────────────────────────────────────────────
def herfindahl_index(values: Iterable[Any]) -> Optional[float]:
    """
    HHI on the standard 0-10,000 scale.

    The demo reported this three ways at once - Overview 197, Suppliers 378.0,
    Regions 1,933 - for the same company under the same filter. Two causes: the
    pages measured concentration over different dimensions without saying which,
    and at least one of them used the 0-1 scale while labelling it like the
    0-10,000 one.

    Scale is fixed here. Dimension is not something a shared function can
    decide, so callers pass the dimension's values and label the result with
    `hhi_label()` - "HHI (customers)", never a bare "HHI".
    """
    amounts = [v for v in (_to_float(x) for x in values or ()) if v is not None and v > 0]
    total = sum(amounts)
    if total <= 0 or not amounts:
        return None
    return sum((amount / total * 100.0) ** 2 for amount in amounts)


def hhi_label(dimension: str) -> str:
    """`HHI (customers)`. A bare "HHI" is what let three of them disagree."""
    return f"HHI ({dimension})"


def hhi_band(value: Optional[float]) -> str:
    """The DOJ/FTC reading of an HHI, so the number carries its meaning."""
    if value is None:
        return "—"
    if value < 1500:
        return "unconcentrated"
    if value < 2500:
        return "moderately concentrated"
    return "highly concentrated"


def top_n_share(values: Iterable[Any], n: int = 5) -> Optional[float]:
    """Share of the total held by the largest `n`, as a percentage."""
    amounts = sorted(
        (v for v in (_to_float(x) for x in values or ()) if v is not None and v > 0),
        reverse=True,
    )
    total = sum(amounts)
    if total <= 0:
        return None
    return sum(amounts[:n]) / total * 100.0


# ─────────────────────────────────────────────────────────────────────────────
# Coverage
# ─────────────────────────────────────────────────────────────────────────────
def coverage_pct(covered: Any, total: Any) -> Optional[float]:
    """
    Share of rows carrying a usable value, as a percentage.

    Returns None rather than 0 when the denominator is empty: "we have no rows"
    and "none of our rows have costs" are different statements, and the Regions
    page rendered the first as `-` while the Overview rendered the second as
    `100%` on the same filter.
    """
    denominator = _to_float(total)
    numerator = _to_float(covered)
    if denominator is None or numerator is None or denominator <= 0:
        return None
    return min(100.0, numerator / denominator * 100.0)


# ─────────────────────────────────────────────────────────────────────────────
# Coercion
# ─────────────────────────────────────────────────────────────────────────────
def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _to_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    try:
        import pandas as pd

        stamp = pd.to_datetime(value, errors="coerce")
        if stamp is None or stamp is pd.NaT:
            return None
        return stamp.date()
    except Exception:
        return None
