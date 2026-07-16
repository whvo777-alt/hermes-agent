"""Launch-mode fixed platform→category policy.

Ported from multi-content-pipeline/config/pipeline-mode.js LAUNCH_CATEGORY_MAP.
V1 애드센스 승인 준비 기간 동안 플랫폼별 카테고리를 고정한다.
"""

from __future__ import annotations

from agent.content.config.categories import Category, find_category

LAUNCH_CATEGORY_MAP = {
    "wordpress": "health",
    "blogspot": "self-dev",
    "tistory": "finance",
    "naver": "it-tech",
}


def get_launch_category(platform_id: str) -> Category:
    category_id = LAUNCH_CATEGORY_MAP.get(platform_id)
    if not category_id:
        raise ValueError(f'플랫폼 "{platform_id}"에 대한 Launch Mode 카테고리 매핑이 없습니다.')
    category = find_category(category_id)
    if category is None:
        raise ValueError(f"카테고리를 찾지 못했습니다: {category_id}")
    return category
