"""HTTP surface for the decision ledger and operational workspaces."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from app.core import rbac
from app.core.audit import log_audit
from app.core.rbac import permission_required

from . import service

bp = Blueprint("decision_ops", __name__)


def _payload() -> dict[str, Any]:
    if request.is_json:
        raw = request.get_json(silent=True) or {}
        return dict(raw) if isinstance(raw, Mapping) else {}
    payload = request.form.to_dict(flat=True)
    for key in ("source_context", "affected_records", "metadata", "before", "after", "duplicate_candidate_keys"):
        value = payload.get(key)
        if not value:
            continue
        try:
            payload[key] = json.loads(value)
        except ValueError:
            payload[key] = {} if key in {"source_context", "metadata", "before", "after"} else []
    return payload


def _is_json_request() -> bool:
    return bool(request.is_json or request.accept_mimetypes.best == "application/json")


def _error(exc: Exception, *, fallback: str):
    if _is_json_request():
        return jsonify({"status": "error", "error": str(exc)}), 400
    flash(str(exc), "danger")
    return redirect(request.referrer or fallback)


def _allowed(permission: str) -> bool:
    if bool(current_app.config.get("LOGIN_DISABLED")) or bool(current_app.config.get("AUTHZ_DISABLED")):
        return True
    return bool(rbac.user_has_permission(current_user, permission))


def _audit(action: str, result: Mapping[str, Any], payload: Mapping[str, Any] | None = None) -> None:
    log_audit(current_user, action, meta={"result_id": result.get("id") or (result.get("action") or {}).get("id"), "payload": dict(payload or {})})


@bp.get("/work")
@bp.get("/work/")
@login_required
@permission_required("page.work.view")
def index():
    listing = service.list_work_items(
        page=request.args.get("page", 1, type=int),
        page_size=request.args.get("page_size", 25, type=int),
        status=request.args.get("status"),
        owner_user_id=request.args.get("owner_user_id", type=int),
        source_module=request.args.get("source_module"),
        priority=request.args.get("priority"),
        attention=request.args.get("attention"),
        q=request.args.get("q"),
        sort=request.args.get("sort"),
    )
    return render_template(
        "decision_ops/workspace.html",
        workspace_key="actions",
        workspace={
            "label": "Action Center",
            "eyebrow": "Decision ledger",
            "description": "Move certified signals into owned work, approvals and measured outcomes.",
            "permission": "page.work.view",
            "manage_permission": "actions.manage",
            "record_types": (),
            "statuses": service.ACTION_STATUSES,
            "source_contracts": (),
        },
        listing=listing,
        source_contracts=[],
        can_manage=_allowed("actions.create") or _allowed("actions.manage"),
        action_seed={
            "source_module": request.args.get("source_module") or "overview",
            "source_record_id": request.args.get("source_record_id") or "",
            "source_url": request.args.get("source_url") or "/work",
            "metric_key": request.args.get("metric_key") or "",
            "title": request.args.get("title") or "",
        },
        assignee_choices=service.active_user_choices(),
        hide_global_filters=True,
    )


@bp.get("/api/work/actions")
@login_required
@permission_required("page.work.view")
def actions_api():
    return jsonify({"status": "ok", **service.list_work_items(
        page=request.args.get("page", 1, type=int),
        page_size=request.args.get("page_size", 25, type=int),
        status=request.args.get("status"),
        owner_user_id=request.args.get("owner_user_id", type=int),
        source_module=request.args.get("source_module"),
        priority=request.args.get("priority"),
        attention=request.args.get("attention"),
        q=request.args.get("q"),
        sort=request.args.get("sort"),
    )})


@bp.get("/api/work/<workspace>/records")
@login_required
def records_api(workspace: str):
    key = str(workspace or "").strip().lower()
    config = service.WORKSPACES.get(key)
    if config is None or key == "enterprise":
        abort(404)
    if not _allowed(config["permission"]):
        abort(403)
    return jsonify({"status": "ok", **service.list_operational_records(
        key,
        page=request.args.get("page", 1, type=int),
        page_size=request.args.get("page_size", 25, type=int),
        status=request.args.get("status"),
        record_type=request.args.get("record_type"),
        owner_user_id=request.args.get("owner_user_id", type=int),
        priority=request.args.get("priority"),
        attention=request.args.get("attention"),
        q=request.args.get("q"),
        sort=request.args.get("sort"),
    )})


@bp.get("/api/work/source-contracts")
@login_required
@permission_required("page.enterprise_admin.view")
def source_contracts_api():
    return jsonify({"status": "ok", "contracts": service.source_contracts()})


@bp.get("/work/<workspace>")
@login_required
def workspace(workspace: str):
    key = str(workspace or "").strip().lower()
    config = service.WORKSPACES.get(key)
    if config is None:
        abort(404)
    if not _allowed(config["permission"]):
        abort(403)
    if key == "enterprise":
        listing = {
            "items": [],
            "summary": {"total": 0, "status_flow": [], "exceptions": 0, "pending_approval": 0, "overdue": 0, "unassigned": 0, "critical": 0},
            "facets": {"statuses": [], "record_types": [], "priorities": [], "owners": []},
            "filters": {"status": "", "record_type": "", "owner_user_id": None, "priority": "", "attention": "", "q": "", "sort": "updated"},
            "page": 1,
            "pages": 1,
            "total": 0,
        }
    else:
        listing = service.list_operational_records(
            key,
            page=request.args.get("page", 1, type=int),
            page_size=request.args.get("page_size", 25, type=int),
            status=request.args.get("status"),
            record_type=request.args.get("record_type"),
            owner_user_id=request.args.get("owner_user_id", type=int),
            priority=request.args.get("priority"),
            attention=request.args.get("attention"),
            q=request.args.get("q"),
            sort=request.args.get("sort"),
        )
    return render_template(
        "decision_ops/workspace.html",
        workspace_key=key,
        workspace=config,
        listing=listing,
        source_contracts=service.source_contracts(config["source_contracts"]),
        can_manage=_allowed(config["manage_permission"]),
        planning_scenario_id=request.args.get("scenario_id", type=int),
        assignee_choices=service.active_user_choices(),
        hide_global_filters=True,
    )


@bp.get("/work/actions/<int:work_item_id>")
@login_required
@permission_required("page.work.view")
def action_detail(work_item_id: int):
    item = service.get_work_item(work_item_id)
    if item is None:
        abort(404)
    return render_template(
        "decision_ops/action_detail.html",
        item=item,
        statuses=service.ACTION_STATUSES,
        can_manage=_allowed("actions.manage"),
        hide_global_filters=True,
    )


@bp.get("/work/records/<int:record_id>")
@login_required
def record_detail(record_id: int):
    item = service.get_operational_record(record_id)
    if item is None:
        abort(404)
    workspace = service.WORKSPACES[item["domain"]]
    if not _allowed(workspace["permission"]):
        abort(403)
    return render_template(
        "decision_ops/record_detail.html",
        item=item,
        workspace=workspace,
        workspace_key=item["domain"],
        can_manage=_allowed(workspace["manage_permission"]),
        hide_global_filters=True,
    )


@bp.post("/work/actions")
@login_required
@permission_required("actions.create")
def create_action():
    payload = _payload()
    try:
        result = service.create_work_item(payload, actor=current_user)
        _audit("action.created", result, payload)
    except service.DecisionOpsError as exc:
        return _error(exc, fallback=url_for("decision_ops.index"))
    if _is_json_request():
        return jsonify({"status": "created", "action": result}), 201
    flash("Action created in the decision ledger.", "success")
    return redirect(url_for("decision_ops.action_detail", work_item_id=result["id"]))


@bp.post("/work/actions/<int:work_item_id>/transition")
@login_required
@permission_required("actions.manage")
def transition_action(work_item_id: int):
    payload = _payload()
    try:
        result = service.transition_work_item(work_item_id, str(payload.get("status") or ""), actor=current_user, notes=payload.get("notes"), outcomes=payload)
        _audit("action.transitioned", result, payload)
    except service.DecisionOpsError as exc:
        return _error(exc, fallback=url_for("decision_ops.action_detail", work_item_id=work_item_id))
    if _is_json_request():
        return jsonify({"status": "ok", "action": result})
    flash(f"Action moved to {result['status'].replace('_', ' ')}.", "success")
    return redirect(url_for("decision_ops.action_detail", work_item_id=work_item_id))


@bp.post("/work/actions/<int:work_item_id>/comments")
@login_required
@permission_required("actions.manage")
def comment_action(work_item_id: int):
    payload = _payload()
    try:
        result = service.add_comment(work_item_id, str(payload.get("body") or ""), actor=current_user)
        _audit("action.comment_added", {"id": work_item_id}, {"comment_id": result["id"]})
    except service.DecisionOpsError as exc:
        return _error(exc, fallback=url_for("decision_ops.action_detail", work_item_id=work_item_id))
    if _is_json_request():
        return jsonify({"status": "created", "comment": result}), 201
    return redirect(url_for("decision_ops.action_detail", work_item_id=work_item_id))


@bp.post("/work/actions/<int:work_item_id>/attachments")
@login_required
@permission_required("actions.manage")
def attach_action_evidence(work_item_id: int):
    payload = _payload()
    try:
        result = service.add_attachment(work_item_id, payload, actor=current_user)
        _audit("action.evidence_linked", {"id": work_item_id}, result)
    except service.DecisionOpsError as exc:
        return _error(exc, fallback=url_for("decision_ops.action_detail", work_item_id=work_item_id))
    if _is_json_request():
        return jsonify({"status": "created", "attachment": result}), 201
    return redirect(url_for("decision_ops.action_detail", work_item_id=work_item_id))


@bp.post("/work/actions/<int:work_item_id>/dependencies")
@login_required
@permission_required("actions.manage")
def add_action_dependency(work_item_id: int):
    payload = _payload()
    try:
        result = service.add_dependency(work_item_id, int(payload.get("depends_on_work_item_id") or 0), actor=current_user)
        _audit("action.dependency_added", {"id": work_item_id}, result)
    except (service.DecisionOpsError, ValueError) as exc:
        return _error(exc, fallback=url_for("decision_ops.action_detail", work_item_id=work_item_id))
    if _is_json_request():
        return jsonify({"status": "created", "dependency": result}), 201
    return redirect(url_for("decision_ops.action_detail", work_item_id=work_item_id))


@bp.post("/work/<workspace>/records")
@login_required
def create_record(workspace: str):
    key = str(workspace or "").strip().lower()
    config = service.WORKSPACES.get(key)
    if config is None or key == "enterprise":
        abort(404)
    if not _allowed(config["manage_permission"]):
        abort(403)
    payload = _payload()
    try:
        result = service.create_operational_record(payload, actor=current_user, expected_domain=key)
        _audit(f"{key}.record_created", result, payload)
    except service.DecisionOpsError as exc:
        return _error(exc, fallback=url_for("decision_ops.workspace", workspace=key))
    if _is_json_request():
        return jsonify({"status": "created", "record": result}), 201
    flash(f"{config['label']} record created.", "success")
    return redirect(url_for("decision_ops.record_detail", record_id=result["id"]))


@bp.post("/work/records/<int:record_id>/transition")
@login_required
def transition_record(record_id: int):
    current = service.get_operational_record(record_id)
    if current is None:
        abort(404)
    config = service.WORKSPACES[current["domain"]]
    if not _allowed(config["manage_permission"]):
        abort(403)
    payload = _payload()
    try:
        result = service.transition_operational_record(record_id, str(payload.get("status") or ""), actor=current_user, notes=payload.get("notes"))
        _audit(f"{current['domain']}.record_transitioned", result, payload)
    except service.DecisionOpsError as exc:
        return _error(exc, fallback=url_for("decision_ops.record_detail", record_id=record_id))
    if _is_json_request():
        return jsonify({"status": "ok", "record": result})
    flash(f"Record moved to {result['status'].replace('_', ' ')}.", "success")
    return redirect(url_for("decision_ops.record_detail", record_id=record_id))


@bp.post("/work/procurement/from-planning/<int:scenario_id>")
@login_required
@permission_required("procurement.manage")
def planning_writeback(scenario_id: int):
    try:
        result = service.create_planning_draft_po(scenario_id, actor=current_user)
        _audit("procurement.planning_writeback", result, {"scenario_id": scenario_id})
    except service.DecisionOpsError as exc:
        return _error(exc, fallback=url_for("decision_ops.workspace", workspace="procurement"))
    if _is_json_request():
        return jsonify({"status": "created", "record": result}), 201
    flash("Approved planning recommendations written to an idempotent draft PO.", "success")
    return redirect(url_for("decision_ops.record_detail", record_id=result["id"]))


@bp.post("/work/master-data/changes")
@login_required
@permission_required("master_data.manage")
def master_data_change():
    payload = _payload()
    try:
        result = service.create_master_data_change(payload, actor=current_user)
        _audit("master_data.change_requested", result, payload)
    except service.DecisionOpsError as exc:
        return _error(exc, fallback=url_for("decision_ops.workspace", workspace="master-data"))
    if _is_json_request():
        return jsonify({"status": "created", "change": result}), 201
    flash("Master-data change sent to governed review.", "success")
    return redirect(url_for("decision_ops.workspace", workspace="master-data"))


@bp.post("/work/enterprise/contracts/<contract_key>")
@login_required
@permission_required("integrations.manage")
def update_contract(contract_key: str):
    payload = _payload()
    try:
        result = service.update_source_contract(contract_key, payload, actor=current_user)
        _audit("enterprise.source_contract_updated", {"id": result["contract_key"]}, {"status": result["status"], "system_name": result["system_name"]})
    except service.DecisionOpsError as exc:
        return _error(exc, fallback=url_for("decision_ops.workspace", workspace="enterprise"))
    if _is_json_request():
        return jsonify({"status": "ok", "contract": result})
    flash("Source contract updated; no credentials were stored.", "success")
    return redirect(url_for("decision_ops.workspace", workspace="enterprise"))


@bp.post("/api/assistant/draft-action")
@login_required
@permission_required("assistant.actions.draft")
def assistant_draft_action():
    payload = _payload()
    try:
        result = service.assistant_action(payload, actor=current_user)
        if result["status"] == "created":
            _audit("assistant.action_draft_created", result, {"source_module": payload.get("source_module"), "metric_key": payload.get("metric_key")})
    except service.DecisionOpsError as exc:
        return jsonify({"status": "error", "error": str(exc)}), 400
    return jsonify(result), 201 if result["status"] == "created" else 200
