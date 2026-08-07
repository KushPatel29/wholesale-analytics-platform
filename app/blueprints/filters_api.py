from __future__ import annotations

import os
import time
from typing import Any, Dict

from flask import Blueprint, jsonify, request, current_app, session
from flask_login import current_user, login_required

from app.services import filters_service
from app.services.filters import (
    bind_filter_cache_key,
    build_filter_summary,
    clear_sticky_filters_in_session,
    filters_to_store,
    mark_filters_last_applied,
    resolve_filters,
    write_sticky_filters_to_session,
)

bp = Blueprint("filters_api", __name__, url_prefix="/api/filters")


def _reset_requested() -> bool:
    token = request.args.get("_gf_reset")
    return str(token or "").strip().lower() in {"1", "true", "yes", "on"}


def _merge_filter_meta(*metas: Dict[str, Any]) -> Dict[str, Any]:
    dropped: Dict[str, list[str]] = {}
    notice = None
    sanitized = False
    for meta in metas:
        if not isinstance(meta, dict):
            continue
        sanitized = sanitized or bool(meta.get("sanitized"))
        if not notice:
            notice = meta.get("filters_notice") or meta.get("notice")
        raw_dropped = meta.get("dropped_filters") or meta.get("dropped") or {}
        if not isinstance(raw_dropped, dict):
            continue
        for key, values in raw_dropped.items():
            if not isinstance(values, (list, tuple, set)):
                continue
            bucket = dropped.setdefault(str(key), [])
            for value in values:
                token = str(value).strip()
                if token and token not in bucket:
                    bucket.append(token)
    return {"sanitized": sanitized, "dropped_filters": dropped, "filters_notice": notice}


def _flag_on(raw: Any) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(raw: Any, default: int) -> int:
    try:
        return max(0, int(str(raw).strip()))
    except Exception:
        return default


