from __future__ import annotations

import gzip
import json
import logging
import math
import os
import re
import time
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional

import duckdb
import pandas as pd
from flask import current_app, has_app_context

from app.services import labor_state, labor_store
from app.services.synerion_client import (
    SynerionClient,
    SynerionConfigError,
    SynerionSettings,
)
from etl import partition_writer


logger = logging.getLogger(__name__)

DEFAULT_LABOR_START_DATE = date(2022, 1, 1)
WINDOW_DAYS = 31

TEXT_COLUMNS = [
    "labor_week",
    "labor_month",
    "weekday_name",
    "month_name",
    "employee_code",
    "employee_name",
    "payroll_code",
    "department_name",
    "department_number",
    "status",
    "work_rule",
    "attended_duration_raw",
    "time_category",
    "memo_amount_raw",
    "employee_key",
    "employee_day_key",
    "department_key",
    "employee_status_group",
    "source_row_hash",
]
DATE_COLUMNS = [
    "labor_date",
    "week_start",
    "source_partition_date",
    "source_window_start",
    "source_window_end",
]
TIMESTAMP_COLUMNS = [
    "labor_datetime",
    "shift_match_date",
    "schedule_start",
    "schedule_end",
    "first_in_punch_time",
    "last_out_punch_time",
    "paid_start_time",
    "paid_end_time",
    "source_loaded_at",
]
FLOAT_COLUMNS = [
    "attended_hours",
    "attended_hours_allocated",
    "paid_hours",
    "paid_hours_allocated",
    "transaction_duration",
    "transaction_duration_hours",
    "effective_rate",
    "labor_cost",
    "memo_amount",
    "schedule_hours",
    "schedule_hours_allocated",
    "punch_span_hours",
    "punch_span_hours_allocated",
    "paid_span_hours",
    "paid_span_hours_allocated",
    "blended_cost_per_paid_hour",
]
INT_COLUMNS = [
    "labor_year",
    "transaction_index",
    "employee_day_transaction_count",
    "active_employee_flag",
]
BOOL_COLUMNS = [
    "is_paid",
    "is_premium",
    "is_absence",
    "is_memo",
    "has_time_transaction",
    "primary_row_flag",
]
LABOR_FACT_COLUMNS = [
    "labor_date",
    "labor_datetime",
    "labor_week",
    "labor_month",
    "labor_year",
    "week_start",
    "weekday_name",
    "month_name",
    "employee_code",
    "employee_name",
    "payroll_code",
    "department_name",
    "department_number",
    "status",
    "work_rule",
    "shift_match_date",
    "schedule_start",
    "schedule_end",
    "first_in_punch_time",
    "last_out_punch_time",
    "paid_start_time",
    "paid_end_time",
    "attended_duration_raw",
    "attended_hours",
    "attended_hours_allocated",
    "paid_hours",
    "paid_hours_allocated",
    "time_category",
    "transaction_index",
    "transaction_duration",
    "transaction_duration_hours",
    "effective_rate",
    "labor_cost",
    "memo_amount_raw",
    "memo_amount",
    "is_paid",
    "is_premium",
    "is_absence",
    "is_memo",
    "has_time_transaction",
    "schedule_hours",
    "schedule_hours_allocated",
    "punch_span_hours",
    "punch_span_hours_allocated",
    "paid_span_hours",
    "paid_span_hours_allocated",
    "blended_cost_per_paid_hour",
    "employee_key",
    "employee_day_key",
    "department_key",
    "employee_status_group",
    "employee_day_transaction_count",
    "active_employee_flag",
    "primary_row_flag",
    "source_loaded_at",
    "source_partition_date",
    "source_row_hash",
    "source_window_start",
    "source_window_end",
]


def _cfg_value(key: str, default: Any = None) -> Any:
    env_value = os.getenv(key)
    if env_value not in (None, ""):
        return env_value
    if has_app_context():
        try:
            return current_app.config.get(key, default)
        except Exception:
            return os.getenv(key, default)
    return os.getenv(key, default)


