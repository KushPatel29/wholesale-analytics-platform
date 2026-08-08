import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func

from app import create_app
from app.auth.models import PasswordResetToken, SessionLocal, User
from app.auth.password_tokens import hash_password_token, issue_password_token
from app.services.mailer import send_email
from app.services.user_invites import INVITE_SUBJECT, render_password_email


@pytest.fixture()
def app():
    os.environ.setdefault("FLASK_ENV", "development")
    os.environ.setdefault("WTF_CSRF_ENABLED", "false")
    os.environ.setdefault("WA_FAST_PWHASH", "1")
    app = create_app()
    app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,
        SECRET_KEY="test-secret",
        LOGIN_DISABLED=False,
        AUTHZ_DISABLED=False,
        INVITES_ENABLED=True,
        SMTP_SERVER="relay.example.com",
        SMTP_PORT=25,
        SMTP_USE_TLS=True,
        MAIL_FROM="Northgate Retail Analytics <no-reply@northgateretail.example.com>",
        APP_PUBLIC_BASE_URL="https://analytics.example.com",
        RESET_TOKEN_TTL_SECONDS=86400,
    )
    return app


@pytest.fixture()
def client(app):
    with app.test_client() as c:
        yield c


def _login_admin(client):
    suffix = uuid.uuid4().hex[:10]
    username = f"admin_{suffix}"
    password = "AdminPass1!word"
    with SessionLocal() as s:
        user = User(
            username=username,
            email=f"{username}@example.com",
            role="admin",
            is_active=True,
            is_approved=True,
            sales_rep_id=f"ERP-{suffix}",
            erp_user_id=f"ERP-{suffix}",
            updated_at=datetime.now(timezone.utc),
        )
        user.set_password(password)
        s.add(user)
        s.commit()

    resp = client.post("/auth/login", data={"username": username, "password": password}, follow_redirects=True)
    assert resp.status_code == 200
    probe = client.get("/api/_admin/users?page=1&page_size=1")
    assert probe.status_code == 200, probe.get_data(as_text=True)[:200]


def _create_user(*, email: str | None = None, is_active: bool = True, is_approved: bool = True, password: str = "OldPass1!word") -> int:
    suffix = uuid.uuid4().hex[:10]
    username = f"invite_{suffix}"
    erp_user_id = f"ERP-{suffix}"
    with SessionLocal() as s:
        user = User(
            username=username,
            email=email or f"{username}@example.com",
            role="sales",
            is_active=is_active,
            is_approved=is_approved,
            sales_rep_id=erp_user_id,
            erp_user_id=erp_user_id,
            updated_at=datetime.now(timezone.utc),
        )
        user.set_password(password)
        s.add(user)
        s.commit()
        s.refresh(user)
        return int(user.id)


def test_token_creation_stores_hash_and_expiry(app):
    user_id = _create_user()
    expected_hash = None
    with app.app_context():
        with SessionLocal() as s:
            raw_token = issue_password_token(s, user_id=user_id, purpose="invite", invalidate_existing=True)
            expected_hash = hash_password_token(raw_token)
            s.commit()
            row = (
                s.query(PasswordResetToken)
                .filter(PasswordResetToken.user_id == user_id)
                .order_by(PasswordResetToken.id.desc())
                .first()
            )

    assert row is not None
    assert row.token_hash != raw_token
    assert len(row.token_hash) == 64
    assert row.token_hash == expected_hash
    assert row.expires_at > row.created_at
    assert row.used_at is None
    assert row.purpose == "invite"


def test_get_set_password_valid_token_returns_200(app, client):
    user_id = _create_user()
    with app.app_context():
        with SessionLocal() as s:
            raw_token = issue_password_token(s, user_id=user_id, purpose="invite", invalidate_existing=True)
            s.commit()

    resp = client.get(f"/auth/set-password/{raw_token}")
    assert resp.status_code == 200
    assert "Set your password" in resp.get_data(as_text=True)


