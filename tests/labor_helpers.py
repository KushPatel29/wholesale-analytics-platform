from __future__ import annotations

from datetime import date
from pathlib import Path

from app.services import labor_etl, labor_store
from etl import partition_writer


def sample_synerion_records() -> list[dict]:
    return [
        {
            "FirstName": "Jane",
            "LastName": "Worker",
            "EmployeeCode": "E100",
            "PayrollCode": "P100",
            "DepartmentName": "Packaging",
            "DepartmentNumber": "10",
            "ShiftMatchDate": "2024-02-01T08:00:00",
            "Status": "Active",
            "WorkRule": "Day",
            "ScheduleStart": "2024-02-01T08:00:00",
            "ScheduleEnd": "2024-02-01T16:00:00",
            "FirstInPunchTime": "2024-02-01T07:58:00",
            "LastOutPunchTime": "2024-02-01T16:04:00",
            "PaidStartTime": "2024-02-01T08:00:00",
            "PaidEndTime": "2024-02-01T16:00:00",
            "AttendedDuration": "08:00",
            "PaidHours": 8,
            "TimeTransactions": [
                {
                    "TimeCategory": "Regular",
                    "Duration": 7.0,
                    "MemoAmount": "0",
                    "EffectiveRate": 24,
                    "DollarAmount": 168,
                    "IsPaid": "true",
                    "IsPremium": "false",
                    "IsAbsence": "false",
                    "IsMemo": "false",
                },
                {
                    "TimeCategory": "Overtime",
                    "Duration": 1.0,
                    "MemoAmount": "0",
                    "EffectiveRate": 36,
                    "DollarAmount": 36,
                    "IsPaid": "true",
                    "IsPremium": "true",
                    "IsAbsence": "false",
                    "IsMemo": "false",
                },
            ],
        },
        {
            "FirstName": "Mark",
            "LastName": "Ops",
            "EmployeeCode": "E200",
            "PayrollCode": "P200",
            "DepartmentName": "Receiving",
            "DepartmentNumber": "20",
            "ShiftMatchDate": "2024-02-02T08:00:00",
            "Status": "Inactive",
            "WorkRule": "Night",
            "ScheduleStart": "2024-02-02T08:00:00",
            "ScheduleEnd": "2024-02-02T16:00:00",
            "FirstInPunchTime": "2024-02-02T08:10:00",
            "LastOutPunchTime": "2024-02-02T16:05:00",
            "PaidStartTime": "2024-02-02T08:00:00",
            "PaidEndTime": "2024-02-02T16:00:00",
            "AttendedDuration": "07:55",
            "PaidHours": 8,
            "TimeTransactions": [
                {
                    "TimeCategory": "Absence",
                    "Duration": "02:00",
                    "MemoAmount": "12.5",
                    "EffectiveRate": 22,
                    "DollarAmount": 44,
                    "IsPaid": "true",
                    "IsPremium": "false",
                    "IsAbsence": "true",
                    "IsMemo": "false",
                },
                {
                    "TimeCategory": "Regular",
                    "Duration": "06:00",
                    "MemoAmount": "0",
                    "EffectiveRate": 22,
                    "DollarAmount": 132,
                    "IsPaid": "true",
                    "IsPremium": "false",
                    "IsAbsence": "false",
                    "IsMemo": "false",
                },
            ],
        },
    ]


def build_labor_dataset(dataset_path: Path, *, records: list[dict] | None = None) -> Path:
    records = list(records or sample_synerion_records())
    normalized = labor_etl.normalize_labor_records(
        records,
        loaded_at="2024-02-10T00:00:00Z",
        window_start=date(2024, 2, 1),
        window_end=date(2024, 2, 10),
    )
    partition_writer.upsert_dataset(
        normalized,
        dataset_path=dataset_path,
        pk_col="source_row_hash",
        date_col="labor_date",
        existing_manifest=labor_store.read_manifest(dataset_path),
        manifest_updates={
            "dataset_type": "labor",
            "source_system": "test",
            "dataset_version": "1",
            "last_refresh_utc": "2024-02-10T00:00:00Z",
            "built_at_utc": "2024-02-10T00:00:00Z",
            "date_column": "labor_date",
        },
        replace_window_start="2024-02-01",
        replace_window_end="2024-02-10",
    )
    return dataset_path
