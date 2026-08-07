from __future__ import annotations

import os
import time
from datetime import datetime

import json
import hashlib

from flask import Blueprint, Response, jsonify, request, current_app, g, session
from flask_login import current_user, login_required
from werkzeug.exceptions import Forbidden

from app.services import bundle_service
from app.services import suppliers_bundle as suppliers_bundle_service, filters_service, fact_store
from app.services.bundle_builder import payload_size, to_json_safe
from app.core.exports import dataframes_to_xlsx_response, dataframe_to_csv_response
from app.core import access_policy
from app.core.rbac import permission_required, requires_roles

bp = Blueprint("bundles", __name__, url_prefix="/api")

_BUNDLE_ETAG_EXCLUDE_META = {
    "cache_hit",
    "cached",
    "cache_age_seconds",
    "duckdb_ms",
    "duckdb_query_count",
    "payload_bytes",
    "serialize_ms",
    "total_ms",
    "duration_ms",
    "request_id",
}


def _flag_on(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _supplier_drilldown_v2_active() -> bool:
    suppliers_v2 = current_app.config.get("SUPPLIERS_V2")
    if suppliers_v2 is None:
        suppliers_v2 = os.getenv("SUPPLIERS_V2", "0")
    drilldown_v2 = current_app.config.get("SUPPLIER_DRILLDOWN_V2")
    if drilldown_v2 is None:
        drilldown_v2 = os.getenv("SUPPLIER_DRILLDOWN_V2", "0")
    return _flag_on(suppliers_v2) and _flag_on(drilldown_v2)


def _stable_bundle_etag(payload: dict | list) -> str:
    try:
        hash_payload = json.loads(json.dumps(payload))
    except Exception:
        hash_payload = payload
    if isinstance(hash_payload, dict):
        meta = hash_payload.get("meta")
        if isinstance(meta, dict):
            for key in _BUNDLE_ETAG_EXCLUDE_META:
                meta.pop(key, None)
    raw = json.dumps(hash_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return '"' + hashlib.sha256(raw.encode("utf-8")).hexdigest() + '"'


def _bundle(page: str):
    start = time.perf_counter()
    if page.endswith(".drilldown"):
        entity = page.split(".", 1)[0]
        payload = bundle_service.drilldown(entity, request.args)
    else:
        payload = bundle_service.bundle(page, request.args)
    meta = payload.setdefault("meta", {})
    safe_payload = to_json_safe(payload)
    etag = _stable_bundle_etag(safe_payload)
    if request.headers.get("If-None-Match") == etag:
        resp = Response(status=304)
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
        resp.headers["Vary"] = "Cookie, Authorization"
        try:
            resp.headers["X-Bundle-Cached"] = str(meta.get("cached", False)).lower()
            if meta.get("dataset_version"):
                resp.headers["X-Dataset-Version"] = str(meta.get("dataset_version"))
            if meta.get("cache_key"):
                resp.headers["X-Bundle-Cache-Key"] = meta.get("cache_key")
            if meta.get("cache_age_seconds") is not None:
                resp.headers["X-Bundle-Cache-Age"] = str(meta.get("cache_age_seconds"))
        except Exception:
            pass
        return resp
    ser_start = time.perf_counter()
    body = json.dumps(safe_payload, separators=(",", ":"), ensure_ascii=False)
    serialize_ms = int((time.perf_counter() - ser_start) * 1000)
    total_ms = int((time.perf_counter() - start) * 1000)
    try:
        g._serialize_ms = serialize_ms
    except Exception:
        pass
    payload_bytes = payload_size(safe_payload) or len(body.encode("utf-8"))
    meta.setdefault("serialize_ms", serialize_ms)
    meta.setdefault("total_ms", total_ms)
    meta.setdefault("payload_bytes", payload_bytes)
    status_code = 200
    try:
        if isinstance(payload, dict) and payload.get("error"):
            message = str(payload.get("error", {}).get("message", "")).lower()
            if "not found" in message:
                status_code = 404
            else:
                status_code = 400
    except Exception:
        status_code = 200

    resp = Response(body, status=status_code, mimetype="application/json")
    resp.headers["ETag"] = etag
    resp.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
    resp.headers["Vary"] = "Cookie, Authorization"
    try:
        resp.headers["X-Bundle-Cached"] = str(meta.get("cached", False)).lower()
        if meta.get("dataset_version"):
            resp.headers["X-Dataset-Version"] = str(meta.get("dataset_version"))
        if meta.get("cache_key"):
            resp.headers["X-Bundle-Cache-Key"] = meta.get("cache_key")
        if meta.get("cache_age_seconds") is not None:
            resp.headers["X-Bundle-Cache-Age"] = str(meta.get("cache_age_seconds"))
        resp.headers["X-Payload-Bytes"] = str(payload_bytes)
    except Exception:
        pass
    try:
        stats = getattr(g, "_duckdb_stats", None)
    except Exception:
        stats = None
    duckdb_count = meta.get("duckdb_query_count", 0 if stats is None else stats.get("count", 0))
    duckdb_ms = meta.get("duckdb_ms", 0 if stats is None else stats.get("total_ms", 0))
    if page == "regions":
        try:
            table_meta = payload.get("table", {}) if isinstance(payload, dict) else {}
            current_app.logger.info(
                "regions.bundle.summary",
                extra={
                    "endpoint": request.path,
                    "query_count": duckdb_count,
                    "duckdb_ms": duckdb_ms,
                    "serialize_ms": serialize_ms,
                    "cache_hit": bool(meta.get("cached")),
                    "rows_returned": len(table_meta.get("rows") or []),
                    "rows_total": table_meta.get("total"),
                },
            )
        except Exception:
            pass
    if page == "salesreps":
        try:
            table_meta = payload.get("table", {}) if isinstance(payload, dict) else {}
            current_app.logger.info(
                "salesreps.bundle.summary",
                extra={
                    "endpoint": request.path,
                    "query_count": duckdb_count,
                    "duckdb_ms": duckdb_ms,
                    "serialize_ms": serialize_ms,
                    "cache_hit": bool(meta.get("cached")),
                    "rows_out": len(table_meta.get("rows") or []),
                },
            )
        except Exception:
            pass
    if page == "regions.drilldown":
        try:
            charts = payload.get("charts", {}) if isinstance(payload, dict) else {}
            current_app.logger.info(
                "regions.drilldown.summary",
                extra={
                    "endpoint": request.path,
                    "query_count": duckdb_count,
                    "duckdb_ms": duckdb_ms,
                    "serialize_ms": serialize_ms,
                    "cache_hit": bool(meta.get("cached")),
                    "top_customers": len(charts.get("top_customers") or []),
                    "top_products": len(charts.get("top_products") or []),
                    "churned": len(charts.get("churned_customers") or []),
                },
            )
        except Exception:
            pass
    if page == "salesreps.drilldown":
        try:
            table_meta = payload.get("table", {}) if isinstance(payload, dict) else {}
            current_app.logger.info(
                "salesreps.drilldown.summary",
                extra={
                    "endpoint": request.path,
                    "query_count": duckdb_count,
                    "duckdb_ms": duckdb_ms,
                    "serialize_ms": serialize_ms,
                    "cache_hit": bool(meta.get("cached")),
                    "rows_out": len(table_meta.get("rows") or []),
                },
            )
        except Exception:
            pass
    if page == "products":
        try:
            bubbles_n = len(payload.get("price_vs_velocity") or payload.get("charts", {}).get("price_velocity") or [])
            performance_n = len(payload.get("performance_bubble", {}).get("points") or [])
            recommendations_n = len(payload.get("recommendations") or [])
            series_n = len(payload.get("monthly_series") or payload.get("charts", {}).get("trajectory", {}).get("labels") or [])
            table_n = len(payload.get("table", {}).get("rows") or [])
            movers_n = len(payload.get("charts", {}).get("movers") or [])
            segments_n = len(payload.get("charts", {}).get("segments", {}).get("summary") or [])
            forecast_n = len(payload.get("forecast_overlay") or [])
            current_app.logger.info(
                "products.bundle.summary",
                extra={
                    "endpoint": request.path,
                    "query_count": duckdb_count,
                    "duckdb_ms": duckdb_ms,
                    "serialize_ms": serialize_ms,
                    "bubbles_n": bubbles_n,
                    "performance_n": performance_n,
                    "recommendations_n": recommendations_n,
                    "series_n": series_n,
                    "table_n": table_n,
                    "movers_n": movers_n,
                    "segments_n": segments_n,
                    "forecast_n": forecast_n,
                    "payload_bytes": payload_bytes,
                    "cache_hit": bool(meta.get("cached")),
                },
            )
        except Exception:
            pass
    if page == "products.drilldown":
        try:
            current_app.logger.info(
                "products.drilldown.summary",
                extra={
                    "endpoint": request.path,
                    "query_count": duckdb_count,
                    "duckdb_ms": duckdb_ms,
                    "serialize_ms": serialize_ms,
                    "payload_bytes": payload_bytes,
                    "cache_hit": bool(meta.get("cached")),
                },
            )
        except Exception:
            pass
    try:
        current_app.logger.info(
            "bundle.request",
            extra={
                "path": request.path,
                "endpoint": f"{page}.bundle",
                "page_id": meta.get("page_id", page),
                "user": getattr(current_user, "id", None),
                "role": getattr(current_user, "role", None),
                "filter_hash": meta.get("filter_hash"),
                "dataset_version": meta.get("dataset_version"),
                "duckdb_query_count": duckdb_count,
                "duckdb_ms": duckdb_ms,
                "serialize_ms": serialize_ms,
                "total_ms": total_ms,
                "payload_bytes": payload_bytes,
                "cached": bool(meta.get("cached")),
            },
        )
    except Exception:
        pass
    try:
        current_app.logger.info(
            "bundle.perf",
            extra={
                "endpoint": request.path,
                "cache_hit": bool(meta.get("cached")),
                "query_count": duckdb_count,
                "duckdb_ms": duckdb_ms,
                "serialize_ms": serialize_ms,
                "payload_bytes": payload_bytes,
                "total_ms": total_ms,
            },
        )
    except Exception:
        pass
    return resp


@bp.get("/products/bundle")
@login_required
def products_bundle():
    return _bundle("products")


@bp.get("/customers/bundle")
@login_required
def customers_bundle():
    return _bundle("customers")


@bp.get("/regions/bundle")
@login_required
def regions_bundle():
    return _bundle("regions")


@bp.get("/suppliers/bundle")
@login_required
def suppliers_bundle():
    return _bundle("suppliers")


@bp.get("/salesreps/bundle")
@login_required
def salesreps_bundle():
    return _bundle("salesreps")


@bp.get("/stakeholder-report/bundle")
@login_required
def stakeholder_report_bundle():
    return _bundle("stakeholder_report")


@bp.get("/salesreps/efficiency")
@login_required
@requires_roles("admin", "owner", "gm", "manager", "sales")
def efficiency_api():
    from app.services import salesreps_bundle
    from app.services.filters import parse_filters
    from app.services import filters_service

    filters = parse_filters(request.args)
    scope = filters_service.scope_from_user(current_user)
    payload = salesreps_bundle.build_efficiency_payload(filters, scope, request.args)
    return jsonify(payload)


@bp.get("/products/drilldown/bundle")
@login_required
def products_drilldown_bundle():
    return _bundle("products.drilldown")


@bp.get("/products/<path:product_id>/forecast")
@login_required
def products_forecast(product_id: str):
    from app.services import filters as canonical_filters
    from app.services import product_drilldown_service

    resolved_filters, _meta = canonical_filters.resolve_filters(
        request,
        current_user_obj=current_user,
        session_obj=session,
        sticky_enabled=True,
        update_session=False,
    )
    freq = str(request.args.get("freq") or "month").strip().lower()
    try:
        horizon = int(request.args.get("horizon") or 0)
    except Exception:
        horizon = 0
    payload = product_drilldown_service.build_product_forecast_payload(
        str(product_id).strip(),
        resolved_filters,
        current_user,
        freq=freq,
        horizon=horizon if horizon > 0 else None,
    )
    status_code = 200
    if isinstance(payload, dict) and payload.get("error"):
        message = str(payload.get("error", {}).get("message", "")).lower()
        if "not found" in message or "disabled" in message:
            status_code = 404
        else:
            status_code = 400
    return jsonify(payload), status_code


@bp.get("/customers/drilldown/bundle")
@login_required
def customers_drilldown_bundle():
    return _bundle("customers.drilldown")


@bp.get("/suppliers/drilldown/bundle")
@login_required
def suppliers_drilldown_bundle():
    return _bundle("suppliers.drilldown")


@bp.get("/regions/drilldown/bundle")
@login_required
def regions_drilldown_bundle():
    return _bundle("regions.drilldown")


@bp.get("/salesreps/drilldown/bundle")
@login_required
def salesreps_drilldown_bundle():
    return _bundle("salesreps.drilldown")


def _suppliers_filters_scope():
    try:
        sticky_enabled = bool(current_app.config.get("STICKY_FILTERS", True))
        user_id = current_user.get_id() if hasattr(current_user, "get_id") else None
        filters = filters_service.resolve_effective_filters(
            request.args or {},
            session_obj=session,
            user_id=user_id,
            sticky_enabled=sticky_enabled,
        )
    except Exception:
        filters = filters_service.default_filters(current_user)
    scope = filters_service.scope_from_user(current_user)
    return filters, scope


def _suppliers_export(fmt: str):
    filters, scope = _suppliers_filters_scope()
    dataset = str(
        request.args.get("scope")
        or request.args.get("dataset")
        or request.args.get("type")
        or request.args.get("export_type")
        or "table"
    ).strip().lower()
    try:
        frame, meta = suppliers_bundle_service.build_suppliers_export_dataset(filters, scope, request.args, dataset)
    except Exception as exc:  # pragma: no cover - defensive
        current_app.logger.exception("suppliers.export.failed", extra={"format": fmt, "dataset": dataset})
        return jsonify({"error": {"message": str(exc)}}), 503

    dataset_version = fact_store.cache_buster()
    metadata_df = suppliers_bundle_service.build_suppliers_export_metadata_frame(
        filters,
        dataset=dataset,
        dataset_version=dataset_version,
        meta=meta or {},
        args=request.args,
    )

    try:
        current_app.logger.info(
            "suppliers.export.summary",
            extra={
                "format": fmt,
                "dataset": dataset,
                "rows": len(frame.index) if frame is not None else 0,
                "duckdb_query_count": meta.get("duckdb_query_count"),
                "duckdb_ms": meta.get("duckdb_ms"),
                "dataset_version": dataset_version,
                "cached": bool(meta.get("cached")),
            },
        )
    except Exception:
        pass

    if fmt == "csv":
        return dataframe_to_csv_response(frame, filename=f"suppliers_{dataset}.csv")
    sheet_map = {
        "table": "Suppliers",
        "movers": "Movers",
        "risk": "Risk",
        "segments": "Segments",
        "concentration": "Concentration",
    }
    sheet_name = sheet_map.get(dataset, "Suppliers")
    sheets = {
        sheet_name: frame,
        "Metadata": metadata_df,
    }
    return dataframes_to_xlsx_response(sheets, filename=f"suppliers_{dataset}.xlsx")


@bp.get("/suppliers/export.csv")
@login_required
def suppliers_export_csv():
    return _suppliers_export("csv")


@bp.get("/suppliers/export.xlsx")
@login_required
def suppliers_export_xlsx():
    return _suppliers_export("xlsx")


@bp.get("/suppliers/<supplier_id>/products/export.csv")
@login_required
def suppliers_products_export_csv(supplier_id: str):
    return _suppliers_products_export(supplier_id)


def _suppliers_products_export(supplier_id: str):
    filters, scope = _suppliers_filters_scope()
    try:
        access_policy.enforce_entity_access("suppliers", str(supplier_id), access_policy.get_current_scope(use_cache=True))
    except Forbidden:
        current_app.logger.warning(
            "suppliers.drilldown.access_denied",
            extra={
                "user_id": getattr(current_user, "id", None),
                "required_permission": "page.suppliers.view",
                "supplier_id": str(supplier_id),
                "scope_hash": (scope or {}).get("scope_hash"),
                "endpoint": request.path,
            },
        )
        raise
    try:
        frame, meta = suppliers_bundle_service.build_supplier_products_frame(supplier_id, filters, scope, request.args)
    except suppliers_bundle_service.SupplierProductsExportLimitError as exc:
        current_app.logger.warning(
            "suppliers.products_export.limit_exceeded",
            extra={
                "supplier_id": str(supplier_id),
                "row_count": exc.row_count,
                "max_rows": exc.max_rows,
                "format": "csv",
            },
        )
        return (
            jsonify(
                {
                    "error": {
                        "message": str(exc),
                        "code": "export_row_limit_exceeded",
                        "row_count": exc.row_count,
                        "max_rows": exc.max_rows,
                    }
                }
            ),
            413,
        )
    except Exception as exc:  # pragma: no cover - defensive
        current_app.logger.exception("suppliers.products_export.failed", extra={"supplier_id": supplier_id})
        return jsonify({"error": {"message": str(exc)}}), 503

    try:
        current_app.logger.info(
            "suppliers.products_export.summary",
            extra={
                "supplier_id": str(supplier_id),
                "format": "csv",
                "rows": len(frame.index) if frame is not None else 0,
                "duckdb_query_count": meta.get("duckdb_query_count"),
                "duckdb_ms": meta.get("duckdb_ms"),
                "dataset_version": meta.get("dataset_version"),
                "cached": bool(meta.get("cached")),
            },
        )
    except Exception:
        pass

    filename = f"supplier_{supplier_id}_products.csv"
    return dataframe_to_csv_response(frame, filename=filename)


def _suppliers_drilldown_export(supplier_id: str, fmt: str):
    filters, scope = _suppliers_filters_scope()
    dataset = str(
        request.args.get("dataset")
        or request.args.get("scope")
        or request.args.get("type")
        or "products"
    ).strip().lower()
    if dataset == "products-vs-customers":
        dataset = "products_vs_customers"

    if dataset == "products_vs_customers" and not _supplier_drilldown_v2_active():
        return jsonify({"error": {"message": "Supplier drilldown V2 export is disabled."}}), 404

    try:
        access_policy.enforce_entity_access("suppliers", str(supplier_id), access_policy.get_current_scope(use_cache=True))
    except Forbidden:
        current_app.logger.warning(
            "suppliers.drilldown.access_denied",
            extra={
                "user_id": getattr(current_user, "id", None),
                "required_permission": "page.suppliers.view",
                "supplier_id": str(supplier_id),
                "scope_hash": (scope or {}).get("scope_hash"),
                "endpoint": request.path,
            },
        )
        raise

    dataset_version = fact_store.cache_buster()
    if dataset == "full" and fmt == "xlsx":
        ordered = [
            "summary",
            "monthly_series",
            "products",
            "customers",
            "products_vs_customers",
            "pricing_outliers",
            "margin_risk",
        ]
        sheet_names = {
            "summary": "Summary",
            "monthly_series": "Monthly Series",
            "products": "Products",
            "customers": "Customers",
            "products_vs_customers": "Products Vs Customers",
            "pricing_outliers": "Pricing Outliers",
            "margin_risk": "Margin At Risk",
        }
        sheets = {}
        metadata_meta = {"start": None, "end": None, "prior_start": None, "prior_end": None, "total_rows": 0}
        for token in ordered:
            frame, meta = suppliers_bundle_service.build_supplier_drilldown_export_dataset(
                str(supplier_id),
                filters,
                scope,
                request.args,
                token,
            )
            sheets[sheet_names[token]] = frame
            for key in ("start", "end", "prior_start", "prior_end"):
                if metadata_meta.get(key) is None and meta.get(key) is not None:
                    metadata_meta[key] = meta.get(key)
            metadata_meta["total_rows"] = int(metadata_meta.get("total_rows") or 0) + int(len(frame.index))

        metadata_df = suppliers_bundle_service.build_suppliers_export_metadata_frame(
            filters,
            dataset=f"supplier_drilldown_full:{supplier_id}",
            dataset_version=dataset_version,
            meta=metadata_meta,
            args=request.args,
        )
        sheets["Metadata"] = metadata_df
        return dataframes_to_xlsx_response(sheets, filename=f"supplier_{supplier_id}_drilldown_full.xlsx")

    frame, meta = suppliers_bundle_service.build_supplier_drilldown_export_dataset(
        str(supplier_id),
        filters,
        scope,
        request.args,
        dataset,
    )
    metadata_df = suppliers_bundle_service.build_suppliers_export_metadata_frame(
        filters,
        dataset=f"supplier_drilldown:{dataset}",
        dataset_version=dataset_version,
        meta=meta or {},
        args=request.args,
    )
    if fmt == "csv":
        if dataset == "products_vs_customers":
            stamp = datetime.now().strftime("%Y%m%d")
            return dataframe_to_csv_response(
                frame,
                filename=f"supplier_{supplier_id}_products_vs_customers_{stamp}.csv",
            )
        return dataframe_to_csv_response(frame, filename=f"supplier_{supplier_id}_{dataset}.csv")
    sheet_name = {
        "summary": "Summary",
        "monthly_series": "Monthly Series",
        "products": "Products",
        "customers": "Customers",
        "products_vs_customers": "Products Vs Customers",
        "pricing_outliers": "Pricing Outliers",
        "margin_risk": "Margin At Risk",
    }.get(dataset, "Supplier Drilldown")
    filename = (
        f"supplier_{supplier_id}_products_vs_customers_{datetime.now().strftime('%Y%m%d')}.xlsx"
        if dataset == "products_vs_customers"
        else f"supplier_{supplier_id}_{dataset}.xlsx"
    )
    return dataframes_to_xlsx_response(
        {
            sheet_name: frame,
            "Metadata": metadata_df,
        },
        filename=filename,
    )


@bp.get("/suppliers/<supplier_id>/drilldown/export.csv")
@login_required
@permission_required("export.suppliers.csv")
def suppliers_drilldown_export_csv(supplier_id: str):
    return _suppliers_drilldown_export(supplier_id, "csv")


@bp.get("/suppliers/<supplier_id>/drilldown/export.xlsx")
@login_required
@permission_required("export.suppliers.csv")
def suppliers_drilldown_export_xlsx(supplier_id: str):
    return _suppliers_drilldown_export(supplier_id, "xlsx")