def resolve_labor_dataset_path() -> Path:
    return labor_store.resolve_dataset_path()


def resolve_labor_raw_path() -> Path:
    raw = _cfg_value("LABOR_RAW_PATH", "cache/labor/raw")
    return Path(str(raw or "cache/labor/raw")).expanduser().resolve()


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_date(raw: Any, *, default: Optional[date] = None) -> Optional[date]:
    if raw is None or raw == "":
        return default
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip().lower()
    if text in {"today", "now"}:
        return _today_utc()
    ts = pd.to_datetime(raw, errors="coerce")
    if pd.isna(ts):
        return default
    return ts.date()


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    return text


_TIME_ONLY_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:\s?[APMapm]{2})?$")


def _normalize_timestamp(ts: Any) -> Optional[pd.Timestamp]:
    if ts is None or pd.isna(ts):
        return None
    try:
        normalized = pd.Timestamp(ts)
    except Exception:
        return None
    try:
        if getattr(normalized, "tzinfo", None) is not None:
            normalized = normalized.tz_convert(None)  # type: ignore[attr-defined]
    except Exception:
        try:
            normalized = normalized.tz_localize(None)  # type: ignore[attr-defined]
        except Exception:
            pass
    return normalized


def _time_only_timestamp(value: Any, *, anchor_date: Optional[date]) -> Optional[pd.Timestamp]:
    if anchor_date is None:
        return None
    text = _clean_text(value)
    if not text or not _TIME_ONLY_RE.match(text):
        return None
    for fmt in ("%H:%M", "%H:%M:%S", "%H:%M:%S.%f", "%I:%M %p", "%I:%M:%S %p", "%I:%M:%S.%f %p"):
        try:
            parsed_time = datetime.strptime(text.upper(), fmt).time()
            return pd.Timestamp(datetime.combine(anchor_date, parsed_time))
        except ValueError:
            continue
    return None


def _timestamp_or_none(value: Any, *, anchor_date: Optional[date] = None) -> Optional[pd.Timestamp]:
    if value is None or value == "":
        return None
    anchored = _time_only_timestamp(value, anchor_date=anchor_date)
    if anchored is not None:
        return anchored
    return _normalize_timestamp(pd.to_datetime(value, errors="coerce"))


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except Exception:
        return None
    if not math.isfinite(out):
        return None
    return out


def _normalize_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y", "t"}:
        return True
    if text in {"0", "false", "no", "off", "n", "f"}:
        return False
    return None


def _duration_hours(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, timedelta):
        return value.total_seconds() / 3600.0
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        numeric = float(value)
        if abs(numeric) > 36 and abs(numeric) <= 1440 and float(numeric).is_integer():
            return numeric / 60.0
        return numeric
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        try:
            sign = -1.0 if text.startswith("-") else 1.0
            clean = text[1:] if text.startswith("-") else text
            parts = clean.split(":")
            if len(parts) == 2:
                hours = float(parts[0])
                minutes = float(parts[1])
                seconds = 0.0
            elif len(parts) == 3:
                hours = float(parts[0])
                minutes = float(parts[1])
                seconds = float(parts[2])
            else:
                hours = minutes = seconds = 0.0
            return sign * (hours + (minutes / 60.0) + (seconds / 3600.0))
        except Exception:
            pass
    td = pd.to_timedelta(text, errors="coerce")
    if pd.notna(td):
        return td.total_seconds() / 3600.0
    return _float_or_none(text)


def _hours_between(start_ts: Optional[pd.Timestamp], end_ts: Optional[pd.Timestamp]) -> Optional[float]:
    if start_ts is None or end_ts is None:
        return None
    delta = end_ts - start_ts
    hours = delta.total_seconds() / 3600.0
    if hours < 0 and abs(hours) <= 24:
        hours += 24.0
    if hours < 0 or hours > 48:
        return None
    return hours


