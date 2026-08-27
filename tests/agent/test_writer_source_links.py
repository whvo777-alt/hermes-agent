"""Regression coverage for verified source links in the writing prompt."""

from unittest.mock import patch

from agent.content.writing.writer import write_blog_post


def _capture_user_prompt(**extra):
    captured = {}

    def _fake_call_llm(*, system: str, user: str) -> str:
        captured["user"] = user
        return "# 제목\n\n본문\n\n---META---\nURL slug: t\n"

    kwargs = {
        "platform_id": "wordpress",
        "platform_label": "테스트",
        "category_id": "health",
        "category_name": "건강",
        "target_audience": "테스트 독자",
        "tone": "테스트 톤",
        "topic_title": "테스트 주제",
        "topic_keywords": ["테스트"],
        "category_keywords": ["건강"],
        "caution_hints": [],
        "current_date": "2026-08-23",
        "research_content": "리서치 내용",
        "planning_content": "기획 내용",
    }
    kwargs.update(extra)

    with patch("agent.content.writing.writer.call_llm", side_effect=_fake_call_llm):
        write_blog_post(**kwargs)

    return captured["user"]


def test_source_links_are_added_to_the_prompt():
    prompt = _capture_user_prompt(
        source_links=[
            {"title": "질병관리청 안내", "url": "https://kdca.go.kr/page"},
        ]
    )

    assert "[쓸 수 있는 바깥 자료]" in prompt
    assert "- 질병관리청 안내 : https://kdca.go.kr/page" in prompt


def test_prompt_forbids_urls_outside_the_source_list():
    prompt = _capture_user_prompt(
        source_links=[{"title": "자료", "url": "https://example.com/source"}]
    )

    assert "목록에 없는 주소는 만들지 않는다." in prompt


def test_empty_source_links_omit_the_source_block():
    prompt = _capture_user_prompt(source_links=[])

    assert "[쓸 수 있는 바깥 자료]" not in prompt
    assert "목록에 없는 주소는 만들지 않는다." not in prompt


def test_source_links_are_optional():
    prompt = _capture_user_prompt()

    assert "카테고리: 건강" in prompt
    assert "[쓸 수 있는 바깥 자료]" not in prompt


def test_source_links_are_limited_to_six_items():
    links = [
        {"title": f"자료 {index}", "url": f"https://example.com/{index}"}
        for index in range(7)
    ]

    prompt = _capture_user_prompt(source_links=links)

    for link in links[:6]:
        assert f"- {link['title']} : {link['url']}" in prompt
    assert f"- {links[6]['title']} : {links[6]['url']}" not in prompt


def test_source_links_are_placed_on_their_own_line():
    prompt = _capture_user_prompt(
        source_links=[{"title": "자료", "url": "https://example.com/source"}]
    )

    assert "설명한 문단 바로 다음 줄에 링크만 홀로 놓는다. 문장 안에 섞지 않는다." in prompt
    assert "글 끝에 몰아넣지 않는다." in prompt
