"""Export retries must retain the right file and obey a real one-minute limit."""
import io
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import openpyxl
import pandas as pd
import pytest
from cachetools import TTLCache

from app.assistant import export_job_store as jobs
from app.assistant import tools


@pytest.fixture()
def queue(monkeypatch):
    clock = [0.0]
    submitted = []

    def submit(fn, *args):
        submitted.append((fn, args))

    monkeypatch.setattr(jobs, "time", SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0]))
    for name in ("_JOBS", "_REQUEST_DEDUPE", "_RATE_COUNTER"):
        ttl = 300 if name == "_JOBS" else 45 if name == "_REQUEST_DEDUPE" else 60
        monkeypatch.setattr(jobs, name, TTLCache(maxsize=128, ttl=ttl, timer=lambda: clock[0]))
    monkeypatch.setattr(jobs, "_EXECUTOR", SimpleNamespace(submit=submit))
    monkeypatch.setenv("ASSISTANT_EXPORT_JOB_MAX_PER_MINUTE", "100")
    monkeypatch.setenv("ASSISTANT_EXPORT_JOB_MAX_ACTIVE_PER_USER", "100")
    return SimpleNamespace(clock=clock, submitted=submitted)


def enqueue(key, user="viewer"):
    return jobs.enqueue_export_job(user, request_key=key, task=lambda: {"export_id": f"file-{key}"})


@pytest.mark.parametrize("status", ["pending", "running", "completed"])
@pytest.mark.parametrize("cap", ["active", "rate"])
def test_identical_retry_returns_existing_job_even_at_capacity(queue, monkeypatch, status, cap):
    first = enqueue("same")
    first["job"].status = status
    if cap == "rate":
        monkeypatch.setenv("ASSISTANT_EXPORT_JOB_MAX_PER_MINUTE", "1")
    else:
        monkeypatch.setenv("ASSISTANT_EXPORT_JOB_MAX_ACTIVE_PER_USER", "1")
        if status == "completed":
            enqueue("other")
    retry = enqueue("same")
    assert retry["status"] == "deduped"
    assert retry["job"].job_id == first["job"].job_id
    assert len(queue.submitted) == (2 if cap == "active" and status == "completed" else 1)


@pytest.mark.parametrize("now", [60.0, 60.01, 79.9])
def test_one_minute_limit_releases_old_requests_without_requiring_idle_time(queue, monkeypatch, now):
    monkeypatch.setenv("ASSISTANT_EXPORT_JOB_MAX_PER_MINUTE", "3")
    for timestamp in (0.0, 20.0, 40.0):
        queue.clock[0] = timestamp
        assert enqueue(str(timestamp))["status"] == "queued"
    queue.clock[0] = now
    assert enqueue("new")["status"] == "queued"
    assert len(queue.submitted) == 4


def test_limit_reports_when_the_oldest_request_leaves_the_window(queue, monkeypatch):
    monkeypatch.setenv("ASSISTANT_EXPORT_JOB_MAX_PER_MINUTE", "3")
    for timestamp in (0.0, 20.0, 40.0):
        queue.clock[0] = timestamp
        enqueue(str(timestamp))
    queue.clock[0] = 59.0
    limited = enqueue("new")
    assert limited == {"status": "rate_limited", "retry_after_seconds": 1}
    assert len(queue.submitted) == 3


def test_rejected_new_work_does_not_consume_a_slot_or_another_users_quota(queue, monkeypatch):
    monkeypatch.setenv("ASSISTANT_EXPORT_JOB_MAX_ACTIVE_PER_USER", "1")
    first = enqueue("first")
    assert enqueue("second")["status"] == "busy"
    assert enqueue("second", user="other")["status"] == "queued"
    first["job"].status = "completed"
    assert enqueue("second")["status"] == "queued"
    assert len(queue.submitted) == 3


def test_worker_submission_failure_leaves_no_phantom_job_and_can_retry(queue, monkeypatch):
    monkeypatch.setenv("ASSISTANT_EXPORT_JOB_MAX_PER_MINUTE", "1")
    real_submit = jobs._EXECUTOR.submit

    def unavailable(*args):
        raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(jobs._EXECUTOR, "submit", unavailable)
    assert enqueue("same")["status"] == "busy"
    assert not list(jobs._JOBS.values())
    monkeypatch.setattr(jobs._EXECUTOR, "submit", real_submit)
    assert enqueue("same")["status"] == "queued"
    assert len(queue.submitted) == 1


def test_failed_task_is_reported_and_another_user_cannot_read_the_job(queue):
    def fail():
        raise ValueError("workbook could not be generated")

    result = jobs.enqueue_export_job("viewer", request_key="fail", task=fail)
    fn, args = queue.submitted[0]
    fn(*args)
    job = jobs.get_export_job("viewer", result["job"].job_id)
    assert job.status == "error" and job.export_id is None
    assert "workbook could not be generated" in job.error
    assert jobs.get_export_job("other", job.job_id) is None
    assert enqueue("fail")["status"] == "queued"


