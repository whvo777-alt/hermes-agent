"""Regression tests for optional AI images in free H2 sections."""

from __future__ import annotations

from pathlib import Path

from agent.content.images.section_ai_images import build_section_ai_images


class _FakeProvider:
    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def generate(self, prompt: str, *, aspect_ratio: str) -> dict:
        self.calls.append((prompt, aspect_ratio))
        if self.error is not None:
            raise self.error
        return {"success": True, "image": "fake://section-image"}


def _configure_success(monkeypatch, tmp_path: Path, *, error: Exception | None = None):
    import agent.content.images.ai_hero_image as ai_hero_image

    provider = _FakeProvider(error=error)
    prompts: list[dict] = []

    monkeypatch.setattr(ai_hero_image, "_is_ai_hero_enabled", lambda: True)
    monkeypatch.setattr(ai_hero_image, "_resolve_provider", lambda: provider)
    monkeypatch.setattr(
        ai_hero_image,
        "build_hero_prompt",
        lambda **kwargs: prompts.append(kwargs) or "fake prompt",
    )

    def fake_materialize(_image_ref: str, *, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        saved = dest_dir / "hero_ai.jpg"
        saved.write_bytes(b"fake image")
        return saved

    monkeypatch.setattr(ai_hero_image, "_materialize_provider_image", fake_materialize)
    return provider, prompts


def _build(markdown: str, tmp_path: Path, **kwargs):
    return build_section_ai_images(
        markdown,
        out_dir=tmp_path / "images",
        category_id="health",
        category_name="건강",
        style_seed="test-seed",
        **kwargs,
    )


def test_card_headings_are_excluded(monkeypatch, tmp_path):
    _configure_success(monkeypatch, tmp_path)

    results = _build(
        "## 첫 번째 대단원\n본문\n## 카드가 붙은 대단원\n본문\n## 세 번째 대단원\n본문",
        tmp_path,
        used_headings=["카드가 붙은 대단원"],
        max_count=2,
    )

    assert [result["heading"] for result in results] == ["첫 번째 대단원", "세 번째 대단원"]


def test_free_headings_are_selected_in_document_order_up_to_max_count(monkeypatch, tmp_path):
    _configure_success(monkeypatch, tmp_path)

    results = _build(
        "## 첫 번째\n본문\n## 두 번째\n본문\n## 세 번째\n본문",
        tmp_path,
        max_count=2,
    )

    assert [result["heading"] for result in results] == ["첫 번째", "두 번째"]


def test_result_has_all_image_keys_and_preserves_source(monkeypatch, tmp_path):
    _configure_success(monkeypatch, tmp_path)

    results = _build("## 물 마시기\n본문", tmp_path, max_count=1)

    assert len(results) == 1
    assert set(results[0]) == {"file", "heading", "alt", "style", "skin", "strip_ranges"}
    assert results[0]["strip_ranges"] == []
    assert results[0]["style"] == "ai_photo"


def test_missing_provider_returns_empty_list(monkeypatch, tmp_path):
    import agent.content.images.ai_hero_image as ai_hero_image

    monkeypatch.setattr(ai_hero_image, "_is_ai_hero_enabled", lambda: True)
    monkeypatch.setattr(ai_hero_image, "_resolve_provider", lambda: None)

    assert _build("## 물 마시기\n본문", tmp_path) == []


def test_provider_exception_returns_empty_list(monkeypatch, tmp_path):
    _configure_success(monkeypatch, tmp_path, error=RuntimeError("fake provider failure"))

    result = _build("## 물 마시기\n본문", tmp_path)

    assert result == []


def test_section_filename_does_not_overwrite_hero_and_prompt_uses_heading(
    monkeypatch, tmp_path
):
    provider, prompts = _configure_success(monkeypatch, tmp_path)

    heading = "집에서 물 마시는 방법"
    results = _build(f"## {heading}\n본문", tmp_path, max_count=1)

    assert Path(results[0]["file"]).name.startswith("section_ai_")
    assert "hero_ai" not in Path(results[0]["file"]).name
    assert prompts[0]["topic_title"] == heading
    assert provider.calls == [("fake prompt", "landscape")]