def _safe_date(ts: Optional[pd.Timestamp]) -> Optional[date]:
    return ts.date() if isinstance(ts, pd.Timestamp) else None


def _hash_key(*parts: Any) -> str:
    payload = json.dumps([part if part is not None else None for part in parts], default=str, sort_keys=False)
    return sha256(payload.encode("utf-8")).hexdigest()


def _employee_name(record: Mapping[str, Any]) -> str:
    first = _clean_text(record.get("FirstName")) or ""
    last = _clean_text(record.get("LastName")) or ""
    full = " ".join(part for part in (first, last) if part)
    return full or _clean_text(record.get("EmployeeCode")) or _clean_text(record.get("PayrollCode")) or "Unknown Employee"


def _status_group(status: Optional[str]) -> str:
    normalized = (status or "").strip().lower()
    if any(token in normalized for token in ("inactive", "terminated", "former", "leave")):
        return "inactive"
    return "active"


def _empty_labor_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=LABOR_FACT_COLUMNS)


def normalize_labor_records(
    records: Iterable[Mapping[str, Any]],
    *,
    loaded_at: str | datetime,
    window_start: date,
    window_end: date,
) -> pd.DataFrame:
    loaded_ts = _timestamp_or_none(loaded_at) or pd.Timestamp.utcnow().tz_localize(None)
    rows: list[dict[str, Any]] = []

    for record in records:
        if not isinstance(record, Mapping):
            continue
        employee_code = _clean_text(record.get("EmployeeCode"))
        payroll_code = _clean_text(record.get("PayrollCode"))
        employee_name = _employee_name(record)
        department_name = _clean_text(record.get("DepartmentName")) or "Unassigned"
        department_number = _clean_text(record.get("DepartmentNumber"))
        status = _clean_text(record.get("Status"))
        work_rule = _clean_text(record.get("WorkRule"))
        shift_match_ts = _timestamp_or_none(record.get("ShiftMatchDate"))
        anchor_date = _safe_date(shift_match_ts)
        schedule_start = _timestamp_or_none(record.get("ScheduleStart"), anchor_date=anchor_date)
        schedule_end = _timestamp_or_none(record.get("ScheduleEnd"), anchor_date=anchor_date)
        first_in_punch_time = _timestamp_or_none(record.get("FirstInPunchTime"), anchor_date=anchor_date)
        last_out_punch_time = _timestamp_or_none(record.get("LastOutPunchTime"), anchor_date=anchor_date)
        paid_start_time = _timestamp_or_none(record.get("PaidStartTime"), anchor_date=anchor_date)
        paid_end_time = _timestamp_or_none(record.get("PaidEndTime"), anchor_date=anchor_date)
        labor_date = (
            _safe_date(shift_match_ts)
            or _safe_date(paid_start_time)
            or _safe_date(first_in_punch_time)
            or _safe_date(schedule_start)
        )
        if labor_date is None:
            continue
        week_start = labor_date - timedelta(days=labor_date.weekday())
        attended_duration_raw = _clean_text(record.get("AttendedDuration"))
        attended_hours = _duration_hours(record.get("AttendedDuration"))
        paid_hours = _float_or_none(record.get("PaidHours"))
        schedule_hours = _hours_between(schedule_start, schedule_end)
        punch_span_hours = _hours_between(first_in_punch_time, last_out_punch_time)
        paid_span_hours = _hours_between(paid_start_time, paid_end_time)
        labor_datetime = paid_start_time or first_in_punch_time or schedule_start or shift_match_ts
        employee_key = _hash_key(employee_code or payroll_code or employee_name)
        department_key = _hash_key(department_number or "", department_name)
        employee_day_key = _hash_key(
            employee_key,
            labor_date.isoformat(),
            department_key,
            _clean_text(status) or "",
            _clean_text(work_rule) or "",
        )
        transactions = record.get("TimeTransactions")
        normalized_transactions = list(transactions) if isinstance(transactions, list) and transactions else [None]
        transaction_count = max(1, len(normalized_transactions))
        paid_hours_allocated = (paid_hours / transaction_count) if paid_hours is not None else None
        attended_hours_allocated = (attended_hours / transaction_count) if attended_hours is not None else None
        schedule_hours_allocated = (schedule_hours / transaction_count) if schedule_hours is not None else None
        punch_span_hours_allocated = (punch_span_hours / transaction_count) if punch_span_hours is not None else None
        paid_span_hours_allocated = (paid_span_hours / transaction_count) if paid_span_hours is not None else None

        for index, raw_transaction in enumerate(normalized_transactions, start=1):
            transaction = raw_transaction if isinstance(raw_transaction, Mapping) else {}
            time_category = _clean_text(transaction.get("TimeCategory")) or "Unclassified"
            duration_raw = transaction.get("Duration")
            duration_numeric = _float_or_none(duration_raw)
            duration_hours = _duration_hours(duration_raw)
            memo_amount_raw = _clean_text(transaction.get("MemoAmount"))
            memo_amount = _float_or_none(transaction.get("MemoAmount"))
            labor_cost = _float_or_none(transaction.get("DollarAmount")) or 0.0
            blended_rate = None
            if labor_cost is not None and paid_hours_allocated not in (None, 0):
                blended_rate = labor_cost / float(paid_hours_allocated)
            source_row_hash = _hash_key(
                employee_day_key,
                index,
                time_category,
                duration_raw,
                memo_amount_raw,
                transaction.get("EffectiveRate"),
                transaction.get("DollarAmount"),
                transaction.get("IsPaid"),
                transaction.get("IsPremium"),
                transaction.get("IsAbsence"),
                transaction.get("IsMemo"),
            )
            rows.append(
                {
                    "labor_date": labor_date,
                    "labor_datetime": labor_datetime,
                    "labor_week": f"{week_start.isocalendar().year}-W{week_start.isocalendar().week:02d}",
                    "labor_month": labor_date.strftime("%Y-%m"),
                    "labor_year": labor_date.year,
                    "week_start": week_start,
                    "weekday_name": labor_date.strftime("%A"),
                    "month_name": labor_date.strftime("%B"),
                    "employee_code": employee_code,
                    "employee_name": employee_name,
                    "payroll_code": payroll_code,
                    "department_name": department_name,
                    "department_number": department_number,
                    "status": status,
                    "work_rule": work_rule,
                    "shift_match_date": shift_match_ts,
                    "schedule_start": schedule_start,
                    "schedule_end": schedule_end,
                    "first_in_punch_time": first_in_punch_time,
                    "last_out_punch_time": last_out_punch_time,
                    "paid_start_time": paid_start_time,
                    "paid_end_time": paid_end_time,
                    "attended_duration_raw": attended_duration_raw,
                    "attended_hours": attended_hours,
                    "attended_hours_allocated": attended_hours_allocated,
                    "paid_hours": paid_hours,
                    "paid_hours_allocated": paid_hours_allocated,
                    "time_category": time_category,
                    "transaction_index": index,
                    "transaction_duration": duration_numeric,
                    "transaction_duration_hours": duration_hours,
                    "effective_rate": _float_or_none(transaction.get("EffectiveRate")),
                    "labor_cost": labor_cost,
                    "memo_amount_raw": memo_amount_raw,
                    "memo_amount": memo_amount,
                    "is_paid": _normalize_bool(transaction.get("IsPaid")),
                    "is_premium": _normalize_bool(transaction.get("IsPremium")),
                    "is_absence": _normalize_bool(transaction.get("IsAbsence")),
                    "is_memo": _normalize_bool(transaction.get("IsMemo")),
                    "has_time_transaction": bool(raw_transaction),
                    "schedule_hours": schedule_hours,
                    "schedule_hours_allocated": schedule_hours_allocated,
                    "punch_span_hours": punch_span_hours,
                    "punch_span_hours_allocated": punch_span_hours_allocated,
                    "paid_span_hours": paid_span_hours,
                    "paid_span_hours_allocated": paid_span_hours_allocated,
                    "blended_cost_per_paid_hour": blended_rate,
                    "employee_key": employee_key,
                    "employee_day_key": employee_day_key,
                    "department_key": department_key,
                    "employee_status_group": _status_group(status),
                    "employee_day_transaction_count": transaction_count,
                    "active_employee_flag": 1 if index == 1 else 0,
                    "primary_row_flag": index == 1,
                    "source_loaded_at": loaded_ts,
                    "source_partition_date": labor_date,
                    "source_row_hash": source_row_hash,
                    "source_window_start": window_start,
                    "source_window_end": window_end,
                }
            )

    if not rows:
        return _empty_labor_frame()

    df = pd.DataFrame.from_records(rows)
    df = df.reindex(columns=LABOR_FACT_COLUMNS)
    df = df.drop_duplicates(subset=["source_row_hash"], keep="last")
    for col in TEXT_COLUMNS:
        if col in df.columns:
            df[col] = df[col].astype("string")
    for col in DATE_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
    for col in TIMESTAMP_COLUMNS:
        if col in df.columns:
            ts = pd.to_datetime(df[col], errors="coerce")
            try:
                ts = ts.dt.tz_localize(None)
            except Exception:
                pass
            df[col] = ts
    for col in FLOAT_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in INT_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    for col in BOOL_COLUMNS:
        if col in df.columns:
            df[col] = df[col].astype("boolean")
    return df


