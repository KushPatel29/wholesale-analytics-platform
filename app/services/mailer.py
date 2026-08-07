from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any, Mapping, Optional, Sequence

from flask import current_app


_LOG = logging.getLogger(__name__)


class MailDeliveryError(RuntimeError):
    """Raised when email delivery fails and raise_on_error=True."""


def _smtp_host() -> str:
    return str(current_app.config.get("SMTP_SERVER") or "").strip()


def _smtp_port() -> int:
    try:
        return int(current_app.config.get("SMTP_PORT") or 25)
    except Exception:
        return 25


def _smtp_timeout_seconds() -> int:
    try:
        timeout = int(current_app.config.get("SMTP_TIMEOUT_SECONDS") or 20)
    except Exception:
        timeout = 20
    return max(5, timeout)


def _mail_from() -> str:
    sender = str(current_app.config.get("MAIL_FROM") or "").strip()
    if sender:
        return sender
    return "no-reply@wholesaleprovisions.example.com"


def _smtp_use_tls() -> bool:
    return bool(current_app.config.get("SMTP_USE_TLS", False))


def _send_message(message: EmailMessage) -> None:
    host = _smtp_host()
    if not host:
        raise MailDeliveryError("SMTP_SERVER is not configured")

    port = _smtp_port()
    timeout = _smtp_timeout_seconds()
    use_tls = _smtp_use_tls()

    with smtplib.SMTP(host=host, port=port, timeout=timeout) as smtp:
        smtp.ehlo_or_helo_if_needed()
        if use_tls:
            try:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo_or_helo_if_needed()
            except Exception as exc:
                _LOG.warning(
                    "mailer.starttls_failed_plain_fallback",
                    extra={"smtp_server": host, "smtp_port": port, "error": str(exc)},
                )
        # SMTP relay mode only: do not call smtp.login().
        smtp.send_message(message)


def _attachment_bytes(raw: Any) -> Optional[bytes]:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, bytearray):
        return bytes(raw)
    if isinstance(raw, memoryview):
        return raw.tobytes()
    if isinstance(raw, str):
        return raw.encode("utf-8")
    return None


def _add_attachments(message: EmailMessage, attachments: Optional[Sequence[Mapping[str, Any]]]) -> int:
    added = 0
    for item in attachments or ():
        if not isinstance(item, Mapping):
            continue
        payload = _attachment_bytes(item.get("data"))
        if payload is None:
            continue
        filename = str(item.get("filename") or "").strip() or f"attachment_{added + 1}.bin"
        mimetype = str(item.get("mimetype") or "application/octet-stream").strip()
        maintype, _, subtype = mimetype.partition("/")
        if not maintype or not subtype:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
        added += 1
    return added


def send_email(
    to_email: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
    *,
    attachments: Optional[Sequence[Mapping[str, Any]]] = None,
    raise_on_error: bool = False,
) -> bool:
    message = EmailMessage()
    message["From"] = _mail_from()
    message["To"] = (to_email or "").strip()
    message["Subject"] = (subject or "").strip()
    message.set_content(text_body or "")
    if html_body:
        message.add_alternative(html_body, subtype="html")
    attachment_count = _add_attachments(message, attachments)

    if bool(current_app.config.get("MAIL_SUPPRESS_SEND", False)):
        _LOG.info(
            "mailer.send_skip",
            extra={
                "reason": "suppressed",
                "to_email": message["To"],
                "subject": message["Subject"],
                "attachment_count": attachment_count,
            },
        )
        return True

    try:
        _send_message(message)
        _LOG.info(
            "mailer.send_success",
            extra={"to_email": message["To"], "subject": message["Subject"], "attachment_count": attachment_count},
        )
        return True
    except Exception as exc:
        _LOG.exception(
            "mailer.send_failed",
            extra={
                "to_email": message.get("To"),
                "subject": message.get("Subject"),
                "error": str(exc),
                "attachment_count": attachment_count,
            },
        )
        if raise_on_error:
            raise MailDeliveryError(str(exc)) from exc
        return False
