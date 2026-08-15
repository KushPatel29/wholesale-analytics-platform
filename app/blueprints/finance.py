from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from app.core.rbac import permission_required


bp = Blueprint("finance", __name__, url_prefix="/finance")


@bp.get("/")
@login_required
@permission_required("data.cost.view")
def index():
    # No `filters_handler`: the statements are entity-level and the global
    # filter bar does not apply to them. See `finance_bundle` for why.
    return render_template("finance/index.html")


@bp.get("/api/bundle")
@login_required
@permission_required("data.cost.view")
def bundle():
    """
    The Finance payload.

    Deliberately not routed through `bundles._bundle`: that dispatcher exists to
    apply and cache global filters, and this page takes none. `fy` is accepted
    for the live app and the tests; the published page renders every fiscal year
    as comparative columns instead, because the static build replays captured
    payloads by path and would serve one year's numbers under every selection.
    """
    from app.services.finance_bundle import FinanceDatasetNotBuiltError, build_finance_bundle

    raw_year = request.args.get("fy")
    try:
        fiscal_year = int(raw_year) if raw_year else None
    except (TypeError, ValueError):
        fiscal_year = None

    try:
        return jsonify(build_finance_bundle(fiscal_year))
    except FinanceDatasetNotBuiltError as exc:
        # A missing dataset is a missing page, not a page of zeroes.
        return jsonify({"available": False, "warning": str(exc)}), 200
