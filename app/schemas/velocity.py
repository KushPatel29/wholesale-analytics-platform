from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Literal, Optional, Tuple

Metric = Literal["units", "lb"]
Freq = Literal["W", "M", "Y"]


@dataclass
class SeriesPoint:
    ds: str
    y: float


@dataclass
class ForecastPoint:
    ds: str
    yhat: float
    yhat_lower: float
    yhat_upper: float


@dataclass
class SeriesPayload:
    x: List[str]
    y: List[float]
    rolling: Dict[str, List[float]] = field(default_factory=dict)
    meta: Dict[str, str] = field(default_factory=dict)


@dataclass
class ForecastPayload:
    history: List[SeriesPoint]
    forecast: List[ForecastPoint]
    meta: Dict[str, str] = field(default_factory=dict)


# Lightweight validation schemas (pydantic-like behavior without dependency)
@dataclass
class ProductSearchQuery:
    q: str = ""
    limit: int = 20
    offset: int = 0

    @classmethod
    def from_args(cls, args) -> Tuple["ProductSearchQuery", Optional[str]]:
        try:
            q = (args.get("q") or "").strip()
            raw_limit = args.get("limit") or 20
            raw_offset = args.get("offset") or 0
            limit = int(raw_limit)
            offset = int(raw_offset)
            if limit < 1 or limit > 50:
                return cls(), "limit must be between 1 and 50"
            if offset < 0:
                return cls(), "offset must be >= 0"
            return cls(q=q, limit=limit, offset=offset), None
        except Exception:
            return cls(), "invalid query params"
