"""Deterministic return suggestion rules for the returns intake UI."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from flask import current_app
from sqlalchemy import func

from .models import ReturnRMAItem, get_session


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _as_int(value: Any, default: int) -> int:
    try:
        if value in (None, ""):
            return int(default)
        return int(value)
    except Exception:
        return int(default)


def _parse_date(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for candidate in (raw, raw.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except Exception:
            continue
    return None


def _frequent_return_counts(skus: list[str]) -> dict[str, int]:
    wanted = [str(sku).strip().lower() for sku in skus if str(sku).strip()]
    if not wanted:
        return {}
    with get_session() as session:
        rows = (
            session.query(func.lower(ReturnRMAItem.sku), func.count(ReturnRMAItem.id))
            .filter(func.lower(ReturnRMAItem.sku).in_(wanted))
            .group_by(func.lower(ReturnRMAItem.sku))
            .all()
        )
    return {
        str(row[0]).strip().lower(): int(row[1] or 0)
        for row in rows
        if row and row[0]
    }


def _is_perishable(item: dict[str, Any]) -> bool:
    haystack = " ".join(
        [
            str(item.get("category") or ""),
            str(item.get("description") or ""),
            str(item.get("product_name") or ""),
            str(item.get("sku") or ""),
        ]
    ).lower()
    return any(token in haystack for token in ("beef", "pork", "lamb", "chicken", "protein", "meat", "veal"))


def _margin_ratio(item: dict[str, Any], margin_target: float) -> tuple[float | None, list[str]]:
    reasons: list[str] = []
    revenue = _as_float(item.get("revenue") if item.get("revenue") is not None else item.get("rev"))
    margin = _as_float(item.get("margin"))
    margin_pct = item.get("margin_pct")
    ratio = None
    if margin_pct not in (None, ""):
        ratio = _as_float(margin_pct)
    elif revenue:
        ratio = margin / revenue
    if ratio is not None and ratio < margin_target:
        reasons.append(f"Margin {ratio:.0%} is below target.")
    if margin and margin <= 0:
        reasons.append("Line margin is negative.")
    return ratio, reasons


def suggest_returns(order: dict[str, Any], items: list[dict[str, Any]], user_scope: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    margin_target = _as_float(current_app.config.get("RETURNS_MARGIN_TARGET"), 0.27)
    frequent_threshold = _as_int(current_app.config.get("RETURNS_FREQUENT_THRESHOLD"), 3)
    policy_days = _as_int(current_app.config.get("RETURNS_POLICY_DAYS"), 14)
    frequent_counts = _frequent_return_counts([str(item.get("sku") or "") for item in items])
    order_date = _parse_date(order.get("order_date"))
    now_utc = datetime.now(timezone.utc)
    within_return_window = False
    if order_date is not None:
        within_return_window = (now_utc - order_date).days <= policy_days

    out: list[dict[str, Any]] = []
    selected_count = 0
    for item in items:
        qty_ordered = max(_as_float(item.get("qty_ordered"), _as_float(item.get("qty"), 1.0)), 0.0)
        qty_shipped = max(_as_float(item.get("qty_shipped"), qty_ordered or 1.0), 0.0)
        max_qty = max(qty_shipped, qty_ordered, 1.0)
        shipped_short = qty_ordered > 0 and qty_shipped > 0 and qty_shipped < qty_ordered
        sku = str(item.get("sku") or "").strip().lower()

        rationale: list[str] = []
        _ratio, margin_reasons = _margin_ratio(item, margin_target)
        rationale.extend(margin_reasons)
        frequent_hits = int(frequent_counts.get(sku, 0))
        if frequent_hits >= frequent_threshold:
            rationale.append(f"SKU has {frequent_hits} prior returns.")
        if shipped_short:
            rationale.append("Shipped quantity is below ordered quantity.")
        if within_return_window:
            rationale.append(f"Order is within the {policy_days}-day return window.")

        selected = bool(rationale)
        if selected:
            selected_count += 1

        reason_code = "quality_issue" if _is_perishable(item) else "customer_return"
        if shipped_short and reason_code == "customer_return":
            reason_code = "wrong_item"
        condition = "opened" if _is_perishable(item) else "unopened"

        suggested_qty = min(max(qty_shipped or 1.0, 1.0), max_qty)
        out.append(
            {
                "order_line_id": item.get("order_line_id"),
                "selected": selected,
                "suggested_return_qty": round(suggested_qty, 2),
                "suggested_reason": reason_code,
                "suggested_condition": condition,
                "rationale": " ".join(rationale).strip() or "Defaulted to a standard customer return suggestion.",
            }
        )

    if out and selected_count == 0:
        for row in out:
            row["selected"] = True
            row["rationale"] = "No elevated risk signals were found, so all items are available for manual selection."

    current_app.logger.info(
        "returns.suggest",
        extra={
            "order_id": order.get("order_id"),
            "customer_id": order.get("customer_id"),
            "item_count": len(items),
            "selected_count": sum(1 for row in out if row.get("selected")),
            "scope_hash": (user_scope or {}).get("scope_hash"),
        },
    )
    return out
