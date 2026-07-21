"""Publish-on-approval — the ONLY place a platform publisher may be called.

Hard rule: this function refuses to call any publisher unless the matching
``DailyBlogApprovalItem.session.status`` is APPROVED. There is no other
entry point in agent/content/ that calls a publisher.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from agent.coo.approval_session import CEOApprovalSessionStatus
from agent.coo.daily_blog_bundle import DailyBlogApprovalBundle, find_item
from agent.content.markdown_html import escape_html, extract_title, markdown_to_html, strip_frontmatter
from agent.content.publishers.blogspot import create_blogspot_draft
from agent.content.publishers.naver import create_naver_draft
from agent.content.publishers.tistory import create_tistory_draft
from agent.content.publishers.wordpress import (
    create_wordpress_draft,
    ensure_wordpress_tags,
    is_live_wordpress_draft_enabled,
    update_rank_math_seo,
    upload_wordpress_media,
)


class PublishBlockedError(RuntimeError):
    """Raised when a publish is attempted before CEO approval."""


def _slugify(title: str) -> str:
    """Prefer Korean-readable slugs for Rank Math + human editors."""
    slug = re.sub(r"[^\w가-힣]+", "-", (title or "").strip(), flags=re.UNICODE)
    slug = re.sub(r"-{2,}", "-", slug).strip("-").lower()
    # Drop empty English-only fallbacks that strip Hangul away.
    if not slug or not re.search(r"[가-힣a-z0-9]", slug):
        return "post"
    return slug[:60]


def _prefer_korean_slug(*, raw_slug: str, title: str, focus_keyword: str) -> str:
    """Always prefer Hangul in the WordPress slug when the topic is Korean."""
    candidate = (raw_slug or "").strip().strip("`").strip("/")
    title_slug = _slugify(title)
    focus_slug = _slugify(focus_keyword) if focus_keyword else ""
    if re.search(r"[가-힣]", candidate):
        return candidate[:60]
    # English-only writer slug → rebuild from Korean title (short, readable).
    if re.search(r"[가-힣]", title_slug):
        return title_slug[:48]
    if focus_slug and re.search(r"[가-힣]", focus_slug):
        return focus_slug[:48]
    return candidate or title_slug or "post"


def _pick_focus_keyword(seo_meta: Dict[str, str], *, topic_title: str, title: str) -> str:
    """Choose a Rank Math focus keyword that already appears in the content."""
    tags = seo_meta.get("태그 후보") or ""
    for raw in re.split(r"[,#|/]", tags):
        candidate = raw.strip()
        if candidate and len(candidate) <= 20:
            return candidate
    for source in (topic_title, title):
        token = re.split(r"[:：|/\-–—]", source or "")[0].strip()
        token = re.sub(r"\s+", " ", token)
        if token:
            # Keep a short phrase Rank Math can match in title/body.
            words = token.split()
            return " ".join(words[:3]) if words else token
    return "블로그"


def _parse_tag_candidates(
    seo_meta: Dict[str, str],
    *,
    focus_keyword: str = "",
    topic_title: str = "",
    category_name: str = "",
    minimum: int = 5,
) -> list:
    """Build at least ``minimum`` WordPress tag names from writer metadata."""
    names: list[str] = []
    seen = set()

    def _add(value: str) -> None:
        name = re.sub(r"\s+", " ", str(value or "")).strip().lstrip("#")
        if not name or len(name) > 40:
            return
        key = name.lower()
        if key in seen:
            return
        seen.add(key)
        names.append(name)

    for raw in re.split(r"[,#|/]", seo_meta.get("태그 후보") or ""):
        _add(raw)
    _add(focus_keyword)
    for token in re.split(r"[\s:/|·,\-–—]+", topic_title or ""):
        if len(token) >= 2:
            _add(token)
    _add(category_name)
    # Generic health/blog fillers only used when the writer omitted tags.
    for filler in ("초보", "체크리스트", "가이드", "생활습관", "건강정보"):
        if len(names) >= minimum:
            break
        _add(filler)
    return names[: max(minimum, 8)]


def _build_rank_math_title(title: str, focus_keyword: str) -> str:
    from agent.content.seo_enrich import ensure_number_in_seo_title

    if focus_keyword and focus_keyword in title:
        base = title
    else:
        base = f"{focus_keyword} | {title}"
    return ensure_number_in_seo_title(base)


_SEO_META_LINE_RE = re.compile(
    r"^\**\s*(URL slug|URL 슬러그|슬러그|slug|Meta description|메타 디스크립션|"
    r"meta\s*title|메타\s*타이틀|메타\s*제목|"
    r"카테고리 후보|태그 후보|이미지 ALT 텍스트|이미지 ALT|대표 이미지 ALT|ALT\s*후보)\s*:?\**\s*:?\s*(.*)$",
    re.M | re.I,
)


def extract_seo_meta(markdown: str) -> Dict[str, str]:
    """Read the writer's SEO suggestion block (slug/meta description/etc.)."""
    meta: Dict[str, str] = {}
    for match in _SEO_META_LINE_RE.finditer(str(markdown or "")):
        key = match.group(1).strip()
        # Normalize Korean aliases to the English keys used downstream.
        if re.search(r"슬러그|slug", key, re.I):
            key = "URL slug"
        elif re.search(r"meta\s*title|메타\s*타이틀|메타\s*제목", key, re.I):
            key = "meta title"
        elif re.search(r"meta|메타", key, re.I):
            key = "Meta description"
        elif re.search(r"태그", key):
            key = "태그 후보"
        elif re.search(r"ALT", key, re.I):
            key = "이미지 ALT 텍스트"
        meta[key] = match.group(2).strip().rstrip("*").strip().strip("`")
    return meta


