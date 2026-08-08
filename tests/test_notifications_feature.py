from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser

from app.auth.models import SessionLocal, User
from app.auth.notifications_models import NotificationEvent, UserNotificationPreference
from app.services import notifications as notifications_service


def _create_user(*, username_prefix: str, role: str, email: str | None, erp_user_id: str | None = None) -> User:
    username = f"{username_prefix}_{uuid.uuid4().hex[:8]}"
    if email and "@" in email:
        local, domain = email.split("@", 1)
        email = f"{local}+{uuid.uuid4().hex[:6]}@{domain}"
    if erp_user_id:
        erp_user_id = f"{erp_user_id}-{uuid.uuid4().hex[:6]}"
    with SessionLocal() as s:
        user = User(
            username=username,
            email=email,
            role=role,
            erp_user_id=erp_user_id,
            sales_rep_id=erp_user_id,
            is_active=True,
            is_approved=True,
            must_reset_password=False,
        )
        user.set_password("test")
        s.add(user)
        s.commit()
        s.refresh(user)
        s.expunge(user)
        return user


class _NotificationsFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.label_fors: set[str] = set()
        self.controls: list[dict[str, object]] = []
        self._label_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key: value for key, value in attrs}
        if tag == "label":
            target = str(attr_map.get("for") or "").strip()
            if target:
                self.label_fors.add(target)
            self._label_depth += 1
            return
        if tag not in {"input", "select", "textarea"}:
            return
        input_type = str(attr_map.get("type") or "").strip().lower()
        if tag == "input" and input_type in {"hidden", "submit", "button", "image", "reset"}:
            return
        self.controls.append(
            {
                "tag": tag,
                "name": str(attr_map.get("name") or "").strip(),
                "id": str(attr_map.get("id") or "").strip(),
                "class": str(attr_map.get("class") or "").strip(),
                "wrapped": self._label_depth > 0,
                "aria_label": str(attr_map.get("aria-label") or "").strip(),
                "aria_labelledby": str(attr_map.get("aria-labelledby") or "").strip(),
            }
        )

    def handle_endtag(self, tag: str) -> None:
        if tag == "label" and self._label_depth > 0:
            self._label_depth -= 1


def test_notifications_page_respects_flag(app, client):
    app.config["NOTIFICATIONS_ENABLED"] = False
    off = client.get("/notifications")
    assert off.status_code == 404

    app.config["NOTIFICATIONS_ENABLED"] = True
    on = client.get("/notifications")
    assert on.status_code == 200
    assert b"Alert controls that match your production scope" in on.data
    assert b"customer_revenue_drop_enabled" in on.data
    assert b"customer_revenue_drop_percent_drop" in on.data
    assert b"Send all real-time samples" in on.data


def test_notifications_page_labels_all_visible_form_controls(app, client):
    app.config["NOTIFICATIONS_ENABLED"] = True
    resp = client.get("/notifications")
    assert resp.status_code == 200

    parser = _NotificationsFormParser()
    parser.feed(resp.get_data(as_text=True))

    assert parser.controls
    ids = [str(control.get("id") or "") for control in parser.controls]
    duplicate_ids = sorted({control_id for control_id in ids if control_id and ids.count(control_id) > 1})
    missing_ids = [
        control.get("name") or control.get("tag")
        for control in parser.controls
        if not str(control.get("id") or "").strip()
    ]
    unlabeled = [
        str(control.get("id") or "")
        for control in parser.controls
        if str(control.get("id") or "").strip()
        and not bool(control.get("wrapped"))
        and str(control.get("id") or "") not in parser.label_fors
        and not str(control.get("aria_label") or "").strip()
        and not str(control.get("aria_labelledby") or "").strip()
    ]
    controls_missing_explicit_name = [
        str(control.get("id") or "")
        for control in parser.controls
        if "form-check-input" in str(control.get("class") or "")
        or "form-select" in str(control.get("class") or "")
        or "form-control" in str(control.get("class") or "")
        if not str(control.get("aria_label") or "").strip()
        and not str(control.get("aria_labelledby") or "").strip()
    ]

    assert not duplicate_ids
    assert not missing_ids
    assert not unlabeled
    assert not controls_missing_explicit_name


def test_notification_preference_save_and_load(app):
    app.config["NOTIFICATIONS_ENABLED"] = True
    with app.app_context():
        user = _create_user(username_prefix="notif_sales", role="sales", email="sales@example.com", erp_user_id="REP-100")
        saved = notifications_service.save_notification_preferences(
            user,
            {
                "data_freshness_sla_enabled": "1",
                "data_freshness_sla_frequency": "weekly",
                "data_freshness_sla_scope_mode": "rbac",
                "data_freshness_sla_max_staleness_days": "3",
                "data_freshness_sla_cooldown_hours": "18",
            },
        )
        assert saved == 1

        settings = notifications_service.get_notification_settings_for_user(user)["by_key"]["data_freshness_sla"]
        assert settings["enabled"] is True
        assert settings["frequency"] == "weekly"
        assert settings["scope_mode"] == "self"
        assert settings["thresholds"]["max_staleness_days"] == 3
        assert settings["thresholds"]["cooldown_hours"] == 18

        with SessionLocal() as s:
            pref = (
                s.query(UserNotificationPreference)
                .filter(
                    UserNotificationPreference.user_id == int(user.id),
                    UserNotificationPreference.type_key == "data_freshness_sla",
                )
                .one()
            )
            payload = json.loads(pref.config_json)
        assert payload["scope_mode"] == "self"


