from __future__ import annotations

from flask import Blueprint, request, redirect, render_template, url_for
from flask_login import login_required



pages = Blueprint("pages", __name__)


@pages.get("/")
@login_required
def home():
    if request.query_string:
        return redirect(f"{url_for('overview_page.overview_landing')}?{request.query_string.decode()}")
    # Avoid an extra redirect hop when already authenticated; render overview directly.
    try:
        from app.blueprints.overview import overview_landing  # type: ignore

        return overview_landing()
    except Exception:
        return redirect(url_for("overview_page.overview_landing"))


@pages.get("/metrics/")
@login_required
def metric_catalogue():
    from app.services.metrics import metric_catalogue as catalogue_rows

    rows = catalogue_rows()
    return render_template(
        "metrics/index.html",
        metrics=rows,
        implemented=sum(row["status"] == "Implemented" for row in rows),
        documented_only=sum(row["status"] != "Implemented" for row in rows),
    )
