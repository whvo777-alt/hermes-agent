"""Regression tests for body AI infographic selection and fallback."""

from __future__ import annotations

from pathlib import Path

import agent.content.images.section_infographics as section_infographics
from agent.content.images.section_ai_images import build_section_ai_images
from agent.content.images.section_infographics import InfographicSpec


class _FakeProvider:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def generate(self, prompt: str, *, aspect_ratio: str) -> dict:
        self.calls.append((prompt, aspect_ratio))
        return {"success": True, "image": "fake://section-image"}


def _configure(monkeypatch, tmp_path: Path):
    import agent.content.images.ai_hero_image as ai_hero_image

    provider = _FakeProvider()
    materialize_calls: list[bool] = []
    hero_prompt_calls: list[dict] = []

    monkeypatch.setattr(ai_hero_image, "_is_ai_hero_enabled", lambda: True)
    monkeypatch.setattr(ai_hero_image, "_resolve_provider", lambda: provider)
    monkeypatch.setattr(
        ai_hero_image,
        "build_hero_prompt",
        lambda **kwargs: hero_prompt_calls.append(kwargs) or "photo prompt",
    )

    def fake_materialize(
        _image_ref: str, *, dest_dir: Path, enforce_landscape: bool = True
    ) -> Path:
        materialize_calls.append(enforce_landscape)
        dest_dir.mkdir(parents=True, exist_ok=True)
        staged = dest_dir / "hero_ai.jpg"
        staged.write_bytes(b"fake image")
        return staged

    monkeypatch.setattr(ai_hero_image, "_materialize_provider_image", fake_materialize)
    return provider, materialize_calls, hero_prompt_calls


def _install_spec(monkeypatch, spec: InfographicSpec):
    monkeypatch.setattr(
        section_infographics,
        "extract_infographic_specs",
        lambda _markdown, **_kwargs: [spec],
    )


def _build(markdown: str, tmp_path: Path, **kwargs):
    return build_section_ai_images(
        markdown,
        out_dir=tmp_path / "images",
        category_id="health",
        category_name="건강",
        style_seed="test-seed",
        **kwargs,
    )


def test_data_bearing_heading_uses_its_text_and_portrait(monkeypatch, tmp_path):
    provider, materialize_calls, hero_prompt_calls = _configure(monkeypatch, tmp_path)
    _install_spec(
        monkeypatch,
        InfographicSpec(
            heading="자세 점검",
            display_title="자세 점검",
            style="checklist",
            items=["첫 번째 기준", "두 번째 기준"],
        ),
    )

    result = _build("## 자세 점검\n본문", tmp_path, max_count=1)

    assert len(result) == 1
    assert "첫 번째 기준" in provider.calls[0][0]
    assert provider.calls[0][1] == "portrait"
    assert materialize_calls == [False]
    assert hero_prompt_calls == []


def test_heading_without_spec_keeps_photo_landscape_path(monkeypatch, tmp_path):
    provider, materialize_calls, hero_prompt_calls = _configure(monkeypatch, tmp_path)
    _install_spec(
        monkeypatch,
        InfographicSpec(
            heading="다른 대단원",
            display_title="다른 대단원",
            style="checklist",
            items=["다른 첫 기준", "다른 두 번째 기준"],
        ),
    )

    result = _build("## 사진 대단원\n본문", tmp_path, max_count=1)

    assert len(result) == 1
    assert provider.calls[0] == ("photo prompt", "landscape")
    assert hero_prompt_calls[0]["topic_title"] == "사진 대단원"
    assert materialize_calls == [True]


def test_infographic_materialization_disables_landscape_crop(monkeypatch, tmp_path):
    provider, materialize_calls, _hero_prompt_calls = _configure(monkeypatch, tmp_path)
    _install_spec(
        monkeypatch,
        InfographicSpec(
            heading="비교 항목",
            display_title="비교 항목",
            style="checklist",
            items=["첫 기준", "둘째 기준"],
        ),
    )

    _build("## 비교 항목\n본문", tmp_path, max_count=1)

    assert provider.calls[0][1] == "portrait"
    assert materialize_calls == [False]


def test_photo_materialization_enables_landscape_crop(monkeypatch, tmp_path):
    provider, materialize_calls, _hero_prompt_calls = _configure(monkeypatch, tmp_path)

    _build("## 사진 항목\n본문", tmp_path, max_count=1)

    assert provider.calls[0][1] == "landscape"
    assert materialize_calls == [True]


def test_extractor_failure_falls_back_to_photo(monkeypatch, tmp_path):
    provider, materialize_calls, hero_prompt_calls = _configure(monkeypatch, tmp_path)

    def broken_extractor(_markdown, **_kwargs):
        raise RuntimeError("fake extractor failure")

    monkeypatch.setattr(section_infographics, "extract_infographic_specs", broken_extractor)

    result = _build("## 안전한 사진 대단원\n본문", tmp_path, max_count=1)

    assert len(result) == 1
    assert provider.calls[0] == ("photo prompt", "landscape")
    assert hero_prompt_calls[0]["topic_title"] == "안전한 사진 대단원"
    assert materialize_calls == [True]


def test_empty_extraction_result_also_keeps_photo_path(monkeypatch, tmp_path):
    provider, materialize_calls, hero_prompt_calls = _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(section_infographics, "extract_infographic_specs", lambda *_args, **_kwargs: [])

    result = _build("## 목록 없는 대단원\n본문", tmp_path, max_count=1)

    assert len(result) == 1
    assert provider.calls[0][1] == "landscape"
    assert hero_prompt_calls[0]["topic_title"] == "목록 없는 대단원"
    assert materialize_calls == [True]