from gateway.native_content_route import _format_native_report


def test_native_report_surfaces_seo_title_length_warning() -> None:
    report = _format_native_report(
        {
            "daily_blog_bundle": {
                "run_date": "2026-07-18",
                "items": [
                    {
                        "platform": "wordpress",
                        "topic_title": "테스트 주제",
                        "quality_score": 90,
                        "quality_passed": True,
                        "quality_warnings": [
                            "TITLE_LENGTH_OUT_OF_RANGE: SEO title 26자 (권장 28~40자, 재작성 1회 후 유지)"
                        ],
                    }
                ],
            }
        },
        stack_trace="",
        platforms=["wordpress"],
    )

    assert "## 제목 길이 확인" in report
    assert "SEO title 26자" in report
