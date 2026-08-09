"""
Generate a synthetic wholesale-distribution fact dataset.

The platform normally reads a partitioned parquet fact dataset that an ETL job
builds from the source ERP. This script invents a comparable business and
writes it through the *same* writer the production ETL uses
(`etl.partition_writer.upsert_dataset`), so the demo data lands in the real
layout — hive partitions by year/month/day plus a manifest — and every
downstream service, filter and dashboard exercises its normal code path.

Nothing here is derived from real customer, pricing or supplier data. The
numbers are invented from the distributions in `seed/catalog.py`, with a fixed
seed so two runs on two machines produce identical bytes.

Usage:
    python -m seed.generate_synthetic_data                  # ~24 months
    python -m seed.generate_synthetic_data --months 6       # smaller, for CI
    python -m seed.generate_synthetic_data --seed 7         # a different company
"""

from __future__ import annotations

import re
import argparse
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from seed import catalog as C  # noqa: E402

DEFAULT_SEED = 4207
DEFAULT_MONTHS = 24
DEFAULT_CUSTOMERS = 620
DEFAULT_PRODUCTS = 880
DEFAULT_SUPPLIERS = 46

# The fiscal calendar the app reports on (app/services/filters.py). The window
# is built backwards from complete fiscal years so "prior fiscal year-to-date"
# lands on real data rather than on the two weeks that happened to precede the
# dataset.
FISCAL_YEAR_START_MONTH = 10
FISCAL_YEAR_START_DAY = 1
DEFAULT_FISCAL_YEARS = 3

# ─────────────────────────────────────────────────────────────────────────────
# Customer lifecycle and trajectory
# ─────────────────────────────────────────────────────────────────────────────
# The previous generator gave every account a constant order rate across a
# single ten-month window. Two consequences, both visible on the deployed demo:
# every customer's first order fell in the first fortnight of the dataset, and
# the prior-year comparison had almost nothing to compare against - so the
# dashboard reported +896% revenue growth, "10 up / 0 down" and "no decliners".
# A book where nothing ever declines is recognisable as synthetic on sight.
#
# Accounts now carry a lifecycle and an annual trajectory, and orders are drawn
# month by month against it.

# Of the accounts trading in both years, the share that grow. 55/45 is a book
# that is expanding overall while still giving a reviewer real decline to find.
GAINER_SHARE = 0.55

# Year-on-year *revenue* multipliers, drawn per account.
#
# The spread is deliberately narrow. A wide one does not produce a livelier
# book, it produces a runaway one: revenue is a sum of `growth ** years`, and
# that is convex, so the upper tail dominates the total long before the median
# account moves. At sd 0.16 over 2.75 years, a handful of accounts clipped at
# the top of the range carried the whole company to +77% year-over-year - the
# same "everything is growing" tell the multi-year window was meant to remove.
GAINER_GROWTH = (1.09, 0.07)   # mean, sd
DECLINER_GROWTH = (0.89, 0.07)
GROWTH_CLIP = (0.62, 1.45)

# How the annual trajectory splits between ordering more often and ordering
# more each time. The two exponents must sum to 1.0, so that `AnnualGrowth`
# means annual *revenue* growth for that account and nothing else - otherwise
# the number in the dimension table and the movement on the dashboard are
# different quantities with the same name.
GROWTH_SPLIT_FREQUENCY = 0.7
GROWTH_SPLIT_BASKET = 0.3

# Lifecycle mix. Shares are of the whole book and are drawn without replacement.
NEW_LOGO_SHARE = 0.18       # first traded in FY2 or FY3
CHURN_SHARE = 0.14          # stopped and never came back
REACTIVATED_SHARE = 0.07    # went quiet for two or three quarters, then returned

# A newly won account ramps rather than arriving at full run rate. Without
# this, one new logo in a region holding six accounts reported that region up
# 120% year-over-year, which is a property of the generator rather than of the
# business - and exactly the kind of number that makes a demo look fabricated.
NEW_LOGO_RAMP_DAYS = 180
NEW_LOGO_RAMP_FLOOR = 0.35

# ─────────────────────────────────────────────────────────────────────────────
# Deliberate, explainable anomalies
# ─────────────────────────────────────────────────────────────────────────────
# A demo dataset with no story in it gives a reviewer nothing to find. These
# three are planted so that the narrative panels have something true to point
# at, and so that someone who drills in is rewarded rather than shown noise.
#
# 1. A department whose margin collapses late in the window: a cost step the
#    retail price never followed.
#    Sized against the department's own margin, not picked for drama:
#    Electronics runs the thinnest markup in the catalogue (12.3%), so a 19%
#    cost step put the whole department at -12.7% margin for nine months. No
#    chain runs a department at a 12-point loss for three quarters; a reviewer
#    reads that as a broken generator rather than as a finding. 10% takes it
#    from ~12% to low single digits, which is a bad year, not a fantasy.
MARGIN_SHOCK_DEPARTMENT = "Electronics"
MARGIN_SHOCK_START_MONTHS_BEFORE_END = 8
MARGIN_SHOCK_COST_STEP = 0.10

# 2. One supplier that tips loss-making: cost rises through the final year
#    until it crosses the price it is sold at.
LOSS_MAKING_SUPPLIER_RANK = 3       # by revenue, so it is big enough to matter
# Enough to cross zero and sit slightly under it, not enough to look invented.
LOSS_MAKING_COST_STEP = 0.26

# 3. A region in genuine decline, so the regions page has a real story rather
#    than a uniform book.
DECLINING_REGION = "Midwest"
DECLINING_REGION_ANNUAL = 0.82

# Cost inflation across the whole window, against a much smaller list-price
# increase. This is what makes margin compression visible in the trend charts
# instead of merely asserted in a README.
BEEF_COST_INFLATION = 0.14
LIST_PRICE_INFLATION = 0.05

# A small number of SKUs are sold below landed cost to hold an account.
LOSS_LEADER_SHARE = 0.02
LOSS_LEADER_DISCOUNT = 0.88
# Only departments with room in the markup are used - see build_products.
LOSS_LEADER_MIN_MARKUP = 1.25




