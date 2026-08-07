from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import asdict, is_dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
import math
import time
from threading import Lock

import numpy as np
import pandas as pd
from flask import current_app
from flask_login import current_user

from app.cache import cache
from app.services import analytics_utils as au
from app.services import overview_v2 as ov2
from app.services.filters import FilterParams, filters_cache_key, normalize_filters
from data.store import manifest_max_date, manifest_version

FORECAST_TTL_SECONDS = 600  # 10 minutes
FORECAST_TIMEOUT_SECONDS = 6
ALLOWED_METRICS = {"revenue", "profit", "margin"}
MARGIN_BOUNDS = (-100.0, 100.0)
MAX_HISTORY_MONTHS = 144
MODEL_VERSION = "2026-03-28-forecast-6"
MODEL_VERSION_V2 = "2026-03-28-forecast-6"
OUTLIER_METHOD = "hampel"
OUTLIER_WINDOW = 3
OUTLIER_N_SIGMA = 3.0
MIN_MONTHLY_FORECAST_POINTS = 6
MIN_WEEKLY_FORECAST_POINTS = 16
RECENT_SHORT_WINDOW = 24
RECENT_MEDIUM_WINDOW = 36
RECENT_TREND_WINDOW = 18
MAX_CANDIDATE_WINDOWS = 6
NOWCAST_LOOKBACK_MONTHS = 24
NOWCAST_MAX_EVAL_MONTHS = 12
NOWCAST_MIN_CANDIDATES = 2
NOWCAST_TOP_CANDIDATES = 3
NOWCAST_MIN_SHARE = 0.05
NOWCAST_MAX_SHARE = 0.98

_forecast_lock: Lock = Lock()
_forecast_locks: Dict[str, Lock] = {}


def _filters_payload(filters: FilterParams) -> Dict[str, Any]:
    """Return a JSON-safe view of the filters for hashing/logging."""
    params = normalize_filters(filters)
    if is_dataclass(params):
        data = asdict(params)
    else:  # pragma: no cover - defensive
        data = getattr(params, "__dict__", {}) or {}
    for key in ("regions", "methods", "customers", "suppliers", "products", "sales_reps"):
        if key in data and isinstance(data[key], (list, tuple, set)):
            data[key] = sorted(data[key])
    for key in ("start", "end"):
        val = data.get(key)
        if isinstance(val, pd.Timestamp):
            data[key] = val.isoformat()
    return data


def _dataset_marker() -> str:
    return manifest_max_date() or manifest_version() or ""


def _normalize_metric(metric: str | None) -> str:
    value = (metric or "revenue").strip().lower()
    if value == "margin_pct":
        return "margin"
    return value


def _lock_for(key: str) -> Lock:
    with _forecast_lock:
        lock = _forecast_locks.get(key)
        if lock is None:
            lock = Lock()
            _forecast_locks[key] = lock
    return lock


def _hampel_filter(series: pd.Series, window: int = OUTLIER_WINDOW, n_sigma: float = OUTLIER_N_SIGMA) -> Tuple[pd.Series, int]:
    if series.empty:
        return series, 0
    s = series.copy()
    values = s.values.astype(float)
    n = len(values)
    k = 1.4826
    outliers = 0
    for i in range(n):
        start = max(0, i - window)
        end = min(n, i + window + 1)
        window_vals = values[start:end]
        med = np.nanmedian(window_vals)
        mad = np.nanmedian(np.abs(window_vals - med))
        if mad == 0 or np.isnan(mad):
            continue
        threshold = n_sigma * k * mad
        if abs(values[i] - med) > threshold:
            values[i] = med
            outliers += 1
    s[:] = values
    return s, outliers


def _clean_series(series: pd.Series) -> Tuple[pd.Series, int]:
    if series.empty:
        return series, 0
    method = OUTLIER_METHOD
    if method == "hampel":
        return _hampel_filter(series)
    return series, 0


def _backtest_metrics(series: pd.Series) -> Dict[str, Any]:
    values = series.dropna()
    n_total = len(values)
    if n_total < 3:
        return {"mape": None, "smape": None, "n": 0}
    n = min(36, n_total - 1)
    actual = values.iloc[-n:]
    preds: List[float] = []
    for i in range(n):
        idx = n_total - n + i
        if n_total >= 12 and idx - 12 >= 0:
            pred = float(values.iloc[idx - 12])
        else:
            history_slice = values.iloc[:idx]
            window = min(3, len(history_slice)) if len(history_slice) else 1
            pred = float(history_slice.tail(window).mean() if len(history_slice) else values.iloc[0])
        preds.append(pred)
    actual_vals = actual.values.astype(float)
    preds_arr = np.array(preds, dtype=float)
    mask = actual_vals != 0
    mape = None
    if mask.any():
        mape = float(np.mean(np.abs((actual_vals[mask] - preds_arr[mask]) / actual_vals[mask])) * 100)
    denom = np.abs(actual_vals) + np.abs(preds_arr)
    smape = None
    if np.any(denom):
        smape = float(np.mean(2 * np.abs(actual_vals - preds_arr) / np.where(denom == 0, 1, denom)) * 100)
    return {"mape": mape, "smape": smape, "n": int(n)}


def _error_metrics(actual: np.ndarray, predicted: np.ndarray) -> Dict[str, Optional[float]]:
    if actual.size == 0 or predicted.size == 0:
        return {"mape": None, "smape": None, "mae": None}
    err = np.abs(actual - predicted)
    mae = float(np.mean(err)) if err.size else None
    nonzero = actual != 0
    mape = float(np.mean(np.abs((actual[nonzero] - predicted[nonzero]) / actual[nonzero])) * 100) if np.any(nonzero) else None
    denom = np.abs(actual) + np.abs(predicted)
    smape = float(np.mean(2 * np.abs(actual - predicted) / np.where(denom == 0, 1, denom)) * 100) if np.any(denom) else None
    return {"mape": mape, "smape": smape, "mae": mae}


