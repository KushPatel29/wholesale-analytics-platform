from __future__ import annotations

from datetime import date

import pandas as pd

from app.services import labor_etl, labor_store
from etl import partition_writer
from tests.labor_helpers import build_labor_dataset, sample_synerion_records


def test_normalize_labor_records_allocates_parent_hours_and_flags():
    frame = labor_etl.normalize_labor_records(
        sample_synerion_records(),
        loaded_at="2024-02-10T00:00:00Z",
        window_start=date(2024, 2, 1),
        window_end=date(2024, 2, 10),
    )

    packaging = frame.loc[frame["department_name"] == "Packaging"].sort_values("transaction_index")
    assert len(packaging) == 2
    assert packaging["paid_hours_allocated"].tolist() == [4.0, 4.0]
    assert packaging["is_premium"].tolist() == [False, True]
    assert packaging["transaction_duration_hours"].round(2).tolist() == [7.0, 1.0]
    receiving = frame.loc[frame["department_name"] == "Receiving"].sort_values("transaction_index")
    assert receiving["is_absence"].tolist() == [True, False]
    assert receiving["employee_status_group"].tolist() == ["inactive", "inactive"]


def test_normalize_labor_records_anchors_time_only_timestamps_to_shift_date():
    records = [
        {
            "FirstName": "Alex",
            "LastName": "Shift",
            "EmployeeCode": "E300",
            "PayrollCode": "P300",
            "DepartmentName": "Butchery",
            "DepartmentNumber": "30",
            "ShiftMatchDate": "2022-01-02T00:00:00",
            "Status": "Active",
            "WorkRule": "Day",
            "ScheduleStart": "14:00",
            "ScheduleEnd": "22:00",
            "FirstInPunchTime": "13:57",
            "LastOutPunchTime": "22:04",
            "PaidStartTime": "14:00",
            "PaidEndTime": "22:00",
            "AttendedDuration": "08:07",
            "PaidHours": 8,
            "TimeTransactions": [
                {
                    "TimeCategory": "Regular",
                    "Duration": "08:00",
                    "MemoAmount": "0",
                    "EffectiveRate": 25,
                    "DollarAmount": 200,
                    "IsPaid": "true",
                    "IsPremium": "false",
                    "IsAbsence": "false",
                    "IsMemo": "false",
                }
            ],
        }
    ]
    frame = labor_etl.normalize_labor_records(
        records,
        loaded_at="2024-02-10T00:00:00Z",
        window_start=date(2022, 1, 2),
        window_end=date(2022, 1, 2),
    )

    row = frame.iloc[0]
    assert row["labor_date"] == date(2022, 1, 2)
    assert row["schedule_start"] == pd.Timestamp("2022-01-02T14:00:00")
    assert row["schedule_end"] == pd.Timestamp("2022-01-02T22:00:00")
    assert row["paid_start_time"] == pd.Timestamp("2022-01-02T14:00:00")
    assert row["paid_end_time"] == pd.Timestamp("2022-01-02T22:00:00")
    assert float(row["schedule_hours"]) == 8.0
    assert float(row["punch_span_hours"]) == 8.116666666666667


def test_labor_dataset_upsert_is_idempotent_and_window_repair_is_deterministic(tmp_path):
    dataset_path = tmp_path / "labor_dataset"
    base_records = sample_synerion_records()
    normalized = labor_etl.normalize_labor_records(
        base_records,
        loaded_at="2024-02-10T00:00:00Z",
        window_start=date(2024, 2, 1),
        window_end=date(2024, 2, 10),
    )

    meta1 = partition_writer.upsert_dataset(
        normalized,
        dataset_path=dataset_path,
        pk_col="source_row_hash",
        date_col="labor_date",
        existing_manifest=labor_store.read_manifest(dataset_path),
        manifest_updates={"dataset_version": "1", "date_column": "labor_date"},
        replace_window_start="2024-02-01",
        replace_window_end="2024-02-10",
    )
    meta2 = partition_writer.upsert_dataset(
        normalized,
        dataset_path=dataset_path,
        pk_col="source_row_hash",
        date_col="labor_date",
        existing_manifest=labor_store.read_manifest(dataset_path),
        manifest_updates={"dataset_version": "2", "date_column": "labor_date"},
        replace_window_start="2024-02-01",
        replace_window_end="2024-02-10",
    )
    assert int(meta1.get("row_count") or 0) == 4
    assert int(meta2.get("row_count") or 0) == 4

    revised_records = sample_synerion_records()
    revised_records[0]["TimeTransactions"][1]["DollarAmount"] = 40
    revised = labor_etl.normalize_labor_records(
        revised_records,
        loaded_at="2024-02-11T00:00:00Z",
        window_start=date(2024, 2, 1),
        window_end=date(2024, 2, 10),
    )
    meta3 = partition_writer.upsert_dataset(
        revised,
        dataset_path=dataset_path,
        pk_col="source_row_hash",
        date_col="labor_date",
        existing_manifest=labor_store.read_manifest(dataset_path),
        manifest_updates={"dataset_version": "3", "date_column": "labor_date"},
        replace_window_start="2024-02-01",
        replace_window_end="2024-02-10",
    )
    assert int(meta3.get("row_count") or 0) == 4
    updated = pd.concat([pd.read_parquet(path) for path in dataset_path.rglob("*.parquet")], ignore_index=True)
    overtime_cost = updated.loc[
        (updated["department_name"] == "Packaging") & (updated["transaction_index"] == 2),
        "labor_cost",
    ].iloc[0]
    assert float(overtime_cost) == 40.0


def test_partition_writer_can_clear_labor_window_with_empty_refresh(tmp_path):
    dataset_path = tmp_path / "labor_dataset"
    build_labor_dataset(dataset_path)

    empty_refresh = labor_etl.normalize_labor_records(
        [],
        loaded_at="2024-02-12T00:00:00Z",
        window_start=date(2024, 2, 1),
        window_end=date(2024, 2, 10),
    )
    meta = partition_writer.upsert_dataset(
        empty_refresh,
        dataset_path=dataset_path,
        pk_col="source_row_hash",
        date_col="labor_date",
        existing_manifest=labor_store.read_manifest(dataset_path),
        manifest_updates={"dataset_version": "2", "date_column": "labor_date"},
        replace_window_start="2024-02-01",
        replace_window_end="2024-02-10",
    )
    assert int(meta.get("row_count") or 0) == 0
