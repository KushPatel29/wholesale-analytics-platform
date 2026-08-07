from __future__ import annotations

from types import SimpleNamespace

from app.assistant import provider as provider_module


def _provider_config(**overrides):
    payload = {
        "enabled": True,
        "provider": "llama_cpp",
        "model": "Qwen2.5-1.5B-Instruct-Q4_K_M",
        "model_path": "",
        "base_url": "",
        "timeout_seconds": 15,
        "context_window": 2048,
        "max_tokens": 192,
        "threads": 2,
        "batch_size": 128,
        "gpu_layers": 0,
    }
    payload.update(overrides)
    return provider_module.ProviderConfig(**payload)


def test_build_provider_supports_llama_cpp_alias():
    provider = provider_module.build_provider(_provider_config(provider="llama.cpp"))
    assert provider.__class__.__name__ == "LlamaCppProvider"


def test_llama_cpp_health_reports_missing_model_file(tmp_path):
    config = _provider_config(model_path=(tmp_path / "missing.gguf").as_posix())
    provider = provider_module.build_provider(config)
    health = provider.health()
    assert health["status"] == "error"
    assert health["provider"] == "llama_cpp"
    assert health["error"] == "model_not_found"


def test_llama_cpp_generate_uses_local_model(monkeypatch, tmp_path):
    model_path = tmp_path / "tiny.gguf"
    model_path.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(provider_module, "_LLAMA_INSTANCE", None, raising=True)
    monkeypatch.setattr(provider_module, "_LLAMA_INSTANCE_KEY", None, raising=True)

    created = {}

    class FakeLlama:
        def __init__(self, **kwargs):
            created.update(kwargs)

        def create_chat_completion(self, **kwargs):
            return {"choices": [{"message": {"content": "Local enterprise summary"}}]}

    monkeypatch.setattr(
        provider_module,
        "_import_llama_cpp_module",
        lambda: SimpleNamespace(Llama=FakeLlama),
        raising=True,
    )

    provider = provider_module.build_provider(_provider_config(model_path=model_path.as_posix()))
    text = provider.generate(
        message="Summarize the support evidence",
        tool_results=[{"status": "ok", "title": "Tool", "data": {"value": 1}}],
        history=[{"role": "user", "content": "Hi"}],
        context={"scope": {"module": "overview"}, "window": {"label": "Last 30 days"}, "trust_flags": {}},
    )

    assert text == "Local enterprise summary"
    assert created["model_path"] == model_path.resolve().as_posix()
    assert created["n_ctx"] == 2048
    assert created["n_threads"] == 2
    assert created["n_batch"] == 128
    assert created["n_gpu_layers"] == 0
    assert created["verbose"] is False