def _strip_seo_meta_block(markdown: str) -> str:
    """Remove the SEO suggestion lines from the publishable body.

    These lines are editor-facing metadata (slug, meta description, tag
    candidates) — they must never appear inside the published post body.
    """
    value = _SEO_META_LINE_RE.sub("", str(markdown or ""))
    # Collapse separator runs left behind by the removed block (e.g. the
    # "---" fence that framed it right under the H1 title).
    value = re.sub(r"\n{2,}(---+\n+)+", "\n\n", value)
    value = re.sub(r"(^# [^\n]+\n+)(?:---+\n+)+", r"\1", value)
    return value


def _strip_ops_checklist_sections(markdown: str) -> str:
    """Remove AdSense / mobile-optimization ops sections from reader body."""
    return re.sub(
        r"\n##\s*(?:▶\s*)?\[?(?:모바일\s*최적화\s*체크|애드센스\s*승인\s*체크)\]?\s*\n"
        r"[\s\S]*?(?=\n##\s|\Z)",
        "\n",
        str(markdown or ""),
    )


def _strip_hero_caption(markdown: str) -> str:
    """Remove hero image caption quotes like '발행용 … 대표 이미지입니다'."""
    value = str(markdown or "")
    value = re.sub(
        r"\n>\s*[^\n]*(?:발행용|대표\s*이미지)[^\n]*\n?",
        "\n",
        value,
    )
    value = re.sub(
        r"\n(?:대표\s*이미지\s*파일|이미지\s*ALT|이미지\s*설명)\s*:[^\n]*",
        "",
        value,
    )
    return value


def _wordpress_publishable_markdown(markdown: str, image_file: str) -> str:
    """Remove local-only hero references and editor metadata from the body."""
    value = str(markdown or "")
    if image_file:
        value = re.sub(
            rf"!\[[^\]]*\]\({re.escape(image_file)}\)\s*"
            rf"(?:\n>\s*[^\n]*)?",
            "",
            value,
        )
    value = re.sub(
        r"\n대표 이미지 파일:\s*[^\n]+"
        r"\n이미지 ALT:\s*[^\n]+"
        r"\n이미지 설명:\s*[^\n]+\s*$",
        "",
        value,
    )
    value = _strip_seo_meta_block(value)
    value = _strip_ops_checklist_sections(value)
    value = _strip_hero_caption(value)
    return value.strip()


