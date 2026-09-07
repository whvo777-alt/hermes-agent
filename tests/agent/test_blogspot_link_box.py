"""Regression tests for Blogspot internal-link and reference-box rendering."""

from agent.content.publishers.blogspot import build_blogger_post


BLOGSPOT_URL = "https://cocoboll.com"


def test_blogspot_site_url_renders_internal_link_as_gray_card(monkeypatch):
    monkeypatch.setenv("BLOGSPOT_SITE_URL", BLOGSPOT_URL)

    post = build_blogger_post(
        f"[내부 글]({BLOGSPOT_URL}/inside)",
        title="테스트 글",
    )

    html = post["content"]
    assert "background:#f8f9fa" in html
    assert "border:1px solid #e5e7eb" in html
    assert "내부 글" in html
    assert "target=\"_blank\"" not in html


def test_blogspot_site_url_renders_external_link_as_reference_box(monkeypatch):
    monkeypatch.setenv("BLOGSPOT_SITE_URL", BLOGSPOT_URL)

    post = build_blogger_post(
        "[참고 자료](https://example.com/reference)",
        title="테스트 글",
    )

    html = post["content"]
    assert "background:#f8fafc" in html
    assert "📚 참고 자료" in html
    assert 'target="_blank" rel="noopener noreferrer"' in html


def test_explicit_internal_host_takes_precedence_over_environment(monkeypatch):
    monkeypatch.setenv("BLOGSPOT_SITE_URL", "https://other.example.com")

    post = build_blogger_post(
        f"[내부 글]({BLOGSPOT_URL}/inside)",
        title="테스트 글",
        internal_host="cocoboll.com",
    )

    html = post["content"]
    assert "background:#f8f9fa" in html
    assert "내부 글" in html


def test_missing_blogspot_site_url_renders_no_card_or_reference_box(monkeypatch):
    monkeypatch.delenv("BLOGSPOT_SITE_URL", raising=False)

    post = build_blogger_post(
        "\n\n".join(
            (
                f"[내부 글]({BLOGSPOT_URL}/inside)",
                "[참고 자료](https://example.com/reference)",
            )
        ),
        title="테스트 글",
    )

    html = post["content"]
    assert "background:#f8f9fa" not in html
    assert "background:#f8fafc" not in html
    assert "📚" not in html
    assert 'target="_blank" rel="noopener noreferrer"' in html


def test_invalid_blogspot_site_url_does_not_raise(monkeypatch):
    monkeypatch.setenv("BLOGSPOT_SITE_URL", "http://[bad")

    post = build_blogger_post(
        "[이상한 주소](https://example.com/reference)",
        title="테스트 글",
    )

    assert "이상한 주소" in post["content"]
