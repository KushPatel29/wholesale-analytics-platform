from __future__ import annotations

from flask import Blueprint, redirect, render_template, request, url_for
from flask_login import login_required

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
    return render_template(
        "stakeholder_report/index.html",
        title="Demand & Supply Planner",
    )


@legacy_bp.route("/")
@legacy_bp.route("")
def legacy_redirect():
    """Permanent redirect from the pre-rename path, query string preserved."""
    target = url_for("stakeholder_report.index")
    if request.query_string:
        target = f"{target}?{request.query_string.decode('utf-8', 'ignore')}"
    return redirect(target, code=301)
