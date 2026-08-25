"""
Seed the operational workspaces from real rows in the fact dataset.

The decision-ops layer ships a complete CRM/ERP surface - nine workspaces, a
governed decision ledger, per-domain lifecycles, approvals, line items, event
timelines - and the deployed demo rendered every one of them as a row of zeros.
The tables were created by `manage.py init-auth-db` and then nothing on earth
ever wrote to them. A pipeline tile reading $0 over an empty stage board does
not demonstrate a CRM; it demonstrates an unfinished one.

Every record here is built against something real: a real customer, the SKUs
they buy, the supplier who ships them, the region they sit in. That matters
more than it sounds. An opportunity against a customer that exists nowhere else
means the drill-through dead-ends, the pipeline bears no relation to the revenue
on the Customers page, and anyone who cross-checks a number finds one that
cannot be reconciled.

Three things this seeder is shaped around, all of them learned the hard way from
reading how the summaries are actually computed:

1.  **The tiles read specific columns, not row counts.** `commit_value` needs
    `forecast_category == "commit"` (the column, not the status of the same
    name); `new_logo_pipeline` needs `metadata_json["motion"]`; `csat` needs a
    numeric `metadata_json["csat"]`; `perfect_order_rate` needs four JSON
    booleans and divides by *closed* orders only. Creating rows without setting
    these leaves a page that is technically populated and still reads zero.

2.  **The CRM stage board is paginated and the summary is not.** The board
    iterates `listing['items']` - page 1, 25 rows, ordered `updated_at DESC`.
    Seed opportunities into the tail of that ordering and you get a seven-figure
    pipeline above seven columns all saying "No opportunities". Opportunities
    therefore carry the newest timestamps in the domain, at least two per stage.

3.  **Two `exceptions` tiles are structurally unreachable and are left at zero.**
    `exceptions` counts statuses `exception|held|escalated`; neither the `crm`
    nor the `master-data` status vocabulary contains any of them. Forcing one in
    would produce a record whose own status is not in its lifecycle - the detail
    page could not render its state and no transition would accept it. An honest
    zero beats a number that breaks the workflow behind it.

Dates are anchored to the moment the seed runs, because that is the clock the
overdue and SLA predicates compare against. In the container that is image build
time, so a long-lived image drifts toward everything being overdue; rebuilding
re-anchors it.

Usage:
    python -m seed.generate_synthetic_operations
    python -m seed.generate_synthetic_operations --replace --seed 11
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_SEED = 20260825

# Money is USD across this dataset (seed/catalog.py). Both models default to CAD
# and the templates print the column verbatim, so every row sets it explicitly.
CURRENCY = "USD"

# Stamped on every seeded row. The unique index is
# (domain, record_type, source_system, source_record_id), and it is also how
# --replace finds what it owns without touching anything a reviewer created.
SOURCE_SYSTEM = "northgate_seed"

# Routes that actually exist, for the drill-through banner on record detail.
DRILL = {
    "customers": "/customers/",
    "products": "/products/",
    "suppliers": "/suppliers/",
    "inventory": "/inventory/",
    "overview": "/overview/",
    "regions": "/regions/",
    "planning": "/planning/#planningScenarios",
    "returns": "/returns",
    "salesreps": "/salesreps/",
}

# The create-action form offers exactly these; the filter matches on equality.
SOURCE_MODULES = ("overview", "products", "inventory", "customers", "suppliers", "salesreps", "planning", "returns")


def _now() -> datetime:
    """Naive UTC - what SQLite returns and what every predicate compares to."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Ref:
    """Real customers, products and suppliers drawn from the fact view."""

    def __init__(self, customers: list[dict], products: list[dict], suppliers: list[dict], regions: list[dict]):
        self.customers = customers
        self.products = products
        self.suppliers = suppliers
        self.regions = regions


def load_reference_data(limit_customers: int = 90, limit_products: int = 160) -> Ref:
    """Read the dimensions off the registered `fact` view.

    The view, not the raw parquet: weight/unit billing is resolved there, which
    is the same reason seed/generate_synthetic_returns.py goes through it.
    """
    from app.services import fact_store

    customers = fact_store.execute_sql_df(
        """
        SELECT CustomerId, ANY_VALUE(CustomerName) AS CustomerName,
               ANY_VALUE(CustomerSegment) AS Segment, ANY_VALUE(City) AS City,
               ANY_VALUE(Province) AS Province, ANY_VALUE(RegionName) AS RegionName,
               SUM(COALESCE(Revenue, 0)) AS Revenue
        FROM fact GROUP BY CustomerId
        HAVING SUM(COALESCE(Revenue, 0)) > 0
        ORDER BY Revenue DESC LIMIT ?
        """,
        [int(limit_customers)],
        tag="seed.operations.customers",
    ).to_dict("records")

    products = fact_store.execute_sql_df(
        """
        SELECT SKU, ANY_VALUE(ProductName) AS ProductName, ANY_VALUE(SkuName) AS SkuName,
               ANY_VALUE(ListPrice) AS ListPrice, ANY_VALUE(CostPrice) AS CostPrice,
               ANY_VALUE(SupplierName) AS SupplierName
        FROM fact WHERE SKU IS NOT NULL GROUP BY SKU
        ORDER BY SUM(COALESCE(Revenue, 0)) DESC LIMIT ?
        """,
        [int(limit_products)],
        tag="seed.operations.products",
    ).to_dict("records")

    suppliers = fact_store.execute_sql_df(
        """
        SELECT SupplierName, ANY_VALUE(SupplierId) AS SupplierId,
               SUM(COALESCE(Revenue, 0)) AS Revenue
        FROM fact WHERE SupplierName IS NOT NULL GROUP BY SupplierName
        ORDER BY Revenue DESC LIMIT 24
        """,
        tag="seed.operations.suppliers",
    ).to_dict("records")

    regions = fact_store.execute_sql_df(
        "SELECT DISTINCT RegionName, City FROM fact WHERE RegionName IS NOT NULL LIMIT 40",
        tag="seed.operations.regions",
    ).to_dict("records")

    if not customers or not products or not suppliers:
        raise SystemExit(
            "fact dataset returned no customers/products/suppliers - run "
            "`python -m seed.generate_synthetic_data` before seeding operations."
        )
    return Ref(customers, products, suppliers, regions)


def _owners(usernames: tuple[str, ...]) -> list[int]:
    """Resolve demo usernames to ids.

    Never hard-code ids: `seed-demo-users` inserts in declaration order, so a
    fresh container numbers them differently from a developer box.
    """
    from app.auth.models import get_user_by_username

    ids: list[int] = []
    for name in usernames:
        user = get_user_by_username(name)
        if user is not None:
            ids.append(int(user.id))
    return ids or [1]


