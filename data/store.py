from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Optional, Set

from app.core.cache_manager import CACHE_MANAGER, CacheManager
from app.core.exceptions import DatasetNotBuiltError

logger = logging.getLogger(__name__)

# Preserve legacy name expected by callers
CACHE_MANAGER: CacheManager = CACHE_MANAGER


def _fact_store():
    # Lazy import to avoid circular dependencies during app startup
    from app.services import fact_store  # type: ignore

    return fact_store


def dataset_glob() -> str:
    return _fact_store().dataset_glob()


def get_conn():
    return _fact_store().get_conn()


def init_views(conn=None) -> None:
    return _fact_store().init_views(conn)


def list_columns(conn=None) -> Set[str]:
    return set(_fact_store().list_columns(conn))


def _meta() -> dict[str, Any]:
    try:
        return CACHE_MANAGER.get_metadata()
    except Exception:
        try:
            meta_path = CACHE_MANAGER.meta_path
            if meta_path.exists():
                return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("duckdb.meta_read_failed", exc_info=True)
    return {}


def manifest_max_date() -> Optional[str]:
    meta = _meta()
    for key in ("max_date", "date_max", "watermark", "watermark_dt", "last_sql_watermark"):
        val = meta.get(key)
        if not val:
            continue
        try:
            dt = datetime.fromisoformat(str(val).replace("Z", ""))
            return dt.date().isoformat()
        except Exception:
            continue
    return None


def manifest_version() -> Optional[str]:
    meta = _meta()
    return meta.get("dataset_version") or meta.get("version") or meta.get("last_refresh_utc")

