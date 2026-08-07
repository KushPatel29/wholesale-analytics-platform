from __future__ import annotations

import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.services import watermark_store

logger = logging.getLogger(__name__)

_STATUS_LOCK = threading.Lock()
_STATUS: Dict[str, Any] = {
    "dataset_version": None,
    "watermark": None,
    "min_date": None,
    "max_date": None,
    "last_refresh_at": None,
    "last_error": None,
    "last_status": None,
    "running": False,
}

_HANDLE_LOCK = threading.Lock()
_HANDLE: Optional["RefreshHandle"] = None


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def _update_status(**updates: Any) -> None:
    with _STATUS_LOCK:
        _STATUS.update(updates)


def _update_from_manifest(manifest: Dict[str, Any], *, status: Optional[str] = None, error: Optional[str] = None) -> None:
    payload = manifest or {}
    _update_status(
        dataset_version=payload.get("dataset_version"),
        watermark=payload.get("watermark") or payload.get("last_sql_watermark"),
        min_date=payload.get("min_date") or payload.get("min_dateexpected"),
        max_date=payload.get("max_date") or payload.get("max_dateexpected"),
        last_refresh_at=payload.get("last_refresh_utc") or payload.get("built_at_utc") or _now_utc_iso(),
        last_error=error,
        last_status=status or payload.get("status"),
    )


def get_status() -> Dict[str, Any]:
    with _STATUS_LOCK:
        return dict(_STATUS)


def _sleep_with_stop(stop_event: threading.Event, total_seconds: int) -> None:
    remaining = max(0, int(total_seconds))
    while remaining > 0 and not stop_event.is_set():
        chunk = min(1, remaining)
        stop_event.wait(timeout=chunk)
        remaining -= chunk


@dataclass
class RefreshHandle:
    thread: threading.Thread
    stop_event: threading.Event
    interval_seconds: int
    lookback_days: int
    jitter_seconds: int

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_event.set()
        try:
            self.thread.join(timeout=timeout)
        finally:
            _update_status(running=False)


def start_background_refresh(app) -> RefreshHandle:
    """
    Start the in-process incremental refresh loop (single thread).
    Returns a handle with stop() for clean shutdown.
    """
    global _HANDLE
    with _HANDLE_LOCK:
        if _HANDLE and _HANDLE.thread.is_alive():
            return _HANDLE

        interval = max(1, _int_env("FACT_REFRESH_INTERVAL_SECONDS", 300))
        lookback = max(1, _int_env("FACT_REFRESH_LOOKBACK_DAYS", 14))
        jitter = max(0, _int_env("FACT_REFRESH_JITTER_SECONDS", 15))
        stop_event = threading.Event()

        def _loop() -> None:
            _update_status(running=True, last_error=None)
            logger.info(
                "inprocess.refresh.loop_start",
                extra={"interval_seconds": interval, "lookback_days": lookback, "jitter_seconds": jitter},
            )
            try:
                while not stop_event.is_set():
                    try:
                        from etl import incremental_refresh
                        with app.app_context():
                            result = incremental_refresh.refresh_once(
                                lookback_days=lookback,
                                require_lock=True,
                            )
                        if isinstance(result, dict):
                            status = result.get("status")
                            if status == "locked" or not result.get("dataset_version"):
                                manifest = watermark_store.read_manifest()
                                _update_from_manifest(manifest, status=status)
                            else:
                                _update_from_manifest(result, status=status)
                        else:
                            manifest = watermark_store.read_manifest()
                            _update_from_manifest(manifest, status="unknown")
                        logger.info(
                            "inprocess.refresh.complete",
                            extra={
                                "status": result.get("status") if isinstance(result, dict) else "unknown",
                                "dataset_version": result.get("dataset_version") if isinstance(result, dict) else None,
                            },
                        )
                    except Exception as exc:
                        logger.exception("inprocess.refresh.failed")
                        _update_status(last_error=str(exc), last_status="error")
                    sleep_for = interval + (random.randint(0, jitter) if jitter else 0)
                    _sleep_with_stop(stop_event, sleep_for)
            finally:
                _update_status(running=False)
                logger.info("inprocess.refresh.loop_stop")

        thread = threading.Thread(target=_loop, name="inprocess-fact-refresh", daemon=True)
        handle = RefreshHandle(
            thread=thread,
            stop_event=stop_event,
            interval_seconds=interval,
            lookback_days=lookback,
            jitter_seconds=jitter,
        )
        _HANDLE = handle
        thread.start()
        return handle
