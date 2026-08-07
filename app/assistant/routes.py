from __future__ import annotations

from io import BytesIO
from typing import Any, Dict

from flask import Blueprint, abort, jsonify, render_template, request, send_file
from flask_login import current_user, login_required

from app.core import rbac
from app.core.audit import log_audit

from .export_store import get_export
from .service import (
    assistant_enabled,
    assistant_health,
    create_digest_schedule,
    delete_digest_schedule,
    get_export_job_status,
    get_proactive_feed,
    get_thread_state,
    handle_chat,
    initial_context,
    list_digest_schedules,
    run_digest_schedule,
    suggested_prompts,
)


bp = Blueprint("assistant", __name__)


def _has_assistant_access() -> bool:
    return bool(
        rbac.user_has_any_permission(
            current_user,
            "page.overview.view",
            "page.customers.view",
            "page.products.view",
            "page.regions.view",
            "page.suppliers.view",
            "page.salesreps.view",
            "page.returns.view",
            "page.admin.view",
            "page.notifications.view",
        )
    )


def _guard_access():
    if not assistant_enabled():
        return jsonify({"status": "disabled", "error": "assistant_disabled"}), 404
    if not _has_assistant_access():
        return jsonify({"status": "forbidden", "error": "no_module_access"}), 403
    return None


@bp.get("/assistant")
@login_required
def assistant_page():
    if not assistant_enabled():
        abort(404)
    if not _has_assistant_access():
        abort(403)
    ref_path = str(request.args.get("ref") or "").strip()
    page_hint = str(request.args.get("page") or "").strip()
    ctx = initial_context(page_hint=page_hint, ref_path=ref_path)
    return render_template(
        "assistant/index.html",
        assistant_context=ctx,
    )


@bp.get("/ai/health")
@login_required
def assistant_health_api_v2():
    if not _has_assistant_access():
        return jsonify({"status": "forbidden", "error": "no_module_access"}), 403
    return jsonify(assistant_health())


@bp.get("/api/assistant/health")
@login_required
def assistant_health_api():
    return assistant_health_api_v2()


@bp.get("/ai/context")
@login_required
def assistant_context_api_v2():
    guard = _guard_access()
    if guard is not None:
        return guard
    ref_path = str(request.args.get("ref") or "").strip()
    page_hint = str(request.args.get("page") or "").strip()
    return jsonify({"status": "ok", "context": initial_context(page_hint=page_hint, ref_path=ref_path)})


@bp.get("/api/assistant/context")
@login_required
def assistant_context_api():
    return assistant_context_api_v2()


@bp.get("/ai/suggestions")
@login_required
def assistant_suggestions_api_v2():
    guard = _guard_access()
    if guard is not None:
        return guard
    ref_path = str(request.args.get("ref") or "").strip()
    page_hint = str(request.args.get("page") or "").strip()
    return jsonify({"status": "ok", "suggestions": suggested_prompts(page_hint=page_hint, ref_path=ref_path)})


@bp.get("/api/assistant/suggestions")
@login_required
def assistant_suggestions_api():
    return assistant_suggestions_api_v2()


@bp.get("/ai/proactive")
@login_required
def assistant_proactive_api_v2():
    guard = _guard_access()
    if guard is not None:
        return guard
    ref_path = str(request.args.get("ref") or "").strip()
    page_hint = str(request.args.get("page") or "").strip()
    triggered_by = str(request.args.get("triggered_by") or "page_load").strip()
    payload: Dict[str, Any] = {
        "context": {
            "page": page_hint,
            "ref_path": ref_path,
        },
        "triggered_by": triggered_by,
    }
    out = get_proactive_feed(payload)
    status = str(out.get("status") or "ok").strip().lower()
    code = 200
    if status == "error":
        code = 400
    elif status == "disabled":
        code = 404
    elif status == "forbidden":
        code = 403
    return jsonify(out), code


@bp.get("/api/assistant/proactive")
@login_required
def assistant_proactive_api():
    return assistant_proactive_api_v2()


@bp.get("/ai/digest/schedules")
@login_required
def assistant_digest_schedules_list_api_v2():
    guard = _guard_access()
    if guard is not None:
        return guard
    out = list_digest_schedules(request.args.to_dict(flat=True))
    status = str(out.get("status") or "ok").strip().lower()
    code = 200
    if status == "error":
        code = 400
    elif status == "disabled":
        code = 404
    elif status == "forbidden":
        code = 403
    return jsonify(out), code


@bp.get("/api/assistant/digest/schedules")
@login_required
def assistant_digest_schedules_list_api():
    return assistant_digest_schedules_list_api_v2()