def test_planned_alert_preferences_can_be_saved(app):
    app.config["NOTIFICATIONS_ENABLED"] = True
    with app.app_context():
        user = _create_user(username_prefix="notif_plan", role="sales_manager", email="plan@example.com", erp_user_id="REP-200")
        saved = notifications_service.save_notification_preferences(
            user,
            {
                "customer_revenue_drop_enabled": "1",
                "customer_revenue_drop_frequency": "weekly",
                "customer_revenue_drop_scope_mode": "rbac",
                "customer_revenue_drop_percent_drop": "35",
                "customer_revenue_drop_min_dollar_impact": "7500",
                "customer_revenue_drop_lookback_months": "2",
                "customer_revenue_drop_cooldown_hours": "30",
            },
            type_keys=["customer_revenue_drop"],
        )
        assert saved == 1

        settings = notifications_service.notification_setting_for_user(user, "customer_revenue_drop")
        assert settings is not None
        assert settings["enabled"] is True
        assert settings["frequency"] == "weekly"
        assert settings["scope_mode"] == "rbac"
        assert settings["thresholds"]["percent_drop"] == 35
        assert settings["thresholds"]["min_dollar_impact"] == 7500
        assert settings["thresholds"]["lookback_months"] == 2
        assert settings["thresholds"]["cooldown_hours"] == 30

        with SessionLocal() as s:
            pref = (
                s.query(UserNotificationPreference)
                .filter(
                    UserNotificationPreference.user_id == int(user.id),
                    UserNotificationPreference.type_key == "customer_revenue_drop",
                )
                .one()
            )
            payload = json.loads(pref.config_json)
        assert payload["scope_mode"] == "rbac"


def test_notification_event_hash_dedupes(app):
    app.config["NOTIFICATIONS_ENABLED"] = True
    with app.app_context():
        user = _create_user(username_prefix="notif_admin", role="admin", email="admin@example.com")
        first, created_first = notifications_service.create_notification_event(
            type_key="data_freshness_sla",
            user_id=int(user.id),
            event_hash="abc123",
            payload={"summary": "stale"},
            window_start=None,
            window_end=None,
        )
        second, created_second = notifications_service.create_notification_event(
            type_key="data_freshness_sla",
            user_id=int(user.id),
            event_hash="abc123",
            payload={"summary": "stale"},
            window_start=None,
            window_end=None,
        )

        assert created_first is True
        assert created_second is False
        assert first["id"] == second["id"]

        with SessionLocal() as s:
            count = (
                s.query(NotificationEvent)
                .filter(NotificationEvent.user_id == int(user.id), NotificationEvent.type_key == "data_freshness_sla")
                .count()
            )
        assert count == 1


def test_notification_email_rendering(app):
    app.config["NOTIFICATIONS_ENABLED"] = True
    with app.app_context():
        user = _create_user(username_prefix="notif_render", role="sales", email="render@example.com", erp_user_id="REP-10")
        subject, text_body, html_body = notifications_service.render_single_alert_email(
            user,
            "data_freshness_sla",
            {
                "summary": "Fact data is stale.",
                "details": [{"label": "Age", "value": "2 day(s)"}],
            },
        )

        assert "Northgate Retail Analytics Alert" in subject
        assert "Fact data is stale." in text_body
        assert "Open related page" in html_body
        assert "/notifications" in text_body


