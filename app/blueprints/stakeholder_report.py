from __future__ import annotations

import json
import time
from flask import Blueprint, render_template, request, current_app, url_for
from flask_login import login_required, current_user
from app.core.rbac import requires_roles
from app.services import bundle_service

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
