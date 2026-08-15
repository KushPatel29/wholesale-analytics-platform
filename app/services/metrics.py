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
from datetime import date, datetime
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


# Commercial, retail and workforce metrics
#
# These functions intentionally return ``None`` when a required source value is
# absent or a denominator is unusable.  A missing metric is not a zero metric.
def net_sales_reconciliation(
    gross_sales: Any,
    discounts: Any,
    returns: Any,
    adjustment_costs: Any,
) -> dict[str, Optional[float]]:
    """Net sales = gross sales - discounts - returns - related costs."""
    values = [_to_float(v) for v in (gross_sales, discounts, returns, adjustment_costs)]
    net_sales = None if any(v is None for v in values) else values[0] - sum(values[1:])
    return {
        "gross_sales": values[0],
        "discounts": values[1],
        "returns": values[2],
        "adjustment_costs": values[3],
        "net_sales": net_sales,
    }


def variance_pct(actual: Any, forecast: Any) -> Optional[float]:
    """((Actual - Forecast) / Actual) * 100."""
    actual_value = _to_float(actual)
    forecast_value = _to_float(forecast)
    if actual_value is None or forecast_value is None or abs(actual_value) < 1e-9:
        return None
    return (actual_value - forecast_value) / actual_value * 100.0


def forecast_accuracy_suite(
    actuals: Sequence[Any],
    forecasts: Sequence[Any],
    *,
    tolerance_pct: float = 10.0,
) -> dict[str, Any]:
    """Score paired periods without silently treating zero actuals as accurate."""
    pairs = [
        (a, f)
        for a, f in zip((_to_float(v) for v in actuals), (_to_float(v) for v in forecasts))
        if a is not None and f is not None
    ]
    if not pairs:
        return {
            "variance_pct": None,
            "mape_pct": None,
            "bias": None,
            "hit_rate_pct": None,
            "scored_periods": 0,
            "zero_actual_periods": 0,
            "tolerance_pct": float(tolerance_pct),
        }
    nonzero = [(a, f) for a, f in pairs if abs(a) >= 1e-9]
    total_actual = sum(a for a, _ in pairs)
    total_forecast = sum(f for _, f in pairs)
    mape = (
        sum(abs(a - f) / abs(a) for a, f in nonzero) / len(nonzero) * 100.0
        if nonzero
        else None
    )
    hit_rate = (
        sum(1 for a, f in nonzero if abs(a - f) / abs(a) * 100.0 <= tolerance_pct)
        / len(nonzero)
        * 100.0
        if nonzero
        else None
    )
    return {
        "variance_pct": variance_pct(total_actual, total_forecast),
        "mape_pct": mape,
        "bias": sum(f - a for a, f in pairs) / len(pairs),
        "hit_rate_pct": hit_rate,
        "scored_periods": len(nonzero),
        "zero_actual_periods": len(pairs) - len(nonzero),
        "tolerance_pct": float(tolerance_pct),
    }


def retention_rate(beginning_customers: Any, ending_customers: Any, new_customers: Any) -> Optional[float]:
    beginning = _to_float(beginning_customers)
    ending = _to_float(ending_customers)
    added = _to_float(new_customers)
    if beginning is None or ending is None or added is None or beginning <= 0:
        return None
    return (ending - added) / beginning * 100.0


def churn_rate(lost_customers: Any, starting_customers: Any) -> Optional[float]:
    return _ratio_pct(lost_customers, starting_customers)


def revenue_churn_rate(churned_revenue: Any, starting_revenue: Any) -> Optional[float]:
    return _ratio_pct(churned_revenue, starting_revenue)


def customer_lifetime_value(
    average_transaction_value: Any,
    average_transactions_per_year: Any,
    average_retention_years: Any,
    profit_margin: Any,
) -> Optional[float]:
    values = [
        _to_float(average_transaction_value),
        _to_float(average_transactions_per_year),
        _to_float(average_retention_years),
        _to_float(profit_margin),
    ]
    if any(value is None for value in values):
        return None
    margin_factor = values[3] / 100.0 if abs(values[3]) > 1.0 else values[3]
    return values[0] * values[1] * values[2] * margin_factor


def net_revenue_retention(
    starting_revenue: Any,
    expansion: Any,
    contraction: Any,
    churned_revenue: Any,
) -> Optional[float]:
    start = _to_float(starting_revenue)
    movements = [_to_float(v) for v in (expansion, contraction, churned_revenue)]
    if start is None or start <= 0 or any(v is None for v in movements):
        return None
    return (start + movements[0] - movements[1] - movements[2]) / start * 100.0