# Two-letter department code for SKU numbers. Derived from initials so
# "Fresh & Produce" and "Household Essentials" cannot collide the way a naive
# name[:2] does ("Gr" for both Grocery and... nothing yet, but the next
# department added would have found it).
_DEPT_CODES = {
    "Grocery": "GR",
    "Fresh & Produce": "FP",
    "Dairy & Frozen": "DF",
    "Meat & Seafood": "MS",
    "Health & Wellness": "HW",
    "Household Essentials": "HE",
    "Apparel": "AP",
    "Electronics": "EL",
    "Home & Kitchen": "HK",
    "Toys & Seasonal": "TS",
}


def _dept_code(name: str) -> str:
    if name in _DEPT_CODES:
        return _DEPT_CODES[name]
    parts = [w for w in re.split(r"[^A-Za-z]+", name) if w]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return name[:2].upper()


@dataclass
class Dimensions:
    customers: pd.DataFrame
    products: pd.DataFrame
    suppliers: pd.DataFrame


def _weighted_choice(rng: np.random.Generator, options, weights, size: int) -> np.ndarray:
    probs = np.asarray(weights, dtype=float)
    probs = probs / probs.sum()
    return rng.choice(len(options), size=size, p=probs)


def build_suppliers(rng: np.random.Generator, n: int) -> pd.DataFrame:
    """Suppliers are named from a fixed vocabulary so names never collide."""
    combos = [(p, s) for p in C.SUPPLIER_PREFIXES for s in C.SUPPLIER_SUFFIXES]
    picks = rng.choice(len(combos), size=min(n, len(combos)), replace=False)
    names = [f"{combos[i][0]} {combos[i][1]}" for i in picks]
    return pd.DataFrame(
        {
            "SupplierId": [f"S{1000 + i}" for i in range(len(names))],
            "SupplierName": names,
        }
    )


def build_products(rng: np.random.Generator, n: int, suppliers: pd.DataFrame) -> pd.DataFrame:
    """
    Build a SKU list spread across protein groups in the proportions the
    catalog specifies, each with a landed cost and a list price.
    """
    group_names = [g.name for g in C.DEPARTMENTS]
    group_idx = _weighted_choice(rng, group_names, [g.weight for g in C.DEPARTMENTS], n)

    rows = []
    for i, gi in enumerate(group_idx):
        group = C.DEPARTMENTS[gi]
        cut = C.CATEGORIES[group.name][rng.integers(len(C.CATEGORIES[group.name]))]
        grade = C.BRAND_TIERS[rng.integers(len(C.BRAND_TIERS))]
        prep = C.PACK_SIZES[rng.integers(len(C.PACK_SIZES))]

        # Landed cost varies around the group's typical cost per lb.
        cost_per_lb = float(group.unit_cost * rng.lognormal(mean=0.0, sigma=0.22))
        price_per_lb = cost_per_lb * group.markup * float(rng.normal(1.0, 0.06))
        price_per_lb = max(price_per_lb, cost_per_lb * 1.02)

        catch_weight = bool(rng.random() < group.weighed_share)
        # Weight-billed lines quote a price per lb; unit-billed lines quote a
        # price per case, so we need a case weight to convert.
        case_weight = float(np.clip(rng.normal(11.0, 4.0), 2.0, 40.0))

        rows.append(
            {
                "ProductId": f"P{10000 + i}",
                "SKU": f"{_dept_code(group.name)}-{1000 + i}",
                "ProductName": f"{cut} {grade} {prep}".replace("  ", " ").strip(),
                "ProteinType": group.name,
                "ProteinName": group.name,
                "Category": group.name,
                "ProductCategory": group.name,
                "IsCatchWeight": catch_weight,
                "UnitOfBillingId": C.BILL_BY_WEIGHT if catch_weight else C.BILL_BY_UNIT,
                "YieldPct": round(float(np.clip(rng.normal(group.sell_through, 0.05), 0.35, 0.99)), 4),
                "CostPerLb": round(cost_per_lb, 4),
                "PricePerLb": round(price_per_lb, 4),
                "CaseWeightLb": round(case_weight, 3),
            }
        )

    products = pd.DataFrame(rows)

    # A few SKUs are deliberately priced under cost.
    #
    # Only in departments that can absorb it. A retailer picks loss leaders from
    # high-traffic staples with room in the markup, not from its thinnest
    # category - and mechanically, one loss leader among the four or five SKUs a
    # 2.5%-weight department gets takes that whole department's margin to zero,
    # which then reads on the dashboard as a finding rather than as an artefact
    # of how few SKUs it was given.
    eligible_markup = products["ProteinType"].map(
        {g.name: g.markup for g in C.DEPARTMENTS}
    ).to_numpy()
    loss_eligible = np.flatnonzero(eligible_markup >= LOSS_LEADER_MIN_MARKUP)
    if len(loss_eligible) == 0:
        loss_eligible = np.arange(len(products))
    n_loss = max(1, min(int(len(products) * LOSS_LEADER_SHARE), len(loss_eligible)))
    loss_idx = rng.choice(loss_eligible, size=n_loss, replace=False)
    products.loc[loss_idx, "PricePerLb"] = (
        products.loc[loss_idx, "CostPerLb"] * LOSS_LEADER_DISCOUNT
    ).round(4)
    products["IsLossLeader"] = False
    products.loc[loss_idx, "IsLossLeader"] = True

    sup_idx = rng.integers(0, len(suppliers), size=len(products))
    products["SupplierId"] = suppliers["SupplierId"].to_numpy()[sup_idx]
    products["SupplierName"] = suppliers["SupplierName"].to_numpy()[sup_idx]
    return products


