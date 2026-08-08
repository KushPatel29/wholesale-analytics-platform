from __future__ import annotations

import importlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Protocol

import requests


_LOG = logging.getLogger(__name__)
_LLAMA_LOCK = Lock()
_LLAMA_INSTANCE: Any | None = None
_LLAMA_INSTANCE_KEY: tuple[str, int, int, int, int] | None = None


@dataclass(frozen=True)
class ProviderConfig:
    enabled: bool
    provider: str
    model: str
    base_url: str
    timeout_seconds: int
    model_path: str = ""
    context_window: int = 4096
    max_tokens: int = 384
    threads: int = 0
    batch_size: int = 256
    gpu_layers: int = 0


class AssistantProvider(Protocol):
    def generate(
        self,
        *,
        message: str,
        tool_results: List[Dict[str, Any]],
        history: List[Dict[str, Any]],
        context: Dict[str, Any],
    ) -> str | None:
        ...

    def health(self) -> Dict[str, Any]:
        ...


def _system_prompt() -> str:
    return (
        "You are the Northgate Retail Analytics Copilot, an enterprise BI assistant for Northgate Retail Group. "
        "Always answer from supplied tool results and context JSON only; never invent metrics, rankings, or trends. "
        "Lead with the business answer in the first one to three sentences. "
        "Do not expose internal scaffolding such as module names, answer type labels, diagnostics, raw tool names, latency, or query-shape metadata unless the user explicitly asks for debug detail. "
        "Before answering, infer question intent and analytics shape (ranking, filtered ranking, grouped metric, nested ranking, history, comparison, risk, export, definition, executive). "
        "For ranking/grouped questions, respond as structured analytics output using metric, dimension, direction, scope, and window from context. "
        "If the tool results include nested parent/child rankings, answer hierarchically and do not collapse the result to parent-only summary. "
        "If the tool results include resolved relationship filters, state the applied scope explicitly (for example, within a sales rep or supplier scope). "
        "For history/comparison questions, include period interpretation and state comparison target when available. "
        "If historical data is missing, say that clearly in business language and offer the best available alternative insight from comparisons, movers, or KPIs. "
        "For export questions, describe exactly what workbook was generated and what sheets are included. "
        "Treat file-generation requests as structured export tasks (plan -> scoped data -> file), not generic summaries. "
        "Respect permissions and masking; if restricted or sparse, clearly state limitation and safer next step. "
        "Use page context only when it improves scope; do not let irrelevant page labels dominate the business answer. "
        "In analyst mode, add materially richer driver, concentration, and trust detail than standard mode. "
        "Separate facts, inference, and action when appropriate. "
        "Use concise enterprise language: direct answer first, then supporting evidence, then caveats, then next action."
    )


def _build_prompt_context(
    *,
    tool_results: List[Dict[str, Any]],
    history: List[Dict[str, Any]],
    context: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "scope": context.get("scope"),
        "window": context.get("window"),
        "trust_flags": context.get("trust_flags"),
        "tool_results": tool_results,
        "history": history[-6:],
    }


def _build_messages(*, message: str, prompt_context: Dict[str, Any]) -> List[Dict[str, str]]:
    return [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": f"Question: {message}\n\nContext JSON:\n{json.dumps(prompt_context, default=str)}"},
    ]


def _extract_chat_text(payload: Any) -> str | None:
    if isinstance(payload, dict):
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = str(message.get("content") or "").strip()
                    if content:
                        return content
        message = payload.get("message")
        if isinstance(message, dict):
            content = str(message.get("content") or "").strip()
            if content:
                return content
    return None


def _extract_completion_text(payload: Any) -> str | None:
    if isinstance(payload, dict):
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                text = str(first.get("text") or "").strip()
                if text:
                    return text
    return None


def _import_llama_cpp_module() -> Any:
    return importlib.import_module("llama_cpp")


def _normalized_model_path(raw_value: str) -> str:
    candidate = str(raw_value or "").strip()
    if not candidate:
        return ""
    try:
        return Path(candidate).expanduser().resolve(strict=False).as_posix()
    except Exception:
        return candidate


def _default_threads() -> int:
    return max(1, min(4, os.cpu_count() or 2))


class OllamaProvider:
    def __init__(self, config: ProviderConfig) -> None:
        self._config = config

    def generate(
        self,
        *,
        message: str,
        tool_results: List[Dict[str, Any]],
        history: List[Dict[str, Any]],
        context: Dict[str, Any],
    ) -> str | None:
        if not self._config.enabled:
            return None
        prompt_context = _build_prompt_context(tool_results=tool_results, history=history, context=context)
        payload = {
            "model": self._config.model,
            "stream": False,
            "messages": _build_messages(message=message, prompt_context=prompt_context),
            "options": {"temperature": 0.2},
        }
        try:
            url = f"{self._config.base_url.rstrip('/')}/api/chat"
            resp = requests.post(url, json=payload, timeout=max(3, int(self._config.timeout_seconds)))
            if resp.status_code >= 400:
                _LOG.warning("assistant.ollama_http_error status=%s body=%s", resp.status_code, resp.text[:400])
                return None
            body = resp.json() if resp.content else {}
            message_obj = body.get("message") if isinstance(body, dict) else None
            content = (message_obj or {}).get("content") if isinstance(message_obj, dict) else None
            text = str(content or "").strip()
            return text or None
        except Exception:
            _LOG.exception("assistant.ollama_generate_failed")
            return None

    def health(self) -> Dict[str, Any]:
        if not self._config.enabled:
            return {"status": "disabled", "provider": "ollama"}
        try:
            url = f"{self._config.base_url.rstrip('/')}/api/tags"
            resp = requests.get(url, timeout=max(3, int(self._config.timeout_seconds)))
            if resp.status_code >= 400:
                return {
                    "status": "error",
                    "provider": "ollama",
                    "http_status": int(resp.status_code),
                }
            body = resp.json() if resp.content else {}
            models = body.get("models") if isinstance(body, dict) else None
            count = len(models) if isinstance(models, list) else None
            return {"status": "ok", "provider": "ollama", "models_visible": count}
        except Exception as exc:
            return {"status": "error", "provider": "ollama", "error": str(exc)}


