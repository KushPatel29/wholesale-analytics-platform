"""Persistence models for versioned Planning scenarios and approved recommendations."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, text

from app.auth.models import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PlanningScenario(Base):
    __tablename__ = "planning_scenarios"
    __table_args__ = (
        Index("ux_planning_scenarios_name_version", "name", "version", unique=True),
        Index("ix_planning_scenarios_status_updated", "status", "updated_at"),
    )

    id = Column(Integer, primary_key=True)
    name = Column(String(180), nullable=False)
    version = Column(Integer, nullable=False, default=1, server_default=text("1"))
    parent_scenario_id = Column(Integer, ForeignKey("planning_scenarios.id"), nullable=True)
    status = Column(String(32), nullable=False, default="draft", server_default=text("'draft'"))
    assumptions_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    results_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    created_by_user_id = Column(Integer, nullable=True)
    submitted_by_user_id = Column(Integer, nullable=True)
    approved_by_user_id = Column(Integer, nullable=True)
    decision_notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))
    submitted_at = Column(DateTime, nullable=True)
    approved_at = Column(DateTime, nullable=True)


class PlanningScenarioEvent(Base):
    __tablename__ = "planning_scenario_events"
    __table_args__ = (Index("ix_planning_scenario_events_scenario_created", "scenario_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    scenario_id = Column(Integer, ForeignKey("planning_scenarios.id"), nullable=False)
    event_type = Column(String(64), nullable=False)
    from_status = Column(String(32), nullable=True)
    to_status = Column(String(32), nullable=True)
    actor_user_id = Column(Integer, nullable=True)
    payload_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


class PlanningReplenishmentRecommendation(Base):
    __tablename__ = "planning_replenishment_recommendations"
    __table_args__ = (
        Index("ix_planning_replenishment_scenario", "scenario_id"),
        Index("ix_planning_replenishment_product_status", "product_id", "status"),
    )

    id = Column(Integer, primary_key=True)
    scenario_id = Column(Integer, ForeignKey("planning_scenarios.id"), nullable=False)
    product_id = Column(String(128), nullable=False)
    product_name = Column(String(255), nullable=True)
    recommended_qty = Column(Float, nullable=False, default=0.0, server_default=text("0"))
    estimated_cost = Column(Float, nullable=True)
    status = Column(String(32), nullable=False, default="approved", server_default=text("'approved'"))
    rationale = Column(Text, nullable=True)
    approved_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))