class RawWindowWriter:
    def __init__(
        self,
        raw_root: Path,
        *,
        window_start: date,
        window_end: date,
        loaded_at: str,
    ) -> None:
        month_root = raw_root / f"year={window_start.year}" / f"month={window_start.month:02d}"
        month_root.mkdir(parents=True, exist_ok=True)
        stamp = loaded_at.replace(":", "").replace("-", "").replace("+", "").replace(".", "")
        self.path = month_root / f"labor_{window_start.isoformat()}_{window_end.isoformat()}_{stamp}.jsonl.gz"
        self._fh = None
        self.window_start = window_start
        self.window_end = window_end
        self.loaded_at = loaded_at

    def __enter__(self) -> "RawWindowWriter":
        self._fh = gzip.open(self.path, mode="wt", encoding="utf-8")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def write_page(self, page: int, rows: Iterable[Mapping[str, Any]]) -> None:
        if self._fh is None:
            return
        for index, record in enumerate(rows, start=1):
            payload = {
                "page": int(page),
                "record_index": int(index),
                "window_start": self.window_start.isoformat(),
                "window_end": self.window_end.isoformat(),
                "loaded_at": self.loaded_at,
                "record": record,
            }
            self._fh.write(json.dumps(payload, default=str))
            self._fh.write("\n")


