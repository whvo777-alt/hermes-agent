"""AI-generated hero image path.

Calls the active ``image_gen`` provider (``agent.image_gen_registry.get_active_provider``)
to render a real hero image instead of the local SVG template in
``hero_image.py``. Disabled by default — opt in via ``content.hero_image.mode: ai``
in ``config.yaml``.

Any failure (disabled, no provider configured, provider error, bad response,
download failure) returns ``None`` from :func:`create_hero_image_ai`'s AI
attempt, and the caller falls back to ``hero_image.create_hero_image``'s SVG
template unchanged. The SVG path must always keep working on its own.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Dict, Optional, Tuple

from agent.content.images.hero_image import (
    HeroImage,
    _category_visual,
    _style_index,
    extract_blog_title,
)

logger = logging.getLogger(__name__)


# Category -> English scene vocabulary for the AI prompt. Separate from
# hero_image._ICONS (Korean chip labels): prompts read best in English, and
# feeding raw Korean topic words to the model risks it trying to render them
# as on-image text instead of treating them as a scene description.
_SCENE_VOCAB: Dict[str, Tuple[str, ...]] = {
    "health": (
        "stretching on a yoga mat at home",
        "a glass of water next to a protein bowl on a kitchen counter",
        "running shoes and a water bottle by a park path",
    ),
    "parenting": (
        "a tidy nursery shelf with folded baby clothes and soft toys",
        "a feeding bottle and small toys arranged on a wooden table",
        "a parenting planner notebook next to tiny baby shoes",
    ),
    "self-dev": (
        "a minimal desk with an open notebook and a planner",
        "a quiet reading corner with warm lamp light",
        "a clean workspace with a coffee cup and handwritten notes",
    ),
    "it-tech": (
        "two laptops side by side on a desk for comparison",
        "a minimalist tech desk setup with a monitor and keyboard",
        "a close-up of a modern keyboard and a smartphone",
    ),
    "finance": (
        "a minimal desk with coins, a growth chart and a calculator",
        "a notebook with a simple bar chart next to stacked coins",
        "a clean financial planning flat-lay with a piggy bank",
    ),
    "travel": (
        "a scenic road with a folded map and a suitcase",
        "a window seat view with a passport and a camera",
        "a cozy cafe table with a travel journal and a boarding pass",
    ),
}
_DEFAULT_SCENE_VOCAB: Tuple[str, ...] = (
    "a clean minimal flat-lay with a notebook and a checklist",
)

_CARD_STYLE = (
    "clean flat-lay editorial illustration, soft studio lighting, "
    "minimal composition, pastel color palette"
)
_PHOTO_STYLE = (
    "cinematic lifestyle photograph, natural light, shallow depth of field, "
    "warm color grading"
)
_NEGATIVE = (
    "no text, no letters, no words, no numbers, no logos, no watermark, "
    "no signature, no border"
)


def build_hero_prompt(*, category_id: str, hero_mode: str, style_seed: str) -> str:
    """Build an English text-to-image prompt from category + hero-mode signals.

    Topic words are deliberately excluded from the prompt itself — see the
    module docstring. Topic still shapes the result indirectly through
    ``hero_mode`` (``choose_hero_mode`` already factors in the topic title)
    and through ``style_seed`` (usually the topic id), which rotates which
    scene is picked.
    """
    vocab = _SCENE_VOCAB.get(category_id, _DEFAULT_SCENE_VOCAB)
    idx = _style_index(style_seed or category_id)
    scene = vocab[idx % len(vocab)]
    style = _PHOTO_STYLE if hero_mode == "photo" else _CARD_STYLE
    return f"{scene}, {style}. {_NEGATIVE}."


def _is_ai_hero_enabled() -> bool:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        content_cfg = cfg.get("content") if isinstance(cfg, dict) else None
        hero_cfg = content_cfg.get("hero_image") if isinstance(content_cfg, dict) else None
        mode = hero_cfg.get("mode") if isinstance(hero_cfg, dict) else None
        return isinstance(mode, str) and mode.strip().lower() == "ai"
    except Exception as exc:  # noqa: BLE001 — missing/broken config just means "disabled"
        logger.debug("Could not read content.hero_image.mode from config: %s", exc)
        return False


def _resolve_provider():
    try:
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Plugin discovery failed before AI hero generation: %s", exc)

    from agent.image_gen_registry import get_active_provider

    return get_active_provider()


def _materialize_provider_image(image_ref: str, *, dest_dir: Path) -> Optional[Path]:
    """Copy (or download) the provider's output into ``dest_dir/hero_ai.<ext>``."""
    if image_ref.startswith("http://") or image_ref.startswith("https://"):
        try:
            from agent.image_gen_provider import save_url_image

            source = save_url_image(image_ref, prefix="hero_ai")
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI hero image download failed: %s", exc)
            return None
    else:
        source = Path(image_ref)
        if not source.is_file():
            logger.warning("AI hero image path does not exist: %s", image_ref)
            return None

    ext = source.suffix.lower().lstrip(".") or "png"
    dest = dest_dir / f"hero_ai.{ext}"
    try:
        shutil.copyfile(source, dest)
    except OSError as exc:
        logger.warning("Could not copy AI hero image into draft dir: %s", exc)
        return None
    return dest