def _blogspot_publishable_markdown(markdown: str) -> str:
    """Reader-facing Blogspot body: keep hero image, drop editor/ops chrome.

    Convert local hero SVG → PNG data-URI so Blogger shows a polished card
    without the old platform-name stamp.
    """
    from agent.content.markdown_html import strip_frontmatter as _strip_fm

    value = _strip_fm(str(markdown or ""))
    value = _strip_hero_caption(value)
    value = _strip_seo_meta_block(value)
    value = _strip_ops_checklist_sections(value)
    value = _embed_blogspot_hero_png(value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _embed_blogspot_hero_png(markdown: str) -> str:
    """Replace ``![...](...hero_adsense.svg)`` with an embedded PNG data URI."""
    match = re.search(r"!\[([^\]]*)\]\(([^)]*hero_adsense\.svg)\)", markdown or "")
    if not match:
        return markdown
    alt, src = match.group(1), match.group(2).strip()
    path = Path(src)
    if not path.is_file():
        return markdown
    try:
        from agent.content.images.hero_image import extract_blog_title, render_hero_png
        from agent.content.markdown_html import extract_title

        title = extract_title(markdown, extract_blog_title(markdown, alt or "가이드"))
        png_path = render_hero_png(
            str(path),
            category_id="self-dev",
            title=title,
            subtitle="",
            platform_label="",
            style_seed=f"blogspot:{title}",
        )
        import base64

        b64 = base64.b64encode(Path(png_path).read_bytes()).decode("ascii")
        data_uri = f"data:image/png;base64,{b64}"
        return markdown[: match.start()] + f"![{alt}]({data_uri})" + markdown[match.end() :]
    except Exception:  # noqa: BLE001 — keep original if render fails
        return markdown


def _strip_wordpress_body_title(markdown: str) -> str:
    """Remove the first H1 because WordPress renders the post title separately."""
    return re.sub(r"^#\s+[^\n]+\n+", "", str(markdown or ""), count=1, flags=re.M).strip()


def _attach_wordpress_infographics(*, html: str, markdown: str, category_id: str,
                                   output_dir: str, live: bool, doc_seed: str) -> tuple:
    """Upload section infographics and insert each after its H2 heading."""
    from agent.content.images.section_infographics import build_section_infographics

    uploaded: list = []
    for info in build_section_infographics(
        markdown=markdown,
        category_id=category_id,
        output_dir=output_dir,
    ):
        media = upload_wordpress_media(
            site_url=os.environ.get("WORDPRESS_SITE_URL"),
            username=os.environ.get("WORDPRESS_USERNAME"),
            app_password=os.environ.get("WORDPRESS_APP_PASSWORD"),
            file_path=info["file"],
            alt_text=info["alt"],
            live=live,
        )
        source_url = media.get("sourceUrl")
        if not source_url:
            continue
        from agent.content.markdown_html import h2_inner_html

        anchor = re.compile(
            rf"(<h2[^>]*>{re.escape(h2_inner_html(info['heading'], seed=doc_seed))}</h2>)"
        )
        figure = (
            '<figure style="margin:1.2em 0 1.8em;">'
            f'<img src="{escape_html(str(source_url))}" alt="{escape_html(info["alt"])}" '
            'style="display:block;width:100%;height:auto;border-radius:12px;" />'
            "</figure>"
        )
        new_html, n = anchor.subn(rf"\1\n{figure}", html, count=1)
        if n:
            html = new_html
            uploaded.append(media)
    return html, uploaded


def publish_approved_item(bundle: DailyBlogApprovalBundle, platform_id: str, *, live: bool = False) -> Dict[str, Any]:
    """Call the matching platform publisher — ONLY if approved.

    ``live=False`` (default) always produces a dry-run/preview result with
    zero network calls, regardless of approval status, matching "승인 없는
    API 호출 금지" / "실제 발행 금지" defaults. ``live=True`` additionally
    requires the item to be APPROVED, or this raises ``PublishBlockedError``
    without calling anything.
    """
    item = find_item(bundle, platform_id)
    if item is None:
        raise ValueError(f"No item for platform: {platform_id}")

    if getattr(item, "revision_requested", False):
        raise PublishBlockedError(
            f"{platform_id} item has a pending revision request — publisher not called."
        )
    if live and item.session.status is not CEOApprovalSessionStatus.APPROVED:
        raise PublishBlockedError(
            f"{platform_id} item is not approved (status={item.session.status.value}) — publisher not called."
        )

    blog_content = Path(item.blog_file).read_text(encoding="utf-8")

    if platform_id == "wordpress":
        publishable_content = _wordpress_publishable_markdown(
            blog_content,
            item.image_file,
        )
        title = extract_title(publishable_content, item.topic_title)
        publishable_content = _strip_wordpress_body_title(publishable_content)
        seo_meta = extract_seo_meta(blog_content)
        focus_keyword = _pick_focus_keyword(
            seo_meta, topic_title=item.topic_title, title=title
        )
        raw_slug = seo_meta.get("URL slug") or _slugify(title)
        raw_slug = _prefer_korean_slug(
            raw_slug=raw_slug,
            title=title,
            focus_keyword=focus_keyword,
        )

        from agent.content.seo_enrich import enrich_wordpress_markdown_for_seo
        from agent.content.publishers.wordpress import _build_auth_header

        site_url = os.environ.get("WORDPRESS_SITE_URL") or ""
        wp_live = live and is_live_wordpress_draft_enabled(os.environ)
        auth_header = ""
        if wp_live:
            try:
                auth_header = _build_auth_header(
                    username=os.environ.get("WORDPRESS_USERNAME"),
                    app_password=os.environ.get("WORDPRESS_APP_PASSWORD"),
                )
            except Exception:  # noqa: BLE001
                auth_header = ""
        seo_enrichment = enrich_wordpress_markdown_for_seo(
            publishable_content,
            focus_keyword=focus_keyword,
            category_id=item.category_id,
            site_url=site_url,
            auth_header=auth_header,
            live=wp_live,
        )
        publishable_content = seo_enrichment["markdown"]

        # Must match markdown_to_html()'s own doc_seed formula exactly — the
        # HTML below is rendered with that seed, so anchors reconstructed
        # here have to use the identical seed or the H2 color/markup diverges
        # and the regex anchor silently fails to match.
        doc_lines = strip_frontmatter(publishable_content).split("\n")
        doc_seed = extract_title(publishable_content, "") or (doc_lines[0] if doc_lines else "post")

        payload = {
            "status": "draft",
            "title": title,
            "slug": raw_slug,
            "content": markdown_to_html(publishable_content),
        }
        if seo_meta.get("Meta description"):
            payload["excerpt"] = seo_meta["Meta description"]

        tag_names = _parse_tag_candidates(
            seo_meta,
            focus_keyword=focus_keyword,
            topic_title=item.topic_title,
            category_name=item.category_name,
            minimum=5,
        )
        tags_result: Dict[str, Any] = {"apiCalled": False, "names": tag_names, "ids": []}
        try:
            tags_result = ensure_wordpress_tags(
                site_url=site_url,
                username=os.environ.get("WORDPRESS_USERNAME"),
                app_password=os.environ.get("WORDPRESS_APP_PASSWORD"),
                tag_names=tag_names,
                live=wp_live,
                minimum=5,
            )
            if tags_result.get("ids"):
                payload["tags"] = tags_result["ids"]
        except Exception as exc:  # noqa: BLE001 — draft must proceed without tags
            logging.getLogger(__name__).warning(
                "WordPress tag ensure failed: %s", exc
            )
            tags_result = {"apiCalled": False, "error": type(exc).__name__, "names": tag_names}

        # Hero image: WordPress rejects SVG, so render the card as PNG and
        # upload it as the featured image. Image failure never blocks the
        # draft itself — the text draft is the primary deliverable.
        media_result: Optional[Dict[str, Any]] = None
        image_file = str(item.image_file or "")
        if not image_file:
            fallback_image = Path(item.blog_file).parent / "images" / "hero_adsense.svg"
            if fallback_image.is_file():
                image_file = str(fallback_image)
        if image_file:
            try:
                from agent.content.images.hero_image import render_hero_png

                png_path = render_hero_png(
                    image_file,
                    category_id=item.category_id,
                    title=title,
                    subtitle=f"{item.category_name} · {item.topic_title}",
                    platform_label="",  # never stamp platform name on the hero
                    style_seed=f"{item.platform}:{item.topic_title}:{title}",
                )
                media_result = upload_wordpress_media(
                    site_url=os.environ.get("WORDPRESS_SITE_URL"),
                    username=os.environ.get("WORDPRESS_USERNAME"),
                    app_password=os.environ.get("WORDPRESS_APP_PASSWORD"),
                    file_path=png_path,
                    alt_text=item.image_alt,
                    live=wp_live,
                )
                if media_result.get("id"):
                    payload["featured_media"] = media_result["id"]
                if media_result.get("sourceUrl"):
                    alt = escape_html(item.image_alt or title)
                    source_url = escape_html(str(media_result["sourceUrl"]))
                    figure = (
                        '<figure style="margin:0 0 2em;">'
                        f'<img src="{source_url}" alt="{alt}" '
                        'style="display:block;width:100%;height:auto;border-radius:12px;" />'
                        "</figure>"
                    )
                    payload["content"] = f"{figure}\n{payload['content']}"
            except Exception as exc:  # noqa: BLE001 — draft must proceed without image
                media_result = {
                    "apiCalled": False,
                    "error": f"hero image upload failed: {type(exc).__name__}",
                }
                logging.getLogger(__name__).warning(
                    "WordPress hero image upload failed: %s", exc
                )

        # Section infographics: 3–5 in-body cards matched to H2 sections.
        # Same policy as the hero — image failure never blocks the draft.
        infographic_media: list[Dict[str, Any]] = []
        try:
            payload["content"], infographic_media = _attach_wordpress_infographics(
                html=payload["content"],
                markdown=publishable_content,
                category_id=item.category_id,
                output_dir=str(Path(item.blog_file).parent / "images"),
                live=wp_live,
                doc_seed=doc_seed,
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "WordPress section infographics failed: %s", exc
            )

        result = create_wordpress_draft(
            site_url=os.environ.get("WORDPRESS_SITE_URL"),
            username=os.environ.get("WORDPRESS_USERNAME"),
            app_password=os.environ.get("WORDPRESS_APP_PASSWORD"),
            payload=payload,
            live=wp_live,
            expected_featured_media=payload.get("featured_media"),
        )
        if result.get("verification", {}).get("featured_media_mismatch"):
            result["hero_image_status"] = "attach_mismatch"
        elif payload.get("featured_media"):
            result["hero_image_status"] = "attached"
        else:
            result["hero_image_status"] = "upload_failed"
        if media_result is not None:
            result["heroImage"] = media_result
        if infographic_media:
            result["sectionImages"] = infographic_media
        result["seoEnrichment"] = {
            "keywordThinned": seo_enrichment.get("keywordThinned"),
            "externalAdded": seo_enrichment.get("externalAdded"),
            "internalCount": seo_enrichment.get("internalCount"),
            "focusKeywordCount": seo_enrichment.get("focusKeywordCount"),
        }
        result["tags"] = tags_result

        _section_images = list(result.get("sectionImages") or [])
        _publish_log = {
            "post_id": (result.get("response") or {}).get("id")
            or (result.get("verification") or {}).get("id"),
            "status": (result.get("verification") or {}).get("status")
            or (result.get("response") or {}).get("status"),
            "hero_image_status": result.get("hero_image_status"),
            "sectionImages_count": len(_section_images),
            "sectionImages_has_sourceUrl": [
                bool(item.get("sourceUrl")) for item in _section_images
            ],
            "live": bool(wp_live),
        }
        if not wp_live:
            _publish_log["dry_run"] = True
        logging.getLogger(__name__).info(
            "WordPress publish result %s",
            json.dumps(_publish_log, ensure_ascii=False),
        )

        # Rank Math needs its own meta — WordPress excerpt alone is not enough.
        # Failure here never blocks the draft (text draft is primary).
        try:
            post_id = (result.get("response") or {}).get("id")
            seo_description = seo_meta.get("Meta description") or ""
            if post_id and wp_live:
                result["rankMath"] = update_rank_math_seo(
                    site_url=os.environ.get("WORDPRESS_SITE_URL"),
                    username=os.environ.get("WORDPRESS_USERNAME"),
                    app_password=os.environ.get("WORDPRESS_APP_PASSWORD"),
                    post_id=int(post_id),
                    focus_keyword=focus_keyword,
                    seo_title=_build_rank_math_title(title, focus_keyword),
                    seo_description=seo_description[:160],
                    live=True,
                )
            else:
                result["rankMath"] = {
                    "apiCalled": False,
                    "focusKeyword": focus_keyword,
                    "seoTitle": _build_rank_math_title(title, focus_keyword),
                    "seoDescription": seo_description[:160],
                }
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "Rank Math meta update failed: %s", exc
            )
            result["rankMath"] = {"apiCalled": False, "error": type(exc).__name__}
        return result

    if platform_id == "blogspot":
        publishable = _blogspot_publishable_markdown(blog_content)
        return create_blogspot_draft(
            markdown=publishable,
            blog_id=os.environ.get("BLOGGER_BLOG_ID"),
            client_id=os.environ.get("BLOGGER_CLIENT_ID"),
            client_secret=os.environ.get("BLOGGER_CLIENT_SECRET"),
            refresh_token=os.environ.get("BLOGGER_REFRESH_TOKEN"),
            live=live,
        )

    if platform_id == "naver":
        return create_naver_draft(markdown=blog_content, live=live)

    if platform_id == "tistory":
        return create_tistory_draft(markdown=blog_content, live=live)

    raise ValueError(f"Unknown platform: {platform_id}")
