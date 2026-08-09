from __future__ import annotations

from flask import Blueprint, render_template
from flask_login import login_required

from app.core.rbac import permission_required


bp = Blueprint("inventory", __name__, url_prefix="/inventory")


@bp.get("/")
@login_required
@permission_required("page.products.view")
def index():
    return render_template("inventory/index.html", filters_handler="ajax")