def test_planned_alert_test_email_renders_and_sends(app, monkeypatch):
    app.config["NOTIFICATIONS_ENABLED"] = True
    deliveries: list[dict[str, object]] = []
    fake_xlsx = b"customer-revenue-drop-xlsx"

    def _fake_send(
        to_email: str,
        subject: str,
        text_body: str,
        html_body: str | None = None,
        *,
        attachments=None,
        **_kwargs,
    ):
        deliveries.append(
            {
                "to_email": to_email,
                "subject": subject,
                "text_body": text_body,
                "html_body": html_body or "",
                "attachments": list(attachments or []),
            }
        )
        return True

    monkeypatch.setattr(notifications_service, "send_email", _fake_send)
    monkeypatch.setattr(notifications_service, "xlsx_export_available", lambda: True)
    monkeypatch.setattr(notifications_service, "dataframes_to_xlsx_bytes", lambda _sheets: fake_xlsx)

    with app.app_context():
        user = _create_user(username_prefix="notif_sample", role="sales_manager", email="sample@example.com", erp_user_id="REP-205")
        ok = notifications_service.send_test_email_for_user(user, "customer_revenue_drop")
        assert ok is True
        assert len(deliveries) == 1
        assert deliveries[0]["to_email"].endswith("@example.com")
        assert "Customer revenue drop" in deliveries[0]["subject"]
        assert "Manage alerts" in deliveries[0]["text_body"]
        attachments = deliveries[0]["attachments"]
        assert isinstance(attachments, list)
        assert len(attachments) == 1
        assert attachments[0]["filename"] == "wholesale_alert_customer_revenue_drop.xlsx"
        assert attachments[0]["mimetype"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        assert attachments[0]["data"] == fake_xlsx


def test_batch_test_email_uses_immediate_filter_and_override(app, monkeypatch):
    app.config["NOTIFICATIONS_ENABLED"] = True
    deliveries: list[dict[str, object]] = []
    fake_xlsx = b"batched-alert-xlsx"

    def _fake_send(
        to_email: str,
        subject: str,
        text_body: str,
        html_body: str | None = None,
        *,
        attachments=None,
        **_kwargs,
    ):
        deliveries.append(
            {
                "to_email": to_email,
                "subject": subject,
                "text_body": text_body,
                "attachments": list(attachments or []),
            }
        )
        return True

    monkeypatch.setattr(notifications_service, "send_email", _fake_send)
    monkeypatch.setattr(notifications_service, "xlsx_export_available", lambda: True)
    monkeypatch.setattr(notifications_service, "dataframes_to_xlsx_bytes", lambda _sheets: fake_xlsx)

    with app.app_context():
        user = _create_user(username_prefix="notif_batch", role="sales", email="batch@example.com", erp_user_id="REP-209")
        sent = notifications_service.send_test_emails_for_user(
            user,
            immediate_only=True,
            override_email="kush@wholesaleprovisions.example.com",
        )
        assert sent == 2
        assert len(deliveries) == 2
        assert {item["to_email"] for item in deliveries} == {"kush@wholesaleprovisions.example.com"}
        assert all(len(item["attachments"]) == 1 for item in deliveries)
        assert all(item["attachments"][0]["data"] == fake_xlsx for item in deliveries)


def test_runner_creates_sends_and_dedupes_data_freshness(app, monkeypatch):
    app.config["NOTIFICATIONS_ENABLED"] = True
    app.config["NOTIFICATIONS_MAX_EMAILS_PER_HOUR"] = 10
    sent_messages: list[dict[str, object]] = []

    def _fake_send(
        to_email: str,
        subject: str,
        text_body: str,
        html_body: str | None = None,
        *,
        attachments=None,
        **_kwargs,
    ):
        sent_messages.append(
            {
                "to_email": to_email,
                "subject": subject,
                "text_body": text_body,
                "html_body": html_body or "",
                "attachments": list(attachments or []),
            }
        )
        return True

    stale_manifest = {
        "watermark": "2026-02-28",
        "dataset_version": "notif-test-v1",
        "last_refresh_utc": "2026-03-01T05:00:00+00:00",
    }
    monkeypatch.setattr(notifications_service.fact_store, "get_meta", lambda: stale_manifest)

    with app.app_context():
        user = _create_user(username_prefix="notif_runner", role="sales", email="runner@example.com", erp_user_id="REP-55")
        notifications_service.save_notification_preferences(
            user,
            {
                "data_freshness_sla_enabled": "1",
                "data_freshness_sla_frequency": "immediate",
                "data_freshness_sla_scope_mode": "self",
                "data_freshness_sla_max_staleness_days": "1",
                "data_freshness_sla_cooldown_hours": "12",
            },
        )

        now = datetime(2026, 3, 3, 12, 0, tzinfo=timezone.utc)
        first = notifications_service.run_notification_cycle(now=now, send_email_func=_fake_send)
        assert first["events_created"] >= 1
        assert first["emails_sent"] >= 1
        assert len(sent_messages) >= 1
        runner_messages = [item for item in sent_messages if item["to_email"] == str(user.email)]
        assert len(runner_messages) == 1
        assert len(runner_messages[0]["attachments"]) == 1
        assert runner_messages[0]["attachments"][0]["filename"] == "wholesale_alert_data_freshness_sla.xlsx"

        with SessionLocal() as s:
            event = (
                s.query(NotificationEvent)
                .filter(NotificationEvent.user_id == int(user.id), NotificationEvent.type_key == "data_freshness_sla")
                .one()
            )
            payload = json.loads(event.event_payload_json)
            assert event.status == "sent"
            assert payload["scope_snapshot"]["notification_scope_mode"] == "self"
            assert payload["scope_snapshot"]["allowed_erp_user_ids"] == [str(user.erp_user_id).lower()]

        second = notifications_service.run_notification_cycle(now=now, send_email_func=_fake_send)
        assert second["events_created"] == 0
        assert second["events_deduped"] >= 1
        assert second["emails_sent"] == 0
        runner_messages_after = [item for item in sent_messages if item["to_email"] == str(user.email)]
        assert len(runner_messages_after) == 1
