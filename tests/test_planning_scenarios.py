from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.auth.models import SessionLocal
from app.planning_scenario_models import (
    PlanningReplenishmentRecommendation,
    PlanningScenario,
    PlanningScenarioEvent,
)
from app.services import planning_scenarios


def _actor(user_id: int = 91):
    return SimpleNamespace(id=user_id, role="manager")


def _measured_result():
    return {
        "source_state": "measured",
        "source_message": "test",
        "summary": {"sku_count": 1, "recommended_units": 12.0, "estimated_cost": 60.0},
        "recommendations": [{
            "product_id": "SKU-PLAN-1",
            "product_name": "Planning SKU",
            "recommended_qty": 12.0,
            "estimated_cost": 60.0,
            "rationale": "Measured shortage.",
        }],
    }


def test_planning_scenario_versions_approve_and_write_back(app, monkeypatch):
    monkeypatch.setattr(
        planning_scenarios,
        "build_replenishment_recommendations",
        lambda assumptions: _measured_result(),
    )
    name = f"Scenario {uuid4().hex}"
    actor = _actor()
    first = planning_scenarios.create_scenario(
        name=name,
        preset="base",
        assumptions=planning_scenarios.PRESETS["base"],
        actor_user=actor,
    )
    second = planning_scenarios.create_version(
        first["id"],
        assumptions={"demand_growth_pct": 8},
        actor_user=actor,
    )
    assert first["version"] == 1
    assert second["version"] == 2
    assert second["parent_scenario_id"] == first["id"]
    assert second["assumptions"]["demand_growth_pct"] == 8.0

    pending = planning_scenarios.transition_scenario(
        second["id"], target_status="pending_approval", actor_user=actor
    )
    assert pending["status"] == "pending_approval"
    approved = planning_scenarios.transition_scenario(
        second["id"], target_status="approved", actor_user=actor, notes="Approved test plan."
    )
    assert approved["status"] == "approved"
    assert approved["recommendation_count"] == 1
    detail = planning_scenarios.get_scenario(second["id"])
    assert detail is not None
    assert detail["approved_recommendations"][0]["product_id"] == "SKU-PLAN-1"
    assert [event["to_status"] for event in detail["events"]] == [
        "draft", "pending_approval", "approved"
    ]

    with SessionLocal() as session:
        ids = [first["id"], second["id"]]
        session.query(PlanningReplenishmentRecommendation).filter(
            PlanningReplenishmentRecommendation.scenario_id.in_(ids)
        ).delete(synchronize_session=False)
        session.query(PlanningScenarioEvent).filter(
            PlanningScenarioEvent.scenario_id.in_(ids)
        ).delete(synchronize_session=False)
        session.query(PlanningScenario).filter(PlanningScenario.id.in_(ids)).delete(synchronize_session=False)
        session.commit()


def test_planning_scenario_assumptions_are_bounded():
    values = planning_scenarios.normalize_assumptions({}, preset="upside")
    assert values["demand_growth_pct"] == 15.0
    assert values["service_level_pct"] == 97.0
    with pytest.raises(planning_scenarios.PlanningScenarioError):
        planning_scenarios.normalize_assumptions({"service_level_pct": 120})