def revenue_movement(
    starting_accounts: Mapping[Any, Any], ending_accounts: Mapping[Any, Any]
) -> dict[str, Any]:
    """Decompose account revenue movement and derive logo/revenue rates."""
    start = {key: _to_float(value) or 0.0 for key, value in (starting_accounts or {}).items()}
    end = {key: _to_float(value) or 0.0 for key, value in (ending_accounts or {}).items()}
    start_keys = {key for key, value in start.items() if value > 0}
    end_keys = {key for key, value in end.items() if value > 0}
    retained = start_keys & end_keys
    lost = start_keys - end_keys
    added = end_keys - start_keys
    expansion = sum(max(end[key] - start[key], 0.0) for key in retained)
    contraction = sum(max(start[key] - end[key], 0.0) for key in retained)
    churned_revenue = sum(start[key] for key in lost)
    starting_revenue = sum(start[key] for key in start_keys)
    ending_revenue = sum(end[key] for key in end_keys)
    return {
        "starting_customers": len(start_keys),
        "ending_customers": len(end_keys),
        "new_customers": len(added),
        "retained_customers": len(retained),
        "lost_customers": len(lost),
        "retention_rate_pct": retention_rate(len(start_keys), len(end_keys), len(added)),
        "logo_churn_rate_pct": churn_rate(len(lost), len(start_keys)),
        "revenue_churn_rate_pct": _ratio_pct(churned_revenue, starting_revenue),
        "starting_revenue": starting_revenue,
        "new_revenue": sum(end[key] for key in added),
        "expansion": expansion,
        "contraction": contraction,
        "churned_revenue": churned_revenue,
        "ending_revenue": ending_revenue,
        "nrr_pct": net_revenue_retention(starting_revenue, expansion, contraction, churned_revenue),
        "arpa": average_revenue_per_account(ending_revenue, len(end_keys)),
        "new_arpa": average_revenue_per_account(sum(end[key] for key in added), len(added)),
        "established_arpa": average_revenue_per_account(
            sum(end[key] for key in retained), len(retained)
        ),
    }


def average_revenue_per_account(revenue: Any, accounts: Any) -> Optional[float]:
    return _ratio(revenue, accounts)


def revenue_per_employee(revenue: Any, employees: Any) -> Optional[float]:
    return _ratio(revenue, employees)


def revenue_per_paid_hour(revenue: Any, paid_hours: Any) -> Optional[float]:
    return _ratio(revenue, paid_hours)


def employee_turnover_rate(separations: Any, average_employees: Any) -> Optional[float]:
    return _ratio_pct(separations, average_employees)


def gmroi(gross_margin_dollars: Any, average_inventory_cost: Any) -> Optional[float]:
    return _ratio(gross_margin_dollars, average_inventory_cost)


def sell_through_rate(units_sold: Any, units_available: Any) -> Optional[float]:
    return _ratio_pct(units_sold, units_available)


def shrink_rate(book_inventory: Any, physical_inventory: Any) -> Optional[float]:
    book = _to_float(book_inventory)
    physical = _to_float(physical_inventory)
    if book is None or physical is None or book <= 0:
        return None
    return (book - physical) / book * 100.0


def return_rate(returns_count: Any, orders_count: Any) -> Optional[float]:
    return _ratio_pct(returns_count, orders_count)


def return_cost_rate(credit_value: Any, net_sales: Any) -> Optional[float]:
    return _ratio_pct(credit_value, net_sales)


def average_resolution_hours(opened_closed_pairs: Iterable[Sequence[Any]]) -> Optional[float]:
    durations: list[float] = []
    for pair in opened_closed_pairs or ():
        if len(pair) < 2:
            continue
        opened = _to_datetime(pair[0])
        closed = _to_datetime(pair[1])
        if opened is not None and closed is not None and closed >= opened:
            durations.append((closed - opened).total_seconds() / 3600.0)
    return sum(durations) / len(durations) if durations else None


def quota_attainment(sales: Any, quota: Any) -> Optional[float]:
    return _ratio_pct(sales, quota)


