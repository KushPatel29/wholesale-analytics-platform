"""
Digest presets: the hour you asked for, and surviving a restart.

Two reported bugs are pinned here. A requested hour of `0` was replaced by the
default `8`, so a midnight digest quietly became an 8 a.m. one - `int(value or
8)`, and `0` is falsy. And the records lived in a process-local `TTLCache`, so
they vanished on restart and each worker had its own set.

The third check is about honesty rather than a crash: nothing in this app
delivers a scheduled digest, so every record has to say so.
"""

from __future__ import annotations

import importlib

import pytest

from app.assistant import digest_schedule_store as store


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Point the store at a temp database, one per test."""
    monkeypatch.setenv("ASSISTANT_DIGEST_STORE_PATH", str(tmp_path / "digests.sqlite3"))
    store._DB_READY.clear()
    yield
    store._DB_READY.clear()


class TestHourOfDay:
    def test_midnight_is_kept(self):
        """The reported bug: hour 0 must not become hour 8."""
        saved = store.create_schedule("u1", {"module": "overview", "hour_local": 0})
        assert saved["hour_local"] == 0

    def test_midnight_survives_a_round_trip(self):
        saved = store.create_schedule("u1", {"module": "overview", "hour_local": 0})
        assert store.get_schedule("u1", saved["schedule_id"])["hour_local"] == 0

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            (0, 0),
            ("0", 0),
            (7, 7),
            (23, 23),
            (24, 23),
            (-3, 0),
            (None, store.DEFAULT_HOUR_LOCAL),
            ("", store.DEFAULT_HOUR_LOCAL),
            ("   ", store.DEFAULT_HOUR_LOCAL),
            ("not an hour", store.DEFAULT_HOUR_LOCAL),
            (True, store.DEFAULT_HOUR_LOCAL),
        ],
    )
    def test_coercion(self, given, expected):
        assert store.coerce_hour_local(given) == expected

    def test_out_of_range_hours_are_clamped_not_stored_raw(self):
        saved = store.create_schedule("u1", {"module": "overview", "hour_local": 99})
        assert saved["hour_local"] == 23


class TestPersistence:
    def test_a_preset_survives_a_module_reload(self):
        """Standing in for a process restart: the record must outlive the module."""
        saved = store.create_schedule("u1", {"module": "customers", "hour_local": 0})
        importlib.reload(store)
        store._DB_READY.clear()

        found = store.get_schedule("u1", saved["schedule_id"])
        assert found is not None, "the preset did not survive a restart"
        assert found["module"] == "customers"
        assert found["hour_local"] == 0

    def test_presets_are_scoped_to_their_owner(self):
        mine = store.create_schedule("u1", {"module": "overview"})
        store.create_schedule("u2", {"module": "products"})

        assert [row["schedule_id"] for row in store.list_schedules("u1")] == [mine["schedule_id"]]
        assert store.get_schedule("u2", mine["schedule_id"]) is None

    def test_delete_removes_it(self):
        saved = store.create_schedule("u1", {"module": "overview"})
        assert store.delete_schedule("u1", saved["schedule_id"]) is True
        assert store.get_schedule("u1", saved["schedule_id"]) is None
        assert store.delete_schedule("u1", saved["schedule_id"]) is False

    def test_marking_a_run_is_recorded(self):
        saved = store.create_schedule("u1", {"module": "overview"})
        updated = store.mark_schedule_run("u1", saved["schedule_id"], status="ok")

        assert updated["run_count"] == 1
        assert updated["last_status"] == "ok"
        assert updated["last_run_at"] is not None
        assert store.get_schedule("u1", saved["schedule_id"])["run_count"] == 1


class TestDeliveryIsNotClaimed:
    def test_every_record_states_that_nothing_delivers_it(self):
        """There is no scheduler process, so no record may imply one."""
        saved = store.create_schedule("u1", {"module": "overview"})

        assert saved["delivery_mode"] == "on_demand"
        assert "no scheduled delivery" in saved["delivery_note"].lower()

    def test_listed_records_carry_it_too(self):
        store.create_schedule("u1", {"module": "overview"})
        assert all(row["delivery_mode"] == "on_demand" for row in store.list_schedules("u1"))
