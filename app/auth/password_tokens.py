from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Tuple

from flask import current_app, request, url_for
from sqlalchemy.orm import Session

from .models import PasswordResetToken, User


TOKEN_PURPOSES = {"invite", "reset"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_purpose(purpose: str) -> str:
    token = str(purpose or "").strip().lower()
    if token not in TOKEN_PURPOSES:
        raise ValueError("purpose must be 'invite' or 'reset'")
    return token


def _token_pepper() -> str:
    pepper = current_app.config.get("RESET_TOKEN_PEPPER")
    if pepper:
        return str(pepper)
    return str(current_app.config.get("SECRET_KEY") or "")


def reset_token_ttl_seconds() -> int:
    try:
        ttl = int(current_app.config.get("RESET_TOKEN_TTL_SECONDS") or 86400)
    except Exception:
        ttl = 86400
    return max(300, ttl)


def hash_password_token(raw_token: str) -> str:
    payload = f"{raw_token}{_token_pepper()}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def request_ip() -> str | None:
    try:
        fwd = request.headers.get("X-Forwarded-For")
        if fwd:
            return fwd.split(",")[0].strip() or None
        addr = (request.remote_addr or "").strip()
        return addr or None
    except Exception:
        return None


def request_user_agent() -> str | None:
    try:
        ua = (request.headers.get("User-Agent") or "").strip()
        return ua[:255] if ua else None
    except Exception:
        return None


def build_set_password_link(raw_token: str) -> str:
    path = url_for("auth.set_password", token=raw_token)
    base_url = str(current_app.config.get("APP_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if base_url:
        return f"{base_url}{path}"
    return url_for("auth.set_password", token=raw_token, _external=True)


def invalidate_password_tokens(
    session: Session,
    *,
    user_id: int,
    purpose: str | None = None,
) -> int:
    now = _now()
    query = session.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == int(user_id),
        PasswordResetToken.used_at.is_(None),
        PasswordResetToken.expires_at > now,
    )
    if purpose:
        query = query.filter(PasswordResetToken.purpose == _canonical_purpose(purpose))
    return int(
        query.update(
            {
                PasswordResetToken.used_at: now,
                PasswordResetToken.expires_at: now,
            },
            synchronize_session=False,
        )
        or 0
    )


def issue_password_token(
    session: Session,
    *,
    user_id: int,
    purpose: str,
    created_by_user_id: int | None = None,
    request_ip_addr: str | None = None,
    request_user_agent_value: str | None = None,
    invalidate_existing: bool = False,
    ttl_seconds: int | None = None,
) -> str:
    now = _now()
    ttl = max(300, int(ttl_seconds or reset_token_ttl_seconds()))
    canonical_purpose = _canonical_purpose(purpose)
    if invalidate_existing:
        invalidate_password_tokens(session, user_id=user_id, purpose=canonical_purpose)

    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_password_token(raw_token)
    session.add(
        PasswordResetToken(
            user_id=int(user_id),
            token_hash=token_hash,
            purpose=canonical_purpose,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl),
            used_at=None,
            created_by_user_id=int(created_by_user_id) if created_by_user_id else None,
            request_ip=(request_ip_addr or "")[:64] or None,
            request_user_agent=(request_user_agent_value or "")[:255] or None,
        )
    )
    return raw_token


def get_valid_password_token(
    session: Session,
    raw_token: str,
    *,
    purpose: str | None = None,
) -> PasswordResetToken | None:
    token_hash = hash_password_token(raw_token)
    now = _now()
    query = session.query(PasswordResetToken).filter(
        PasswordResetToken.token_hash == token_hash,
        PasswordResetToken.used_at.is_(None),
        PasswordResetToken.expires_at > now,
    )
    if purpose:
        query = query.filter(PasswordResetToken.purpose == _canonical_purpose(purpose))
    return query.order_by(PasswordResetToken.id.desc()).first()


def consume_password_token(
    session: Session,
    raw_token: str,
    *,
    new_password: str,
) -> Tuple[User | None, PasswordResetToken | None]:
    now = _now()
    token = get_valid_password_token(session, raw_token)
    if token is None:
        return None, None

    user = session.get(User, int(token.user_id))
    if user is None:
        return None, None

    claimed = (
        session.query(PasswordResetToken)
        .filter(
            PasswordResetToken.id == int(token.id),
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
        .update({PasswordResetToken.used_at: now}, synchronize_session=False)
    )
    if int(claimed or 0) != 1:
        return None, None

    user.set_password(new_password)
    user.must_reset_password = False
    user.updated_at = now
    session.add(user)
    return user, token