def _iter_windows(start_date: date, end_date: date) -> Iterator[tuple[date, date]]:
    cursor = start_date
    while cursor <= end_date:
        window_end = min(end_date, cursor + timedelta(days=WINDOW_DAYS - 1))
        yield cursor, window_end
        cursor = window_end + timedelta(days=1)


def _resolve_window(mode: str, *, start: Any = None, end: Any = None, state: Optional[Mapping[str, Any]] = None) -> tuple[date, date]:
    labor_start = _parse_date(_cfg_value("LABOR_START_DATE", DEFAULT_LABOR_START_DATE.isoformat()), default=DEFAULT_LABOR_START_DATE)
    if labor_start is None:
        labor_start = DEFAULT_LABOR_START_DATE
    end_date = _parse_date(end, default=_today_utc()) or _today_utc()
    incremental_days = max(1, int(_cfg_value("LABOR_INCREMENTAL_DAYS", 7) or 7))
    recent_reload_days = max(incremental_days, int(_cfg_value("LABOR_RECENT_RELOAD_DAYS", 45) or 45))
    if mode == "backfill":
        start_date = _parse_date(start, default=labor_start) or labor_start
    elif mode == "recent-repair":
        start_date = max(labor_start, end_date - timedelta(days=recent_reload_days - 1))
    else:
        watermark = ((state or {}).get("watermark") or {}).get("labor_date_max")
        watermark_date = _parse_date(watermark)
        if watermark_date is not None:
            start_date = max(
                labor_start,
                min(watermark_date, end_date) - timedelta(days=recent_reload_days - 1),
                end_date - timedelta(days=incremental_days - 1),
            )
        else:
            start_date = max(labor_start, end_date - timedelta(days=recent_reload_days - 1))
    if start is not None and mode != "recent-repair":
        explicit_start = _parse_date(start)
        if explicit_start is not None:
            start_date = max(labor_start, explicit_start)
    if start_date > end_date:
        start_date = end_date
    return start_date, end_date