# ─────────────────────────────────────────────────────────────────────────────
# Financial statements
# ─────────────────────────────────────────────────────────────────────────────
# Everything above this line stops at the gross-margin line, which is as far as
# the operational fact table reaches. These read the seeded financial layer -
# entity-level, monthly, and deliberately not sliceable by region or protein,
# because a balance sheet has no such dimension.
def period_average(opening: Any, closing: Any) -> Optional[float]:
    """
    (Opening + Closing) / 2 - the denominator every turnover ratio wants.

    A turnover computed against the closing balance alone flatters a company
    that drew its receivables down on the last day of the period, which is
    exactly when a month-end balance is least representative.
    """
    start = _to_float(opening)
    end = _to_float(closing)
    if start is None and end is None:
        return None
    if start is None:
        return end
    if end is None:
        return start
    return (start + end) / 2.0


def share_pct(part: Any, whole: Any) -> Optional[float]:
    """
    `part` as a percentage of `whole` - the common-size column on a statement.

    A presentation helper rather than a metric in its own right, which is why it
    carries no catalogue entry: the catalogued metric is whichever line is being
    expressed, not the act of dividing it by revenue.
    """
    return _ratio_pct(part, whole)


def average_balance(balances: Iterable[Any]) -> Optional[float]:
    """
    Mean of a period's month-end balances.

    `period_average` needs an opening balance, and the first fiscal year in any
    dataset does not have one - it would silently fall back to the closing
    balance alone, which is why FY2024's receivable turnover read a full point
    higher than FY2025's on a book that had not changed. Averaging the months
    the period actually contains is the same idea, uniformly available.
    """
    values = [v for v in (_to_float(b) for b in balances or ()) if v is not None]
    if not values:
        return None
    return sum(values) / len(values)


def net_income(
    revenue: Any,
    cogs: Any,
    operating_expenses: Any,
    other_expenses: Any,
    interest: Any,
    taxes: Any,
    depreciation_amortization: Any,
) -> Optional[float]:
    """
    Revenue - COGS - OpEx - Other - Interest - Taxes - D&A.

    Every argument after `revenue` is a cost stated positive, so a caller that
    holds "other income" passes it negative. Returning None when any line is
    missing is deliberate: a P&L with a hole in it is not a smaller profit, it
    is an unknown one.
    """
    values = [
        _to_float(v)
        for v in (
            revenue,
            cogs,
            operating_expenses,
            other_expenses,
            interest,
            taxes,
            depreciation_amortization,
        )
    ]
    if any(value is None for value in values):
        return None
    return values[0] - sum(values[1:])


def net_profit_margin(net_income_value: Any, revenue: Any) -> Optional[float]:
    """Net income / Revenue * 100. Negative net income is a real answer."""
    return _ratio_pct(net_income_value, revenue)


def current_ratio(current_assets: Any, current_liabilities: Any) -> Optional[float]:
    """Current assets / Current liabilities, as a multiple."""
    return _ratio(current_assets, current_liabilities)


def working_capital(current_assets: Any, current_liabilities: Any) -> Optional[float]:
    """Current assets - Current liabilities, in dollars."""
    assets = _to_float(current_assets)
    liabilities = _to_float(current_liabilities)
    if assets is None or liabilities is None:
        return None
    return assets - liabilities


def accounts_receivable_turnover(
    net_credit_sales: Any, average_accounts_receivable: Any
) -> Optional[float]:
    """
    Net credit sales / Average AR, as a multiple.

    Every sale in this book is invoiced to a trade account, so net credit sales
    and net sales are the same figure here - stated rather than assumed, because
    the two diverge the moment a cash channel exists.
    """
    return _ratio(net_credit_sales, average_accounts_receivable)


def accounts_payable_overdue_pct(overdue_payables: Any, total_payables: Any) -> Optional[float]:
    """AP overdue / Total AP * 100."""
    return _ratio_pct(overdue_payables, total_payables)


def return_on_assets(net_income_value: Any, total_assets: Any) -> Optional[float]:
    """Net income / Total assets * 100."""
    return _ratio_pct(net_income_value, total_assets)


def inventory_turnover(cogs: Any, average_inventory: Any) -> Optional[float]:
    """
    COGS / Average inventory, as a multiple - "turns".

    The brief writes this as `(COGS / Average inventory) * 100`, which is the
    one formula in it that cannot be taken verbatim: turnover is a multiple, not
    a proportion, and the extra factor renders a healthy 8.2 turns as 820. The
    multiple is what every reference and every reviewer means by the word.

    This is also not the Inventory page's `turns`, which is `52 / weeks on hand`
    - a usage-based measure off on-hand quantity rather than a cost-based one off
    the balance sheet. Both are legitimate and they do not agree, so they carry
    separate catalogue keys and each states its basis on screen.
    """
    return _ratio(cogs, average_inventory)


