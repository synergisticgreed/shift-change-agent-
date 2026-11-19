"""LLM clients behind one tiny interface: complete(system, user, json_mode, timeout) -> str.
Data Flow: node -> budget.ask (sets timeout) -> GroqLLM (HTTP in a worker thread)
           -> wait at most `timeout` seconds (wall clock) -> raw text -> node validates it.
           Any failure or timeout -> LLMError -> node falls back to code/templates."""
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Protocol

import httpx

from app.config import settings


class LLMError(Exception):
    """Timeout, HTTP error, bad response or budget used up. Caller falls back."""


class LLM(Protocol):
    def complete(self, system: str, user: str, json_mode: bool = False,
                 timeout: float | None = None) -> str: ...


# Worker threads for HTTP calls, so we can stop WAITING at the deadline even if the
# network is slow (httpx's own timeout is per phase: connect, write, each read...)
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm")


class GroqLLM:
    """Groq's OpenAI-compatible chat API (free tier, fast small model)."""
    URL = "https://api.groq.com/openai/v1/chat/completions"

    def complete(self, system: str, user: str, json_mode: bool = False,
                 timeout: float | None = None) -> str:
        t = timeout or settings.llm_timeout_s
        future = _pool.submit(self._post, system, user, json_mode, t)
        try:
            return future.result(timeout=t)   # hard wall-clock limit for the caller
        except FutureTimeout as e:
            raise LLMError(f"no answer within {t:.1f}s") from e

    def _post(self, system: str, user: str, json_mode: bool, t: float) -> str:
        body = {"model": settings.llm_model, "temperature": 0,
                "max_tokens": settings.llm_max_tokens,   # bounds generation time
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            res = httpx.post(self.URL, json=body, timeout=t,  # also ends the worker thread soon
                             headers={"Authorization": f"Bearer {settings.llm_api_key}"})
            res.raise_for_status()
            text = res.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
            raise LLMError(str(e)) from e
        if not isinstance(text, str):
            raise LLMError("empty answer")
        return text


def get_llm() -> LLM:
    if settings.llm_provider == "groq":
        return GroqLLM()
    from app.llm.fake import FakeLLM
    return FakeLLM()