class Builder:
    """Accumulates records, lines, events and approvals, then writes them once."""

    def __init__(self, rng: np.random.Generator, ref: Ref, owners: dict[str, list[int]]):
        self.rng = rng
        self.ref = ref
        self.owners = owners
        self.records: list[dict] = []
        self.lines: list[tuple[str, list[dict]]] = []
        self.events: list[tuple[str, list[dict]]] = []
        self.approvals: list[tuple[str, dict]] = []

    def pick(self, seq: list) -> Any:
        return seq[int(self.rng.integers(0, len(seq)))]

    def owner(self, domain: str) -> int:
        pool = self.owners.get(domain) or self.owners["default"]
        return int(pool[int(self.rng.integers(0, len(pool)))])

    def add(
        self,
        *,
        domain: str,
        record_type: str,
        record_number: str,
        title: str,
        status: str,
        created_ago_days: float,
        updated_ago_days: float | None = None,
        description: str | None = None,
        approval_status: str = "not_required",
        priority: str = "medium",
        amount: float | None = None,
        quantity: float | None = None,
        fulfilled_quantity: float | None = None,
        probability_pct: float | None = None,
        stage: str | None = None,
        forecast_category: str | None = None,
        next_step: str | None = None,
        due_in_days: float | None = None,
        close_in_days: float | None = None,
        service_started_ago_days: float | None = None,
        service_due_in_days: float | None = None,
        account_ref: str | None = None,
        contact_ref: str | None = None,
        product_ref: str | None = None,
        supplier_ref: str | None = None,
        location_ref: str | None = None,
        source_module: str | None = None,
        metadata: dict | None = None,
        lines: list[dict] | None = None,
        history: list[tuple[str, str]] | None = None,
    ) -> str:
        now = _now()
        created = now - timedelta(days=float(created_ago_days))
        updated = now - timedelta(days=float(updated_ago_days if updated_ago_days is not None else created_ago_days / 3))
        self.records.append(
            {
                "domain": domain,
                "record_type": record_type,
                "record_number": record_number,
                "title": title,
                "description": description,
                "status": status,
                "approval_status": approval_status,
                "priority": priority,
                "owner_user_id": self.owner(domain),
                "account_ref": account_ref,
                "contact_ref": contact_ref,
                "product_ref": product_ref,
                "supplier_ref": supplier_ref,
                "location_ref": location_ref,
                "amount": amount,
                "currency": CURRENCY,
                "quantity": quantity,
                "fulfilled_quantity": fulfilled_quantity,
                "probability_pct": probability_pct,
                "stage": stage,
                "forecast_category": forecast_category,
                "next_step": next_step,
                "due_at": (now + timedelta(days=float(due_in_days))) if due_in_days is not None else None,
                "close_at": (now + timedelta(days=float(close_in_days))) if close_in_days is not None else None,
                "service_started_at": (now - timedelta(days=float(service_started_ago_days)))
                if service_started_ago_days is not None
                else None,
                "service_due_at": (now + timedelta(days=float(service_due_in_days)))
                if service_due_in_days is not None
                else None,
                "source_system": SOURCE_SYSTEM,
                "source_record_id": record_number,
                "source_url": DRILL.get(source_module or "overview", "/overview/"),
                "metadata_json": json.dumps(metadata or {}),
                "created_at": created,
                "updated_at": updated,
            }
        )
        if lines:
            self.lines.append((record_number, lines))

        # Every record gets a timeline: a creation event plus one hop per
        # lifecycle transition. record_detail.html prints "N events" and an
        # empty panel otherwise, which reads as a broken page.
        chain: list[dict] = [
            {
                "event_type": "created",
                "from_status": None,
                "to_status": history[0][0] if history else status,
                "created_at": created,
            }
        ]
        if history:
            span = max((updated - created).total_seconds(), 3600.0)
            for index, (from_status, to_status) in enumerate(history, start=1):
                chain.append(
                    {
                        "event_type": "status_changed",
                        "from_status": from_status,
                        "to_status": to_status,
                        "created_at": created + timedelta(seconds=span * index / (len(history) + 1)),
                    }
                )
        self.events.append((record_number, chain))

        if status == "pending_approval" or approval_status == "pending":
            self.approvals.append(
                (
                    record_number,
                    {
                        "route": f"{domain}.review",
                        "status": "pending",
                        "notes": "Awaiting reviewer decision.",
                        "requested_at": updated,
                    },
                )
            )
        return record_number


# --------------------------------------------------------------------------
# CRM - the stage board is page-limited, so opportunities carry the newest
# timestamps and cover all seven stages at least twice.
# --------------------------------------------------------------------------

CRM_STAGES = ("prospecting", "discovery", "proposal", "negotiation", "commit", "won", "lost")
STAGE_PROBABILITY = {"prospecting": 10.0, "discovery": 25.0, "proposal": 45.0, "negotiation": 65.0, "commit": 85.0}
NEXT_STEPS = (
    "Confirm category review date with the buyer",
    "Send the assortment proposal for sign-off",
    "Schedule the quarterly business review",
    "Agree promotional calendar for the next period",
    "Collect signed pricing agreement",
    "Walk the planogram with the regional lead",
)


