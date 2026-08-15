"""
Generate the monthly financial layer for the portfolio demo.

Every metric above the gross-margin line already exists in the sales fact. This
seed supplies only what sits *below* it - operating expenses, D&A, interest, tax
- plus a month-end balance sheet, which is the part a distributor has and this
demo did not.

Two rules shape the whole file:

**Revenue and COGS are read, never invented.** The seed queries the same
pack-based revenue and cost expressions the DuckDB `fact` view uses, so the
Finance page's top line is the Overview's top line. A financial statement that
disagrees with the operational pages it sits beside is worse than no financial
statement, and `test_finance_seed.py` asserts the tie-out rather than trusting
it.

**Statements are entity-level.** There is no region column and no protein
column here, because a balance sheet does not have one. The Finance page says so
on screen rather than offering filters that would quietly produce nonsense.

Modelled lines are deterministic: each month draws from `default_rng(seed +
month_index)`, so a month's figures depend only on its own position and the
dataset regenerates identically.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd


FISCAL_YEAR_START_MONTH = 10

# Billing unit 3 bills by weight; everything else bills by item count. This
# mirrors `seed.catalog.BILL_BY_WEIGHT` and the `fact` view's revenue
# expression - if that ever changes, the tie-out test fails loudly.
BILL_BY_WEIGHT = 3

# Operating expense structure, as a share of invoiced sales. A wholesale
# distributor's cost base is dominated by people and freight; the split below is
# the shape of a mid-size distributor's P&L rather than an even spread.
OPEX_STRUCTURE: tuple[tuple[str, str, float, float], ...] = (
    # key, label, share of revenue, month-to-month volatility
    ("selling", "Selling & sales compensation", 0.0620, 0.055),
    ("warehouse", "Warehouse & distribution", 0.0480, 0.070),
    ("occupancy", "Occupancy", 0.0160, 0.020),
    ("admin", "Administration & general", 0.0240, 0.035),
    ("technology", "Technology", 0.0090, 0.040),
    ("marketing", "Marketing", 0.0070, 0.090),
)

DA_SHARE = 0.0110          # depreciation and amortisation, share of revenue
INTEREST_SHARE = 0.0055    # interest on the revolver and term debt
OTHER_SHARE = 0.0008       # net other expense, occasionally a credit
EFFECTIVE_TAX_RATE = 0.265  # BC + federal combined corporate rate

DSO_DAYS = 38.0            # days sales outstanding
DIO_DAYS = 46.0            # days inventory on hand
DPO_DAYS = 32.0            # days payable outstanding

# AR aging shares, oldest last. A distributor collects most of its book on time;
# the tail is what makes the aging chart worth plotting.
AR_AGING_SHARES: tuple[tuple[str, float], ...] = (
    ("current", 0.712),
    ("d1_30", 0.183),
    ("d31_60", 0.061),
    ("d61_90", 0.028),
    ("d90_plus", 0.016),
)

OPENING_PPE = 8_400_000.0
OPENING_INTANGIBLES = 2_150_000.0
OPENING_LONG_TERM_DEBT = 7_800_000.0
OPENING_CASH = 1_240_000.0
CURRENT_PORTION_OF_DEBT = 1_200_000.0


def fiscal_year_of(month: date) -> int:
    """FY2025 runs Oct 1 2024 -> Sep 30 2025, so October belongs to the next FY."""
    return month.year + 1 if month.month >= FISCAL_YEAR_START_MONTH else month.year


def fiscal_period_of(month: date) -> int:
    """Period 1 is October."""
    return ((month.month - FISCAL_YEAR_START_MONTH) % 12) + 1


def read_monthly_actuals(fact_glob: str) -> pd.DataFrame:
    """
    Monthly gross sales, discounts, invoiced revenue and COGS, straight from the
    sales fact, using the fact view's own revenue and cost expressions.
    """
    revenue_expr = (
        f"CASE WHEN UnitOfBillingId = {BILL_BY_WEIGHT} "
        "THEN pack_weight_lb_sum * Price ELSE pack_item_count_sum * Price END"
    )
    cost_expr = (
        f"CASE WHEN UnitOfBillingId = {BILL_BY_WEIGHT} "
        "THEN pack_weight_lb_sum * CostPrice ELSE pack_item_count_sum * CostPrice END"
    )
    sql = f"""
        SELECT
            CAST(date_trunc('month', CAST(DateExpected AS DATE)) AS DATE) AS month,
            SUM(GrossSales)                       AS gross_sales,
            SUM(DiscountAmount)                   AS discounts,
            SUM(DiscountHandlingCost)             AS discount_handling_cost,
            SUM({revenue_expr})                   AS revenue,
            SUM({cost_expr})                      AS cogs,
            COUNT(DISTINCT OrderId)               AS orders,
            MAX(CAST(DateExpected AS DATE))       AS last_activity
        FROM read_parquet('{fact_glob}', hive_partitioning=true)
        GROUP BY 1
        ORDER BY 1
    """
    con = duckdb.connect()
    try:
        frame = con.execute(sql).df()
    finally:
        con.close()
    frame["month"] = pd.to_datetime(frame["month"]).dt.date
    frame["last_activity"] = pd.to_datetime(frame["last_activity"]).dt.date
    return frame


def drop_incomplete_months(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """
    Keep only months the fact covers to their final day.

    The dataset stops mid-July, and a July that holds four days of trading would
    render as a revenue collapse on every chart on the page. Dropping it is the
    honest read; the dropped months are recorded in the manifest so the omission
    is visible rather than silent.
    """
    dataset_end = max(frame["last_activity"])
    complete: list[bool] = []
    for month in frame["month"]:
        month_end = (pd.Timestamp(month) + pd.offsets.MonthEnd(0)).date()
        complete.append(month_end <= dataset_end)
    dropped = [str(m) for m, ok in zip(frame["month"], complete) if not ok]
    return frame.loc[complete].reset_index(drop=True), dropped


def build(*, fact_glob: str, seed: int = 5311) -> tuple[pd.DataFrame, dict[str, object]]:
    actuals = read_monthly_actuals(fact_glob)
    actuals, dropped_months = drop_incomplete_months(actuals)
    if actuals.empty:
        raise SystemExit("no complete months in the sales fact - nothing to build")

    rows: list[dict[str, object]] = []
    cash = OPENING_CASH
    net_ppe = OPENING_PPE
    long_term_debt = OPENING_LONG_TERM_DEBT
    prior_ar: float | None = None
    prior_inventory: float | None = None
    prior_ap: float | None = None
    prior_total_assets: float | None = None

    for index, record in actuals.iterrows():
        month: date = record["month"]
        rng = np.random.default_rng(seed + int(index))
        days_in_month = (pd.Timestamp(month) + pd.offsets.MonthEnd(0)).day

        gross_sales = float(record["gross_sales"])
        discounts = float(record["discounts"])
        discount_handling_cost = float(record["discount_handling_cost"])
        revenue = float(record["revenue"])
        cogs = float(record["cogs"])

        # ── P&L below the gross-margin line ──────────────────────────────────
        opex_lines: dict[str, float] = {}
        for key, _label, share, volatility in OPEX_STRUCTURE:
            jitter = 1.0 + float(rng.normal(0.0, volatility))
            opex_lines[f"opex_{key}"] = round(revenue * share * max(jitter, 0.55), 2)
        operating_expenses = round(sum(opex_lines.values()), 2)

        depreciation_amortization = round(
            revenue * DA_SHARE * (1.0 + float(rng.normal(0.0, 0.02))), 2
        )
        interest_expense = round(
            revenue * INTEREST_SHARE * (1.0 + float(rng.normal(0.0, 0.06))), 2
        )
        # Occasionally a credit rather than a cost - an insurance recovery or a
        # scrap sale - so the line is not a one-way ratchet.
        other_expense = round(revenue * OTHER_SHARE * float(rng.normal(1.0, 1.4)), 2)

        pre_tax = (
            revenue
            - cogs
            - operating_expenses
            - other_expense
            - interest_expense
            - depreciation_amortization
        )
        # A loss carries no current tax expense; booking one would be wrong and
        # would also hide the loss behind a smaller net number.
        tax_expense = round(pre_tax * EFFECTIVE_TAX_RATE, 2) if pre_tax > 0 else 0.0
        net_income = round(pre_tax - tax_expense, 2)

        # ── Month-end balance sheet ──────────────────────────────────────────
        daily_revenue = revenue / days_in_month
        daily_cogs = cogs / days_in_month

        accounts_receivable = round(
            daily_revenue * DSO_DAYS * (1.0 + float(rng.normal(0.0, 0.045))), 2
        )
        inventory = round(daily_cogs * DIO_DAYS * (1.0 + float(rng.normal(0.0, 0.055))), 2)
        accounts_payable = round(daily_cogs * DPO_DAYS * (1.0 + float(rng.normal(0.0, 0.05))), 2)
        prepaid_expenses = round(operating_expenses * 0.42 * (1.0 + float(rng.normal(0.0, 0.05))), 2)

        # Jitter every bucket, then normalise back onto AR. Jittering four
        # buckets and letting the fifth absorb the remainder does not survive
        # a bad draw: the remainder goes negative, the clamp hides it, and the
        # aging chart quietly stops summing to the receivable it is aging.
        weights = [
            max(share * (1.0 + float(rng.normal(0.0, 0.06))), 1e-6)
            for _bucket, share in AR_AGING_SHARES
        ]
        total_weight = sum(weights)
        aging = {
            f"ar_{bucket}": round(accounts_receivable * weight / total_weight, 2)
            for (bucket, _share), weight in zip(AR_AGING_SHARES, weights)
        }
        # Rounding drift lands on the largest bucket, so the buckets always sum
        # to AR exactly and `test_finance_seed.py` can assert the identity.
        aging["ar_current"] = round(
            aging["ar_current"] + (accounts_receivable - sum(aging.values())), 2
        )

        # Overdue AP tracks the same pressure that ages the receivable book.
        ap_overdue = round(accounts_payable * float(rng.uniform(0.058, 0.112)), 2)
        accrued_liabilities = round(operating_expenses * 0.55 * (1.0 + float(rng.normal(0.0, 0.04))), 2)

        # Cash absorbs the period's earnings and working-capital swing, so the
        # balance sheet moves for a reason instead of drifting at random.
        working_capital_swing = (
            (accounts_receivable - (prior_ar if prior_ar is not None else accounts_receivable))
            + (inventory - (prior_inventory if prior_inventory is not None else inventory))
            - (accounts_payable - (prior_ap if prior_ap is not None else accounts_payable))
        )
        capex = round(revenue * 0.0085 * (1.0 + float(rng.normal(0.0, 0.25))), 2)
        debt_repayment = round(OPENING_LONG_TERM_DEBT * 0.0045, 2)
        cash = round(
            cash
            + net_income
            + depreciation_amortization
            - working_capital_swing
            - capex
            - debt_repayment,
            2,
        )
        # A revolver floor: a distributor with a credit line does not run its
        # operating account negative, it draws. Modelled as a floor rather than
        # a separate facility because the demo does not need the extra line.
        cash = max(cash, 350_000.0)

        # And a sweep at the top. Earnings plus D&A land in cash every month,
        # and with nothing drawing them out the balance grew without limit -
        # which pushed the current ratio past 3.4 by FY2026 and made the
        # balance sheet read like a company that had stopped operating. Real
        # distributors sweep surplus cash to owners or the revolver; holding a
        # month of operating expenses is the target.
        cash_target = round(operating_expenses, 2)
        distributions = 0.0
        if cash > cash_target * 1.35:
            distributions = round(cash - cash_target, 2)
            cash = cash_target

        net_ppe = round(net_ppe + capex - depreciation_amortization, 2)
        long_term_debt = round(max(long_term_debt - debt_repayment, 0.0), 2)

        current_assets = round(cash + accounts_receivable + inventory + prepaid_expenses, 2)
        current_liabilities = round(
            accounts_payable + accrued_liabilities + CURRENT_PORTION_OF_DEBT, 2
        )
        total_assets = round(current_assets + net_ppe + OPENING_INTANGIBLES, 2)
        total_liabilities = round(current_liabilities + long_term_debt, 2)

        row: dict[str, object] = {
            "month": month,
            "month_label": pd.Timestamp(month).strftime("%b %Y"),
            "fiscal_year": fiscal_year_of(month),
            "fiscal_period": fiscal_period_of(month),
            "days_in_month": int(days_in_month),
            # Read from the sales fact - not modelled.
            "gross_sales": round(gross_sales, 2),
            "discounts": round(discounts, 2),
            "discount_handling_cost": round(discount_handling_cost, 2),
            "revenue": round(revenue, 2),
            "cogs": round(cogs, 2),
            "orders": int(record["orders"]),
            "gross_profit": round(revenue - cogs, 2),
            # Modelled.
            "operating_expenses": operating_expenses,
            "depreciation_amortization": depreciation_amortization,
            "interest_expense": interest_expense,
            "other_expense": other_expense,
            "pre_tax_income": round(pre_tax, 2),
            "tax_expense": tax_expense,
            "net_income": net_income,
            "cash": cash,
            "accounts_receivable": accounts_receivable,
            "inventory": inventory,
            "prepaid_expenses": prepaid_expenses,
            "current_assets": current_assets,
            "net_ppe": net_ppe,
            "intangibles": OPENING_INTANGIBLES,
            "total_assets": total_assets,
            "accounts_payable": accounts_payable,
            "ap_overdue": ap_overdue,
            "accrued_liabilities": accrued_liabilities,
            "current_portion_debt": CURRENT_PORTION_OF_DEBT,
            "current_liabilities": current_liabilities,
            "long_term_debt": long_term_debt,
            "total_liabilities": total_liabilities,
            "capex": capex,
            "distributions": distributions,
            # Opening balances make the turnover ratios use an average rather
            # than a month-end snapshot, without the page having to re-sort rows.
            "opening_accounts_receivable": prior_ar,
            "opening_inventory": prior_inventory,
            "opening_accounts_payable": prior_ap,
            "opening_total_assets": prior_total_assets,
        }
        row.update(opex_lines)
        row.update(aging)
        rows.append(row)

        prior_ar = accounts_receivable
        prior_inventory = inventory
        prior_ap = accounts_payable
        prior_total_assets = total_assets

    frame = pd.DataFrame(rows)
    manifest = {
        "source": "seed.generate_synthetic_finance",
        "synthetic": True,
        "grain": "company / month",
        "row_count": int(len(frame)),
        "rows": int(len(frame)),
        "min_date": str(frame["month"].min()),
        "max_date": str(frame["month"].max()),
        "fiscal_years": sorted({int(v) for v in frame["fiscal_year"]}),
        "dropped_incomplete_months": dropped_months,
        "revenue_basis": "Invoiced sales after discounts, read from the sales fact",
        "modelled_lines": [
            "operating_expenses",
            "depreciation_amortization",
            "interest_expense",
            "other_expense",
            "tax_expense",
            "balance sheet",
        ],
        "effective_tax_rate": EFFECTIVE_TAX_RATE,
        "dso_days": DSO_DAYS,
        "dio_days": DIO_DAYS,
        "dpo_days": DPO_DAYS,
    }
    return frame, manifest


def generate(
    *,
    fact_glob: str = "cache/fact_dataset/**/*.parquet",
    seed: int = 5311,
    output: Path = Path("cache/finance/fact_dataset"),
) -> dict[str, object]:
    frame, manifest = build(fact_glob=fact_glob, seed=seed)
    loaded_at = datetime.now(timezone.utc).replace(microsecond=0)
    manifest.update(
        {
            "built_at_utc": loaded_at.isoformat(),
            "last_refresh_utc": loaded_at.isoformat(),
            "dataset_version": str(int(loaded_at.timestamp() * 1000)),
        }
    )
    output.mkdir(parents=True, exist_ok=True)
    for existing in output.glob("*.parquet"):
        existing.unlink()
    frame.to_parquet(output / "finance_demo.parquet", index=False, compression="zstd")
    (output / "_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fact-glob", default="cache/fact_dataset/**/*.parquet")
    parser.add_argument("--seed", type=int, default=5311)
    parser.add_argument("--output", type=Path, default=Path("cache/finance/fact_dataset"))
    args = parser.parse_args()
    print(json.dumps(generate(fact_glob=args.fact_glob, seed=args.seed, output=args.output), indent=2, default=str))


if __name__ == "__main__":
    main()