def build_customers(rng: np.random.Generator, n: int, start: date, end: date) -> pd.DataFrame:
    """
    Build the customer book, including which segment and region each account
    belongs to, who owns it, and the window over which it actually trades.
    """
    seg_idx = _weighted_choice(
        rng, C.STORE_FORMATS, [s.weight for s in C.STORE_FORMATS], n
    )
    reg_idx = _weighted_choice(rng, C.REGIONS, [r.weight for r in C.REGIONS], n)

    used: set[str] = set()
    names: list[str] = []
    for i in range(n):
        while True:
            first = C.CUSTOMER_FIRST[rng.integers(len(C.CUSTOMER_FIRST))]
            second = C.CUSTOMER_SECOND[rng.integers(len(C.CUSTOMER_SECOND))]
            name = f"{first} {second}"
            if name not in used:
                used.add(name)
                names.append(name)
                break
            # Numbered suffix keeps the vocabulary small without collisions.
            name = f"{first} {second} {len(used)}"
            if name not in used:
                used.add(name)
                names.append(name)
                break

    regions = [C.REGIONS[i] for i in reg_idx]
    segments = [C.STORE_FORMATS[i] for i in seg_idx]

    # Reps own accounts in the regions they cover; anything uncovered falls to
    # the rep with the largest book, which is how concentration builds up.
    rep_names: list[str] = []
    rep_ids: list[str] = []
    for region in regions:
        eligible = [r for r in C.MARKET_MANAGERS if region.name in r.regions]
        if not eligible:
            eligible = [C.MARKET_MANAGERS[0]]
        shares = np.array([r.book_share for r in eligible], dtype=float)
        pick = eligible[int(rng.choice(len(eligible), p=shares / shares.sum()))]
        rep_names.append(pick.name)
        rep_ids.append(f"R{C.MARKET_MANAGERS.index(pick) + 1:02d}")

    total_days = (end - start).days
    first_order = np.full(n, start, dtype=object)
    last_order = np.full(n, end, dtype=object)
    # A dormancy gap, for accounts that go quiet and later come back. Stored as
    # a half-open window; NaT-equivalent is None.
    dormant_from: list[date | None] = [None] * n
    dormant_to: list[date | None] = [None] * n

    # ------------------------------------------------------------------
    # Lifecycle. Assigned by drawing disjoint groups so an account is exactly
    # one of continuing / new logo / churned / reactivated.
    #
    # New logos are drawn from outside the declining region. With a book this
    # size a region holds only a handful of accounts, so one new logo landing
    # in the region that is supposed to be shrinking turns a planted -18% into
    # a reported +14% and the story a reviewer is meant to find is not there.
    # Acquisition concentrating away from a weak territory is also what
    # actually happens.
    region_names = [r.name for r in regions]
    eligible_for_new = np.array(
        [i for i in range(n) if region_names[i] != DECLINING_REGION], dtype=int
    )
    if len(eligible_for_new) == 0:
        eligible_for_new = np.arange(n)

    n_new = min(int(n * NEW_LOGO_SHARE), len(eligible_for_new))
    new_idx = rng.choice(eligible_for_new, size=n_new, replace=False)

    remaining = np.setdiff1d(np.arange(n), new_idx)
    remaining = rng.permutation(remaining)
    n_churn = min(int(n * CHURN_SHARE), len(remaining))
    n_react = min(int(n * REACTIVATED_SHARE), max(0, len(remaining) - n_churn))
    churn_idx = remaining[:n_churn]
    react_idx = remaining[n_churn : n_churn + n_react]

    lifecycle = np.array(["continuing"] * n, dtype=object)

    # New logos land anywhere from a third of the way in to near the end, so
    # the cohort view has several distinct acquisition cohorts rather than one.
    for i in new_idx:
        lifecycle[i] = "new_logo"
        first_order[i] = start + timedelta(
            days=int(rng.integers(int(total_days * 0.34), max(int(total_days * 0.34) + 1, total_days - 45)))
        )

    # Churn is spread across the whole window, not bunched at the end. An
    # account that stopped 20 months ago and one that stopped last month are
    # different findings, and the RFM and churn-risk panels need both.
    for i in churn_idx:
        lifecycle[i] = "churned"
        last_order[i] = start + timedelta(
            days=int(rng.integers(int(total_days * 0.15), max(int(total_days * 0.15) + 1, total_days - 40)))
        )

    # Reactivation: a gap of two to four quarters, then back.
    for i in react_idx:
        lifecycle[i] = "reactivated"
        gap_days = int(rng.integers(180, 380))
        latest_gap_start = total_days - gap_days - 60
        if latest_gap_start <= 60:
            lifecycle[i] = "continuing"
            continue
        gap_start = int(rng.integers(60, latest_gap_start))
        dormant_from[i] = start + timedelta(days=gap_start)
        dormant_to[i] = start + timedelta(days=gap_start + gap_days)

    # ------------------------------------------------------------------
    # Annual trajectory. A two-population mix rather than noise around 1.0:
    # a book where every account drifts by a few percent has no movers, and the
    # movement panels exist to rank movers.
    is_gainer = rng.random(n) < GAINER_SHARE
    annual_growth = np.where(
        is_gainer,
        rng.normal(*GAINER_GROWTH, size=n),
        rng.normal(*DECLINER_GROWTH, size=n),
    ).clip(*GROWTH_CLIP)

    # New logos are not given a growth bonus. The growth they contribute is
    # that they did not exist in the prior year, which the comparison already
    # captures; boosting their trajectory as well counts them twice.

    # The planted regional decline is carried in its own column rather than
    # folded into each account's trajectory.
    #
    # Folding it in did not survive: a region holds six or seven accounts at
    # this book size, so whether the territory read as -18% or as +14% came down
    # to which individual accounts happened to be drawn as gainers. A region in
    # decline is a fact about the territory, so it is modelled at the territory
    # level and applies deterministically - while each account keeps its own
    # trajectory on top, so the region falls without every account in it falling.
    region_factor = np.array(
        [DECLINING_REGION_ANNUAL if name == DECLINING_REGION else 1.0 for name in region_names]
    )

    return pd.DataFrame(
        {
            "CustomerId": [f"C{20000 + i}" for i in range(n)],
            "CustomerName": names,
            "Segment": [s.name for s in segments],
            "IsRetail": [s.is_retail for s in segments],
            "RegionName": region_names,
            "RegionId": [f"RG{C.REGIONS.index(r) + 1:02d}" for r in regions],
            "City": [r.cities[int(rng.integers(len(r.cities)))] for r in regions],
            "Province": [C.STATE_BY_REGION[r.name] for r in regions],
            "SalesRepId": rep_ids,
            "SalesRepName": rep_names,
            "OrdersPerMonth": [s.orders_per_month for s in segments],
            "LinesPerOrder": [s.lines_per_order for s in segments],
            "PriceIndex": [s.price_index for s in segments],
            "SizeIndex": [s.size_index for s in segments],
            "TransitDays": [r.transit_days for r in regions],
            "FirstOrderDate": first_order,
            "LastOrderDate": last_order,
            "DormantFrom": dormant_from,
            "DormantTo": dormant_to,
            "AnnualGrowth": annual_growth,
            "RegionAnnualFactor": region_factor,
            "Lifecycle": lifecycle,
            "IsChurned": lifecycle == "churned",
        }
    )