# ─────────────────────────────────────────────────────────────────────────────
# Acquisition
# ─────────────────────────────────────────────────────────────────────────────
# These need a source system the sales fact does not contain - campaigns and
# spend - which is why they sat in the catalogue as "not implemented" until the
# marketing layer existed. They are still the softest numbers on the site, and
# the pages that carry them say so.
def customer_acquisition_cost(marketing_and_sales_spend: Any, new_customers: Any) -> Optional[float]:
    """
    Total marketing and sales spend / New customers.

    "Sales spend" is the acquisition share of selling compensation, not the
    whole selling line: charging a distributor's entire field organisation to
    the ~64 accounts it wins in a year would say nothing about acquisition and
    everything about servicing the ~470 accounts it already has. The share is a
    stated seed constant, shown on the page.
    """
    return _ratio(marketing_and_sales_spend, new_customers)


def cost_per_lead(marketing_spend: Any, new_leads: Any) -> Optional[float]:
    """Total marketing spend / New leads."""
    return _ratio(marketing_spend, new_leads)


def return_on_marketing_investment(
    sales_growth: Any, marketing_cost: Any, marketing_investment: Any
) -> Optional[float]:
    """
    (Sales growth - Marketing cost) / Marketing investment * 100.

    Implemented exactly as the brief writes it, including the assumption buried
    in it: every dollar of period-over-period sales movement is credited to
    marketing. That makes the measure violently negative in any year revenue
    fell for reasons campaigns do not drive - which is the case here - so the
    page states the limitation beside the number rather than quietly swapping in
    a friendlier denominator.
    """
    growth = _to_float(sales_growth)
    cost = _to_float(marketing_cost)
    investment = _to_float(marketing_investment)
    if growth is None or cost is None or investment is None or investment <= 0:
        return None
    return (growth - cost) / investment * 100.0


def clv_to_cac_ratio(customer_lifetime_value_amount: Any, acquisition_cost: Any) -> Optional[float]:
    """
    CLV / CAC, as a multiple.

    The conventional 3:1 benchmark is quoted against a *lifetime* value. This
    site's CLV is a 12-month, gross-profit, discounted figure, so the ratio here
    is a stricter first-year measure and is labelled as one. Comparing it to 3:1
    without that basis would be comparing two different quantities.
    """
    return _ratio(customer_lifetime_value_amount, acquisition_cost)


def cac_payback_months(acquisition_cost: Any, annual_customer_value: Any) -> Optional[float]:
    """
    Months of customer value needed to repay acquisition cost.

    The readable companion to `clv_to_cac_ratio`. A first-year ratio of 1.3x
    invites a reviewer to read "barely above water" when it means "repaid in
    nine months", and the second statement is the one a commercial reader
    actually wants.
    """
    monthly = _ratio(annual_customer_value, 12)
    return _ratio(acquisition_cost, monthly)


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    name: str
    formula: str
    grain: str
    source_table: str
    owner_page: str
    basis: str
    status: str = "Implemented"

    def as_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "name": self.name,
            "formula": self.formula,
            "grain": self.grain,
            "source_table": self.source_table,
            "owner_page": self.owner_page,
            "basis": self.basis,
            "status": self.status,
        }


def _definition(key: str, name: str, formula: str, grain: str, source: str, owner: str, basis: str) -> MetricDefinition:
    return MetricDefinition(key, name, formula, grain, source, owner, basis)