def _fetch_window_records(
    client: SynerionClient,
    *,
    window_start: date,
    window_end: date,
    loaded_at: str,
    write_raw: bool,
) -> list[dict[str, Any]]:
    raw_root = resolve_labor_raw_path()
    writer_ctx = RawWindowWriter(raw_root, window_start=window_start, window_end=window_end, loaded_at=loaded_at) if write_raw else nullcontext()
    with writer_ctx as writer:
        handler = writer.write_page if isinstance(writer, RawWindowWriter) else None
        return list(
            client.iter_time_transactions(
                start_date=window_start,
                end_date=window_end,
                per_page=int(_cfg_value("SYNERION_PER_PAGE", 100) or 100),
                raw_page_handler=handler,
            )
        )


def _recompute_manifest_bounds(dataset_path: Path) -> dict[str, Any]:
    manifest = labor_store.read_manifest(dataset_path)
    if not dataset_path.exists() or not any(dataset_path.rglob("*.parquet")):
        manifest["row_count"] = 0
        manifest["rows"] = 0
        manifest["min_date"] = None
        manifest["max_date"] = None
        manifest["last_refresh_utc"] = manifest.get("last_refresh_utc") or _now_utc_iso()
        out_path = labor_store.manifest_path(dataset_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.parent / f".{out_path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
        tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str), encoding="utf-8")
        os.replace(tmp, out_path)
        return manifest
    pattern = (dataset_path / "**" / "*.parquet").as_posix().replace("'", "''")
    conn = duckdb.connect()
    try:
        row = conn.execute(
            f"SELECT COUNT(*) AS rows, MIN(CAST(labor_date AS DATE)) AS min_date, MAX(CAST(labor_date AS DATE)) AS max_date "
            f"FROM read_parquet('{pattern}', union_by_name=true)"
        ).fetchone()
    finally:
        conn.close()
    manifest["row_count"] = int(row[0] or 0) if row else 0
    manifest["rows"] = manifest["row_count"]
    manifest["min_date"] = row[1].isoformat() if row and row[1] is not None else None
    manifest["max_date"] = row[2].isoformat() if row and row[2] is not None else None
    manifest["min_dateexpected"] = manifest["min_date"]
    manifest["max_dateexpected"] = manifest["max_date"]
    out_path = labor_store.manifest_path(dataset_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.parent / f".{out_path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}"
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str), encoding="utf-8")
    os.replace(tmp, out_path)
    return manifest