def _month_starts(start: date, end: date) -> list[date]:
    """Every month start touching [start, end], in order."""
    months: list[date] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        months.append(cursor)
        cursor = date(cursor.year + (cursor.month // 12), (cursor.month % 12) + 1, 1)
    return months


def build_orders(rng: np.random.Generator, customers: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """
    One row per order, drawn month by month against each account's trajectory.

    Sampling per month rather than uniformly across the whole span is what makes
    a year-over-year comparison mean anything: an account on a 0.88 annual
    trajectory genuinely orders less in FY3 than it did in FY2, so the movement
    panels rank real movement instead of Poisson noise. It also lets an account
    go dormant and come back, which a single uniform draw cannot express.

    Returns a DemandFactor per order, carried through to the lines so the
    trajectory moves basket size as well as order frequency - a shrinking
    account usually does both.
    """
    order_customer: list[int] = []
    order_dates: list[date] = []
    order_demand: list[float] = []

    months = _month_starts(start, end)

    for i, row in enumerate(customers.itertuples(index=False)):
        first = row.FirstOrderDate
        last = row.LastOrderDate
        if (last - first).days <= 0:
            continue
        # The account's own trajectory and the territory it trades in are
        # separate effects that compound.
        growth = float(row.AnnualGrowth) * float(row.RegionAnnualFactor)
        dormant_from = row.DormantFrom
        dormant_to = row.DormantTo
        is_new_logo = row.Lifecycle == "new_logo"

        for month_start in months:
            days_in_month = (
                date(month_start.year + (month_start.month // 12), (month_start.month % 12) + 1, 1)
                - month_start
            ).days
            month_end = month_start + timedelta(days=days_in_month - 1)

            # Clip the month to this account's trading window.
            window_start = max(month_start, first, start)
            window_end = min(month_end, last, end)
            if window_end < window_start:
                continue
            if dormant_from is not None and dormant_to is not None:
                if window_start >= dormant_from and window_end <= dormant_to:
                    continue

            # Years elapsed since the window opened, so `growth` compounds the
            # way an annual growth rate is understood to.
            years_elapsed = (month_start - start).days / 365.25
            frequency_factor = growth ** (years_elapsed * GROWTH_SPLIT_FREQUENCY)

            # A newly won account ramps to full run rate over its first two
            # quarters instead of switching on at full volume.
            ramp = 1.0
            if is_new_logo:
                days_trading = (window_end - first).days
                if days_trading < NEW_LOGO_RAMP_DAYS:
                    progress = max(0.0, days_trading) / NEW_LOGO_RAMP_DAYS
                    ramp = NEW_LOGO_RAMP_FLOOR + (1.0 - NEW_LOGO_RAMP_FLOOR) * progress

            # Part-months at the edge of a trading window get proportionally
            # fewer orders rather than a full month's worth.
            coverage = ((window_end - window_start).days + 1) / days_in_month
            expected = row.OrdersPerMonth * frequency_factor * ramp * coverage
            if expected <= 0:
                continue
            n_orders = int(rng.poisson(expected))
            if n_orders <= 0:
                continue

            span = (window_end - window_start).days
            offsets = rng.integers(0, span + 1, size=n_orders)
            # The remainder of the trajectory lands on basket size: a shrinking
            # account both orders less often and orders less each time.
            basket_factor = float(growth ** (years_elapsed * GROWTH_SPLIT_BASKET))
            for off in offsets:
                order_customer.append(i)
                order_dates.append(window_start + timedelta(days=int(off)))
                order_demand.append(basket_factor)

    orders = pd.DataFrame(
        {"cust_idx": order_customer, "DateOrdered": order_dates, "DemandFactor": order_demand}
    )
    orders = orders.sort_values("DateOrdered", kind="mergesort").reset_index(drop=True)
    orders["OrderId"] = [f"O{300000 + i}" for i in range(len(orders))]

    # Restaurants order for delivery a day or two out; big accounts book further
    # ahead. Expected date is what the fact dataset partitions on.
    lead = rng.integers(1, 5, size=len(orders))
    orders["DateExpected"] = [
        d + timedelta(days=int(x)) for d, x in zip(orders["DateOrdered"], lead)
    ]
    return orders


def build_lines(
    rng: np.random.Generator,
    orders: pd.DataFrame,
    customers: pd.DataFrame,
    products: pd.DataFrame,
    start: date,
    end: date,
    loss_making_supplier_id: str | None = None,
) -> pd.DataFrame:
    """Explode orders into order lines and price them."""
    cust = customers.iloc[orders["cust_idx"].to_numpy()].reset_index(drop=True)

    lines_per = np.maximum(1, rng.poisson(cust["LinesPerOrder"].to_numpy()))
    line_order_idx = np.repeat(np.arange(len(orders)), lines_per)
    n_lines = len(line_order_idx)

    prod_idx = rng.integers(0, len(products), size=n_lines)
    prod = products.iloc[prod_idx].reset_index(drop=True)
    cust_l = cust.iloc[line_order_idx].reset_index(drop=True)
    ord_l = orders.iloc[line_order_idx].reset_index(drop=True)

    expected = pd.to_datetime(ord_l["DateExpected"])
    month_idx = expected.dt.month.to_numpy() - 1

    # Seasonality is applied to quantity, so revenue swings with the calendar
    # the way a real perishables book does.
    season_lookup = {g.name: np.asarray(g.seasonality) for g in C.DEPARTMENTS}
    dept_short_rate = {g.name: g.short_ship_rate for g in C.DEPARTMENTS}
    dept_fill_when_short = {g.name: g.typical_fill_when_short for g in C.DEPARTMENTS}
    season = np.array(
        [season_lookup[p][m] for p, m in zip(prod["ProteinType"].to_numpy(), month_idx)]
    )

    # How far through the window each line sits, for cost/price drift.
    total_days = max((end - start).days, 1)
    progress = ((expected - pd.Timestamp(start)).dt.days.to_numpy() / total_days).clip(0, 1)

    is_beef = (prod["ProteinType"].to_numpy() == "Beef").astype(float)
    cost_drift = 1.0 + progress * BEEF_COST_INFLATION * is_beef
    price_drift = 1.0 + progress * LIST_PRICE_INFLATION

    # ------------------------------------------------------------------
    # The two planted cost anomalies. Both are applied to cost only: the retail
    # price does not follow, which is what makes them show up as margin rather
    # than as revenue, and what makes them worth finding.
    #
    # 1. A department-wide cost step partway through the final year. This reads
    #    on the dashboard as a margin cliff in one department while revenue
    #    holds - the shape of a supplier renegotiation that went badly.
    shock_start = pd.Timestamp(end) - pd.DateOffset(months=MARGIN_SHOCK_START_MONTHS_BEFORE_END)
    in_shock_window = (expected >= shock_start).to_numpy()
    is_shock_dept = prod["ProteinType"].to_numpy() == MARGIN_SHOCK_DEPARTMENT
    cost_drift = cost_drift * np.where(in_shock_window & is_shock_dept, 1.0 + MARGIN_SHOCK_COST_STEP, 1.0)

    # 2. One supplier drifts loss-making across the final year. Ramped rather
    #    than stepped, so the supplier trend line shows it crossing over.
    if loss_making_supplier_id:
        is_loss_supplier = prod["SupplierId"].to_numpy() == loss_making_supplier_id
        final_year_start = pd.Timestamp(end) - pd.DateOffset(months=12)
        ramp = ((expected - final_year_start).dt.days.to_numpy() / 365.0).clip(0.0, 1.0)
        cost_drift = cost_drift * np.where(is_loss_supplier, 1.0 + ramp * LOSS_MAKING_COST_STEP, 1.0)

    # Cases ordered, scaled by how big this class of buyer is. A single
    # restaurant line is typically one or two cases; the scale parameter is set
    # so the median line lands in that range rather than at pallet volumes.
    base_cases = rng.gamma(shape=2.0, scale=0.55, size=n_lines)
    demand_factor = ord_l["DemandFactor"].to_numpy() if "DemandFactor" in ord_l else np.ones(n_lines)
    cases = np.maximum(
        1, np.round(base_cases * cust_l["SizeIndex"].to_numpy() * season * demand_factor)
    )

    case_weight = prod["CaseWeightLb"].to_numpy()
    # Catch weight means the shipped weight varies from the nominal case weight.
    weight_noise = rng.normal(1.0, 0.06, size=n_lines).clip(0.75, 1.25)
    shipped_lb = cases * case_weight * weight_noise

    by_weight = prod["UnitOfBillingId"].to_numpy() == C.BILL_BY_WEIGHT

    cost_per_lb = prod["CostPerLb"].to_numpy() * cost_drift
    price_per_lb = prod["PricePerLb"].to_numpy() * price_drift * cust_l["PriceIndex"].to_numpy()

    # `Price` and `CostPrice` are per billing unit: per lb when billed by
    # weight, per case otherwise. The DuckDB fact view multiplies these by the
    # pack weight or pack unit count, so the two branches must agree here.
    price = np.where(by_weight, price_per_lb, price_per_lb * case_weight)
    cost_price = np.where(by_weight, cost_per_lb, cost_per_lb * case_weight)

    # Delivery performance: region transit + method surcharge, with a
    # method-specific chance of running late.
    method_idx = _weighted_choice(
        rng, C.FULFILLMENT_METHODS, [m.weight for m in C.FULFILLMENT_METHODS], n_lines
    )
    methods = [C.FULFILLMENT_METHODS[i] for i in method_idx]
    extra = np.array([m.extra_days for m in methods])
    late_rate = np.array([m.late_rate for m in methods])
    is_late = rng.random(n_lines) < late_rate
    slip = np.where(is_late, rng.integers(1, 5, size=n_lines), 0)
    transit = cust_l["TransitDays"].to_numpy() + extra + slip
    ship_date = expected - pd.to_timedelta(np.ones(n_lines), unit="D")
    delivery_date = ship_date + pd.to_timedelta(transit, unit="D")

    # ------------------------------------------------------------------
    # Availability: what the store asked for against what the DC could send.
    #
    # Until now QuantityShipped was a copy of QuantityOrdered, which made fill
    # rate and OTIF trivially 100% - two of the metrics a replenishment team
    # actually runs on, reporting a number that could never move. Lines now go
    # short at a department-specific rate, because the reason a supercenter
    # misses a case differs by department: produce is grown rather than
    # manufactured, seasonal is committed months before the demand is known,
    # and packaged grocery almost never misses.
    #
    # Short shipping is correlated with lateness rather than independent of it.
    # A lane under pressure misses dates and misses cases at the same time, and
    # modelling them independently would let a dashboard "discover" that they
    # are unrelated, which is an artefact of the generator rather than a finding.
    short_rate = np.array([dept_short_rate[name] for name in prod["ProteinType"]])
    fill_when_short = np.array([dept_fill_when_short[name] for name in prod["ProteinType"]])

    # Lines on a late lane are about 1.8x more likely to also go short.
    pressure = np.where(is_late, 1.8, 0.92)
    is_short = rng.random(n_lines) < np.clip(short_rate * pressure, 0.0, 0.85)

    fill_ratio = np.where(
        is_short,
        np.clip(rng.normal(fill_when_short, 0.16, size=n_lines), 0.05, 0.97),
        1.0,
    )
    shipped_cases = np.maximum(np.where(is_short, np.floor(cases * fill_ratio), cases), 0.0)
    # A line that fills to zero is a stockout rather than a short ship.
    is_stockout = is_short & (shipped_cases <= 0)
    backorder_cases = np.maximum(cases - shipped_cases, 0.0)

    # Weight and packs follow what actually shipped, not what was asked for -
    # revenue is derived from these downstream, so billing a short line at the
    # ordered quantity would silently overstate the book.
    shipped_lb = shipped_cases * case_weight * weight_noise
    pack_count = np.maximum(1, np.round(np.maximum(shipped_cases, 1) * rng.uniform(0.8, 1.2, size=n_lines)))
    pack_weight_lb_sum = shipped_lb
    pack_item_count_sum = shipped_cases

    # ------------------------------------------------------------------
    # Inventory position at the moment the line was picked.
    #
    # The fact table is at order-line grain and a true inventory snapshot is a
    # different grain entirely (SKU x location x day). Rather than invent a
    # second table the query layer cannot see, each line carries the position
    # it was picked against - which is enough for cover, turns, stockout rate
    # and excess, and is how most ERP extracts hand it over anyway.
    #
    # Cover drives availability rather than the other way round: a line that
    # went short was picked against a thin position, and one that filled was
    # picked against a healthy one. Generating them independently would break
    # the relationship the page exists to show.
    daily_demand = np.maximum(cases / 7.0, 0.15)
    # Cover is not normally distributed in a real chain: most SKUs sit near the
    # plan and a long tail of slow movers accumulates months of stock nobody
    # has written off yet. A symmetric distribution produces a book with no
    # excess at all, which is the one thing every retailer's inventory has.
    slow_mover = rng.random(n_lines) < 0.14
    target_cover_days = np.where(
        by_weight,
        rng.normal(9.0, 2.5, size=n_lines),    # perishable: short cover by design
        np.where(
            slow_mover,
            rng.lognormal(mean=4.5, sigma=0.45, size=n_lines),  # the dead tail
            rng.normal(31.0, 9.0, size=n_lines),
        ),
    ).clip(2.0, 400.0)

    cover_days = np.where(
        is_stockout,
        rng.uniform(0.0, 0.6, size=n_lines),
        np.where(
            is_short,
            np.clip(rng.normal(2.4, 1.1, size=n_lines), 0.2, 6.0),
            target_cover_days,
        ),
    )
    on_hand_cases = np.maximum(np.round(cover_days * daily_demand), 0.0)

    # Safety stock and reorder point, so "below reorder point" is a fact about
    # the row rather than a threshold invented in the dashboard.
    safety_stock_cases = np.maximum(np.round(daily_demand * np.where(by_weight, 3.0, 9.0)), 1.0)
    lead_time_days = cust_l["TransitDays"].to_numpy() + extra
    reorder_point_cases = np.maximum(
        np.round(daily_demand * lead_time_days + safety_stock_cases), 1.0
    )

    on_hand_value = on_hand_cases * cost_price

    frame = pd.DataFrame(
        {
            "OrderLineId": np.arange(1, n_lines + 1),
            "OrderId": ord_l["OrderId"].to_numpy(),
            "OrderStatus": C.ORDER_STATUSES[0],
            "DateOrdered": pd.to_datetime(ord_l["DateOrdered"]).to_numpy(),
            "DateExpected": expected.to_numpy(),
            "ShipDate": ship_date.to_numpy(),
            "DeliveryDate": delivery_date.to_numpy(),
            "CustomerId": cust_l["CustomerId"].to_numpy(),
            "CustomerName": cust_l["CustomerName"].to_numpy(),
            "CustomerSegment": cust_l["Segment"].to_numpy(),
            "IsRetail": cust_l["IsRetail"].to_numpy(),
            "RegionId": cust_l["RegionId"].to_numpy(),
            "RegionName": cust_l["RegionName"].to_numpy(),
            "City": cust_l["City"].to_numpy(),
            "Province": cust_l["Province"].to_numpy(),
            "SalesRepId": cust_l["SalesRepId"].to_numpy(),
            "SalesRepName": cust_l["SalesRepName"].to_numpy(),
            "PrimarySalesRepId": cust_l["SalesRepId"].to_numpy(),
            "PrimarySalesRepName": cust_l["SalesRepName"].to_numpy(),
            "ProductId": prod["ProductId"].to_numpy(),
            "SKU": prod["SKU"].to_numpy(),
            "SkuName": prod["ProductName"].to_numpy(),
            "ProductName": prod["ProductName"].to_numpy(),
            "ProteinType": prod["ProteinType"].to_numpy(),
            "ProteinName": prod["ProteinName"].to_numpy(),
            "Category": prod["Category"].to_numpy(),
            "ProductCategory": prod["ProductCategory"].to_numpy(),
            "SupplierId": prod["SupplierId"].to_numpy(),
            "SupplierName": prod["SupplierName"].to_numpy(),
            "IsCatchWeight": prod["IsCatchWeight"].to_numpy(),
            "YieldPct": prod["YieldPct"].to_numpy(),
            "UnitOfBillingId": prod["UnitOfBillingId"].to_numpy().astype("int64"),
            "ShippingMethodId": np.array([f"SM{i + 1:02d}" for i in method_idx]),
            "ShippingMethodName": np.array([m.name for m in methods]),
            "ShipperName": np.array([m.carrier for m in methods]),
            "Carrier": np.array([m.carrier for m in methods]),
            "QuantityOrdered": cases,
            "QuantityShipped": shipped_cases,
            # Availability. Fill rate and OTIF are computed from these
            # downstream rather than stored, so a filter changes them.
            "BackorderQty": backorder_cases,
            "IsShortShip": is_short,
            "IsStockout": is_stockout,
            # Inventory position the line was picked against.
            "OnHandQty": on_hand_cases,
            "OnHandValue": on_hand_value.round(2),
            "DaysOfSupply": cover_days.round(2),
            "SafetyStockQty": safety_stock_cases,
            "ReorderPointQty": reorder_point_cases,
            "WeightLb": shipped_lb.round(3),
            "ShippedLb": shipped_lb.round(3),
            "pack_count": pack_count.astype("int64"),
            "pack_weight_lb_sum": pack_weight_lb_sum.round(3),
            "pack_item_count_sum": pack_item_count_sum,
            "Price": price.round(4),
            "CostPrice": cost_price.round(4),
            "TransitDays": transit,
            "IsLate": is_late,
        }
    )

    # `Date` is the canonical analysis date; the ETL and the DuckDB view both
    # coalesce to DateExpected, so keep them consistent.
    frame["Date"] = frame["DateExpected"]
    frame["OrderDate"] = frame["DateOrdered"]
    frame["CreatedAt_order"] = frame["DateOrdered"]
    # Watermark column the incremental refresh keys off.
    frame["UpdatedAt"] = frame["DateExpected"]
    frame["DeliveryStatus"] = np.where(frame["IsLate"], "Late", "On Time")
    return frame


def summarise(lines: pd.DataFrame) -> dict[str, float]:
    """
    Recompute the headline numbers the way the DuckDB view does, so the
    generator can report what it actually produced rather than what it intended.
    """
    by_weight = lines["UnitOfBillingId"] == C.BILL_BY_WEIGHT
    revenue = np.where(
        by_weight,
        lines["pack_weight_lb_sum"] * lines["Price"],
        lines["pack_item_count_sum"] * lines["Price"],
    )
    cost = np.where(
        by_weight,
        lines["pack_weight_lb_sum"] * lines["CostPrice"],
        lines["pack_item_count_sum"] * lines["CostPrice"],
    )
    total_rev = float(revenue.sum())
    total_cost = float(cost.sum())
    return {
        "lines": int(len(lines)),
        "orders": int(lines["OrderId"].nunique()),
        "customers": int(lines["CustomerId"].nunique()),
        "products": int(lines["ProductId"].nunique()),
        "revenue": total_rev,
        "cost": total_cost,
        "margin_pct": (total_rev - total_cost) / total_rev if total_rev else 0.0,
        "late_pct": float(lines["IsLate"].mean()),
        "min_date": str(pd.to_datetime(lines["Date"]).min().date()),
        "max_date": str(pd.to_datetime(lines["Date"]).max().date()),
    }


def fiscal_year_start_on_or_before(day: date) -> date:
    """The start of the fiscal year containing `day`."""
    year = day.year
    if (day.month, day.day) < (FISCAL_YEAR_START_MONTH, FISCAL_YEAR_START_DAY):
        year -= 1
    return date(year, FISCAL_YEAR_START_MONTH, FISCAL_YEAR_START_DAY)


def window_for(end: date, *, fiscal_years: int | None, months: int) -> date:
    """
    Where the dataset should start.

    With `fiscal_years`, the window opens on a fiscal year boundary N years
    back, so "prior fiscal year-to-date" has a complete year behind it. That is
    the whole point: the demo previously held ten months of data and reported
    growth against the fortnight that preceded it.
    """
    if fiscal_years and fiscal_years > 0:
        current_fy_start = fiscal_year_start_on_or_before(end)
        return date(
            current_fy_start.year - (fiscal_years - 1),
            FISCAL_YEAR_START_MONTH,
            FISCAL_YEAR_START_DAY,
        )
    return end - timedelta(days=int(months * 30.44))


def line_revenue(lines: pd.DataFrame) -> np.ndarray:
    """Revenue per line, computed the way the DuckDB fact view does."""
    by_weight = lines["UnitOfBillingId"] == C.BILL_BY_WEIGHT
    return np.where(
        by_weight,
        lines["pack_weight_lb_sum"] * lines["Price"],
        lines["pack_item_count_sum"] * lines["Price"],
    )


def line_cost(lines: pd.DataFrame) -> np.ndarray:
    by_weight = lines["UnitOfBillingId"] == C.BILL_BY_WEIGHT
    return np.where(
        by_weight,
        lines["pack_weight_lb_sum"] * lines["CostPrice"],
        lines["pack_item_count_sum"] * lines["CostPrice"],
    )


def report_year_over_year(lines: pd.DataFrame, dims: Dimensions) -> None:
    """
    Print the fiscal-year shape of the generated book.

    This exists because the failure it guards against is invisible in a row
    count: a dataset can have three years of history and still report every
    account growing, which is the tell that gave the previous demo away.
    """
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(lines["Date"]),
            "customer": lines["CustomerId"].to_numpy(),
            "region": lines["RegionName"].to_numpy(),
            "revenue": line_revenue(lines),
            "cost": line_cost(lines),
        }
    )
    fy = frame["date"].dt.year + (frame["date"].dt.month >= FISCAL_YEAR_START_MONTH).astype(int)
    frame["fy"] = fy

    print("\n  fiscal years")
    by_fy = frame.groupby("fy").agg(revenue=("revenue", "sum"), cost=("cost", "sum"))
    for year, row in by_fy.iterrows():
        margin = (row.revenue - row.cost) / row.revenue if row.revenue else 0.0
        print(f"    FY{year}: ${row.revenue:,.0f}  margin {margin * 100:.1f}%")

    years = sorted(by_fy.index.tolist())
    if len(years) < 2:
        return
    prior, current = years[-2], years[-1]

    # Compare on a like-for-like calendar footprint: the current fiscal year is
    # partial, so measuring it against a complete prior year would report a
    # collapse that is an artefact of the window, not the book.
    cutoff = frame.loc[frame["fy"] == current, "date"].max()
    fy_start_current = pd.Timestamp(year=current - 1, month=FISCAL_YEAR_START_MONTH, day=FISCAL_YEAR_START_DAY)
    elapsed = (cutoff - fy_start_current).days
    fy_start_prior = pd.Timestamp(year=prior - 1, month=FISCAL_YEAR_START_MONTH, day=FISCAL_YEAR_START_DAY)
    prior_window = frame[(frame["date"] >= fy_start_prior) & (frame["date"] <= fy_start_prior + pd.Timedelta(days=elapsed))]
    current_window = frame[frame["fy"] == current]

    per_customer = (
        current_window.groupby("customer")["revenue"].sum().rename("current").to_frame()
        .join(prior_window.groupby("customer")["revenue"].sum().rename("prior"), how="outer")
        .fillna(0.0)
    )
    traded_both = per_customer[(per_customer["current"] > 0) & (per_customer["prior"] > 0)]
    gainers = int((traded_both["current"] > traded_both["prior"]).sum())
    decliners = int((traded_both["current"] < traded_both["prior"]).sum())
    new_logos = int(((per_customer["prior"] == 0) & (per_customer["current"] > 0)).sum())
    lapsed = int(((per_customer["current"] == 0) & (per_customer["prior"] > 0)).sum())

    total_current = float(current_window["revenue"].sum())
    total_prior = float(prior_window["revenue"].sum())
    growth = (total_current - total_prior) / total_prior * 100 if total_prior else float("nan")

    print(f"\n  FY{current} to date vs FY{prior} same window")
    print(f"    revenue    : ${total_current:,.0f} vs ${total_prior:,.0f}  ({growth:+.1f}%)")
    print(f"    accounts   : {gainers} up / {decliners} down / {new_logos} new / {lapsed} lapsed")

    by_region = (
        current_window.groupby("region")["revenue"].sum().rename("current").to_frame()
        .join(prior_window.groupby("region")["revenue"].sum().rename("prior"), how="outer")
        .fillna(0.0)
    )
    by_region["delta_pct"] = np.where(
        by_region["prior"] > 0,
        (by_region["current"] - by_region["prior"]) / by_region["prior"] * 100,
        np.nan,
    )
    falling = by_region[by_region["delta_pct"] < 0].sort_values("delta_pct")
    if not falling.empty:
        worst = falling.index[0]
        print(f"    regions dn : {len(falling)} of {len(by_region)} (worst: {worst} {falling.iloc[0]['delta_pct']:+.1f}%)")


def generate(
    *,
    seed: int = DEFAULT_SEED,
    months: int = DEFAULT_MONTHS,
    fiscal_years: int | None = DEFAULT_FISCAL_YEARS,
    customers: int = DEFAULT_CUSTOMERS,
    products: int = DEFAULT_PRODUCTS,
    suppliers: int = DEFAULT_SUPPLIERS,
    end: date | None = None,
) -> tuple[pd.DataFrame, Dimensions]:
    """Build the fact frame and the dimensions it was drawn from."""
    rng = np.random.default_rng(seed)
    # Anchored to a fixed date, not today, so the dataset is reproducible.
    end = end or date(2026, 6, 30)
    start = window_for(end, fiscal_years=fiscal_years, months=months)

    sup = build_suppliers(rng, suppliers)
    prod = build_products(rng, products, sup)
    cust = build_customers(rng, customers, start, end)
    orders = build_orders(rng, cust, start, end)

    # The loss-making supplier is chosen by revenue rank rather than at random,
    # so the planted anomaly lands on an account big enough that a reviewer
    # meets it on the suppliers page instead of having to go looking. That means
    # pricing the book once to rank suppliers, then pricing it again with the
    # anomaly applied. The generator is fast enough that two passes is cheaper
    # than a heuristic that guesses which supplier will be large.
    provisional = build_lines(rng.spawn(1)[0], orders, cust, prod, start, end)
    loss_supplier = _nth_supplier_by_revenue(provisional, LOSS_MAKING_SUPPLIER_RANK)

    lines = build_lines(
        np.random.default_rng(seed + 1),
        orders,
        cust,
        prod,
        start,
        end,
        loss_making_supplier_id=loss_supplier,
    )
    return lines, Dimensions(customers=cust, products=prod, suppliers=sup)


def _nth_supplier_by_revenue(lines: pd.DataFrame, rank: int) -> str | None:
    """The SupplierId at 1-based `rank` when ordered by revenue, descending."""
    by_weight = lines["UnitOfBillingId"] == C.BILL_BY_WEIGHT
    revenue = np.where(
        by_weight,
        lines["pack_weight_lb_sum"] * lines["Price"],
        lines["pack_item_count_sum"] * lines["Price"],
    )
    totals = (
        pd.DataFrame({"SupplierId": lines["SupplierId"].to_numpy(), "revenue": revenue})
        .groupby("SupplierId")["revenue"]
        .sum()
        .sort_values(ascending=False)
    )
    if len(totals) < rank:
        return None
    return str(totals.index[rank - 1])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--months", type=int, default=DEFAULT_MONTHS)
    ap.add_argument(
        "--fiscal-years",
        type=int,
        default=DEFAULT_FISCAL_YEARS,
        help=(
            "Whole fiscal years of history to generate, ending in the current one. "
            "Overrides --months. Pass 0 to use --months instead."
        ),
    )
    ap.add_argument(
        "--end",
        default=None,
        help="Last date in the dataset as YYYY-MM-DD, or 'today'. Default: a fixed anchor.",
    )
    ap.add_argument("--customers", type=int, default=DEFAULT_CUSTOMERS)
    ap.add_argument("--products", type=int, default=DEFAULT_PRODUCTS)
    ap.add_argument("--suppliers", type=int, default=DEFAULT_SUPPLIERS)
    ap.add_argument(
        "--dataset-path",
        default=None,
        help="Target dataset directory (default: cache/fact_dataset)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and summarise without writing the dataset",
    )
    args = ap.parse_args(argv)

    if args.end is None:
        end_date = None
    elif str(args.end).strip().lower() == "today":
        end_date = date.today()
    else:
        end_date = date.fromisoformat(str(args.end).strip())

    lines, dims = generate(
        seed=args.seed,
        months=args.months,
        fiscal_years=args.fiscal_years,
        customers=args.customers,
        products=args.products,
        suppliers=args.suppliers,
        end=end_date,
    )
    stats = summarise(lines)

    print(f"  order lines : {stats['lines']:,}")
    print(f"  orders      : {stats['orders']:,}")
    print(f"  customers   : {stats['customers']:,}")
    print(f"  products    : {stats['products']:,}")
    print(f"  window      : {stats['min_date']} .. {stats['max_date']}")
    print(f"  revenue     : ${stats['revenue']:,.0f}")
    print(f"  gross margin: {stats['margin_pct'] * 100:.1f}%")
    print(f"  late lines  : {stats['late_pct'] * 100:.1f}%")

    # The point of the multi-year window is that year-over-year movement is
    # real and goes both ways. Report it, so a change that flattens the book
    # shows up here rather than on the deployed dashboard.
    report_year_over_year(lines, dims)

    if args.dry_run:
        return 0

    from app.services import watermark_store
    from etl.partition_writer import upsert_dataset

    dataset_path = (
        Path(args.dataset_path).expanduser().resolve()
        if args.dataset_path
        else watermark_store.resolve_dataset_path()
    )
    print(f"\nwriting -> {dataset_path}")

    result = upsert_dataset(
        lines,
        dataset_path=dataset_path,
        pk_col="OrderLineId",
        date_col="DateExpected",
        manifest_updates={
            "source": "seed.generate_synthetic_data",
            "synthetic": True,
            "seed": args.seed,
        },
    )
    print(f"  rows in dataset: {result.get('row_count'):,}")
    print(f"  manifest       : {dataset_path / watermark_store.MANIFEST_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
