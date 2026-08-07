"""Data service combining cached parquet via DuckDB with filters."""

from __future__ import annotations

import os
from pathlib import Path
import numpy as np
from typing import Any, Iterable, Optional
from collections.abc import Mapping
import uuid

import pandas as pd
from werkzeug.exceptions import InternalServerError
from flask import request, g, has_request_context
from flask_login import current_user

from app.services.filters import (
    FilterParams,
    normalize_filters as _normalize_filters,
    parse_filters as _parse_filters,
    apply_filters as _apply_filter_params,
)
from app.services import analytics_utils as au
from app.services import fact_store
from app.core.exceptions import DatasetNotBuiltError
from app.services.frame import canonicalize
from app.services.data_access import get_fact_context


def get_fact_df(
    start: Optional[Any] = None,
    end: Optional[Any] = None,
    sales_rep_override: Optional[str] = None, # Added sales_rep_override
    force_refresh: bool = False,
    columns: Optional[list[str]] = None,
    filters: Any = None,
    best_effort: Optional[bool] = None,
) -> pd.DataFrame:
    """Return analytics fact DataFrame from the latest immutable snapshot."""

    parsed_filters: Optional[FilterParams] = None

    if filters is not None:
        parsed_filters = _normalize_filters(filters)
        start = start or getattr(parsed_filters, "start", None)
        end = end or getattr(parsed_filters, "end", None)
    elif start is None and end is None:
        try:
            parsed_filters = _normalize_filters(_parse_filters(getattr(request, "args", {}) or {}))
            start = getattr(parsed_filters, "start", None) or start
            end = getattr(parsed_filters, "end", None) or end
        except Exception:
            parsed_filters = None
    elif start is not None or end is not None:
        try:
            parsed_filters = _normalize_filters(
                FilterParams(
                    start=pd.to_datetime(start) if start is not None else None,
                    end=pd.to_datetime(end) if end is not None else None,
                )
            )
        except Exception:
            parsed_filters = None

    if force_refresh:
        raise DatasetNotBuiltError("Force refresh is disabled in request path; run the ETL job instead.")

    if best_effort is None and has_request_context():
        try:
            raw_be = (getattr(request, "args", {}) or {}).get("best_effort")
            best_effort = str(raw_be).strip().lower() in {"1", "true", "yes", "on"} if raw_be is not None else False
        except Exception:
            best_effort = False
    best_effort_flag = bool(best_effort)
    try:
        from flask import g  # type: ignore
        g.fact_best_effort = best_effort_flag
    except Exception:
        pass

    ctx = get_fact_context(
        user=current_user if "current_user" in globals() else None,
        filters=parsed_filters,
        columns=columns,
        sales_rep_override=sales_rep_override,
        best_effort=best_effort_flag,
    )
    df = ctx.df

    return df

def fetch_df(start: str | None = None, end: str | None = None, statuses: Optional[list[str]] = None, *, best_effort: bool = False) -> pd.DataFrame:
    """Fetch a filtered fact frame from the parquet/DuckDB store (no live SQL)."""
    filters: dict[str, Any] = {}
    if start is not None:
        filters["start"] = start
    if end is not None:
        filters["end"] = end
    if statuses is not None:
        filters["statuses"] = statuses

    if not best_effort and has_request_context():
        try:
            raw_be = request.args.get("best_effort")
            if raw_be is not None:
                best_effort = str(raw_be).strip().lower() in {"1", "true", "yes", "on"}
        except Exception:
            best_effort = False

    df = fact_store.query_fact(filters=filters, use_cache=True)
    df = canonicalize(df)
    if best_effort:
        cost_col = au.cost_column(df) or "Cost"
        rev_col = au.revenue_column(df) or "Revenue"
        df[cost_col] = au.to_numeric_safe(df.get(cost_col, pd.Series(dtype=float))).fillna(0.0)
        if rev_col in df.columns:
            df["Profit"] = au.to_numeric_safe(df[rev_col]).fillna(0.0) - df[cost_col]
    try:
        df.reset_index(drop=True, inplace=True)
    except Exception:
        pass
    return df

def apply_global_filters(df: pd.DataFrame, form_or_dict: Any) -> pd.DataFrame:
    """Apply canonical filters to the DataFrame using FilterParams."""
    if df is None or df.empty:
        return df

    if hasattr(form_or_dict, "data"):
        source = form_or_dict.data
    elif isinstance(form_or_dict, Mapping):
        source = form_or_dict
    else:
        try:
            source = dict(form_or_dict)  # type: ignore[arg-type]
        except Exception:
            source = {}

    params = _parse_filters(source)
    return _apply_filter_params(df, params)

def get_status() -> str:
    return "data_service ready"
