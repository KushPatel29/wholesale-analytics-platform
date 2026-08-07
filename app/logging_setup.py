from __future__ import annotations

import json
import logging
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
        # Include common HTTP/request fields if provided via `extra=`
        for k in (
            "request_id",
            "method",
            "path",
            "route",
            "status",
            "duration_ms",
            "user_id",
            "user_role",
        ):
            v = getattr(record, k, None)
            if v is not None:
                payload[k] = v
        return json.dumps(payload, ensure_ascii=False)


def configure_json_logging(app_logger: logging.Logger, log_path: str = "logs/app.jsonl") -> None:
    # Ensure directory exists
    p = Path(log_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(p.as_posix(), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(JsonFormatter())

    # Avoid duplicate handlers if called twice
    have = any(isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", None) == handler.baseFilename for h in app_logger.handlers)
    if not have:
        app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False

