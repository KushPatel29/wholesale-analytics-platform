"""
Seed the RMA tracker from real rows in the fact dataset.

The returns module implements a full credit-request workflow - intake, two-step
warehouse and manager approval, rejection with a reason, PDF export - and the
deployed demo rendered it as "No returns match the current filters." above an
empty table. A tracker with nothing in it demonstrates none of that, and a
reviewer reads an empty page as an unfinished one.

Every RMA here is built against a real order: a real order id, the customer who
placed it, the SKUs on it and the prices they were billed at. That matters more
than it sounds. An RMA whose order id matches nothing means the drilldown links
out of the tracker dead-end, the credit amounts bear no relation to the revenue
on the rest of the site, and anyone who cross-checks one against the Customers
page finds a number that cannot be reconciled.

Statuses are spread across the whole workflow rather than bunched at intake, so
the approvals queue, the warehouse view and the completed-credit reporting all
have rows to show.

Usage:
    python -m seed.generate_synthetic_returns
    python -m seed.generate_synthetic_returns --count 120 --seed 7
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_COUNT = 140
DEFAULT_SEED = 4207

# Share of order lines that come back. Retail return rates run 5-10% overall and
# much higher in apparel and electronics; the tracker only ever holds the ones
# raised as credit requests, which is a small fraction of that.
RETURN_REASONS = (
    # (reason_code, reason_text, category, weight, typical credit share)
    ("damaged", "Damaged in transit", "Warehouse", 0.26, 1.00),
    ("quality_issue", "Quality issue on arrival", "Production", 0.21, 1.00),
    ("short_issue", "Short shipped", "Warehouse", 0.18, 1.00),
    ("wrong_item", "Wrong item picked", "Sales", 0.15, 1.00),
    ("customer_return", "Customer changed order", "Sales", 0.13, 0.85),
    ("vendor_return", "Vendor recall", "Other", 0.07, 1.00),
)

# Where each RMA has got to. Weighted so most are resolved - a queue that is
# almost entirely "requested" looks abandoned rather than busy - while leaving
# enough live work for the approvals and warehouse views to be worth opening.
STATUS_MIX = (
    ("completed", 0.34),
    ("approved", 0.16),
    ("wh_approved", 0.12),
    ("received", 0.10),
    ("requested", 0.11),
    ("needs_review", 0.07),
    ("in_transit", 0.05),
    ("rejected", 0.05),
)

REJECT_REASONS = (
    "Outside the 14-day credit window.",
    "Product was signed for in good condition; no damage evidence supplied.",
    "Weight variance is within the catch-weight tolerance for this item.",
    "Duplicate of an RMA already credited on this order.",
)

# The managers who sign off credits, matching the approver filter on the
# tracker so the dropdown has something to select.
APPROVERS = ("Marisa Whitfield", "Devon Clarke", "Priya Raman")

APPROVER_NOTES = (
    "Credit approved in full; pallet scrapped on receipt.",
    "Partial credit agreed with the account after review.",
    "Approved. Vendor debit raised against the supplier.",
    "Approved on inspection; product restocked as B-grade.",
)


def _weighted_pick(rng: np.random.Generator, options, weights, size: int) -> np.ndarray:
    probs = np.asarray(weights, dtype=float)
    probs = probs / probs.sum()
    return rng.choice(len(options), size=size, p=probs)


def load_order_lines(limit: int = 6000):
    """
    A sample of real order lines, newest first.

    Newest first because a returns tracker is a working queue: a credit request
    raised eighteen months ago is history, not work, and the default filter
    window would hide it anyway.
    """
    from app.services import fact_store

    # The `fact` view, which is what every page queries - not the raw parquet.
    # An RMA priced off the raw rows would not reconcile with the revenue shown
    # anywhere else, because the view is where weight/unit billing is resolved.
    sql = """
        SELECT
            OrderId,
            CustomerId,
            CustomerName,
            SalesRepName,
            SKU,
            ProductName,
            ProteinType,
            DateExpected,
            QuantityShipped,
            Price,
            WeightLb,
            UnitOfBillingId,
            pack_weight_lb_sum,
            pack_item_count_sum
        FROM fact
        WHERE QuantityShipped > 0
        ORDER BY DateExpected DESC
        LIMIT ?
    """
    return fact_store.execute_sql_df(sql, [int(limit)], tag="seed.returns")


def build_returns(count: int = DEFAULT_COUNT, seed: int = DEFAULT_SEED) -> list[dict]:
    """Assemble the RMA payloads, each drawn from one real order."""
    rng = np.random.default_rng(seed)
    lines = load_order_lines()
    if lines is None or lines.empty:
        raise SystemExit(
            "No fact rows found. Run `python -m seed.generate_synthetic_data` first."
        )

    # One RMA per order, so an order does not appear twice in the tracker.
    orders = lines.groupby("OrderId", sort=False)
    order_ids = list(orders.groups.keys())
    if not order_ids:
        raise SystemExit("No orders in the fact dataset.")

    picked = rng.choice(len(order_ids), size=min(count, len(order_ids)), replace=False)

    status_names = [name for name, _ in STATUS_MIX]
    status_weights = [weight for _, weight in STATUS_MIX]
    status_idx = _weighted_pick(rng, status_names, status_weights, len(picked))

    reason_idx = _weighted_pick(
        rng, RETURN_REASONS, [r[3] for r in RETURN_REASONS], len(picked)
    )

    payloads: list[dict] = []
    for n, order_pos in enumerate(picked):
        order_id = order_ids[order_pos]
        group = orders.get_group(order_id)
        head = group.iloc[0]

        order_date = head["DateExpected"]
        if hasattr(order_date, "date"):
            order_date = order_date.date()

        # Credit requests are raised days after delivery, not on the same day.
        submitted = datetime.combine(order_date, datetime.min.time()) + timedelta(
            days=int(rng.integers(2, 15)), hours=int(rng.integers(8, 18))
        )

        reason_code, reason_text, category, _weight, credit_share = RETURN_REASONS[reason_idx[n]]
        status = status_names[status_idx[n]]

        # One to three lines off the order come back, never the whole order
        # unless it only had one line.
        line_count = min(len(group), int(rng.integers(1, 4)))
        returned = group.head(line_count)

        items = []
        total_credit = 0.0
        total_weight = 0.0
        total_packs = 0
        for _, row in returned.iterrows():
            billed_units = float(row.get("pack_item_count_sum") or 0.0)
            billed_weight = float(row.get("pack_weight_lb_sum") or 0.0)
            price = float(row.get("Price") or 0.0)
            # Bill by weight or by unit, matching how the line was sold.
            line_value = price * (billed_weight if billed_weight and price else billed_units)
            # Partial returns are the norm: a case or two off a pallet.
            share = float(rng.uniform(0.25, 1.0))
            credit = round(max(line_value * share * credit_share, 0.0), 2)
            weight = round(billed_weight * share, 2)
            packs = max(1, int(round(billed_units * share)))
            total_credit += credit
            total_weight += weight
            total_packs += packs
            items.append(
                {
                    "sku": str(row.get("SKU") or ""),
                    "product_name": str(row.get("ProductName") or ""),
                    "product_code": str(row.get("SKU") or ""),
                    "price": round(price, 4),
                    "weight_lb": weight,
                    "packs_count": packs,
                    "credit_amount": credit,
                    "credit_pct": round(share * credit_share * 100, 2),
                    "reason_code": reason_code,
                    "reason_for_return": reason_text,
                    "category": category,
                    "qty": packs,
                }
            )

        payloads.append(
            {
                "rma_number": f"RMA-{submitted.year}-{4200 + n:05d}",
                "order_id": str(order_id),
                "order_date": order_date,
                "customer_id": str(head.get("CustomerId") or ""),
                "customer_name": str(head.get("CustomerName") or ""),
                "rep_name": str(head.get("SalesRepName") or ""),
                "date_submitted": submitted,
                "status": status,
                "primary_reason": reason_text,
                "primary_category": category,
                "total_credit_amount": round(total_credit, 2),
                "total_weight_lb": round(total_weight, 2),
                "total_packs": total_packs,
                "return_type": "credit",
                "company": "Northgate Retail Group",
                # Who signs it off. The tracker renders this column directly,
                # so leaving it null showed "-" for every row on a page whose
                # whole subject is the approval chain.
                "approval_target": APPROVERS[int(rng.integers(len(APPROVERS)))],
                "items": items,
                "reject_reason": (
                    REJECT_REASONS[int(rng.integers(len(REJECT_REASONS)))]
                    if status == "rejected"
                    else None
                ),
                "decision_summary": (
                    APPROVER_NOTES[int(rng.integers(len(APPROVER_NOTES)))]
                    if status in {"approved", "completed"}
                    else None
                ),
                "additional_notes": (
                    f"Raised by {head.get('SalesRepName') or 'the account team'} "
                    f"against order {order_id}."
                ),
            }
        )
    return payloads


# The approval trail each status implies. Written as data rather than as a
# chain of ifs, so "what has happened to this RMA" and "which timestamps are
# set" cannot drift apart.
_TRAIL = {
    "requested": (),
    "needs_review": ("submitted",),
    "in_transit": ("submitted",),
    "received": ("submitted", "wh"),
    "wh_approved": ("submitted", "wh"),
    "approved": ("submitted", "wh", "mgr"),
    "completed": ("submitted", "wh", "mgr", "credited"),
    "rejected": ("submitted", "rejected"),
}


def write_returns(payloads: list[dict], *, wh_user_id: int | None, mgr_user_id: int | None) -> int:
    """Insert the RMAs, their lines and their approval trail."""
    from app.returns.models import (
        ReturnApproval,
        ReturnEvent,
        ReturnRMA,
        ReturnRMAItem,
        get_session,
    )

    written = 0
    with get_session() as session:
        for payload in payloads:
            items = payload.pop("items")
            reject_reason = payload.pop("reject_reason", None)

            rma = ReturnRMA(
                rma_number=payload["rma_number"],
                customer_id=payload["customer_id"],
                customer_name=payload["customer_name"],
                order_id=payload["order_id"],
                order_date=payload["order_date"],
                rep_name=payload["rep_name"],
                date_submitted=payload["date_submitted"],
                return_type=payload["return_type"],
                additional_notes=payload["additional_notes"],
                approval_target=payload.get("approval_target"),
                total_credit_amount=payload["total_credit_amount"],
                total_weight_lb=payload["total_weight_lb"],
                total_packs=payload["total_packs"],
                primary_reason=payload["primary_reason"],
                primary_category=payload["primary_category"],
                status=payload["status"],
                company=payload["company"],
                decision_summary=payload.get("decision_summary"),
                reject_reason=reject_reason,
                created_at=payload["date_submitted"],
                updated_at=payload["date_submitted"],
                metadata_json=json.dumps({"source": "seed.generate_synthetic_returns"}),
            )

            stages = _TRAIL.get(payload["status"], ())
            submitted = payload["date_submitted"]
            wh_at = submitted + timedelta(days=1, hours=6)
            mgr_at = wh_at + timedelta(days=1, hours=3)
            credited_at = mgr_at + timedelta(days=2)

            if "wh" in stages:
                rma.wh_approved_at = wh_at
                rma.wh_approved_by_user_id = wh_user_id
                rma.wh_reviewed_at = wh_at
            if "mgr" in stages:
                rma.mgr_approved_at = mgr_at
                rma.mgr_approved_by_user_id = mgr_user_id
                rma.ops_cleared_at = mgr_at
            if "credited" in stages:
                rma.fin_cleared_at = credited_at
                rma.updated_at = credited_at
            if "rejected" in stages:
                rma.rejected_at = wh_at
                rma.rejected_by_user_id = mgr_user_id
                rma.updated_at = wh_at

            session.add(rma)
            session.flush()

            for item in items:
                session.add(ReturnRMAItem(rma_id=rma.id, **item))

            if "wh" in stages or "mgr" in stages or "rejected" in stages:
                session.add(
                    ReturnApproval(
                        rma_id=rma.id,
                        wh_approved_by=wh_user_id if "wh" in stages else None,
                        wh_approved_at=wh_at if "wh" in stages else None,
                        mgr_approved_by=mgr_user_id if "mgr" in stages else None,
                        mgr_approved_at=mgr_at if "mgr" in stages else None,
                        rejected_by=mgr_user_id if "rejected" in stages else None,
                        rejected_at=wh_at if "rejected" in stages else None,
                        reject_reason=reject_reason,
                    )
                )

            # The audit trail the RMA detail view renders.
            session.add(
                ReturnEvent(
                    rma_id=rma.id,
                    event_type="created",
                    to_status="requested",
                    created_at=submitted,
                    payload_json="{}",
                )
            )
            if "wh" in stages:
                session.add(
                    ReturnEvent(
                        rma_id=rma.id,
                        event_type="status_change",
                        from_status="requested",
                        to_status="wh_approved",
                        actor_user_id=wh_user_id,
                        created_at=wh_at,
                        payload_json="{}",
                    )
                )
            if "mgr" in stages:
                session.add(
                    ReturnEvent(
                        rma_id=rma.id,
                        event_type="status_change",
                        from_status="wh_approved",
                        to_status="approved",
                        actor_user_id=mgr_user_id,
                        created_at=mgr_at,
                        payload_json="{}",
                    )
                )
            if "rejected" in stages:
                session.add(
                    ReturnEvent(
                        rma_id=rma.id,
                        event_type="status_change",
                        from_status="requested",
                        to_status="rejected",
                        actor_user_id=mgr_user_id,
                        created_at=wh_at,
                        payload_json=json.dumps({"reason": reject_reason}),
                    )
                )
            written += 1
        session.commit()
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--replace", action="store_true", help="Delete existing seeded RMAs first")
    args = ap.parse_args(argv)

    from app import create_app

    app = create_app()
    with app.app_context():
        from app.returns.models import (
            ReturnApproval,
            ReturnEvent,
            ReturnRMA,
            ReturnRMAItem,
            get_session,
        )
        # init_auth_db creates every table in the shared metadata, returns
        # tables included, and applies their migrations.
        from app.auth.models import init_auth_db

        init_auth_db()

        if args.replace:
            with get_session() as session:
                ids = [row[0] for row in session.query(ReturnRMA.id).all()]
                if ids:
                    session.query(ReturnRMAItem).filter(ReturnRMAItem.rma_id.in_(ids)).delete(
                        synchronize_session=False
                    )
                    session.query(ReturnApproval).filter(ReturnApproval.rma_id.in_(ids)).delete(
                        synchronize_session=False
                    )
                    session.query(ReturnEvent).filter(ReturnEvent.rma_id.in_(ids)).delete(
                        synchronize_session=False
                    )
                    session.query(ReturnRMA).filter(ReturnRMA.id.in_(ids)).delete(
                        synchronize_session=False
                    )
                    session.commit()
                print(f"  removed {len(ids)} existing RMAs")

        # Approvals are attributed to the seeded demo accounts, so the tracker
        # shows a named approver rather than a bare user id.
        from app.auth.models import get_user_by_username

        wh_user = get_user_by_username("viewer.nocost")
        mgr_user = get_user_by_username("manager.coast")

        payloads = build_returns(count=args.count, seed=args.seed)
        written = write_returns(
            payloads,
            wh_user_id=int(wh_user.id) if wh_user else None,
            mgr_user_id=int(mgr_user.id) if mgr_user else None,
        )

    print(f"  RMAs written : {written:,}")
    credit = sum(p["total_credit_amount"] for p in payloads)
    print(f"  total credit : ${credit:,.2f}")
    by_status: dict[str, int] = {}
    for p in payloads:
        by_status[p["status"]] = by_status.get(p["status"], 0) + 1
    for status, n in sorted(by_status.items(), key=lambda kv: -kv[1]):
        print(f"    {status:16} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
