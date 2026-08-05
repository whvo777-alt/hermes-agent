"""Tests for quality_gate.py's WordPress/Blogspot length/H2-count thresholds.

First coverage for these numeric boundaries (none existed before). Added
alongside raising the writer prompt target from 4,000~6,500자/H2 4~5개 to
5,500~7,500자/H2 6~7개 -- the gate's own recommend()-tier thresholds moved
with it (_WP_BLOGSPOT_LENGTH: body_min_chars 3200->4700, body_max_chars
8500->9500, h2_min 3->5, h2_max 6->8), so these pin the new boundaries and
would catch a future edit that changes the prompt target without updating
the gate to match.
"""

from __future__ import annotations

from agent.content.quality.quality_gate import _WP_BLOGSPOT_LENGTH, run_quality_gate

_H2_BLOCK = "\n\n".join(f"## 섹션 {n}\n본문 문단입니다. 실제 내용이 이어집니다." for n in range(1, 7))


def _padded_body(target_chars: int) -> str:
    filler = "이 문장은 분량 테스트를 위한 반복되지 않는 자연스러운 설명 문단입니다. "
    body = f"# 제목\n\n{_H2_BLOCK}\n\n"
    while len(body) < target_chars:
        body += filler
    return body


def _image_ready() -> dict[str, str]:
    return {
        "status": "ready",
        "file": "title_card.png",
        "alt": "대표 이미지",
        "caption": "대표 이미지",
    }


def _image_gate_content(platform_id: str) -> str:
    content = (
        "---\n"
        f"platform: {platform_id}\n"
        "category_id: it-tech\n"
        "slug: image-gate-test\n"
        "---\n\n"
        "# 테스트 제목\n\n"
        "테스트 제목에 대한 기준을 설명합니다.\n\n"
        f"{_H2_BLOCK}\n\n"
    )
    paragraph_index = 0
    while len(content) < 6500:
        content += f"테스트 설명 문단 {paragraph_index}에서 판단 기준과 적용 방법을 정리합니다.\n\n"
        paragraph_index += 1
    return content


def _image_gate_result(platform_id: str, image):
    return run_quality_gate(
        topic_title="테스트",
        category_id="it-tech",
        platform_id=platform_id,
        content_type="blog",
        content=_image_gate_content(platform_id),
        image=image,
    )


def test_thresholds_match_documented_values():
    assert _WP_BLOGSPOT_LENGTH == {
        "body_min_chars": 4700,
        "body_max_chars": 9500,
        "h2_min": 5,
        "h2_max": 8,
    }


def test_body_below_new_floor_but_above_old_floor_now_flagged_short():
    """4,000자 is above the OLD floor (3,200) but below the NEW floor
    (4,700) -- must now trigger the short-body recommendation."""
    content = _padded_body(4000)
    result = run_quality_gate(
        topic_title="t", category_id="health", platform_id="wordpress",
        content_type="blog", content=content, image=None,
    )
    assert any("너무 짧거나" in w for w in result.warnings)


def test_body_between_old_and_new_ceiling_no_longer_flagged_long():
    """9,000자 was OVER the OLD ceiling (8,500) but is now UNDER the NEW
    ceiling (9,500) -- must no longer trigger the too-long recommendation."""
    content = _padded_body(9000)
    result = run_quality_gate(
        topic_title="t", category_id="health", platform_id="wordpress",
        content_type="blog", content=content, image=None,
    )
    assert not any("과도하게 깁니다" in w for w in result.warnings)


def test_body_within_new_target_range_is_not_flagged():
    content = _padded_body(6500)
    result = run_quality_gate(
        topic_title="t", category_id="health", platform_id="wordpress",
        content_type="blog", content=content, image=None,
    )
    assert not any("너무 짧거나" in w or "과도하게 깁니다" in w for w in result.warnings)


def test_h2_count_four_now_flagged_low_matches_new_ideal_six_to_seven():
    """4 H2s used to be inside the old ideal (4-5) and was never flagged;
    the new ideal is 6-7, so 4 must now trigger the 'H2가 적습니다' nag."""
    content = "# 제목\n\n" + "\n\n".join(f"## 섹션 {n}\n본문." for n in range(1, 5))
    content = content + "\n\n" + ("문단 내용입니다. " * 200)
    result = run_quality_gate(
        topic_title="t", category_id="health", platform_id="wordpress",
        content_type="blog", content=content, image=None,
    )
    assert any("H2가 적습니다" in w for w in result.warnings)


def test_naver_platform_is_unaffected_by_wordpress_blogspot_thresholds():
    """These thresholds are scoped to platform_id in {wordpress, blogspot}
    -- naver must never see the '너무 짧거나'/'과도하게 깁니다' nags."""
    content = _padded_body(4000)
    result = run_quality_gate(
        topic_title="t", category_id="it-tech", platform_id="naver",
        content_type="blog", content=content, image=None,
    )
    assert not any("너무 짧거나" in w or "과도하게 깁니다" in w for w in result.warnings)


def test_tistory_missing_hero_does_not_create_image_failure_or_score_drop():
    missing = _image_gate_result("tistory", None)
    ready = _image_gate_result("tistory", _image_ready())

    assert missing.passed == ready.passed
    assert missing.score == ready.score
    assert not any("대표 이미지" in message for message in [*missing.errors, *missing.warnings])


def test_wordpress_and_blogspot_missing_hero_still_fail_image_gate():
    for platform_id in ("wordpress", "blogspot"):
        missing = _image_gate_result(platform_id, None)
        ready = _image_gate_result(platform_id, _image_ready())

        assert not missing.passed
        assert any("대표 이미지" in error for error in missing.errors)
        assert missing.metadata["hardFailCount"] == ready.metadata["hardFailCount"] + 1
        assert missing.score <= ready.score - 30


def test_naver_missing_hero_keeps_existing_image_warning_behavior():
    result = _image_gate_result("naver", None)

    assert not any("본문 이미지" in warning for warning in result.warnings)
    assert any("대표 이미지 상태 확인 필요" in warning for warning in result.warnings)


def test_repeated_title_template_is_an_important_warning():
    content = (
        "# 수면 확인할 때 알아야 할 5가지 기준\n\n"
        "수면 주제의 판단 기준을 설명합니다.\n\n"
        "본문은 수면 습관과 회복 상황을 실제로 다룹니다.\n\n"
        "마지막으로 오늘 적용할 점검 방법을 정리합니다."
    )
    result = run_quality_gate(
        topic_title="수면",
        category_id="health",
        platform_id="naver",
        content_type="blog",
        content=content,
        image=None,
        recent_titles=[
            "운동 확인할 때 알아야 할 5가지 기준",
            "다이어트 확인할 때 알아야 할 7가지 기준",
        ],
    )

    assert any("TITLE_TEMPLATE_REPEATED" in warning for warning in result.warnings)
    assert any("TITLE_TEMPLATE_REPEATED" in warning for warning in result.metadata["importantWarnings"])


def test_one_matching_recent_title_does_not_trigger_template_warning():
    result = run_quality_gate(
        topic_title="수면",
        category_id="health",
        platform_id="naver",
        content_type="blog",
        content="# 수면 확인할 때 알아야 할 5가지 기준\n\n수면 기준을 설명합니다. " * 5,
        image=None,
        recent_titles=["운동 확인할 때 알아야 할 5가지 기준"],
    )

    assert not any("TITLE_TEMPLATE_REPEATED" in warning for warning in result.warnings)
