from __future__ import annotations

from flask import Blueprint, render_template, request, current_app, redirect, url_for
from flask_login import login_required, current_user

from app.services.filters import parse_filters, apply_filters as apply_filter_params
from app.services.frame import canonicalize
from app.services.overview_query import compute_overview
from app.services.overview_summary import build_summary_payload


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