class LlamaCppProvider:
    def __init__(self, config: ProviderConfig) -> None:
        self._config = config

    def _resolved_model_path(self) -> str:
        explicit = _normalized_model_path(self._config.model_path)
        if explicit:
            return explicit
        model_value = str(self._config.model or "").strip()
        if model_value.lower().endswith(".gguf") or "/" in model_value or "\\" in model_value:
            return _normalized_model_path(model_value)
        return ""

    def _load_model(self) -> Any:
        model_path = self._resolved_model_path()
        if not model_path:
            raise RuntimeError("AI_MODEL_PATH is not configured")
        if not Path(model_path).exists():
            raise RuntimeError(f"Model file not found: {model_path}")
        n_ctx = max(1024, int(self._config.context_window or 4096))
        n_threads = max(1, int(self._config.threads or _default_threads()))
        n_batch = max(64, int(self._config.batch_size or 256))
        n_gpu_layers = max(0, int(self._config.gpu_layers or 0))
        cache_key = (model_path, n_ctx, n_threads, n_batch, n_gpu_layers)
        global _LLAMA_INSTANCE, _LLAMA_INSTANCE_KEY
        with _LLAMA_LOCK:
            if _LLAMA_INSTANCE is not None and _LLAMA_INSTANCE_KEY == cache_key:
                return _LLAMA_INSTANCE
            module = _import_llama_cpp_module()
            llama_cls = getattr(module, "Llama", None)
            if llama_cls is None:
                raise RuntimeError("llama_cpp.Llama is unavailable")
            _LLAMA_INSTANCE = llama_cls(
                model_path=model_path,
                n_ctx=n_ctx,
                n_threads=n_threads,
                n_batch=n_batch,
                n_gpu_layers=n_gpu_layers,
                verbose=False,
            )
            _LLAMA_INSTANCE_KEY = cache_key
            return _LLAMA_INSTANCE

    def generate(
        self,
        *,
        message: str,
        tool_results: List[Dict[str, Any]],
        history: List[Dict[str, Any]],
        context: Dict[str, Any],
    ) -> str | None:
        if not self._config.enabled:
            return None
        prompt_context = _build_prompt_context(tool_results=tool_results, history=history, context=context)
        messages = _build_messages(message=message, prompt_context=prompt_context)
        try:
            llm = self._load_model()
            max_tokens = max(96, int(self._config.max_tokens or 384))
            with _LLAMA_LOCK:
                try:
                    response = llm.create_chat_completion(messages=messages, temperature=0.2, max_tokens=max_tokens)
                    text = _extract_chat_text(response)
                    if text:
                        return text
                except Exception:
                    _LOG.warning("assistant.llama_cpp_chat_failed", exc_info=True)
                response = llm.create_completion(
                    prompt=f"{_system_prompt()}\n\nQuestion: {message}\n\nContext JSON:\n{json.dumps(prompt_context, default=str)}",
                    temperature=0.2,
                    max_tokens=max_tokens,
                )
                return _extract_completion_text(response)
        except Exception:
            _LOG.exception("assistant.llama_cpp_generate_failed")
            return None

    def health(self) -> Dict[str, Any]:
        if not self._config.enabled:
            return {"status": "disabled", "provider": "llama_cpp"}
        model_path = self._resolved_model_path()
        if not model_path:
            return {"status": "error", "provider": "llama_cpp", "error": "model_path_missing"}
        if not Path(model_path).exists():
            return {
                "status": "error",
                "provider": "llama_cpp",
                "error": "model_not_found",
                "model_path": model_path,
            }
        try:
            _import_llama_cpp_module()
        except Exception as exc:
            return {
                "status": "error",
                "provider": "llama_cpp",
                "error": str(exc),
                "model_path": model_path,
            }
        return {
            "status": "ok",
            "provider": "llama_cpp",
            "model_path": model_path,
            "loaded": bool(_LLAMA_INSTANCE_KEY and _LLAMA_INSTANCE_KEY[0] == model_path),
        }


class DisabledProvider:
    def generate(
        self,
        *,
        message: str,
        tool_results: List[Dict[str, Any]],
        history: List[Dict[str, Any]],
        context: Dict[str, Any],
    ) -> str | None:
        return None

    def health(self) -> Dict[str, Any]:
        return {"status": "disabled", "provider": "disabled"}


def build_provider(config: ProviderConfig) -> AssistantProvider:
    token = str(config.provider or "").strip().lower()
    if not config.enabled:
        return DisabledProvider()
    if token == "ollama":
        return OllamaProvider(config)
    if token in {"llama_cpp", "llamacpp", "llama.cpp"}:
        return LlamaCppProvider(config)
    return DisabledProvider()