def build_crm(b: Builder) -> None:
    seq = 1040
    # Two to three opportunities per stage, newest first so the board fills.
    for index, stage in enumerate(CRM_STAGES):
        for slot in range(3 if stage in ("discovery", "proposal", "negotiation") else 2):
            cust = b.pick(b.ref.customers)
            seq += 1
            closed = stage in ("won", "lost")
            amount = float(round(b.rng.uniform(38_000, 480_000), -2))
            motion = "expansion" if b.rng.random() < 0.55 else "new_logo"
            # ~80% of open opportunities carry a next step; the rest are the
            # "stalled" tile, which should be a believable minority.
            has_next = closed or b.rng.random() < 0.8
            age = 4 + index * 3 + slot
            b.add(
                domain="crm",
                record_type="opportunity",
                record_number=f"OPP-{seq}",
                title=f"{cust['CustomerName']} — {'annual renewal' if motion == 'expansion' else 'new banner rollout'}",
                description=f"{cust['Segment']} account in {cust['City']}, {cust['Province']}.",
                status=stage,
                stage=stage,
                amount=amount,
                probability_pct=(100.0 if stage == "won" else 0.0 if stage == "lost" else STAGE_PROBABILITY[stage]),
                forecast_category=("commit" if stage in ("commit", "negotiation") and b.rng.random() < 0.7 else "pipeline"),
                next_step=(b.pick(list(NEXT_STEPS)) if has_next else None),
                close_in_days=(-age if closed else float(b.rng.integers(8, 95))),
                account_ref=str(cust["CustomerId"]),
                contact_ref=f"{cust['CustomerName'].split()[0].lower()}.buyer@northgate.example",
                source_module="customers",
                metadata={"motion": motion, "region": cust["RegionName"]},
                created_ago_days=age + float(b.rng.integers(20, 120)),
                updated_ago_days=age * 0.12,
                history=[("prospecting", stage)] if stage != "prospecting" else None,
                priority=("high" if amount > 300_000 else "medium"),
            )

    # Supporting CRM records. Kept slightly older than the opportunities so they
    # do not push the stage board off page 1.
    for offset in range(6):
        cust = b.pick(b.ref.customers)
        seq += 1
        b.add(
            domain="crm",
            record_type="account",
            record_number=f"ACC-{seq}",
            title=f"{cust['CustomerName']} account plan",
            status="active",
            account_ref=str(cust["CustomerId"]),
            source_module="customers",
            metadata={"segment": cust["Segment"], "region": cust["RegionName"]},
            created_ago_days=180 + offset * 11,
            updated_ago_days=9 + offset,
        )
    for offset in range(5):
        cust = b.pick(b.ref.customers)
        seq += 1
        amount = float(round(b.rng.uniform(12_000, 90_000), -2))
        b.add(
            domain="crm",
            record_type="quote",
            record_number=f"QTE-{seq}",
            title=f"Quote for {cust['CustomerName']}",
            status="proposal",
            amount=amount,
            account_ref=str(cust["CustomerId"]),
            source_module="customers",
            created_ago_days=30 + offset * 6,
            updated_ago_days=11 + offset,
        )
    for offset in range(4):
        cust = b.pick(b.ref.customers)
        seq += 1
        b.add(
            domain="crm",
            record_type="renewal",
            record_number=f"RNW-{seq}",
            title=f"{cust['CustomerName']} supply agreement renewal",
            status="active",
            amount=float(round(b.rng.uniform(60_000, 260_000), -2)),
            account_ref=str(cust["CustomerId"]),
            next_step="Confirm renewal terms with category management",
            close_in_days=float(b.rng.integers(20, 140)),
            source_module="customers",
            created_ago_days=140 + offset * 9,
            updated_ago_days=13 + offset,
        )


# --------------------------------------------------------------------------
# Orders - backlog, holds, partial shipments, backorders, and the four
# perfect-order flags on closed orders only.
# --------------------------------------------------------------------------

ORDER_FLOW = ("draft", "credit_check", "approved", "picking", "packed", "partially_shipped", "shipped", "invoiced", "paid", "closed")


def build_orders(b: Builder) -> None:
    seq = 20400
    plan = (
        [("closed", 8), ("paid", 4), ("invoiced", 3), ("shipped", 4)]
        + [("partially_shipped", 4), ("picking", 3), ("packed", 2)]
        + [("approved", 3), ("credit_check", 2), ("held", 3), ("draft", 2)]
    )
    for status, count in plan:
        for _ in range(count):
            cust = b.pick(b.ref.customers)
            seq += 1
            line_count = int(b.rng.integers(2, 6))
            lines = []
            total = 0.0
            ordered_total = 0.0
            filled_total = 0.0
            for line_no in range(1, line_count + 1):
                prod = b.pick(b.ref.products)
                qty = float(int(b.rng.integers(12, 240)))
                price = float(prod.get("ListPrice") or b.rng.uniform(4, 90))
                if status == "partially_shipped":
                    filled = float(int(qty * float(b.rng.uniform(0.25, 0.8))))
                elif status in ("draft", "credit_check", "approved", "held"):
                    filled = 0.0
                elif status in ("picking", "packed"):
                    filled = float(int(qty * float(b.rng.uniform(0.0, 0.5))))
                else:
                    filled = qty
                amount = round(qty * price, 2)
                total += amount
                ordered_total += qty
                filled_total += filled
                lines.append(
                    {
                        "line_number": line_no,
                        "item_ref": str(prod["SKU"]),
                        "description": str(prod.get("SkuName") or prod.get("ProductName") or prod["SKU"]),
                        "quantity": qty,
                        "fulfilled_quantity": filled,
                        "unit_price": round(price, 2),
                        "amount": amount,
                        "metadata_json": "{}",
                    }
                )
            age = float(b.rng.integers(2, 70))
            # The four flags are what perfect_order_rate divides by closed
            # orders. Put them on closed orders only or the rate exceeds 100%.
            meta: dict[str, Any] = {"channel": b.pick(["DC", "DSD", "cross_dock"])}
            if status == "closed":
                perfect = b.rng.random() < 0.82
                meta.update(
                    {
                        "on_time": bool(perfect or b.rng.random() < 0.5),
                        "complete": bool(perfect),
                        "damage_free": bool(perfect or b.rng.random() < 0.7),
                        "invoice_accurate": bool(perfect or b.rng.random() < 0.6),
                    }
                )
            if status == "held":
                meta["hold_reason"] = b.pick(["credit_limit_exceeded", "stock_shortfall", "pricing_review"])
            flow = list(ORDER_FLOW)
            history = None
            if status in flow:
                cut = flow.index(status)
                if cut > 0:
                    history = [(flow[i], flow[i + 1]) for i in range(max(0, cut - 2), cut)]
            b.add(
                domain="orders",
                record_type="sales_order",
                record_number=f"SO-{seq}",
                title=f"Order {seq} — {cust['CustomerName']}",
                description=f"{line_count} lines to {cust['City']}, {cust['Province']}.",
                status=status,
                amount=round(total, 2),
                quantity=ordered_total,
                fulfilled_quantity=filled_total,
                account_ref=str(cust["CustomerId"]),
                location_ref=f"{cust['City']}, {cust['Province']}",
                source_module="customers",
                metadata=meta,
                lines=lines,
                created_ago_days=age,
                updated_ago_days=age * 0.2,
                priority=("high" if status == "held" else "medium"),
                history=history,
            )

    for offset in range(3):
        cust = b.pick(b.ref.customers)
        seq += 1
        b.add(
            domain="orders",
            record_type="hold",
            record_number=f"HLD-{seq}",
            title=f"Credit hold — {cust['CustomerName']}",
            status="held",
            amount=float(round(b.rng.uniform(9_000, 60_000), -2)),
            account_ref=str(cust["CustomerId"]),
            source_module="customers",
            metadata={"hold_reason": "credit_limit_exceeded"},
            created_ago_days=6 + offset * 3,
            updated_ago_days=1 + offset,
            priority="high",
        )


# --------------------------------------------------------------------------
# Procurement - open commitments, three-way-match exceptions, PPV and GRNI.
# --------------------------------------------------------------------------