def _split_train_test(series: pd.Series, *, min_train: int = 6, max_test: int = 12) -> Tuple[pd.Series, pd.Series]:
    values = series.dropna().astype(float)
    n = len(values)
    if n <= min_train + 1:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    test_size = min(max_test, max(2, n // 4))
    if (n - test_size) < min_train:
        test_size = n - min_train
    if test_size <= 0:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    train = values.iloc[:-test_size]
    test = values.iloc[-test_size:]
    return train, test


def _forecast_seasonal_naive(history: pd.Series, horizon: int) -> pd.Series:
    if history.empty:
        return pd.Series(dtype=float)
    idx = pd.period_range(start=history.index.max() + 1, periods=horizon, freq="M")
    values: List[float] = []
    for step in range(horizon):
        if len(history) >= 12:
            ref_pos = -12 + step
            if abs(ref_pos) > len(history):
                ref_pos = -12
            values.append(float(history.iloc[ref_pos]))
        else:
            values.append(float(history.iloc[-1]))
    return pd.Series(values, index=idx)


def _forecast_mean(history: pd.Series, horizon: int, window: int = 6) -> pd.Series:
    if history.empty:
        return pd.Series(dtype=float)
    idx = pd.period_range(start=history.index.max() + 1, periods=horizon, freq="M")
    lookback = max(1, min(window, len(history)))
    mean_val = float(history.tail(lookback).mean() or 0.0)
    return pd.Series([mean_val] * horizon, index=idx)


def _forecast_naive_last(history: pd.Series, horizon: int) -> pd.Series:
    if history.empty:
        return pd.Series(dtype=float)
    idx = pd.period_range(start=history.index.max() + 1, periods=horizon, freq="M")
    last = float(history.iloc[-1] or 0.0)
    return pd.Series([last] * horizon, index=idx)


def _forecast_ets(history: pd.Series, horizon: int) -> Tuple[pd.Series, float]:
    if history.empty:
        return pd.Series(dtype=float), 0.0
    from statsmodels.tsa.holtwinters import ExponentialSmoothing  # type: ignore

    def _fit_and_forecast():
        use_damped = len(history) >= 24
        model = ExponentialSmoothing(
            history,
            trend="add",
            seasonal="add",
            seasonal_periods=12,
            initialization_method="estimated",
            damped_trend=use_damped,
        )
        fitted = model.fit(optimized=True)
        fc = pd.Series(fitted.forecast(horizon))
        resid = _residual_band(fitted.fittedvalues, history)
        return fc, resid

    fc_series, resid_std = _forecast_with_timeout(_fit_and_forecast, FORECAST_TIMEOUT_SECONDS)
    return pd.Series(fc_series), float(resid_std or 0.0)


def _evaluate_candidate(
    history: pd.Series,
    *,
    name: str,
    forecast_fn: Callable[[pd.Series, int], pd.Series],
) -> Dict[str, Any]:
    train, test = _split_train_test(history)
    if train.empty or test.empty:
        return {"name": name, "mape": None, "smape": None, "mae": None, "n": 0}
    preds = forecast_fn(train, len(test))
    if preds.empty:
        return {"name": name, "mape": None, "smape": None, "mae": None, "n": 0}
    actual_vals = test.values.astype(float)
    pred_vals = np.array([float(v) if pd.notna(v) else np.nan for v in preds.values], dtype=float)
    mask = np.isfinite(actual_vals) & np.isfinite(pred_vals)
    if not np.any(mask):
        return {"name": name, "mape": None, "smape": None, "mae": None, "n": 0}
    metrics = _error_metrics(actual_vals[mask], pred_vals[mask])
    metrics["name"] = name
    metrics["n"] = int(np.sum(mask))
    return metrics


def _confidence_from_smape(smape: Optional[float]) -> Optional[str]:
    if smape is None:
        return None
    if smape <= 10:
        return "high"
    if smape <= 20:
        return "medium"
    return "low"


def _monthly_from_frame_context(frame_ctx: Any) -> pd.DataFrame:
    """Build a monthly series from a legacy FrameContext (used in tests)."""
    try:
        df = getattr(frame_ctx, "df", None)
        colmap = getattr(frame_ctx, "colmap", {}) or {}
    except Exception:
        return pd.DataFrame()
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    date_col = colmap.get("date")
    rev_col = colmap.get("revenue")
    cost_col = colmap.get("cost")
    qty_col = colmap.get("qty")
    if not date_col or date_col not in df.columns or not rev_col or rev_col not in df.columns:
        return pd.DataFrame()

    work = pd.DataFrame()
    work["Date"] = pd.to_datetime(df[date_col], errors="coerce")
    work = work.dropna(subset=["Date"])
    if work.empty:
        return pd.DataFrame()
    work["month"] = work["Date"].dt.to_period("M")
    work["revenue"] = pd.to_numeric(df[rev_col], errors="coerce").fillna(0.0)
    if cost_col and cost_col in df.columns:
        work["cost"] = pd.to_numeric(df[cost_col], errors="coerce").fillna(0.0)
    else:
        work["cost"] = 0.0
    if qty_col and qty_col in df.columns:
        work["qty"] = pd.to_numeric(df[qty_col], errors="coerce").fillna(0.0)
    else:
        work["qty"] = 0.0

    monthly = work.groupby("month")[["revenue", "cost", "qty"]].sum().sort_index()
    if monthly.empty:
        return monthly

    monthly["profit"] = monthly["revenue"] - monthly["cost"]
    monthly["margin_pct"] = au.safe_div(monthly["profit"], monthly["revenue"]) * 100
    monthly["margin_pct"] = (
        monthly["margin_pct"].replace([np.inf, -np.inf], np.nan).clip(lower=MARGIN_BOUNDS[0], upper=MARGIN_BOUNDS[1])
    )
    monthly["asp"] = au.safe_div(monthly["revenue"], monthly["qty"].replace(0, pd.NA))
    monthly["month_start"] = monthly.index.to_timestamp().normalize()
    return monthly


def monthly_series(filters: FilterParams, *, include_partial_current: bool = True) -> Tuple[pd.DataFrame, dict]:
    """
    Aggregated monthly series for the current filters.

    Returns:
        monthly dataframe indexed by Period["M"] with columns revenue, cost, profit, margin_pct, qty, asp
        frame context (for logging/cache metadata)
    """
    ctx: dict = {}
    monthly = pd.DataFrame()

    # TESTING fallback: allow monkeypatched get_filtered_frame to drive forecasts.
    if current_app and current_app.config.get("TESTING"):
        try:
            frame_ctx = ov2.get_filtered_frame(current_user, filters)
            monthly = _monthly_from_frame_context(frame_ctx)
            if not monthly.empty:
                rows_val = len(getattr(frame_ctx, "df", monthly))
                ctx = {
                    "payload": {
                        "health": {"rows": rows_val},
                        "meta": {
                            "version": getattr(frame_ctx, "version", None),
                            "cache_hit": bool(getattr(frame_ctx, "cache_hit", False)),
                        },
                    },
                    "monthly": monthly,
                    "cache_hit": bool(getattr(frame_ctx, "cache_hit", False)),
                    "version": getattr(frame_ctx, "version", None),
                }
        except Exception:
            ctx = {}
            monthly = pd.DataFrame()

    if monthly.empty:
        ctx = ov2.get_bundle_context(filters, include_current_month=bool(include_partial_current), defaulted_window=False)
        monthly = ctx.get("monthly")
        if not isinstance(monthly, pd.DataFrame) or monthly.empty:
            return pd.DataFrame(), ctx

    monthly = monthly.sort_index()
    if not include_partial_current and not monthly.empty:
        now_period = pd.Timestamp.utcnow().tz_localize(None).to_period("M")
        if monthly.index.max() == now_period and len(monthly) > 1:
            monthly = monthly.iloc[:-1]

    if "profit" not in monthly.columns:
        monthly["profit"] = monthly.get("revenue", pd.Series(dtype=float)) - monthly.get("cost", pd.Series(dtype=float))
    if "margin_pct" in monthly.columns:
        monthly["margin_pct"] = monthly["margin_pct"].replace([np.inf, -np.inf], np.nan)
        monthly["margin_pct"] = monthly["margin_pct"].clip(lower=MARGIN_BOUNDS[0], upper=MARGIN_BOUNDS[1])
    else:
        monthly["margin_pct"] = au.safe_div(monthly.get("profit"), monthly.get("revenue")) * 100
        monthly["margin_pct"] = monthly["margin_pct"].replace([np.inf, -np.inf], np.nan)
        monthly["margin_pct"] = monthly["margin_pct"].clip(lower=MARGIN_BOUNDS[0], upper=MARGIN_BOUNDS[1])

    if "asp" in monthly.columns:
        monthly["asp"] = monthly["asp"].where(~monthly["asp"].isna(), np.nan)
    monthly["month_start"] = monthly.index.to_timestamp().normalize()
    return monthly, ctx


def _series_for_metric(monthly: pd.DataFrame, metric: str) -> pd.Series:
    metric = _normalize_metric(metric)
    column = {
        "revenue": "revenue",
        "profit": "profit",
        "margin": "margin_pct",
    }.get(metric)
    if not column or column not in monthly.columns:
        return pd.Series(dtype=float)
    series = monthly[column]
    series = pd.to_numeric(series, errors="coerce")
    if isinstance(series.index, pd.PeriodIndex):
        try:
            series = series.asfreq("M")
        except Exception:
            pass
    return series


def _forecast_with_timeout(fn, timeout: float):
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(fn)
        return fut.result(timeout=timeout)


def _residual_band(fitted: pd.Series, actual: pd.Series) -> float:
    if fitted is None or actual is None or fitted.empty or actual.empty:
        return 0.0
    residuals = actual.align(fitted, join="left")[0] - fitted
    try:
        return float(residuals.std(skipna=True) or 0.0)
    except Exception:
        return 0.0


def _clip_non_negative(series: pd.Series) -> pd.Series:
    return series.apply(lambda v: max(0.0, float(v) if pd.notna(v) else 0.0))


def _bound_value(metric: str, value: float | None) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if metric in {"revenue", "profit"}:
        return max(0.0, float(value))
    if metric == "margin":
        return float(min(MARGIN_BOUNDS[1], max(MARGIN_BOUNDS[0], float(value))))
    return float(value)


def _apply_metric_bounds(series: pd.Series, metric: str) -> pd.Series:
    if series.empty:
        return series
    metric = _normalize_metric(metric)
    if metric in {"revenue", "profit"}:
        return _clip_non_negative(series)
    if metric == "margin":
        return series.clip(lower=MARGIN_BOUNDS[0], upper=MARGIN_BOUNDS[1])
    return series


def _model_info_payload(payload: Dict[str, Any], history_points: int) -> Dict[str, Any]:
    backtest = payload.get("backtest") or {}
    return {
        "name": payload.get("model_used"),
        "mape": backtest.get("mape"),
        "smape": backtest.get("smape"),
        "n_points": history_points,
    }


def _normalize_granularity(granularity: str | None) -> str:
    token = str(granularity or "monthly").strip().lower()
    if token in {"weekly", "week", "w"}:
        return "weekly"
    return "monthly"


def _future_index(last_ts: pd.Timestamp, horizon: int, granularity: str) -> pd.DatetimeIndex:
    if _normalize_granularity(granularity) == "weekly":
        start = pd.Timestamp(last_ts) + pd.Timedelta(days=7)
        return pd.date_range(start=start.normalize(), periods=horizon, freq="W-MON")
    start = (pd.Timestamp(last_ts) + pd.offsets.MonthBegin(1)).normalize()
    return pd.date_range(start=start, periods=horizon, freq="MS")


def _forecast_naive_generic(history: pd.Series, horizon: int, granularity: str) -> pd.Series:
    if history.empty:
        return pd.Series(dtype=float)
    last = float(history.iloc[-1] or 0.0)
    idx = _future_index(pd.Timestamp(history.index[-1]), horizon, granularity)
    return pd.Series([last] * horizon, index=idx, dtype="float64")


def _forecast_seasonal_naive_generic(history: pd.Series, horizon: int, period: int, granularity: str) -> pd.Series:
    if history.empty:
        return pd.Series(dtype=float)
    idx = _future_index(pd.Timestamp(history.index[-1]), horizon, granularity)
    vals: List[float] = []
    n = len(history)
    for step in range(horizon):
        if n >= period:
            ref = max(0, n - period + (step % period))
        else:
            ref = n - 1
        vals.append(float(history.iloc[ref] or 0.0))
    return pd.Series(vals, index=idx, dtype="float64")


def _forecast_ets_generic(
    history: pd.Series,
    horizon: int,
    *,
    granularity: str,
    seasonal_periods: int | None = None,
    damped: bool = True,
    trend_mode: str | None = "add",
    seasonal_mode: str | None = "add",
) -> Tuple[pd.Series, float]:
    if history.empty:
        return pd.Series(dtype=float), 0.0
    from statsmodels.tsa.holtwinters import ExponentialSmoothing  # type: ignore

    gran = _normalize_granularity(granularity)
    inferred_freq = "W-MON" if gran == "weekly" else "MS"

    def _fit_and_forecast():
        use_seasonal = seasonal_periods is not None and len(history) >= int(seasonal_periods) * 2
        seasonal_token = seasonal_mode if use_seasonal else None
        if seasonal_token == "mul":
            try:
                if bool((history <= 0).any()):
                    seasonal_token = "add"
            except Exception:
                seasonal_token = "add"
        model = ExponentialSmoothing(
            history,
            trend=trend_mode,
            damped_trend=bool(damped),
            seasonal=seasonal_token,
            seasonal_periods=int(seasonal_periods) if use_seasonal else None,
            initialization_method="estimated",
        )
        fitted = model.fit(optimized=True)
        fc = pd.Series(fitted.forecast(horizon))
        if not isinstance(fc.index, pd.DatetimeIndex):
            fc.index = _future_index(pd.Timestamp(history.index[-1]), horizon, granularity)
        else:
            try:
                fc = fc.asfreq(inferred_freq)
            except Exception:
                pass
        resid = _residual_band(pd.Series(fitted.fittedvalues, index=history.index), history)
        return fc, resid

    fc_series, resid_std = _forecast_with_timeout(_fit_and_forecast, FORECAST_TIMEOUT_SECONDS)
    return pd.Series(fc_series), float(resid_std or 0.0)


def _forecast_arima_generic(
    history: pd.Series,
    horizon: int,
    *,
    granularity: str,
    seasonal_periods: int | None = None,
) -> Tuple[pd.Series, float]:
    if history.empty:
        return pd.Series(dtype=float), 0.0
    from statsmodels.tsa.statespace.sarimax import SARIMAX  # type: ignore

    def _fit_and_forecast():
        use_seasonal = seasonal_periods is not None and len(history) >= int(seasonal_periods) * 2
        model = SARIMAX(
            history,
            order=(1, 1, 1),
            seasonal_order=(1, 0, 0, int(seasonal_periods)) if use_seasonal else (0, 0, 0, 0),
            enforce_stationarity=False,
            enforce_invertibility=False,
        )
        fitted = model.fit(disp=False, maxiter=200)
        fc = pd.Series(fitted.forecast(steps=horizon))
        if not isinstance(fc.index, pd.DatetimeIndex):
            fc.index = _future_index(pd.Timestamp(history.index[-1]), horizon, granularity)
        fitted_vals = pd.Series(getattr(fitted, "fittedvalues", pd.Series(dtype=float)))
        if not fitted_vals.empty:
            try:
                fitted_vals.index = history.index[-len(fitted_vals.index):]
            except Exception:
                pass
        resid = _residual_band(fitted_vals, history)
        return fc, resid

    fc_series, resid_std = _forecast_with_timeout(_fit_and_forecast, FORECAST_TIMEOUT_SECONDS)
    return pd.Series(fc_series), float(resid_std or 0.0)


def _rolling_origin_eval(
    history: pd.Series,
    *,
    forecast_fn: Callable[[pd.Series, int], pd.Series],
    holdout_points: int,
) -> Dict[str, Any]:
    clean = history.dropna().astype(float)
    n = len(clean)
    if n < 8:
        return {"mape": None, "smape": None, "mae": None, "n": 0, "resid_std": 0.0}
    holdout = min(max(3, int(holdout_points)), max(3, n // 3))
    if (n - holdout) < 6:
        holdout = max(2, n - 6)
    if holdout <= 1:
        return {"mape": None, "smape": None, "mae": None, "n": 0, "resid_std": 0.0}

    actuals: List[float] = []
    preds: List[float] = []
    for pos in range(n - holdout, n):
        train = clean.iloc[:pos]
        if len(train) < 6:
            continue
        try:
            pred_series = forecast_fn(train, 1)
        except Exception:
            continue
        if pred_series is None or pred_series.empty:
            continue
        try:
            pred = float(pred_series.iloc[0])
            actual = float(clean.iloc[pos])
        except Exception:
            continue
        if not (np.isfinite(pred) and np.isfinite(actual)):
            continue
        actuals.append(actual)
        preds.append(pred)

    if len(actuals) < 2:
        return {"mape": None, "smape": None, "mae": None, "n": int(len(actuals)), "resid_std": 0.0}

    actual_arr = np.asarray(actuals, dtype=float)
    pred_arr = np.asarray(preds, dtype=float)
    metrics = _error_metrics(actual_arr, pred_arr)
    resid = actual_arr - pred_arr
    metrics["n"] = int(len(actual_arr))
    metrics["resid_std"] = float(np.std(resid, ddof=1)) if len(resid) > 1 else 0.0
    return metrics


def _build_weekly_history(filters: FilterParams, metric: str, *, include_current: bool) -> Tuple[pd.Series, Dict[str, Any]]:
    bundle = ov2.build_overview_bundle(filters, include_current_month=bool(include_current), defaulted_window=False)
    trend = bundle.get("trend") or {}
    weekly = trend.get("weekly") or {}
    labels = list(weekly.get("months") or [])
    metric_key = {"revenue": "revenue", "profit": "profit", "margin": "margin_pct"}.get(_normalize_metric(metric), "revenue")
    values = list(weekly.get(metric_key) or [])
    if not labels or not values:
        return pd.Series(dtype="float64"), {"bundle_meta": bundle.get("meta") or {}}
    limit = min(len(labels), len(values))
    idx = pd.to_datetime(labels[:limit], errors="coerce")
    arr = pd.to_numeric(pd.Series(values[:limit]), errors="coerce")
    series = pd.Series(arr.values, index=idx).dropna()
    series = series.sort_index()
    if not include_current and not series.empty:
        now_floor = pd.Timestamp.utcnow().tz_localize(None).normalize()
        series = series[series.index <= now_floor]
    series = series.tail(MAX_HISTORY_MONTHS * 5)
    return series.astype("float64"), {"bundle_meta": bundle.get("meta") or {}}


def _build_monthly_history(filters: FilterParams, metric: str, *, include_current: bool) -> Tuple[pd.Series, pd.Series, Dict[str, Any]]:
    monthly, ctx = monthly_series(filters, include_partial_current=True)
    if not isinstance(monthly, pd.DataFrame) or monthly.empty:
        return pd.Series(dtype="float64"), pd.Series(dtype="float64"), {"bundle_ctx": ctx, "partial_period": {"detected": False}}

    work = _regularize_monthly_frame(monthly)
    partial_meta = _terminal_month_meta(filters, work)
    nowcast_payload: Dict[str, Any] = {}
    closed_work = work.copy()
    if partial_meta.get("detected") and len(closed_work.index) > 1:
        closed_work = closed_work.iloc[:-1]

    if partial_meta.get("detected") and include_current:
        nowcast_payload = _current_month_nowcast(filters, work)
        if nowcast_payload.get("applied"):
            period = pd.Period(str(nowcast_payload.get("period")), freq="M")
            if period in work.index:
                work.loc[period, "revenue"] = float(nowcast_payload.get("estimated_month_end_revenue") or work.loc[period, "revenue"])
                work.loc[period, "cost"] = float(nowcast_payload.get("estimated_month_end_cost") or work.loc[period, "cost"])
                work.loc[period, "profit"] = float(nowcast_payload.get("estimated_month_end_profit") or work.loc[period, "profit"])
                work.loc[period, "margin_pct"] = _bound_value("margin", nowcast_payload.get("estimated_month_end_margin_pct"))
            partial_meta["included"] = True
            partial_meta["excluded"] = False
            partial_meta["nowcast_applied"] = True
            basis = str(nowcast_payload.get("current_month_basis") or "").strip().lower()
            if basis == "validation_weighted_pacing_ensemble":
                partial_meta["note"] = (
                    f"Replaced incomplete month {partial_meta.get('period')} through {partial_meta.get('effective_end')} "
                    "with a backtested month-end nowcast for training."
                )
            else:
                partial_meta["note"] = (
                    f"Replaced incomplete month {partial_meta.get('period')} through {partial_meta.get('effective_end')} "
                    "with a protected month-end pace nowcast for training."
                )
        else:
            work = closed_work.copy()
            partial_meta["included"] = False
            partial_meta["excluded"] = True
            partial_meta["nowcast_applied"] = False
            partial_meta["note"] = (
                f"Excluded incomplete month {partial_meta.get('period')} from training because a stable month-end nowcast was unavailable."
            )
    elif partial_meta.get("detected") and not include_current:
        work = closed_work.copy()
        partial_meta["included"] = False
        partial_meta["excluded"] = True
        partial_meta["nowcast_applied"] = False
        partial_meta["note"] = (
            f"Excluded incomplete month {partial_meta.get('period')} from training through "
            f"{partial_meta.get('effective_end')}."
        )
    else:
        partial_meta["included"] = False
        partial_meta["excluded"] = False
        partial_meta["nowcast_applied"] = False
        partial_meta["note"] = "Training uses completed months only."

    display_metric_series = _series_for_metric(closed_work if partial_meta.get("detected") else work, metric)
    metric_series = _series_for_metric(work, metric)
    imputed_points = 0
    if metric == "margin":
        display_metric_series, display_imputed_points = _fill_margin_gaps(display_metric_series)
        metric_series, imputed_points = _fill_margin_gaps(metric_series)
        imputed_points = max(int(imputed_points), int(display_imputed_points))
    else:
        display_metric_series = pd.to_numeric(display_metric_series, errors="coerce").fillna(0.0)
        metric_series = pd.to_numeric(metric_series, errors="coerce").fillna(0.0)

    display_metric_series = display_metric_series.dropna()
    metric_series = metric_series.dropna()
    if isinstance(display_metric_series.index, pd.PeriodIndex):
        display_metric_series.index = display_metric_series.index.to_timestamp().normalize()
    if isinstance(metric_series.index, pd.PeriodIndex):
        metric_series.index = metric_series.index.to_timestamp().normalize()
    display_metric_series = display_metric_series.sort_index().astype("float64").tail(MAX_HISTORY_MONTHS)
    metric_series = metric_series.sort_index().astype("float64").tail(MAX_HISTORY_MONTHS)
    display_metric_series, history_basis = _select_monthly_training_history(display_metric_series)
    metric_series = display_metric_series.copy()
    if partial_meta.get("nowcast_applied") and nowcast_payload.get("applied"):
        nowcast_view = _metric_nowcast_view(nowcast_payload, metric)
        terminal_estimate = nowcast_view.get("estimated_month_end")
        period_token = str(nowcast_payload.get("period") or "")
        period_ts = _coerce_timestamp(f"{period_token}-01") if len(period_token) == 7 else _coerce_timestamp(period_token)
        if period_ts is not None and terminal_estimate is not None:
            metric_series.loc[period_ts.normalize()] = float(terminal_estimate)
            metric_series = metric_series.sort_index()
    meta = {
        "bundle_ctx": ctx,
        "partial_period": partial_meta,
        "history_start": display_metric_series.index.min().date().isoformat() if len(display_metric_series.index) else None,
        "history_end": display_metric_series.index.max().date().isoformat() if len(display_metric_series.index) else None,
        "imputed_points": int(imputed_points),
        "history_basis": history_basis,
        "nowcast": _metric_nowcast_view(nowcast_payload, metric) if nowcast_payload.get("applied") else {"applied": False},
    }
    return display_metric_series, metric_series, meta


def _select_monthly_training_history(series: pd.Series) -> Tuple[pd.Series, Dict[str, Any]]:
    values = pd.to_numeric(series, errors="coerce").dropna().astype("float64")
    if values.empty:
        return values, {
            "label": "No training history",
            "mode": "empty",
            "reason": "No comparable history was available under the active filters.",
            "available_points": 0,
            "selected_points": 0,
            "available_start": None,
            "available_end": None,
            "selected_start": None,
            "selected_end": None,
            "excluded_points": 0,
            "non_zero_share_pct": 0.0,
        }

    available_points = int(len(values))
    non_zero_share = float(((values.fillna(0.0).abs() > 0).sum() / available_points) * 100.0) if available_points else 0.0
    selected_points = available_points
    label = "All available history"
    mode = "full"
    reason = "Available history is limited, so the forecast used the full comparable series."

    if available_points >= 36:
        selected_points = 36
        label = "Last 36 complete months"
        mode = "recent_36_default"
        reason = "Preferred the most recent 36 complete months to keep model selection aligned with the current business run-rate."
        if non_zero_share < 55.0 and available_points >= 24:
            selected_points = 24
            label = "Last 24 months (sparse-adapted)"
            mode = "recent_24_sparse"
            reason = "Active-filter history is sparse, so training tightened to the most recent 24 months with usable signal."
    elif available_points >= 24:
        selected_points = 24
        label = "Last 24 complete months"
        mode = "recent_24_default"
        reason = "Used the most recent 24 complete months because a full 36-month basis was not available."
    elif available_points >= 18:
        selected_points = 18
        label = "Last 18 complete months"
        mode = "recent_18_default"
        reason = "Used the most recent 18 complete months because the filtered slice is shorter than a two-year baseline."

    selected = values.tail(selected_points)
    return selected, {
        "label": label,
        "mode": mode,
        "reason": reason,
        "available_points": available_points,
        "selected_points": int(len(selected)),
        "available_start": values.index.min().date().isoformat() if len(values.index) else None,
        "available_end": values.index.max().date().isoformat() if len(values.index) else None,
        "selected_start": selected.index.min().date().isoformat() if len(selected.index) else None,
        "selected_end": selected.index.max().date().isoformat() if len(selected.index) else None,
        "excluded_points": max(0, available_points - int(len(selected))),
        "non_zero_share_pct": round(non_zero_share, 1),
    }


def _coerce_timestamp(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
        if getattr(ts, "tzinfo", None) is not None:
            try:
                ts = ts.tz_convert(None)
            except Exception:
                ts = ts.tz_localize(None)
        return ts.normalize()
    except Exception:
        return None


def _month_end_timestamp(value: pd.Timestamp | None) -> pd.Timestamp | None:
    if value is None:
        return None
    try:
        return (pd.Timestamp(value).normalize() + pd.offsets.MonthEnd(0)).normalize()
    except Exception:
        return None


def _effective_window_end(filters: FilterParams) -> pd.Timestamp | None:
    filter_end = _coerce_timestamp(getattr(filters, "end", None))
    if filter_end is not None:
        return filter_end
    data_end = _coerce_timestamp(manifest_max_date())
    if data_end is not None:
        return data_end
    return _coerce_timestamp(pd.Timestamp.utcnow())


def _replace_filters(filters: FilterParams, **changes: Any) -> FilterParams:
    try:
        return replace(filters, **changes)
    except Exception:
        payload = {
            "start": getattr(filters, "start", None),
            "end": getattr(filters, "end", None),
            "statuses": tuple(getattr(filters, "statuses", ()) or ()),
            "regions": tuple(getattr(filters, "regions", ()) or ()),
            "methods": tuple(getattr(filters, "methods", ()) or ()),
            "customers": tuple(getattr(filters, "customers", ()) or ()),
            "suppliers": tuple(getattr(filters, "suppliers", ()) or ()),
            "products": tuple(getattr(filters, "products", ()) or ()),
            "sales_reps": tuple(getattr(filters, "sales_reps", ()) or ()),
            "preset": getattr(filters, "preset", None),
            "protein_min": getattr(filters, "protein_min", None),
            "protein_max": getattr(filters, "protein_max", None),
            "protein_name_like": getattr(filters, "protein_name_like", None),
            "complete_months_only": getattr(filters, "complete_months_only", False),
        }
        payload.update(changes)
        return FilterParams(**payload)


def _expanded_forecast_filters(filters: FilterParams, *, months_back: int = NOWCAST_LOOKBACK_MONTHS) -> FilterParams:
    effective_end = _effective_window_end(filters) or _coerce_timestamp(pd.Timestamp.utcnow())
    if effective_end is None:
        return _replace_filters(filters, complete_months_only=False)
    lookback_start = (effective_end.replace(day=1) - pd.DateOffset(months=max(1, int(months_back)) - 1)).normalize()
    base_start = _coerce_timestamp(getattr(filters, "start", None))
    if base_start is not None:
        start = min(base_start.normalize(), lookback_start)
    else:
        start = lookback_start
    return _replace_filters(filters, start=start, end=effective_end.normalize(), complete_months_only=False)


def _daily_frame_from_source(df: pd.DataFrame, colmap: Dict[str, Optional[str]]) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    date_col = colmap.get("date")
    if not date_col or date_col not in df.columns:
        return pd.DataFrame()

    revenue_col = colmap.get("revenue")
    cost_col = colmap.get("cost")
    qty_col = colmap.get("qty")

    work = pd.DataFrame()
    work["date"] = pd.to_datetime(df[date_col], errors="coerce").dt.normalize()
    work = work.dropna(subset=["date"])
    if work.empty:
        return pd.DataFrame()

    if revenue_col and revenue_col in df.columns:
        work["revenue"] = pd.to_numeric(df[revenue_col], errors="coerce").fillna(0.0)
    else:
        work["revenue"] = 0.0
    if cost_col and cost_col in df.columns:
        work["cost"] = pd.to_numeric(df[cost_col], errors="coerce").fillna(0.0)
    else:
        work["cost"] = 0.0
    if qty_col and qty_col in df.columns:
        work["qty"] = pd.to_numeric(df[qty_col], errors="coerce").fillna(0.0)
    else:
        work["qty"] = 0.0

    daily = work.groupby("date")[["revenue", "cost", "qty"]].sum().sort_index()
    if daily.empty:
        return pd.DataFrame()
    daily["date"] = pd.to_datetime(daily.index, errors="coerce").normalize()
    daily["period"] = daily["date"].dt.to_period("M")
    daily["profit"] = daily["revenue"] - daily["cost"]
    daily["margin_pct"] = au.safe_div(daily["profit"], daily["revenue"].replace(0, pd.NA)) * 100
    daily["margin_pct"] = daily["margin_pct"].replace([np.inf, -np.inf], np.nan).clip(lower=MARGIN_BOUNDS[0], upper=MARGIN_BOUNDS[1])
    return daily


def _daily_fact_frame(filters: FilterParams) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    expanded_filters = _expanded_forecast_filters(filters)
    if current_app and current_app.config.get("TESTING"):
        try:
            frame_ctx = ov2.get_filtered_frame(current_user, expanded_filters)
            if frame_ctx is not None:
                df = getattr(frame_ctx, "df", None)
                if isinstance(df, pd.DataFrame) and not df.empty:
                    colmap = getattr(frame_ctx, "colmap", {}) or au.column_map(df)
                    daily = _daily_frame_from_source(df, colmap)
                    if not daily.empty:
                        return daily, {
                            "source": "testing_frame_ctx",
                            "rows": int(len(df.index)),
                            "version": getattr(frame_ctx, "version", None),
                        }
        except RuntimeError:
            pass
        except Exception:
            current_app.logger.debug("overview_forecast.daily_frame.testing_failed", exc_info=True)

    try:
        from app.services import fact_store  # type: ignore

        df = fact_store.query_fact(filters=expanded_filters, use_cache=True)
        colmap = au.column_map(df)
        daily = _daily_frame_from_source(df, colmap)
        return daily, {
            "source": "fact_store",
            "rows": int(len(df.index)) if isinstance(df, pd.DataFrame) else 0,
            "filters": _filters_payload(expanded_filters),
        }
    except Exception:
        current_app.logger.debug("overview_forecast.daily_frame.query_failed", exc_info=True)
        return pd.DataFrame(), {"source": "fact_store_failed"}


def _calendar_cutoff_date(period: pd.Period, day_of_month: int) -> pd.Timestamp:
    start = period.to_timestamp().normalize()
    month_end = _month_end_timestamp(start) or start
    target_day = max(1, min(int(day_of_month), int(month_end.day)))
    return (start + pd.Timedelta(days=target_day - 1)).normalize()


def _business_day_cutoff_date(period: pd.Period, business_day_number: int) -> pd.Timestamp:
    start = period.to_timestamp().normalize()
    month_end = _month_end_timestamp(start) or start
    bdays = pd.bdate_range(start, month_end)
    if len(bdays) == 0:
        return month_end
    idx = max(0, min(len(bdays), max(1, int(business_day_number))) - 1)
    return pd.Timestamp(bdays[idx]).normalize()


def _series_total_for_period(monthly: pd.DataFrame, period: pd.Period, column: str) -> float | None:
    if not isinstance(monthly, pd.DataFrame) or monthly.empty or period not in monthly.index or column not in monthly.columns:
        return None
    try:
        value = monthly.at[period, column]
    except Exception:
        return None
    if pd.isna(value):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _partial_total_for_period(daily: pd.DataFrame, period: pd.Period, cutoff_date: pd.Timestamp, column: str) -> float | None:
    if not isinstance(daily, pd.DataFrame) or daily.empty or column not in daily.columns:
        return None
    mask = (daily["period"] == period) & (daily["date"] <= pd.Timestamp(cutoff_date).normalize())
    if not mask.any():
        return 0.0
    try:
        return float(pd.to_numeric(daily.loc[mask, column], errors="coerce").fillna(0.0).sum())
    except Exception:
        return None


def _candidate_metric_summary(actuals: List[float], preds: List[float]) -> Dict[str, Any]:
    if not actuals or not preds:
        return {"smape": None, "wape": None, "bias_pct": None, "n": 0}
    actual_arr = np.asarray(actuals, dtype=float)
    pred_arr = np.asarray(preds, dtype=float)
    denom = np.abs(actual_arr) + np.abs(pred_arr)
    smape = float(np.mean(2.0 * np.abs(actual_arr - pred_arr) / np.where(denom == 0, 1.0, denom)) * 100.0) if np.any(denom) else None
    total_actual = float(np.sum(np.abs(actual_arr)))
    wape = float(np.sum(np.abs(actual_arr - pred_arr)) / total_actual * 100.0) if total_actual > 0 else None
    bias_pct = float(np.sum(pred_arr - actual_arr) / total_actual * 100.0) if total_actual > 0 else None
    return {"smape": smape, "wape": wape, "bias_pct": bias_pct, "n": int(len(actual_arr))}


def _estimate_from_share(current_partial: float | None, share: float | None) -> float | None:
    if current_partial is None or share is None or not np.isfinite(current_partial) or not np.isfinite(share):
        return None
    bounded_share = max(NOWCAST_MIN_SHARE, min(NOWCAST_MAX_SHARE, float(share)))
    if bounded_share <= 0:
        return None
    return float(current_partial / bounded_share)


def _recent_periods_before(monthly: pd.DataFrame, period: pd.Period, limit: int) -> List[pd.Period]:
    if not isinstance(monthly, pd.DataFrame) or monthly.empty:
        return []
    periods = [idx for idx in monthly.index if isinstance(idx, pd.Period) and idx < period]
    return periods[-max(0, int(limit)) :]


def _share_for_period(
    daily: pd.DataFrame,
    monthly: pd.DataFrame,
    period: pd.Period,
    *,
    column: str,
    cutoff_date: pd.Timestamp,
) -> float | None:
    full_total = _series_total_for_period(monthly, period, column)
    if full_total is None or not np.isfinite(full_total) or full_total <= 0:
        return None
    partial_total = _partial_total_for_period(daily, period, cutoff_date, column)
    if partial_total is None or not np.isfinite(partial_total) or partial_total < 0:
        return None
    share = float(partial_total / full_total)
    return max(NOWCAST_MIN_SHARE, min(NOWCAST_MAX_SHARE, share))


def _avg_recent_share(
    daily: pd.DataFrame,
    monthly: pd.DataFrame,
    periods: Sequence[pd.Period],
    *,
    column: str,
    calendar_day: int | None = None,
    business_day_number: int | None = None,
) -> float | None:
    shares: List[float] = []
    for period in periods:
        if calendar_day is not None:
            cutoff_date = _calendar_cutoff_date(period, calendar_day)
        elif business_day_number is not None:
            cutoff_date = _business_day_cutoff_date(period, business_day_number)
        else:
            continue
        share = _share_for_period(daily, monthly, period, column=column, cutoff_date=cutoff_date)
        if share is not None and np.isfinite(share):
            shares.append(float(share))
    if not shares:
        return None
    return float(np.median(np.asarray(shares, dtype=float)))


def _nowcast_candidate_estimate(
    daily: pd.DataFrame,
    monthly: pd.DataFrame,
    target_period: pd.Period,
    *,
    column: str,
    current_partial: float,
    calendar_day: int,
    business_day_number: int,
    candidate_name: str,
) -> float | None:
    if not np.isfinite(current_partial) or current_partial < 0:
        return None
    if candidate_name == "prior_month_same_day":
        ref_period = target_period - 1
        ref_cutoff = _calendar_cutoff_date(ref_period, calendar_day)
        ref_full = _series_total_for_period(monthly, ref_period, column)
        ref_partial = _partial_total_for_period(daily, ref_period, ref_cutoff, column)
        if ref_full is None or ref_partial is None or ref_full <= 0 or ref_partial <= 0:
            return None
        return float(ref_full * (current_partial / ref_partial))
    if candidate_name == "prior_year_same_day":
        ref_period = target_period - 12
        ref_cutoff = _calendar_cutoff_date(ref_period, calendar_day)
        ref_full = _series_total_for_period(monthly, ref_period, column)
        ref_partial = _partial_total_for_period(daily, ref_period, ref_cutoff, column)
        if ref_full is None or ref_partial is None or ref_full <= 0 or ref_partial <= 0:
            return None
        return float(ref_full * (current_partial / ref_partial))
    if candidate_name == "recent3_calendar_curve":
        share = _avg_recent_share(
            daily,
            monthly,
            _recent_periods_before(monthly, target_period, 3),
            column=column,
            calendar_day=calendar_day,
        )
        return _estimate_from_share(current_partial, share)
    if candidate_name == "recent6_calendar_curve":
        share = _avg_recent_share(
            daily,
            monthly,
            _recent_periods_before(monthly, target_period, 6),
            column=column,
            calendar_day=calendar_day,
        )
        return _estimate_from_share(current_partial, share)
    if candidate_name == "recent6_business_curve":
        share = _avg_recent_share(
            daily,
            monthly,
            _recent_periods_before(monthly, target_period, 6),
            column=column,
            business_day_number=business_day_number,
        )
        return _estimate_from_share(current_partial, share)
    if candidate_name == "blended_recent_curve":
        cal_share = _avg_recent_share(
            daily,
            monthly,
            _recent_periods_before(monthly, target_period, 6),
            column=column,
            calendar_day=calendar_day,
        )
        biz_share = _avg_recent_share(
            daily,
            monthly,
            _recent_periods_before(monthly, target_period, 6),
            column=column,
            business_day_number=business_day_number,
        )
        shares = [share for share in (cal_share, biz_share) if share is not None]
        if not shares:
            return None
        if len(shares) == 1:
            return _estimate_from_share(current_partial, shares[0])
        blended_share = (0.6 * float(cal_share or 0.0)) + (0.4 * float(biz_share or 0.0))
        return _estimate_from_share(current_partial, blended_share)
    return None


def _nowcast_candidates() -> Tuple[str, ...]:
    return (
        "prior_month_same_day",
        "prior_year_same_day",
        "recent3_calendar_curve",
        "recent6_calendar_curve",
        "recent6_business_curve",
        "blended_recent_curve",
    )


def _candidate_display_name(name: str) -> str:
    mapping = {
        "prior_month_same_day": "Prior Month Pace",
        "prior_year_same_day": "Prior Year Pace",
        "recent3_calendar_curve": "Recent 3M Day-Curve",
        "recent6_calendar_curve": "Recent 6M Day-Curve",
        "recent6_business_curve": "Recent 6M Business-Day Curve",
        "blended_recent_curve": "Blended Recent Pace",
    }
    return mapping.get(name, name)


def _current_month_growth_regime(
    *,
    estimated_revenue: float,
    monthly: pd.DataFrame,
    pace_vs_prior_month: float | None,
    pace_vs_prior_year: float | None,
    stability_score: float,
) -> str:
    recent_full = pd.to_numeric(monthly.get("revenue", pd.Series(dtype=float)), errors="coerce").dropna().astype("float64")
    if recent_full.empty:
        return "stable"
    recent_baseline = float(recent_full.tail(min(3, len(recent_full))).mean() or 0.0)
    revenue_growth = ((estimated_revenue - recent_baseline) / max(abs(recent_baseline), 1e-6)) if recent_baseline else 0.0
    if stability_score < 50 and abs(revenue_growth) >= 0.08:
        return "volatile"
    if revenue_growth >= 0.08 and (pace_vs_prior_month or 1.0) >= 1.04:
        return "accelerating"
    if revenue_growth <= -0.06 and (pace_vs_prior_month or 1.0) <= 0.97:
        return "decelerating"
    if pace_vs_prior_year is not None and abs(float(pace_vs_prior_year) - 1.0) >= 0.12 and stability_score < 60:
        return "volatile"
    return "stable"


def _uncertainty_level(uncertainty_pct: float) -> str:
    if uncertainty_pct <= 10:
        return "low"
    if uncertainty_pct <= 18:
        return "medium"
    return "high"


def _fallback_monthly_nowcast(
    monthly: pd.DataFrame,
    target_period: pd.Period,
    *,
    effective_end: pd.Timestamp,
    source: str,
    reason: str | None = None,
) -> Dict[str, Any]:
    if not isinstance(monthly, pd.DataFrame) or monthly.empty or target_period not in monthly.index:
        return {"applied": False, "source": source}

    raw_mtd_revenue = float(_series_total_for_period(monthly, target_period, "revenue") or 0.0)
    raw_mtd_cost = float(_series_total_for_period(monthly, target_period, "cost") or 0.0)
    if raw_mtd_revenue <= 0 and raw_mtd_cost <= 0:
        return {"applied": False, "source": source}

    month_start = target_period.to_timestamp().normalize()
    month_end = _month_end_timestamp(month_start) or pd.Timestamp(effective_end).normalize()
    cutoff_date = min(pd.Timestamp(effective_end).normalize(), month_end)
    calendar_progress = float(cutoff_date.day / max(1, int(month_end.day)))
    business_elapsed = int(len(pd.bdate_range(month_start, cutoff_date)))
    business_total = int(len(pd.bdate_range(month_start, month_end)))
    business_progress = float(business_elapsed / max(1, business_total))
    pace_share = max(
        NOWCAST_MIN_SHARE,
        min(
            NOWCAST_MAX_SHARE,
            (0.55 * calendar_progress) + (0.45 * business_progress),
        ),
    )

    closed = monthly.loc[monthly.index < target_period].copy()
    recent_revenue = pd.to_numeric(closed.get("revenue", pd.Series(dtype=float)), errors="coerce").dropna().astype("float64")
    recent_cost = pd.to_numeric(closed.get("cost", pd.Series(dtype=float)), errors="coerce").dropna().astype("float64")
    prior_year_revenue = _series_total_for_period(monthly, target_period - 12, "revenue")
    prior_year_cost = _series_total_for_period(monthly, target_period - 12, "cost")

    revenue_pace = float(_estimate_from_share(raw_mtd_revenue, pace_share) or raw_mtd_revenue)
    cost_pace = float(_estimate_from_share(raw_mtd_cost, pace_share) or raw_mtd_cost)
    revenue_recent_anchor = recent_revenue.tail(min(6, len(recent_revenue))).median() if not recent_revenue.empty else None
    cost_recent_anchor = recent_cost.tail(min(6, len(recent_cost))).median() if not recent_cost.empty else None
    revenue_anchor_candidates = [
        float(val)
        for val in [revenue_recent_anchor, prior_year_revenue]
        if val is not None and np.isfinite(val) and float(val) > 0
    ]
    cost_anchor_candidates = [
        float(val)
        for val in [cost_recent_anchor, prior_year_cost]
        if val is not None and np.isfinite(val) and float(val) >= 0
    ]
    revenue_anchor = float(np.median(np.asarray(revenue_anchor_candidates, dtype=float))) if revenue_anchor_candidates else revenue_pace
    cost_anchor = float(np.median(np.asarray(cost_anchor_candidates, dtype=float))) if cost_anchor_candidates else cost_pace

    if calendar_progress >= 0.9:
        progress_weight = 0.82
    elif calendar_progress >= 0.75:
        progress_weight = 0.74
    elif calendar_progress >= 0.55:
        progress_weight = 0.66
    else:
        progress_weight = 0.58

    est_revenue = max(raw_mtd_revenue, (progress_weight * revenue_pace) + ((1.0 - progress_weight) * max(raw_mtd_revenue, revenue_anchor)))
    est_cost = max(raw_mtd_cost, (progress_weight * cost_pace) + ((1.0 - progress_weight) * max(raw_mtd_cost, cost_anchor)))
    est_profit = max(0.0, est_revenue - est_cost)
    raw_mtd_profit = max(0.0, raw_mtd_revenue - raw_mtd_cost)
    est_margin = _bound_value("margin", float(au.safe_div(est_profit, est_revenue) * 100 if est_revenue else 0.0))
    raw_mtd_margin = _bound_value("margin", float(au.safe_div(raw_mtd_profit, raw_mtd_revenue) * 100 if raw_mtd_revenue else 0.0))

    revenue_volatility = float(recent_revenue.pct_change().dropna().abs().tail(6).median() * 100.0) if len(recent_revenue) >= 2 else 0.0
    base_uncertainty = 7.0 if calendar_progress >= 0.9 else (9.0 if calendar_progress >= 0.8 else (12.0 if calendar_progress >= 0.65 else 16.0))
    uncertainty_pct = max(base_uncertainty, min(28.0, base_uncertainty + (revenue_volatility * 0.35)))
    revenue_lower = max(raw_mtd_revenue, est_revenue * (1.0 - (uncertainty_pct / 100.0)))
    revenue_upper = est_revenue * (1.0 + (uncertainty_pct / 100.0))
    cost_lower = max(raw_mtd_cost, est_cost * (1.0 - (uncertainty_pct / 100.0)))
    cost_upper = est_cost * (1.0 + (uncertainty_pct / 100.0))
    profit_lower = max(0.0, revenue_lower - cost_upper)
    profit_upper = max(0.0, revenue_upper - cost_lower)

    stability_score = max(42.0, min(82.0, 100.0 - (float(uncertainty_pct) * 2.4)))
    growth_regime = _current_month_growth_regime(
        estimated_revenue=est_revenue,
        monthly=closed,
        pace_vs_prior_month=None,
        pace_vs_prior_year=None,
        stability_score=float(stability_score),
    )

    return {
        "applied": True,
        "period": str(target_period),
        "effective_end": cutoff_date.date().isoformat(),
        "progress_pct": round(calendar_progress * 100.0, 1),
        "business_progress_pct": round(business_progress * 100.0, 1),
        "raw_mtd_revenue": raw_mtd_revenue,
        "raw_mtd_cost": raw_mtd_cost,
        "raw_mtd_profit": raw_mtd_profit,
        "raw_mtd_margin_pct": raw_mtd_margin,
        "estimated_month_end_revenue": est_revenue,
        "estimated_month_end_cost": est_cost,
        "estimated_month_end_profit": est_profit,
        "estimated_month_end_margin_pct": est_margin,
        "revenue_interval": {"lower": revenue_lower, "upper": revenue_upper},
        "cost_interval": {"lower": cost_lower, "upper": cost_upper},
        "profit_interval": {"lower": profit_lower, "upper": profit_upper},
        "pace_vs_prior_month_same_day": None,
        "pace_vs_prior_year_same_day": None,
        "blend_weights": [
            {
                "name": "monthly_progress_fallback",
                "display_name": "Protected Month Progress Pace",
                "weight": 1.0,
                "validation_points": 0,
                "smape": None,
                "bias_pct": None,
                "estimate": est_revenue,
            }
        ],
        "growth_regime": growth_regime,
        "stability_score": round(float(stability_score), 1),
        "bias_risk": "medium",
        "uncertainty_level": _uncertainty_level(float(uncertainty_pct)),
        "uncertainty_pct": round(float(uncertainty_pct), 1),
        "current_month_basis": "monthly_progress_fallback",
        "source": source,
        "fallback_reason": reason,
    }


def _nowcast_metric(
    daily: pd.DataFrame,
    monthly: pd.DataFrame,
    target_period: pd.Period,
    *,
    effective_end: pd.Timestamp,
    column: str,
) -> Dict[str, Any]:
    cutoff_date = pd.Timestamp(effective_end).normalize()
    current_partial = _partial_total_for_period(daily, target_period, cutoff_date, column)
    if current_partial is None or not np.isfinite(current_partial):
        return {"applied": False, "column": column}

    month_end = _month_end_timestamp(target_period.to_timestamp().normalize()) or cutoff_date
    calendar_day = int(min(cutoff_date.day, month_end.day))
    progress_pct = float((calendar_day / max(1, int(month_end.day))) * 100.0)
    business_elapsed = int(len(pd.bdate_range(target_period.to_timestamp().normalize(), min(cutoff_date, month_end))))
    business_total = int(len(pd.bdate_range(target_period.to_timestamp().normalize(), month_end)))
    business_progress_pct = float((business_elapsed / max(1, business_total)) * 100.0)

    evaluation_periods = _recent_periods_before(monthly, target_period, NOWCAST_MAX_EVAL_MONTHS)
    candidate_rows: List[Dict[str, Any]] = []
    for candidate_name in _nowcast_candidates():
        actuals: List[float] = []
        preds: List[float] = []
        for eval_period in evaluation_periods:
            actual_full = _series_total_for_period(monthly, eval_period, column)
            if actual_full is None or not np.isfinite(actual_full) or actual_full <= 0:
                continue
            eval_cutoff = _calendar_cutoff_date(eval_period, calendar_day)
            eval_partial = _partial_total_for_period(daily, eval_period, eval_cutoff, column)
            if eval_partial is None or not np.isfinite(eval_partial) or eval_partial <= 0:
                continue
            pred = _nowcast_candidate_estimate(
                daily,
                monthly,
                eval_period,
                column=column,
                current_partial=float(eval_partial),
                calendar_day=calendar_day,
                business_day_number=business_elapsed,
                candidate_name=candidate_name,
            )
            if pred is None or not np.isfinite(pred) or pred <= 0:
                continue
            actuals.append(float(actual_full))
            preds.append(float(pred))

        metrics = _candidate_metric_summary(actuals, preds)
        estimate = _nowcast_candidate_estimate(
            daily,
            monthly,
            target_period,
            column=column,
            current_partial=float(current_partial),
            calendar_day=calendar_day,
            business_day_number=business_elapsed,
            candidate_name=candidate_name,
        )
        score = None
        if metrics.get("smape") is not None:
            score = (float(metrics.get("smape") or 0.0) * 0.55) + (float(metrics.get("wape") or 0.0) * 0.25) + (abs(float(metrics.get("bias_pct") or 0.0)) * 0.20)
        candidate_rows.append(
            {
                "name": candidate_name,
                "display_name": _candidate_display_name(candidate_name),
                "estimate": float(estimate) if estimate is not None and np.isfinite(estimate) else None,
                "smape": metrics.get("smape"),
                "wape": metrics.get("wape"),
                "bias_pct": metrics.get("bias_pct"),
                "validation_points": metrics.get("n"),
                "score": score,
            }
        )

    valid = [
        row
        for row in candidate_rows
        if row.get("estimate") is not None and row.get("score") is not None and int(row.get("validation_points") or 0) >= NOWCAST_MIN_CANDIDATES
    ]
    ranked = sorted(valid, key=lambda item: (float(item.get("score") or 9999.0), -int(item.get("validation_points") or 0)))
    selected = ranked[:NOWCAST_TOP_CANDIDATES]

    if not selected:
        naive_share = max(NOWCAST_MIN_SHARE, min(NOWCAST_MAX_SHARE, progress_pct / 100.0))
        naive_estimate = _estimate_from_share(float(current_partial), naive_share) or float(current_partial)
        uncertainty_pct = 18.0 if progress_pct < 60 else 12.0
        return {
            "applied": True,
            "column": column,
            "raw_mtd": float(current_partial),
            "estimate": max(float(current_partial), float(naive_estimate)),
            "lower": max(float(current_partial), float(naive_estimate) * (1.0 - (uncertainty_pct / 100.0))),
            "upper": float(naive_estimate) * (1.0 + (uncertainty_pct / 100.0)),
            "progress_pct": round(progress_pct, 1),
            "business_progress_pct": round(business_progress_pct, 1),
            "uncertainty_pct": round(uncertainty_pct, 1),
            "uncertainty_level": _uncertainty_level(uncertainty_pct),
            "blend_weights": [],
            "candidates": candidate_rows,
        }

    raw_weights = []
    for row in selected:
        score = max(1.0, float(row.get("score") or 1.0))
        support = max(1.0, min(6.0, float(row.get("validation_points") or 1.0)))
        raw_weights.append((1.0 / score) * (support / 6.0))
    weight_arr = np.asarray(raw_weights, dtype=float)
    if float(weight_arr.sum()) <= 0:
        weight_arr = np.ones(len(selected), dtype=float)
    weight_arr = weight_arr / float(weight_arr.sum())

    estimates = np.asarray([float(row.get("estimate") or 0.0) for row in selected], dtype=float)
    blended_estimate = float(np.sum(estimates * weight_arr))
    blended_smape = float(np.sum(np.asarray([float(row.get("smape") or 0.0) for row in selected], dtype=float) * weight_arr))
    blended_bias = float(np.sum(np.asarray([float(abs(row.get("bias_pct") or 0.0)) for row in selected], dtype=float) * weight_arr))
    dispersion_pct = float(np.std(estimates) / max(abs(blended_estimate), 1e-6) * 100.0) if len(estimates) > 1 else 0.0
    progress_penalty = 8.0 if progress_pct < 30 else (4.5 if progress_pct < 60 else 2.5)
    uncertainty_pct = max(6.0, min(32.0, (blended_smape * 0.55) + (blended_bias * 0.15) + (dispersion_pct * 0.30) + progress_penalty))
    bounded_estimate = max(float(current_partial), float(blended_estimate))
    lower = max(float(current_partial), bounded_estimate * (1.0 - (uncertainty_pct / 100.0)))
    upper = bounded_estimate * (1.0 + (uncertainty_pct / 100.0))
    blend_weights = []
    for row, weight in zip(selected, weight_arr):
        blend_weights.append(
            {
                "name": row.get("name"),
                "display_name": row.get("display_name"),
                "weight": round(float(weight), 3),
                "validation_points": int(row.get("validation_points") or 0),
                "smape": row.get("smape"),
                "bias_pct": row.get("bias_pct"),
                "estimate": row.get("estimate"),
            }
        )
    return {
        "applied": True,
        "column": column,
        "raw_mtd": float(current_partial),
        "estimate": float(bounded_estimate),
        "lower": float(lower),
        "upper": float(upper),
        "progress_pct": round(progress_pct, 1),
        "business_progress_pct": round(business_progress_pct, 1),
        "uncertainty_pct": round(float(uncertainty_pct), 1),
        "uncertainty_level": _uncertainty_level(float(uncertainty_pct)),
        "blend_weights": blend_weights,
        "candidates": candidate_rows,
    }


def _current_month_nowcast(filters: FilterParams, monthly: pd.DataFrame) -> Dict[str, Any]:
    if not isinstance(monthly, pd.DataFrame) or monthly.empty:
        return {"applied": False}
    effective_end = _effective_window_end(filters)
    last_period = monthly.index.max()
    if effective_end is None or not isinstance(last_period, pd.Period):
        return {"applied": False}
    target_period = effective_end.to_period("M")
    if last_period != target_period:
        return {"applied": False}

    daily, daily_meta = _daily_fact_frame(filters)
    if daily.empty:
        return _fallback_monthly_nowcast(
            monthly,
            target_period,
            effective_end=effective_end,
            source=str(daily_meta.get("source") or "monthly_progress_fallback"),
            reason="Daily fact frame was unavailable for the scoped nowcast path.",
        )

    daily = daily[daily["date"] <= effective_end].copy()
    if daily.empty:
        return _fallback_monthly_nowcast(
            monthly,
            target_period,
            effective_end=effective_end,
            source=str(daily_meta.get("source") or "monthly_progress_fallback"),
            reason="Scoped daily fact rows were empty through the effective end date.",
        )
    monthly_totals = daily.groupby("period")[["revenue", "cost", "qty", "profit"]].sum().sort_index()
    if monthly_totals.empty or target_period not in monthly_totals.index:
        return _fallback_monthly_nowcast(
            monthly,
            target_period,
            effective_end=effective_end,
            source=str(daily_meta.get("source") or "monthly_progress_fallback"),
            reason="Daily nowcast totals were unavailable for the target period.",
        )
    monthly_totals["margin_pct"] = au.safe_div(monthly_totals["profit"], monthly_totals["revenue"].replace(0, pd.NA)) * 100
    monthly_totals["margin_pct"] = monthly_totals["margin_pct"].replace([np.inf, -np.inf], np.nan).clip(lower=MARGIN_BOUNDS[0], upper=MARGIN_BOUNDS[1])

    revenue_nowcast = _nowcast_metric(daily, monthly_totals, target_period, effective_end=effective_end, column="revenue")
    cost_nowcast = _nowcast_metric(daily, monthly_totals, target_period, effective_end=effective_end, column="cost")
    if not revenue_nowcast.get("applied") or not cost_nowcast.get("applied"):
        return _fallback_monthly_nowcast(
            monthly_totals,
            target_period,
            effective_end=effective_end,
            source=str(daily_meta.get("source") or "monthly_progress_fallback"),
            reason="Detailed pacing candidates were unstable, so a protected monthly pace fallback was used.",
        )

    raw_mtd_revenue = float(revenue_nowcast.get("raw_mtd") or 0.0)
    raw_mtd_cost = float(cost_nowcast.get("raw_mtd") or 0.0)
    est_revenue = max(raw_mtd_revenue, float(revenue_nowcast.get("estimate") or raw_mtd_revenue))
    est_cost = max(raw_mtd_cost, float(cost_nowcast.get("estimate") or raw_mtd_cost))
    est_profit = max(0.0, est_revenue - est_cost)
    raw_mtd_profit = max(0.0, raw_mtd_revenue - raw_mtd_cost)
    est_margin = _bound_value("margin", float(au.safe_div(est_profit, est_revenue) * 100 if est_revenue else 0.0))
    raw_mtd_margin = _bound_value("margin", float(au.safe_div(raw_mtd_profit, raw_mtd_revenue) * 100 if raw_mtd_revenue else 0.0))

    profit_lower = max(0.0, float(revenue_nowcast.get("lower") or raw_mtd_revenue) - float(cost_nowcast.get("upper") or raw_mtd_cost))
    profit_upper = max(0.0, float(revenue_nowcast.get("upper") or est_revenue) - float(cost_nowcast.get("lower") or est_cost))

    prior_month = target_period - 1
    prior_year = target_period - 12
    cutoff_date = pd.Timestamp(effective_end).normalize()
    prior_month_partial = _partial_total_for_period(daily, prior_month, _calendar_cutoff_date(prior_month, cutoff_date.day), "revenue")
    prior_year_partial = _partial_total_for_period(daily, prior_year, _calendar_cutoff_date(prior_year, cutoff_date.day), "revenue")
    pace_vs_prior_month = float(raw_mtd_revenue / prior_month_partial) if prior_month_partial and prior_month_partial > 0 else None
    pace_vs_prior_year = float(raw_mtd_revenue / prior_year_partial) if prior_year_partial and prior_year_partial > 0 else None

    top_weights = revenue_nowcast.get("blend_weights") or []
    if top_weights:
        weight_conf = sum(float(item.get("weight") or 0.0) ** 2 for item in top_weights)
        stability_score = max(35.0, min(92.0, 100.0 - (float(revenue_nowcast.get("uncertainty_pct") or 0.0) * 2.2) - (max(0.0, 1.0 - weight_conf) * 22.0)))
    else:
        stability_score = max(35.0, 100.0 - (float(revenue_nowcast.get("uncertainty_pct") or 0.0) * 2.4))
    growth_regime = _current_month_growth_regime(
        estimated_revenue=est_revenue,
        monthly=monthly_totals.loc[monthly_totals.index < target_period],
        pace_vs_prior_month=pace_vs_prior_month,
        pace_vs_prior_year=pace_vs_prior_year,
        stability_score=float(stability_score),
    )
    bias_risk = "low"
    if top_weights:
        weighted_bias = float(sum(abs(float(item.get("bias_pct") or 0.0)) * float(item.get("weight") or 0.0) for item in top_weights))
        if weighted_bias >= 12:
            bias_risk = "high"
        elif weighted_bias >= 6:
            bias_risk = "medium"

    nowcast_payload = {
        "applied": True,
        "period": str(target_period),
        "effective_end": effective_end.date().isoformat(),
        "progress_pct": revenue_nowcast.get("progress_pct"),
        "business_progress_pct": revenue_nowcast.get("business_progress_pct"),
        "raw_mtd_revenue": raw_mtd_revenue,
        "raw_mtd_cost": raw_mtd_cost,
        "raw_mtd_profit": raw_mtd_profit,
        "raw_mtd_margin_pct": raw_mtd_margin,
        "estimated_month_end_revenue": est_revenue,
        "estimated_month_end_cost": est_cost,
        "estimated_month_end_profit": est_profit,
        "estimated_month_end_margin_pct": est_margin,
        "revenue_interval": {"lower": revenue_nowcast.get("lower"), "upper": revenue_nowcast.get("upper")},
        "cost_interval": {"lower": cost_nowcast.get("lower"), "upper": cost_nowcast.get("upper")},
        "profit_interval": {"lower": profit_lower, "upper": profit_upper},
        "pace_vs_prior_month_same_day": round(float(pace_vs_prior_month), 3) if pace_vs_prior_month is not None else None,
        "pace_vs_prior_year_same_day": round(float(pace_vs_prior_year), 3) if pace_vs_prior_year is not None else None,
        "blend_weights": revenue_nowcast.get("blend_weights") or [],
        "growth_regime": growth_regime,
        "stability_score": round(float(stability_score), 1),
        "bias_risk": bias_risk,
        "uncertainty_level": revenue_nowcast.get("uncertainty_level"),
        "uncertainty_pct": revenue_nowcast.get("uncertainty_pct"),
        "current_month_basis": "validation_weighted_pacing_ensemble",
        "source": daily_meta.get("source"),
    }
    return nowcast_payload


def _metric_nowcast_view(nowcast: Dict[str, Any], metric: str) -> Dict[str, Any]:
    if not nowcast.get("applied"):
        return {"applied": False}
    metric_key = _normalize_metric(metric)
    if metric_key == "revenue":
        raw_mtd = nowcast.get("raw_mtd_revenue")
        estimate = nowcast.get("estimated_month_end_revenue")
        interval = nowcast.get("revenue_interval") or {}
    elif metric_key == "profit":
        raw_mtd = nowcast.get("raw_mtd_profit")
        estimate = nowcast.get("estimated_month_end_profit")
        interval = nowcast.get("profit_interval") or {}
    else:
        raw_mtd = nowcast.get("raw_mtd_margin_pct")
        estimate = nowcast.get("estimated_month_end_margin_pct")
        revenue_interval = nowcast.get("revenue_interval") or {}
        profit_interval = nowcast.get("profit_interval") or {}
        revenue_upper = float(revenue_interval.get("upper") or 0.0)
        revenue_lower = float(revenue_interval.get("lower") or 0.0)
        profit_lower = profit_interval.get("lower")
        profit_upper = profit_interval.get("upper")
        interval = {
            "lower": _bound_value("margin", au.safe_div(float(profit_lower), revenue_upper) * 100)
            if profit_lower is not None and revenue_upper > 0
            else None,
            "upper": _bound_value("margin", au.safe_div(float(profit_upper), revenue_lower) * 100)
            if profit_upper is not None and revenue_lower > 0
            else None,
        }
    return {
        **nowcast,
        "raw_mtd_actual": _bound_value(metric_key, raw_mtd),
        "estimated_month_end": _bound_value(metric_key, estimate),
        "yhat_lower": _bound_value(metric_key, interval.get("lower")),
        "yhat_upper": _bound_value(metric_key, interval.get("upper")),
    }


def _regularize_monthly_frame(monthly: pd.DataFrame) -> pd.DataFrame:
    work = monthly.copy()
    if work.empty:
        return work
    if not isinstance(work.index, pd.PeriodIndex):
        idx = pd.to_datetime(work.index, errors="coerce")
        mask = idx.notna()
        work = work.loc[mask].copy()
        if work.empty:
            return work
        work.index = idx[mask].to_period("M")
    work = work.sort_index()
    full_index = pd.period_range(work.index.min(), work.index.max(), freq="M")
    work = work.reindex(full_index)
    for col in ("revenue", "cost", "qty", "orders", "customers", "weight"):
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0.0)
    if "revenue" not in work.columns:
        work["revenue"] = 0.0
    if "cost" not in work.columns:
        work["cost"] = 0.0
    work["profit"] = pd.to_numeric(work.get("revenue"), errors="coerce").fillna(0.0) - pd.to_numeric(work.get("cost"), errors="coerce").fillna(0.0)
    if "qty" not in work.columns:
        work["qty"] = 0.0
    if "orders" in work.columns:
        work["aov"] = au.safe_div(work["revenue"], work["orders"].replace(0, pd.NA))
    if "qty" in work.columns:
        work["asp"] = au.safe_div(work["revenue"], work["qty"].replace(0, pd.NA))
    work["margin_pct"] = au.safe_div(work["profit"], work["revenue"].replace(0, pd.NA)) * 100
    work["margin_pct"] = work["margin_pct"].replace([np.inf, -np.inf], np.nan).clip(lower=MARGIN_BOUNDS[0], upper=MARGIN_BOUNDS[1])
    return work


def _terminal_month_meta(filters: FilterParams, monthly: pd.DataFrame) -> Dict[str, Any]:
    if not isinstance(monthly, pd.DataFrame) or monthly.empty:
        return {"detected": False, "period": None, "effective_end": None}
    last_period = monthly.index.max()
    if not isinstance(last_period, pd.Period):
        return {"detected": False, "period": None, "effective_end": None}
    effective_end = _effective_window_end(filters)
    if effective_end is None:
        return {"detected": False, "period": str(last_period), "effective_end": None}
    last_start = last_period.to_timestamp().normalize()
    last_end = _month_end_timestamp(last_start)
    detected = bool(last_start <= effective_end <= last_end and effective_end < last_end)
    return {
        "detected": detected,
        "period": str(last_period),
        "effective_end": effective_end.date().isoformat(),
        "month_end": last_end.date().isoformat() if last_end is not None else None,
        "days_elapsed": int(effective_end.day) if detected else None,
        "days_in_month": int(last_end.day) if detected and last_end is not None else None,
    }


def _fill_margin_gaps(series: pd.Series) -> Tuple[pd.Series, int]:
    clean = pd.to_numeric(series, errors="coerce").replace([np.inf, -np.inf], np.nan)
    missing = int(clean.isna().sum())
    if not missing:
        return clean.clip(lower=MARGIN_BOUNDS[0], upper=MARGIN_BOUNDS[1]), 0
    if isinstance(clean.index, (pd.PeriodIndex, pd.DatetimeIndex)) and len(clean) >= 6:
        if isinstance(clean.index, pd.PeriodIndex):
            slots = pd.Series(clean.index.month, index=clean.index)
        else:
            slots = pd.Series(clean.index.month, index=clean.index)
        month_medians = clean.groupby(slots).median()
        for idx in clean[clean.isna()].index:
            try:
                slot = int(idx.month)
            except Exception:
                slot = None
            fill_val = month_medians.get(slot) if slot is not None else None
            if fill_val is not None and pd.notna(fill_val):
                clean.loc[idx] = float(fill_val)
    clean = clean.interpolate(method="linear", limit_direction="both")
    if clean.isna().any():
        fallback = float(clean.dropna().median()) if not clean.dropna().empty else 0.0
        clean = clean.fillna(fallback)
    return clean.clip(lower=MARGIN_BOUNDS[0], upper=MARGIN_BOUNDS[1]), missing


def _seasonal_slot_index(index: Sequence[Any], granularity: str) -> List[int]:
    slots: List[int] = []
    gran = _normalize_granularity(granularity)
    for raw in index:
        ts = pd.Timestamp(raw)
        if gran == "weekly":
            try:
                slots.append(int(ts.isocalendar().week))
            except Exception:
                slots.append(int(ts.weekofyear))
        else:
            slots.append(int(ts.month))
    return slots


def _seasonal_profile(history: pd.Series, seasonal_period: int, granularity: str) -> pd.Series:
    if history.empty or len(history) < max(6, seasonal_period):
        return pd.Series(np.zeros(len(history)), index=history.index, dtype="float64")
    slots = _seasonal_slot_index(history.index, granularity)
    grouped = pd.DataFrame({"y": history.values.astype(float), "slot": slots})
    slot_means = grouped.groupby("slot")["y"].median()
    centered = slot_means - float(slot_means.mean() or 0.0)
    values = [float(centered.get(slot, 0.0)) for slot in slots]
    return pd.Series(values, index=history.index, dtype="float64")


def _future_seasonal_component(history: pd.Series, horizon: int, seasonal_period: int, granularity: str) -> pd.Series:
    if history.empty:
        return pd.Series(dtype="float64")
    idx = _future_index(pd.Timestamp(history.index[-1]), horizon, granularity)
    if len(history) < max(6, seasonal_period):
        return pd.Series(np.zeros(horizon), index=idx, dtype="float64")
    slots = _seasonal_slot_index(history.index, granularity)
    grouped = pd.DataFrame({"y": history.values.astype(float), "slot": slots})
    slot_means = grouped.groupby("slot")["y"].median()
    centered = slot_means - float(slot_means.mean() or 0.0)
    future_slots = _seasonal_slot_index(idx, granularity)
    values = [float(centered.get(slot, 0.0)) for slot in future_slots]
    return pd.Series(values, index=idx, dtype="float64")


def _robust_outlier_adjust(
    history: pd.Series,
    *,
    metric: str,
    granularity: str,
    seasonal_period: int,
) -> Tuple[pd.Series, Dict[str, Any]]:
    clean = history.dropna().astype("float64").copy()
    if clean.empty or len(clean) < 8:
        return clean, {"count": 0, "share_pct": 0.0, "positions": [], "applied": False}

    if len(clean) >= seasonal_period * 2:
        baseline = _seasonal_profile(clean, seasonal_period, granularity)
        baseline = baseline + float((clean - baseline).rolling(window=min(5, len(clean)), min_periods=1).median().iloc[-1] or 0.0)
    else:
        baseline = clean.rolling(window=min(5, len(clean)), center=True, min_periods=1).median()

    residual = clean - baseline
    local_med = residual.rolling(window=min(5, len(residual)), center=True, min_periods=1).median()
    local_mad = (residual - local_med).abs().rolling(window=min(5, len(residual)), center=True, min_periods=1).median()
    scale = (1.4826 * local_mad).replace(0, np.nan)
    z = ((residual - local_med).abs() / scale).replace([np.inf, -np.inf], np.nan)
    flags = z > 4.5
    isolated = flags & ~flags.shift(1, fill_value=False) & ~flags.shift(-1, fill_value=False)
    if int(isolated.sum()) > max(3, int(len(clean) * 0.12)):
        isolated[:] = False
    adjusted = clean.copy()
    if isolated.any():
        threshold = 4.5 * scale
        clipped = local_med + np.sign(residual - local_med) * np.minimum((residual - local_med).abs(), threshold.fillna(np.inf))
        adjusted.loc[isolated] = (baseline + clipped).loc[isolated]
        adjusted = _apply_metric_bounds(adjusted, metric)
    positions = [pd.Timestamp(idx).date().isoformat() for idx in adjusted.index[isolated]]
    return adjusted, {
        "count": int(isolated.sum()),
        "share_pct": round(float(isolated.sum()) / float(len(clean)) * 100.0, 1) if len(clean) else 0.0,
        "positions": positions[:6],
        "applied": bool(isolated.any()),
    }


def _weighted_mean(values: np.ndarray, weights: np.ndarray) -> float | None:
    if values.size == 0 or weights.size == 0:
        return None
    total_weight = float(np.sum(weights))
    if total_weight <= 0:
        return None
    return float(np.sum(values * weights) / total_weight)


def _safe_mean_abs(values: np.ndarray) -> float:
    if values.size == 0:
        return 0.0
    return float(np.mean(np.abs(values)))


def _selection_score(metrics: Dict[str, Any], *, prefer_recent: bool = False) -> float:
    smape = float(metrics.get("smape") or 100.0)
    wape = float(metrics.get("wape") or smape)
    rmse_pct = float(metrics.get("rmse_pct") or max(smape, wape))
    bias_pct = abs(float(metrics.get("bias_pct") or 0.0))
    direction_acc = float(metrics.get("directional_accuracy") or 0.0)
    score = (smape * 0.36) + (wape * 0.24) + (rmse_pct * 0.18) + (bias_pct * 0.12) + ((100.0 - direction_acc) * 0.10)
    if prefer_recent:
        score -= 2.5
    return float(score)


def _quality_score_from_metrics(metrics: Dict[str, Any]) -> float:
    smape = float(metrics.get("smape") or 100.0)
    wape = float(metrics.get("wape") or smape)
    bias_pct = abs(float(metrics.get("bias_pct") or 0.0))
    direction_acc = float(metrics.get("directional_accuracy") or 0.0)
    penalty = (smape * 0.45) + (wape * 0.25) + (bias_pct * 0.10) + ((100.0 - direction_acc) * 0.20)
    return float(max(0.0, min(100.0, 100.0 - penalty)))


def _recent_slope(series: pd.Series, window: int = RECENT_TREND_WINDOW) -> float:
    clean = series.dropna().astype("float64")
    if clean.empty:
        return 0.0
    sample = clean.tail(max(3, min(window, len(clean))))
    if len(sample) < 2:
        return 0.0
    x = np.arange(len(sample), dtype=float)
    try:
        return float(np.polyfit(x, sample.values.astype(float), 1)[0])
    except Exception:
        return float((sample.iloc[-1] - sample.iloc[0]) / max(1, len(sample) - 1))


def _seasonality_strength(history: pd.Series, seasonal_period: int, granularity: str) -> float:
    clean = history.dropna().astype("float64")
    if clean.empty or len(clean) < seasonal_period:
        return 0.0
    try:
        ac = float(clean.autocorr(lag=seasonal_period) or 0.0)
    except Exception:
        ac = 0.0
    profile = _seasonal_profile(clean, seasonal_period, granularity)
    signal = float(np.nanstd(profile.values.astype(float))) if not profile.empty else 0.0
    noise = float(np.nanstd(clean.values.astype(float))) if len(clean) else 0.0
    profile_ratio = (signal / noise) if noise > 0 else 0.0
    score = max(0.0, min(100.0, (max(ac, 0.0) * 65.0) + min(35.0, profile_ratio * 55.0)))
    return float(score)


def _level_shift_score(history: pd.Series) -> float:
    clean = history.dropna().astype("float64")
    if len(clean) < 12:
        return 0.0
    recent_window = min(12, max(6, len(clean) // 4))
    recent = clean.tail(recent_window)
    prior = clean.iloc[:-recent_window].tail(min(24, max(12, len(clean) - recent_window)))
    if prior.empty:
        return 0.0
    recent_mean = float(recent.mean() or 0.0)
    prior_mean = float(prior.mean() or 0.0)
    scale = float(clean.std(ddof=0) or abs(prior_mean) or 1.0)
    level_delta = abs(recent_mean - prior_mean) / max(scale, 1e-6)
    pct_delta = abs(recent_mean - prior_mean) / max(abs(prior_mean), 1e-6)
    return float(max(0.0, min(100.0, (level_delta * 30.0) + (pct_delta * 55.0))))


def _volatility_pct(history: pd.Series) -> float:
    clean = history.dropna().astype("float64")
    if clean.empty:
        return 0.0
    mean_abs = _safe_mean_abs(clean.values.astype(float))
    if mean_abs <= 0:
        return 0.0
    return float((clean.std(ddof=0) / mean_abs) * 100.0)


def _zero_share_pct(history: pd.Series) -> float:
    clean = history.dropna().astype("float64")
    if clean.empty:
        return 0.0
    return float((clean.eq(0).sum() / len(clean)) * 100.0)


def _trend_strength_score(history: pd.Series) -> float:
    clean = history.dropna().astype("float64")
    if len(clean) < 6:
        return 0.0
    slope = abs(_recent_slope(clean))
    level = max(_safe_mean_abs(clean.tail(min(12, len(clean))).values.astype(float)), 1e-6)
    return float(max(0.0, min(100.0, (slope / level) * 1200.0)))


def _score_label(score: float) -> str:
    if score >= 80:
        return "High"
    if score >= 60:
        return "Medium"
    if score >= 40:
        return "Watch"
    return "Limited"


def _confidence_tier(score: float) -> str:
    if score >= 72:
        return "high"
    if score >= 52:
        return "medium"
    return "low"


def _rolling_eval(
    history: pd.Series,
    *,
    forecast_fn: Callable[[pd.Series, int], pd.Series],
    holdout_points: int,
    eval_horizon: int = 1,
    max_windows: int = MAX_CANDIDATE_WINDOWS,
) -> Dict[str, Any]:
    clean = history.dropna().astype("float64")
    n = len(clean)
    if n < 6:
        return {
            "mape": None,
            "smape": None,
            "wape": None,
            "mae": None,
            "rmse": None,
            "mae_pct": None,
            "rmse_pct": None,
            "bias": None,
            "bias_pct": None,
            "directional_accuracy": None,
            "validation_windows": 0,
            "resid_std": 0.0,
        }

    holdout = min(max(3, int(holdout_points)), max(3, n - 4))
    origins = list(range(max(3, n - holdout), n))
    if max_windows and len(origins) > max_windows:
        origins = origins[-max_windows:]

    actuals: List[float] = []
    preds: List[float] = []
    weights: List[float] = []
    direction_hits: List[float] = []
    residuals: List[float] = []

    for offset, pos in enumerate(origins, start=1):
        train = clean.iloc[:pos]
        actual_window = clean.iloc[pos : pos + max(1, eval_horizon)]
        if train.empty or actual_window.empty:
            continue
        try:
            pred_series = forecast_fn(train, len(actual_window))
        except Exception:
            continue
        if pred_series is None or pred_series.empty:
            continue
        pred_vals = np.asarray([float(v) if pd.notna(v) else np.nan for v in pred_series.iloc[: len(actual_window)].values], dtype=float)
        actual_vals = np.asarray(actual_window.values, dtype=float)
        mask = np.isfinite(actual_vals) & np.isfinite(pred_vals)
        if not np.any(mask):
            continue
        weight = float(offset)
        baseline = float(train.iloc[-1]) if len(train) else 0.0
        actual_first = float(actual_vals[mask][0])
        pred_first = float(pred_vals[mask][0])
        actual_dir = np.sign(actual_first - baseline)
        pred_dir = np.sign(pred_first - baseline)
        direction_hits.append(100.0 if actual_dir == pred_dir else 0.0)
        for actual_val, pred_val in zip(actual_vals[mask], pred_vals[mask]):
            actuals.append(float(actual_val))
            preds.append(float(pred_val))
            weights.append(weight)
            residuals.append(float(actual_val - pred_val))

    if not actuals:
        return {
            "mape": None,
            "smape": None,
            "wape": None,
            "mae": None,
            "rmse": None,
            "mae_pct": None,
            "rmse_pct": None,
            "bias": None,
            "bias_pct": None,
            "directional_accuracy": None,
            "validation_windows": 0,
            "resid_std": 0.0,
        }

    actual_arr = np.asarray(actuals, dtype=float)
    pred_arr = np.asarray(preds, dtype=float)
    weight_arr = np.asarray(weights, dtype=float)
    err = pred_arr - actual_arr
    abs_err = np.abs(err)
    denom = np.abs(actual_arr) + np.abs(pred_arr)
    nonzero = np.abs(actual_arr) > 1e-9
    mean_abs_actual = max(_safe_mean_abs(actual_arr), 1e-6)
    mae = _weighted_mean(abs_err, weight_arr)
    rmse = float(np.sqrt(_weighted_mean(np.square(err), weight_arr) or 0.0))
    smape = _weighted_mean((2.0 * np.abs(actual_arr - pred_arr) / np.where(denom == 0, 1.0, denom)) * 100.0, weight_arr) if np.any(denom) else 0.0
    mape = _weighted_mean((np.abs((actual_arr[nonzero] - pred_arr[nonzero]) / actual_arr[nonzero])) * 100.0, weight_arr[nonzero]) if np.any(nonzero) else None
    total_actual = float(np.sum(np.abs(actual_arr) * weight_arr))
    wape = float(np.sum(abs_err * weight_arr) / total_actual * 100.0) if total_actual > 0 else None
    bias = _weighted_mean(err, weight_arr)
    bias_pct = float(np.sum(err * weight_arr) / total_actual * 100.0) if total_actual > 0 else None
    direction_acc = float(np.mean(direction_hits)) if direction_hits else None
    return {
        "mape": mape,
        "smape": smape,
        "wape": wape,
        "mae": mae,
        "rmse": rmse,
        "mae_pct": (float(mae) / mean_abs_actual * 100.0) if mae is not None else None,
        "rmse_pct": (float(rmse) / mean_abs_actual * 100.0) if rmse is not None else None,
        "bias": bias,
        "bias_pct": bias_pct,
        "directional_accuracy": direction_acc,
        "validation_windows": int(len(direction_hits)),
        "resid_std": float(np.std(np.asarray(residuals, dtype=float), ddof=1)) if len(residuals) > 1 else 0.0,
    }


def _forecast_robust_trend_generic(
    history: pd.Series,
    horizon: int,
    *,
    granularity: str,
    seasonal_period: int | None = None,
    recent_window: int = RECENT_TREND_WINDOW,
    damp: float = 0.88,
) -> pd.Series:
    clean = history.dropna().astype("float64")
    if clean.empty:
        return pd.Series(dtype="float64")
    sample = clean.tail(max(3, min(recent_window, len(clean))))
    idx = _future_index(pd.Timestamp(clean.index[-1]), horizon, granularity)
    base = float(sample.tail(min(3, len(sample))).median() if len(sample) else 0.0)
    slope = _recent_slope(sample, window=len(sample))
    future = np.asarray([base + (slope * step * (damp ** max(0, step - 1))) for step in range(1, horizon + 1)], dtype=float)
    if seasonal_period is not None and len(clean) >= seasonal_period:
        season = _future_seasonal_component(clean, horizon, seasonal_period, granularity)
        future = future + season.values.astype(float)
    return pd.Series(future, index=idx, dtype="float64")


def _forecast_level_baseline_generic(
    history: pd.Series,
    horizon: int,
    *,
    granularity: str,
    window: int = 6,
) -> pd.Series:
    clean = history.dropna().astype("float64")
    if clean.empty:
        return pd.Series(dtype="float64")
    sample = clean.tail(max(2, min(window, len(clean))))
    base = float(sample.median() if len(sample) else 0.0)
    idx = _future_index(pd.Timestamp(clean.index[-1]), horizon, granularity)
    return pd.Series([base] * horizon, index=idx, dtype="float64")


def _forecast_theta_generic(
    history: pd.Series,
    horizon: int,
    *,
    granularity: str,
    seasonal_period: int | None = None,
    recent_window: int = RECENT_SHORT_WINDOW,
) -> pd.Series:
    clean = history.dropna().astype("float64")
    if clean.empty:
        return pd.Series(dtype="float64")
    sample = clean.tail(max(6, min(recent_window, len(clean))))
    idx = _future_index(pd.Timestamp(clean.index[-1]), horizon, granularity)
    if seasonal_period is not None and len(sample) >= seasonal_period:
        seasonal_hist = _seasonal_profile(sample, seasonal_period, granularity)
        deseasonal = sample - seasonal_hist
    else:
        seasonal_hist = pd.Series(np.zeros(len(sample)), index=sample.index, dtype="float64")
        deseasonal = sample
    level = float(deseasonal.ewm(alpha=0.35, adjust=False).mean().iloc[-1] if not deseasonal.empty else 0.0)
    slope = _recent_slope(deseasonal, window=min(12, len(deseasonal)))
    future_season = _future_seasonal_component(sample, horizon, seasonal_period or 1, granularity)
    preds: List[float] = []
    last_val = float(deseasonal.iloc[-1]) if len(deseasonal) else level
    for step in range(1, horizon + 1):
        line = last_val + (slope * step)
        ses = level
        preds.append((0.58 * ses) + (0.42 * line))
    future = np.asarray(preds, dtype=float)
    if seasonal_period is not None and len(sample) >= seasonal_period:
        future = future + future_season.values.astype(float)
    return pd.Series(future, index=idx, dtype="float64")


def _forecast_stl_trend_generic(
    history: pd.Series,
    horizon: int,
    *,
    granularity: str,
    seasonal_period: int,
    recent_window: int = RECENT_MEDIUM_WINDOW,
) -> Tuple[pd.Series, float]:
    clean = history.dropna().astype("float64")
    if clean.empty:
        return pd.Series(dtype="float64"), 0.0
    sample = clean.tail(max(seasonal_period * 2, min(recent_window, len(clean))))
    if len(sample) < max(8, seasonal_period * 2):
        fc = _forecast_theta_generic(sample, horizon, granularity=granularity, seasonal_period=seasonal_period)
        return fc, float(sample.diff().std(skipna=True) or 0.0)

    from statsmodels.tsa.seasonal import STL  # type: ignore

    def _fit_and_forecast():
        stl = STL(sample, period=seasonal_period, robust=True)
        fitted = stl.fit()
        deseasonal = pd.Series(sample.values - fitted.seasonal, index=sample.index, dtype="float64")
        base = float(deseasonal.tail(min(3, len(deseasonal))).mean() if len(deseasonal) else 0.0)
        slope = _recent_slope(deseasonal, window=min(18, len(deseasonal)))
        idx = _future_index(pd.Timestamp(sample.index[-1]), horizon, granularity)
        future_season = _future_seasonal_component(pd.Series(fitted.seasonal, index=sample.index), horizon, seasonal_period, granularity)
        preds = np.asarray([base + (slope * step * (0.9 ** max(0, step - 1))) for step in range(1, horizon + 1)], dtype=float)
        preds = preds + future_season.values.astype(float)
        resid_std = float(np.nanstd(fitted.resid)) if len(fitted.resid) else 0.0
        return pd.Series(preds, index=idx, dtype="float64"), resid_std

    fc_series, resid_std = _forecast_with_timeout(_fit_and_forecast, FORECAST_TIMEOUT_SECONDS)
    return pd.Series(fc_series), float(resid_std or 0.0)


def _candidate_wrapper(
    fn: Callable[..., Any],
    *,
    window_points: int | None = None,
    as_tuple: bool = False,
    **kwargs: Any,
) -> Callable[[pd.Series, int], pd.Series]:
    def _wrapped(history: pd.Series, horizon: int) -> pd.Series:
        sample = history.tail(window_points) if window_points and len(history) > window_points else history
        result = fn(sample, horizon, **kwargs)
        if as_tuple:
            return result[0] if isinstance(result, tuple) else result
        return result

    return _wrapped


def _candidate_residual(
    fn: Callable[..., Any],
    history: pd.Series,
    horizon: int,
    *,
    window_points: int | None = None,
    fallback_std: float = 0.0,
    **kwargs: Any,
) -> Tuple[pd.Series, float]:
    sample = history.tail(window_points) if window_points and len(history) > window_points else history
    result = fn(sample, horizon, **kwargs)
    if isinstance(result, tuple):
        series, resid_std = result
        return pd.Series(series), float(resid_std or 0.0)
    return pd.Series(result), float(fallback_std or 0.0)


def _model_display_name(name: str | None) -> str | None:
    mapping = {
        "seasonal_naive": "Seasonal Naive",
        "seasonal_naive_recent24": "Seasonal Naive (Recent 24)",
        "holt_winters_add": "Holt-Winters Additive",
        "holt_winters_mul": "Holt-Winters Multiplicative",
        "holt_winters_recent36": "Holt-Winters Recent 36",
        "stl_trend_recent36": "STL + Damped Trend",
        "stl_trend_full": "STL + Damped Trend (Full)",
        "theta_recent24": "Theta-Style Trend",
        "robust_trend_recent18": "Robust Recent Trend",
        "level_baseline": "Conservative Baseline",
    }
    return mapping.get(str(name or "").strip().lower(), name)


def _forecastability_score(
    *,
    history_points: int,
    selected_metrics: Dict[str, Any],
    seasonality_strength: float,
    trend_strength: float,
    volatility_pct: float,
    level_shift_score: float,
    zero_share_pct: float,
    outlier_share_pct: float,
    partial_included: bool,
    nowcast_uncertainty_pct: float = 0.0,
) -> float:
    score = 58.0
    score += min(18.0, (history_points / 36.0) * 18.0)
    if history_points < 12:
        score -= 20.0
    elif history_points < 18:
        score -= 10.0
    score += min(10.0, seasonality_strength * 0.10)
    score += min(8.0, trend_strength * 0.08)
    smape = float(selected_metrics.get("smape") or 35.0)
    score -= min(22.0, smape * 0.55)
    score -= min(14.0, volatility_pct * 0.12)
    score -= min(10.0, level_shift_score * 0.10)
    score -= min(12.0, zero_share_pct * 0.12)
    score -= min(8.0, outlier_share_pct * 0.35)
    if partial_included:
        score -= 6.0
        score -= min(8.0, max(0.0, nowcast_uncertainty_pct) * 0.18)
    return float(max(0.0, min(100.0, score)))


def _selected_model_reason(selected: Dict[str, Any], diagnostics: Dict[str, Any]) -> str:
    name = str(selected.get("name") or "")
    history_mode = str(selected.get("history_mode") or "full")
    seasonality_strength = float(diagnostics.get("seasonality_strength_score") or 0.0)
    level_shift = float(diagnostics.get("level_shift_score") or 0.0)
    if name.startswith("stl_"):
        if history_mode.startswith("recent") or level_shift >= 35:
            return "Recent regime shift was material, so a decomposition model anchored level and trend on the latest business run-rate."
        return "A decomposition model balanced recurring seasonality with smoother trend behavior across the full history."
    if name.startswith("holt_winters"):
        if "mul" in name:
            return "Positive seasonal amplitude scaled with business level, so multiplicative Holt-Winters outperformed flatter additive baselines."
        if history_mode.startswith("recent"):
            return "Recent Holt-Winters fit beat longer-history alternatives, indicating current trend and level matter more than older periods."
        return "Holt-Winters captured both recurring seasonality and trend with lower validation error than simpler baselines."
    if name.startswith("seasonal_naive"):
        if seasonality_strength >= 45:
            return "Recurring monthly seasonality dominated the series, so the safest high-signal forecast came from repeating comparable periods."
        return "A seasonal baseline was the most stable option under the current filtered slice."
    if name.startswith("theta_"):
        return "A theta-style trend model balanced recent direction with conservative mean reversion better than heavier smoothing models."
    if name.startswith("robust_trend"):
        return "Recent movement was informative but noisy, so a damped recent-trend model outperformed broader history fits."
    return "Filtered history is limited or unstable, so the forecast stayed on a conservative baseline."


def _build_forecast_summary(metric: str, selected: Dict[str, Any], diagnostics: Dict[str, Any], partial_meta: Dict[str, Any]) -> str:
    metric_label = {"revenue": "Revenue", "profit": "Profit", "margin": "Margin"}.get(metric, metric.title())
    confidence = diagnostics.get("confidence_badge") or "Watch"
    seasonality_strength = float(diagnostics.get("seasonality_strength_score") or 0.0)
    level_shift = float(diagnostics.get("level_shift_score") or 0.0)
    partial_note = ""
    if partial_meta.get("detected") and partial_meta.get("nowcast_applied"):
        partial_note = " Current month is represented as an estimated month-end nowcast rather than raw partial actuals."
    elif partial_meta.get("detected") and partial_meta.get("excluded"):
        partial_note = " Incomplete current month was excluded from training."
    elif partial_meta.get("detected") and partial_meta.get("included"):
        partial_note = " Incomplete current month is included, so near-term uncertainty is wider."
    if selected.get("name", "").startswith("seasonal_naive") and seasonality_strength >= 45:
        return f"{metric_label} outlook follows a strong recurring seasonal pattern with {confidence.lower()} confidence.{partial_note}"
    if level_shift >= 35 and str(selected.get("history_mode") or "").startswith("recent"):
        return f"{metric_label} outlook is anchored to the recent regime rather than older history because the business level shifted materially.{partial_note}"
    return f"{metric_label} outlook balances recent trend, seasonal rhythm, and forecast uncertainty with {confidence.lower()} confidence.{partial_note}"


def _build_candidate_specs(
    history: pd.Series,
    *,
    metric: str,
    granularity: str,
    seasonal_period: int,
    diagnostics: Dict[str, Any],
) -> List[Dict[str, Any]]:
    specs: List[Dict[str, Any]] = []
    n = len(history)
    positive_only = metric in {"revenue", "profit"} and bool((history.dropna() > 0).all())
    seasonality_strength = float(diagnostics.get("seasonality_strength_score") or 0.0)
    level_shift = float(diagnostics.get("level_shift_score") or 0.0)

    def _add(
        name: str,
        family: str,
        history_mode: str,
        forecast_fn: Callable[[pd.Series, int], pd.Series],
        runner: Callable[[pd.Series, int], Tuple[pd.Series, float]],
        *,
        max_windows: int = MAX_CANDIDATE_WINDOWS,
        priority: int = 5,
    ) -> None:
        specs.append(
            {
                "name": name,
                "family": family,
                "history_mode": history_mode,
                "forecast_fn": forecast_fn,
                "runner": runner,
                "max_windows": max_windows,
                "priority": int(priority),
            }
        )

    _add(
        "level_baseline",
        "fallback",
        "recent_6",
        _candidate_wrapper(_forecast_level_baseline_generic, window_points=6, granularity=granularity, window=6),
        lambda s, h: (_forecast_level_baseline_generic(s.tail(6), h, granularity=granularity, window=6), float(s.tail(6).std(skipna=True) or 0.0)),
        priority=9,
    )
    _add(
        "robust_trend_recent18",
        "trend",
        "recent_18",
        _candidate_wrapper(
            _forecast_robust_trend_generic,
            window_points=min(RECENT_TREND_WINDOW, n),
            granularity=granularity,
            seasonal_period=seasonal_period if seasonality_strength >= 30 and n >= seasonal_period else None,
            recent_window=min(RECENT_TREND_WINDOW, n),
        ),
        lambda s, h: (
            _forecast_robust_trend_generic(
                s.tail(min(RECENT_TREND_WINDOW, len(s))),
                h,
                granularity=granularity,
                seasonal_period=seasonal_period if seasonality_strength >= 30 and len(s) >= seasonal_period else None,
                recent_window=min(RECENT_TREND_WINDOW, len(s)),
            ),
            float(s.tail(min(RECENT_TREND_WINDOW, len(s))).diff().std(skipna=True) or 0.0),
        ),
        priority=7,
    )

    if n >= 8:
        _add(
            "theta_recent24",
            "theta",
            "recent_24",
            _candidate_wrapper(
                _forecast_theta_generic,
                window_points=min(RECENT_SHORT_WINDOW, n),
                granularity=granularity,
                seasonal_period=seasonal_period if n >= seasonal_period else None,
                recent_window=min(RECENT_SHORT_WINDOW, n),
            ),
            lambda s, h: (
                _forecast_theta_generic(
                    s.tail(min(RECENT_SHORT_WINDOW, len(s))),
                    h,
                    granularity=granularity,
                    seasonal_period=seasonal_period if len(s) >= seasonal_period else None,
                    recent_window=min(RECENT_SHORT_WINDOW, len(s)),
                ),
                float(s.tail(min(RECENT_SHORT_WINDOW, len(s))).diff().std(skipna=True) or 0.0),
            ),
            priority=5,
        )

    if n >= seasonal_period:
        _add(
            "seasonal_naive",
            "seasonal_naive",
            "full",
            _candidate_wrapper(_forecast_seasonal_naive_generic, granularity=granularity, period=seasonal_period),
            lambda s, h: (
                _forecast_seasonal_naive_generic(s, h, seasonal_period, granularity),
                float((s - s.shift(seasonal_period)).std(skipna=True) or 0.0),
            ),
            priority=4,
        )

    if n >= max(12, seasonal_period * 2):
        _add(
            "stl_trend_recent36",
            "stl",
            "recent_36",
            _candidate_wrapper(
                _forecast_stl_trend_generic,
                window_points=min(RECENT_MEDIUM_WINDOW, n),
                as_tuple=True,
                granularity=granularity,
                seasonal_period=seasonal_period,
                recent_window=min(RECENT_MEDIUM_WINDOW, n),
            ),
            lambda s, h: _candidate_residual(
                _forecast_stl_trend_generic,
                s,
                h,
                window_points=min(RECENT_MEDIUM_WINDOW, len(s)),
                granularity=granularity,
                seasonal_period=seasonal_period,
                recent_window=min(RECENT_MEDIUM_WINDOW, len(s)),
            ),
            max_windows=4,
            priority=2,
        )
        _add(
            "holt_winters_recent36",
            "ets",
            "recent_36",
            _candidate_wrapper(
                _forecast_ets_generic,
                window_points=min(RECENT_MEDIUM_WINDOW, n),
                as_tuple=True,
                granularity=granularity,
                seasonal_periods=seasonal_period,
                damped=True,
                seasonal_mode="add",
            ),
            lambda s, h: _candidate_residual(
                _forecast_ets_generic,
                s,
                h,
                window_points=min(RECENT_MEDIUM_WINDOW, len(s)),
                granularity=granularity,
                seasonal_periods=seasonal_period,
                damped=True,
                seasonal_mode="add",
            ),
            max_windows=4,
            priority=3,
        )
        if positive_only and seasonality_strength >= 45:
            _add(
                "holt_winters_mul",
                "ets",
                "recent_36",
                _candidate_wrapper(
                    _forecast_ets_generic,
                    window_points=min(RECENT_MEDIUM_WINDOW, n),
                    as_tuple=True,
                    granularity=granularity,
                    seasonal_periods=seasonal_period,
                    damped=True,
                    seasonal_mode="mul",
                ),
                lambda s, h: _candidate_residual(
                    _forecast_ets_generic,
                    s,
                    h,
                    window_points=min(RECENT_MEDIUM_WINDOW, len(s)),
                    granularity=granularity,
                    seasonal_periods=seasonal_period,
                    damped=True,
                    seasonal_mode="mul",
                ),
                max_windows=4,
                priority=3,
            )

    if n >= max(18, seasonal_period * 2) and level_shift < 40:
        _add(
            "stl_trend_full",
            "stl",
            "full",
            _candidate_wrapper(
                _forecast_stl_trend_generic,
                as_tuple=True,
                granularity=granularity,
                seasonal_period=seasonal_period,
                recent_window=min(MAX_HISTORY_MONTHS, n),
            ),
            lambda s, h: _candidate_residual(
                _forecast_stl_trend_generic,
                s,
                h,
                granularity=granularity,
                seasonal_period=seasonal_period,
                recent_window=min(MAX_HISTORY_MONTHS, len(s)),
            ),
            max_windows=4,
            priority=4,
        )

    return specs


def _upper_cap(history: pd.Series) -> float:
    if history.empty:
        return 0.0
    recent_mean = float(history.tail(min(12, len(history))).mean() or 0.0)
    recent_max = float(history.tail(min(24, len(history))).max() or 0.0)
    hist_max = float(history.max() or 0.0)
    return max(recent_mean * 2.1, recent_max * 1.4, hist_max * 1.25, 0.0)


def _v2_band_for_step(resid_std: float | None, step: int) -> float | None:
    if resid_std is None or not np.isfinite(resid_std) or resid_std <= 0:
        return None
    horizon_step = max(1, int(step))
    return float(1.645 * float(resid_std) * math.sqrt(horizon_step))


def _serialize_v2_history(history: pd.Series) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    hist = history.dropna().astype("float64")
    for idx, value in hist.items():
        ts = pd.Timestamp(idx)
        rows.append({"ds": ts.date().isoformat(), "y": float(value)})
    return rows


def _serialize_v2_forecast(
    forecast: pd.Series,
    *,
    metric: str,
    resid_std: float | None,
    upper_cap: float | None,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    fc = forecast.copy() if isinstance(forecast, pd.Series) else pd.Series(dtype="float64")
    for step, (idx, raw_value) in enumerate(fc.items(), start=1):
        ts = pd.Timestamp(idx)
        yhat = _bound_value(metric, float(raw_value) if pd.notna(raw_value) else None)
        band = _v2_band_for_step(resid_std, step)
        lo = None
        hi = None
        if yhat is not None and band is not None:
            lo = yhat - band
            hi = yhat + band
        lo = _bound_value(metric, lo)
        hi = _bound_value(metric, hi)
        if metric in {"revenue", "profit"} and upper_cap is not None and hi is not None:
            hi = min(float(upper_cap), float(hi))
        rows.append({"ds": ts.date().isoformat(), "yhat": yhat, "yhat_lower": lo, "yhat_upper": hi})
    return rows


def _serialize_v2_series(
    history: pd.Series,
    forecast: pd.Series,
    *,
    granularity: str,
    metric: str,
    resid_std: float | None,
    upper_cap: float | None,
) -> Tuple[List[Dict[str, Any]], bool]:
    gran = _normalize_granularity(granularity)
    hist = history.dropna().astype(float).copy()
    fc = forecast.copy()
    if fc is None or fc.empty:
        fc = pd.Series(dtype="float64")

    bounded = False
    out: List[Dict[str, Any]] = []
    all_idx = list(hist.index) + [idx for idx in fc.index if idx not in hist.index]
    for idx in all_idx:
        ts = pd.Timestamp(idx)
        actual = float(hist.loc[idx]) if idx in hist.index else None
        forecast_val = None
        lo = None
        hi = None
        if idx in fc.index:
            raw = fc.loc[idx]
            if pd.notna(raw):
                forecast_val = float(raw)
                if metric in {"revenue", "profit"}:
                    forecast_val = max(0.0, forecast_val)
                    if upper_cap is not None and upper_cap > 0 and forecast_val > upper_cap:
                        forecast_val = upper_cap
                        bounded = True
                elif metric == "margin":
                    forecast_val = float(min(MARGIN_BOUNDS[1], max(MARGIN_BOUNDS[0], forecast_val)))
                band = _v2_band_for_step(resid_std, len([i for i in fc.index if i <= idx]))
                if band is not None and band > 0:
                    lo = forecast_val - band
                    hi = forecast_val + band
                    if metric in {"revenue", "profit"}:
                        lo = max(0.0, lo)
                        if upper_cap is not None and upper_cap > 0:
                            hi = min(upper_cap, hi)
                    elif metric == "margin":
                        lo = float(min(MARGIN_BOUNDS[1], max(MARGIN_BOUNDS[0], lo)))
                        hi = float(min(MARGIN_BOUNDS[1], max(MARGIN_BOUNDS[0], hi)))

        token = ts.strftime("%Y-%m") if gran == "monthly" else ts.strftime("%Y-%m-%d")
        out.append(
            {
                "t": token,
                "actual": actual,
                "forecast": forecast_val,
                "lo": lo,
                "hi": hi,
            }
        )
    return out, bounded


def _inject_nowcast_rows(
    history_rows: List[Dict[str, Any]],
    forecast_rows: List[Dict[str, Any]],
    series_rows: List[Dict[str, Any]],
    nowcast_payload: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not nowcast_payload.get("applied"):
        return history_rows, forecast_rows, series_rows

    period_token = str(nowcast_payload.get("period") or "").strip()
    if not period_token:
        return history_rows, forecast_rows, series_rows

    ds_token = f"{period_token}-01"
    nowcast_row = {
        "ds": ds_token,
        "yhat": nowcast_payload.get("estimated_month_end"),
        "yhat_lower": nowcast_payload.get("yhat_lower"),
        "yhat_upper": nowcast_payload.get("yhat_upper"),
    }
    series_insert = {
        "t": period_token,
        "actual": None,
        "forecast": nowcast_payload.get("estimated_month_end"),
        "lo": nowcast_payload.get("yhat_lower"),
        "hi": nowcast_payload.get("yhat_upper"),
    }

    merged_forecast = [row for row in forecast_rows if str(row.get("ds") or "") != ds_token]
    merged_forecast = [nowcast_row] + merged_forecast

    merged_series = [row for row in series_rows if str(row.get("t") or "") != period_token]
    insert_at = 0
    while insert_at < len(merged_series) and str(merged_series[insert_at].get("t") or "") < period_token:
        insert_at += 1
    merged_series = merged_series[:insert_at] + [series_insert] + merged_series[insert_at:]
    return history_rows, merged_forecast, merged_series


def forecast_metric_v2(
    filters: FilterParams,
    *,
    metric: str = "revenue",
    horizon: int = 6,
    granularity: str = "monthly",
    include_current: bool = False,
) -> Dict[str, Any]:
    metric = _normalize_metric(metric)
    if metric not in ALLOWED_METRICS:
        raise ValueError(f"Unsupported metric '{metric}'. Allowed: {', '.join(sorted(ALLOWED_METRICS))}")

    gran = _normalize_granularity(granularity)
    try:
        horizon_points = int(horizon)
    except Exception:
        horizon_points = 6
    if gran == "weekly":
        horizon_points = max(4, min(horizon_points, 24))
    else:
        horizon_points = max(3, min(horizon_points, 12))

    dataset_marker = _dataset_marker()
    cache_key = filters_cache_key(
        current_user,
        filters,
        extras={
            "scope": "overview_forecast_v2",
            "metric": metric,
            "granularity": gran,
            "horizon": horizon_points,
            "dataset": dataset_marker,
            "model_v": MODEL_VERSION_V2,
            "include_current": bool(include_current),
        },
    )
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        cached["cache_hit"] = True
        return cached

    min_points = MIN_WEEKLY_FORECAST_POINTS if gran == "weekly" else MIN_MONTHLY_FORECAST_POINTS
    notes: List[str] = []
    warnings: List[str] = []

    nowcast_payload: Dict[str, Any] = {"applied": False}
    fit_history = pd.Series(dtype="float64")
    if gran == "weekly":
        raw_history, context = _build_weekly_history(filters, metric, include_current=include_current)
        fit_history = raw_history.copy()
        partial_meta = {
            "detected": False,
            "included": False,
            "excluded": False,
            "note": "Weekly forecast uses the filtered weekly history.",
        }
        imputed_points = 0
    else:
        monthly_history_result = _build_monthly_history(filters, metric, include_current=include_current)
        if isinstance(monthly_history_result, tuple) and len(monthly_history_result) == 3:
            raw_history, fit_history, context = monthly_history_result
        elif isinstance(monthly_history_result, tuple) and len(monthly_history_result) == 2:
            raw_history, context = monthly_history_result
            fit_history = raw_history
        else:
            raw_history = pd.Series(dtype="float64")
            fit_history = pd.Series(dtype="float64")
            context = {}
        partial_meta = dict((context or {}).get("partial_period") or {})
        imputed_points = int((context or {}).get("imputed_points") or 0)
        nowcast_payload = dict((context or {}).get("nowcast") or {"applied": False})
    history_basis = dict((context or {}).get("history_basis") or {})

    raw_history = raw_history.dropna().astype("float64")
    fit_history = fit_history.dropna().astype("float64")
    if gran == "weekly":
        seasonal_period = 52 if len(raw_history) >= 104 else 13
    else:
        seasonal_period = 12
    holdout_points = min(8 if gran == "weekly" else 6, max(3, len(raw_history) // 3 if len(raw_history) else 3))

    if len(raw_history) < min_points:
        reason = f"Insufficient history for {gran} forecast ({len(raw_history)} points, need at least {min_points})."
        summary = reason
        notes = [reason, "Forecast stays disabled until more comparable history is available under the active filters."]
        history_rows = _serialize_v2_history(raw_history)
        forecast_rows: List[Dict[str, Any]] = []
        series_rows: List[Dict[str, Any]] = []
        if gran == "monthly" and nowcast_payload.get("applied"):
            series_rows, _ = _serialize_v2_series(
                raw_history,
                pd.Series(dtype="float64"),
                granularity=gran,
                metric=metric,
                resid_std=None,
                upper_cap=None,
            )
            history_rows, forecast_rows, series_rows = _inject_nowcast_rows(
                history_rows,
                forecast_rows,
                series_rows,
                nowcast_payload,
            )
            summary = "Current month is estimated to month-end, but forward monthly forecasting stays disabled until more comparable history is available."
            notes = [
                "Current month is estimated to month-end using validation-weighted pacing and recent business-day curves.",
                reason,
                "Forward forecast stays disabled until more comparable history is available under the active filters.",
            ]
        payload = {
            "eligible": False,
            "reason": reason,
            "summary": summary,
            "warnings": [reason],
            "notes": notes,
            "metric": metric,
            "granularity": gran,
            "horizon": horizon_points,
            "model": {
                "name": None,
                "display_name": None,
                "smape": None,
                "mae": None,
                "wape": None,
                "train_points": int(len(raw_history)),
                "holdout_points": int(min(holdout_points, max(0, len(raw_history) - 1))),
                "confidence": "low",
                "confidence_badge": "Limited",
                "forecastability_score": 0.0,
                "quality_score": 0.0,
                "candidate_count": 0,
                "runner_ups": [],
                "candidates": [],
            },
            "diagnostics": {
                "history_points": int(len(raw_history)),
                "available_history_points": int(history_basis.get("available_points") or len(raw_history)),
                "history_start": raw_history.index.min().date().isoformat() if len(raw_history.index) else None,
                "history_end": raw_history.index.max().date().isoformat() if len(raw_history.index) else None,
                "history_basis_label": history_basis.get("label") or "Filtered comparable history",
                "history_basis_mode": history_basis.get("mode") or "full",
                "history_basis_reason": history_basis.get("reason"),
                "training_cutoff": raw_history.index.max().date().isoformat() if len(raw_history.index) else None,
                "seasonality_strength_score": 0.0,
                "trend_strength_score": 0.0,
                "volatility_pct": 0.0,
                "level_shift_score": 0.0,
                "zero_share_pct": 0.0,
                "outliers_adjusted": 0,
                "partial_period": partial_meta,
                "growth_regime": nowcast_payload.get("growth_regime"),
                "stability_score": nowcast_payload.get("stability_score"),
                "bias_risk": nowcast_payload.get("bias_risk"),
                "current_month_basis": nowcast_payload.get("current_month_basis"),
                "nowcast_uncertainty_pct": nowcast_payload.get("uncertainty_pct"),
            },
            "history": history_rows,
            "forecast": forecast_rows,
            "series": series_rows,
            "nowcast": nowcast_payload,
            "cache_hit": False,
            "dataset_version": dataset_marker,
            "model_version": MODEL_VERSION_V2,
        }
        cache.set(cache_key, payload, timeout=FORECAST_TTL_SECONDS)
        return payload

    clean_history, outlier_meta = _robust_outlier_adjust(
        raw_history,
        metric=metric,
        granularity=gran,
        seasonal_period=max(4, seasonal_period),
    )
    if clean_history.empty:
        clean_history = raw_history.copy()
    fit_history_for_runner = clean_history.copy()
    if gran == "monthly" and nowcast_payload.get("applied") and len(fit_history.index) > len(raw_history.index):
        terminal_idx = fit_history.index.max()
        terminal_value = fit_history.loc[terminal_idx]
        if pd.notna(terminal_value):
            fit_history_for_runner.loc[pd.Timestamp(terminal_idx)] = float(terminal_value)
            fit_history_for_runner = fit_history_for_runner.sort_index()

    seasonality_strength = _seasonality_strength(clean_history, max(4, seasonal_period), gran)
    trend_strength = _trend_strength_score(clean_history)
    volatility_pct = _volatility_pct(clean_history)
    level_shift_score = _level_shift_score(clean_history)
    zero_share_pct = _zero_share_pct(raw_history if metric != "margin" else clean_history)

    diagnostics: Dict[str, Any] = {
        "history_points": int(len(raw_history)),
        "train_points": int(len(clean_history)),
        "history_start": raw_history.index.min().date().isoformat() if len(raw_history.index) else None,
        "history_end": raw_history.index.max().date().isoformat() if len(raw_history.index) else None,
        "available_history_points": int(history_basis.get("available_points") or len(raw_history)),
        "available_history_start": history_basis.get("available_start"),
        "available_history_end": history_basis.get("available_end"),
        "history_basis_label": history_basis.get("label") or "Filtered comparable history",
        "history_basis_mode": history_basis.get("mode") or "full",
        "history_basis_reason": history_basis.get("reason"),
        "history_basis_points": int(history_basis.get("selected_points") or len(raw_history)),
        "history_basis_start": history_basis.get("selected_start") or (raw_history.index.min().date().isoformat() if len(raw_history.index) else None),
        "history_basis_end": history_basis.get("selected_end") or (raw_history.index.max().date().isoformat() if len(raw_history.index) else None),
        "history_excluded_points": int(history_basis.get("excluded_points") or 0),
        "history_non_zero_share_pct": float(history_basis.get("non_zero_share_pct") or 0.0),
        "training_cutoff": clean_history.index.max().date().isoformat() if len(clean_history.index) else None,
        "seasonality_period": int(seasonal_period),
        "seasonality_strength_score": round(float(seasonality_strength), 1),
        "seasonality_strength_label": _score_label(float(seasonality_strength)),
        "trend_strength_score": round(float(trend_strength), 1),
        "volatility_pct": round(float(volatility_pct), 1),
        "level_shift_score": round(float(level_shift_score), 1),
        "level_shift_detected": bool(level_shift_score >= 35.0),
        "zero_share_pct": round(float(zero_share_pct), 1),
        "outliers_adjusted": int(outlier_meta.get("count") or 0),
        "outlier_share_pct": float(outlier_meta.get("share_pct") or 0.0),
        "outlier_positions": outlier_meta.get("positions") or [],
        "imputed_points": int(imputed_points),
        "partial_period": partial_meta,
        "history_scope": history_basis.get("mode") or "recent-aware",
        "growth_regime": nowcast_payload.get("growth_regime"),
        "stability_score": nowcast_payload.get("stability_score"),
        "bias_risk": nowcast_payload.get("bias_risk"),
        "current_month_basis": nowcast_payload.get("current_month_basis"),
        "nowcast_uncertainty_pct": nowcast_payload.get("uncertainty_pct"),
        "nowcast_period": nowcast_payload.get("period"),
    }

    if partial_meta.get("note"):
        notes.append(str(partial_meta.get("note")))
    if nowcast_payload.get("applied"):
        notes.append(
            "Current month is nowcasted to month-end using validation-weighted pacing, recent business-day curves, and prior comparable periods."
        )
    if history_basis.get("reason"):
        notes.append(str(history_basis.get("reason")))
    if outlier_meta.get("count"):
        notes.append(f"Adjusted {int(outlier_meta.get('count') or 0)} isolated outlier period(s) before fitting.")
    if imputed_points:
        notes.append(f"Filled {int(imputed_points)} missing margin period(s) to stabilize bounded margin forecasting.")
    if diagnostics["level_shift_detected"]:
        notes.append("Recent level shift detected, so recent-history candidates receive extra weight in selection.")
    if seasonality_strength >= 45:
        notes.append("Strong recurring seasonality detected in the training history.")
    elif seasonality_strength < 20:
        warnings.append("Seasonality is weak under the active filters, so the forecast leans more on recent trend than repeating annual patterns.")
    if zero_share_pct >= 30:
        warnings.append("Sparse periods are elevated under the current filters; forecast uncertainty is wider.")

    candidate_specs = _build_candidate_specs(
        clean_history,
        metric=metric,
        granularity=gran,
        seasonal_period=max(4, seasonal_period),
        diagnostics=diagnostics,
    )

    candidates: List[Dict[str, Any]] = []
    for spec in candidate_specs:
        metrics = _rolling_eval(
            clean_history,
            forecast_fn=spec["forecast_fn"],
            holdout_points=holdout_points,
            eval_horizon=min(2, horizon_points) if gran == "monthly" else 1,
            max_windows=int(spec.get("max_windows") or MAX_CANDIDATE_WINDOWS),
        )
        prefer_recent = str(spec.get("history_mode") or "").startswith("recent") and diagnostics["level_shift_detected"]
        selection_score = _selection_score(metrics, prefer_recent=prefer_recent) if metrics.get("smape") is not None else float("inf")
        quality_score = _quality_score_from_metrics(metrics) if metrics.get("smape") is not None else 0.0
        candidates.append(
            {
                **spec,
                **metrics,
                "selection_score": round(float(selection_score), 3) if np.isfinite(selection_score) else None,
                "quality_score": round(float(quality_score), 1),
                "display_name": _model_display_name(spec.get("name")),
            }
        )

    valid_candidates = [c for c in candidates if c.get("smape") is not None and c.get("selection_score") is not None]
    ranked_candidates = sorted(
        valid_candidates,
        key=lambda item: (
            float(item.get("selection_score")) if item.get("selection_score") is not None else float("inf"),
            int(item.get("priority") or 9),
            float(item.get("smape")) if item.get("smape") is not None else float("inf"),
        ),
    )

    selected = ranked_candidates[0] if ranked_candidates else next((c for c in candidates if c.get("name") == "level_baseline"), None)
    if selected is None:
        selected = {
            "name": "level_baseline",
            "display_name": "Conservative Baseline",
            "history_mode": "recent_6",
            "family": "fallback",
            "runner": lambda s, h: (_forecast_level_baseline_generic(s, h, granularity=gran, window=6), float(s.std(skipna=True) or 0.0)),
            "smape": None,
            "wape": None,
            "mae": None,
            "rmse": None,
            "bias_pct": None,
            "directional_accuracy": None,
            "quality_score": 0.0,
            "selection_score": None,
            "priority": 9,
        }
        warnings.append("Model validation was incomplete, so the forecast fell back to a conservative baseline.")

    try:
        forecast_series, resid_std = selected["runner"](fit_history_for_runner, horizon_points)
    except Exception:
        forecast_series = _forecast_level_baseline_generic(fit_history_for_runner, horizon_points, granularity=gran, window=6)
        resid_std = float(fit_history_for_runner.std(skipna=True) or 0.0)
        warnings.append("Selected model failed during final scoring; conservative baseline used instead.")
        selected["name"] = "level_baseline"
        selected["display_name"] = "Conservative Baseline"
        selected["family"] = "fallback"

    forecast_series = _apply_metric_bounds(pd.Series(forecast_series), metric)
    if not isinstance(forecast_series.index, pd.DatetimeIndex):
        forecast_series.index = _future_index(pd.Timestamp(clean_history.index[-1]), horizon_points, gran)

    cap_val = _upper_cap(clean_history) if metric in {"revenue", "profit"} else None
    series_rows, bounded = _serialize_v2_series(
        raw_history,
        forecast_series,
        granularity=gran,
        metric=metric,
        resid_std=float(resid_std or selected.get("resid_std") or 0.0),
        upper_cap=cap_val,
    )
    if bounded:
        notes.append("Applied a high-end cap to keep projected scale within observed business bounds.")

    history_rows = _serialize_v2_history(raw_history)
    forecast_rows = _serialize_v2_forecast(
        forecast_series,
        metric=metric,
        resid_std=float(resid_std or selected.get("resid_std") or 0.0),
        upper_cap=cap_val,
    )
    if gran == "monthly" and nowcast_payload.get("applied"):
        history_rows, forecast_rows, series_rows = _inject_nowcast_rows(
            history_rows,
            forecast_rows,
            series_rows,
            nowcast_payload,
        )

    selected_metrics = {
        "smape": selected.get("smape"),
        "wape": selected.get("wape"),
        "mae": selected.get("mae"),
        "rmse": selected.get("rmse"),
        "bias_pct": selected.get("bias_pct"),
        "directional_accuracy": selected.get("directional_accuracy"),
        "quality_score": selected.get("quality_score"),
    }
    forecastability = _forecastability_score(
        history_points=int(len(clean_history)),
        selected_metrics=selected_metrics,
        seasonality_strength=float(seasonality_strength),
        trend_strength=float(trend_strength),
        volatility_pct=float(volatility_pct),
        level_shift_score=float(level_shift_score),
        zero_share_pct=float(zero_share_pct),
        outlier_share_pct=float(outlier_meta.get("share_pct") or 0.0),
        partial_included=bool(partial_meta.get("included")),
        nowcast_uncertainty_pct=float(nowcast_payload.get("uncertainty_pct") or 0.0),
    )
    combined_confidence_score = max(0.0, min(100.0, (forecastability * 0.55) + (float(selected.get("quality_score") or 0.0) * 0.45)))
    confidence_badge = _score_label(combined_confidence_score)
    confidence_tier = _confidence_tier(combined_confidence_score)
    diagnostics["forecastability_score"] = round(float(forecastability), 1)
    diagnostics["confidence_score"] = round(float(combined_confidence_score), 1)
    diagnostics["confidence_badge"] = confidence_badge

    if combined_confidence_score < 40:
        warnings.append("Forecastability is limited under the current filters; use the outlook directionally rather than as a hard plan.")
    elif combined_confidence_score < 60:
        warnings.append("Forecast quality is moderate; validate the outlook against the underlying movers and risk panels.")
    if nowcast_payload.get("applied") and float(nowcast_payload.get("uncertainty_pct") or 0.0) >= 18.0:
        warnings.append("Current-month nowcast uncertainty is elevated, so treat the ongoing month estimate as directional.")

    model_reason = _selected_model_reason(selected, diagnostics)
    notes.append(model_reason)
    summary = _build_forecast_summary(metric, selected, diagnostics, partial_meta)

    runner_ups = []
    for cand in ranked_candidates[1:3]:
        runner_ups.append(
            {
                "name": cand.get("name"),
                "display_name": cand.get("display_name"),
                "smape": cand.get("smape"),
                "wape": cand.get("wape"),
                "selection_score": cand.get("selection_score"),
                "history_mode": cand.get("history_mode"),
            }
        )

    model_payload = {
        "name": selected.get("name"),
        "display_name": selected.get("display_name"),
        "family": selected.get("family"),
        "history_mode": selected.get("history_mode"),
        "smape": selected.get("smape"),
        "mape": selected.get("mape"),
        "wape": selected.get("wape"),
        "mae": selected.get("mae"),
        "rmse": selected.get("rmse"),
        "bias_pct": selected.get("bias_pct"),
        "directional_accuracy": selected.get("directional_accuracy"),
        "selection_score": selected.get("selection_score"),
        "quality_score": round(float(selected.get("quality_score") or 0.0), 1),
        "forecastability_score": round(float(forecastability), 1),
        "train_points": int(len(clean_history)),
        "holdout_points": int(holdout_points),
        "validation_windows": int(selected.get("validation_windows") or 0),
        "confidence": confidence_tier,
        "confidence_badge": confidence_badge,
        "stability_score": diagnostics.get("stability_score"),
        "bias_risk": diagnostics.get("bias_risk"),
        "candidate_count": int(len(candidates)),
        "selection_reason": model_reason,
        "runner_ups": runner_ups,
        "candidates": [
            {
                "name": c.get("name"),
                "display_name": c.get("display_name"),
                "family": c.get("family"),
                "history_mode": c.get("history_mode"),
                "smape": c.get("smape"),
                "wape": c.get("wape"),
                "mae": c.get("mae"),
                "rmse": c.get("rmse"),
                "bias_pct": c.get("bias_pct"),
                "directional_accuracy": c.get("directional_accuracy"),
                "selection_score": c.get("selection_score"),
                "quality_score": c.get("quality_score"),
            }
            for c in ranked_candidates[:6]
        ],
    }

    payload = {
        "eligible": True,
        "reason": None,
        "summary": summary,
        "warnings": warnings,
        "notes": notes,
        "metric": metric,
        "granularity": gran,
        "horizon": horizon_points,
        "history": history_rows,
        "forecast": forecast_rows,
        "series": series_rows,
        "model": model_payload,
        "diagnostics": diagnostics,
        "nowcast": nowcast_payload,
        "confidence": confidence_tier,
        "confidence_badge": confidence_badge,
        "cache_hit": False,
        "dataset_version": dataset_marker,
        "model_version": MODEL_VERSION_V2,
    }
    cache.set(cache_key, payload, timeout=FORECAST_TTL_SECONDS)
    return payload


def forecast_metric(
    filters: FilterParams,
    *,
    metric: str = "revenue",
    horizon_months: int = 6,
    include_current_month: bool = False,
) -> Dict[str, Any]:
    metric = _normalize_metric(metric)
    if metric not in ALLOWED_METRICS:
        raise ValueError(f"Unsupported metric '{metric}'. Allowed: {', '.join(sorted(ALLOWED_METRICS))}")
    try:
        horizon = max(1, min(int(horizon_months), 12))
    except Exception:
        horizon = 6

    dataset_marker = _dataset_marker()
    cache_key = filters_cache_key(
        current_user,
        filters,
        extras={
            "scope": "overview_forecast_legacy",
            "metric": metric,
            "horizon": horizon,
            "dataset": dataset_marker,
            "model_v": MODEL_VERSION,
            "include_current_month": bool(include_current_month),
        },
    )
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        cached["cache_hit"] = True
        return cached

    v2_payload = forecast_metric_v2(
        filters,
        metric=metric,
        horizon=horizon,
        granularity="monthly",
        include_current=include_current_month,
    )

    history_rows = list(v2_payload.get("history") or [])
    forecast_rows = list(v2_payload.get("forecast") or [])
    model = dict(v2_payload.get("model") or {})
    diagnostics = dict(v2_payload.get("diagnostics") or {})
    warnings = list(v2_payload.get("warnings") or [])
    notes = list(v2_payload.get("notes") or [])

    legacy_series: List[Dict[str, Any]] = []
    for row in v2_payload.get("series") or []:
        token = row.get("t")
        month = f"{token}-01" if token and len(str(token)) == 7 else token
        legacy_series.append(
            {
                "month": month,
                "actual": row.get("actual"),
                "yhat": row.get("forecast"),
                "yhat_lower": row.get("lo"),
                "yhat_upper": row.get("hi"),
            }
        )

    payload: Dict[str, Any] = {
        "metric": metric,
        "horizon_months": horizon,
        "include_partial_current_month": bool(include_current_month),
        "history_points": int(diagnostics.get("history_points") or model.get("train_points") or 0),
        "model_used": model.get("name"),
        "warnings": warnings + [note for note in notes if note not in warnings],
        "series": legacy_series,
        "history": history_rows,
        "forecast": forecast_rows,
        "model_info": {
            "name": model.get("name"),
            "display_name": model.get("display_name"),
            "mape": model.get("mape"),
            "smape": model.get("smape"),
            "mae": model.get("mae"),
            "wape": model.get("wape"),
            "n_points": int(diagnostics.get("train_points") or model.get("train_points") or 0),
            "candidate_count": int(model.get("candidate_count") or 0),
            "quality_score": model.get("quality_score"),
            "forecastability_score": model.get("forecastability_score"),
        },
        "model_candidates": list(model.get("candidates") or []),
        "error": None if v2_payload.get("eligible") else (v2_payload.get("reason") or "Forecast unavailable."),
        "cache_hit": False,
        "model_version": MODEL_VERSION,
        "dataset_version": dataset_marker,
        "backtest": {
            "mape": model.get("mape"),
            "smape": model.get("smape"),
            "mae": model.get("mae"),
            "wape": model.get("wape"),
            "rmse": model.get("rmse"),
            "bias_pct": model.get("bias_pct"),
            "directional_accuracy": model.get("directional_accuracy"),
            "n": model.get("validation_windows"),
        },
        "model_selection": {
            "selected_model": model.get("name"),
            "criterion": "rolling_walk_forward_weighted_score",
            "selection_score": model.get("selection_score"),
        },
        "confidence": model.get("confidence"),
        "confidence_badge": model.get("confidence_badge"),
        "last_train_date": diagnostics.get("training_cutoff"),
        "summary": v2_payload.get("summary"),
        "eligible": v2_payload.get("eligible"),
        "reason": v2_payload.get("reason"),
        "diagnostics": diagnostics,
    }

    if payload["error"] and not payload["series"]:
        payload["history"] = history_rows
        payload["forecast"] = []

    cache.set(cache_key, payload, timeout=FORECAST_TTL_SECONDS)
    return payload


def _serialize_history(history: pd.Series) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    hist_idx = history.index if isinstance(history.index, pd.PeriodIndex) else pd.Index([])
    for period in hist_idx:
        month = period.to_timestamp().normalize() if hasattr(period, "to_timestamp") else None
        raw_val = history.loc[period]
        val = None
        if pd.notna(raw_val):
            try:
                val = float(raw_val)
            except Exception:
                val = None
        out.append(
            {
                "ds": month.strftime("%Y-%m-%d") if month is not None else None,
                "y": val,
            }
        )
    return out


def _serialize_forecast(forecast: pd.Series, *, resid_std: float = 0.0, metric: str = "revenue") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    fc_idx = forecast.index if isinstance(forecast.index, pd.PeriodIndex) else pd.Index([])
    band = float(resid_std or 0.0)
    metric = _normalize_metric(metric)
    for period in fc_idx:
        month = period.to_timestamp().normalize() if hasattr(period, "to_timestamp") else None
        raw_val = forecast.loc[period]
        yhat = None
        if pd.notna(raw_val):
            try:
                yhat = float(raw_val)
            except Exception:
                yhat = None
        lower = None
        upper = None
        if yhat is not None and band:
            lower = yhat - 1.96 * band
            upper = yhat + 1.96 * band
        yhat = _bound_value(metric, yhat)
        lower = _bound_value(metric, lower)
        upper = _bound_value(metric, upper)
        out.append(
            {
                "ds": month.strftime("%Y-%m-%d") if month is not None else None,
                "yhat": yhat,
                "yhat_lower": lower,
                "yhat_upper": upper,
            }
        )
    return out


def _serialize_series(history: pd.Series, forecast: pd.Series, resid_std: float = 0.0, metric: str = "revenue") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    hist_idx = history.index if isinstance(history.index, pd.PeriodIndex) else pd.Index([])
    fc_idx = forecast.index if isinstance(forecast.index, pd.PeriodIndex) else pd.Index([])
    hist_map = {p: float(history.loc[p]) if pd.notna(history.loc[p]) else None for p in hist_idx}
    fc_map = {p: float(forecast.loc[p]) if pd.notna(forecast.loc[p]) else None for p in fc_idx}

    all_periods = list(hist_idx) + [p for p in fc_idx if p not in hist_idx]
    band = float(resid_std or 0.0)
    metric = _normalize_metric(metric)

    for period in all_periods:
        month = period.to_timestamp().normalize() if hasattr(period, "to_timestamp") else None
        actual = hist_map.get(period)
        yhat = fc_map.get(period)
        lower = None
        upper = None
        if yhat is not None and band:
            lower = yhat - 1.96 * band
            upper = yhat + 1.96 * band
        out.append(
            {
                "month": month.strftime("%Y-%m-%d") if month is not None else None,
                "actual": actual,
                "yhat": _bound_value(metric, yhat),
                "yhat_lower": _bound_value(metric, lower),
                "yhat_upper": _bound_value(metric, upper),
            }
        )
    return out
