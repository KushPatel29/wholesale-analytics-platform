"""
Generate the campaign and spend layer for the portfolio demo.

This is the §B2 source system: the one thing CAC, CPL and ROMI need that the
sales fact cannot supply. Three rules keep it honest:

**New customers are counted, not invented.** The seed reads each customer's
first order date out of the sales fact and counts acquisitions per month, then
allocates that real total across campaigns. The campaign split is modelled; the
total is not.

**Spend reconciles to the Finance page.** Campaign spend for a month sums to the
`Marketing` line of the operating expense schedule in `cache/finance`. Two
seeded layers that disagreed about the same company's marketing budget would be
worse than one, and `test_marketing_layer.py` asserts the tie.

**FY2024 is excluded.** The dataset opens with 504 of its 620 customers placing
a first order in the very first month - that is the inherited book, not
acquisition, and the eleven months after it record almost none. A CAC computed
against an opening book is a meaningless number, so the layer starts at FY2025
where acquisition runs at a steady 2-10 accounts a month.

Each month draws from `default_rng(seed + month_index)`, so the dataset
regenerates identically.
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

# The layer starts here. See the module docstring: everything before this is the
# opening book rather than acquisition.
FIRST_FISCAL_YEAR = 2025

# Channels a foodservice distributor actually buys. No display network, no
# programmatic - those belong to a business whose customers arrive through a
# website, which this one's do not.
#
# key, label, share of monthly spend, cost per lead, lead->qualified rate
CHANNELS: tuple[tuple[str, str, float, float, float], ...] = (
    ("field", "Field sales prospecting", 0.31, 900.0, 0.52),
    ("tradeshow", "Trade shows & industry events", 0.24, 1450.0, 0.44),
    ("referral", "Referral & partner program", 0.14, 520.0, 0.61),
    ("email", "Email & CRM nurture", 0.12, 240.0, 0.28),
    ("trade_press", "Trade press & print", 0.11, 1100.0, 0.21),
    ("search", "Search & trade directories", 0.08, 380.0, 0.24),
)

# The share of selling compensation that chases new business rather than
# servicing the existing book. CAC is defined on total marketing *and sales*
# spend, but charging the whole selling line to acquisition would say this
# company spends its entire field organisation on the ~64 accounts it wins in a
# year, which is plainly false - most of that team services the ~470 accounts it
# already has. The split is stated on the page and in the catalogue so the
# assumption is auditable rather than buried.
ACQUISITION_SHARE_OF_SELLING = 0.18


def fiscal_year_of(month: date) -> int:
    return month.year + 1 if month.month >= FISCAL_YEAR_START_MONTH else month.year


def fiscal_period_of(month: date) -> int:
    return ((month.month - FISCAL_YEAR_START_MONTH) % 12) + 1


def read_acquisitions(fact_glob: str) -> pd.DataFrame:
    """Real new-customer counts, by the month of each customer's first order."""
    sql = f"""
        WITH first_order AS (
            SELECT CustomerId, MIN(CAST(DateExpected AS DATE)) AS first_date
            FROM read_parquet('{fact_glob}', hive_partitioning=true)
            GROUP BY 1
        )
        SELECT
            CAST(date_trunc('month', first_date) AS DATE) AS month,
            COUNT(*) AS new_customers
        FROM first_order
        GROUP BY 1
        ORDER BY 1
    """
    con = duckdb.connect()
    try:
        frame = con.execute(sql).df()
    finally:
        con.close()
    frame["month"] = pd.to_datetime(frame["month"]).dt.date
    return frame


def read_finance_months(finance_dir: Path) -> pd.DataFrame:
    """
    The monthly marketing and selling lines from the seeded financial layer.

    Marketing spend is not chosen here - it is read from the P&L, so the two
    layers describe one company.
    """
    files = sorted(finance_dir.glob("*.parquet"))
    if not files:
        raise SystemExit(
            f"finance layer not found at {finance_dir.as_posix()}; "
            "run `python -m seed.generate_synthetic_finance` first"
        )
    frame = pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)
    frame["month"] = pd.to_datetime(frame["month"]).dt.date
    keep = ["month", "fiscal_year", "opex_marketing", "opex_selling", "revenue"]
    return frame[keep].sort_values("month").reset_index(drop=True)


