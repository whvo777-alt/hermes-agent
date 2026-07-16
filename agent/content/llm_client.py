"""Self-contained LLM caller for the content pipeline (Research/Planning/
Writing/rewrite only) — ported from multi-content-pipeline/utils/llm.js.

Deliberately independent of Hermes' main chat transports
(agent/transports/*): this is a simple, non-streaming, single-turn
completion call used only by the content generation stages, so touching it
carries zero risk to Hermes' primary interactive chat path.

Provider selected by CONTENT_LLM_PROVIDER env var (mirrors R2's
LLM_PROVIDER): 'mock' (default, safe for tests) | 'anthropic' | 'openai'.
Never logs or returns raw API keys.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

import httpx

_DEFAULT_TIMEOUT = 60.0


class LLMClientError(RuntimeError):
    pass


def _mock_content(system: str, user: str) -> str:
    return (
        f"[MOCK CONTENT — CONTENT_LLM_PROVIDER=mock]\n"
        f"system_len={len(system)} user_len={len(user)}\n"
        "실제 발행 전 반드시 CONTENT_LLM_PROVIDER를 anthropic 또는 openai로 설정하세요."
    )


def _call_anthropic(system: str, user: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMClientError("ANTHROPIC_API_KEY가 없습니다. 환경변수를 설정하거나 CONTENT_LLM_PROVIDER=mock으로 설정하세요.")

    model = os.environ.get("CONTENT_ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
    max_tokens = int(os.environ.get("CONTENT_LLM_MAX_TOKENS", "4000"))

    response = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=_DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise LLMClientError(f"Anthropic API 실패: {response.status_code} {response.text[:300]}")

    data = response.json()
    blocks = data.get("content") or []
    return "\n".join(block.get("text", "") for block in blocks).strip()


def _call_openai(system: str, user: str) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise LLMClientError("OPENAI_API_KEY가 없습니다. 환경변수를 설정하거나 CONTENT_LLM_PROVIDER=mock으로 설정하세요.")

    model = os.environ.get("CONTENT_OPENAI_MODEL", "gpt-4o-mini")
    max_tokens = int(os.environ.get("CONTENT_LLM_MAX_TOKENS", "4000"))

    response = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"content-type": "application/json", "authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=_DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise LLMClientError(f"OpenAI API 실패: {response.status_code} {response.text[:300]}")

    data = response.json()
    choices = data.get("choices") or []
    return (choices[0].get("message", {}).get("content") or "").strip() if choices else ""


def call_llm(*, system: str, user: str) -> str:
    """Single-turn completion call for Research/Planning/Writing/rewrite."""
    provider = os.environ.get("CONTENT_LLM_PROVIDER", "mock").lower()
    if provider == "anthropic":
        return _call_anthropic(system, user)
    if provider == "openai":
        return _call_openai(system, user)
    return _mock_content(system, user)


_SECRET_PATTERNS = [
    (re.compile(r"sk-[a-zA-Z0-9_-]{12,}"), "[MASKED_KEY]"),
    (re.compile(r"(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|bot[_-]?token|webhook|password|secret)(\s*[:=]\s*)([^\s`'\"<>]+)", re.I), r"\1\2[MASKED_SECRET]"),
    (re.compile(r"(Bearer\s+)[a-zA-Z0-9._~+/=-]{16,}"), r"\1[MASKED_TOKEN]"),
]


def redact_secrets(text: str) -> str:
    """Mask likely-secret substrings before logging/snapshotting a prompt."""
    masked = str(text or "")
    for pattern, replacement in _SECRET_PATTERNS:
        masked = pattern.sub(replacement, masked)
    for key, value in os.environ.items():
        if not re.search(r"(KEY|TOKEN|SECRET|PASSWORD|WEBHOOK|CREDENTIAL|AUTH)", key, re.I):
            continue
        if not value or len(value) < 8:
            continue
        masked = masked.replace(value, f"[MASKED_ENV:{key}]")
    return masked
