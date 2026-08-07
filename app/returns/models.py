"""SQLAlchemy models and persistence helpers for the returns module."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text, text

from app.auth.models import Base, SessionLocal
from .migrations import apply_returns_migrations


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def dumps_json(payload: Any, default: str = "{}") -> str:
    if payload is None:
        return default
    try:
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)
    except Exception:
        return default


def loads_json(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


class ReturnRMA(Base):
    __tablename__ = "return_rmas"
    __table_args__ = (
        Index("ux_return_rmas_rma_number", "rma_number", unique=True),
        Index("ix_return_rmas_order_id", "order_id"),
        Index("ix_return_rmas_customer_id", "customer_id"),
        Index("ix_return_rmas_status_date_submitted", "status", "date_submitted"),
        Index("ix_return_rmas_date_submitted", "date_submitted"),
        Index("ix_return_rmas_status_created", "status", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    rma_number = Column(String(64), nullable=True)
    customer_id = Column(String(128), nullable=False, index=True)
    customer_name = Column(String(255), nullable=True)
    customer_email = Column(String(255), nullable=True)
    customer_phone = Column(String(64), nullable=True)
    order_id = Column(String(128), nullable=False, index=True)
    order_date = Column(Date, nullable=True)
    rep_user_id = Column(Integer, nullable=True, index=True)
    rep_name = Column(String(255), nullable=True)
    date_submitted = Column(DateTime, nullable=True)
    date_shipped = Column(DateTime, nullable=True)
    return_type = Column(String(64), nullable=True)
    advised_customer = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    additional_notes = Column(Text, nullable=True)
    total_credit_amount = Column(Float, nullable=False, default=0.0, server_default=text("0"))
    total_weight_lb = Column(Float, nullable=False, default=0.0, server_default=text("0"))
    total_packs = Column(Integer, nullable=False, default=0, server_default=text("0"))
    primary_reason = Column(String(255), nullable=True)
    primary_category = Column(String(64), nullable=True)
    rec_prod_signoff = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    rec_prod_signed_by_user_id = Column(Integer, nullable=True)
    rec_prod_signed_at = Column(DateTime, nullable=True)
    wh_approved_by_user_id = Column(Integer, nullable=True)
    wh_approved_at = Column(DateTime, nullable=True)
    mgr_approved_by_user_id = Column(Integer, nullable=True)
    mgr_approved_at = Column(DateTime, nullable=True)
    rejected_by_user_id = Column(Integer, nullable=True)
    rejected_at = Column(DateTime, nullable=True)
    reject_reason = Column(Text, nullable=True)
    status = Column(String(64), nullable=False, default="requested")
    company = Column(String(64), nullable=True)
    approval_target = Column(String(64), nullable=True)
    created_by_user_id = Column(Integer, nullable=True)
    
    # SLA Timestamps
    ops_cleared_at = Column(DateTime, nullable=True)
    wh_reviewed_at = Column(DateTime, nullable=True)
    fin_cleared_at = Column(DateTime, nullable=True)

    assigned_user_id = Column(Integer, nullable=True)
    decision_summary = Column(String(255), nullable=True)
    policy_version = Column(String(64), nullable=True)
    risk_score = Column(Float, nullable=False, default=0.0, server_default=text("0"))
    external_reference = Column(String(128), nullable=True)
    metadata_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


class ReturnRMAItem(Base):
    __tablename__ = "return_rma_items"
    __table_args__ = (
        Index("ix_return_rma_items_rma_id", "rma_id"),
        Index("ix_return_rma_items_product_code", "product_code"),
        Index("ix_return_rma_items_reason_code", "reason_code"),
    )

    id = Column(Integer, primary_key=True)
    rma_id = Column(Integer, ForeignKey("return_rmas.id"), nullable=False, index=True)
    order_line_id = Column(String(128), nullable=True)
    sku = Column(String(128), nullable=True)
    product_name = Column(String(255), nullable=True)
    product_code = Column(String(128), nullable=True)
    product_desc = Column(String(255), nullable=True)
    price_per_lb = Column(Float, nullable=False, default=0.0, server_default=text("0"))
    weight_lb = Column(Float, nullable=False, default=0.0, server_default=text("0"))
    credit_pct = Column(Numeric(5, 2), nullable=False, default=100.0, server_default=text("100.00"))
    credit_amount = Column(Float, nullable=False, default=0.0, server_default=text("0"))
    product_returning = Column(Boolean, nullable=False, default=True, server_default=text("1"))
    packs_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    pack_barcode = Column(String(255), nullable=True)
    reason_for_return = Column(String(255), nullable=True)
    follow_up_action = Column(String(255), nullable=True)
    category = Column(String(64), nullable=True)
    supplier_credit = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    qty = Column(Float, nullable=False, default=0.0, server_default=text("0"))
    price = Column(Float, nullable=False, default=0.0, server_default=text("0"))
    reason_code = Column(String(64), nullable=True)
    item_condition = Column(String(64), nullable=True)
    notes = Column(Text, nullable=True)
    receiving_notes = Column(Text, nullable=True)
    warehouse_outcome = Column(String(128), nullable=True)
    received_weight_lb = Column(Float, nullable=True)
    metadata_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


class ReturnReasonCode(Base):
    __tablename__ = "return_reason_codes"
    __table_args__ = (
        Index("ux_return_reason_codes_reason_code", "reason_code", unique=True),
        Index("ix_return_reason_codes_active", "active"),
    )

    id = Column(Integer, primary_key=True)
    reason_code = Column(String(64), nullable=False, unique=True, index=True)
    reason_text = Column(String(255), nullable=False)
    category = Column(String(32), nullable=False, default="Other", server_default=text("'Other'"))
    active = Column(Boolean, nullable=False, default=True, server_default=text("1"))


class ReturnSetting(Base):
    __tablename__ = "return_settings"
    __table_args__ = (
        Index("ux_return_settings_key", "setting_key", unique=True),
    )

    id = Column(Integer, primary_key=True)
    setting_key = Column(String(128), nullable=False, unique=True, index=True)
    setting_value = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    updated_by_user_id = Column(Integer, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


class ReturnEvent(Base):
    __tablename__ = "return_events"
    __table_args__ = (Index("ix_return_events_rma_created", "rma_id", "created_at"),)

    id = Column(Integer, primary_key=True)
    rma_id = Column(Integer, ForeignKey("return_rmas.id"), nullable=False, index=True)
    event_type = Column(String(64), nullable=False)
    from_status = Column(String(64), nullable=True)
    to_status = Column(String(64), nullable=True)
    
    # Granular Field Changes for Enterprise Auditing
    field_name = Column(String(128), nullable=True)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)

    actor_user_id = Column(Integer, nullable=True)
    payload_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


class ReturnApproval(Base):
    __tablename__ = "return_approvals"
    __table_args__ = (
        Index("ux_return_approvals_rma_id", "rma_id", unique=True),
    )

    id = Column(Integer, primary_key=True)
    rma_id = Column(Integer, ForeignKey("return_rmas.id"), nullable=False, index=True)
    wh_approved_by = Column(Integer, nullable=True)
    wh_approved_at = Column(DateTime, nullable=True)
    mgr_approved_by = Column(Integer, nullable=True)
    mgr_approved_at = Column(DateTime, nullable=True)
    rejected_by = Column(Integer, nullable=True)
    rejected_at = Column(DateTime, nullable=True)
    reject_reason = Column(Text, nullable=True)


class ReturnComment(Base):
    __tablename__ = "return_comments"
    __table_args__ = (Index("ix_return_comments_rma_id", "rma_id"),)

    id = Column(Integer, primary_key=True)
    rma_id = Column(Integer, ForeignKey("return_rmas.id"), nullable=False, index=True)
    user_id = Column(Integer, nullable=True)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


class ReturnAttachment(Base):
    __tablename__ = "return_attachments"
    __table_args__ = (Index("ix_return_attachments_rma_id", "rma_id"),)

    id = Column(Integer, primary_key=True)
    rma_id = Column(Integer, ForeignKey("return_rmas.id"), nullable=False, index=True)
    item_id = Column(Integer, ForeignKey("return_rma_items.id"), nullable=True)
    filename = Column(String(255), nullable=True)
    mimetype = Column(String(255), nullable=True)
    storage_path = Column(Text, nullable=True)
    file_path = Column(Text, nullable=False)
    mime = Column(String(255), nullable=True)
    size = Column(Integer, nullable=False, default=0, server_default=text("0"))
    uploaded_by_user_id = Column(Integer, nullable=True)
    uploaded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


class ReturnInspection(Base):
    __tablename__ = "return_inspections"
    __table_args__ = (Index("ix_return_inspections_rma_id", "rma_id"),)

    id = Column(Integer, primary_key=True)
    rma_id = Column(Integer, ForeignKey("return_rmas.id"), nullable=False, index=True)
    disposition = Column(String(64), nullable=True)
    notes = Column(Text, nullable=True)
    photos_json = Column(Text, nullable=False, default="[]", server_default=text("'[]'"))
    inspected_by_user_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


class ReturnShipment(Base):
    __tablename__ = "return_shipments"
    __table_args__ = (Index("ix_return_shipments_rma_id", "rma_id"),)

    id = Column(Integer, primary_key=True)
    rma_id = Column(Integer, ForeignKey("return_rmas.id"), nullable=False, index=True)
    carrier = Column(String(64), nullable=True)
    label_url = Column(Text, nullable=True)
    tracking_number = Column(String(128), nullable=True)
    shipping_cost = Column(Float, nullable=False, default=0.0, server_default=text("0"))
    status = Column(String(64), nullable=True)
    metadata_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


class ReturnRefund(Base):
    __tablename__ = "return_refunds"
    __table_args__ = (Index("ix_return_refunds_rma_id", "rma_id"),)

    id = Column(Integer, primary_key=True)
    rma_id = Column(Integer, ForeignKey("return_rmas.id"), nullable=False, index=True)
    amount = Column(Float, nullable=False, default=0.0, server_default=text("0"))
    method = Column(String(64), nullable=True)
    status = Column(String(64), nullable=True)
    processor_ref = Column(String(128), nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


class ReturnPolicyVersion(Base):
    __tablename__ = "return_policy_versions"
    __table_args__ = (Index("ux_return_policy_versions_version", "version", unique=True),)

    id = Column(Integer, primary_key=True)
    version = Column(String(64), nullable=False, unique=True, index=True)
    rules_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    is_active = Column(Boolean, nullable=False, default=False, server_default=text("0"))
    created_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))


class ReturnWebhookEvent(Base):
    __tablename__ = "return_webhook_events"
    __table_args__ = (
        Index("ux_return_webhook_events_idempotency", "idempotency_key", unique=True),
        Index("ix_return_webhook_events_status", "status"),
    )

    id = Column(Integer, primary_key=True)
    source = Column(String(64), nullable=False)
    event_type = Column(String(64), nullable=False)
    idempotency_key = Column(String(255), nullable=False, unique=True, index=True)
    payload_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    received_at = Column(DateTime, nullable=False, default=utcnow, server_default=text("CURRENT_TIMESTAMP"))
    processed_at = Column(DateTime, nullable=True)
    status = Column(String(64), nullable=False, default="pending", server_default=text("'pending'"))


def apply_returns_schema_migrations(conn: Any, safe_exec: Any) -> None:
    apply_returns_migrations(conn, safe_exec)


def get_session():
    return SessionLocal()
