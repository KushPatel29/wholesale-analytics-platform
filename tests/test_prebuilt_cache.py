from __future__ import annotations

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
