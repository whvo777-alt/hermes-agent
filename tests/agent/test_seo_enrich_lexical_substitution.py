"""Regression coverage for unsafe Korean keyword thinning substitutions."""

from agent.content.seo_enrich import enrich_wordpress_markdown_for_seo


def test_seo_enrichment_preserves_korean_words_and_particles():
    repeated_keyword_text = "\n".join(
        "요가를 시작할 때는 무리하지 않는 것이 좋습니다." for _ in range(15)
    )
    content = f"{repeated_keyword_text}\n\n필요가 있습니다.\n\n[외부 자료](https://example.org)"

    result = enrich_wordpress_markdown_for_seo(
        content,
        focus_keyword="요가",
        category_id="health",
    )
    markdown = result["markdown"]

    assert "요가를 시작할 때는" in markdown
    assert "수업를" not in markdown
    assert "필요가 있습니다" in markdown
    assert "필요 있습니다" not in markdown
