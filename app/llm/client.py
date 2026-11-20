"""LLM clients behind one tiny interface: complete(system, user, json_mode, timeout) -> str.
Data Flow: node -> budget.ask (sets timeout) -> GroqLLM | GeminiLLM (HTTP in a worker thread)
           -> wait at most `timeout` seconds (wall clock) -> raw text -> node validates it.
           Any failure or timeout -> LLMError -> node falls back to code/templates."""
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Protocol

import httpx

from app.config import settings
from app.errors import log


class LLMError(Exception):
    """Timeout, HTTP error, bad response or budget used up. Caller falls back."""


class LLM(Protocol):
    def complete(self, system: str, user: str, json_mode: bool = False,
                 timeout: float | None = None) -> str: ...


# Worker threads for HTTP calls, so we can stop WAITING at the deadline even if the
# network is slow (httpx's own timeout is per phase: connect, write, each read...)
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm")


class GroqLLM:
    """Groq's OpenAI-compatible chat API (free tier, fast small model).
    Subclasses only change URL, MODEL and _extra (same request/response shape)."""
    URL = "https://api.groq.com/openai/v1/chat/completions"
    MODEL = "openai/gpt-oss-20b"   # llama-3.1-8b-instant was retired

    def complete(self, system: str, user: str, json_mode: bool = False,
                 timeout: float | None = None) -> str:
        t = timeout or settings.llm_timeout_s
        future = _pool.submit(self._post, system, user, json_mode, t)
        try:
            return future.result(timeout=t)   # hard wall-clock limit for the caller
        except FutureTimeout as e:
            raise LLMError(f"no answer within {t:.1f}s") from e

    def _extra(self, json_mode: bool) -> dict:
        extra = {"response_format": {"type": "json_object"}} if json_mode else {}
        if "gpt-oss" in (settings.llm_model or self.MODEL):  # reasoning model: think little, hide it
            extra.update(reasoning_effort="low", include_reasoning=False)
        return extra

    def _post(self, system: str, user: str, json_mode: bool, t: float) -> str:
        body = {"model": settings.llm_model or self.MODEL, "temperature": 0,
                "max_tokens": settings.llm_max_tokens,   # bounds generation time
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                **self._extra(json_mode)}
        try:
            res = httpx.post(self.URL, json=body, timeout=t,  # also ends the worker thread soon
                             headers={"Authorization": f"Bearer {settings.llm_api_key}"})
            res.raise_for_status()
            text = res.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
            raise LLMError(str(e)) from e
        if not isinstance(text, str) or not text.strip():
            raise LLMError("empty answer")
        return _strip_fences(text) if json_mode else text


class GeminiLLM(GroqLLM):
    """Google Gemini via its OpenAI-compatible endpoint (free tier with an AI Studio key).
    JSON mode is asked for in the prompt only (json_object isn't documented there);
    the understand node validates the JSON and retries / falls back anyway."""
    URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    MODEL = "gemini-3.5-flash-lite"

    def _extra(self, json_mode: bool) -> dict:
        return {"reasoning_effort": "minimal"}  # least thinking = fastest + no tokens eaten


def _strip_fences(text: str) -> str:
    """'```json {...} ```' -> '{...}' (some models wrap JSON in markdown)."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1].rsplit("```", 1)[0]
    return t.strip()


PROVIDERS = {"groq": GroqLLM, "gemini": GeminiLLM}


def get_llm() -> LLM:
    """Data Flow: settings.llm_provider (+ key) -> real client | FakeLLM (no key = no calls)."""
    cls = PROVIDERS.get(settings.llm_provider)
    if cls and settings.llm_api_key:
        return cls()
    if cls:
        log.warning("LLM_PROVIDER=%s but LLM_API_KEY is empty: using the fake LLM", settings.llm_provider)
    from app.llm.fake import FakeLLM
    return FakeLLM()
