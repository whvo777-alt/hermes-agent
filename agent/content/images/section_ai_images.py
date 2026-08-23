"""본문 대단원 중 카드가 안 붙은 곳에 AI 그림을 하나씩 넣는다."""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_MAX = 2


def _plain_heading(text: str) -> str:
    return " ".join(str(text or "").split())


def build_section_ai_images(
    markdown: str,
    *,
    out_dir: Path,
    category_id: str,
    category_name: str,
    used_headings: Optional[List[str]] = None,
    style_seed: str = "",
    max_count: int = _DEFAULT_MAX,
) -> List[Dict[str, Any]]:
    """카드가 안 붙은 대단원에 AI 그림을 만들어 카드와 같은 모양으로 돌려준다.

    실패하면 빈 목록. 글이 멈추면 안 되므로 예외를 위로 던지지 않는다.
    """
    if max_count <= 0:
        return []
    try:
        from agent.content.images.ai_hero_image import _is_ai_hero_enabled

        if not _is_ai_hero_enabled():
            return []
    except Exception:  # noqa: BLE001
        return []

    used = {_plain_heading(h) for h in (used_headings or [])}
    try:
        headings = _collect_free_headings(markdown, used)
    except Exception:  # noqa: BLE001
        logger.warning("section AI image: heading scan failed", exc_info=True)
        return []

    results: List[Dict[str, Any]] = []
    for index, heading in enumerate(headings[:max_count]):
        saved = _render_one(
            heading=heading,
            out_dir=out_dir,
            category_id=category_id,
            category_name=category_name,
            style_seed=f"{style_seed}:{index}",
            index=index,
        )
        if not saved:
            continue
        results.append(
            {
                "file": str(saved),
                "heading": heading,
                "alt": f"{heading} 관련 이미지",
                "style": "ai_photo",
                "skin": "",
                "strip_ranges": [],
            }
        )
    return results


def _collect_free_headings(markdown: str, used: set) -> List[str]:
    """카드가 안 붙은 H2 를 문서 순서대로 모은다."""
    from agent.content.images.section_infographics import _SKIP_HEADING_RE

    out: List[str] = []
    for line in str(markdown or "").splitlines():
        if not line.startswith("## "):
            continue
        heading = _plain_heading(line[3:])
        if not heading:
            continue
        if _SKIP_HEADING_RE.search(heading):
            continue
        if heading in used:
            continue
        out.append(heading)
    return out


def _render_one(
    *,
    heading: str,
    out_dir: Path,
    category_id: str,
    category_name: str,
    style_seed: str,
    index: int,
) -> Optional[Path]:
    """대단원 하나에 맞는 AI 그림을 만든다. 안 되면 None."""
    try:
        from agent.content.images.ai_hero_image import (
            _materialize_provider_image,
            _resolve_provider,
            build_hero_prompt,
        )
    except Exception:  # noqa: BLE001
        return None

    try:
        provider = _resolve_provider()
        if not provider:
            return None
        prompt = build_hero_prompt(
            category_id=category_id,
            hero_mode="photo",
            style_seed=style_seed,
            topic_title=heading,
            category_name=category_name,
        )
        response = provider.generate(prompt, aspect_ratio="landscape")
        if not isinstance(response, dict) or not response.get("success"):
            return None
        image_ref = response.get("image")
        if not image_ref:
            return None
        with tempfile.TemporaryDirectory() as tmp:
            staged = _materialize_provider_image(str(image_ref), dest_dir=Path(tmp))
            if not staged:
                return None
            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / f"section_ai_{index + 1:02d}{staged.suffix}"
            try:
                shutil.copyfile(staged, target)
            except OSError:
                logger.warning("section AI image copy failed for %r", heading, exc_info=True)
                return None
    except Exception:  # noqa: BLE001 - 그림 하나 실패해도 글은 나간다
        logger.warning("section AI image failed for %r", heading, exc_info=True)
        return None

    return target