def build_procurement(b: Builder) -> None:
    seq = 3300
    for status, count in (("ordered", 7), ("partially_received", 4), ("received", 3), ("approved", 3), ("pending_approval", 4), ("draft_po", 3), ("closed", 3)):
        for _ in range(count):
            sup = b.pick(b.ref.suppliers)
            prod = b.pick(b.ref.products)
            seq += 1
            qty = float(int(b.rng.integers(200, 2400)))
            unit = float(prod.get("CostPrice") or b.rng.uniform(2, 40))
            total = round(qty * unit, 2)
            received = qty if status in ("received", "closed") else (float(int(qty * 0.55)) if status == "partially_received" else 0.0)
            b.add(
                domain="procurement",
                record_type="purchase_order",
                record_number=f"PO-{seq}",
                title=f"PO {seq} — {sup['SupplierName']}",
                description=f"Replenishment of {prod.get('SkuName') or prod['SKU']}.",
                status=status,
                approval_status=("pending" if status == "pending_approval" else "not_required"),
                amount=total,
                quantity=qty,
                fulfilled_quantity=received,
                supplier_ref=str(sup["SupplierName"]),
                product_ref=str(prod["SKU"]),
                source_module="suppliers",
                metadata={"incoterm": b.pick(["DDP", "FOB", "EXW"]), "lead_time_days": int(b.rng.integers(7, 45))},
                lines=[
                    {
                        "line_number": 1,
                        "item_ref": str(prod["SKU"]),
                        "description": str(prod.get("SkuName") or prod["SKU"]),
                        "quantity": qty,
                        "fulfilled_quantity": received,
                        "unit_price": round(unit, 2),
                        "amount": total,
                        "metadata_json": "{}",
                    }
                ],
                created_ago_days=float(b.rng.integers(5, 90)),
                updated_ago_days=float(b.rng.integers(0, 6)),
                history=[("requisition", "draft_po"), ("draft_po", status)] if status != "draft_po" else None,
            )

    # Three-way match exceptions: these are the `match_exceptions` tile and also
    # the domain's `exceptions` tile.
    for offset in range(5):
        sup = b.pick(b.ref.suppliers)
        seq += 1
        b.add(
            domain="procurement",
            record_type="invoice_match",
            record_number=f"MTC-{seq}",
            title=f"Three-way match exception — {sup['SupplierName']}",
            description="Invoice quantity exceeds the receipted quantity.",
            status="exception",
            amount=float(round(b.rng.uniform(1_200, 24_000), 2)),
            supplier_ref=str(sup["SupplierName"]),
            source_module="suppliers",
            metadata={"variance_type": b.pick(["quantity", "price", "tax"])},
            created_ago_days=3 + offset * 4,
            updated_ago_days=float(offset),
            priority="high",
        )
    for offset in range(3):
        sup = b.pick(b.ref.suppliers)
        seq += 1
        b.add(
            domain="procurement",
            record_type="invoice_match",
            record_number=f"MTC-{seq}",
            title=f"Matched invoice — {sup['SupplierName']}",
            status="matched",
            amount=float(round(b.rng.uniform(4_000, 40_000), 2)),
            supplier_ref=str(sup["SupplierName"]),
            source_module="suppliers",
            created_ago_days=12 + offset * 5,
            updated_ago_days=3 + offset,
        )

    # PPV is summed regardless of status and is signed: favourable variances are
    # negative. A demo showing only unfavourable variance reads as fabricated.
    for offset in range(6):
        sup = b.pick(b.ref.suppliers)
        prod = b.pick(b.ref.products)
        seq += 1
        variance = float(round(b.rng.uniform(-9_000, 16_000), 2))
        b.add(
            domain="procurement",
            record_type="purchase_price_variance",
            record_number=f"PPV-{seq}",
            title=f"Price variance — {prod.get('SkuName') or prod['SKU']}",
            description=f"{'Unfavourable' if variance > 0 else 'Favourable'} variance against contract price.",
            status=("exception" if variance > 12_000 else "matched"),
            amount=variance,
            supplier_ref=str(sup["SupplierName"]),
            product_ref=str(prod["SKU"]),
            source_module="suppliers",
            metadata={"contract_price": round(float(prod.get("CostPrice") or 10.0), 4)},
            created_ago_days=float(b.rng.integers(6, 60)),
            updated_ago_days=float(b.rng.integers(0, 8)),
        )

    for offset in range(5):
        sup = b.pick(b.ref.suppliers)
        seq += 1
        b.add(
            domain="procurement",
            record_type="grni",
            record_number=f"GRNI-{seq}",
            title=f"Goods received not invoiced — {sup['SupplierName']}",
            status=b.pick(["received", "partially_received"]),
            amount=float(round(b.rng.uniform(3_000, 52_000), 2)),
            supplier_ref=str(sup["SupplierName"]),
            source_module="suppliers",
            metadata={"aged_days": int(b.rng.integers(5, 70))},
            created_ago_days=float(b.rng.integers(4, 55)),
            updated_ago_days=float(b.rng.integers(0, 5)),
        )

    for offset in range(4):
        sup = b.pick(b.ref.suppliers)
        seq += 1
        b.add(
            domain="procurement",
            record_type="commitment",
            record_number=f"CMT-{seq}",
            title=f"Annual commitment — {sup['SupplierName']}",
            status="approved",
            amount=float(round(b.rng.uniform(120_000, 900_000), -3)),
            supplier_ref=str(sup["SupplierName"]),
            source_module="suppliers",
            created_ago_days=200 + offset * 12,
            updated_ago_days=20 + offset,
        )


# --------------------------------------------------------------------------
# Finance - close tasks, journals, reconciliations, 13-week cash, budget.
# --------------------------------------------------------------------------

CLOSE_TASKS = (
    "Accrue freight and logistics costs",
    "Reconcile inventory sub-ledger to GL",
    "Post payroll allocation journal",
    "Review supplier rebate accruals",
    "Revalue foreign currency payables",
    "Confirm intercompany balances",
    "Depreciation run and fixed-asset roll-forward",
    "Sign off revenue cut-off testing",
)


