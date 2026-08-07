from __future__ import annotations

from flask import Flask

from app import __init__ as app_module


def test_background_refresh_skips_when_disabled(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    app = Flask("test-disabled")
    app.config.update(AUTO_REFRESH=False, ENV="production")

    app_module._bootstrap_background_refresh(app)
    assert "wa_background" not in app.extensions


def test_background_refresh_skips_when_enabled(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    app = Flask("test-enabled")
    app.config.update(AUTO_REFRESH=True, ENV="production")

    app_module._bootstrap_background_refresh(app)
    assert "wa_background" not in app.extensions
