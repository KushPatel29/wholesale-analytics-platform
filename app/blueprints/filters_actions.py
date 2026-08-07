from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from typing import Any

from flask import Blueprint, current_app, redirect, request, session, url_for
from flask_login import current_user, login_required

from app.services.filters import (
    clear_sticky_filters_in_session,
    mark_filters_last_applied,
    resolve_filters,
)


bp = Blueprint("filters_actions", __name__, url_prefix="/filters")

_FILTER_KEYS = {
    "start",
    "start_date",
    "startdate",
    "end",
    "end_date",
    "enddate",
    "date_preset",
    "preset",
    "range_preset",
    "date_type",
    "status",
    "statuses",
    "order_status",
    "order_statuses",
    "region",
    "regions",
    "shipping_method",
    "shipping_methods",
    "shippingmethod",
    "shippingmethods",
    "methods",
    "customer",
    "customers",
    "customer_id",
    "customer_ids",
    "customerid",
    "supplier",
    "suppliers",
    "supplier_id",
    "supplier_ids",
    "supplierid",
    "product",
    "products",
    "product_id",
    "product_ids",
    "productid",
    "sales_reps",
    "sales_rep_ids",
    "salesreps",
    "salesrep_ids",
    "protein_min",
    "protein_max",
    "protein_name",
    "protein_name_like",
    "protein",
    "complete_months_only",
    "completemonthsonly",
    "full_months_only",
    "_gf",
    "_gf_reset",
}

_NON_FORWARD_KEYS = {"csrf_token", "next", "_gf_apply", "_gf_reset"}


def _canonical_key(name: Any) -> str:
    raw = str(name or "").strip().lower()
    if raw.endswith("[]"):
        raw = raw[:-2]
    return raw.replace("-", "_")


def _is_filter_key(name: Any) -> bool:
    return _canonical_key(name) in _FILTER_KEYS


def _safe_next(default_endpoint: str) -> str:
    raw = (
        request.form.get("next")
        or request.args.get("next")
        or request.headers.get("Referer")
        or url_for(default_endpoint)
    )
    try:
        parsed = urlparse(str(raw))
    except Exception:
        return url_for(default_endpoint)
    # keep redirects relative to this app
    if parsed.scheme or parsed.netloc:
        return url_for(default_endpoint)
    if not parsed.path.startswith("/"):
        return url_for(default_endpoint)
    return raw


def _merge_redirect_with_passthrough(next_url: str) -> str:
    try:
        parsed = urlparse(next_url)
    except Exception:
        return next_url
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    keep = [(k, v) for (k, v) in pairs if not _is_filter_key(k)]

    for key in request.form.keys():
        key_norm = _canonical_key(key)
        if key_norm in _NON_FORWARD_KEYS or _is_filter_key(key):
            continue
        for value in request.form.getlist(key):
            keep.append((key, value))

    query = urlencode(keep, doseq=True)
    return urlunparse(parsed._replace(query=query))


@bp.post("/apply")
@login_required
def apply_filters():
    sticky_enabled = bool(current_app.config.get("STICKY_FILTERS", True))
    resolve_filters(
        request,
        current_user,
        session_obj=session,
        source=request.form,
        sticky_enabled=sticky_enabled,
        update_session=True,
    )
    mark_filters_last_applied(session)
    next_url = _safe_next("overview_page.overview_landing")
    return redirect(_merge_redirect_with_passthrough(next_url))


@bp.post("/reset")
@login_required
def reset_filters():
    clear_sticky_filters_in_session(session)
    mark_filters_last_applied(session)
    next_url = _safe_next("overview_page.overview_landing")
    return redirect(_merge_redirect_with_passthrough(next_url))