def refresh_labor(
    *,
    mode: str = "incremental",
    start: Any = None,
    end: Any = None,
    keep_prev: bool = False,
    write_raw: bool = True,
) -> dict[str, Any]:
    settings = SynerionSettings.from_env(current_app.config if has_app_context() else None)
    settings.validate()
    dataset_path = resolve_labor_dataset_path()
    state = labor_state.load_state(dataset_path=dataset_path)
    window_start, window_end = _resolve_window(mode, start=start, end=end, state=state)
    loaded_at = _now_utc_iso()
    client = SynerionClient(settings)
    manifest = labor_store.read_manifest(dataset_path)
    total_source_rows = 0
    total_rows = 0
    processed_windows = 0
    latest_manifest = manifest

    try:
        for index, (chunk_start, chunk_end) in enumerate(_iter_windows(window_start, window_end), start=1):
            source_records = _fetch_window_records(
                client,
                window_start=chunk_start,
                window_end=chunk_end,
                loaded_at=loaded_at,
                write_raw=write_raw,
            )
            normalized = normalize_labor_records(
                source_records,
                loaded_at=loaded_at,
                window_start=chunk_start,
                window_end=chunk_end,
            )
            total_source_rows += len(source_records)
            total_rows += int(len(normalized))
            processed_windows += 1
            manifest_updates = {
                "dataset_type": "labor",
                "source_system": "synerion",
                "date_column": "labor_date",
                "primary_key": "source_row_hash",
                "labor_start_date": str(_cfg_value("LABOR_START_DATE", DEFAULT_LABOR_START_DATE.isoformat())),
                "last_refresh_utc": loaded_at,
                "built_at_utc": loaded_at,
                "refresh_mode": mode,
                "source_window_start": chunk_start.isoformat(),
                "source_window_end": chunk_end.isoformat(),
            }
            latest_manifest = partition_writer.upsert_dataset(
                normalized,
                dataset_path=dataset_path,
                pk_col="source_row_hash",
                date_col="labor_date",
                existing_manifest=labor_store.read_manifest(dataset_path),
                manifest_updates=manifest_updates,
                replace_window_start=chunk_start,
                replace_window_end=chunk_end,
                keep_prev=bool(keep_prev),
            )

        latest_manifest = _recompute_manifest_bounds(dataset_path)
        state["dataset_version"] = latest_manifest.get("dataset_version")
        state["initial_backfill_complete"] = bool(state.get("initial_backfill_complete")) or mode == "backfill"
        state["watermark"] = {"labor_date_max": latest_manifest.get("max_date")}
        state["last_success"] = loaded_at
        state["last_error"] = None
        state["rows_last_pull"] = int(total_rows)
        state["windows_last_pull"] = int(processed_windows)
        labor_state.save_state(state, dataset_path=dataset_path)
        labor_store.reset_duckdb_state()
        return {
            "status": "ok",
            "mode": mode,
            "path": dataset_path.as_posix(),
            "dataset_version": latest_manifest.get("dataset_version"),
            "row_count": latest_manifest.get("row_count"),
            "rows_last_pull": int(total_rows),
            "source_rows_last_pull": int(total_source_rows),
            "windows_processed": int(processed_windows),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "last_refresh_utc": loaded_at,
        }
    except SynerionConfigError:
        raise
    except Exception as exc:
        sanitized = f"{type(exc).__name__}: {str(exc)}"
        logger.exception(
            "labor_etl.refresh_failed",
            extra={
                "mode": mode,
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "dataset_path": dataset_path.as_posix(),
            },
        )
        state["last_error"] = sanitized
        state["rows_last_pull"] = int(total_rows)
        state["windows_last_pull"] = int(processed_windows)
        labor_state.save_state(state, dataset_path=dataset_path)
        raise


def backfill_labor(*, start: Any = None, end: Any = None, keep_prev: bool = False, write_raw: bool = True) -> dict[str, Any]:
    return refresh_labor(mode="backfill", start=start, end=end, keep_prev=keep_prev, write_raw=write_raw)


def repair_recent_labor(*, end: Any = None, keep_prev: bool = False, write_raw: bool = True) -> dict[str, Any]:
    return refresh_labor(mode="recent-repair", end=end, keep_prev=keep_prev, write_raw=write_raw)
