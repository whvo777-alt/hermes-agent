"""Regression coverage for standalone external-link boxes."""

from agent.content.markdown_html import markdown_to_html


SITE_URL = "https://cocoboll.com"


def test_public_site_root_becomes_blue_official_button():
    html = markdown_to_html("[복지로](https://bokjiro.go.kr/)", internal_host=SITE_URL)

    assert "background:#2563eb" in html
    assert "🔗 복지로 ↗" in html


def test_public_subpage_becomes_light_reference_card():
    html = markdown_to_html(
        "[복지 서비스 안내](https://bokjiro.go.kr/ssis-tbu/)",
        internal_host=SITE_URL,
    )

    assert "background:#f8fafc" in html
    assert "📚 복지 서비스 안내" in html
    assert "border:1px solid #cbd5e1" in html


def test_news_site_becomes_orange_news_card():
    html = markdown_to_html("[뉴스 기사](https://news.naver.com/article/1)", internal_host=SITE_URL)

    assert "background:#fff7ed" in html
    assert "border-left:5px solid #f97316" in html
    assert "📰 뉴스 기사 ↗" in html


def test_unknown_site_becomes_reference_card():
    html = markdown_to_html("[참고 자료](https://example.com/reference)", internal_host=SITE_URL)

    assert "background:#f8fafc" in html
    assert "📚 참고 자료" in html
    assert "background:#2563eb" not in html
    assert "background:#fff7ed" not in html


def test_all_external_boxes_open_in_a_new_window():
    markdown = "\n\n".join(
        (
            "[공식](https://bokjiro.go.kr/)",
            "[참고](https://example.com/reference)",
            "[뉴스](https://news.naver.com/article/1)",
        )
    )

    html = markdown_to_html(markdown, internal_host=SITE_URL)

    assert html.count('target="_blank" rel="noopener noreferrer"') == 3


def test_inline_external_link_is_not_a_box():
    html = markdown_to_html(
        "본문 안의 [참고 자료](https://example.com/reference)입니다.",
        internal_host=SITE_URL,
    )

    assert "display:block;padding:15px 20px" not in html
    assert "display:block;padding:16px 20px" not in html
    assert 'target="_blank" rel="noopener noreferrer"' in html


def test_internal_link_keeps_the_existing_gray_card():
    html = markdown_to_html(
        f"[내부 글]({SITE_URL}/inside)",
        internal_host="cocoboll.com",
    )

    assert "background:#f8f9fa" in html
    assert "border:1px solid #e5e7eb" in html
    assert 'target="_blank"' not in html
    assert "📚" not in html


def test_external_box_does_not_add_a_generic_call_to_action():
    html = markdown_to_html(
        "[자료 제목](https://example.com/reference)",
        internal_host=SITE_URL,
    )

    assert "자료 제목" in html
    assert "바로가기" not in html


def test_malformed_external_url_does_not_raise():
    html = markdown_to_html("[이상한 주소](https://[bad)", internal_host=SITE_URL)

    assert "이상한 주소" in html


def test_external_box_is_rendered_when_internal_host_is_configured():
    html = markdown_to_html(
        "[참고 자료](https://example.com/reference)",
        internal_host=SITE_URL,
    )

    assert "display:block;padding:16px 20px;margin:18px 0" in html
    assert 'target="_blank" rel="noopener noreferrer"' in html
