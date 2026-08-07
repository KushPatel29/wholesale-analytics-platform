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

# Fraction of the customer book that stops ordering partway through the window.
CHURN_SHARE = 0.12
# Fraction that only appears partway through (new business).
NEW_BUSINESS_SHARE = 0.15

# Beef cost inflation across the whole window, against a much smaller list-price
# increase. This is what makes margin compression visible in the trend charts
# instead of merely asserted in a README.
BEEF_COST_INFLATION = 0.14
LIST_PRICE_INFLATION = 0.05

# A small number of SKUs are sold below landed cost to hold an account.
LOSS_LEADER_SHARE = 0.02
LOSS_LEADER_DISCOUNT = 0.88


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
    group_names = [g.name for g in C.PROTEIN_GROUPS]
    group_idx = _weighted_choice(rng, group_names, [g.weight for g in C.PROTEIN_GROUPS], n)

    rows = []
    for i, gi in enumerate(group_idx):
        group = C.PROTEIN_GROUPS[gi]
        cut = C.CUTS[group.name][rng.integers(len(C.CUTS[group.name]))]
        grade = C.GRADES[rng.integers(len(C.GRADES))]
        prep = C.PREPARATIONS[rng.integers(len(C.PREPARATIONS))]

        # Landed cost varies around the group's typical cost per lb.
        cost_per_lb = float(group.cost_per_lb * rng.lognormal(mean=0.0, sigma=0.22))
        price_per_lb = cost_per_lb * group.markup * float(rng.normal(1.0, 0.06))
        price_per_lb = max(price_per_lb, cost_per_lb * 1.02)

        catch_weight = bool(rng.random() < group.catch_weight_share)
        # Weight-billed lines quote a price per lb; unit-billed lines quote a
        # price per case, so we need a case weight to convert.
        case_weight = float(np.clip(rng.normal(11.0, 4.0), 2.0, 40.0))

        rows.append(
            {
                "ProductId": f"P{10000 + i}",
                "SKU": f"{group.name[:2].upper()}-{1000 + i}",
                "ProductName": f"{cut} {grade} {prep}".replace("  ", " ").strip(),
                "ProteinType": group.name,
                "ProteinName": group.name,
                "Category": group.name,
                "ProductCategory": group.name,
                "IsCatchWeight": catch_weight,
                "UnitOfBillingId": C.BILL_BY_WEIGHT if catch_weight else C.BILL_BY_UNIT,
                "YieldPct": round(float(np.clip(rng.normal(group.yield_pct, 0.05), 0.35, 0.99)), 4),
                "CostPerLb": round(cost_per_lb, 4),
                "PricePerLb": round(price_per_lb, 4),
                "CaseWeightLb": round(case_weight, 3),
            }
        )

    products = pd.DataFrame(rows)

    # A few SKUs are deliberately priced under cost.
    n_loss = max(1, int(len(products) * LOSS_LEADER_SHARE))
    loss_idx = rng.choice(len(products), size=n_loss, replace=False)
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
        rng, C.CUSTOMER_SEGMENTS, [s.weight for s in C.CUSTOMER_SEGMENTS], n
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
    segments = [C.CUSTOMER_SEGMENTS[i] for i in seg_idx]

    # Reps own accounts in the regions they cover; anything uncovered falls to
    # the rep with the largest book, which is how concentration builds up.
    rep_names: list[str] = []
    rep_ids: list[str] = []
    for region in regions:
        eligible = [r for r in C.SALES_REPS if region.name in r.regions]
        if not eligible:
            eligible = [C.SALES_REPS[0]]
        shares = np.array([r.book_share for r in eligible], dtype=float)
        pick = eligible[int(rng.choice(len(eligible), p=shares / shares.sum()))]
        rep_names.append(pick.name)
        rep_ids.append(f"R{C.SALES_REPS.index(pick) + 1:02d}")

    total_days = (end - start).days
    first_order = np.full(n, start, dtype=object)
    last_order = np.full(n, end, dtype=object)

    # New business appears after the window opens.
    n_new = int(n * NEW_BUSINESS_SHARE)
    new_idx = rng.choice(n, size=n_new, replace=False)
    for i in new_idx:
        first_order[i] = start + timedelta(days=int(rng.integers(30, max(31, total_days - 60))))

    # Churn: accounts that go quiet and never come back.
    remaining = np.setdiff1d(np.arange(n), new_idx)
    n_churn = int(n * CHURN_SHARE)
    churn_idx = rng.choice(remaining, size=min(n_churn, len(remaining)), replace=False)
    for i in churn_idx:
        last_order[i] = start + timedelta(days=int(rng.integers(60, max(61, total_days - 30))))

    return pd.DataFrame(
        {
            "CustomerId": [f"C{20000 + i}" for i in range(n)],
            "CustomerName": names,
            "Segment": [s.name for s in segments],
            "IsRetail": [s.is_retail for s in segments],
            "RegionName": [r.name for r in regions],
            "RegionId": [f"RG{C.REGIONS.index(r) + 1:02d}" for r in regions],
            "City": [r.cities[int(rng.integers(len(r.cities)))] for r in regions],
            "Province": [C.PROVINCE_BY_REGION[r.name] for r in regions],
            "SalesRepId": rep_ids,
            "SalesRepName": rep_names,
            "OrdersPerMonth": [s.orders_per_month for s in segments],
            "LinesPerOrder": [s.lines_per_order for s in segments],
            "PriceIndex": [s.price_index for s in segments],
            "SizeIndex": [s.size_index for s in segments],
            "TransitDays": [r.transit_days for r in regions],
            "FirstOrderDate": first_order,
            "LastOrderDate": last_order,
            "IsChurned": [i in set(churn_idx.tolist()) for i in range(n)],
        }
    )


