from __future__ import annotations

import pandas as pd
from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import login_required

from app.core.json_sanitizer import sanitize_for_json
from app.core.exports import dataframes_to_xlsx_response, dataframe_to_csv_response, sanitize_filename
from app.services import labor_bundle
from app.services.labor_store import LaborDatasetNotBuiltError


bp = Blueprint("labor", __name__, url_prefix="/labor")


@bp.get("/")
@login_required
def index():
    payload = labor_bundle.build_page_payload(request.args)
    payload_safe = sanitize_for_json(payload)
    client_payload = sanitize_for_json(labor_bundle.build_client_payload(payload_safe))
    return render_template(
        "labor/index.html",
        payload=payload_safe,
        payload_json=client_payload,
        filters=payload_safe.get("filters") or {},
        filter_options=payload_safe.get("filter_options") or {},
        export_urls=payload_safe.get("export_urls") or {},
        hide_global_filters=True,
    )


@bp.get("/api/bundle")
@login_required
def api_bundle():
    return jsonify(sanitize_for_json(labor_bundle.build_page_payload(request.args)))


@bp.get("/export/<dataset>")
@login_required
def export_dataset(dataset: str):
    dataset_key = str(dataset or "snapshot").strip().lower()
    if dataset_key not in {"snapshot", "detail", "department-summary", "category-summary", "employee-summary", "watchlist"}:
        abort(404)
    fmt = str(request.args.get("format") or ("csv" if dataset_key in {"detail", "watchlist"} else "xlsx")).strip().lower()
    filters = labor_bundle.resolve_filters(request.args)
    try:
        frames, stem = labor_bundle.build_export_frames(filters, dataset_key)
    except LaborDatasetNotBuiltError:
        frames, stem = {"Labor": pd.DataFrame()}, f"labor_{dataset_key}"
    safe_stem = sanitize_filename(stem or f"labor_{dataset_key}", default=f"labor_{dataset_key}")
    if fmt == "csv":
        first_frame = next(iter(frames.values()), None)
        return dataframe_to_csv_response(first_frame, filename=f"{safe_stem}.csv")
    return dataframes_to_xlsx_response(frames, filename=f"{safe_stem}.xlsx")
