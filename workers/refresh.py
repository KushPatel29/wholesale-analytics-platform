from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

import uuid

from app.services import event_bus, watermark_store

LOGGER = logging.getLogger("workers.refresh")
DATASET_PATH = watermark_store.resolve_dataset_path()
LOCK_PATH = DATASET_PATH / ".refresh.lock"

_scheduler: Optional[BackgroundScheduler] = None
_scheduler_lock = threading.Lock()


class FileLock:
    """Lightweight cross-platform advisory lock using a single-byte file lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Optional[IO[bytes]] = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.touch(exist_ok=True)
        except Exception:
            pass
        fh = open(self.path, "r+b")
        try:
            size = fh.seek(0, os.SEEK_END)
            if size == 0:
                fh.write(b"0")
                fh.flush()
            fh.seek(0)
            if os.name == "nt":
                import msvcrt  # type: ignore

                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    fh.close()
                    return False
            else:
                import fcntl  # type: ignore

                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    fh.close()
                    return False
        except Exception:
            fh.close()
            raise
        self._fh = fh
        return True

    def release(self) -> None:
        fh = self._fh
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt  # type: ignore

                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl  # type: ignore

                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                fh.close()
            finally:
                self._fh = None


@contextmanager
def _acquire_lock(path: Path):
    lock = FileLock(path)
    acquired = False
    try:
        acquired = lock.acquire()
        yield acquired
    finally:
        if acquired:
            lock.release()


def refresh_job() -> None:
    """Run the parquet refresh in the background, guarding with a lock."""

    with _acquire_lock(LOCK_PATH) as acquired:
        if not acquired:
            LOGGER.info("Refresh already running; skipping this trigger.")
            return
        try:
            from app.services import fact_store

            LOGGER.info("Refreshing analytics cache via fact_store.refresh_once().")
            meta = fact_store.refresh_once(start_date=os.getenv("INITIAL_START_DATE", "2017-01-01"))
            LOGGER.info(
                "Refresh completed successfully. status=%s rows=%s watermark=%s",
                meta.get("status"),
                meta.get("row_count"),
                meta.get("watermark") or meta.get("watermark_dt"),
            )
            try:
                event_bus.publish(
                    {
                        "type": "data_refresh",
                        "version": meta.get("dataset_version") or meta.get("watermark") or meta.get("last_refresh_utc"),
                        "built_at": meta.get("built_at_utc") or meta.get("last_refresh_utc"),
                    }
                )
            except Exception:
                LOGGER.exception("Failed to publish data_refresh event.")
        except Exception:
            LOGGER.exception("Background refresh failed.")


def start_refresh_scheduler() -> Optional[BackgroundScheduler]:
    """Start the APScheduler background scheduler if not already running."""

    global _scheduler
    with _scheduler_lock:
        if _scheduler and _scheduler.running:
            return _scheduler

        interval_min = _refresh_interval_minutes()
        if interval_min <= 0:
            LOGGER.info("Refresh scheduler disabled; REFRESH_EVERY_MIN=%s", interval_min)
            return None

        scheduler = BackgroundScheduler(timezone=timezone.utc)
        trigger = IntervalTrigger(minutes=interval_min, timezone=timezone.utc)
        first_run = _initial_run_time(interval_min)
        scheduler.add_job(
            refresh_job,
            trigger=trigger,
            id="analytics-refresh",
            name="refresh_parquet",
            max_instances=1,
            coalesce=True,
            next_run_time=first_run,
        )
        scheduler.start()
        _scheduler = scheduler
        LOGGER.info(
            "Started refresh scheduler: interval=%s min startup_immediate=%s",
            interval_min,
            first_run <= datetime.now(timezone.utc) if first_run else False,
        )
        return scheduler


def enqueue_refresh() -> None:
    """Schedule an immediate background refresh, starting the scheduler if needed."""

    scheduler = start_refresh_scheduler()
    run_at = datetime.now(timezone.utc)

    if scheduler is None:
        LOGGER.info("Scheduler disabled; running refresh synchronously in a background thread.")
        threading.Thread(target=refresh_job, name="manual-refresh", daemon=True).start()
        return

    job_id = f"manual-refresh-{uuid.uuid4()}"
    try:
        scheduler.add_job(
            refresh_job,
            trigger='date',
            run_date=run_at,
            id=job_id,
            coalesce=True,
            replace_existing=False,
        )
        LOGGER.info("Manual refresh enqueued with job_id=%s", job_id)
    except Exception:
        LOGGER.exception("Failed to enqueue manual refresh job. Falling back to direct execution.")
        threading.Thread(target=refresh_job, name="manual-refresh-fallback", daemon=True).start()


def _refresh_interval_minutes(default: int = 15) -> int:
    value = os.getenv("REFRESH_EVERY_MIN")
    if value is None:
        return default
    try:
        parsed = int(value)
        if parsed <= 0:
            LOGGER.warning("REFRESH_EVERY_MIN=%s is not positive; disabling scheduler.", value)
            return 0
        return parsed
    except ValueError:
        LOGGER.warning("Invalid REFRESH_EVERY_MIN=%s; using default %s minutes.", value, default)
        return default


def _startup_fetch_enabled() -> bool:
    value = os.getenv("STARTUP_FETCH")
    if value is None:
        return True
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _initial_run_time(interval_min: int) -> datetime:
    now = datetime.now(timezone.utc)
    if _startup_fetch_enabled():
        return now
    return now + timedelta(minutes=interval_min)

