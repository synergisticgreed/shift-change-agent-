"""LLM clients behind one tiny interface: complete(system, user, json_mode) -> str.
Data Flow: node prompt -> GroqLLM (HTTP, 2s timeout) -> raw text -> node validates it.
           Any failure -> LLMError -> node falls back to code/templates."""
from typing import Protocol

import httpx

from app.config import settings


class LLMError(Exception):
    """Timeout, HTTP error or bad response. Caller falls back to templates."""


class LLM(Protocol):
    def complete(self, system: str, user: str, json_mode: bool = False) -> str: ...


class GroqLLM:
    """Groq's OpenAI-compatible chat API (free tier)."""
    URL = "https://api.groq.com/openai/v1/chat/completions"

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        body = {"model": settings.llm_model, "temperature": 0,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            res = httpx.post(self.URL, json=body, timeout=settings.llm_timeout_s,
                             headers={"Authorization": f"Bearer {settings.llm_api_key}"})
            res.raise_for_status()
            return res.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError) as e:
            raise LLMError(str(e)) from e


def get_llm() -> LLM:
    if settings.llm_provider == "groq":
        return GroqLLM()
    from app.llm.fake import FakeLLM
    return FakeLLM()
