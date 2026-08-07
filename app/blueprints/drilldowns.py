from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request
from flask_login import current_user, login_required

from app.services import drilldown_service


bp = Blueprint("drilldowns", __name__, url_prefix="/drilldowns")


@bp.get("/go")
@login_required
def go():
    try:
        raw_payload = drilldown_service.decode_context_param(request.args.get("context"))
        token, context = drilldown_service.issue_context_token(raw_payload, user_obj=current_user)
        target_url = drilldown_service.resolve_target_url(context, token, user_obj=current_user)
    except PermissionError:
        abort(403)
    except ValueError as exc:
        flash(f"Unable to open drilldown: {str(exc)}.", "warning")
        abort(400)
    return redirect(target_url)


@bp.get("/workspace")
@login_required
def workspace():
    token = request.args.get("token")
    if not token:
        abort(400)
    try:
        context = drilldown_service.load_context_token(token, user_obj=current_user)
        model = drilldown_service.build_workspace_model(context, user_obj=current_user)
    except PermissionError:
        abort(403)
    except ValueError:
        return render_template(
            "drilldowns/workspace.html",
            workspace_title="Drilldown unavailable",
            workspace_subtitle="The drilldown token is invalid, expired, or no longer valid under the current scope.",
            workspace=None,
            hide_drill_context_banner=True,
            drilldown_invalid=True,
        )

    if str(request.args.get("format") or "").strip().lower() == "csv":
        resp = drilldown_service.workspace_export_response(model, token=token)
        if resp is not None:
            return resp

    return render_template(
        "drilldowns/workspace.html",
        workspace_title=model.get("title"),
        workspace_subtitle=model.get("subtitle"),
        workspace=model,
        workspace_token=token,
        hide_drill_context_banner=True,
        drilldown_invalid=False,
    )
