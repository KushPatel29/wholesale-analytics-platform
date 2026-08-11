from __future__ import annotations

import threading
import time
from concurrent.futures import Future

from app.core.cache_manager import TTLValueCache


def test_single_flight_waiter_does_not_hold_cache_lock() -> None:
    cache = TTLValueCache()
    inflight: Future[str] = Future()
    with cache._lock:
        cache._inflight["shared"] = inflight

    result: list[tuple[str, bool]] = []

    def wait_for_builder() -> None:
        result.append(cache.get_or_compute("shared", 60, lambda: "unexpected"))

    waiter = threading.Thread(target=wait_for_builder, daemon=True)
    waiter.start()

    # Give the waiter a chance to observe the in-flight value. A correct
    # waiter blocks on the Future only; unrelated cache operations can still
    # acquire the lock while the builder is working.
    time.sleep(0.05)
    acquired = cache._lock.acquire(timeout=0.5)
    if acquired:
        cache._lock.release()

    inflight.set_result("built")
    waiter.join(timeout=1)

    assert acquired is True
    assert waiter.is_alive() is False
    assert result == [("built", True)]


def test_single_flight_builder_can_publish_to_waiters() -> None:
    cache = TTLValueCache()
    builder_started = threading.Event()
    release_builder = threading.Event()
    results: list[tuple[str, bool]] = []

    def build() -> str:
        builder_started.set()
        assert release_builder.wait(timeout=1)
        return "shared-value"

    def read() -> None:
        results.append(cache.get_or_compute("shared", 60, build))

    builder = threading.Thread(target=read, daemon=True)
    waiter = threading.Thread(target=read, daemon=True)
    builder.start()
    assert builder_started.wait(timeout=1)
    waiter.start()
    time.sleep(0.05)
    release_builder.set()
    builder.join(timeout=1)
    waiter.join(timeout=1)

    assert builder.is_alive() is False
    assert waiter.is_alive() is False
    assert sorted(results, key=lambda item: item[1]) == [
        ("shared-value", False),
        ("shared-value", True),
    ]
