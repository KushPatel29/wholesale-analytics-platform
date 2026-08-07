from __future__ import annotations

import json
from urllib.parse import urlparse

from flask import Blueprint, request, redirect, url_for, session, flash
from flask_login import current_user, login_required

from ..auth.models import save_view, get_saved_view, update_view, delete_view
from ..core.audit import log_audit
from ..services import filters_service
from ..services.filters import (
    ACTIVE_SAVED_VIEW_SESSION_KEY,
    canonical_filters_json,
    capture_filters_from,
    filters_to_store,
    mark_filters_last_applied,
    read_sticky_filters_from_session,
    write_sticky_filters_to_session,
)


bp = Blueprint("views", __name__, url_prefix="/views")


def _redirect_next(default_endpoint: str = "dashboard.index"):
    nxt = request.form.get("next") or request.args.get("next")
    try:
        parsed = urlparse(str(nxt or ""))
    except Exception:
        parsed = None
    if parsed and not parsed.scheme and not parsed.netloc and str(parsed.path or "").startswith("/"):
        return redirect(str(nxt))
    return redirect(url_for(default_endpoint))


def _current_filters_payload() -> dict:
    return read_sticky_filters_from_session(session, user_id=current_user.get_id()) or session.get("filters") or {}


def _can_manage_view(view) -> bool:
    return bool(view) and int(current_user.get_id()) == int(view.user_id)


@bp.route("/save", methods=["POST"])
@login_required
def save_current():
    name = (request.form.get("name") or "").strip()[:120]
    if not name:
        flash("Name is required to save a view.", "warning")
        return _redirect_next()
    filters = capture_filters_from(request.form) or _current_filters_payload()
    scope = filters_service.scope_from_user(current_user)
    validated_filters, validation_meta = filters_service.validate_filters(filters, scope)
    try:
        vid = save_view(int(current_user.get_id()), name, canonical_filters_json(validated_filters))
        session[ACTIVE_SAVED_VIEW_SESSION_KEY] = int(vid)
        try:
            log_audit(current_user, "save_view", {"view_id": vid, "name": name})
        except Exception:
            pass
        flash("View saved.", "success")
        if validation_meta.get("sanitized") and validation_meta.get("filters_notice"):
            flash(str(validation_meta.get("filters_notice")), "warning")
        if validation_meta.get("validation_degraded"):
            flash("Saved view validation was partially degraded. Review the current filter scope.", "warning")
    except Exception:
        flash("Failed to save view.", "danger")
    return _redirect_next()


@bp.route("/load/<int:view_id>", methods=["POST"])
@login_required
def load_view(view_id: int):
    v = get_saved_view(view_id)
    if not v or not _can_manage_view(v):
        flash("View not found.", "warning")
        return _redirect_next()
    try:
        payload = json.loads(v.filters_json or "{}")
        scope = filters_service.scope_from_user(current_user)
        validated_filters, sanitize_meta = filters_service.validate_filters(payload, scope)
        stored_filters = filters_to_store(validated_filters)
        write_sticky_filters_to_session(session, stored_filters, user_id=current_user.get_id())
        session[ACTIVE_SAVED_VIEW_SESSION_KEY] = int(view_id)
        mark_filters_last_applied(session)
        try:
            log_audit(current_user, "load_view", {"view_id": view_id})
        except Exception:
            pass
        flash("View loaded.", "success")
        if sanitize_meta.get("sanitized") and sanitize_meta.get("filters_notice"):
            flash(str(sanitize_meta.get("filters_notice")), "warning")
        if sanitize_meta.get("validation_degraded"):
            flash("Saved view validation was partially degraded. Review the current filter scope.", "warning")
    except Exception:
        flash("Invalid view payload.", "danger")
    return _redirect_next()


@bp.route("/update/<int:view_id>", methods=["POST"])
@login_required
def update_saved(view_id: int):
    v = get_saved_view(view_id)
    if not v or not _can_manage_view(v):
        flash("View not found.", "warning")
        return _redirect_next()
    name = (request.form.get("name") or v.name or "").strip()[:120]
    scope = filters_service.scope_from_user(current_user)
    validated_filters, sanitize_meta = filters_service.validate_filters(
        capture_filters_from(request.form) or _current_filters_payload(),
        scope,
    )
    filters = filters_to_store(validated_filters)
    try:
        next_name = name or v.name
        saved = update_view(int(view_id), name=next_name, filters_json=canonical_filters_json(validated_filters))
        if not saved:
            flash("View not found.", "warning")
            return _redirect_next()
        if filters:
            write_sticky_filters_to_session(session, filters, user_id=current_user.get_id())
            mark_filters_last_applied(session)
        session[ACTIVE_SAVED_VIEW_SESSION_KEY] = int(view_id)
        try:
            log_audit(current_user, "update_view", {"view_id": view_id, "name": next_name})
        except Exception:
            pass
        flash("View updated.", "success")
        if sanitize_meta.get("sanitized") and sanitize_meta.get("filters_notice"):
            flash(str(sanitize_meta.get("filters_notice")), "warning")
        if sanitize_meta.get("validation_degraded"):
            flash("Saved view validation was partially degraded. Review the current filter scope.", "warning")
    except Exception:
        flash("Failed to update view.", "danger")
    return _redirect_next()


@bp.route("/delete/<int:view_id>", methods=["POST"])
@login_required
def delete_saved(view_id: int):
    v = get_saved_view(view_id)
    if not v or not _can_manage_view(v):
        flash("View not found.", "warning")
        return _redirect_next()
    try:
        delete_view(view_id)
        if str(session.get(ACTIVE_SAVED_VIEW_SESSION_KEY) or "") == str(view_id):
            session.pop(ACTIVE_SAVED_VIEW_SESSION_KEY, None)
        try:
            log_audit(current_user, "delete_view", {"view_id": view_id})
        except Exception:
            pass
        flash("View deleted.", "success")
    except Exception:
        flash("Failed to delete view.", "danger")
    return _redirect_next()
