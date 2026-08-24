"""Persistence for the shared decision ledger and native workflow envelopes."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, text

from app.auth.models import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorkItem(Base):
    __tablename__ = "work_items"
    __table_args__ = (
        Index("ix_work_items_status_due", "status", "due_at"),
        Index("ix_work_items_owner_status", "owner_user_id", "status"),
        Index("ix_work_items_source", "source_module", "source_record_id"),
    )

    id = Column(Integer, primary_key=True)
    title = Column(String(240), nullable=False)
    description = Column(Text, nullable=True)
    source_module = Column(String(64), nullable=False)
    source_record_id = Column(String(180), nullable=True)
    source_url = Column(String(500), nullable=True)
    source_context_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    affected_records_json = Column(Text, nullable=False, default="[]", server_default=text("'[]'"))
    metric_key = Column(String(160), nullable=True)
    metric_label = Column(String(200), nullable=True)
    baseline_value = Column(Float, nullable=True)
    target_value = Column(Float, nullable=True)
    outcome_value = Column(Float, nullable=True)
    metric_unit = Column(String(32), nullable=True)
    expected_financial_impact = Column(Float, nullable=True)
    realized_financial_impact = Column(Float, nullable=True)
    currency = Column(String(8), nullable=False, default="CAD", server_default=text("'CAD'"))
    priority = Column(String(16), nullable=False, default="medium", server_default=text("'medium'"))
    status = Column(String(32), nullable=False, default="draft", server_default=text("'draft'"))
    approval_status = Column(String(32), nullable=False, default="not_required", server_default=text("'not_required'"))
    approval_route = Column(String(120), nullable=True)
    owner_user_id = Column(Integer, nullable=True, index=True)
    created_by_user_id = Column(Integer, nullable=True)
    approved_by_user_id = Column(Integer, nullable=True)
    completed_by_user_id = Column(Integer, nullable=True)
    created_via = Column(String(24), nullable=False, default="manual", server_default=text("'manual'"))
    due_at = Column(DateTime, nullable=True)
    reminder_at = Column(DateTime, nullable=True)
    escalation_at = Column(DateTime, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


class WorkItemEvent(Base):
    __tablename__ = "work_item_events"
    __table_args__ = (Index("ix_work_item_events_item_created", "work_item_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    work_item_id = Column(Integer, ForeignKey("work_items.id"), nullable=False)
    event_type = Column(String(64), nullable=False)
    from_status = Column(String(32), nullable=True)
    to_status = Column(String(32), nullable=True)
    actor_user_id = Column(Integer, nullable=True)
    payload_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


class WorkItemComment(Base):
    __tablename__ = "work_item_comments"
    __table_args__ = (Index("ix_work_item_comments_item_created", "work_item_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    work_item_id = Column(Integer, ForeignKey("work_items.id"), nullable=False)
    body = Column(Text, nullable=False)
    author_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


class WorkItemAttachment(Base):
    __tablename__ = "work_item_attachments"
    __table_args__ = (Index("ix_work_item_attachments_item_created", "work_item_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    work_item_id = Column(Integer, ForeignKey("work_items.id"), nullable=False)
    display_name = Column(String(240), nullable=False)
    uri = Column(String(800), nullable=False)
    content_type = Column(String(120), nullable=True)
    checksum = Column(String(128), nullable=True)
    added_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


class WorkItemDependency(Base):
    __tablename__ = "work_item_dependencies"
    __table_args__ = (
        Index("ux_work_item_dependencies_pair", "work_item_id", "depends_on_work_item_id", unique=True),
    )

    id = Column(Integer, primary_key=True)
    work_item_id = Column(Integer, ForeignKey("work_items.id"), nullable=False)
    depends_on_work_item_id = Column(Integer, ForeignKey("work_items.id"), nullable=False)
    created_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


class OperationalRecord(Base):
    __tablename__ = "operational_records"
    __table_args__ = (
        Index("ix_operational_records_domain_status", "domain", "status"),
        Index("ix_operational_records_owner_due", "owner_user_id", "due_at"),
        Index("ix_operational_records_source", "source_system", "source_record_id"),
        Index("ux_operational_records_source_identity", "domain", "record_type", "source_system", "source_record_id", unique=True),
    )

    id = Column(Integer, primary_key=True)
    domain = Column(String(40), nullable=False)
    record_type = Column(String(64), nullable=False)
    record_number = Column(String(80), nullable=False, unique=True)
    title = Column(String(240), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(40), nullable=False, default="draft", server_default=text("'draft'"))
    approval_status = Column(String(32), nullable=False, default="not_required", server_default=text("'not_required'"))
    priority = Column(String(16), nullable=False, default="medium", server_default=text("'medium'"))
    owner_user_id = Column(Integer, nullable=True, index=True)
    account_ref = Column(String(160), nullable=True)
    contact_ref = Column(String(160), nullable=True)
    product_ref = Column(String(160), nullable=True)
    supplier_ref = Column(String(160), nullable=True)
    location_ref = Column(String(160), nullable=True)
    amount = Column(Float, nullable=True)
    currency = Column(String(8), nullable=False, default="CAD", server_default=text("'CAD'"))
    quantity = Column(Float, nullable=True)
    fulfilled_quantity = Column(Float, nullable=True)
    probability_pct = Column(Float, nullable=True)
    stage = Column(String(64), nullable=True)
    forecast_category = Column(String(32), nullable=True)
    next_step = Column(String(240), nullable=True)
    due_at = Column(DateTime, nullable=True)
    close_at = Column(DateTime, nullable=True)
    service_started_at = Column(DateTime, nullable=True)
    service_due_at = Column(DateTime, nullable=True)
    parent_id = Column(Integer, ForeignKey("operational_records.id"), nullable=True)
    related_work_item_id = Column(Integer, ForeignKey("work_items.id"), nullable=True)
    source_system = Column(String(80), nullable=True)
    source_record_id = Column(String(180), nullable=True)
    source_url = Column(String(500), nullable=True)
    metadata_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    created_by_user_id = Column(Integer, nullable=True)
    updated_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


class OperationalRecordLine(Base):
    __tablename__ = "operational_record_lines"
    __table_args__ = (Index("ix_operational_record_lines_record", "operational_record_id"),)

    id = Column(Integer, primary_key=True)
    operational_record_id = Column(Integer, ForeignKey("operational_records.id"), nullable=False)
    line_number = Column(Integer, nullable=False)
    item_ref = Column(String(160), nullable=True)
    description = Column(String(300), nullable=True)
    quantity = Column(Float, nullable=False, default=0, server_default=text("0"))
    fulfilled_quantity = Column(Float, nullable=False, default=0, server_default=text("0"))
    unit_price = Column(Float, nullable=True)
    amount = Column(Float, nullable=True)
    metadata_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))


class OperationalRecordEvent(Base):
    __tablename__ = "operational_record_events"
    __table_args__ = (Index("ix_operational_record_events_record_created", "operational_record_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    operational_record_id = Column(Integer, ForeignKey("operational_records.id"), nullable=False)
    event_type = Column(String(64), nullable=False)
    from_status = Column(String(40), nullable=True)
    to_status = Column(String(40), nullable=True)
    actor_user_id = Column(Integer, nullable=True)
    payload_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


class ApprovalRecord(Base):
    __tablename__ = "approval_records"
    __table_args__ = (Index("ix_approval_records_target_status", "target_type", "target_id", "status"),)

    id = Column(Integer, primary_key=True)
    target_type = Column(String(32), nullable=False)
    target_id = Column(Integer, nullable=False)
    route = Column(String(120), nullable=False)
    status = Column(String(24), nullable=False, default="pending", server_default=text("'pending'"))
    requested_by_user_id = Column(Integer, nullable=True)
    decided_by_user_id = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)
    requested_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))
    decided_at = Column(DateTime, nullable=True)


class SourceContract(Base):
    __tablename__ = "source_contracts"
    __table_args__ = (Index("ux_source_contracts_key", "contract_key", unique=True),)

    id = Column(Integer, primary_key=True)
    contract_key = Column(String(80), nullable=False, unique=True)
    display_name = Column(String(160), nullable=False)
    category = Column(String(64), nullable=False)
    system_name = Column(String(120), nullable=True)
    status = Column(String(32), nullable=False, default="not_connected", server_default=text("'not_connected'"))
    owner = Column(String(160), nullable=True)
    base_url = Column(String(500), nullable=True)
    expected_grain = Column(String(300), nullable=False)
    refresh_mode = Column(String(80), nullable=False)
    capabilities_json = Column(Text, nullable=False, default="[]", server_default=text("'[]'"))
    last_verified_at = Column(DateTime, nullable=True)
    updated_by_user_id = Column(Integer, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


class MasterDataChange(Base):
    __tablename__ = "master_data_changes"
    __table_args__ = (Index("ix_master_data_changes_domain_status", "entity_type", "status"),)

    id = Column(Integer, primary_key=True)
    entity_type = Column(String(64), nullable=False)
    entity_key = Column(String(180), nullable=False)
    change_type = Column(String(40), nullable=False)
    status = Column(String(32), nullable=False, default="draft", server_default=text("'draft'"))
    before_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    after_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    duplicate_candidate_keys_json = Column(Text, nullable=False, default="[]", server_default=text("'[]'"))
    requested_by_user_id = Column(Integer, nullable=True)
    approved_by_user_id = Column(Integer, nullable=True)
    related_work_item_id = Column(Integer, ForeignKey("work_items.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))
    decided_at = Column(DateTime, nullable=True)


# Retained for migration/model discovery tools that import this module directly.
ALL_MODELS = (
    WorkItem,
    WorkItemEvent,
    WorkItemComment,
    WorkItemAttachment,
    WorkItemDependency,
    OperationalRecord,
    OperationalRecordLine,
    OperationalRecordEvent,
    ApprovalRecord,
    SourceContract,
    MasterDataChange,
)