@pytest.mark.parametrize("identical", [True, False])
def test_concurrent_requests_respect_deduplication_and_admission_limits(queue, monkeypatch, identical):
    monkeypatch.setenv("ASSISTANT_EXPORT_JOB_MAX_PER_MINUTE", "3")
    barrier = Barrier(8)

    def request(index):
        barrier.wait(timeout=5)
        return enqueue("same" if identical else f"request-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(request, range(8)))
    statuses = [result["status"] for result in results]
    assert statuses.count("queued") == (1 if identical else 3)
    assert statuses.count("deduped" if identical else "rate_limited") == (7 if identical else 5)
    assert len(queue.submitted) == (1 if identical else 3)
    if identical:
        assert len({result["job"].job_id for result in results}) == 1


@pytest.fixture()
def workbook_queue(queue, monkeypatch):
    ctx = SimpleNamespace(user=SimpleNamespace(id="viewer", username="viewer"),
                          filters=SimpleNamespace(start=None, end=None), scope={}, sensitive_flags={})
    monkeypatch.setattr(tools, "_can_export_module", lambda *args: True)
    monkeypatch.setattr(tools, "_scope_used", lambda ctx: dict(ctx.scope))
    monkeypatch.setattr(tools, "_apply_export_column_policy",
                        lambda ctx, frame, **kwargs: (frame, {"selected_columns": list(frame.columns)}))

    def register(*, sheets=None, filename="report", charts=None, plan=None, generated_metadata=False):
        sheets = dict(sheets) if sheets is not None else {"Data": pd.DataFrame({"item": ["A"], "units": [7]})}
        if generated_metadata:
            sheets["Metadata"] = tools._metadata_frame(ctx, module="products", export_type="entity_history")
        return tools._register_workbook_export(
            ctx, module="products", export_type="entity_history",
            sheets=sheets,
            filename_stem=filename, chart_specs=charts, export_plan=plan,
            async_export=True,
        )["data"]["job_id"]

    return register


@pytest.mark.parametrize("change", ["values", "columns", "sheet", "type", "filename", "chart", "plan"])
def test_different_exports_with_equal_row_counts_never_reuse_the_wrong_file(workbook_queue, change):
    charts = [{"source_sheet": "Data", "chart_type": "bar", "category_col": "item", "value_cols": ["units"]}]
    first = workbook_queue(charts=charts)
    options = {"charts": charts}
    if change == "values":
        options["sheets"] = {"Data": pd.DataFrame({"item": ["B"], "units": [9]})}
    elif change == "columns":
        options["sheets"] = {"Data": pd.DataFrame({"category": ["A"], "units": [7]})}
    elif change == "sheet":
        options["sheets"] = {"Other": pd.DataFrame({"item": ["A"], "units": [7]})}
        options["charts"] = [{**charts[0], "source_sheet": "Other"}]
    elif change == "type":
        options["sheets"] = {"Data": pd.DataFrame({"item": ["A"], "units": ["7"]})}
    elif change == "filename":
        options["filename"] = "other-report"
    elif change == "chart":
        options["charts"] = [{**charts[0], "chart_type": "line"}]
    else:
        options["plan"] = {"entity_id": "other"}
    assert workbook_queue(**options) != first


@pytest.mark.parametrize("generated_metadata", [False, True])
def test_identical_export_dedupes_despite_a_new_generated_timestamp(workbook_queue, monkeypatch, generated_metadata):
    monkeypatch.setattr(tools.time, "strftime", lambda *args: "2026-10-25 12:00:00")
    first = workbook_queue(generated_metadata=generated_metadata)
    monkeypatch.setattr(tools.time, "strftime", lambda *args: "2026-10-25 12:00:01")
    assert workbook_queue(generated_metadata=generated_metadata) == first


def test_distinct_queued_reports_download_their_own_workbook_content(workbook_queue, queue):
    from app.assistant.export_store import clear_export, get_export

    first = workbook_queue()
    second = workbook_queue(sheets={"Data": pd.DataFrame({"item": ["B"], "units": [9]})})
    assert first != second
    assert len(queue.submitted) == 2
    for fn, args in queue.submitted:
        fn(*args)
    for job_id, expected in ((first, ("A", 7)), (second, ("B", 9))):
        job = jobs.get_export_job("viewer", job_id)
        assert job.status == "completed"
        artifact = get_export("viewer", job.export_id)
        assert artifact and artifact.filename == "report.xlsx"
        book = openpyxl.load_workbook(io.BytesIO(artifact.data))
        try:
            assert tuple(book["Data"].values)[1] == expected
        finally:
            book.close()
            clear_export("viewer", job.export_id)