def _find_cached_ai_hero(out_dir: Path) -> Optional[Path]:
    for ext in ("png", "jpg", "jpeg", "webp"):
        cached = out_dir / f"hero_ai.{ext}"
        if cached.is_file():
            return cached
    return None


def _build_hero_image_record(
    file_path: Path,
    *,
    category_name: str,
    topic_title: str,
    blog_content: str,
    prompt: str,
) -> HeroImage:
    from datetime import datetime, timezone

    blog_title = extract_blog_title(blog_content, topic_title or f"{category_name} 가이드")
    return HeroImage(
        role="hero",
        status="ai_generated_ready_upload_required",
        file=str(file_path),
        alt=f"{blog_title} — {category_name} 핵심 기준을 한눈에 보여주는 대표 이미지",
        caption=f"{category_name} 대표 이미지입니다.",
        prompt=prompt,
        inserted_in="after_title",
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _try_generate(
    *,
    out_dir: Path,
    category_id: str,
    category_name: str,
    topic_title: str,
    blog_content: str,
    style_seed: str,
) -> Optional[HeroImage]:
    if not _is_ai_hero_enabled():
        return None

    # A re-run of the same draft (same style_seed/topic) must not re-bill the
    # provider for an image that's already sitting on disk.
    cached = _find_cached_ai_hero(out_dir)
    if cached is not None:
        logger.debug("Reusing cached AI hero image: %s", cached)
        return _build_hero_image_record(
            cached, category_name=category_name, topic_title=topic_title,
            blog_content=blog_content, prompt="(cached)",
        )

    provider = _resolve_provider()
    if provider is None:
        logger.info(
            "content.hero_image.mode=ai but no image_gen provider is active; "
            "falling back to the SVG hero."
        )
        return None

    visual = _category_visual(category_id, category_name, None, style_seed=style_seed, topic_title=topic_title)
    prompt = build_hero_prompt(category_id=category_id, hero_mode=visual.hero_mode, style_seed=style_seed)

    try:
        result = provider.generate(prompt, aspect_ratio="landscape")
    except Exception as exc:  # noqa: BLE001 — any provider failure falls back to SVG
        logger.warning("AI hero image generation raised %s: %s", type(exc).__name__, exc)
        return None

    if not isinstance(result, dict) or not result.get("success") or not result.get("image"):
        logger.warning(
            "AI hero image generation failed: %s",
            (result or {}).get("error", "no image returned"),
        )
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    dest = _materialize_provider_image(str(result["image"]), dest_dir=out_dir)
    if dest is None:
        return None

    return _build_hero_image_record(
        dest, category_name=category_name, topic_title=topic_title,
        blog_content=blog_content, prompt=prompt,
    )


def create_hero_image_ai(
    *,
    out_dir: Path,
    platform_id: str,
    platform_label: str,
    category_id: str,
    category_name: str,
    category_keywords: Optional[list],
    topic_title: str,
    blog_content: str,
    style_seed: str = "",
) -> HeroImage:
    """Try a real AI-generated hero image; fall back to the local SVG hero
    on any failure, when disabled, or when no provider is configured.

    Signature mirrors ``hero_image.create_hero_image`` so callers can swap
    one for the other directly.
    """
    ai_image = _try_generate(
        out_dir=out_dir,
        category_id=category_id,
        category_name=category_name,
        topic_title=topic_title,
        blog_content=blog_content,
        style_seed=style_seed or f"{platform_id}:{topic_title}",
    )
    if ai_image is not None:
        return ai_image

    from agent.content.images.hero_image import create_hero_image

    return create_hero_image(
        out_dir=out_dir,
        platform_id=platform_id,
        platform_label=platform_label,
        category_id=category_id,
        category_name=category_name,
        category_keywords=category_keywords,
        topic_title=topic_title,
        blog_content=blog_content,
        style_seed=style_seed,
    )
