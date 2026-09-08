"""Tests for resolving the Codex Responses host model for image generation."""

from __future__ import annotations

import importlib


codex_plugin = importlib.import_module("plugins.image_gen.openai-codex")


_FALLBACK = "gpt-5.6-luna"


def _write_config(codex_home, content: str) -> None:
    (codex_home / "config.toml").write_text(content, encoding="utf-8")


def test_environment_model_overrides_config(tmp_path, monkeypatch):
    _write_config(tmp_path, 'model = "gpt-5.6-sol"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_CHAT_MODEL", "gpt-5.6-luna")

    assert codex_plugin._codex_chat_model() == "gpt-5.6-luna"


def test_config_model_is_used_without_environment_override(tmp_path, monkeypatch):
    _write_config(tmp_path, 'model = "gpt-5.6-terra"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_CHAT_MODEL", raising=False)

    assert codex_plugin._codex_chat_model() == "gpt-5.6-terra"


def test_missing_config_uses_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_CHAT_MODEL", raising=False)

    assert codex_plugin._codex_chat_model() == _FALLBACK


def test_config_without_model_uses_fallback(tmp_path, monkeypatch):
    _write_config(tmp_path, 'reasoning_effort = "high"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_CHAT_MODEL", raising=False)

    assert codex_plugin._codex_chat_model() == _FALLBACK


def test_config_read_error_uses_fallback(tmp_path, monkeypatch):
    invalid_codex_home = tmp_path / "not-a-directory"
    invalid_codex_home.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(invalid_codex_home))
    monkeypatch.delenv("CODEX_CHAT_MODEL", raising=False)

    assert codex_plugin._codex_chat_model() == _FALLBACK


def test_single_quoted_config_model_is_used(tmp_path, monkeypatch):
    _write_config(tmp_path, "model = 'gpt-5.4-mini'\n")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_CHAT_MODEL", raising=False)

    assert codex_plugin._codex_chat_model() == "gpt-5.4-mini"


def test_commented_model_line_is_ignored(tmp_path, monkeypatch):
    _write_config(tmp_path, '# model = "gpt-5.5"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_CHAT_MODEL", raising=False)

    assert codex_plugin._codex_chat_model() == _FALLBACK
