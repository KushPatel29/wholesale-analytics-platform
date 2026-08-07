from __future__ import annotations

from queue import Queue
from threading import Lock
from typing import Any, Dict, List


EventDict = Dict[str, Any]
SubscriberQueue = Queue[EventDict]

_subscribers: List[SubscriberQueue] = []
_lock = Lock()


def subscribe() -> SubscriberQueue:
    """Register a new subscriber queue for server-sent events."""

    queue: SubscriberQueue = Queue()
    with _lock:
        _subscribers.append(queue)
    return queue


def unsubscribe(queue: SubscriberQueue) -> None:
    """Remove the subscriber queue when a client disconnects."""

    with _lock:
        if queue in _subscribers:
            _subscribers.remove(queue)


def publish(event: EventDict) -> None:
    """Broadcast an event dictionary to all subscribers."""

    with _lock:
        targets = list(_subscribers)

    for queue in targets:
        try:
            queue.put_nowait(dict(event))
        except Exception:
            # If a subscriber queue is misbehaving, drop the event for that queue.
            pass

