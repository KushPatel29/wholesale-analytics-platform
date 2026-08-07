from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Callable, Dict, Mapping

from cachetools import TTLCache


def _int_env(name: str, default: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except Exception:
        return int(default)


_JOB_TTL_SECONDS = max(300, _int_env("ASSISTANT_EXPORT_JOB_TTL_SECONDS", 60 * 30))
_JOBS = TTLCache(maxsize=max(128, _int_env("ASSISTANT_EXPORT_JOB_CACHE_SIZE", 1024)), ttl=_JOB_TTL_SECONDS)
_REQUEST_DEDUPE = TTLCache(maxsize=max(128, _int_env("ASSISTANT_EXPORT_JOB_DEDUPE_SIZE", 2048)), ttl=max(10, _int_env("ASSISTANT_EXPORT_JOB_DEDUPE_SECONDS", 45)))
_RATE_COUNTER = TTLCache(maxsize=max(64, _int_env("ASSISTANT_EXPORT_RATE_CACHE_SIZE", 2048)), ttl=60)
_LOCK = RLock()
_EXECUTOR = ThreadPoolExecutor(max_workers=max(1, _int_env("ASSISTANT_EXPORT_JOB_WORKERS", 2)), thread_name_prefix="assistant-export")


@dataclass
class ExportJob:
    job_id: str
    user_id: str
    request_key: str
    status: str
    created_at: float
    updated_at: float
    expires_at: float
    submitted_at: float
    started_at: float | None = None
    finished_at: float | None = None
    export_id: str | None = None
    filename: str | None = None
    content_type: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


def _normalize_user(user_id: Any) -> str:
    return str(user_id or "anon")


def _normalize_key(request_key: str) -> str:
    token = str(request_key or "").strip().lower()
    return token or f"anon:{uuid.uuid4().hex[:8]}"


def _active_job_count_for_user(user_id: str) -> int:
    count = 0
    for job in list(_JOBS.values()):
        if not isinstance(job, ExportJob):
            continue
        if job.user_id == user_id and str(job.status) in {"pending", "running"}:
            count += 1
    return count


def _job_from_result(job: ExportJob, result: Any) -> None:
    if isinstance(result, Mapping):
        job.export_id = str(result.get("export_id") or "").strip() or job.export_id
        filename = str(result.get("filename") or "").strip()
        if filename:
            job.filename = filename
        content_type = str(result.get("content_type") or "").strip()
        if content_type:
            job.content_type = content_type
        meta = result.get("meta")
        if isinstance(meta, Mapping):
            job.meta.update(dict(meta))
        return
    export_id = str(getattr(result, "export_id", "") or "").strip()
    if export_id:
        job.export_id = export_id
    filename = str(getattr(result, "filename", "") or "").strip()
    if filename:
        job.filename = filename
    content_type = str(getattr(result, "content_type", "") or "").strip()
    if content_type:
        job.content_type = content_type
    meta = getattr(result, "meta", None)
    if isinstance(meta, Mapping):
        job.meta.update(dict(meta))


def _run_job(job_id: str, task: Callable[[], Any]) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not isinstance(job, ExportJob):
            return
        job.status = "running"
        job.started_at = time.time()
        job.updated_at = job.started_at
        _JOBS[job_id] = job
    try:
        result = task()
        with _LOCK:
            current = _JOBS.get(job_id)
            if not isinstance(current, ExportJob):
                return
            _job_from_result(current, result)
            current.status = "completed"
            current.finished_at = time.time()
            current.updated_at = current.finished_at
            _JOBS[job_id] = current
    except Exception as exc:
        with _LOCK:
            current = _JOBS.get(job_id)
            if not isinstance(current, ExportJob):
                return
            current.status = "error"
            current.finished_at = time.time()
            current.updated_at = current.finished_at
            current.error = str(exc)
            _JOBS[job_id] = current


def enqueue_export_job(
    user_id: Any,
    *,
    request_key: str,
    task: Callable[[], Any],
    meta: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    user_token = _normalize_user(user_id)
    request_token = _normalize_key(request_key)
    now = time.time()
    max_per_minute = max(1, _int_env("ASSISTANT_EXPORT_JOB_MAX_PER_MINUTE", 8))
    max_active = max(1, _int_env("ASSISTANT_EXPORT_JOB_MAX_ACTIVE_PER_USER", 3))
    with _LOCK:
        rate_key = f"{user_token}:count"
        used = int(_RATE_COUNTER.get(rate_key, 0))
        if used >= max_per_minute:
            return {"status": "rate_limited", "retry_after_seconds": 60}
        active = _active_job_count_for_user(user_token)
        if active >= max_active:
            return {"status": "busy", "retry_after_seconds": 10}
        dedupe_key = f"{user_token}:{request_token}"
        existing_id = _REQUEST_DEDUPE.get(dedupe_key)
        existing = _JOBS.get(existing_id) if existing_id else None
        if isinstance(existing, ExportJob) and str(existing.status) in {"pending", "running", "completed"}:
            return {"status": "deduped", "job": existing}

        job_id = f"aj_{uuid.uuid4().hex[:20]}"
        job = ExportJob(
            job_id=job_id,
            user_id=user_token,
            request_key=request_token,
            status="pending",
            created_at=now,
            updated_at=now,
            expires_at=now + float(_JOB_TTL_SECONDS),
            submitted_at=now,
            notes=[],
            meta=dict(meta or {}),
        )
        _JOBS[job_id] = job
        _REQUEST_DEDUPE[dedupe_key] = job_id
        _RATE_COUNTER[rate_key] = used + 1

    _EXECUTOR.submit(_run_job, job_id, task)
    return {"status": "queued", "job": job}


def get_export_job(user_id: Any, job_id: str) -> ExportJob | None:
    token = str(job_id or "").strip()
    if not token:
        return None
    user_token = _normalize_user(user_id)
    with _LOCK:
        job = _JOBS.get(token)
    if not isinstance(job, ExportJob):
        return None
    if job.user_id != user_token:
        return None
    return job


def export_job_payload(job: ExportJob) -> Dict[str, Any]:
    return {
        "job_id": job.job_id,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "expires_at": job.expires_at,
        "export_id": job.export_id,
        "filename": job.filename,
        "content_type": job.content_type,
        "error": job.error,
        "notes": list(job.notes or []),
        "meta": dict(job.meta or {}),
    }

