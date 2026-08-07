from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import pandas as pd

_logger = logging.getLogger("fact_checkpoints")


def _safe_numeric(series: Optional[pd.Series]) -> pd.Series:
    if series is None or not isinstance(series, pd.Series):
        return pd.Series(dtype="float64")
    return pd.to_numeric(series, errors="coerce").fillna(0.0)


def _coerce_frame(obj: Any) -> pd.DataFrame:
    if isinstance(obj, pd.DataFrame):
        return obj
    if obj is None:
        return pd.DataFrame()
    try:
        return pd.DataFrame(obj)
    except Exception:
        return pd.DataFrame()


def log_fact_checkpoint(stage: str, df_or_query_result: Any, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Emit a structured diagnostic for fact lifecycle checkpoints.

    Metrics:
      - rows
      - distinct OrderId / OrderLineId / ProductId
      - min/max DateExpected/Date
      - SUM(Revenue/Cost/Profit) when present
      - filter payload (start/end/status/role/user/region)
    """
    meta = dict(meta or {})
    df = _coerce_frame(df_or_query_result)

    stats: Dict[str, Any] = {
        "stage": stage,
        "rows": int(len(df)),
        "distinct_order_ids": int(df["OrderId"].nunique()) if "OrderId" in df.columns else None,
        "distinct_order_line_ids": int(df["OrderLineId"].nunique()) if "OrderLineId" in df.columns else None,
        "distinct_product_ids": int(df["ProductId"].nunique()) if "ProductId" in df.columns else None,
    }

    # Date bounds
    date_col = None
    for candidate in ("DateExpected", "Date", "ShipDate"):
        if candidate in df.columns:
            date_col = candidate
            break
    if date_col:
        dates = pd.to_datetime(df[date_col], errors="coerce").dropna()
        stats["date_min"] = dates.min().isoformat() if not dates.empty else None
        stats["date_max"] = dates.max().isoformat() if not dates.empty else None

    # Numeric sums
    for col in ("Revenue", "Cost", "Profit"):
        if col in df.columns:
            series = _safe_numeric(df[col])
            stats[f"sum_{col.lower()}"] = float(round(series.sum(), 6))

    stats.update({k: v for k, v in meta.items() if v is not None})

    try:
        _logger.info("fact_checkpoint", extra=stats)
    except Exception:
        # Logging must never break execution
        pass
    return stats

