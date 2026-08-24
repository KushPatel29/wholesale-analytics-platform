"""Versioned demand scenarios that write approved replenishment recommendations."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

import pandas as pd
from sqlalchemy import func

from app.auth.models import SessionLocal
from app.planning_scenario_models import (
    PlanningReplenishmentRecommendation,
    PlanningScenario,
    PlanningScenarioEvent,
)
from app.services import fact_store


class PlanningScenarioError(RuntimeError):
    pass


TRANSITIONS: dict[str, set[str]] = {
    "draft": {"pending_approval"},
    "pending_approval": {"approved", "rejected"},
    "approved": set(),
    "rejected": set(),
}

PRESETS: dict[str, dict[str, float | None]] = {
    "base": {
        "demand_growth_pct": 0.0,
        "lead_time_days": 14.0,
        "safety_stock_days": 7.0,
        "service_level_pct": 95.0,
        "budget_limit": None,
    },
    "upside": {
        "demand_growth_pct": 15.0,
        "lead_time_days": 16.0,
        "safety_stock_days": 10.0,
        "service_level_pct": 97.0,
        "budget_limit": None,
    },
    "downside": {
        "demand_growth_pct": -10.0,
        "lead_time_days": 12.0,
        "safety_stock_days": 5.0,
        "service_level_pct": 92.0,
        "budget_limit": None,
    },
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _actor_id(actor_user: Any) -> int | None:
    try:
        return int(getattr(actor_user, "id", None))
    except (TypeError, ValueError):
        return None


def _loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw or ""))
    except Exception:
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def normalize_assumptions(payload: Mapping[str, Any], *, preset: str = "base") -> dict[str, float | None]:
    defaults = dict(PRESETS.get(str(preset).strip().lower(), PRESETS["base"]))

    def number(key: str, minimum: float, maximum: float, *, nullable: bool = False) -> float | None:
        raw = payload.get(key, defaults[key])
        if nullable and raw in (None, ""):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise PlanningScenarioError(f"{key.replace('_', ' ').title()} must be numeric.") from exc
        if value < minimum or value > maximum:
            raise PlanningScenarioError(
                f"{key.replace('_', ' ').title()} must be between {minimum:g} and {maximum:g}."
            )
        return round(value, 2)

    return {
        "demand_growth_pct": number("demand_growth_pct", -50, 200),
        "lead_time_days": number("lead_time_days", 1, 180),
        "safety_stock_days": number("safety_stock_days", 0, 90),
        "service_level_pct": number("service_level_pct", 80, 99.9),
        "budget_limit": number("budget_limit", 0, 1_000_000_000, nullable=True),
    }


def build_replenishment_recommendations(assumptions: Mapping[str, Any]) -> dict[str, Any]:
    """Calculate recommendations from recent shipped demand and latest on-hand state."""
    values = normalize_assumptions(assumptions)
    try:
        columns = fact_store.list_columns()
        required = {"Date", "ProductId"}
        qty_col = next((name for name in ("QuantityShipped", "QuantityOrdered") if name in columns), None)
        if not required <= set(columns) or not qty_col:
            return {
                "source_state": "source_required",
                "source_message": "Product, date, and shipped-quantity fields are required.",
                "recommendations": [],
                "summary": {"sku_count": 0, "recommended_units": 0.0, "estimated_cost": None},
            }
        name_expr = "ARG_MAX(ProductName, Date)" if "ProductName" in columns else "CAST(ProductId AS VARCHAR)"
        on_hand_expr = "ARG_MAX(COALESCE(CAST(OnHandQty AS DOUBLE), 0), Date)" if "OnHandQty" in columns else "0::DOUBLE"
        if "CostPrice" in columns:
            unit_cost_expr = "ARG_MAX(COALESCE(CAST(CostPrice AS DOUBLE), 0), Date)"
        elif "Cost" in columns:
            unit_cost_expr = f"SUM(CAST(Cost AS DOUBLE)) / NULLIF(SUM(CAST({qty_col} AS DOUBLE)), 0)"
        else:
            unit_cost_expr = "NULL::DOUBLE"
        frame = fact_store.execute_sql_df(
            f"""
            WITH cutoff AS (SELECT MAX(CAST(Date AS DATE)) AS max_date FROM fact)
            SELECT CAST(ProductId AS VARCHAR) AS product_id,
                   {name_expr} AS product_name,
                   SUM(CAST({qty_col} AS DOUBLE)) / 90.0 AS avg_daily_units,
                   {on_hand_expr} AS on_hand_qty,
                   {unit_cost_expr} AS unit_cost
            FROM fact, cutoff
            WHERE CAST(Date AS DATE) > cutoff.max_date - INTERVAL 90 DAY
              AND CAST(Date AS DATE) <= cutoff.max_date
            GROUP BY 1
            """,
            [],
            tag="planning.scenarios.replenishment",
        )
    except Exception as exc:
        return {
            "source_state": "computation_error",
            "source_message": f"Recommendation source could not be evaluated: {exc}",
            "recommendations": [],
            "summary": {"sku_count": 0, "recommended_units": 0.0, "estimated_cost": None},
        }
    growth_factor = 1.0 + float(values["demand_growth_pct"] or 0) / 100.0
    cover_days = float(values["lead_time_days"] or 0) + float(values["safety_stock_days"] or 0)
    recommendations: list[dict[str, Any]] = []
    for raw in frame.to_dict(orient="records"):
        daily = max(float(raw.get("avg_daily_units") or 0), 0.0)
        on_hand = max(float(raw.get("on_hand_qty") or 0), 0.0)
        target_stock = daily * cover_days * growth_factor
        recommended = max(target_stock - on_hand, 0.0)
        if recommended <= 0.005:
            continue
        cost = raw.get("unit_cost")
        unit_cost = float(cost) if cost is not None and pd.notna(cost) else None
        recommendations.append({
            "product_id": str(raw.get("product_id") or ""),
            "product_name": str(raw.get("product_name") or raw.get("product_id") or ""),
            "avg_daily_units": round(daily, 3),
            "on_hand_qty": round(on_hand, 3),
            "target_stock_qty": round(target_stock, 3),
            "recommended_qty": round(recommended, 3),
            "unit_cost": round(unit_cost, 4) if unit_cost is not None else None,
            "estimated_cost": round(recommended * unit_cost, 2) if unit_cost is not None else None,
            "rationale": f"{cover_days:g} days of lead-time plus safety-stock cover at {growth_factor:.2f}× base demand.",
        })
    recommendations.sort(key=lambda row: (-float(row["recommended_qty"]), row["product_id"]))
    budget = values.get("budget_limit")
    if budget is not None:
        remaining = float(budget)
        for row in recommendations:
            if row["estimated_cost"] is None:
                continue
            cost = float(row["estimated_cost"])
            if cost <= remaining:
                remaining -= cost
                continue
            unit_cost = float(row["unit_cost"] or 0)
            affordable = max(remaining / unit_cost, 0.0) if unit_cost else 0.0
            row["recommended_qty"] = round(min(float(row["recommended_qty"]), affordable), 3)
            row["estimated_cost"] = round(row["recommended_qty"] * unit_cost, 2)
            row["budget_constrained"] = True
            remaining = 0.0
        recommendations = [row for row in recommendations if float(row["recommended_qty"]) > 0]
    known_costs = [float(row["estimated_cost"]) for row in recommendations if row["estimated_cost"] is not None]
    return {
        "source_state": "measured",
        "source_message": "Recommendations use the latest 90 days of shipped demand and latest captured on-hand quantity.",
        "recommendations": recommendations[:100],
        "summary": {
            "sku_count": len(recommendations[:100]),
            "recommended_units": round(sum(float(row["recommended_qty"]) for row in recommendations[:100]), 3),
            "estimated_cost": round(sum(known_costs[:100]), 2) if known_costs else None,
        },
    }


def _event(
    session: Any,
    scenario: PlanningScenario,
    *,
    event_type: str,
    actor_user: Any,
    from_status: str | None = None,
    to_status: str | None = None,
    payload: Mapping[str, Any] | None = None,
) -> None:
    session.add(PlanningScenarioEvent(
        scenario_id=int(scenario.id),
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        actor_user_id=_actor_id(actor_user),
        payload_json=_dumps(dict(payload or {})),
    ))


def _serialize(row: PlanningScenario, *, recommendation_count: int = 0) -> dict[str, Any]:
    results = _loads(row.results_json, {})
    return {
        "id": int(row.id),
        "name": row.name,
        "version": int(row.version),
        "parent_scenario_id": row.parent_scenario_id,
        "status": row.status,
        "assumptions": _loads(row.assumptions_json, {}),
        "results": results,
        "recommendation_count": int(recommendation_count),
        "created_by_user_id": row.created_by_user_id,
        "submitted_by_user_id": row.submitted_by_user_id,
        "approved_by_user_id": row.approved_by_user_id,
        "decision_notes": row.decision_notes or "",
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
        "submitted_at": row.submitted_at.isoformat() if row.submitted_at else "",
        "approved_at": row.approved_at.isoformat() if row.approved_at else "",
    }


def create_scenario(
    *,
    name: str,
    assumptions: Mapping[str, Any],
    preset: str = "base",
    actor_user: Any = None,
    parent_scenario_id: int | None = None,
) -> dict[str, Any]:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise PlanningScenarioError("Scenario name is required.")
    normalized = normalize_assumptions(assumptions, preset=preset)
    results = build_replenishment_recommendations(normalized)
    with SessionLocal() as session:
        current_version = session.query(func.max(PlanningScenario.version)).filter(
            PlanningScenario.name == clean_name
        ).scalar()
        row = PlanningScenario(
            name=clean_name,
            version=int(current_version or 0) + 1,
            parent_scenario_id=parent_scenario_id,
            status="draft",
            assumptions_json=_dumps(normalized),
            results_json=_dumps(results),
            created_by_user_id=_actor_id(actor_user),
        )
        session.add(row)
        session.flush()
        _event(session, row, event_type="created", actor_user=actor_user, to_status="draft", payload={"assumptions": normalized})
        session.commit()
        session.refresh(row)
        return _serialize(row)


def create_version(
    scenario_id: int,
    *,
    assumptions: Mapping[str, Any],
    actor_user: Any = None,
) -> dict[str, Any]:
    with SessionLocal() as session:
        source = session.get(PlanningScenario, int(scenario_id))
        if not source:
            raise PlanningScenarioError("Scenario not found.")
        name = source.name
        base = _loads(source.assumptions_json, {})
    base.update({key: value for key, value in assumptions.items() if value not in (None, "")})
    return create_scenario(
        name=name,
        assumptions=base,
        actor_user=actor_user,
        parent_scenario_id=int(scenario_id),
    )


def list_scenarios(*, limit: int = 50) -> list[dict[str, Any]]:
    with SessionLocal() as session:
        rows = session.query(PlanningScenario).order_by(
            PlanningScenario.updated_at.desc(), PlanningScenario.id.desc()
        ).limit(max(1, min(int(limit), 200))).all()
        counts = dict(session.query(
            PlanningReplenishmentRecommendation.scenario_id,
            func.count(PlanningReplenishmentRecommendation.id),
        ).group_by(PlanningReplenishmentRecommendation.scenario_id).all())
        return [_serialize(row, recommendation_count=counts.get(int(row.id), 0)) for row in rows]


def get_scenario(scenario_id: int) -> dict[str, Any] | None:
    with SessionLocal() as session:
        row = session.get(PlanningScenario, int(scenario_id))
        if not row:
            return None
        recommendations = session.query(PlanningReplenishmentRecommendation).filter(
            PlanningReplenishmentRecommendation.scenario_id == int(row.id)
        ).order_by(PlanningReplenishmentRecommendation.estimated_cost.desc()).all()
        events = session.query(PlanningScenarioEvent).filter(
            PlanningScenarioEvent.scenario_id == int(row.id)
        ).order_by(PlanningScenarioEvent.created_at.asc(), PlanningScenarioEvent.id.asc()).all()
        payload = _serialize(row, recommendation_count=len(recommendations))
        payload["approved_recommendations"] = [{
            "product_id": item.product_id,
            "product_name": item.product_name,
            "recommended_qty": item.recommended_qty,
            "estimated_cost": item.estimated_cost,
            "status": item.status,
            "rationale": item.rationale,
        } for item in recommendations]
        payload["events"] = [{
            "event_type": item.event_type,
            "from_status": item.from_status,
            "to_status": item.to_status,
            "actor_user_id": item.actor_user_id,
            "created_at": item.created_at.isoformat() if item.created_at else "",
        } for item in events]
        return payload


def transition_scenario(
    scenario_id: int,
    *,
    target_status: str,
    actor_user: Any = None,
    notes: str | None = None,
) -> dict[str, Any]:
    target = str(target_status or "").strip().lower()
    with SessionLocal() as session:
        row = session.get(PlanningScenario, int(scenario_id))
        if not row:
            raise PlanningScenarioError("Scenario not found.")
        current = str(row.status or "draft")
        if target not in TRANSITIONS.get(current, set()):
            raise PlanningScenarioError(f"Cannot move scenario from {current} to {target}.")
        now = _utcnow()
        if target == "pending_approval":
            row.submitted_by_user_id = _actor_id(actor_user)
            row.submitted_at = now
        if target in {"approved", "rejected"}:
            row.approved_by_user_id = _actor_id(actor_user)
            row.approved_at = now
            row.decision_notes = str(notes or "").strip() or None
        row.status = target
        row.updated_at = now
        if target == "approved":
            results = _loads(row.results_json, {})
            for item in results.get("recommendations") or []:
                session.add(PlanningReplenishmentRecommendation(
                    scenario_id=int(row.id),
                    product_id=str(item.get("product_id") or ""),
                    product_name=str(item.get("product_name") or "") or None,
                    recommended_qty=float(item.get("recommended_qty") or 0),
                    estimated_cost=item.get("estimated_cost"),
                    status="approved",
                    rationale=str(item.get("rationale") or "") or None,
                    approved_by_user_id=_actor_id(actor_user),
                ))
        _event(
            session, row, event_type="status_changed", actor_user=actor_user,
            from_status=current, to_status=target, payload={"notes": str(notes or "").strip()},
        )
        session.commit()
        session.refresh(row)
        count = session.query(func.count(PlanningReplenishmentRecommendation.id)).filter(
            PlanningReplenishmentRecommendation.scenario_id == int(row.id)
        ).scalar()
        return _serialize(row, recommendation_count=int(count or 0))
