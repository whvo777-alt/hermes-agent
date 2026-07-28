"""Tests for agent.credential_pool.get_dead_credentials() -- added so the
gateway housekeeping loop can alert on Discord only when a credential is
permanently STATUS_DEAD (needs `hermes auth add <provider>`), never on
STATUS_EXHAUSTED (self-heals via TTL + the next natural use -- alerting on
that would just be noise, confirmed by a real openai-codex token_expired
incident that self-healed without any action needed).
"""

from __future__ import annotations

import json
import time

import pytest


def _write_auth_store(tmp_path, payload: dict) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(json.dumps(payload, indent=2))


def test_dead_credential_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "cred-dead",
                        "label": "openai-codex-oauth-1",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:device_code",
                        "access_token": "***",
                        "refresh_token": "***",
                        "last_status": "dead",
                        "last_status_at": time.time(),
                        "last_error_code": 401,
                        "last_error_reason": "token_invalidated",
                        "last_error_message": "refresh token revoked",
                    },
                ],
            },
        },
    )
    from agent.credential_pool import get_dead_credentials

    dead = get_dead_credentials()
    matches = [d for d in dead if d["provider"] == "openai-codex"]
    assert len(matches) == 1
    assert matches[0]["label"] == "openai-codex-oauth-1"
    assert matches[0]["last_error_reason"] == "token_invalidated"


def test_exhausted_credential_is_not_reported(tmp_path, monkeypatch):
    """STATUS_EXHAUSTED (e.g. token_expired) self-heals -- must never show
    up here, or the alert would fire on the exact scenario that turned out
    to need no action at all."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "cred-exhausted",
                        "label": "openai-codex-oauth-1",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:device_code",
                        "access_token": "***",
                        "refresh_token": "***",
                        "last_status": "exhausted",
                        "last_status_at": time.time(),
                        "last_error_code": 401,
                        "last_error_reason": None,
                        "last_error_message": "token_expired",
                    },
                ],
            },
        },
    )
    from agent.credential_pool import get_dead_credentials

    dead = get_dead_credentials()
    assert not any(d["provider"] == "openai-codex" for d in dead)


def test_ok_credential_is_not_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(
        tmp_path,
        {
            "version": 1,
            "credential_pool": {
                "openai-codex": [
                    {
                        "id": "cred-ok",
                        "label": "openai-codex-oauth-1",
                        "auth_type": "oauth",
                        "priority": 0,
                        "source": "manual:device_code",
                        "access_token": "***",
                        "refresh_token": "***",
                        "last_status": "ok",
                        "last_status_at": None,
                    },
                ],
            },
        },
    )
    from agent.credential_pool import get_dead_credentials

    dead = get_dead_credentials()
    assert not any(d["provider"] == "openai-codex" for d in dead)


def test_no_pool_data_returns_empty_list(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    _write_auth_store(tmp_path, {"version": 1, "credential_pool": {}})
    from agent.credential_pool import get_dead_credentials

    assert get_dead_credentials() == []
