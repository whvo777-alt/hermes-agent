"""Self-contained LLM caller for the content pipeline (Research/Planning/
Writing/rewrite only) — ported from multi-content-pipeline/utils/llm.js.

Deliberately independent of Hermes' main chat transports
(agent/transports/*): this is a simple, non-streaming, single-turn
completion call used only by the content generation stages, so touching it
carries zero risk to Hermes' primary interactive chat path.

Provider selected by CONTENT_LLM_PROVIDER env var:
  'hermes' | 'anthropic' | 'openai' | 'nexos' | 'mock' (tests only, must be explicit).

  hermes — uses Hermes' configured main model (agent.auxiliary_client),
           e.g. openai-codex / Anthropic as set in config.yaml.

Fail-closed policy:
- Unset / unknown provider → raise (never silently default to mock).
- Missing API key for a real provider → raise.
- HTTP/API failure → raise (never fall back to mock).
Never logs or returns raw API keys.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Dict

import httpx

_DEFAULT_TIMEOUT = float(os.environ.get("CONTENT_LLM_TIMEOUT", "180"))
_REAL_PROVIDERS = frozenset({"hermes", "anthropic", "openai", "nexos"})
_ALLOWED_PROVIDERS = _REAL_PROVIDERS | frozenset({"mock"})
_NEXOS_BASE_URL = "https://api.nexos.ai/v1"

_stats_lock = threading.Lock()
_CALL_STATS: Dict[str, Any] = {
    "provider": None,
    "calls": 0,
    "mock_calls": 0,
    "real_calls": 0,
    "last_error": None,
}


class LLMClientError(RuntimeError):
    pass


def reset_call_stats() -> None:
    with _stats_lock:
        _CALL_STATS.update(
            provider=None,
            calls=0,
            mock_calls=0,
            real_calls=0,
            last_error=None,
        )


def get_call_stats() -> Dict[str, Any]:
    with _stats_lock:
        return dict(_CALL_STATS)


def _record_call(*, provider: str, mock: bool) -> None:
    logging.getLogger(__name__).info("LLM call provider=%s mock=%s", provider, mock)
    with _stats_lock:
        _CALL_STATS["provider"] = provider
        _CALL_STATS["calls"] += 1
        if mock:
            _CALL_STATS["mock_calls"] += 1
        else:
            _CALL_STATS["real_calls"] += 1


def _record_error(exc: BaseException) -> None:
    with _stats_lock:
        _CALL_STATS["last_error"] = f"{type(exc).__name__}: {exc}"


def resolve_content_llm_provider() -> str:
    """Return the configured provider or raise — never imply mock by default."""
    raw = os.environ.get("CONTENT_LLM_PROVIDER")
    if raw is None or not str(raw).strip():
        raise LLMClientError(
            "CONTENT_LLM_PROVIDER가 설정되지 않았습니다. "
            "원고 생성을 중단합니다. "
            "명시적으로 hermes | anthropic | openai | nexos | mock(테스트 전용) 중 하나를 설정하세요. "
            "미설정 시 mock 자동 선택은 비활성화되어 있습니다."
        )
    provider = str(raw).strip().lower()
    if provider not in _ALLOWED_PROVIDERS:
        raise LLMClientError(
            f"지원하지 않는 CONTENT_LLM_PROVIDER={provider!r}. "
            f"허용 값: {', '.join(sorted(_ALLOWED_PROVIDERS))}."
        )
    return provider


def _mock_content(system: str, user: str) -> str:
    return (
        f"[MOCK CONTENT — CONTENT_LLM_PROVIDER=mock]\n"
        f"system_len={len(system)} user_len={len(user)}\n"
        "테스트 전용 mock 응답입니다. 실제 발행 경로에서는 "
        "CONTENT_LLM_PROVIDER=hermes|anthropic|openai|nexos 를 사용하세요."
    )


def _call_hermes(system: str, user: str) -> str:
    """Use Hermes' main configured model via auxiliary_client (no mock fallback)."""
    try:
        from agent.auxiliary_client import call_llm as hermes_call_llm
    except ImportError as exc:
        raise LLMClientError(
            "CONTENT_LLM_PROVIDER=hermes 이지만 agent.auxiliary_client를 불러올 수 없습니다. "
            "mock으로 자동 전환하지 않습니다."
        ) from exc

    max_tokens = int(os.environ.get("CONTENT_LLM_MAX_TOKENS", "4000"))
    try:
        response = hermes_call_llm(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            timeout=_DEFAULT_TIMEOUT,
        )
    except Exception as exc:
        raise LLMClientError(
            f"Hermes 메인 모델 호출 실패: {type(exc).__name__}: {exc}. "
            "mock으로 자동 전환하지 않습니다."
        ) from exc

    choices = getattr(response, "choices", None) or []
    if not choices:
        raise LLMClientError("Hermes 메인 모델이 빈 choices를 반환했습니다.")
    message = getattr(choices[0], "message", None)
    text = (getattr(message, "content", None) or "").strip()
    if not text:
        raise LLMClientError("Hermes 메인 모델이 빈 응답을 반환했습니다.")
    return text


