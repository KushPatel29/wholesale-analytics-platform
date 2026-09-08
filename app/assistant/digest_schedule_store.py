"""Saved digest presets.

Two things were wrong with the previous version of this module, and they are
worth naming because the second is the reason the first mattered.

**Schedules did not survive a restart.** They lived in a process-local
`TTLCache`, so every worker had its own set and a redeploy - or the free
instance spinning down after fifteen idle minutes - dropped all of them. A
schedule the user could create and then not find again is worse than no
schedule at all.

**Nothing ever delivered them.** There is no scheduler process and no mailer
wired to this store; `mark_schedule_run` is only ever reached from an explicit
"run it now" call. The response text nonetheless said "delivery is governed and
reviewable", which reads as a promise that an email goes out at the chosen
hour. It does not. Rather than build a delivery worker that a spun-down free
instance could not run anyway, these records are presented for what they
actually are: a **saved preset** - the module, audience, length and cadence you
want a digest built from - that you generate on demand. `DELIVERY_MODE` and
`DELIVERY_NOTE` are the single place that claim is made, so the API, the
assistant and the tests cannot drift from each other.

Persistence mirrors `app/assistant/memory.py`: SQLite under `CACHE_DIR`, with
the same env override and temp-dir fallback. On an ephemeral filesystem this
survives worker and process restarts but not a redeploy, which is the honest
limit of the deployment rather than of this store.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List

from flask import current_app, has_app_context


_LOCK = RLock()
_DB_READY: set[str] = set()

# Retained so a preset a visitor made months ago is not silently resurrected
# with a stale window, while still far longer than any demo session.
_PRESET_TTL_SECONDS = 60 * 60 * 24 * 90

#: No process delivers these. Said once, here.
DELIVERY_MODE = "on_demand"
DELIVERY_NOTE = (
    "Saved as a preset. This demo generates digests on request - no scheduled "
    "delivery runs and no email is sent."
)

DEFAULT_HOUR_LOCAL = 8


def coerce_hour_local(value: Any, *, default: int = DEFAULT_HOUR_LOCAL) -> int:
    """Read an hour-of-day, keeping midnight.

    `int(value or 8)` turned a requested hour of `0` into `8`, because `0` is
    falsy - so a midnight digest silently became an 8 a.m. one. Only an absent
    or unparseable value may fall back to the default; `0` is a real hour.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            hour = int(float(text))
        except ValueError:
            return default
    else:
        try:
            hour = int(value)
        except (TypeError, ValueError):
            return default
    return max(0, min(23, hour))


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
            # Sent on every record so no caller has to remember to add it.
            "delivery_mode": DELIVERY_MODE,
            "delivery_note": DELIVERY_NOTE,
        }


def _key(user_id: Any, schedule_id: str) -> str:
    return f"{str(user_id or 'anon')}:{schedule_id}"


