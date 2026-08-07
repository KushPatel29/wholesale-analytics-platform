from __future__ import annotations

import runpy


def test_default_config_does_not_override_worker_class(monkeypatch):
    monkeypatch.delenv("GUNICORN_WORKER_CLASS", raising=False)

    config = runpy.run_module("gunicorn_conf")

    assert "worker_class" not in config


def test_worker_class_can_be_configured(monkeypatch):
    monkeypatch.setenv("GUNICORN_WORKER_CLASS", " gevent ")

    config = runpy.run_module("gunicorn_conf")

    assert config["worker_class"] == "gevent"
