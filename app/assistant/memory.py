from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List

from cachetools import TTLCache
from flask import current_app, has_app_context


_THREAD_TTL_SECONDS = max(300, int(os.getenv("ASSISTANT_THREAD_TTL_SECONDS", str(60 * 60 * 4))))
_CACHE = TTLCache(maxsize=512, ttl=_THREAD_TTL_SECONDS)
_LOCK = RLock()
_DB_READY: set[str] = set()
_LAST_PRUNE_TS = 0.0


@dataclass
class ThreadState:
    thread_id: str
    user_id: str
    created_at: float
    updated_at: float
    messages: List[Dict[str, Any]] = field(default_factory=list)
    tool_history: List[Dict[str, Any]] = field(default_factory=list)
    state: Dict[str, Any] = field(default_factory=dict)


def _key(user_id: Any, thread_id: str) -> str:
    return f"{str(user_id or 'anon')}:{thread_id}"


def _store_path() -> Path:
    configured = str(os.getenv("ASSISTANT_THREAD_STORE_PATH") or "").strip()
    if not configured and has_app_context():
        configured = str(current_app.config.get("ASSISTANT_THREAD_STORE_PATH") or "").strip()
        if not configured:
            configured = str(current_app.config.get("CACHE_DIR") or current_app.config.get("DATA_DIR") or "").strip()
            if configured:
                configured = os.path.join(configured, "assistant", "thread_state.sqlite3")
    if not configured:
        configured = str(os.getenv("CACHE_DIR") or os.getenv("DATA_DIR") or "").strip()
        if configured:
            configured = os.path.join(configured, "assistant", "thread_state.sqlite3")
    if not configured:
        configured = os.path.join(tempfile.gettempdir(), "wa_assistant", "thread_state.sqlite3")
    path = Path(configured).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection | None:
    path = _store_path()
    try:
        conn = sqlite3.connect(path.as_posix(), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        if path.as_posix() not in _DB_READY:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assistant_threads (
                    cache_key TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    messages_json TEXT NOT NULL,
                    tool_history_json TEXT NOT NULL,
                    state_json TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_assistant_threads_updated_at ON assistant_threads(updated_at)")
            conn.commit()
            _DB_READY.add(path.as_posix())
        return conn
    except Exception:
        return None


def _prune_expired(conn: sqlite3.Connection) -> None:
    global _LAST_PRUNE_TS
    now = time.time()
    if now - _LAST_PRUNE_TS < 300:
        return
    cutoff = now - _THREAD_TTL_SECONDS
    try:
        conn.execute("DELETE FROM assistant_threads WHERE updated_at < ?", (cutoff,))
        conn.commit()
        _LAST_PRUNE_TS = now
    except Exception:
        return


def _decode_state(row: sqlite3.Row | None) -> ThreadState | None:
    if row is None:
        return None
    try:
        return ThreadState(
            thread_id=str(row["thread_id"] or "").strip(),
            user_id=str(row["user_id"] or "").strip(),
            created_at=float(row["created_at"] or time.time()),
            updated_at=float(row["updated_at"] or time.time()),
            messages=list(json.loads(row["messages_json"] or "[]")),
            tool_history=list(json.loads(row["tool_history_json"] or "[]")),
            state=dict(json.loads(row["state_json"] or "{}")),
        )
    except Exception:
        return None


def _load_persisted(user_id: Any, thread_id: str) -> ThreadState | None:
    conn = _connect()
    if conn is None:
        return None
    cache_key = _key(user_id, thread_id)
    cutoff = time.time() - _THREAD_TTL_SECONDS
    try:
        _prune_expired(conn)
        row = conn.execute(
            """
            SELECT cache_key, thread_id, user_id, created_at, updated_at, messages_json, tool_history_json, state_json
            FROM assistant_threads
            WHERE cache_key = ? AND updated_at >= ?
            """,
            (cache_key, cutoff),
        ).fetchone()
        return _decode_state(row)
    except Exception:
        return None
    finally:
        conn.close()


def _persist_state(state: ThreadState) -> None:
    conn = _connect()
    if conn is None:
        return
    try:
        _prune_expired(conn)
        conn.execute(
            """
            INSERT INTO assistant_threads (
                cache_key, thread_id, user_id, created_at, updated_at, messages_json, tool_history_json, state_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                thread_id=excluded.thread_id,
                user_id=excluded.user_id,
                created_at=excluded.created_at,
                updated_at=excluded.updated_at,
                messages_json=excluded.messages_json,
                tool_history_json=excluded.tool_history_json,
                state_json=excluded.state_json
            """,
            (
                _key(state.user_id, state.thread_id),
                state.thread_id,
                state.user_id,
                float(state.created_at),
                float(state.updated_at),
                json.dumps(list(state.messages or []), separators=(",", ":"), default=str),
                json.dumps(list(state.tool_history or []), separators=(",", ":"), default=str),
                json.dumps(dict(state.state or {}), separators=(",", ":"), default=str),
            ),
        )
        conn.commit()
    except Exception:
        return
    finally:
        conn.close()


def _delete_persisted(user_id: Any, thread_id: str) -> None:
    conn = _connect()
    if conn is None:
        return
    try:
        conn.execute("DELETE FROM assistant_threads WHERE cache_key = ?", (_key(user_id, thread_id),))
        conn.commit()
    except Exception:
        return
    finally:
        conn.close()


def new_thread_id() -> str:
    return f"th_{uuid.uuid4().hex[:16]}"


def get_or_create(user_id: Any, thread_id: str | None) -> ThreadState:
    now = time.time()
    resolved_id = str(thread_id or "").strip() or new_thread_id()
    persisted = _load_persisted(user_id, resolved_id)
    if isinstance(persisted, ThreadState):
        with _LOCK:
            _CACHE[_key(user_id, resolved_id)] = persisted
        return persisted
    cache_key = _key(user_id, resolved_id)
    with _LOCK:
        state = _CACHE.get(cache_key)
        if isinstance(state, ThreadState):
            state.updated_at = now
            return state
        state = ThreadState(
            thread_id=resolved_id,
            user_id=str(user_id or "anon"),
            created_at=now,
            updated_at=now,
        )
        _CACHE[cache_key] = state
    _persist_state(state)
    return state


def append_turn(
    user_id: Any,
    thread_id: str,
    *,
    user_message: str,
    assistant_answer: str,
    tool_trace: List[Dict[str, Any]] | None = None,
    state_update: Dict[str, Any] | None = None,
    max_turns: int = 12,
) -> ThreadState:
    state = get_or_create(user_id, thread_id)
    now = time.time()
    state.updated_at = now
    state.messages.append({"role": "user", "content": str(user_message or ""), "ts": now})
    state.messages.append({"role": "assistant", "content": str(assistant_answer or ""), "ts": now})
    if tool_trace:
        state.tool_history.extend(tool_trace)
    if isinstance(state_update, dict) and state_update:
        base = dict(state.state or {})
        base.update(state_update)
        state.state = base
    max_messages = max(2, int(max_turns) * 2)
    if len(state.messages) > max_messages:
        state.messages = state.messages[-max_messages:]
    if len(state.tool_history) > max_messages:
        state.tool_history = state.tool_history[-max_messages:]
    with _LOCK:
        _CACHE[_key(user_id, thread_id)] = state
    _persist_state(state)
    return state


def recent_messages(user_id: Any, thread_id: str, *, limit: int = 6) -> List[Dict[str, Any]]:
    state = get_or_create(user_id, thread_id)
    if limit <= 0:
        return []
    return list(state.messages[-int(limit):])


def summarize_history(user_id: Any, thread_id: str) -> Dict[str, Any]:
    state = get_or_create(user_id, thread_id)
    last_user = next((m["content"] for m in reversed(state.messages) if m.get("role") == "user"), None)
    last_assistant = next((m["content"] for m in reversed(state.messages) if m.get("role") == "assistant"), None)
    return {
        "thread_id": state.thread_id,
        "message_count": len(state.messages),
        "last_user": last_user,
        "last_assistant": last_assistant,
        "tool_calls": len(state.tool_history),
        "state": dict(state.state or {}),
    }


def thread_snapshot(user_id: Any, thread_id: str, *, limit: int = 12) -> Dict[str, Any]:
    state = get_or_create(user_id, thread_id)
    take = max(1, int(limit))
    return {
        "thread_id": state.thread_id,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "messages": list(state.messages[-take:]),
        "tool_trace": list(state.tool_history[-take:]),
        "state": dict(state.state or {}),
    }


def thread_state(user_id: Any, thread_id: str) -> Dict[str, Any]:
    state = get_or_create(user_id, thread_id)
    return dict(state.state or {})


def update_thread_state(user_id: Any, thread_id: str, update: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(update, dict):
        return thread_state(user_id, thread_id)
    state = get_or_create(user_id, thread_id)
    merged = dict(state.state or {})
    merged.update(update)
    state.state = merged
    state.updated_at = time.time()
    with _LOCK:
        _CACHE[_key(user_id, thread_id)] = state
    _persist_state(state)
    return dict(merged)


def clear_thread(user_id: Any, thread_id: str) -> None:
    with _LOCK:
        _CACHE.pop(_key(user_id, thread_id), None)
    _delete_persisted(user_id, thread_id)