def _store_path() -> Path:
    configured = str(os.getenv("ASSISTANT_DIGEST_STORE_PATH") or "").strip()
    if not configured and has_app_context():
        configured = str(current_app.config.get("ASSISTANT_DIGEST_STORE_PATH") or "").strip()
        if not configured:
            configured = str(current_app.config.get("CACHE_DIR") or current_app.config.get("DATA_DIR") or "").strip()
            if configured:
                configured = os.path.join(configured, "assistant", "digest_schedules.sqlite3")
    if not configured:
        configured = str(os.getenv("CACHE_DIR") or os.getenv("DATA_DIR") or "").strip()
        if configured:
            configured = os.path.join(configured, "assistant", "digest_schedules.sqlite3")
    if not configured:
        configured = os.path.join(tempfile.gettempdir(), "wa_assistant", "digest_schedules.sqlite3")
    path = Path(configured).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection | None:
    try:
        path = _store_path()
        conn = sqlite3.connect(path.as_posix(), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        if path.as_posix() not in _DB_READY:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS digest_schedules (
                    cache_key TEXT PRIMARY KEY,
                    schedule_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    module TEXT NOT NULL,
                    cadence TEXT NOT NULL,
                    audience TEXT NOT NULL,
                    length TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    hour_local INTEGER NOT NULL,
                    active INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_run_at REAL,
                    run_count INTEGER NOT NULL DEFAULT 0,
                    last_status TEXT,
                    scope_json TEXT NOT NULL DEFAULT '{}',
                    filters_json TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_digest_schedules_user ON digest_schedules(user_id, updated_at)")
            conn.commit()
            _DB_READY.add(path.as_posix())
        return conn
    except Exception:
        return None


def _decode(row: sqlite3.Row | None) -> DigestSchedule | None:
    if row is None:
        return None
    try:
        return DigestSchedule(
            schedule_id=str(row["schedule_id"] or "").strip(),
            user_id=str(row["user_id"] or "").strip(),
            module=str(row["module"] or "overview"),
            cadence=str(row["cadence"] or "weekly"),
            audience=str(row["audience"] or "leadership"),
            length=str(row["length"] or "short"),
            timezone=str(row["timezone"] or "UTC"),
            hour_local=int(row["hour_local"]),
            active=bool(row["active"]),
            created_at=float(row["created_at"] or time.time()),
            updated_at=float(row["updated_at"] or time.time()),
            last_run_at=(float(row["last_run_at"]) if row["last_run_at"] is not None else None),
            run_count=int(row["run_count"] or 0),
            last_status=(str(row["last_status"]) if row["last_status"] is not None else None),
            scope=dict(json.loads(row["scope_json"] or "{}")),
            filters=dict(json.loads(row["filters_json"] or "{}")),
        )
    except Exception:
        return None


def _write(conn: sqlite3.Connection, cache_key: str, schedule: DigestSchedule) -> None:
    conn.execute(
        """
        INSERT INTO digest_schedules (
            cache_key, schedule_id, user_id, module, cadence, audience, length,
            timezone, hour_local, active, created_at, updated_at, last_run_at,
            run_count, last_status, scope_json, filters_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET
            module=excluded.module,
            cadence=excluded.cadence,
            audience=excluded.audience,
            length=excluded.length,
            timezone=excluded.timezone,
            hour_local=excluded.hour_local,
            active=excluded.active,
            updated_at=excluded.updated_at,
            last_run_at=excluded.last_run_at,
            run_count=excluded.run_count,
            last_status=excluded.last_status,
            scope_json=excluded.scope_json,
            filters_json=excluded.filters_json
        """,
        (
            cache_key,
            schedule.schedule_id,
            schedule.user_id,
            schedule.module,
            schedule.cadence,
            schedule.audience,
            schedule.length,
            schedule.timezone,
            int(schedule.hour_local),
            1 if schedule.active else 0,
            float(schedule.created_at),
            float(schedule.updated_at),
            (float(schedule.last_run_at) if schedule.last_run_at is not None else None),
            int(schedule.run_count),
            schedule.last_status,
            json.dumps(schedule.scope or {}, default=str),
            json.dumps(schedule.filters or {}, default=str),
        ),
    )
    conn.commit()


def _prune(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("DELETE FROM digest_schedules WHERE updated_at < ?", (time.time() - _PRESET_TTL_SECONDS,))
        conn.commit()
    except Exception:
        return


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
        hour_local=coerce_hour_local(payload.get("hour_local")),
        active=bool(payload.get("active", True)),
        created_at=now,
        updated_at=now,
        scope=dict(payload.get("scope") or {}),
        filters=dict(payload.get("filters") or {}),
    )
    with _LOCK:
        conn = _connect()
        if conn is not None:
            try:
                _write(conn, _key(user_id, schedule_id), schedule)
            except Exception:
                pass
            finally:
                conn.close()
    return schedule.as_dict()


def list_schedules(user_id: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with _LOCK:
        conn = _connect()
        if conn is None:
            return rows
        try:
            _prune(conn)
            for row in conn.execute(
                "SELECT * FROM digest_schedules WHERE user_id = ? ORDER BY updated_at DESC",
                (str(user_id or "anon"),),
            ):
                decoded = _decode(row)
                if decoded is not None:
                    rows.append(decoded.as_dict())
        except Exception:
            return rows
        finally:
            conn.close()
    return rows


def get_schedule(user_id: Any, schedule_id: str) -> Dict[str, Any] | None:
    with _LOCK:
        conn = _connect()
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT * FROM digest_schedules WHERE cache_key = ?",
                (_key(user_id, schedule_id),),
            ).fetchone()
        except Exception:
            return None
        finally:
            conn.close()
    decoded = _decode(row)
    return decoded.as_dict() if decoded is not None else None


def delete_schedule(user_id: Any, schedule_id: str) -> bool:
    with _LOCK:
        conn = _connect()
        if conn is None:
            return False
        try:
            cursor = conn.execute(
                "DELETE FROM digest_schedules WHERE cache_key = ?",
                (_key(user_id, schedule_id),),
            )
            conn.commit()
            return int(cursor.rowcount or 0) > 0
        except Exception:
            return False
        finally:
            conn.close()


def mark_schedule_run(user_id: Any, schedule_id: str, *, status: str) -> Dict[str, Any] | None:
    cache_key = _key(user_id, schedule_id)
    with _LOCK:
        conn = _connect()
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT * FROM digest_schedules WHERE cache_key = ?", (cache_key,)).fetchone()
            schedule = _decode(row)
            if schedule is None:
                return None
            schedule.last_run_at = time.time()
            schedule.last_status = str(status or "unknown")
            schedule.run_count = int(schedule.run_count) + 1
            schedule.updated_at = schedule.last_run_at
            _write(conn, cache_key, schedule)
            return schedule.as_dict()
        except Exception:
            return None
        finally:
            conn.close()
