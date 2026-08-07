from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.core.rbac import permission_required
from app.services import notifications as notifications_service


bp = Blueprint("notifications", __name__)


def _require_feature_flag() -> None:
    if not notifications_service.notifications_enabled():
        abort(404)


@bp.route("/notifications", methods=["GET", "POST"])
@login_required
@permission_required("page.notifications.view")
def index():
    _require_feature_flag()
    if request.method == "POST":
        action = str(request.form.get("action") or "save").strip().lower()
        type_key = str(request.form.get("type_key") or "").strip()
        if action in {"test_email", "test_all_samples", "test_all_immediate"}:
            email = str(getattr(current_user, "email", "") or "").strip()
            if not email:
                flash("Add an email address to your account before sending test alerts.", "warning")
            else:
                if action == "test_email":
                    sent_count = 1 if notifications_service.send_test_email_for_user(current_user, type_key) else 0
                else:
                    sent_count = notifications_service.send_test_emails_for_user(
                        current_user,
                        immediate_only=(action == "test_all_immediate"),
                    )
                if sent_count > 0:
                    flash(
                        "Test alert email sent." if sent_count == 1 else f"{sent_count} test alert emails sent.",
                        "success",
                    )
                else:
                    flash("Unable to send the test alert email.", "danger")
        elif action in {"enable", "disable"}:
            form_data = request.form.to_dict(flat=True)
            if type_key:
                form_data[f"{type_key}_enabled"] = "1" if action == "enable" else ""
            saved = notifications_service.save_notification_preferences(
                current_user,
                form_data,
                type_keys=[type_key] if type_key else None,
            )
            if saved:
                flash("Alert enabled." if action == "enable" else "Alert disabled.", "success")
            else:
                flash("Unable to update that alert preference.", "warning")
        else:
            saved = notifications_service.save_notification_preferences(
                current_user,
                request.form,
                type_keys=[type_key] if type_key else None,
            )
            if saved:
                flash("Alert preference saved.", "success")
            else:
                flash("Unable to save that alert preference.", "warning")
        return redirect(url_for("notifications.index"))

    settings = notifications_service.get_notification_settings_for_user(current_user)
    all_items = list(settings["by_key"].values())
    notification_summary = {
        "total": len(all_items),
        "enabled": sum(1 for item in all_items if item.get("enabled")),
        "live": sum(1 for item in all_items if item.get("runner_supported")),
        "config_ready": sum(1 for item in all_items if not item.get("runner_supported")),
        "immediate": sum(1 for item in all_items if item.get("frequency") == "immediate"),
    }
    return render_template(
        "notifications/index.html",
        notification_sections=settings["sections"],
        notification_map=settings["by_key"],
        notification_summary=notification_summary,
        notifications_enabled=True,
    )


@bp.get("/settings/notifications")
@login_required
@permission_required("page.notifications.view")
def notifications_alias():
    _require_feature_flag()
    return redirect(url_for("notifications.index"))


@bp.get("/admin/notifications-defaults")
@login_required
@permission_required("admin.notifications.defaults")
def notifications_defaults():
    _require_feature_flag()
    if not notifications_service.admin_defaults_enabled():
        abort(404)
    rows = notifications_service.list_notification_types()
    return render_template("admin/notifications_defaults.html", notification_types=rows)
