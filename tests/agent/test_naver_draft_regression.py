"""Golden regression coverage for Naver's independent draft parser."""

from __future__ import annotations

from agent.content.publishers.naver import parse_draft


NAVER_FIXTURE = """---
title: 회귀 테스트
---
제목: 네이버 회귀 제목
태그: #태그1, #태그2
# 본문 H1

첫 문장 **굵게** ==형광==

- 목록 항목
"""

EXPECTED_NAVER_DRAFT = {
    "title": "네이버 회귀 제목",
    "body": "첫 문장 굵게 ==형광==\n\n• 목록 항목\n\n#태그1, #태그2",
    "imageFile": None,
}


def test_naver_parse_draft_output_remains_byte_for_byte_golden() -> None:
    assert parse_draft(NAVER_FIXTURE) == EXPECTED_NAVER_DRAFT