def build_finance(b: Builder) -> None:
    seq = 8800
    for index, task in enumerate(CLOSE_TASKS):
        seq += 1
        status = ("closed" if index < 2 else "pending_approval" if index in (2, 3) else "approved" if index == 4 else "draft")
        b.add(
            domain="finance",
            record_type="close_task",
            record_number=f"CT-{seq}",
            title=task,
            description="Month-end close checklist item.",
            status=status,
            approval_status=("pending" if status == "pending_approval" else "not_required"),
            due_in_days=float(index - 3),
            source_module="overview",
            metadata={"close_period": "current_month"},
            created_ago_days=12 + index,
            updated_ago_days=float(index) * 0.5,
            priority=("high" if index < 3 else "medium"),
        )

    for offset in range(9):
        seq += 1
        status = "posted" if offset < 6 else b.pick(["draft", "pending_approval"])
        b.add(
            domain="finance",
            record_type="journal_entry",
            record_number=f"JE-{seq}",
            title=f"Journal {seq} — {b.pick(['accrual', 'reclass', 'allocation', 'revaluation'])}",
            status=status,
            approval_status=("pending" if status == "pending_approval" else "not_required"),
            amount=float(round(b.rng.uniform(4_000, 180_000), 2)),
            source_module="overview",
            created_ago_days=float(b.rng.integers(3, 40)),
            updated_ago_days=float(b.rng.integers(0, 4)),
            history=[("draft", "posted")] if status == "posted" else None,
        )

    for offset in range(4):
        seq += 1
        status = "reconciled" if offset < 2 else ("exception" if offset == 2 else "draft")
        b.add(
            domain="finance",
            record_type="bank_reconciliation",
            record_number=f"BR-{seq}",
            title=f"Bank reconciliation — {b.pick(['operating', 'payroll', 'merchant settlement'])} account",
            status=status,
            amount=float(round(b.rng.uniform(-40_000, 120_000), 2)),
            source_module="overview",
            metadata={"unmatched_items": int(b.rng.integers(0, 14))},
            created_ago_days=8 + offset * 4,
            updated_ago_days=float(offset),
            priority=("high" if status == "exception" else "medium"),
        )

    # Thirteen weekly rows: the "13-week cash" tile is their sum.
    for week in range(13):
        seq += 1
        b.add(
            domain="finance",
            record_type="cash_forecast",
            record_number=f"CF-{seq}",
            title=f"Week {week + 1} net cash forecast",
            status="approved",
            amount=float(round(b.rng.uniform(180_000, 940_000), -2)),
            source_module="overview",
            metadata={"week_offset": week},
            created_ago_days=10,
            updated_ago_days=float(week) * 0.2,
        )

    for dept in ("Fresh", "Consumables", "General Merchandise", "Distribution", "Store Operations"):
        seq += 1
        b.add(
            domain="finance",
            record_type="budget",
            record_number=f"BUD-{seq}",
            title=f"{dept} operating budget",
            status="approved",
            amount=float(round(b.rng.uniform(700_000, 4_200_000), -3)),
            source_module="overview",
            metadata={"cost_center": dept},
            created_ago_days=150,
            updated_ago_days=float(b.rng.integers(5, 30)),
        )

    seq += 1
    b.add(
        domain="finance",
        record_type="ap_transaction",
        record_number=f"AP-{seq}",
        title="Aged payables review — over 60 days",
        status="exception",
        amount=float(round(b.rng.uniform(20_000, 90_000), 2)),
        source_module="suppliers",
        created_ago_days=18,
        updated_ago_days=2,
        priority="high",
    )


# --------------------------------------------------------------------------
# Inventory - reorder proposals and pending adjustments.
# --------------------------------------------------------------------------

def build_inventory(b: Builder) -> None:
    seq = 550
    for status, count in (("proposed", 7), ("pending_approval", 4), ("approved", 3), ("in_progress", 2), ("completed", 3)):
        for _ in range(count):
            prod = b.pick(b.ref.products)
            sup = b.pick(b.ref.suppliers)
            seq += 1
            qty = float(int(b.rng.integers(120, 1800)))
            unit = float(prod.get("CostPrice") or b.rng.uniform(2, 40))
            b.add(
                domain="inventory",
                record_type="reorder_proposal",
                record_number=f"RP-{seq}",
                title=f"Replenish {prod.get('SkuName') or prod['SKU']}",
                description="Projected cover below the safety-stock threshold.",
                status=status,
                approval_status=("pending" if status == "pending_approval" else "not_required"),
                amount=round(qty * unit, 2),
                quantity=qty,
                fulfilled_quantity=(qty if status == "completed" else 0.0),
                product_ref=str(prod["SKU"]),
                supplier_ref=str(sup["SupplierName"]),
                location_ref=b.pick(["DC-Central", "DC-West", "DC-South"]),
                source_module="inventory",
                metadata={"days_of_supply": int(b.rng.integers(2, 16)), "reorder_point": int(qty * 0.4)},
                lines=[
                    {
                        "line_number": 1,
                        "item_ref": str(prod["SKU"]),
                        "description": str(prod.get("SkuName") or prod["SKU"]),
                        "quantity": qty,
                        "fulfilled_quantity": (qty if status == "completed" else 0.0),
                        "unit_price": round(unit, 2),
                        "amount": round(qty * unit, 2),
                        "metadata_json": "{}",
                    }
                ],
                created_ago_days=float(b.rng.integers(1, 30)),
                updated_ago_days=float(b.rng.integers(0, 3)),
            )

    for offset in range(5):
        prod = b.pick(b.ref.products)
        seq += 1
        b.add(
            domain="inventory",
            record_type="adjustment",
            record_number=f"ADJ-{seq}",
            title=f"Stock adjustment — {prod.get('SkuName') or prod['SKU']}",
            description="Cycle count variance awaiting approval.",
            status="pending_approval",
            approval_status="pending",
            amount=float(round(b.rng.uniform(-8_000, 12_000), 2)),
            quantity=float(int(b.rng.integers(-90, 140))),
            product_ref=str(prod["SKU"]),
            location_ref=b.pick(["DC-Central", "DC-West", "DC-South"]),
            source_module="inventory",
            metadata={"count_type": "cycle"},
            created_ago_days=float(b.rng.integers(1, 14)),
            updated_ago_days=float(b.rng.integers(0, 2)),
        )

    for offset in range(3):
        prod = b.pick(b.ref.products)
        seq += 1
        b.add(
            domain="inventory",
            record_type="cycle_count",
            record_number=f"CC-{seq}",
            title=f"Cycle count — {b.pick(['DC-Central', 'DC-West', 'DC-South'])} zone {offset + 1}",
            status=b.pick(["in_progress", "completed"]),
            quantity=float(int(b.rng.integers(200, 900))),
            product_ref=str(prod["SKU"]),
            source_module="inventory",
            created_ago_days=float(b.rng.integers(2, 20)),
            updated_ago_days=float(b.rng.integers(0, 3)),
        )

    seq += 1
    b.add(
        domain="inventory",
        record_type="movement",
        record_number=f"MOV-{seq}",
        title="Blocked transfer — temperature excursion",
        status="exception",
        quantity=float(int(b.rng.integers(60, 400))),
        location_ref="DC-West",
        source_module="inventory",
        metadata={"excursion_minutes": int(b.rng.integers(20, 180))},
        created_ago_days=3,
        updated_ago_days=0.5,
        priority="critical",
    )


