from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        payload = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Common HTTP/request fields accepted via logging `extra=`
        for k in (
            "request_id",
            "method",
            "path",
            "route",
            "status",
            "duration_ms",
            "user_id",
            "current_user_id",
            "role",
            "user_role",
            "endpoint",
            "effective_filters",
            "filters_source",
            "filter_resolution_source",
            "filters_hash",
            "cache_key_hash",
            "window_start",
            "window_end",
        ):
            v = getattr(record, k, None)
            if v is not None:
                payload[k] = v
        return json.dumps(payload, ensure_ascii=False)


def _level_from_env(default: str = "INFO") -> int:
    val = (os.getenv("LOG_LEVEL") or default).strip().upper()
    return getattr(logging, val, logging.INFO)


def configure_json_logging(app_logger: logging.Logger, log_path: str = "logs/app.jsonl") -> None:
    """Attach a rotating JSON file handler to the given logger.

    - Path: logs/app.jsonl (overridable)
    - Rotation: 10MB x 5 files
    - Level: LOG_LEVEL env (default INFO)
    """
    # Ensure directory exists
    p = Path(log_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(p.as_posix(), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    handler.setLevel(_level_from_env())

    # Avoid duplicates if reconfigured
    have = any(isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", None) == handler.baseFilename for h in app_logger.handlers)
    if not have:
        app_logger.addHandler(handler)

    app_logger.setLevel(_level_from_env())
    app_logger.propagate = False
