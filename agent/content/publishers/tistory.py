"""Tistory publisher — ported from
multi-content-pipeline/publishers/tistory-publisher.js.

Repository 2 has NO real Tistory publish client (confirmed by the
architecture audit: no fetch/axios/playwright call exists anywhere for
Tistory). This module honestly reports that: it returns a preflight
checklist only, exactly like the source, and never claims to publish.
"""

from __future__ import annotations

from typing import Any, Dict, List

_PREFLIGHT_CHECKLIST: List[str] = [
    "H1/요약/대표 이미지가 첫 화면에서 자연스럽게 이어지는지 확인",
    "목차와 H2/H3 구조 확인",
    "애드센스 승인 체크 섹션 확인",
    "대표 이미지 ALT와 캡션 확인",
    "내부링크 후보 확인",
    "문단이 모바일 기준 2~3문장 이하인지 확인",
    "정보 제공/면책 문구 확인",
    "네이버식 이웃 추가 표현이 없는지 확인",
    "발행 전 공개/카테고리/태그 설정 확인",
]


def create_tistory_draft(*, markdown: str, live: bool) -> Dict[str, Any]:
    return {
        "apiCalled": False,
        "dryRun": True,
        "capability": "not_implemented",
        "preflightChecklist": list(_PREFLIGHT_CHECKLIST),
        "requiredCredentials": ["TISTORY_ACCESS_TOKEN 또는 브라우저 세션", "TISTORY_BLOG_NAME"],
        "nextAction": "실제 Tistory 발행 클라이언트는 아직 구현되지 않았습니다 — 수동 업로드 필요",
    }
