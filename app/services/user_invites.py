from __future__ import annotations

from flask import render_template

from .mailer import send_email


INVITE_SUBJECT = "Your Wholesale Analytics account is ready"
RESET_SUBJECT = "Reset your Wholesale Analytics password"


def _subject_for_purpose(purpose: str) -> str:
    token = str(purpose or "").strip().lower()
    if token == "invite":
        return INVITE_SUBJECT
    return RESET_SUBJECT


def render_password_email(
    *,
    recipient_name: str | None,
    set_password_link: str,
    purpose: str,
) -> tuple[str, str, str]:
    subject = _subject_for_purpose(purpose)
    text_body = render_template(
        "emails/invite_user.txt",
        recipient_name=recipient_name,
        set_password_link=set_password_link,
        purpose=purpose,
    )
    html_body = render_template(
        "emails/invite_user.html",
        recipient_name=recipient_name,
        set_password_link=set_password_link,
        purpose=purpose,
    )
    return subject, text_body, html_body


def send_password_email(
    *,
    to_email: str,
    recipient_name: str | None,
    set_password_link: str,
    purpose: str,
) -> bool:
    subject, text_body, html_body = render_password_email(
        recipient_name=recipient_name,
        set_password_link=set_password_link,
        purpose=purpose,
    )
    return send_email(to_email, subject, text_body, html_body=html_body, raise_on_error=False)

