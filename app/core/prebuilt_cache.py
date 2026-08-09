"""Small, trusted on-disk cache for the self-contained demo image.

The hosted demo uses an immutable synthetic dataset.  Building its default
analytics during the Docker build is both faster for visitors and cheaper than
repeating the same pandas/DuckDB work whenever the free container wakes up.

Reading and writing are deliberately opt-in.  The files contain pickle data,
so they must only ever come from the application image itself; never point the
directory at an upload or another user-writable location.
"""

from __future__ import annotations

import gzip
import os
import pickle
from pathlib import Path
from typing import Any


_FORMAT_VERSION = 1
_TRUE_VALUES = {"1", "true", "yes", "on"}


def _enabled(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in _TRUE_VALUES


def _cache_dir() -> Path | None:
    raw = str(os.getenv("DEMO_PREBUILT_CACHE_DIR", "")).strip()
    return Path(raw).expanduser().resolve() if raw else None


def _path(namespace: str, key: str) -> Path | None:
    root = _cache_dir()
    if root is None:
        return None
    safe_namespace = "".join(ch for ch in namespace if ch.isalnum() or ch in {"-", "_"}) or "cache"
    safe_key = "".join(ch for ch in key if ch.isalnum())
    if not safe_key:
        return None
    return root / safe_namespace / f"{safe_key}.pickle.gz"


def load(namespace: str, key: str) -> Any | None:
    """Load an app-built value when the immutable demo cache is enabled."""
    if not _enabled("DEMO_PREBUILT_CACHE_READ"):
        return None
    path = _path(namespace, key)
    if path is None or not path.is_file():
        return None
    try:
        with gzip.open(path, "rb") as handle:
            envelope = pickle.load(handle)  # noqa: S301 - trusted image-only cache
        if not isinstance(envelope, dict):
            return None
        if envelope.get("format") != _FORMAT_VERSION or envelope.get("key") != key:
            return None
        return envelope.get("value")
    except (OSError, EOFError, pickle.PickleError, AttributeError, ValueError):
        return None


def save(namespace: str, key: str, value: Any) -> bool:
    """Atomically persist a value while the Docker build writer is enabled."""
    if not _enabled("DEMO_PREBUILT_CACHE_WRITE"):
        return False
    path = _path(namespace, key)
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        with gzip.open(temporary, "wb", compresslevel=5) as handle:
            pickle.dump(
                {"format": _FORMAT_VERSION, "key": key, "value": value},
                handle,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        os.replace(temporary, path)
        return True
    except (OSError, pickle.PickleError, AttributeError, TypeError, ValueError):
        try:
            temporary.unlink(missing_ok=True)
        except (OSError, UnboundLocalError):
            pass
        return False