def _call_anthropic(system: str, user: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMClientError(
            "CONTENT_LLM_PROVIDER=anthropic 이지만 ANTHROPIC_API_KEY가 없습니다. "
            "키를 설정하거나 다른 실제 provider를 지정하세요. mock으로 자동 전환하지 않습니다."
        )

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
        raise LLMClientError(
            f"Anthropic API 실패: {response.status_code} {response.text[:300]}"
        )

    data = response.json()
    blocks = data.get("content") or []
    text = "\n".join(block.get("text", "") for block in blocks).strip()
    if not text:
        raise LLMClientError("Anthropic API가 빈 응답을 반환했습니다.")
    return text


def _call_openai_compatible(
    *,
    system: str,
    user: str,
    api_key: str,
    base_url: str,
    model: str,
    provider_label: str,
) -> str:
    max_tokens = int(os.environ.get("CONTENT_LLM_MAX_TOKENS", "4000"))
    endpoint = base_url.rstrip("/") + "/chat/completions"

    response = httpx.post(
        endpoint,
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {api_key}",
        },
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
        raise LLMClientError(
            f"{provider_label} API 실패: {response.status_code} {response.text[:300]}"
        )

    data = response.json()
    choices = data.get("choices") or []
    text = (
        (choices[0].get("message", {}).get("content") or "").strip()
        if choices
        else ""
    )
    if not text:
        raise LLMClientError(f"{provider_label} API가 빈 응답을 반환했습니다.")
    return text


def _call_openai(system: str, user: str) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise LLMClientError(
            "CONTENT_LLM_PROVIDER=openai 이지만 OPENAI_API_KEY가 없습니다. "
            "키를 설정하거나 다른 실제 provider를 지정하세요. mock으로 자동 전환하지 않습니다."
        )

    model = os.environ.get("CONTENT_OPENAI_MODEL", "gpt-4o-mini")
    base_url = os.environ.get("CONTENT_OPENAI_BASE_URL", "https://api.openai.com/v1")
    return _call_openai_compatible(
        system=system,
        user=user,
        api_key=api_key,
        base_url=base_url,
        model=model,
        provider_label="OpenAI",
    )


def _call_nexos(system: str, user: str) -> str:
    api_key = os.environ.get("NEXOS_API_KEY")
    if not api_key:
        raise LLMClientError(
            "CONTENT_LLM_PROVIDER=nexos 이지만 NEXOS_API_KEY가 없습니다. "
            "키를 설정하거나 다른 실제 provider를 지정하세요. mock으로 자동 전환하지 않습니다."
        )

    model = os.environ.get(
        "CONTENT_NEXOS_MODEL",
        os.environ.get("CONTENT_OPENAI_MODEL", "Claude Sonnet 4.6"),
    )
    base_url = os.environ.get("CONTENT_NEXOS_BASE_URL", _NEXOS_BASE_URL)
    return _call_openai_compatible(
        system=system,
        user=user,
        api_key=api_key,
        base_url=base_url,
        model=model,
        provider_label="Nexos",
    )


def call_llm(*, system: str, user: str) -> str:
    """Single-turn completion call for Research/Planning/Writing/rewrite."""
    try:
        provider = resolve_content_llm_provider()
        if provider == "mock":
            _record_call(provider=provider, mock=True)
            return _mock_content(system, user)
        if provider == "hermes":
            text = _call_hermes(system, user)
        elif provider == "anthropic":
            text = _call_anthropic(system, user)
        elif provider == "openai":
            text = _call_openai(system, user)
        elif provider == "nexos":
            text = _call_nexos(system, user)
        else:
            raise LLMClientError(f"내부 오류: 처리되지 않은 provider={provider!r}")

        if "[MOCK CONTENT" in text:
            raise LLMClientError(
                f"CONTENT_LLM_PROVIDER={provider} 인데 mock 마커가 응답에 포함되었습니다. "
                "mock fallback이 감지되어 원고 생성을 중단합니다."
            )
        _record_call(provider=provider, mock=False)
        return text
    except LLMClientError as exc:
        _record_error(exc)
        raise
    except Exception as exc:
        wrapped = LLMClientError(
            f"콘텐츠 LLM 호출 실패 (provider={os.environ.get('CONTENT_LLM_PROVIDER', '')!r}): "
            f"{type(exc).__name__}: {exc}. mock으로 자동 전환하지 않습니다."
        )
        _record_error(wrapped)
        raise wrapped from exc


_SECRET_PATTERNS = [
    (re.compile(r"sk-[a-zA-Z0-9_-]{12,}"), "[MASKED_KEY]"),
    (
        re.compile(
            r"(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
            r"bot[_-]?token|webhook|password|secret)(\s*[:=]\s*)([^\s`'\"<>]+)",
            re.I,
        ),
        r"\1\2[MASKED_SECRET]",
    ),
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
