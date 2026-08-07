from __future__ import annotations

import math
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd


def sanitize_for_json(obj: Any) -> Any:
    """
    Recursively coerce payloads into JSON-safe primitives.

    Rules:
    - NaN/Infinity → None
    - numpy/pandas scalars → native Python types
    - timestamps/periods → ISO-8601 strings (tz-naive)
    - Decimal → float
    """

    def _is_bad_number(value: float) -> bool:
        try:
            return math.isnan(value) or math.isinf(value)
        except Exception:
            return False

    def _coerce(val: Any) -> Any:
        if val is None:
            return None

        # Fast-path primitives
        if isinstance(val, (str, bool, int)):
            return val

        # Numeric coercion
        if isinstance(val, Decimal):
            coerced = float(val)
            return None if _is_bad_number(coerced) else coerced
        if isinstance(val, (float, np.floating)):
            return None if _is_bad_number(float(val)) else float(val)
        if isinstance(val, (np.integer,)):
            return int(val)
        if isinstance(val, (np.bool_,)):
            return bool(val)

        # Datetime-like
        if isinstance(val, (pd.Timestamp, datetime)):
            return val.tz_localize(None).isoformat() if hasattr(val, "tz_localize") else val.replace(tzinfo=None).isoformat()
        if isinstance(val, pd.Period):
            return val.to_timestamp().replace(tzinfo=None).isoformat()
        if isinstance(val, (pd.Timedelta,)):
            return val.isoformat()
        if isinstance(val, date):
            return datetime.combine(val, datetime.min.time()).isoformat()

        # Collections
        if isinstance(val, dict):
            return {k: _coerce(v) for k, v in val.items()}
        if isinstance(val, (list, tuple, set)):
            return [_coerce(v) for v in list(val)]
        if isinstance(val, np.ndarray):
            return [_coerce(v) for v in val.tolist()]

        # Pandas containers
        if isinstance(val, pd.Series):
            return [_coerce(v) for v in val.tolist()]
        if isinstance(val, pd.DataFrame):
            return [_coerce(rec) for rec in val.to_dict(orient="records")]
        if isinstance(val, (pd.Index, pd.TimedeltaIndex, pd.DatetimeIndex)):
            return [_coerce(v) for v in val.tolist()]

        # Pandas / numpy NA
        try:
            if pd.isna(val):
                return None
        except Exception:
            pass

        return val

    return _coerce(obj)


def dumps_sanitized(payload: Any) -> str:
    """JSON dumps with NaN/Inf protection via sanitize_for_json."""
    safe = sanitize_for_json(payload)
    return json.dumps(safe, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
