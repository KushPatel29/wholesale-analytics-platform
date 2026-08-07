from __future__ import annotations

from flask import Blueprint, render_template
from flask_login import login_required
from app.core.rbac import requires_roles

bp = Blueprint("stakeholder_report", __name__, url_prefix="/stakeholder-report")

@bp.route("/")
@login_required
@requires_roles("admin", "owner", "gm", "manager")
def index():
    """
    Renders the Stakeholder Executive Report landing page.
    """
    return render_template(
        "stakeholder_report/index.html",
        title="Stakeholder Executive Briefing"
    )