def test_post_set_password_consumes_token_and_reuse_fails(app, client):
    old_password = "OldPass1!word"
    new_password = "BrandNew1!word"
    user_id = _create_user(password=old_password, is_active=True, is_approved=True)
    with app.app_context():
        with SessionLocal() as s:
            raw_token = issue_password_token(s, user_id=user_id, purpose="reset", invalidate_existing=True)
            token_hash = hash_password_token(raw_token)
            s.commit()

    resp = client.post(
        f"/auth/set-password/{raw_token}",
        data={"password": new_password, "confirm": new_password},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/dashboard/" in (resp.headers.get("Location") or "")

    with SessionLocal() as s:
        user = s.get(User, user_id)
        assert user is not None
        assert user.check_password(new_password)
        row = s.query(PasswordResetToken).filter(PasswordResetToken.token_hash == token_hash).first()
        assert row is not None
        assert row.used_at is not None

    second = client.post(
        f"/auth/set-password/{raw_token}",
        data={"password": "Another1!word", "confirm": "Another1!word"},
        follow_redirects=False,
    )
    assert second.status_code == 302
    assert "/auth/login" in (second.headers.get("Location") or "")


def test_invite_email_render_contains_absolute_link(app):
    link = "https://analytics.example.com/auth/set-password/token123"
    with app.app_context():
        subject, text_body, html_body = render_password_email(
            recipient_name="Taylor User",
            set_password_link=link,
            purpose="invite",
        )
    assert subject == INVITE_SUBJECT
    assert link in text_body
    assert link in html_body


def test_mailer_falls_back_when_starttls_fails_without_login(monkeypatch, app):
    calls = {"starttls": 0, "send_message": 0, "login": 0}

    class FakeSMTP:
        def __init__(self, host=None, port=None, timeout=None):
            self.host = host
            self.port = port
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def ehlo_or_helo_if_needed(self):
            return None

        def starttls(self, context=None):
            _ = context
            calls["starttls"] += 1
            raise RuntimeError("STARTTLS not available")

        def send_message(self, message):
            _ = message
            calls["send_message"] += 1

        def login(self, user, password):
            _ = (user, password)
            calls["login"] += 1
            raise AssertionError("login() must not be called")

    monkeypatch.setattr("app.services.mailer.smtplib.SMTP", FakeSMTP)

    with app.app_context():
        ok = send_email("person@example.com", "Subject", "Body")

    assert ok is True
    assert calls["starttls"] == 1
    assert calls["send_message"] == 1
    assert calls["login"] == 0


def test_send_email_supports_attachments(monkeypatch, app):
    captured = {}

    def _fake_send(message):
        captured["message"] = message

    monkeypatch.setattr("app.services.mailer._send_message", _fake_send)

    with app.app_context():
        ok = send_email(
            "person@example.com",
            "Subject",
            "Body",
            html_body="<p>Body</p>",
            attachments=[
                {
                    "filename": "alerts.xlsx",
                    "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "data": b"test-bytes",
                }
            ],
        )

    assert ok is True
    message = captured["message"]
    attachments = list(message.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "alerts.xlsx"
    assert attachments[0].get_content_type() == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_admin_create_user_sends_invite_and_stores_token(client, monkeypatch):
    _login_admin(client)
    monkeypatch.setattr("app.blueprints.admin_api._send_password_email_for_user", lambda **kwargs: True)
    suffix = uuid.uuid4().hex[:10]
    email = f"invite_create_{suffix}@example.com"
    payload = {
        "email": email,
        "first_name": "Create",
        "last_name": "Invite",
        "role": "sales",
        "approve": True,
        "erp_user_id": f"ERP-{suffix}",
        "visibility": [],
        "scope": {"sales_rep_ids": []},
    }
    resp = client.post("/api/_admin/users", json=payload)
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["invite_sent"] is True
    assert body["invite_error"] is None

    with SessionLocal() as s:
        user = s.query(User).filter(func.lower(User.email) == email.lower()).first()
        assert user is not None
        assert bool(user.must_reset_password) is True
        token = (
            s.query(PasswordResetToken)
            .filter(PasswordResetToken.user_id == int(user.id), PasswordResetToken.purpose == "invite")
            .order_by(PasswordResetToken.id.desc())
            .first()
        )
        assert token is not None
        assert len(token.token_hash) == 64
        assert token.used_at is None


def test_resend_invite_invalidates_previous_unused_tokens(client, monkeypatch):
    _login_admin(client)
    monkeypatch.setattr("app.blueprints.admin_api._send_password_email_for_user", lambda **kwargs: True)
    user_id = _create_user(email=f"resend_{uuid.uuid4().hex[:10]}@example.com")

    first = client.post(f"/api/_admin/users/{user_id}/resend-invite")
    assert first.status_code == 200
    second = client.post(f"/api/_admin/users/{user_id}/resend-invite")
    assert second.status_code == 200

    with SessionLocal() as s:
        rows = (
            s.query(PasswordResetToken)
            .filter(PasswordResetToken.user_id == user_id, PasswordResetToken.purpose == "invite")
            .order_by(PasswordResetToken.id.asc())
            .all()
        )
        assert len(rows) >= 2
        assert rows[-1].used_at is None
        assert rows[-2].used_at is not None