METRIC_CATALOGUE: tuple[MetricDefinition, ...] = (
    _definition("net_sales", "Net sales", "Gross sales - Discounts - Returns - Costs associated with discounts and returns", "Selected period", "sales_fact + return_rma", "Overview", "Net"),
    _definition("forecast_variance", "Forecast variance %", "((Actual - Forecast) / Actual) * 100", "Period / SKU / region / horizon", "sales_fact (rolling-origin backtest)", "Planner", "Transaction sales"),
    _definition("forecast_mape", "Forecast MAPE", "mean(|Actual - Forecast| / Actual) * 100", "Period / SKU / region / horizon", "sales_fact (rolling-origin backtest)", "Planner", "Transaction sales"),
    _definition("forecast_bias", "Forecast bias", "mean(Forecast - Actual)", "Period / SKU / region / horizon", "sales_fact (rolling-origin backtest)", "Planner", "Transaction sales"),
    _definition("forecast_hit_rate", "Forecast hit rate", "% of periods within +/-X% tolerance", "Period / SKU / region / horizon", "sales_fact (rolling-origin backtest)", "Planner", "Transaction sales"),
    _definition("retention", "Customer retention rate", "(Customers at end of period - Customers added during period) / Customers at beginning", "Customer cohort and selected period", "sales_fact", "Customers", "Logo"),
    _definition("churn", "Customer churn rate", "Customers lost during period / Customers at start of period * 100", "Customer cohort and selected period", "sales_fact", "Customers", "Logo and revenue-weighted"),
    _definition("clv", "Customer lifetime value", "(Avg transaction value * Avg transactions per year * Avg retention in years) * Profit margin", "Customer / segment", "sales_fact", "Customers", "Gross-profit default; finite-horizon retention proxy and discount factor are disclosed"),
    _definition("nrr", "Net revenue retention", "(Starting revenue + Expansion - Contraction - Churn) / Starting revenue * 100", "Account and selected period", "sales_fact", "Overview / Customers", "Transaction sales"),
    _definition("arpa", "ARPA", "Total revenue in period / Number of accounts in period", "Selected period / account cohort", "sales_fact", "Customers", "Transaction sales"),
    _definition("revenue_per_employee", "Revenue per employee", "Total revenue / Number of employees", "Selected period", "sales_fact + labor_fact", "Labor", "Transaction sales"),
    _definition("revenue_per_paid_hour", "Revenue per paid hour", "Total revenue / Paid hours", "Selected period", "sales_fact + labor_fact", "Labor", "Transaction sales"),
    _definition("turnover", "Employee turnover rate", "Separations in period / Average employees in period * 100", "Selected period / separation type", "labor_fact", "Labor", "Headcount"),
    _definition("gmroi", "GMROI", "Gross margin $ / Average inventory cost", "SKU / selected period", "sales_fact", "Inventory", "Gross margin"),
    _definition("sell_through", "Sell-through %", "Units sold / Units available * 100", "SKU / selected period", "sales_fact", "Inventory", "Units"),
    _definition("shrink", "Shrink %", "(Book inventory - Physical inventory) / Book inventory * 100", "SKU / physical count", "sales_fact inventory snapshot", "Inventory", "Units"),
    _definition("return_rate", "Return rate", "Returns / Orders * 100", "Selected period", "return_rma + sales_fact", "Returns", "Orders"),
    _definition("return_cost", "Return cost rate", "Credit value / Net sales * 100", "Selected period", "return_rma + sales_fact", "Returns", "Net"),
    _definition("resolution", "Average return resolution", "mean(closed_at - opened_at) across returns", "Resolved return / selected period", "return_rma", "Returns", "Hours"),
    _definition("quota", "Quota attainment", "Sales achieved by rep or region / Goal for that rep or region", "Rep / region / month", "sales_fact quota", "Sales Reps", "Transaction sales"),
    _definition("gross_profit_margin", "Gross profit margin", "(Revenue - COGS) / Revenue * 100", "Any grain with revenue and cost", "sales_fact", "Overview / Finance", "Invoiced sales, revenue-weighted"),
    _definition("net_income", "Net income", "Revenue - COGS - OpEx - Other - Interest - Taxes - D&A", "Company / month / fiscal year", "finance_month + sales_fact", "Finance", "Invoiced sales (gross less discounts), matching every other page; entity-level, not sliceable by operational filters"),
    _definition("net_profit_margin", "Net profit margin", "Net income / Revenue * 100", "Company / month / fiscal year", "finance_month + sales_fact", "Finance", "Invoiced sales; the net sales bridge is shown separately on the page"),
    _definition("current_ratio", "Current ratio", "Current assets / Current liabilities", "Company / month end", "finance_month", "Finance", "Month-end balance, multiple not percent"),
    _definition("working_capital", "Working capital", "Current assets - Current liabilities", "Company / month end", "finance_month", "Finance", "Month-end balance"),
    _definition("ar_turnover", "AR turnover", "Net credit sales / Average AR", "Company / month / fiscal year", "finance_month", "Finance", "All sales are credit sales in this book; average AR is the mean of the period's month-end balances, and a short year is annualised and labelled"),
    _definition("ap_overdue", "AP overdue %", "AP overdue / Total AP * 100", "Company / month end", "finance_month", "Finance", "Month-end balance, past contractual due date"),
    _definition("roa", "Return on assets", "Net income / Total assets * 100", "Company / month / fiscal year", "finance_month", "Finance", "Net income over closing total assets"),
    _definition("inventory_turnover_cost", "Inventory turnover (cost basis)", "COGS / Average inventory", "Company / month / fiscal year", "finance_month", "Finance", "Cost basis, multiple; the brief's x100 is dropped because turnover is a multiple, not a percentage"),
    _definition("inventory_turns_usage", "Inventory turns (usage basis)", "52 / Weeks on hand", "SKU / selected period", "sales_fact inventory snapshot", "Inventory", "Usage basis off on-hand quantity; deliberately distinct from the cost-basis turnover on Finance"),
    _definition("cac", "Customer acquisition cost", "Total marketing and sales spend / New customers", "Company / month / fiscal year / channel", "marketing_campaign + finance_month + sales_fact", "Marketing", "Marketing spend plus an 18% acquisition share of selling compensation; new customers counted by first order in the sales fact"),
    _definition("cpl", "Cost per lead", "Total marketing spend / New leads", "Company / month / fiscal year / channel", "marketing_campaign", "Marketing", "Marketing spend only; leads are modelled from channel cost-per-lead"),
    _definition("romi", "ROMI", "(Sales growth - Marketing cost) / Marketing investment * 100", "Company / fiscal year", "marketing_campaign + finance_month", "Marketing", "Attributes all company sales movement to marketing, as the formula is written; reads deeply negative in a year revenue fell for unrelated reasons"),
    _definition("clv_cac", "CLV:CAC ratio", "CLV / CAC", "Company / fiscal year", "marketing_campaign + sales_fact", "Marketing / Overview", "12-month discounted gross-profit CLV over CAC; stricter than the lifetime basis the 3:1 benchmark assumes"),
    _definition("cac_payback", "CAC payback", "CAC / (Annual customer value / 12)", "Company / fiscal year", "marketing_campaign + sales_fact", "Marketing", "Months of 12-month gross-profit CLV needed to repay acquisition cost"),
    MetricDefinition("lead_response_time", "Lead response time", "mean(first touch - lead created)", "Lead", "Requires CRM touch timestamps", "Metric catalogue", "Not applicable", "Not implemented - the campaign layer carries spend and lead counts, not per-lead touch timestamps"),
    MetricDefinition("mrr", "MRR / ARR", "Subscription recurring revenue", "Month", "Requires subscription billing", "Metric catalogue", "Not applicable", "Not implemented - domain mismatch"),
    MetricDefinition("survey_scores", "NPS / eNPS / CSAT", "Requires survey responses", "Survey response", "Requires survey platform", "Metric catalogue", "Not applicable", "Not implemented - requires survey system"),
    MetricDefinition("web_funnel", "Web funnel metrics", "Traffic and conversion measures", "Web session", "Requires web analytics", "Metric catalogue", "Not applicable", "Not implemented - requires web analytics"),
    MetricDefinition("hris_metrics", "Training / hiring / diversity metrics", "Requires employee lifecycle records", "Employee", "Requires HRIS", "Metric catalogue", "Not applicable", "Not implemented - requires HRIS"),
    MetricDefinition("scrap", "Manufacturing scrap", "Scrapped production / production", "Production run", "Requires manufacturing system", "Metric catalogue", "Not applicable", "Not implemented - use retail shrink"),
    MetricDefinition("market_metrics", "Market share / industry growth", "Company performance / external market", "Market / period", "Requires external market data", "Metric catalogue", "Not applicable", "Not implemented - requires external data"),
)


def metric_catalogue() -> list[dict[str, str]]:
    return [definition.as_dict() for definition in METRIC_CATALOGUE]


def _ratio(numerator: Any, denominator: Any) -> Optional[float]:
    top = _to_float(numerator)
    bottom = _to_float(denominator)
    if top is None or bottom is None or bottom <= 0:
        return None
    return top / bottom


def _ratio_pct(numerator: Any, denominator: Any) -> Optional[float]:
    ratio = _ratio(numerator, denominator)
    return None if ratio is None else ratio * 100.0


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


def _to_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        import pandas as pd

        stamp = pd.to_datetime(value, errors="coerce")
        if stamp is None or stamp is pd.NaT:
            return None
        return stamp.to_pydatetime()
    except Exception:
        return None