def _options_log_method(payload: Dict[str, Any], payload_error: Exception | None) -> str:
    verbose = _flag_on(os.getenv("FILTER_OPTIONS_LOG_ALL_SUCCESS")) or _flag_on(os.getenv("DEBUG_FILTERS")) or _flag_on(os.getenv("DEBUG"))
    if verbose or payload_error is not None:
        return "info"
    meta = payload.get("meta") if isinstance(payload, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    duration_ms = _positive_int(payload.get("duration_ms") or 0, 0) if payload.get("duration_ms") is not None else 0
    slow_ms = _positive_int(os.getenv("FILTER_OPTIONS_INFO_MS"), 1000)
    degraded = bool(meta.get("degraded"))
    cached = bool(payload.get("cached") or meta.get("cached"))
    if degraded or duration_ms >= slow_ms or (not cached and duration_ms >= max(250, slow_ms // 2)):
        return "info"
    return "debug"


def _request_payload() -> Any:
    if request.is_json:
        payload = request.get_json(silent=True)
        if isinstance(payload, dict):
            return payload
    if request.form:
        return request.form
    return request.args or {}


def _state_payload(filters: Any, scope: Dict[str, Any], *, meta: Dict[str, Any] | None = None, action: str | None = None) -> Dict[str, Any]:
    summary = build_filter_summary(filters)
    payload: Dict[str, Any] = {
        "filters": filters_to_store(filters),
        "summary": summary,
        "scope": scope,
        "last_applied_at": session.get("global_filters_last_applied_at"),
        "meta": meta or {},
    }
    if action:
        payload["meta"]["action"] = action
    return payload


@bp.get("/schema")
@login_required
def schema() -> Any:
    sticky_enabled = bool(current_app.config.get("STICKY_FILTERS", True))
    v2_mode = bool(current_app.config.get("FILTERS_CANONICAL_V2", False))
    effective, _meta = resolve_filters(
        request,
        current_user,
        session_obj=session,
        source=request.args or {},
        sticky_enabled=sticky_enabled,
        update_session=sticky_enabled and not v2_mode and not _reset_requested(),
    )
    payload = filters_service.schema(filters=effective)
    payload["scope"] = filters_service.scope_from_user(current_user)
    payload["meta"] = {
        "sanitized": bool(_meta.get("sanitized")),
        "dropped_filters": _meta.get("dropped_filters") or {},
        "filters_notice": _meta.get("notice"),
    }
    return jsonify(payload)


@bp.get("/options")
@login_required
def options() -> Any:
    sticky_enabled = bool(current_app.config.get("STICKY_FILTERS", True))
    v2_mode = bool(current_app.config.get("FILTERS_CANONICAL_V2", False))
    requested_dimensions = filters_service.normalize_requested_option_keys(request.args.getlist("dimensions"))
    params, _meta = resolve_filters(
        request,
        current_user,
        session_obj=session,
        source=request.args or {},
        sticky_enabled=sticky_enabled,
        update_session=sticky_enabled and not v2_mode and not _reset_requested(),
    )
    scope = filters_service.scope_from_user(current_user)
    request_page = str(request.args.get("page") or "").strip().lower() or None
    request_phase = str(request.args.get("phase") or "").strip().lower() or None
    started = time.perf_counter()
    payload_error = None
    try:
        payload = filters_service.get_filter_options(params, scope, requested_keys=requested_dimensions)
        validated_params, payload_meta = filters_service.sanitize_filters_against_options(params, payload)
        if payload_meta.get("sanitized"):
            params = validated_params
            if sticky_enabled and not v2_mode:
                write_sticky_filters_to_session(session, params, user_id=current_user.get_id())
            payload = filters_service.get_filter_options(params, scope, requested_keys=requested_dimensions)
        merged_filter_meta = _merge_filter_meta(_meta, payload_meta)
    except Exception as exc:
        payload_error = exc
        current_app.logger.exception(
            "filters.options.failed",
            extra={
                "dimensions": list(requested_dimensions),
                "page": request_page,
                "phase": request_phase,
                "scope_mode": scope.get("scope_mode"),
                "filter_hash": filters_service.filters_hash(params),
            },
        )
        payload = filters_service.empty_options_payload(params, scope, requested_keys=requested_dimensions, error=exc)
        merged_filter_meta = _merge_filter_meta(_meta)
        payload.setdefault("meta", {})
        payload["meta"]["degraded"] = True
    bind_filter_cache_key(payload.get("meta", {}).get("cache_key") if isinstance(payload.get("meta"), dict) else None)
    duration_ms = payload.get("duration_ms")
    if duration_ms is None:
        duration_ms = int((time.perf_counter() - started) * 1000)
        payload["duration_ms"] = duration_ms
    payload.setdefault("filters", filters_to_store(params))
    payload.setdefault("summary", build_filter_summary(params))
    payload.setdefault("scope", scope)
    payload.setdefault("meta", {})
    if isinstance(payload.get("meta"), dict):
        payload["meta"]["requested_dimensions"] = list(requested_dimensions)
        payload["meta"]["sanitized"] = bool(merged_filter_meta.get("sanitized"))
        payload["meta"]["dropped_filters"] = merged_filter_meta.get("dropped_filters") or {}
        payload["meta"]["filters_notice"] = merged_filter_meta.get("filters_notice")
        payload["meta"].setdefault("degraded", False)
    etag = filters_service.options_etag(payload)
    if etag and request.if_none_match and etag in request.if_none_match:
        resp = current_app.response_class(status=304)
        resp.set_etag(etag)
        resp.headers["Cache-Control"] = f"private, max-age={filters_service.OPTIONS_TTL_SECONDS}"
        resp.vary.add("Cookie")
        return resp
    try:
        extra = {
            "duration_ms": payload.get("duration_ms"),
            "cached": payload.get("cached", False),
            "dataset_version": payload.get("dataset_version"),
            "dimensions": list(requested_dimensions),
            "page": request_page,
            "phase": request_phase,
            "dimension_meta": payload.get("meta", {}).get("dimension_meta") if isinstance(payload.get("meta"), dict) else {},
            "option_counts": payload.get("meta", {}).get("option_counts") if isinstance(payload.get("meta"), dict) else {},
            "degraded": bool(payload.get("meta", {}).get("degraded")) if isinstance(payload.get("meta"), dict) else False,
            "scope_mode": scope.get("scope_mode"),
            "scope_hash": scope.get("scope_hash"),
            "scope_counts": {
                "rep": scope.get("allowed_count"),
                "customer": scope.get("allowed_customer_count"),
                "region": scope.get("allowed_region_count"),
                "supplier": scope.get("allowed_supplier_count"),
            },
            "error": str(payload_error) if payload_error else None,
        }
        log_method = getattr(current_app.logger, _options_log_method(payload, payload_error), current_app.logger.info)
        log_method("filters.options", extra=extra)
    except Exception:
        pass
    resp = jsonify(payload)
    if etag:
        resp.set_etag(etag)
        resp.headers["Cache-Control"] = f"private, max-age={filters_service.OPTIONS_TTL_SECONDS}"
        resp.vary.add("Cookie")
    return resp


@bp.post("/apply")
@login_required
def apply_state() -> Any:
    sticky_enabled = bool(current_app.config.get("STICKY_FILTERS", True))
    scope = filters_service.scope_from_user(current_user)
    params, resolve_meta = resolve_filters(
        request,
        current_user,
        session_obj=session,
        source=_request_payload(),
        sticky_enabled=sticky_enabled,
        update_session=True,
    )
    validated_params, validation_meta = filters_service.validate_filters(params, scope)
    if sticky_enabled:
        write_sticky_filters_to_session(session, validated_params, user_id=current_user.get_id())
    stamp = mark_filters_last_applied(session)
    merged_meta = _merge_filter_meta(resolve_meta, validation_meta)
    response_meta = {
        "sanitized": bool(merged_meta.get("sanitized")),
        "dropped_filters": merged_meta.get("dropped_filters") or {},
        "filters_notice": merged_meta.get("filters_notice"),
        "validation_degraded": bool(validation_meta.get("validation_degraded")),
        "requested_dimensions": validation_meta.get("requested_dimensions") or [],
    }
    payload = _state_payload(validated_params, scope, meta=response_meta, action="apply")
    payload["last_applied_at"] = stamp
    return jsonify(payload)


@bp.post("/reset")
@login_required
def reset_state() -> Any:
    clear_sticky_filters_in_session(session)
    stamp = mark_filters_last_applied(session)
    sticky_enabled = bool(current_app.config.get("STICKY_FILTERS", True))
    scope = filters_service.scope_from_user(current_user)
    params, meta = resolve_filters(
        request,
        current_user,
        session_obj=session,
        source={},
        sticky_enabled=sticky_enabled,
        update_session=sticky_enabled,
    )
    response_meta = {
        "sanitized": bool(meta.get("sanitized")),
        "dropped_filters": meta.get("dropped_filters") or {},
        "filters_notice": meta.get("notice"),
    }
    payload = _state_payload(params, scope, meta=response_meta, action="reset")
    payload["last_applied_at"] = stamp
    return jsonify(payload)
