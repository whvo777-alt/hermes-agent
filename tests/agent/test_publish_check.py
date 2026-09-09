from agent.content.publish_check import check_draft_html, format_check_report


def test_counts_six_img_tags_with_attributes():
    html = "".join(f'<img src="image-{index}.png" alt="그림">' for index in range(6))

    result = check_draft_html(html, expected_images=6, min_chars=0)

    assert result["image_count"] == 6
    assert result["hero_image"] is True


def test_counts_img_tag_without_attributes():
    result = check_draft_html("<img>", min_chars=0)

    assert result["image_count"] == 1
    assert result["hero_image"] is True


def test_does_not_count_image_or_imgsrc_as_img():
    html = '<image src="wrong.png"><imgsrc="wrong-too.png">'

    result = check_draft_html(html, min_chars=0)

    assert result["image_count"] == 0
    assert result["hero_image"] is False


def test_missing_hero_image_has_hero_and_empty_body_image_warnings():
    result = check_draft_html("<p>본문</p>", min_chars=0)

    assert result["hero_image"] is False
    assert "대표 이미지가 없습니다." in result["warnings"]
    assert "본문 그림이 없습니다." in result["warnings"]


def test_counts_only_internal_links():
    html = (
        '<a href="https://blog.example.com/inside">내부</a>'
        '<a href="https://outside.example.net/post">외부</a>'
    )

    result = check_draft_html(html, site_host="blog.example.com", min_chars=0)

    assert result["internal_links"] == 1


def test_empty_site_host_disables_internal_link_check_and_warning():
    html = '<a href="https://blog.example.com/inside">내부</a>'

    result = check_draft_html(html, site_host="", min_chars=0)

    assert result["internal_links"] == 0
    assert not any("내부 링크" in warning for warning in result["warnings"])


def test_blocker_is_returned_with_line_number():
    result = check_draft_html("<p>검토 필요</p>", min_chars=0)

    assert ("검토 필요", 1) in result["blockers"]
    assert any('"검토 필요" 1번째 줄' in warning for warning in result["warnings"])


def test_quoted_blocker_is_not_reported():
    result = check_draft_html('<p>“검토 필요”</p>', min_chars=0)

    assert result["blockers"] == []
    assert not any("금지 문구" in warning for warning in result["warnings"])


def test_text_chars_are_counted_after_tags_are_removed():
    result = check_draft_html("<p>안녕</p>", min_chars=0)

    assert result["text_chars"] == 2


def test_short_body_has_warning():
    result = check_draft_html("<p>짧은 글</p>", min_chars=10)

    assert result["text_chars"] == 4
    assert any("본문 글자 수가 너무 짧습니다:" in warning for warning in result["warnings"])


def test_fewer_expected_images_warns_but_more_images_do_not():
    fewer = check_draft_html("<img><img>", expected_images=3, min_chars=0)
    more = check_draft_html("<img><img><img>", expected_images=2, min_chars=0)

    assert any("원고보다 적습니다" in warning for warning in fewer["warnings"])
    assert not any("원고보다 적습니다" in warning for warning in more["warnings"])


def test_all_normal_checks_have_no_warnings():
    html = '<img src="hero.png"><a href="https://blog.example.com/old">이전 글</a>' + (
        "<p>" + ("본문 " * 600) + "</p>"
    )

    result = check_draft_html(
        html,
        site_host="blog.example.com",
        expected_images=1,
        min_chars=1500,
    )

    assert result["hero_image"] is True
    assert result["image_count"] == 1
    assert result["internal_links"] == 1
    assert result["blockers"] == []
    assert result["warnings"] == []


def test_formatted_report_is_at_most_1000_characters():
    result = {
        "hero_image": False,
        "image_count": 0,
        "expected_images": 0,
        "internal_links": 0,
        "blockers": [("검토 필요", 1)],
        "text_chars": 0,
        "warnings": ["대표 이미지가 없습니다."],
    }

    report = format_check_report(result, label="x" * 2000)

    assert len(report) <= 1000


def test_formatted_report_shows_only_three_blockers_and_remaining_count():
    result = check_draft_html(
        "\n".join(
            [
                "<p>검토 필요</p>",
                "<p>현재 제공된 자료</p>",
                "<p>발행 전</p>",
                "<p>별도 제공된 자료</p>",
                "<p>자료 없음</p>",
            ]
        ),
        min_chars=0,
    )
    assert len(result["blockers"]) == 5

    report = format_check_report(result, label="블로그스팟 초안")

    assert '"검토 필요" 1번째 줄' in report
    assert '"현재 제공된 자료" 2번째 줄' in report
    assert '"발행 전" 3번째 줄' in report
    assert "외 2건" in report
    assert '"별도 제공된 자료" 4번째 줄' not in report
    assert '"자료 없음" 5번째 줄' not in report
