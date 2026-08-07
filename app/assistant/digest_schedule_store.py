from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from threading import RLock
from typing import Any, Dict, List

from cachetools import TTLCache


_SCHEDULES = TTLCache(maxsize=2048, ttl=60 * 60 * 24 * 90)
_LOCK = RLock()


@dataclass
class DigestSchedule:
    schedule_id: str
    user_id: str
    module: str
    cadence: str
    audience: str
    length: str
    timezone: str
    hour_local: int
    active: bool
    created_at: float
    updated_at: float
    last_run_at: float | None = None
    run_count: int = 0
    last_status: str | None = None
    scope: Dict[str, Any] | None = None
    filters: Dict[str, Any] | None = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "user_id": self.user_id,
            "module": self.module,
            "cadence": self.cadence,
            "audience": self.audience,
            "length": self.length,
            "timezone": self.timezone,
            "hour_local": self.hour_local,
            "active": bool(self.active),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_run_at": self.last_run_at,
            "run_count": int(self.run_count),
            "last_status": self.last_status,
            "scope": dict(self.scope or {}),
            "filters": dict(self.filters or {}),
        }


def _key(user_id: Any, schedule_id: str) -> str:
    return f"{str(user_id or 'anon')}:{schedule_id}"


def create_schedule(user_id: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    now = time.time()
    schedule_id = f"sch_{uuid.uuid4().hex[:12]}"
    schedule = DigestSchedule(
        schedule_id=schedule_id,
        user_id=str(user_id or "anon"),
        module=str(payload.get("module") or "overview"),
        cadence=str(payload.get("cadence") or "weekly"),
        audience=str(payload.get("audience") or "leadership"),
        length=str(payload.get("length") or "short"),
        timezone=str(payload.get("timezone") or "UTC"),
        hour_local=int(payload.get("hour_local") or 8),
        active=bool(payload.get("active", True)),
        created_at=now,
        updated_at=now,
        scope=dict(payload.get("scope") or {}),
        filters=dict(payload.get("filters") or {}),
    )
    with _LOCK:
        _SCHEDULES[_key(user_id, schedule_id)] = schedule
    return schedule.as_dict()


def list_schedules(user_id: Any) -> List[Dict[str, Any]]:
    prefix = f"{str(user_id or 'anon')}:"
    rows: List[Dict[str, Any]] = []
    with _LOCK:
        for key, value in _SCHEDULES.items():
            if not key.startswith(prefix):
                continue
            if not isinstance(value, DigestSchedule):
                continue
            rows.append(value.as_dict())
    rows.sort(key=lambda row: float(row.get("updated_at") or 0.0), reverse=True)
    return rows


def get_schedule(user_id: Any, schedule_id: str) -> Dict[str, Any] | None:
    with _LOCK:
        value = _SCHEDULES.get(_key(user_id, schedule_id))
    if not isinstance(value, DigestSchedule):
        return None
    return value.as_dict()


def delete_schedule(user_id: Any, schedule_id: str) -> bool:
    with _LOCK:
        existed = _SCHEDULES.pop(_key(user_id, schedule_id), None)
    return isinstance(existed, DigestSchedule)


def mark_schedule_run(user_id: Any, schedule_id: str, *, status: str) -> Dict[str, Any] | None:
    cache_key = _key(user_id, schedule_id)
    with _LOCK:
        row = _SCHEDULES.get(cache_key)
        if not isinstance(row, DigestSchedule):
            return None
        row.last_run_at = time.time()
        row.last_status = str(status or "unknown")
        row.run_count = int(row.run_count) + 1
        row.updated_at = row.last_run_at
        _SCHEDULES[cache_key] = row
        return row.as_dict()
