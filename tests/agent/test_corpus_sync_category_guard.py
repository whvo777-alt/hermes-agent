"""Tests for constraining WordPress categories to known Hermes categories."""

from __future__ import annotations

import builtins

from agent.content.memory import corpus_sync


def test_known_category_slug_is_kept():
    assert corpus_sync._resolve_category([1], {1: "health"}, "self-dev") == "health"


def test_fallback_category_wins_when_listed():
    assert (
        corpus_sync._resolve_category(
            [1, 2], {1: "self-dev", 2: "health"}, "health"
        )
        == "health"
    )


def test_unknown_category_slugs_fall_back():
    for unknown in ("diet", "daily-health"):
        assert corpus_sync._resolve_category([1], {1: unknown}, "health") == "health"


def test_known_category_wins_among_unknown_categories():
    assert (
        corpus_sync._resolve_category(
            [1, 2], {1: "diet", 2: "self-dev"}, "health"
        )
        == "self-dev"
    )


def test_empty_category_list_uses_fallback():
    assert corpus_sync._resolve_category([], {}, "health") == "health"


def test_alias_is_normalized_before_category_guard():
    normalized = corpus_sync._normalize_wp_slug("건강")

    assert normalized == "health"
    assert corpus_sync._resolve_category([1], {1: normalized}, "self-dev") == "health"


def test_ignored_category_is_filtered_before_category_guard():
    normalized = corpus_sync._normalize_wp_slug("uncategorized")

    assert normalized == ""
    assert corpus_sync._resolve_category([1], {1: normalized}, "health") == "health"


def test_category_lookup_failure_uses_fallback(monkeypatch):
    original_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "agent.content.config.categories":
            raise ImportError("category definitions unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)

    assert corpus_sync._resolve_category([1], {1: "diet"}, "health") == "health"