# --------------------------------------------------------------------------
# Master data - governed change requests. `exceptions` is unreachable here by
# design, so total and pending_approval carry the page.
# --------------------------------------------------------------------------

def build_master_data(b: Builder) -> None:
    seq = 220
    plan = (("pending_approval", 6), ("applied", 6), ("approved", 3), ("draft", 3), ("rejected", 2))
    for status, count in plan:
        for _ in range(count):
            seq += 1
            kind = b.pick(["product", "supplier", "customer", "price_list", "duplicate_review"])
            if kind == "product":
                prod = b.pick(b.ref.products)
                title = f"New item setup — {prod.get('SkuName') or prod['SKU']}"
                before, after = {"status": "not_listed"}, {"status": "listed", "sku": str(prod["SKU"])}
                refs = {"product_ref": str(prod["SKU"]), "source_module": "products"}
            elif kind == "supplier":
                sup = b.pick(b.ref.suppliers)
                title = f"Vendor record change — {sup['SupplierName']}"
                before, after = {"payment_terms": "NET30"}, {"payment_terms": "NET45"}
                refs = {"supplier_ref": str(sup["SupplierName"]), "source_module": "suppliers"}
            elif kind == "customer":
                cust = b.pick(b.ref.customers)
                title = f"Customer hierarchy change — {cust['CustomerName']}"
                before, after = {"parent": None}, {"parent": cust["RegionName"]}
                refs = {"account_ref": str(cust["CustomerId"]), "source_module": "customers"}
            elif kind == "price_list":
                prod = b.pick(b.ref.products)
                old = round(float(prod.get("ListPrice") or 10.0), 2)
                title = f"Price list update — {prod.get('SkuName') or prod['SKU']}"
                before, after = {"list_price": old}, {"list_price": round(old * 1.04, 2)}
                refs = {"product_ref": str(prod["SKU"]), "source_module": "products"}
            else:
                cust = b.pick(b.ref.customers)
                title = f"Duplicate review — {cust['CustomerName']}"
                before, after = {"records": 2}, {"records": 1}
                refs = {"account_ref": str(cust["CustomerId"]), "source_module": "customers"}

            b.add(
                domain="master-data",
                record_type=kind,
                record_number=f"MDC-{seq}",
                title=title,
                description="Governed master-data change with before and after values.",
                status=status,
                approval_status=("pending" if status == "pending_approval" else "approved" if status in ("approved", "applied") else "not_required"),
                metadata={"before": before, "after": after, "steward": "data.governance"},
                created_ago_days=float(b.rng.integers(2, 80)),
                updated_ago_days=float(b.rng.integers(0, 6)),
                history=[("draft", status)] if status != "draft" else None,
                **refs,
            )


# --------------------------------------------------------------------------
# Service - open cases, SLA breaches, reopened, and a numeric CSAT.
# --------------------------------------------------------------------------

CASE_SUBJECTS = (
    "Short shipment on promotional pallet",
    "Invoice price does not match agreed promotion",
    "Damaged cases reported on delivery",
    "Requested substitution for discontinued SKU",
    "Delivery window missed at store receiving",
    "Credit note not received for approved return",
    "Planogram compliance query",
    "Temperature excursion reported on chilled load",
)


def build_service(b: Builder) -> None:
    seq = 7700
    plan = (("new", 4), ("triaged", 4), ("in_progress", 6), ("pending_customer", 3), ("escalated", 3), ("reopened", 2), ("resolved", 8), ("closed", 4))
    for status, count in plan:
        for _ in range(count):
            cust = b.pick(b.ref.customers)
            seq += 1
            closed = status in ("resolved", "closed")
            age = float(b.rng.integers(1, 45))
            # A believable minority breach SLA: open cases mostly still have
            # time left, escalated ones mostly do not.
            if closed:
                due = float(b.rng.uniform(-30, -1))
            elif status == "escalated":
                due = float(b.rng.uniform(-6, -0.5))
            else:
                due = float(b.rng.uniform(-2, 9))
            meta: dict[str, Any] = {"channel": b.pick(["email", "phone", "portal"]), "reason": b.pick(["shortage", "pricing", "damage", "delivery"])}
            if closed or b.rng.random() < 0.3:
                meta["csat"] = float(round(b.rng.uniform(3.1, 5.0), 1))
            b.add(
                domain="service",
                record_type="case",
                record_number=f"CS-{seq}",
                title=b.pick(list(CASE_SUBJECTS)),
                description=f"Raised by {cust['CustomerName']} ({cust['City']}, {cust['Province']}).",
                status=status,
                account_ref=str(cust["CustomerId"]),
                service_started_ago_days=age,
                service_due_in_days=due,
                source_module="returns",
                metadata=meta,
                created_ago_days=age,
                updated_ago_days=age * 0.2,
                priority=("critical" if status == "escalated" else "high" if status == "reopened" else "medium"),
                history=[("new", status)] if status != "new" else None,
            )

    for offset in range(4):
        seq += 1
        b.add(
            domain="service",
            record_type="knowledge_article",
            record_number=f"KB-{seq}",
            title=b.pick(["Shortage claim process", "Promotional pricing disputes", "Chilled delivery exceptions", "Return authorisation guide"]),
            status="closed",
            source_module="returns",
            created_ago_days=120 + offset * 10,
            updated_ago_days=20 + offset,
        )


# --------------------------------------------------------------------------
# The decision ledger. Work items point back at the analytics signal that
# raised them and forward at the operational record that carries the work.
# --------------------------------------------------------------------------

