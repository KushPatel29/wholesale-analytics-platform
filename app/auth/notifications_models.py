from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, text

from .models import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_loads(raw: str | None, default: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not raw:
        return dict(default or {})
    try:
        value = json.loads(raw)
    except Exception:
        return dict(default or {})
    return value if isinstance(value, dict) else dict(default or {})


class NotificationType(Base):
    __tablename__ = "notification_types"
    __table_args__ = (
        Index("ix_notification_types_key", "key", unique=True),
    )

    id = Column(Integer, primary_key=True)
    key = Column(String(120), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    default_config_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    created_at = Column(DateTime, nullable=False, default=_utcnow, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=_utcnow, server_default=text("CURRENT_TIMESTAMP"))

    def default_config(self) -> Dict[str, Any]:
        return _json_loads(self.default_config_json, {})


class UserNotificationPreference(Base):
    __tablename__ = "user_notification_prefs"
    __table_args__ = (
        Index("ix_user_notification_prefs_user", "user_id"),
        Index("ix_user_notification_prefs_type", "type_key"),
        Index("ux_user_notification_prefs_pair", "user_id", "type_key", unique=True),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    type_key = Column(String(120), ForeignKey("notification_types.key"), nullable=False)
    enabled = Column(Integer, nullable=False, default=0, server_default=text("0"))
    frequency = Column(String(20), nullable=False, default="daily", server_default=text("'daily'"))
    config_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    created_at = Column(DateTime, nullable=False, default=_utcnow, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, nullable=False, default=_utcnow, server_default=text("CURRENT_TIMESTAMP"))

    def config(self) -> Dict[str, Any]:
        return _json_loads(self.config_json, {})


class NotificationEvent(Base):
    __tablename__ = "notification_events"
    __table_args__ = (
        Index("ix_notification_events_type", "type_key"),
        Index("ix_notification_events_user", "user_id"),
        Index("ix_notification_events_status", "status"),
        Index("ix_notification_events_sent_at", "sent_at"),
        Index("ix_notification_events_created_at", "created_at"),
        Index("ux_notification_events_hash", "type_key", "user_id", "event_hash", unique=True),
    )

    id = Column(Integer, primary_key=True)
    type_key = Column(String(120), ForeignKey("notification_types.key"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    event_hash = Column(String(64), nullable=False)
    event_payload_json = Column(Text, nullable=False, default="{}", server_default=text("'{}'"))
    window_start = Column(DateTime, nullable=True)
    window_end = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow, server_default=text("CURRENT_TIMESTAMP"))
    sent_at = Column(DateTime, nullable=True)
    status = Column(String(32), nullable=False, default="pending", server_default=text("'pending'"))
    error = Column(Text, nullable=True)

    def payload(self) -> Dict[str, Any]:
        return _json_loads(self.event_payload_json, {})
