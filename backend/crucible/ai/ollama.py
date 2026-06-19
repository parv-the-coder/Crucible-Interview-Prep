"""Local Ollama provider.

Useful when there is no API key: everything runs on the machine at no cost.
Slower and weaker than a hosted model, which is why it is not the default, but
it makes the AI features genuinely runnable offline.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from crucible.ai.base import AIError, AIProvider, AIRequest, AIResponse, AIUnavailableError
from crucible.core.config import settings


class OllamaProvider(AIProvider):
    name = "ollama"

    def __init__(self, model: str | None = None, base_url: str | None = None) -> None:
        self.model = model or settings.ollama_model
        self._base = (base_url or settings.ollama_base_url).rstrip("/")

    def healthy(self) -> bool:
        try:
            response = httpx.get(f"{self._base}/api/tags", timeout=3)
            return response.status_code == 200
        except Exception:
            return False

    def complete(self, request: AIRequest) -> AIResponse:
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": request.prompt,
            "stream": False,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_output_tokens or settings.ai_max_output_tokens,
            },
        }
        if request.system:
            payload["system"] = request.system
        if request.schema:
            # Ollama supports constrained decoding against a JSON schema, which
            # is far more reliable than asking politely for JSON.
            payload["format"] = request.schema

        try:
            response = httpx.post(
                f"{self._base}/api/generate",
                json=payload,
                timeout=settings.ai_request_timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise AIUnavailableError(f"ollama request failed: {exc}") from exc

        text = str(body.get("response", "")).strip()
        data: dict[str, Any] = {}
        if request.schema:
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise AIError(f"ollama returned invalid JSON: {exc}") from exc

        return AIResponse(
            text=text,
            data=data,
            provider=self.name,
            model=self.model,
            prompt_tokens=int(body.get("prompt_eval_count", 0)),
            completion_tokens=int(body.get("eval_count", 0)),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