WORK_SEEDS = (
    ("Recover margin on the ten worst private-label lines", "products", "gross_margin_pct", "Gross margin %", 21.4, 24.0, "pct", 148_000.0, "in_progress", 6, "high"),
    ("Clear the three-way match backlog before close", "suppliers", "match_exceptions", "Match exceptions", 12.0, 0.0, "count", 62_000.0, "in_progress", -3, "critical"),
    ("Rebalance safety stock on chronic short-ship SKUs", "inventory", "otif_pct", "OTIF %", 88.0, 93.0, "pct", 96_000.0, "blocked", -1, "high"),
    ("Win back the two lapsed Mountain West banners", "customers", "net_revenue_retention", "NRR", 96.2, 101.0, "pct", 210_000.0, "planned", 21, "high"),
    ("Cut preventable returns on the damage reason code", "returns", "return_rate_pct", "Return rate %", 3.1, 2.2, "pct", 78_000.0, "in_progress", 9, "medium"),
    ("Close the labour premium gap in Distribution", "salesreps", "premium_share_pct", "Premium share", 19.2, 15.0, "pct", 54_000.0, "pending_approval", 4, "medium"),
    ("Renegotiate freight terms with the top three vendors", "suppliers", "landed_cost_pct", "Landed cost %", 11.8, 10.4, "pct", 132_000.0, "planned", 30, "medium"),
    ("Fix the assortment gap in Neighborhood Market stores", "products", "assortment_coverage", "Assortment coverage", 74.0, 85.0, "pct", 88_000.0, "blocked", -6, "high"),
    ("Tighten the 13-week cash forecast variance", "overview", "cash_variance_pct", "Cash variance", 8.4, 4.0, "pct", 45_000.0, "in_progress", 14, "medium"),
    ("Escalate chilled temperature excursions to the carrier", "inventory", "excursion_count", "Excursions", 7.0, 2.0, "count", 36_000.0, "in_progress", -2, "critical"),
)

COMPLETED_WORK = (
    ("Consolidated duplicate vendor records", "suppliers", "duplicate_vendors", "Duplicate vendors", 18.0, 0.0, "count", 41_000.0, 38_500.0),
    ("Repriced the twelve loss-making promo lines", "products", "promo_margin_pct", "Promo margin %", -2.1, 3.0, "pct", 120_000.0, 133_400.0),
    ("Recovered supplier credits on damaged inbound", "suppliers", "credit_recovery", "Credit recovered", 0.0, 60_000.0, "usd", 60_000.0, 57_200.0),
    ("Cleared the aged GRNI balance over 60 days", "suppliers", "grni_aged", "Aged GRNI", 92_000.0, 0.0, "usd", 92_000.0, 88_900.0),
)


def build_work_items(b: Builder) -> list[dict]:
    items: list[dict] = []
    now = _now()
    for index, (title, module, metric_key, metric_label, baseline, target, unit, impact, status, due_offset, priority) in enumerate(WORK_SEEDS):
        created = now - timedelta(days=float(20 + index * 3))
        items.append(
            {
                "title": title,
                "description": "Raised from a certified metric movement and assigned to an owner with a measurable target.",
                "source_module": module,
                "source_record_id": f"{metric_key}:current",
                "source_url": DRILL.get(module, "/overview/"),
                "source_context_json": json.dumps({"metric": metric_key, "window": "current_fy"}),
                "affected_records_json": json.dumps([]),
                "metric_key": metric_key,
                "metric_label": metric_label,
                "baseline_value": baseline,
                "target_value": target,
                "outcome_value": None,
                "metric_unit": unit,
                "expected_financial_impact": impact,
                "realized_financial_impact": None,
                "currency": CURRENCY,
                "priority": priority,
                "status": status,
                "approval_status": ("pending" if status == "pending_approval" else "not_required"),
                "approval_route": ("finance.review" if status == "pending_approval" else None),
                "owner_user_id": b.owner("actions"),
                "created_by_user_id": b.owner("actions"),
                "created_via": "seed",
                "due_at": now + timedelta(days=float(due_offset)),
                "created_at": created,
                "updated_at": now - timedelta(days=float(index) * 0.4),
            }
        )

    for index, (title, module, metric_key, metric_label, baseline, target, unit, expected, realized) in enumerate(COMPLETED_WORK):
        created = now - timedelta(days=float(70 + index * 9))
        completed = now - timedelta(days=float(9 + index * 4))
        items.append(
            {
                "title": title,
                "description": "Completed corrective action with a measured outcome.",
                "source_module": module,
                "source_record_id": f"{metric_key}:closed",
                "source_url": DRILL.get(module, "/overview/"),
                "source_context_json": json.dumps({"metric": metric_key, "window": "prior_period"}),
                "affected_records_json": json.dumps([]),
                "metric_key": metric_key,
                "metric_label": metric_label,
                "baseline_value": baseline,
                "target_value": target,
                "outcome_value": target,
                "metric_unit": unit,
                # Both are set: `expected_impact` sums open items only, so a
                # completed item shows its expected value on the detail page
                # while contributing to the Realized tile.
                "expected_financial_impact": expected,
                "realized_financial_impact": realized,
                "currency": CURRENCY,
                "priority": "high",
                "status": "completed",
                "approval_status": "approved",
                "approval_route": "finance.review",
                "owner_user_id": b.owner("actions"),
                "created_by_user_id": b.owner("actions"),
                "approved_by_user_id": b.owner("actions"),
                "completed_by_user_id": b.owner("actions"),
                "created_via": "seed",
                "due_at": completed - timedelta(days=2),
                "approved_at": completed - timedelta(days=5),
                "completed_at": completed,
                "created_at": created,
                "updated_at": completed,
            }
        )
    return items