@bp.post("/ai/digest/schedules")
@login_required
def assistant_digest_schedules_create_api_v2():
    guard = _guard_access()
    if guard is not None:
        return guard
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    out = create_digest_schedule(payload)
    status = str(out.get("status") or "ok").strip().lower()
    code = 200
    if status == "error":
        code = 400
    elif status == "disabled":
        code = 404
    elif status == "forbidden":
        code = 403
    return jsonify(out), code


@bp.post("/api/assistant/digest/schedules")
@login_required
def assistant_digest_schedules_create_api():
    return assistant_digest_schedules_create_api_v2()


@bp.post("/ai/digest/schedules/<schedule_id>/run")
@login_required
def assistant_digest_schedule_run_api_v2(schedule_id: str):
    guard = _guard_access()
    if guard is not None:
        return guard
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    out = run_digest_schedule(schedule_id, payload)
    status = str(out.get("status") or "ok").strip().lower()
    code = 200
    if status == "error":
        code = 400
    elif status == "disabled":
        code = 404
    elif status == "forbidden":
        code = 403
    return jsonify(out), code


@bp.post("/api/assistant/digest/schedules/<schedule_id>/run")
@login_required
def assistant_digest_schedule_run_api(schedule_id: str):
    return assistant_digest_schedule_run_api_v2(schedule_id)


@bp.delete("/ai/digest/schedules/<schedule_id>")
@login_required
def assistant_digest_schedule_delete_api_v2(schedule_id: str):
    guard = _guard_access()
    if guard is not None:
        return guard
    out = delete_digest_schedule(schedule_id, {})
    status = str(out.get("status") or "ok").strip().lower()
    code = 200
    if status == "error":
        code = 400
    elif status == "disabled":
        code = 404
    elif status == "forbidden":
        code = 403
    return jsonify(out), code


@bp.delete("/api/assistant/digest/schedules/<schedule_id>")
@login_required
def assistant_digest_schedule_delete_api(schedule_id: str):
    return assistant_digest_schedule_delete_api_v2(schedule_id)


@bp.post("/ai/chat")
@login_required
def assistant_chat_api_v2():
    guard = _guard_access()
    if guard is not None:
        return guard
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    out = handle_chat(payload)
    status = str(out.get("status") or "ok").strip().lower()
    code = 200
    if status == "error":
        code = 400
    elif status == "disabled":
        code = 404
    return jsonify(out), code


@bp.post("/api/assistant/chat")
@login_required
def assistant_chat_api():
    return assistant_chat_api_v2()


@bp.get("/ai/thread/<thread_id>")
@login_required
def assistant_thread_api_v2(thread_id: str):
    guard = _guard_access()
    if guard is not None:
        return guard
    out = get_thread_state(thread_id)
    code = 400 if str(out.get("status") or "").lower() == "error" else 200
    return jsonify(out), code


@bp.get("/api/assistant/thread/<thread_id>")
@login_required
def assistant_thread_api(thread_id: str):
    return assistant_thread_api_v2(thread_id)


@bp.get("/ai/exports/<export_id>/download")
@login_required
def assistant_export_download_api_v2(export_id: str):
    guard = _guard_access()
    if guard is not None:
        return guard
    artifact = get_export(getattr(current_user, "id", "anon"), export_id)
    if artifact is None:
        return jsonify({"status": "error", "error": "export_not_found", "message": "Export link is invalid or expired."}), 404
    try:
        log_audit(
            current_user,
            "assistant.export.download",
            meta={
                "export_id": artifact.export_id,
                "filename": artifact.filename,
                "content_type": artifact.content_type,
                "meta": dict(artifact.meta or {}),
            },
            target_user_id=getattr(current_user, "id", None),
        )
    except Exception:
        pass
    return send_file(
        BytesIO(artifact.data),
        as_attachment=True,
        download_name=artifact.filename,
        mimetype=artifact.content_type,
    )


@bp.get("/api/assistant/exports/<export_id>/download")
@login_required
def assistant_export_download_api(export_id: str):
    return assistant_export_download_api_v2(export_id)


@bp.get("/ai/exports/jobs/<job_id>")
@login_required
def assistant_export_job_status_api_v2(job_id: str):
    guard = _guard_access()
    if guard is not None:
        return guard
    out = get_export_job_status(job_id)
    status = str(out.get("status") or "ok").strip().lower()
    code = 200
    if status == "error":
        code = 404
    elif status == "disabled":
        code = 404
    return jsonify(out), code


@bp.get("/api/assistant/exports/jobs/<job_id>")
@login_required
def assistant_export_job_status_api(job_id: str):
    return assistant_export_job_status_api_v2(job_id)
