"""Optional language-model calls. OFF by default (LLM_PROVIDER=none).

The product never depends on a model: every place that can use one has a
deterministic rule-based path that runs first. When a model is configured, text is
redacted before it leaves the server (names, card numbers, phone numbers, long IDs).
"""
from __future__ import annotations

import json
import re

import httpx

from .config import settings


class LLMUnavailable(RuntimeError):
    pass


def enabled() -> bool:
    return settings.llm_provider == "anthropic" and bool(settings.anthropic_api_key)


_PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\d)")
_LONG_ID = re.compile(r"(?<![\w])[A-Z0-9]{0,4}\d{6,}[A-Z0-9]*(?![\w])")
_AADHAAR = re.compile(r"(?<!\d)\d{4}\s\d{4}\s\d{4}(?!\d)")


def redact(text: str, names: list[str] | None = None) -> str:
    out = text or ""
    for n in sorted({n for n in (names or []) if n and len(n) > 2}, key=len, reverse=True):
        for part in [n, *n.split()]:
            if len(part) > 2:
                out = re.sub(re.escape(part), "[NAME]", out, flags=re.I)
    out = _AADHAAR.sub("[ID]", out)
    out = _PHONE.sub("[PHONE]", out)
    out = _LONG_ID.sub("[ID]", out)
    return out


def complete_json(system: str, user: str, max_tokens: int = 800) -> dict:
    if not enabled():
        raise LLMUnavailable("No language model configured.")
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": settings.anthropic_model, "max_tokens": max_tokens, "system": system,
              "messages": [{"role": "user", "content": user}]},
        timeout=60,
    )
    resp.raise_for_status()
    text = "".join(b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise LLMUnavailable("Model did not return JSON.")
    return json.loads(m.group(0))
