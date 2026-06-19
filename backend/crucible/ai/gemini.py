"""Google Gemini provider.

Default because the free tier is generous enough to develop against without a
billing account.
"""

from __future__ import annotations

import json
import time

from crucible.ai.base import AIError, AIProvider, AIRequest, AIResponse, AIUnavailableError
from crucible.core.config import settings


class GeminiProvider(AIProvider):
    name = "gemini"

    # Free-tier pricing is zero; these are the paid rates so the ledger still
    # reports what the usage *would* cost, which is what tells you whether a
    # feature is viable before you turn billing on.
    price_per_mtok_in = 0.10
    price_per_mtok_out = 0.40

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        key = api_key or settings.gemini_api_key.get_secret_value()
        if not key:
            raise AIUnavailableError(
                "GEMINI_API_KEY is not set. Get a free key at "
                "https://aistudio.google.com/apikey, or set AI_PROVIDER=ollama "
                "to run locally, or AI_PROVIDER=fake to disable real calls."
            )
        try:
            from google import genai
        except ImportError as exc:
            raise AIUnavailableError("google-genai is not installed") from exc

        self.model = model or settings.gemini_model
        self._genai = genai
        self._client = genai.Client(api_key=key)

    def healthy(self) -> bool:
        try:
            # Listing models is the cheapest call that proves the key works.
            next(iter(self._client.models.list()), None)
            return True
        except Exception:
            return False

    def complete(self, request: AIRequest) -> AIResponse:
        from google.genai import types

        started = time.perf_counter()
        config = types.GenerateContentConfig(
            temperature=request.temperature,
            max_output_tokens=request.max_output_tokens or settings.ai_max_output_tokens,
            system_instruction=request.system,
        )
        if request.schema:
            # Structured output is enforced by the model rather than requested
            # in the prompt. Asking for JSON in prose gets you JSON wrapped in
            # a markdown fence often enough to matter.
            config.response_mime_type = "application/json"
            config.response_schema = request.schema

        try:
            response = self._client.models.generate_content(
                model=self.model, contents=request.prompt, config=config
            )
        except Exception as exc:
            raise AIUnavailableError(f"gemini request failed: {exc}") from exc

        text = (getattr(response, "text", None) or "").strip()
        if not text:
            raise AIError("gemini returned an empty response")

        data: dict[str, object] = {}
        if request.schema:
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise AIError(f"gemini returned invalid JSON: {exc}") from exc

        usage = getattr(response, "usage_metadata", None)
        return AIResponse(
            text=text,
            data=data,
            provider=self.name,
            model=self.model,
            prompt_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            completion_tokens=int(getattr(usage, "candidates_token_count", 0) or 0),
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
