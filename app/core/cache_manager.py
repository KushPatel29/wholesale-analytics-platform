from __future__ import annotations

import os
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

from app.services.cache_manager import CacheManager as _DatasetCacheManager

CacheManager = _DatasetCacheManager

# Canonical cache + dataset locations derived from environment.
_DATASET_HINT = os.getenv("FACT_DATASET_PATH") or os.getenv("PARQUET_PATH")
DEFAULT_DATASET_PATH = Path(_DATASET_HINT or (Path("cache") / "fact_dataset")).expanduser().resolve()
CACHE_DIR = Path(os.getenv("CACHE_DIR", DEFAULT_DATASET_PATH.parent.as_posix())).expanduser().resolve()
PARQUET_PATH = DEFAULT_DATASET_PATH

_SINGLETON_LOCK = threading.RLock()
_CACHE_SINGLETON: CacheManager | None = None


def get_cache_manager() -> CacheManager:
    """Return the process-wide CacheManager singleton (backed by the legacy implementation)."""
    global _CACHE_SINGLETON
    with _SINGLETON_LOCK:
        if _CACHE_SINGLETON is None:
            dataset_name = PARQUET_PATH.stem if PARQUET_PATH.suffix else PARQUET_PATH.name
            _CACHE_SINGLETON = CacheManager(dataset=dataset_name, cache_dir=PARQUET_PATH.parent)
        return _CACHE_SINGLETON


CACHE_MANAGER: CacheManager = get_cache_manager()


class TTLValueCache:
    """Lightweight TTL cache with single-flight protection."""

    def __init__(self, maxsize: int = 128) -> None:
        self._store: Dict[str, Tuple[float, Any]] = {}
        self._lock = threading.RLock()
        self._inflight: Dict[str, Future] = {}
        self._maxsize = maxsize

    def _evict_if_needed(self) -> None:
        if len(self._store) <= self._maxsize:
            return
        # Drop the stalest entry to stay within bounds
        oldest_key = None
        oldest_expiry = float("inf")
        for key, (exp, _) in self._store.items():
            if exp < oldest_expiry:
                oldest_expiry = exp
                oldest_key = key
        if oldest_key:
            self._store.pop(oldest_key, None)

    def get(self, key: str) -> Any | None:
        now = time.time()
        with self._lock:
            payload = self._store.get(key)
            if not payload:
                return None
            expiry, value = payload
            if expiry < now:
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            self._store[key] = (time.time() + float(ttl), value)
            self._evict_if_needed()

    def get_or_compute(self, key: str, ttl: float, builder: Callable[[], Any]) -> tuple[Any, bool]:
        """
        Return cached value if present and fresh, otherwise compute with single-flight.
        Returns (value, cache_hit_flag).
        """
        now = time.time()
        with self._lock:
            payload = self._store.get(key)
            if payload:
                expiry, value = payload
                if expiry >= now:
                    return value, True
                self._store.pop(key, None)
            inflight = self._inflight.get(key)
            if inflight:
                return inflight.result(), True
            fut: Future = Future()
            self._inflight[key] = fut

        try:
            value = builder()
            with self._lock:
                self._store[key] = (time.time() + float(ttl), value)
                fut.set_result(value)
                self._evict_if_needed()
                self._inflight.pop(key, None)
            return value, False
        except Exception as exc:  # pragma: no cover - defensive
            with self._lock:
                try:
                    fut.set_exception(exc)
                finally:
                    self._inflight.pop(key, None)
            raise

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._inflight.clear()
