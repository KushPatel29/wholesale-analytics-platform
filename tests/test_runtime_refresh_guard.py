from __future__ import annotations

from app.core import runtime


def test_should_start_refresh_loop_dev_reloader_child(monkeypatch):
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("WERKZEUG_RUN_MAIN", "true")
    monkeypatch.delenv("ENABLE_INPROCESS_REFRESH", raising=False)
    monkeypatch.delenv("GUNICORN_CMD_ARGS", raising=False)
    assert runtime.should_start_refresh_loop(debug=True) is True


def test_should_start_refresh_loop_dev_reloader_parent(monkeypatch):
    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
    monkeypatch.delenv("ENABLE_INPROCESS_REFRESH", raising=False)
    monkeypatch.delenv("GUNICORN_CMD_ARGS", raising=False)
    assert runtime.should_start_refresh_loop(debug=True) is False


def test_should_start_refresh_loop_prod_default_off(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.delenv("ENABLE_INPROCESS_REFRESH", raising=False)
    monkeypatch.delenv("GUNICORN_CMD_ARGS", raising=False)
    assert runtime.should_start_refresh_loop(debug=False) is False


def test_should_start_refresh_loop_prod_enabled(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("ENABLE_INPROCESS_REFRESH", "1")
    monkeypatch.delenv("GUNICORN_CMD_ARGS", raising=False)
    assert runtime.should_start_refresh_loop(debug=False) is True


def test_should_start_refresh_loop_gunicorn_guard(monkeypatch):
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("ENABLE_INPROCESS_REFRESH", "1")
    monkeypatch.setenv("GUNICORN_CMD_ARGS", "--workers 2")
    monkeypatch.delenv("ALLOW_GUNICORN_INPROCESS_REFRESH", raising=False)
    assert runtime.should_start_refresh_loop(debug=False) is False

    monkeypatch.setenv("ALLOW_GUNICORN_INPROCESS_REFRESH", "1")
    assert runtime.should_start_refresh_loop(debug=False) is True
