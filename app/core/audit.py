from __future__ import annotations

import json
from typing import Any, Optional
from flask import request

from ..auth.models import SessionLocal, AuditLog


def _ip_from_request() -> str:
    try:
        fwd = request.headers.get("X-Forwarded-For")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.remote_addr or ""
    except Exception:
        return ""


def _safe_json(val: Any) -> Optional[str]:
    if val is None:
        return None
    try:
        return json.dumps(val)[:4000]
    except Exception:
        try:
            return json.dumps(str(val))[:4000]
        except Exception:
            return str(val)[:4000]


def log_audit(
    user: Any,
    action: str,
    meta: Optional[dict] = None,
    *,
    target_user_id: Optional[int] = None,
    before: Any = None,
    after: Any = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """Best-effort audit trail that captures actor, target, and change payloads."""
    try:
        username = None
        actor_user_id = None
        if isinstance(user, str):
            username = user
        else:
            actor_user_id = getattr(user, "id", None)
            username = getattr(user, "username", None) or actor_user_id
            if username is not None:
                username = str(username)
        ip_addr = ip or _ip_from_request()
        ua = user_agent
        try:
            if ua is None:
                ua = request.headers.get("User-Agent")  # type: ignore[attr-defined]
        except Exception:
            ua = user_agent

        payload_meta = _safe_json(meta)
        before_json = _safe_json(before)
        after_json = _safe_json(after)

        with SessionLocal() as s:
            s.add(
                AuditLog(
                    actor_user_id=actor_user_id,
                    username=username,
                    action=action,
                    ip=ip_addr,
                    user_agent=ua,
                    target_user_id=target_user_id,
                    meta=payload_meta,
                    before_json=before_json,
                    after_json=after_json,
                )
            )
            s.commit()
    except Exception:
        # Never break app for audit failures
        pass


def log_audit_change(
    user: Any,
    action: str,
    *,
    target_user_id: Optional[int] = None,
    before: Any = None,
    after: Any = None,
    meta: Optional[dict] = None,
) -> None:
    log_audit(user, action, meta=meta, target_user_id=target_user_id, before=before, after=after)