def write_all(builder: Builder, work_items: list[dict], *, replace: bool) -> dict[str, int]:
    from app.auth.models import SessionLocal
    from app.decision_ops.models import (
        ApprovalRecord,
        OperationalRecord,
        OperationalRecordEvent,
        OperationalRecordLine,
        SourceContract,
        WorkItem,
        WorkItemComment,
        WorkItemDependency,
        WorkItemEvent,
    )

    counts: dict[str, int] = {}
    with SessionLocal() as session:
        if replace:
            owned = [row[0] for row in session.query(OperationalRecord.id).filter(OperationalRecord.source_system == SOURCE_SYSTEM).all()]
            if owned:
                session.query(OperationalRecordLine).filter(OperationalRecordLine.operational_record_id.in_(owned)).delete(synchronize_session=False)
                session.query(OperationalRecordEvent).filter(OperationalRecordEvent.operational_record_id.in_(owned)).delete(synchronize_session=False)
                session.query(ApprovalRecord).filter(ApprovalRecord.target_type == "operational_record", ApprovalRecord.target_id.in_(owned)).delete(synchronize_session=False)
                session.query(OperationalRecord).filter(OperationalRecord.id.in_(owned)).delete(synchronize_session=False)
            seeded_work = [row[0] for row in session.query(WorkItem.id).filter(WorkItem.created_via == "seed").all()]
            if seeded_work:
                session.query(WorkItemDependency).filter(WorkItemDependency.work_item_id.in_(seeded_work)).delete(synchronize_session=False)
                session.query(WorkItemComment).filter(WorkItemComment.work_item_id.in_(seeded_work)).delete(synchronize_session=False)
                session.query(WorkItemEvent).filter(WorkItemEvent.work_item_id.in_(seeded_work)).delete(synchronize_session=False)
                session.query(ApprovalRecord).filter(ApprovalRecord.target_type == "work_item", ApprovalRecord.target_id.in_(seeded_work)).delete(synchronize_session=False)
                session.query(WorkItem).filter(WorkItem.id.in_(seeded_work)).delete(synchronize_session=False)
            session.commit()

        existing = {row[0] for row in session.query(OperationalRecord.record_number).all()}
        by_number: dict[str, OperationalRecord] = {}
        for payload in builder.records:
            if payload["record_number"] in existing:
                continue
            row = OperationalRecord(**payload)
            session.add(row)
            by_number[payload["record_number"]] = row
        session.flush()
        counts["records"] = len(by_number)

        line_total = 0
        for number, lines in builder.lines:
            parent = by_number.get(number)
            if parent is None:
                continue
            for line in lines:
                session.add(OperationalRecordLine(operational_record_id=parent.id, **line))
                line_total += 1
        counts["lines"] = line_total

        event_total = 0
        for number, chain in builder.events:
            parent = by_number.get(number)
            if parent is None:
                continue
            for event in chain:
                session.add(
                    OperationalRecordEvent(
                        operational_record_id=parent.id,
                        actor_user_id=parent.owner_user_id,
                        payload_json="{}",
                        **event,
                    )
                )
                event_total += 1
        counts["record_events"] = event_total

        approval_total = 0
        for number, approval in builder.approvals:
            parent = by_number.get(number)
            if parent is None:
                continue
            session.add(
                ApprovalRecord(
                    target_type="operational_record",
                    target_id=parent.id,
                    requested_by_user_id=parent.owner_user_id,
                    **approval,
                )
            )
            approval_total += 1
        counts["approvals"] = approval_total

        work_rows: list[WorkItem] = []
        existing_titles = {row[0] for row in session.query(WorkItem.title).all()}
        for payload in work_items:
            if payload["title"] in existing_titles:
                continue
            row = WorkItem(**payload)
            session.add(row)
            work_rows.append(row)
        session.flush()
        counts["work_items"] = len(work_rows)

        # Blocked items get a real dependency so the detail page explains why.
        blocked = [row for row in work_rows if row.status == "blocked"]
        candidates = [row for row in work_rows if row.status in ("in_progress", "planned", "pending_approval")]
        dep_total = 0
        for index, row in enumerate(blocked):
            if not candidates:
                break
            target = candidates[index % len(candidates)]
            session.add(WorkItemDependency(work_item_id=row.id, depends_on_work_item_id=target.id, created_by_user_id=row.owner_user_id))
            dep_total += 1
        counts["dependencies"] = dep_total

        wevent_total = 0
        comment_total = 0
        for row in work_rows:
            session.add(
                WorkItemEvent(
                    work_item_id=row.id,
                    event_type="created",
                    from_status=None,
                    to_status="draft",
                    actor_user_id=row.created_by_user_id,
                    payload_json="{}",
                    created_at=row.created_at,
                )
            )
            session.add(
                WorkItemEvent(
                    work_item_id=row.id,
                    event_type="status_changed",
                    from_status="draft",
                    to_status=row.status,
                    actor_user_id=row.owner_user_id,
                    payload_json="{}",
                    created_at=row.updated_at,
                )
            )
            wevent_total += 2
            session.add(
                WorkItemComment(
                    work_item_id=row.id,
                    body=(
                        "Owner confirmed the target and the measurement window."
                        if row.status != "blocked"
                        else "Blocked pending the upstream action; revisit once that clears."
                    ),
                    author_user_id=row.owner_user_id,
                    created_at=row.updated_at,
                )
            )
            comment_total += 1
            if row.status == "pending_approval":
                session.add(
                    ApprovalRecord(
                        target_type="work_item",
                        target_id=row.id,
                        route=row.approval_route or "finance.review",
                        status="pending",
                        requested_by_user_id=row.created_by_user_id,
                        notes="Awaiting finance sign-off on the expected impact.",
                        requested_at=row.updated_at,
                    )
                )
        counts["work_events"] = wevent_total
        counts["comments"] = comment_total

        # Source contracts. One stays not_connected on purpose: the enterprise
        # page is meant to show what is *not* wired up, and a test asserts it.
        connected = {
            "accounting_ledger": ("Northgate GL", "finance.controller"),
            "billing_payments": ("Northgate Billing", "finance.ar"),
            "warehouse_execution": ("DC Execution", "supply.ops"),
            "email_calendar": ("Workplace Mail", "it.platform"),
            "returns": ("Returns Tracker", "service.ops"),
        }
        contract_total = 0
        from app.decision_ops.service import SOURCE_CONTRACT_CATALOG

        present = {row[0] for row in session.query(SourceContract.contract_key).all()}
        for key, definition in SOURCE_CONTRACT_CATALOG.items():
            if key in present or key in ("identity_sso_scim", "tenant_directory", "hris_payroll", "marketing_automation"):
                continue
            system_name, owner = connected.get(key, (None, None))
            if system_name is None:
                continue
            session.add(
                SourceContract(
                    contract_key=key,
                    display_name=definition["display_name"],
                    category=definition["category"],
                    system_name=system_name,
                    status="connected",
                    owner=owner,
                    base_url=None,
                    expected_grain=definition["expected_grain"],
                    refresh_mode=definition["refresh_mode"],
                    capabilities_json=json.dumps(definition["capabilities"]),
                    last_verified_at=_now() - timedelta(hours=float(contract_total + 1)),
                )
            )
            contract_total += 1
        counts["source_contracts"] = contract_total

        session.commit()
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--replace", action="store_true", help="Delete previously seeded operations rows first")
    args = ap.parse_args(argv)

    from app import create_app

    app = create_app()
    with app.app_context():
        from app.auth.models import init_auth_db

        # Creates every table in the shared metadata, decision_ops included.
        init_auth_db()

        rng = np.random.default_rng(int(args.seed))
        ref = load_reference_data()
        owners = {
            "default": _owners(("gm", "admin")),
            "actions": _owners(("gm", "manager.coast")),
            "crm": _owners(("rep.dana", "rep.tomasz", "manager.coast")),
            "orders": _owners(("manager.coast", "gm")),
            "procurement": _owners(("gm", "admin")),
            "finance": _owners(("admin", "gm")),
            "inventory": _owners(("manager.coast", "gm")),
            "master-data": _owners(("admin",)),
            "service": _owners(("rep.dana", "manager.coast")),
        }

        builder = Builder(rng, ref, owners)
        build_crm(builder)
        build_orders(builder)
        build_procurement(builder)
        build_finance(builder)
        build_inventory(builder)
        build_master_data(builder)
        build_service(builder)
        work_items = build_work_items(builder)

        counts = write_all(builder, work_items, replace=bool(args.replace))

    print(
        "operations seed: "
        + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
