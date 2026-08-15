from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from app.core.rbac import permission_required


bp = Blueprint("marketing", __name__, url_prefix="/marketing")


@bp.get("/")
@login_required
@permission_required("data.cost.view")
def index():
    # Entity-level, like Finance: the global filter bar has nothing to act on.
    return render_template("marketing/index.html")


@bp.get("/api/bundle")
@login_required
@permission_required("data.cost.view")
def bundle():
    """
    The Marketing payload.

    Not routed through `bundles._bundle` for the same reason Finance is not:
    that dispatcher applies and caches global filters, and this page takes none.
    """
    from app.services.marketing_bundle import (
        MarketingDatasetNotBuiltError,
        build_marketing_bundle,
    )

    raw_year = request.args.get("fy")
    try:
        fiscal_year = int(raw_year) if raw_year else None
    except (TypeError, ValueError):
        fiscal_year = None

    try:
        return jsonify(build_marketing_bundle(fiscal_year))
    except MarketingDatasetNotBuiltError as exc:
        return jsonify({"available": False, "warning": str(exc)}), 200
