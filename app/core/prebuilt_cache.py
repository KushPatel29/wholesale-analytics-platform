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
import hashlib
import os
import pickle
import threading
from pathlib import Path
from typing import Any


_FORMAT_VERSION = 1
_TRUE_VALUES = {"1", "true", "yes", "on"}

# Modules whose output these files are. The cache key is built from the dataset
# and the filters, so it says nothing about the code that produced the value -
# which means editing a bundle builder leaves every stale entry looking valid.
# That is not hypothetical: a fix to the suppliers revenue trend was invisible
# in the browser while this cache kept serving the pre-fix series.
#
# It is the whole services package, not just the files named `*_bundle.py`. A
# bundle is assembled from its neighbours - `planning.py` computes the planner
# payload, `formatting.py` renders the figures in it - and naming only the
# assemblers meant a change to the analysis itself was invisible. A widened fix
# to the planner's action list read as no change at all locally, because this
# fingerprint had not moved and the pre-fix payload was served straight back.
#
# The cost of the wider glob is cache misses after edits to service modules
# that no bundle reads. That is the safe direction to be wrong in.
_BUILDER_GLOBS = ("services/*.py",)
_build_id_cache: str | None = None
_runtime_ready_cache: bool | None = None
_runtime_ready_lock = threading.Lock()


def _enabled(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in _TRUE_VALUES


def build_id() -> str:
    """Fingerprint of the code that produces cached values.

    In the image this is the build's commit, set once and cheap. In a working
    tree there is no commit to trust, so fall back to the builders' modification
    times: editing one then invalidates its cached output automatically, which
    is the behaviour you want while changing a bundle.
    """
    global _build_id_cache
    if _build_id_cache is not None:
        return _build_id_cache

    explicit = str(os.getenv("APP_BUILD_ID", "") or os.getenv("GIT_COMMIT", "")).strip()
    if explicit:
        _build_id_cache = explicit[:40]
        return _build_id_cache

    app_root = Path(__file__).resolve().parent.parent
    stamps: list[str] = []
    try:
        for pattern in _BUILDER_GLOBS:
            for path in sorted(app_root.glob(pattern)):
                stamps.append(f"{path.name}:{int(path.stat().st_mtime)}")
    except OSError:
        stamps = []
    digest = hashlib.sha256("|".join(stamps).encode("utf-8")).hexdigest()[:16] if stamps else "unknown"
    _build_id_cache = digest
    return _build_id_cache


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
        if envelope.get("build") != build_id():
            # Built by different code; recomputing is correct and cheap
            # relative to serving a stale answer nobody can explain.
            return None
        return envelope.get("value")
    except (OSError, EOFError, pickle.PickleError, AttributeError, ValueError):
        return None


def runtime_ready() -> bool:
    """Load and validate the packaged filter artifacts before health is ready."""
    global _runtime_ready_cache
    if not _enabled("DEMO_PREBUILT_CACHE_READ"):
        return True
    if _runtime_ready_cache is True:
        return True
    with _runtime_ready_lock:
        if _runtime_ready_cache is True:
            return True
        root = _cache_dir()
        option_dir = root / "filter-options" if root is not None else None
        files = sorted(option_dir.glob("*.pickle.gz")) if option_dir and option_dir.is_dir() else []
        # Reading through `load` verifies the trusted envelope, key, format and
        # builder fingerprint. The payload is small, and the immutable image
        # means this check only has to pass once per process.
        ready = bool(files) and all(
            load("filter-options", path.name.removesuffix(".pickle.gz")) is not None for path in files
        )
        if ready:
            _runtime_ready_cache = True
        return ready


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
                {"format": _FORMAT_VERSION, "key": key, "build": build_id(), "value": value},
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