def build(*, fact_glob: str, finance_dir: Path, seed: int = 7717) -> tuple[pd.DataFrame, dict[str, object]]:
    finance = read_finance_months(finance_dir)
    acquisitions = read_acquisitions(fact_glob)

    scoped = finance[finance["fiscal_year"] >= FIRST_FISCAL_YEAR].reset_index(drop=True)
    if scoped.empty:
        raise SystemExit("no finance months at or after the first marketing fiscal year")

    by_month = {row["month"]: int(row["new_customers"]) for _, row in acquisitions.iterrows()}
    excluded = sorted({str(m) for m in finance["month"]} - {str(m) for m in scoped["month"]})

    rows: list[dict[str, object]] = []
    # Fractional wins owed to each channel, carried between months.
    carry: dict[str, float] = {key: 0.0 for key, _l, _s, _c, _q in CHANNELS}
    for index, record in scoped.iterrows():
        month: date = record["month"]
        rng = np.random.default_rng(seed + int(index))
        marketing_spend = float(record["opex_marketing"])
        selling_spend = float(record["opex_selling"])
        new_customers = by_month.get(month, 0)

        # Jitter the channel mix, then normalise so the campaign rows always sum
        # back to the P&L's marketing line for the month.
        weights = np.array(
            [max(share * (1.0 + float(rng.normal(0.0, 0.10))), 1e-6) for _key, _label, share, _cpl, _q in CHANNELS]
        )
        weights = weights / weights.sum()

        channel_rows: list[dict[str, object]] = []
        for (key, label, _share, cost_per_lead, qualify_rate), weight in zip(CHANNELS, weights):
            spend = marketing_spend * float(weight)
            effective_cpl = cost_per_lead * (1.0 + float(rng.normal(0.0, 0.07)))
            leads = int(max(round(spend / max(effective_cpl, 1.0)), 0))
            qualified = int(round(leads * qualify_rate * (1.0 + float(rng.normal(0.0, 0.08)))))
            qualified = int(min(max(qualified, 0), leads))
            channel_rows.append(
                {
                    "channel_key": key,
                    "channel": label,
                    "spend": spend,
                    "leads": leads,
                    "qualified_leads": qualified,
                }
            )

        # Allocate the month's *real* acquisitions across channels in proportion
        # to qualified leads, by largest remainder (Hamilton).
        #
        # The obvious version - floor each share, then hand the whole remainder
        # to the channel with the most qualified leads - does not survive these
        # magnitudes. A month wins 2-10 accounts across six channels, so almost
        # every floor rounds to zero and the remainder *is* the allocation: 46%
        # of all wins were being dumped on one or two channels. The published
        # channel table then showed field sales closing 45% of qualified leads
        # and trade shows 3%, against a book-wide rate of 23% and a seed that
        # models no per-channel close-rate difference whatsoever. That is a
        # rounding artefact presented as a channel-efficiency finding.
        #
        # Largest remainder gives the spare wins to the channels with the
        # largest fractional parts instead, which is the standard apportionment
        # and keeps every channel near the book-wide rate.
        # The fractional part carries forward between months (`carry`), so a
        # channel that is owed 0.2 of a win every month is paid one after five
        # of them instead of never. Without the carry, largest-remainder still
        # starved the small channels over a year: trade press earned 12
        # qualified leads and 0 wins on $48k of spend, which the page would
        # have published as a channel that never closes.
        qualified_total = sum(int(entry["qualified_leads"]) for entry in channel_rows)
        if qualified_total > 0 and new_customers > 0:
            exact = [
                carry[entry["channel_key"]]
                + new_customers * int(entry["qualified_leads"]) / qualified_total
                for entry in channel_rows
            ]
            floors = [int(np.floor(value)) for value in exact]
            fractions = [value - floor for value, floor in zip(exact, floors)]
            # Settle the difference on the largest fractional parts, ties broken
            # on qualified leads so the result never depends on dict order.
            order = sorted(
                range(len(channel_rows)),
                key=lambda i: (fractions[i], int(channel_rows[i]["qualified_leads"])),
                reverse=True,
            )
            shortfall = new_customers - sum(floors)
            for position in range(shortfall):
                index = order[position % len(order)]
                floors[index] += 1
                fractions[index] -= 1.0
            while sum(floors) > new_customers:
                for index in reversed(order):
                    if floors[index] > 0:
                        floors[index] -= 1
                        fractions[index] += 1.0
                        break
            for entry, won, fraction in zip(channel_rows, floors, fractions):
                entry["new_customers"] = int(won)
                carry[entry["channel_key"]] = fraction
        else:
            for entry in channel_rows:
                entry["new_customers"] = 0

        for entry in channel_rows:
            rows.append(
                {
                    "month": month,
                    "month_label": pd.Timestamp(month).strftime("%b %Y"),
                    "fiscal_year": int(record["fiscal_year"]),
                    "fiscal_period": fiscal_period_of(month),
                    "campaign_id": f"CMP-{pd.Timestamp(month).strftime('%Y%m')}-{entry['channel_key'].upper()}",
                    "campaign_name": f"{entry['channel']} - {pd.Timestamp(month).strftime('%b %Y')}",
                    "channel_key": entry["channel_key"],
                    "channel": entry["channel"],
                    "spend": round(float(entry["spend"]), 2),
                    "leads": int(entry["leads"]),
                    "qualified_leads": int(entry["qualified_leads"]),
                    "new_customers": int(entry["new_customers"]),
                    # Carried per row so the bundle never has to re-open the
                    # finance layer to compute CAC.
                    "selling_spend_month": round(selling_spend, 2),
                    "acquisition_selling_spend_month": round(
                        selling_spend * ACQUISITION_SHARE_OF_SELLING, 2
                    ),
                    "revenue_month": round(float(record["revenue"]), 2),
                }
            )

    frame = pd.DataFrame(rows)
    manifest = {
        "source": "seed.generate_synthetic_marketing",
        "synthetic": True,
        "grain": "campaign (channel) / month",
        "row_count": int(len(frame)),
        "rows": int(len(frame)),
        "min_date": str(frame["month"].min()),
        "max_date": str(frame["month"].max()),
        "fiscal_years": sorted({int(v) for v in frame["fiscal_year"]}),
        "channels": [label for _key, label, _s, _c, _q in CHANNELS],
        "excluded_months": excluded,
        "exclusion_reason": (
            "FY2024 is the dataset's opening book: 504 of 620 customers place a first order in the "
            "first month and the eleven months after it record almost none. CAC against an opening "
            "book is meaningless, so the marketing layer starts at FY2025."
        ),
        "new_customer_basis": "Month of each customer's first order in the sales fact",
        "spend_basis": "Campaign spend sums to the Marketing operating-expense line in cache/finance",
        "acquisition_share_of_selling": ACQUISITION_SHARE_OF_SELLING,
        "modelled": ["channel mix", "leads", "qualified leads", "campaign attribution of new customers"],
    }
    return frame, manifest


def generate(
    *,
    fact_glob: str = "cache/fact_dataset/**/*.parquet",
    finance_dir: Path = Path("cache/finance/fact_dataset"),
    seed: int = 7717,
    output: Path = Path("cache/marketing/fact_dataset"),
) -> dict[str, object]:
    frame, manifest = build(fact_glob=fact_glob, finance_dir=finance_dir, seed=seed)
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
    frame.to_parquet(output / "marketing_demo.parquet", index=False, compression="zstd")
    (output / "_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fact-glob", default="cache/fact_dataset/**/*.parquet")
    parser.add_argument("--finance-dir", type=Path, default=Path("cache/finance/fact_dataset"))
    parser.add_argument("--seed", type=int, default=7717)
    parser.add_argument("--output", type=Path, default=Path("cache/marketing/fact_dataset"))
    args = parser.parse_args()
    print(json.dumps(
        generate(fact_glob=args.fact_glob, finance_dir=args.finance_dir, seed=args.seed, output=args.output),
        indent=2, default=str,
    ))


if __name__ == "__main__":
    main()
