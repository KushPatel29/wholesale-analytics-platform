from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import time
import uuid
from typing import Any, Callable, Optional

from flask import g, has_request_context, request


def _current_request_id() -> Optional[str]:
    try:
        return getattr(g, "request_id", None)
    except Exception:
        return None


def _hash_payload(obj: Any) -> Optional[str]:
    try:
        serialized = json.dumps(obj or {}, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    except Exception:
        return None


def _request_fingerprint() -> Optional[str]:
    """Stable hash of query params + JSON/form body for observability."""
    if not has_request_context():
        return None
    try:
        payload: dict[str, Any] = {"args": request.args.to_dict(flat=False)}
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.is_json:
                payload["json"] = request.get_json(silent=True)
            else:
                payload["form"] = request.form.to_dict(flat=False)
        return _hash_payload(payload)
    except Exception:
        return None


def _log(logger: logging.Logger, event: str, extra: Optional[dict[str, Any]] = None) -> None:
    try:
        payload = {"event": event, "request_id": _current_request_id()}
        if extra:
            payload.update({k: v for k, v in extra.items() if v is not None})
        verbose = str(os.getenv("DEBUG_OBS") or os.getenv("OBS_VERBOSE") or os.getenv("DEBUG") or "").strip().lower() in {"1", "true", "yes", "on"}
        log_fn = logger.info if verbose else logger.debug
        log_fn("obs", extra=payload)
    except Exception:
        logger.debug("obs.log_failed", exc_info=True)


def _flag_on(raw: Any) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_int(raw: Any, default: int) -> int:
    try:
        return max(0, int(str(raw).strip()))
    except Exception:
        return default


def _request_summary_log_info(resp: Any, duration_ms: Optional[int], meta: dict[str, Any], duck: dict[str, Any]) -> bool:
    if _flag_on(os.getenv("REQUEST_SUMMARY_LOG_ALL")) or _flag_on(os.getenv("DEBUG_OBS")) or _flag_on(os.getenv("OBS_VERBOSE")) or _flag_on(os.getenv("DEBUG")):
        return True
    slow_ms = _positive_int(os.getenv("REQUEST_SUMMARY_INFO_MS"), 1200)
    query_ms = _positive_int(duck.get("total_ms") or 0, 0) if duck.get("total_ms") is not None else 0
    query_count = _positive_int(duck.get("count") or 0, 0) if duck.get("count") is not None else 0
    cached = meta.get("cache_hit") if meta.get("cache_hit") is not None else meta.get("cached")
    if getattr(resp, "status_code", 200) >= 500:
        return True
    if duration_ms is not None and duration_ms >= slow_ms:
        return True
    if query_ms >= slow_ms:
        return True
    if cached is False and duration_ms is not None and duration_ms >= max(250, slow_ms // 2):
        return True
    if query_count >= 8 and query_ms >= 250:
        return True
    return False


def install_request_logging(app) -> None:
    """Attach request-id and start/end logging middleware."""

    @app.before_request
    def _obs_request_start():  # pragma: no cover - side-effect logging
        rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        g.request_id = rid
        g._req_started_at = time.time()
        g._req_t0 = time.perf_counter()
        g._req_fingerprint = _request_fingerprint()
        _log(
            app.logger,
            "request.start",
            {
                "method": request.method,
                "path": request.path,
                "route": request.url_rule.rule if getattr(request, "url_rule", None) else request.path,
                "fingerprint": g._req_fingerprint,
                "started_at": g._req_started_at,
            },
        )

    @app.after_request
    def _obs_request_end(resp):  # pragma: no cover - side-effect logging
        duration_ms: Optional[int] = None
        started = getattr(g, "_req_t0", None)
        if isinstance(started, (int, float)):
            try:
                duration_ms = int((time.perf_counter() - started) * 1000)
            except Exception:
                duration_ms = None

        resp.headers.setdefault("X-Request-ID", getattr(g, "request_id", "") or "")
        try:
            filters_meta = getattr(g, "effective_filters_meta", {}) or {}
        except Exception:
            filters_meta = {}
        if isinstance(filters_meta, dict):
            if filters_meta.get("filters_hash"):
                resp.headers.setdefault("X-Filters-Hash", str(filters_meta.get("filters_hash")))
            if filters_meta.get("source"):
                resp.headers.setdefault("X-Filters-Source", str(filters_meta.get("source")))
        try:
            cache_key_hash = getattr(g, "filter_cache_key_hash", None)
        except Exception:
            cache_key_hash = None
        if cache_key_hash:
            resp.headers.setdefault("X-Filter-Cache-Key-Hash", str(cache_key_hash))

        _log(
            app.logger,
            "request.complete",
            {
                "method": request.method,
                "path": request.path,
                "route": request.url_rule.rule if getattr(request, "url_rule", None) else request.path,
                "status": resp.status_code,
                "duration_ms": duration_ms,
                "fingerprint": getattr(g, "_req_fingerprint", None),
                "started_at": getattr(g, "_req_started_at", None),
                "ended_at": time.time(),
            },
        )
        if str(getattr(request, "path", "")).startswith("/api/"):
            try:
                meta = getattr(g, "fact_meta", {}) or {}
            except Exception:
                meta = {}
            try:
                filters_meta = getattr(g, "effective_filters_meta", {}) or {}
            except Exception:
                filters_meta = {}
            try:
                duck = getattr(g, "_duckdb_stats", {}) or {}
            except Exception:
                duck = {}
            try:
                serialize_ms = getattr(g, "_serialize_ms", None)
            except Exception:
                serialize_ms = None
            try:
                cache_key_hash = getattr(g, "filter_cache_key_hash", None)
            except Exception:
                cache_key_hash = None
            summary = {
                "request_id": getattr(g, "request_id", None),
                "path": request.path,
                "method": request.method,
                "status": resp.status_code,
                "cached": meta.get("cache_hit") or meta.get("cached"),
                "duckdb_query_count": duck.get("count"),
                "duckdb_total_ms": duck.get("total_ms"),
                "query_count": duck.get("count"),
                "query_ms": duck.get("total_ms"),
                "serialization_ms": serialize_ms,
                "serialize_ms": serialize_ms,
                "total_ms": duration_ms,
                "dataset_version": meta.get("data_version"),
                "role": meta.get("role"),
                "user_id": meta.get("user_id"),
                "current_user_id": filters_meta.get("current_user_id") or meta.get("user_id"),
                "effective_filters": filters_meta.get("effective_filters"),
                "filters_hash": filters_meta.get("filters_hash"),
                "filters_source": filters_meta.get("filters_source"),
                "filter_resolution_source": filters_meta.get("source"),
                "window_start": filters_meta.get("window_start"),
                "window_end": filters_meta.get("window_end"),
                "endpoint": filters_meta.get("endpoint") or request.path,
                "cache_key_hash": cache_key_hash,
            }
            try:
                log_fn = app.logger.info if _request_summary_log_info(resp, duration_ms, meta, duck) else app.logger.debug
                log_fn("api.request.summary", extra=summary)
            except Exception:
                app.logger.debug("api.request.summary_failed", exc_info=True)
        return resp


def patch_pandas_logging(logger: Optional[logging.Logger] = None) -> None:
    """Log every pandas parquet read/write with duration and request-id."""
    try:
        import pandas as pd  # type: ignore
    except Exception:
        return
    log = logger or logging.getLogger(__name__)
    if getattr(pd, "_wa_parquet_patched", False):
        return

    def _log_parquet(event: str, path_hint: Any, started: Optional[float] = None) -> None:
        duration_ms = None
        if started is not None:
            try:
                duration_ms = int((time.perf_counter() - started) * 1000)
            except Exception:
                duration_ms = None
        _log(
            log,
            event,
            {"path": str(path_hint) if path_hint is not None else None, "duration_ms": duration_ms},
        )

    orig_read = pd.read_parquet

    @functools.wraps(orig_read)
    def _wrapped_read(*args, **kwargs):
        target = args[0] if args else kwargs.get("path") or kwargs.get("filepath_or_buffer")
        started = time.perf_counter()
        _log_parquet("pandas.read_parquet.start", target, None)
        try:
            return orig_read(*args, **kwargs)
        finally:
            _log_parquet("pandas.read_parquet.complete", target, started)

    orig_to_parquet = pd.DataFrame.to_parquet

    @functools.wraps(orig_to_parquet)
    def _wrapped_to_parquet(self, *args, **kwargs):
        target = args[0] if args else kwargs.get("path") or kwargs.get("fname")
        started = time.perf_counter()
        _log_parquet("pandas.to_parquet.start", target, None)
        try:
            return orig_to_parquet(self, *args, **kwargs)
        finally:
            _log_parquet("pandas.to_parquet.complete", target, started)

    pd.read_parquet = _wrapped_read  # type: ignore[assignment]
    pd.DataFrame.to_parquet = _wrapped_to_parquet  # type: ignore[assignment]
    pd._wa_parquet_patched = True  # type: ignore[attr-defined]


def patch_data_loader_logging(logger: Optional[logging.Logger] = None) -> None:
    """Wrap SQL data_loader entrypoints to log timing and request-id."""
    try:
        import data_loader  # type: ignore
    except Exception:
        return

    log = logger or logging.getLogger(__name__)

    def _wrap(name: str) -> None:
        fn: Optional[Callable[..., Any]] = getattr(data_loader, name, None)
        if fn is None or not callable(fn) or getattr(fn, "_wa_obs_wrapped", False):
            return

        @functools.wraps(fn)
        def _wrapped(*args, **kwargs):
            started = time.perf_counter()
            _log(log, "sql.query.start", {"fn": name})
            try:
                return fn(*args, **kwargs)
            finally:
                _log(log, "sql.query.complete", {"fn": name, "duration_ms": int((time.perf_counter() - started) * 1000)})

        _wrapped._wa_obs_wrapped = True  # type: ignore[attr-defined]
        setattr(data_loader, name, _wrapped)

    for target in ("get_dataframe_for_user", "get_dataframe", "run_query"):
        _wrap(target)
