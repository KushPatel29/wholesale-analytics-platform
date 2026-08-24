"""
The Finance page payload.

Two things this module refuses to do, both of them deliberate:

**It does not recompute a metric.** Every number below comes out of
`app.services.metrics`. Three disagreeing HHIs is what page-local arithmetic
already cost this codebase, and a financial statement is the last place to
repeat it.

**It does not accept operational filters.** A balance sheet has no region or
department dimension. Offering a filter that silently returned a
region's share of total assets would be worse than offering none, so the page
states that it is entity-level and the payload carries no filter parameters at
all.

Fiscal years run October to September, matching the rest of the app. The most
recent one in the dataset is short - the sales fact stops mid-year - so its
flow-based ratios are annualised and labelled, rather than presented as if nine
months were twelve.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from app.services import metrics


logger = logging.getLogger(__name__)

DATASET_DIR = Path("cache") / "finance" / "fact_dataset"
MANIFEST_NAME = "_manifest.json"
FISCAL_YEAR_START_MONTH = 10

_cache_lock = threading.RLock()
_cache: dict[str, Any] = {"mtime": None, "frame": None, "manifest": None}

OPEX_LABELS: tuple[tuple[str, str], ...] = (
    ("opex_selling", "Selling & sales compensation"),
    ("opex_warehouse", "Warehouse & distribution"),
    ("opex_occupancy", "Occupancy"),
    ("opex_admin", "Administration & general"),
    ("opex_technology", "Technology"),
    ("opex_marketing", "Marketing"),
)

AR_AGING_LABELS: tuple[tuple[str, str], ...] = (
    ("ar_current", "Current"),
    ("ar_d1_30", "1-30 days"),
    ("ar_d31_60", "31-60 days"),
    ("ar_d61_90", "61-90 days"),
    ("ar_d90_plus", "90+ days"),
)


class FinanceDatasetNotBuiltError(RuntimeError):
    """Raised when the seeded financial layer is missing."""


def _manifest_path() -> Path:
    return DATASET_DIR / MANIFEST_NAME


def dataset_available() -> bool:
    return DATASET_DIR.exists() and any(DATASET_DIR.glob("*.parquet"))


def _load() -> tuple[pd.DataFrame, dict[str, Any]]:
    """Read the monthly parquet, re-reading only when the manifest changes."""
    if not dataset_available():
        raise FinanceDatasetNotBuiltError(
            f"Finance dataset not found at {DATASET_DIR.as_posix()}. "
            "Run `python -m seed.generate_synthetic_finance`."
        )
    try:
        mtime = _manifest_path().stat().st_mtime
    except FileNotFoundError:
        mtime = None
    with _cache_lock:
        if _cache["frame"] is not None and _cache["mtime"] == mtime:
            return _cache["frame"], _cache["manifest"]
        frame = pd.concat(
            [pd.read_parquet(path) for path in sorted(DATASET_DIR.glob("*.parquet"))],
            ignore_index=True,
        )
        frame["month"] = pd.to_datetime(frame["month"]).dt.date
        frame = frame.sort_values("month").reset_index(drop=True)
        manifest: dict[str, Any] = {}
        if _manifest_path().exists():
            try:
                manifest = json.loads(_manifest_path().read_text(encoding="utf-8"))
            except Exception:
                logger.debug("finance_bundle.manifest_unreadable", exc_info=True)
        _cache.update({"mtime": mtime, "frame": frame, "manifest": manifest})
        return frame, manifest


def fiscal_year_bounds(year: int) -> tuple[date, date]:
    """FY2025 -> (2024-10-01, 2025-09-30)."""
    return date(year - 1, FISCAL_YEAR_START_MONTH, 1), date(year, FISCAL_YEAR_START_MONTH, 1) - timedelta(days=1)


def _f(value: Any) -> Optional[float]:
    """Coerce a pandas cell, turning NaN into None rather than a number."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def available_fiscal_years() -> list[dict[str, Any]]:
    frame, _ = _load()
    years: list[dict[str, Any]] = []
    for year in sorted({int(v) for v in frame["fiscal_year"]}):
        months = int((frame["fiscal_year"] == year).sum())
        start, end = fiscal_year_bounds(year)
        years.append(
            {
                "year": year,
                "label": f"FY{year}",
                "months": months,
                "complete": months >= 12,
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
        )
    return years


def default_fiscal_year() -> int:
    """
    The most recent *complete* fiscal year, falling back to the latest.

    Landing on a nine-month year would put a partial-year revenue figure at the
    top of the page, which reads as a collapse rather than as a year in
    progress.
    """
    years = available_fiscal_years()
    complete = [y for y in years if y["complete"]]
    return (complete or years)[-1]["year"]


def _sum(frame: pd.DataFrame, column: str) -> Optional[float]:
    if column not in frame.columns:
        return None
    total = frame[column].sum(min_count=1)
    return _f(total)


def _income_statement(frame: pd.DataFrame, reconciliation: dict[str, Any]) -> dict[str, Any]:
    revenue = _sum(frame, "revenue")
    cogs = _sum(frame, "cogs")
    operating_expenses = _sum(frame, "operating_expenses")
    other_expense = _sum(frame, "other_expense")
    interest = _sum(frame, "interest_expense")
    taxes = _sum(frame, "tax_expense")
    da = _sum(frame, "depreciation_amortization")

    gross_profit = None if revenue is None or cogs is None else revenue - cogs
    income = metrics.net_income(revenue, cogs, operating_expenses, other_expense, interest, taxes, da)
    pre_tax = _sum(frame, "pre_tax_income")

    def line(key: str, label: str, amount: Optional[float], kind: str = "cost") -> dict[str, Any]:
        return {
            "key": key,
            "label": label,
            "amount": amount,
            "pct_of_revenue": metrics.share_pct(amount, revenue),
            "kind": kind,
        }

    lines = [
        line("revenue", "Invoiced sales", revenue, "revenue"),
        line("cogs", "Cost of goods sold", cogs),
        line("gross_profit", "Gross profit", gross_profit, "subtotal"),
        line("operating_expenses", "Operating expenses", operating_expenses),
        line("depreciation_amortization", "Depreciation & amortisation", da),
        line("other_expense", "Other expense (income)", other_expense),
        line("interest_expense", "Interest expense", interest),
        line("pre_tax_income", "Pre-tax income", pre_tax, "subtotal"),
        line("tax_expense", "Income tax expense", taxes),
        line("net_income", "Net income", income, "total"),
    ]
    return {
        "lines": lines,
        "revenue": revenue,
        "cogs": cogs,
        "gross_profit": gross_profit,
        "net_income": income,
        "pre_tax_income": pre_tax,
        "operating_expenses": operating_expenses,
        "depreciation_amortization": da,
        "interest_expense": interest,
        "tax_expense": taxes,
        "other_expense": other_expense,
        "opex_breakdown": [
            {"key": key, "label": label, "amount": _sum(frame, key)}
            for key, label in OPEX_LABELS
        ],
        "gross_to_net": reconciliation,
    }


def _balance_sheet(closing: pd.Series) -> dict[str, Any]:
    def item(key: str, label: str) -> dict[str, Any]:
        return {"key": key, "label": label, "amount": _f(closing.get(key))}

    assets = [
        item("cash", "Cash"),
        item("accounts_receivable", "Accounts receivable"),
        item("inventory", "Inventory"),
        item("prepaid_expenses", "Prepaid expenses"),
        item("current_assets", "Total current assets"),
        item("net_ppe", "Property, plant & equipment, net"),
        item("intangibles", "Intangible assets"),
        item("total_assets", "Total assets"),
    ]
    liabilities = [
        item("accounts_payable", "Accounts payable"),
        item("accrued_liabilities", "Accrued liabilities"),
        item("current_portion_debt", "Current portion of debt"),
        item("current_liabilities", "Total current liabilities"),
        item("long_term_debt", "Long-term debt"),
        item("total_liabilities", "Total liabilities"),
    ]
    # Equity closes the statement. Assets and liabilities are modelled from
    # independent drivers (AR from DSO, inventory from DIO, AP from DPO), so
    # there is no retained-earnings account rolling forward behind them - the
    # residual moves by up to $2M in a month against $186k of net income.
    # Publishing it as "retained earnings" would be a fabrication; omitting it
    # leaves a page headed "Balance sheet" that visibly does not balance. So it
    # is stated as the residual it is, and the page says the statement does not
    # articulate to a roll-forward.
    total_assets = _f(closing.get("total_assets"))
    total_liabilities = _f(closing.get("total_liabilities"))
    total_equity = (
        None
        if total_assets is None or total_liabilities is None
        else total_assets - total_liabilities
    )
    equity = [
        {"key": "total_equity", "label": "Net assets (residual)", "amount": total_equity},
    ]

    return {
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "balances": (
            total_assets is not None
            and total_liabilities is not None
            and total_equity is not None
            and abs(total_assets - (total_liabilities + total_equity)) < 0.01
        ),
        "equity_note": (
            "Net assets is derived as total assets less total liabilities. Assets and liabilities are "
            "modelled from independent drivers, so this is a residual rather than a retained-earnings "
            "account that rolls forward from net income."
        ),
        "ar_aging": [
            {"key": key, "label": label, "amount": _f(closing.get(key))}
            for key, label in AR_AGING_LABELS
        ],
        "ap_overdue": _f(closing.get("ap_overdue")),
        "accounts_payable": _f(closing.get("accounts_payable")),
        "as_of": str(
            (pd.Timestamp(closing["month"]) + pd.offsets.MonthEnd(0)).date()
        ),
    }


def _ratios(frame: pd.DataFrame, closing: pd.Series, *, months: int) -> list[dict[str, Any]]:
    """
    The ratio block, every entry computed by `metrics` and carrying its formula.

    Flow measures on a short year are annualised so a nine-month turnover is not
    compared against a twelve-month one; stock measures are month-end balances
    and need no such adjustment.
    """
    annualise = 12.0 / months if 0 < months < 12 else 1.0
    revenue = _sum(frame, "revenue")
    cogs = _sum(frame, "cogs")
    income = _sum(frame, "net_income")
    revenue_annual = None if revenue is None else revenue * annualise
    cogs_annual = None if cogs is None else cogs * annualise
    income_annual = None if income is None else income * annualise

    avg_ar = metrics.average_balance(frame["accounts_receivable"].tolist())
    avg_inventory = metrics.average_balance(frame["inventory"].tolist())
    current_assets = _f(closing.get("current_assets"))
    current_liabilities = _f(closing.get("current_liabilities"))
    total_assets = _f(closing.get("total_assets"))

    annualised_note = "Annualised from {} months".format(months) if annualise != 1.0 else None

    return [
        {
            "key": "gross_profit_margin",
            "label": "Gross profit margin",
            "value": metrics.margin_pct(revenue, cogs),
            "unit": "percent",
            "formula": "(Revenue - COGS) / Revenue * 100",
            "basis": "Invoiced sales, revenue-weighted",
            "note": None,
        },
        {
            "key": "net_profit_margin",
            "label": "Net profit margin",
            "value": metrics.net_profit_margin(income, revenue),
            "unit": "percent",
            "formula": "Net income / Revenue * 100",
            "basis": "Invoiced sales",
            "note": None,
        },
        {
            "key": "current_ratio",
            "label": "Current ratio",
            "value": metrics.current_ratio(current_assets, current_liabilities),
            "unit": "multiple",
            "formula": "Current assets / Current liabilities",
            "basis": "Month-end balance",
            "note": None,
        },
        {
            "key": "working_capital",
            "label": "Working capital",
            "value": metrics.working_capital(current_assets, current_liabilities),
            "unit": "currency",
            "formula": "Current assets - Current liabilities",
            "basis": "Month-end balance",
            "note": None,
        },
        {
            "key": "ar_turnover",
            "label": "AR turnover",
            "value": metrics.accounts_receivable_turnover(revenue_annual, avg_ar),
            "unit": "multiple",
            "formula": "Net credit sales / Average AR",
            "basis": "All sales are credit sales; average of month-end balances",
            "note": annualised_note,
        },
        {
            "key": "inventory_turnover_cost",
            "label": "Inventory turnover (cost basis)",
            "value": metrics.inventory_turnover(cogs_annual, avg_inventory),
            "unit": "multiple",
            "formula": "COGS / Average inventory",
            "basis": "Cost basis - not the Inventory page's usage-based 52 / weeks on hand",
            "note": annualised_note,
        },
        {
            "key": "ap_overdue",
            "label": "AP overdue",
            "value": metrics.accounts_payable_overdue_pct(
                _f(closing.get("ap_overdue")), _f(closing.get("accounts_payable"))
            ),
            "unit": "percent",
            "formula": "AP overdue / Total AP * 100",
            "basis": "Month-end balance, past contractual due date",
            "note": None,
        },
        {
            "key": "roa",
            "label": "Return on assets",
            "value": metrics.return_on_assets(income_annual, total_assets),
            "unit": "percent",
            "formula": "Net income / Total assets * 100",
            "basis": "Closing total assets",
            "note": annualised_note,
        },
    ]


def _charts(
    frame: pd.DataFrame,
    full_frame: pd.DataFrame,
    statement: dict[str, Any],
    balance_sheet: dict[str, Any],
) -> dict[str, Any]:
    """
    `frame` is the selected fiscal year; `full_frame` is every month there is.

    Liquidity is drawn over the whole dataset on purpose - a balance-sheet trend
    that restarts each October tells you nothing about direction, and the card
    says "across the whole dataset", so it had better be.
    """
    months = [str(m) for m in frame["month"]]
    labels = [str(v) for v in frame["month_label"]]

    def deduction(key: str) -> Optional[float]:
        """A cost drawn on a bridge points downwards; None stays None."""
        value = statement.get(key)
        return None if value is None else -value

    return {
        "profit_bridge": {
            "labels": [
                "Invoiced sales",
                "COGS",
                "Operating expenses",
                "D&A",
                "Other",
                "Interest",
                "Tax",
                "Net income",
            ],
            "values": [
                statement["revenue"],
                deduction("cogs"),
                deduction("operating_expenses"),
                deduction("depreciation_amortization"),
                deduction("other_expense"),
                deduction("interest_expense"),
                deduction("tax_expense"),
                statement["net_income"],
            ],
        },
        "monthly_result": {
            "months": months,
            "labels": labels,
            "revenue": [_f(v) for v in frame["revenue"]],
            "net_income": [_f(v) for v in frame["net_income"]],
            "net_margin": [
                metrics.net_profit_margin(_f(ni), _f(rev))
                for ni, rev in zip(frame["net_income"], frame["revenue"])
            ],
        },
        "opex_composition": {
            "labels": [entry["label"] for entry in statement["opex_breakdown"]],
            "values": [entry["amount"] for entry in statement["opex_breakdown"]],
        },
        "ar_aging": {
            "labels": [entry["label"] for entry in balance_sheet["ar_aging"]],
            "values": [entry["amount"] for entry in balance_sheet["ar_aging"]],
        },
        "liquidity_trend": {
            "months": [str(m) for m in full_frame["month"]],
            "labels": [str(v) for v in full_frame["month_label"]],
            "working_capital": [
                metrics.working_capital(_f(ca), _f(cl))
                for ca, cl in zip(full_frame["current_assets"], full_frame["current_liabilities"])
            ],
            "current_ratio": [
                metrics.current_ratio(_f(ca), _f(cl))
                for ca, cl in zip(full_frame["current_assets"], full_frame["current_liabilities"])
            ],
        },
    }


def _comparatives(frame: pd.DataFrame, years: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Every fiscal year side by side, which is how a statement is read anyway.

    This also replaces the fiscal-year dropdown the page would otherwise have
    grown. The static build replays captured API payloads by path and ignores
    the query string, so a selector that re-fetched per year would render, be
    clicked, and do nothing - the exact dead control the Planning page was
    rebuilt to remove.
    """
    summaries: list[dict[str, Any]] = []
    for entry in years:
        scoped = frame[frame["fiscal_year"] == entry["year"]].sort_values("month").reset_index(drop=True)
        if scoped.empty:
            continue
        closing = scoped.iloc[-1]
        revenue = _sum(scoped, "revenue")
        cogs = _sum(scoped, "cogs")
        summaries.append(
            {
                "label": entry["label"],
                "months": entry["months"],
                "complete": entry["complete"],
                "revenue": revenue,
                "cogs": cogs,
                "gross_profit": None if revenue is None or cogs is None else revenue - cogs,
                "operating_expenses": _sum(scoped, "operating_expenses"),
                # Every line the statement renders, not just the headline five:
                # a comparative column that carried only some of them printed a
                # dash against D&A, interest and tax, which reads as missing
                # data rather than as a column that was never asked for them.
                "depreciation_amortization": _sum(scoped, "depreciation_amortization"),
                "other_expense": _sum(scoped, "other_expense"),
                "interest_expense": _sum(scoped, "interest_expense"),
                "pre_tax_income": _sum(scoped, "pre_tax_income"),
                "tax_expense": _sum(scoped, "tax_expense"),
                "net_income": _sum(scoped, "net_income"),
                "ratios": {
                    ratio["key"]: ratio["value"]
                    for ratio in _ratios(scoped, closing, months=int(entry["months"]))
                },
            }
        )
    return summaries


def _gross_to_net(frame: pd.DataFrame, start: date, end: date) -> dict[str, Any]:
    """
    The A1 reconciliation for the fiscal year, on the shared definition.

    Return credits come from the returns subsystem through the same helper the
    Overview uses. When that subsystem is unavailable the returns line is None
    and `net_sales` is None with it - the page then says the bridge is
    incomplete rather than printing a net sales figure that is really a gross
    one.
    """
    gross_sales = _sum(frame, "gross_sales")
    discounts = _sum(frame, "discounts")
    discount_handling_cost = _sum(frame, "discount_handling_cost")
    returns_value: Optional[float] = None
    return_handling_cost: Optional[float] = None
    return_count: Optional[int] = None
    available = False
    try:
        from app.services.overview_v2 import entity_return_adjustments

        adjustments = entity_return_adjustments(start=start, end_exclusive=end + timedelta(days=1))
        available = bool(adjustments.get("available"))
        returns_value = adjustments.get("returns")
        return_handling_cost = adjustments.get("return_handling_cost")
        return_count = adjustments.get("return_count")
    except Exception:
        logger.debug("finance_bundle.returns_unavailable", exc_info=True)

    adjustment_costs = (
        discount_handling_cost + return_handling_cost
        if discount_handling_cost is not None and return_handling_cost is not None
        else None
    )
    reconciliation = metrics.net_sales_reconciliation(
        gross_sales, discounts, returns_value, adjustment_costs
    )
    reconciliation.update(
        {
            "invoice_sales": _sum(frame, "revenue"),
            "discount_handling_cost": discount_handling_cost,
            "return_handling_cost": return_handling_cost,
            "return_count": return_count,
            "returns_available": available,
            "basis_note": (
                "The statement above is stated on invoiced sales - gross sales after discounts and "
                "before approved return credits - which is the basis every other page uses. Net sales "
                "nets the return credits as well and is shown here for the bridge."
            ),
        }
    )
    return reconciliation


def build_finance_bundle(fiscal_year: Optional[int] = None) -> dict[str, Any]:
    """Entity-level financial statements for one fiscal year."""
    frame, manifest = _load()
    years = available_fiscal_years()
    known = {entry["year"] for entry in years}
    year = int(fiscal_year) if fiscal_year is not None and int(fiscal_year) in known else default_fiscal_year()
    selected = next(entry for entry in years if entry["year"] == year)

    scoped = frame[frame["fiscal_year"] == year].sort_values("month").reset_index(drop=True)
    if scoped.empty:
        raise FinanceDatasetNotBuiltError(f"No finance rows for FY{year}")

    closing = scoped.iloc[-1]
    # Bound the returns window by the months the statement actually contains,
    # not by the fiscal year. They differ whenever the tail month was dropped as
    # incomplete: FY2026 covers Oct-Jun, but fiscal-year bounds reach to Sep 30
    # and pulled in a July credit, so the bridge deducted returns from a month
    # whose sales were not on the page - while the page's own provenance line
    # claimed that month was excluded from every figure on it.
    start = min(scoped["month"])
    end = (pd.Timestamp(max(scoped["month"])) + pd.offsets.MonthEnd(0)).date()
    reconciliation = _gross_to_net(scoped, start, end)
    statement = _income_statement(scoped, reconciliation)
    balance_sheet = _balance_sheet(closing)
    ratios = _ratios(scoped, closing, months=int(selected["months"]))

    return {
        "available": True,
        "generated_from": manifest.get("source"),
        "dataset_version": manifest.get("dataset_version"),
        "fiscal_years": years,
        "selected": selected,
        "basis": {
            "grain": "Company / month",
            "scope": (
                "Entity-level statements. Operational filters do not apply - a balance sheet has no "
                "region or department dimension."
            ),
            "revenue_source": (
                "Invoiced sales, gross sales, discounts and COGS are read from the sales fact, so the "
                "top line matches the Overview."
            ),
            "modelled": (
                "Operating expenses, D&A, interest, tax and the balance sheet are a seeded financial "
                "layer. Every other figure on this page derives from them."
            ),
            "incomplete_months_excluded": manifest.get("dropped_incomplete_months") or [],
            "effective_tax_rate": manifest.get("effective_tax_rate"),
        },
        "income_statement": statement,
        "balance_sheet": balance_sheet,
        "ratios": ratios,
        "comparatives": _comparatives(frame, years),
        "charts": _charts(scoped, frame, statement, balance_sheet),
        "monthly": [
            {
                "month": str(row["month"]),
                "label": str(row["month_label"]),
                "revenue": _f(row["revenue"]),
                "gross_profit": _f(row["gross_profit"]),
                "operating_expenses": _f(row["operating_expenses"]),
                "net_income": _f(row["net_income"]),
                "net_margin": metrics.net_profit_margin(_f(row["net_income"]), _f(row["revenue"])),
                "current_ratio": metrics.current_ratio(
                    _f(row["current_assets"]), _f(row["current_liabilities"])
                ),
                "working_capital": metrics.working_capital(
                    _f(row["current_assets"]), _f(row["current_liabilities"])
                ),
            }
            for _, row in scoped.iterrows()
        ],
    }
