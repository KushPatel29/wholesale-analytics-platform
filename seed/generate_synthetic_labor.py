"""Generate a compact, deterministic labor dataset for the portfolio demo."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


DEPARTMENTS = (
    ("100", "Distribution Center", 29.0, 8),
    ("200", "Fresh Operations", 25.5, 6),
    ("300", "Store Operations", 23.5, 7),
    ("400", "Transportation", 31.0, 5),
    ("500", "Merchandising", 30.5, 4),
    ("600", "Customer Service", 22.0, 4),
    ("700", "Finance & Administration", 34.0, 3),
    ("800", "Technology & Data", 40.0, 3),
)

FIRST_NAMES = ("Avery", "Jordan", "Maya", "Noah", "Priya", "Mateo", "Elena", "Sam", "Amara", "Liam", "Rina", "Owen")
LAST_NAMES = ("Chen", "Patel", "Johnson", "Garcia", "Okafor", "Nguyen", "Brown", "Kowalski", "Singh", "Martin", "Lopez", "Clark")


def _key(*parts: object) -> str:
    return hashlib.md5("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()


def _fact_anchor() -> date:
    manifest_path = Path("cache/fact_dataset/_manifest.json")
    if manifest_path.exists():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            value = payload.get("max_date") or payload.get("max_dateexpected")
            if value:
                return pd.Timestamp(value).date()
        except Exception:
            pass
    return date.today() - timedelta(days=30)


def generate(*, days: int = 210, seed: int = 4207, output: Path = Path("cache/labor/fact_dataset")) -> dict[str, object]:
    rng = np.random.default_rng(seed)
    end = _fact_anchor()
    start = end - timedelta(days=max(days, 90) - 1)
    loaded_at = datetime.now(timezone.utc).replace(microsecond=0)
    rows: list[dict[str, object]] = []

    employee_number = 0
    employees: list[tuple[str, str, str, str, float]] = []
    for dept_no, dept_name, base_rate, headcount in DEPARTMENTS:
        for local_index in range(headcount):
            employee_number += 1
            code = f"E{employee_number:04d}"
            first = FIRST_NAMES[(employee_number + local_index) % len(FIRST_NAMES)]
            last = LAST_NAMES[(employee_number * 3 + local_index) % len(LAST_NAMES)]
            rate = base_rate * (0.92 + 0.035 * (local_index % 5))
            employees.append((code, f"{first} {last}", dept_no, dept_name, round(rate, 2)))

    for labor_day in pd.date_range(start, end, freq="D"):
        if labor_day.weekday() >= 5:
            continue
        seasonal = 1.0 + 0.08 * np.sin((labor_day.dayofyear / 365.25) * 2 * np.pi)
        for code, name, dept_no, dept_name, base_rate in employees:
            day_seed = int(hashlib.sha1(f"{code}|{labor_day.date()}".encode()).hexdigest()[:8], 16)
            local = np.random.default_rng(seed + day_seed)
            absence = bool(local.random() < (0.035 if dept_no not in {"100", "200"} else 0.05))
            premium = bool(not absence and local.random() < (0.17 if dept_no in {"100", "200", "400"} else 0.08))
            scheduled = 8.0
            paid_hours = 8.0 if not absence else float(local.choice([4.0, 8.0], p=[0.25, 0.75]))
            if premium:
                paid_hours += float(local.choice([1.0, 2.0, 3.0], p=[0.25, 0.55, 0.20]))
            category = "Absence" if absence else "Overtime" if premium else "Regular"
            effective_rate = base_rate * (1.5 if premium else 1.0)
            if absence:
                effective_rate = base_rate
            labor_cost = paid_hours * effective_rate * seasonal * (0.97 + local.random() * 0.06)
            start_time = labor_day + pd.Timedelta(hours=7 + int(local.integers(0, 3)))
            end_time = start_time + pd.Timedelta(hours=max(paid_hours, scheduled))
            employee_key = _key(code)
            department_key = _key(dept_no, dept_name)
            employee_day_key = _key(code, labor_day.date(), dept_no)
            row_hash = _key(employee_day_key, category, paid_hours, round(labor_cost, 2))
            rows.append({
                "labor_date": labor_day.date(),
                "labor_datetime": start_time.to_pydatetime(),
                "labor_week": f"{labor_day.isocalendar().year}-W{labor_day.isocalendar().week:02d}",
                "labor_month": labor_day.strftime("%Y-%m"),
                "labor_year": int(labor_day.year),
                "week_start": (labor_day - pd.Timedelta(days=labor_day.weekday())).date(),
                "weekday_name": labor_day.strftime("%A"),
                "month_name": labor_day.strftime("%B"),
                "employee_code": code,
                "employee_name": name,
                "payroll_code": code,
                "department_name": dept_name,
                "department_number": dept_no,
                "status": "Active",
                "work_rule": "Hourly",
                "shift_match_date": labor_day.to_pydatetime(),
                "schedule_start": start_time.to_pydatetime(),
                "schedule_end": (start_time + pd.Timedelta(hours=scheduled)).to_pydatetime(),
                "first_in_punch_time": None if absence else start_time.to_pydatetime(),
                "last_out_punch_time": None if absence else end_time.to_pydatetime(),
                "paid_start_time": start_time.to_pydatetime(),
                "paid_end_time": end_time.to_pydatetime(),
                "attended_duration_raw": f"{0 if absence else paid_hours:.2f}",
                "attended_hours": 0.0 if absence else paid_hours,
                "attended_hours_allocated": 0.0 if absence else paid_hours,
                "paid_hours": paid_hours,
                "paid_hours_allocated": paid_hours,
                "time_category": category,
                "transaction_index": 1,
                "transaction_duration": paid_hours,
                "transaction_duration_hours": paid_hours,
                "effective_rate": effective_rate,
                "labor_cost": round(labor_cost, 2),
                "memo_amount_raw": None,
                "memo_amount": None,
                "is_paid": True,
                "is_premium": premium,
                "is_absence": absence,
                "is_memo": False,
                "has_time_transaction": True,
                "schedule_hours": scheduled,
                "schedule_hours_allocated": scheduled,
                "punch_span_hours": 0.0 if absence else paid_hours,
                "punch_span_hours_allocated": 0.0 if absence else paid_hours,
                "paid_span_hours": paid_hours,
                "paid_span_hours_allocated": paid_hours,
                "blended_cost_per_paid_hour": round(labor_cost / paid_hours, 2) if paid_hours else None,
                "employee_key": employee_key,
                "employee_day_key": employee_day_key,
                "department_key": department_key,
                "employee_status_group": "active",
                "employee_day_transaction_count": 1,
                "active_employee_flag": 1 if not absence else 0,
                "primary_row_flag": True,
                "source_loaded_at": loaded_at,
                "source_partition_date": end,
                "source_row_hash": row_hash,
                "source_window_start": start,
                "source_window_end": end,
            })

    frame = pd.DataFrame(rows)
    output.mkdir(parents=True, exist_ok=True)
    for existing in output.glob("*.parquet"):
        existing.unlink()
    frame.to_parquet(output / "labor_demo.parquet", index=False, compression="zstd")
    manifest = {
        "built_at_utc": loaded_at.isoformat(),
        "last_refresh_utc": loaded_at.isoformat(),
        "dataset_version": str(int(loaded_at.timestamp() * 1000)),
        "row_count": int(len(frame)),
        "rows": int(len(frame)),
        "min_date": start.isoformat(),
        "max_date": end.isoformat(),
        "source": "seed.generate_synthetic_labor",
        "synthetic": True,
        "departments": len(DEPARTMENTS),
        "employees": len(employees),
    }
    (output / "_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=210)
    parser.add_argument("--seed", type=int, default=4207)
    parser.add_argument("--output", type=Path, default=Path("cache/labor/fact_dataset"))
    args = parser.parse_args()
    print(json.dumps(generate(days=args.days, seed=args.seed, output=args.output), indent=2))


if __name__ == "__main__":
    main()
