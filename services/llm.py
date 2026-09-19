import os
from typing import Any
from groq import Groq
from dotenv import load_dotenv

load_dotenv()
_client = None
MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


def call_llm(messages: list[dict[str, Any]], json_mode: bool = True, temperature: float = 0.4) -> str:
    """Single entry point for all LLM calls."""
    global _client
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY is not set. Please set it in your .env file.")
    if _client is None or getattr(_client, "api_key", None) != api_key:
        _client = Groq(api_key=api_key)

    kwargs: dict[str, Any] = {
        "model": os.getenv("GROQ_MODEL", MODEL),
        "messages": messages,
        "temperature": temperature,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = _client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content or ""
