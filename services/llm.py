import os
from typing import Any
from groq import Groq
from dotenv import load_dotenv

load_dotenv()
_client = None
_last_finish_reason: str | None = None
MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


def get_last_finish_reason() -> str | None:
    """Return finish_reason from the last LLM call."""
    return _last_finish_reason


def call_llm(
    messages: list[dict[str, Any]],
    json_mode: bool = True,
    temperature: float = 0.4,
    max_tokens: int | None = None,
) -> str:
    """Single entry point for all LLM calls."""
    global _client, _last_finish_reason
    _last_finish_reason = None
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY is not set. Please set it in your .env file.")
    if _client is None or getattr(_client, "api_key", None) != api_key:
        _client = Groq(api_key=api_key)

    model = os.getenv("GROQ_MODEL", MODEL)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if "gpt-oss" in model.lower():
        kwargs["reasoning_effort"] = "low"
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        resp = _client.chat.completions.create(**kwargs)
        if resp.choices:
            _last_finish_reason = resp.choices[0].finish_reason
        return resp.choices[0].message.content or ""
    except Exception as exc:
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            err_dict = body.get("error", {})
            _last_finish_reason = err_dict.get("code") or "error"
            failed_gen = err_dict.get("failed_generation")
            if isinstance(failed_gen, str) and failed_gen.strip():
                return failed_gen
        raise