def build_orders(rng: np.random.Generator, customers: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """One row per order, dated inside each customer's own trading window."""
    order_customer: list[int] = []
    order_dates: list[date] = []

    for i, row in enumerate(customers.itertuples(index=False)):
        first = row.FirstOrderDate
        last = row.LastOrderDate
        span_days = (last - first).days
        if span_days <= 0:
            continue
        months = span_days / 30.44
        expected = row.OrdersPerMonth * months
        n_orders = int(rng.poisson(max(expected, 0.5)))
        if n_orders <= 0:
            continue
        offsets = rng.integers(0, span_days, size=n_orders)
        for off in offsets:
            order_customer.append(i)
            order_dates.append(first + timedelta(days=int(off)))

    orders = pd.DataFrame({"cust_idx": order_customer, "DateOrdered": order_dates})
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
    season_lookup = {g.name: np.asarray(g.seasonality) for g in C.PROTEIN_GROUPS}
    season = np.array(
        [season_lookup[p][m] for p, m in zip(prod["ProteinType"].to_numpy(), month_idx)]
    )

    # How far through the window each line sits, for cost/price drift.
    total_days = max((end - start).days, 1)
    progress = ((expected - pd.Timestamp(start)).dt.days.to_numpy() / total_days).clip(0, 1)

    is_beef = (prod["ProteinType"].to_numpy() == "Beef").astype(float)
    cost_drift = 1.0 + progress * BEEF_COST_INFLATION * is_beef
    price_drift = 1.0 + progress * LIST_PRICE_INFLATION

    # Cases ordered, scaled by how big this class of buyer is. A single
    # restaurant line is typically one or two cases; the scale parameter is set
    # so the median line lands in that range rather than at pallet volumes.
    base_cases = rng.gamma(shape=2.0, scale=0.55, size=n_lines)
    cases = np.maximum(1, np.round(base_cases * cust_l["SizeIndex"].to_numpy() * season))

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

    # Packs are the physical boxes picked for the line.
    pack_count = np.maximum(1, np.round(cases * rng.uniform(0.8, 1.2, size=n_lines)))
    pack_weight_lb_sum = shipped_lb
    pack_item_count_sum = cases

    # Delivery performance: region transit + method surcharge, with a
    # method-specific chance of running late.
    method_idx = _weighted_choice(
        rng, C.SHIPPING_METHODS, [m.weight for m in C.SHIPPING_METHODS], n_lines
    )
    methods = [C.SHIPPING_METHODS[i] for i in method_idx]
    extra = np.array([m.extra_days for m in methods])
    late_rate = np.array([m.late_rate for m in methods])
    is_late = rng.random(n_lines) < late_rate
    slip = np.where(is_late, rng.integers(1, 5, size=n_lines), 0)
    transit = cust_l["TransitDays"].to_numpy() + extra + slip
    ship_date = expected - pd.to_timedelta(np.ones(n_lines), unit="D")
    delivery_date = ship_date + pd.to_timedelta(transit, unit="D")

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
            "QuantityShipped": cases,
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


def generate(
    *,
    seed: int = DEFAULT_SEED,
    months: int = DEFAULT_MONTHS,
    customers: int = DEFAULT_CUSTOMERS,
    products: int = DEFAULT_PRODUCTS,
    suppliers: int = DEFAULT_SUPPLIERS,
    end: date | None = None,
) -> tuple[pd.DataFrame, Dimensions]:
    """Build the fact frame and the dimensions it was drawn from."""
    rng = np.random.default_rng(seed)
    # Anchored to a fixed date, not today, so the dataset is reproducible.
    end = end or date(2026, 6, 30)
    start = end - timedelta(days=int(months * 30.44))

    sup = build_suppliers(rng, suppliers)
    prod = build_products(rng, products, sup)
    cust = build_customers(rng, customers, start, end)
    orders = build_orders(rng, cust, start, end)
    lines = build_lines(rng, orders, cust, prod, start, end)
    return lines, Dimensions(customers=cust, products=prod, suppliers=sup)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--months", type=int, default=DEFAULT_MONTHS)
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

    lines, dims = generate(
        seed=args.seed,
        months=args.months,
        customers=args.customers,
        products=args.products,
        suppliers=args.suppliers,
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
