from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.core.rbac import requires_roles

# The nav has always labelled this page "Planning" while pointing at
# /stakeholder-report/, so the URL a visitor sees in the address bar disagreed
# with the link they clicked, and a hand-typed /planning/ 404'd. `/planning/` is
# now the canonical path; the old one 301s to it so existing links and any
# bookmark still resolve.
#
# The blueprint keeps its `stakeholder_report` name: renaming it would break
# every `url_for('stakeholder_report.index')` in the templates for no user-
# visible gain.
bp = Blueprint("stakeholder_report", __name__, url_prefix="/planning")

legacy_bp = Blueprint("stakeholder_report_legacy", __name__, url_prefix="/stakeholder-report")


@bp.route("/")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "analyst", "demo_viewer")
def index():
    """
    Renders the Demand & Supply Planner landing page.
    """
    from app.services import planning_scenarios

    return render_template(
        "stakeholder_report/index.html",
        title="Demand & Supply Planner",
        scenarios=planning_scenarios.list_scenarios(),
        scenario_presets=planning_scenarios.PRESETS,
    )


def _scenario_assumptions_from_form() -> dict[str, str]:
    return {
        key: request.form.get(key, "")
        for key in (
            "demand_growth_pct",
            "lead_time_days",
            "safety_stock_days",
            "service_level_pct",
            "budget_limit",
        )
    }


@bp.post("/scenarios")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "analyst")
def create_scenario():
    from app.services import planning_scenarios

    try:
        row = planning_scenarios.create_scenario(
            name=request.form.get("name") or "",
            preset=request.form.get("preset") or "base",
            assumptions=_scenario_assumptions_from_form(),
            actor_user=current_user,
        )
        flash(f"Scenario {row['name']} v{row['version']} created as a draft.", "success")
    except planning_scenarios.PlanningScenarioError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("stakeholder_report.index", _anchor="planningScenarios"))


@bp.post("/scenarios/<int:scenario_id>/version")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "analyst")
def version_scenario(scenario_id: int):
    from app.services import planning_scenarios

    try:
        row = planning_scenarios.create_version(
            scenario_id,
            assumptions=_scenario_assumptions_from_form(),
            actor_user=current_user,
        )
        flash(f"Scenario version v{row['version']} created.", "success")
    except planning_scenarios.PlanningScenarioError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("stakeholder_report.index", _anchor="planningScenarios"))


@bp.post("/scenarios/<int:scenario_id>/submit")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "analyst")
def submit_scenario(scenario_id: int):
    from app.services import planning_scenarios

    try:
        planning_scenarios.transition_scenario(
            scenario_id,
            target_status="pending_approval",
            actor_user=current_user,
            notes=request.form.get("notes"),
        )
        flash("Scenario submitted for approval.", "success")
    except planning_scenarios.PlanningScenarioError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("stakeholder_report.index", _anchor="planningScenarios"))


@bp.post("/scenarios/<int:scenario_id>/decision")
@login_required
@requires_roles("admin", "owner", "gm", "manager")
def decide_scenario(scenario_id: int):
    from app.services import planning_scenarios

    target = (request.form.get("decision") or "").strip().lower()
    if target not in {"approved", "rejected"}:
        abort(400, description="Decision must be approved or rejected.")
    try:
        row = planning_scenarios.transition_scenario(
            scenario_id,
            target_status=target,
            actor_user=current_user,
            notes=request.form.get("notes"),
        )
        if target == "approved":
            flash(
                f"Scenario approved; {row['recommendation_count']} replenishment recommendations were written back.",
                "success",
            )
        else:
            flash("Scenario rejected; no replenishment recommendations were written.", "warning")
    except planning_scenarios.PlanningScenarioError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("stakeholder_report.index", _anchor="planningScenarios"))


@legacy_bp.route("/")
@legacy_bp.route("")
def legacy_redirect():
    """Permanent redirect from the pre-rename path, query string preserved."""
    target = url_for("stakeholder_report.index")
    if request.query_string:
        target = f"{target}?{request.query_string.decode('utf-8', 'ignore')}"
    return redirect(target, code=301)
