from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from threading import RLock
from typing import Any, Dict, Mapping

from cachetools import TTLCache


_EXPORT_CACHE = TTLCache(maxsize=256, ttl=60 * 30)
_LOCK = RLock()


@dataclass
class ExportArtifact:
    export_id: str
    user_id: str
    filename: str
    content_type: str
    created_at: float
    expires_at: float
    data: bytes
    meta: Dict[str, Any]


def create_export(
    user_id: Any,
    *,
    filename: str,
    data: bytes,
    content_type: str = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    meta: Mapping[str, Any] | None = None,
) -> ExportArtifact:
    now = time.time()
    export_id = f"ax_{uuid.uuid4().hex[:20]}"
    ttl = getattr(_EXPORT_CACHE, "ttl", 60 * 30)
    artifact = ExportArtifact(
        export_id=export_id,
        user_id=str(user_id or "anon"),
        filename=str(filename or "assistant_export.xlsx"),
        content_type=str(content_type or "application/octet-stream"),
        created_at=now,
        expires_at=now + float(ttl),
        data=bytes(data or b""),
        meta=dict(meta or {}),
    )
    with _LOCK:
        _EXPORT_CACHE[export_id] = artifact
    return artifact


def get_export(user_id: Any, export_id: str) -> ExportArtifact | None:
    token = str(export_id or "").strip()
    if not token:
        return None
    with _LOCK:
        artifact = _EXPORT_CACHE.get(token)
    if not isinstance(artifact, ExportArtifact):
        return None
    if artifact.user_id != str(user_id or "anon"):
        return None
    return artifact


def clear_export(user_id: Any, export_id: str) -> None:
    artifact = get_export(user_id, export_id)
    if artifact is None:
        return
    with _LOCK:
        _EXPORT_CACHE.pop(export_id, None)
