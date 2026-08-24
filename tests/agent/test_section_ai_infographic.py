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


def _install_specs(monkeypatch, specs: list[InfographicSpec]):
    monkeypatch.setattr(
        section_infographics,
        "extract_infographic_specs",
        lambda _markdown, **_kwargs: list(specs),
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


def test_extractor_receives_the_section_ai_style_seed(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    calls: list[dict] = []
    spec = InfographicSpec(
        heading="seed 확인",
        display_title="seed 확인",
        style="checklist",
        items=["첫 기준", "둘째 기준"],
    )

    def capture_extractor(_markdown, **kwargs):
        calls.append(kwargs)
        return [spec]

    monkeypatch.setattr(section_infographics, "extract_infographic_specs", capture_extractor)

    _build("## seed 확인\n본문", tmp_path, max_count=1)

    assert calls == [{"max_count": 9999, "style_seed": "test-seed"}]


def test_card_consumed_headings_can_still_create_infographics(monkeypatch, tmp_path):
    provider, _materialize_calls, _hero_prompt_calls = _configure(monkeypatch, tmp_path)
    specs = [
        InfographicSpec(
            heading="첫 번째 카드 대단원",
            display_title="첫 번째 카드 대단원",
            style="checklist",
            items=["첫 기준", "둘째 기준"],
        ),
        InfographicSpec(
            heading="두 번째 카드 대단원",
            display_title="두 번째 카드 대단원",
            style="checklist",
            items=["셋째 기준", "넷째 기준"],
        ),
    ]
    _install_specs(monkeypatch, specs)

    results = _build(
        "## 첫 번째 카드 대단원\n본문\n## 두 번째 카드 대단원\n본문",
        tmp_path,
        used_headings=[spec.heading for spec in specs],
        max_count=2,
    )

    assert [result["heading"] for result in results] == [
        "첫 번째 카드 대단원",
        "두 번째 카드 대단원",
    ]
    assert [aspect_ratio for _prompt, aspect_ratio in provider.calls] == [
        "portrait",
        "portrait",
    ]


def test_headings_with_more_material_are_ranked_first(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    specs = [
        InfographicSpec(
            heading="재료 적은 대단원",
            display_title="재료 적은 대단원",
            style="checklist",
            items=["한 줄"],
        ),
        InfographicSpec(
            heading="재료 많은 대단원",
            display_title="재료 많은 대단원",
            style="checklist",
            items=["첫 줄", "둘째 줄", "셋째 줄", "넷째 줄"],
        ),
    ]
    _install_specs(monkeypatch, specs)

    results = _build(
        "## 재료 적은 대단원\n본문\n## 재료 많은 대단원\n본문",
        tmp_path,
        max_count=1,
    )

    assert [result["heading"] for result in results] == ["재료 많은 대단원"]


def test_empty_infographic_prompt_removes_candidate(monkeypatch, tmp_path):
    provider, _materialize_calls, hero_prompt_calls = _configure(monkeypatch, tmp_path)
    spec = InfographicSpec(
        heading="프롬프트 없는 대단원",
        display_title="프롬프트 없는 대단원",
        style="checklist",
        items=["첫 줄", "둘째 줄"],
    )
    _install_spec(monkeypatch, spec)

    import agent.content.images.infographic_prompt as infographic_prompt

    monkeypatch.setattr(infographic_prompt, "build_infographic_prompt", lambda *args, **kwargs: "")

    results = _build("## 프롬프트 없는 대단원\n본문", tmp_path, max_count=1)

    assert results == []
    assert provider.calls == []
    assert hero_prompt_calls == []


def test_photo_fallback_fills_when_infographic_candidates_are_short(
    monkeypatch, tmp_path
):
    provider, _materialize_calls, _hero_prompt_calls = _configure(monkeypatch, tmp_path)
    spec = InfographicSpec(
        heading="재료 있는 마지막 대단원",
        display_title="재료 있는 마지막 대단원",
        style="checklist",
        items=["첫 줄", "둘째 줄"],
    )
    _install_spec(monkeypatch, spec)

    results = _build(
        "## 사진 첫 대단원\n본문\n## 사진 둘째 대단원\n본문\n"
        "## 재료 있는 마지막 대단원\n본문",
        tmp_path,
        max_count=2,
    )

    assert [result["heading"] for result in results] == [
        "재료 있는 마지막 대단원",
        "사진 첫 대단원",
    ]
    assert [aspect_ratio for _prompt, aspect_ratio in provider.calls] == [
        "portrait",
        "landscape",
    ]


def test_section_ai_images_never_exceed_max_count(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    specs = [
        InfographicSpec(
            heading=f"대단원 {index}",
            display_title=f"대단원 {index}",
            style="checklist",
            items=[f"기준 {index}", f"추가 기준 {index}"],
        )
        for index in range(3)
    ]
    _install_specs(monkeypatch, specs)

    results = _build(
        "## 대단원 0\n본문\n## 대단원 1\n본문\n## 대단원 2\n본문",
        tmp_path,
        max_count=2,
    )

    assert len(results) == 2


def test_skipped_headings_are_not_ai_image_candidates(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    specs = [
        InfographicSpec(
            heading="FAQ",
            display_title="FAQ",
            style="checklist",
            items=["질문", "답변"],
        ),
        InfographicSpec(
            heading="실제 대단원",
            display_title="실제 대단원",
            style="checklist",
            items=["기준", "추가 기준"],
        ),
    ]
    _install_specs(monkeypatch, specs)

    results = _build("## FAQ\n질문\n## 실제 대단원\n본문", tmp_path, max_count=2)

    assert [result["heading"] for result in results] == ["실제 대단원"]


def test_diverse_kind_is_selected_before_same_kind_tie(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    specs = [
        InfographicSpec(
            heading="체크리스트 많은 대단원",
            display_title="체크리스트 많은 대단원",
            style="checklist",
            items=["첫 기준", "둘째 기준", "셋째 기준", "넷째 기준", "다섯째 기준", "여섯째 기준"],
        ),
        InfographicSpec(
            heading="체크리스트 다음 대단원",
            display_title="체크리스트 다음 대단원",
            style="checklist",
            items=["첫 기준", "둘째 기준", "셋째 기준", "넷째 기준", "다섯째 기준"],
        ),
        InfographicSpec(
            heading="타임라인 대단원",
            display_title="타임라인 대단원",
            style="timeline",
            items=["첫 단계", "둘째 단계", "셋째 단계", "넷째 단계", "다섯째 단계"],
        ),
    ]
    _install_specs(monkeypatch, specs)

    results = _build(
        "## 체크리스트 많은 대단원\n본문\n"
        "## 체크리스트 다음 대단원\n본문\n## 타임라인 대단원\n본문",
        tmp_path,
        max_count=2,
    )

    assert [result["heading"] for result in results] == [
        "체크리스트 많은 대단원",
        "타임라인 대단원",
    ]


def test_same_kind_is_selected_twice_when_no_other_kind_exists(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    specs = [
        InfographicSpec(
            heading="첫 체크리스트 대단원",
            display_title="첫 체크리스트 대단원",
            style="checklist",
            items=["첫 기준", "둘째 기준", "셋째 기준"],
        ),
        InfographicSpec(
            heading="둘째 체크리스트 대단원",
            display_title="둘째 체크리스트 대단원",
            style="checklist",
            items=["넷째 기준", "다섯째 기준"],
        ),
    ]
    _install_specs(monkeypatch, specs)

    results = _build(
        "## 첫 체크리스트 대단원\n본문\n## 둘째 체크리스트 대단원\n본문",
        tmp_path,
        max_count=2,
    )

    assert [result["heading"] for result in results] == [
        "첫 체크리스트 대단원",
        "둘째 체크리스트 대단원",
    ]


def test_zero_material_candidate_is_selected_after_positive_material(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    specs = [
        InfographicSpec(
            heading="한 문장 인용 대단원",
            display_title="한 문장 인용 대단원",
            style="quote",
            quote_text="핵심이 되는 한 문장입니다.",
        ),
        InfographicSpec(
            heading="재료 있는 대단원",
            display_title="재료 있는 대단원",
            style="checklist",
            items=["첫 기준", "둘째 기준"],
        ),
    ]
    _install_specs(monkeypatch, specs)

    results = _build(
        "## 한 문장 인용 대단원\n본문\n## 재료 있는 대단원\n본문",
        tmp_path,
        max_count=2,
    )

    assert [result["heading"] for result in results] == [
        "재료 있는 대단원",
        "한 문장 인용 대단원",
    ]


def test_infographic_result_uses_content_alt(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    _install_spec(
        monkeypatch,
        InfographicSpec(
            heading="자세 점검",
            display_title="자세 점검",
            style="checklist",
            items=["첫 번째 기준", "두 번째 기준"],
        ),
    )

    results = _build("## 자세 점검\n본문", tmp_path, max_count=1)

    assert "첫 번째 기준" in results[0]["alt"]
    assert results[0]["alt"] != "자세 점검 관련 이미지"


def test_photo_result_keeps_existing_alt_fallback(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    _install_specs(monkeypatch, [])

    results = _build("## 사진 항목\n본문", tmp_path, max_count=1)

    assert results[0]["alt"] == "사진 항목 관련 이미지"


def test_alt_failure_keeps_image_and_existing_fallback(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    _install_spec(
        monkeypatch,
        InfographicSpec(
            heading="ALT 오류 대단원",
            display_title="ALT 오류 대단원",
            style="checklist",
            items=["첫 번째 기준", "두 번째 기준"],
        ),
    )

    import agent.content.images.infographic_prompt as infographic_prompt

    def broken_alt(_spec):
        raise RuntimeError("fake alt failure")

    monkeypatch.setattr(infographic_prompt, "build_infographic_alt", broken_alt)

    results = _build("## ALT 오류 대단원\n본문", tmp_path, max_count=1)

    assert len(results) == 1
    assert results[0]["alt"] == "ALT 오류 대단원 관련 이미지"
