from __future__ import annotations

import os
import subprocess
import sys

from app.core import prebuilt_cache


def test_prebuilt_cache_round_trip_is_opt_in(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_PREBUILT_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("DEMO_PREBUILT_CACHE_WRITE", "1")
    monkeypatch.setenv("DEMO_PREBUILT_CACHE_READ", "0")

    payload = {"meta": {"cached": False}, "rows": [1, 2, 3]}
    assert prebuilt_cache.save("bundles", "abc123", payload) is True
    assert prebuilt_cache.load("bundles", "abc123") is None

    monkeypatch.setenv("DEMO_PREBUILT_CACHE_READ", "1")
    assert prebuilt_cache.load("bundles", "abc123") == payload


def test_prebuilt_cache_rejects_invalid_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_PREBUILT_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("DEMO_PREBUILT_CACHE_WRITE", "1")
    monkeypatch.setenv("DEMO_PREBUILT_CACHE_READ", "1")

    assert prebuilt_cache.save("bundles", "---", {"ok": True}) is False
    assert prebuilt_cache.load("bundles", "---") is None


def test_runtime_readiness_requires_a_valid_filter_artifact(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_PREBUILT_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("DEMO_PREBUILT_CACHE_READ", "1")
    monkeypatch.setenv("DEMO_PREBUILT_CACHE_WRITE", "1")
    monkeypatch.setattr(prebuilt_cache, "_runtime_ready_cache", None)

    assert prebuilt_cache.runtime_ready() is False
    assert prebuilt_cache.save("filter-options", "abc123", {"options": {"regions": ["West"]}})
    monkeypatch.setattr(prebuilt_cache, "_runtime_ready_cache", None)
    assert prebuilt_cache.runtime_ready() is True


def test_importing_static_builder_does_not_change_runtime_cache_mode():
    env = os.environ.copy()
    env["DEMO_PREBUILT_CACHE_READ"] = "0"
    env["DEMO_PREBUILT_CACHE_WRITE"] = "0"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, build_static; "
                "assert os.environ['DEMO_PREBUILT_CACHE_READ'] == '0'; "
                "assert os.environ['DEMO_PREBUILT_CACHE_WRITE'] == '0'"
            ),
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
