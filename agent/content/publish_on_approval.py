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
from agent.content.images.section_infographics import extract_all_qa_pairs
from agent.content.markdown_html import escape_html, extract_labels, extract_title, markdown_to_html, strip_frontmatter
from agent.content.publishers.blogspot import create_blogspot_draft
from agent.content.publishers.naver import create_naver_draft
from agent.content.publishers.tistory import create_tistory_draft
from agent.content.publishers.wordpress import (
    create_wordpress_draft,
    ensure_wordpress_tags,
    is_live_wordpress_draft_enabled,
    resolve_or_create_wordpress_category,
    update_rank_math_seo,
    upload_wordpress_media,
)
from agent.content.seo_enrich import (
    format_seo_title_length_warning,
    is_seo_title_length_valid,
    truncate_seo_title,
)
from agent.content.structured_data import build_structured_data_html


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
        # Route through _slugify() even though the writer already supplied a
        # slug -- a writer-provided "URL slug" field is free-form Korean
        # text, not guaranteed to already be hyphenated (real published-post
        # bug: a space-separated candidate like "한의학 확인 기준 7가지" was
        # returned verbatim, so the WordPress URL had no hyphens at all).
        return _slugify(candidate)
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
    if focus_keyword and focus_keyword in title:
        base = title
    else:
        base = f"{focus_keyword} | {title}"
    return truncate_seo_title(base)


_META_DELIM_RE = re.compile(r"\n-{3,}\s*META\s*-{3,}\s*\n?", re.I)


def _split_meta_block(markdown: str) -> tuple:
    """Split writer output into (body, meta_block) on the ``---META---`` delimiter.

    ``meta_block`` is "" when the delimiter is absent — callers fall back to
    the legacy whole-document regex scan for drafts written before this
    convention (already-queued approvals).
    """
    parts = _META_DELIM_RE.split(str(markdown or ""), maxsplit=1)
    if len(parts) == 2:
        return parts[0].rstrip(), parts[1].strip()
    return str(markdown or ""), ""


_SEO_META_LINE_RE = re.compile(
    r"^\**\s*(URL slug|URL 슬러그|슬러그|slug|Meta description|메타 디스크립션|"
    r"meta\s*title|메타\s*타이틀|메타\s*제목|"
    r"제목|title|"
    r"카테고리 후보|태그 후보|이미지 ALT 텍스트|이미지 ALT|대표 이미지 ALT|ALT\s*후보)\s*:?\**\s*:?\s*(.*)$",
    re.M | re.I,
)


def extract_seo_meta(markdown: str) -> Dict[str, str]:
    """Read the writer's SEO suggestion block (slug/meta description/etc.)."""
    _, meta_block = _split_meta_block(markdown)
    scan_target = meta_block or str(markdown or "")  # legacy fallback: no delimiter
    meta: Dict[str, str] = {}
    for match in _SEO_META_LINE_RE.finditer(scan_target):
        key = match.group(1).strip()
        # Normalize Korean aliases to the English keys used downstream.
        if re.search(r"슬러그|slug", key, re.I):
            key = "URL slug"
        elif re.search(r"meta\s*title|메타\s*타이틀|메타\s*제목", key, re.I):
            key = "meta title"
        elif re.fullmatch(r"제목|title", key, re.I):
            key = "title"
        elif re.search(r"meta|메타", key, re.I):
            key = "Meta description"
        elif re.search(r"ALT", key, re.I):
            key = "이미지 ALT 텍스트"
        meta[key] = match.group(2).strip().rstrip("*").strip().strip("`")
    return meta


def _strip_seo_meta_block(markdown: str) -> str:
    """Remove the SEO suggestion lines from the publishable body.

    Primary path: split on the ``---META---`` delimiter the writer prompt
    now mandates and keep only the body. Legacy fallback (no delimiter
    found, e.g. an approval already queued before this change): scan the
    whole document line-by-line as before.
    """
    body, meta_block = _split_meta_block(markdown)
    if meta_block:
        return body
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


_HTML_BLOCK_START_RE = re.compile(r"^<([a-zA-Z][\w-]*)\b[^>]*>")
_VOID_HTML_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}


def _strip_raw_html_blocks(markdown: str) -> str:
    """Drop stray raw-HTML blocks the writer sometimes hand-writes inline
    (e.g. a styled "hook card" div opened right under the H1). Blogspot's
    ``markdown_to_html()`` has no raw-HTML passthrough -- every line falls
    through to the plain-paragraph branch, gets ``escape_html()``'d, and
    the tags show up as literal broken text on the published page (real
    published-post bug). The two supported ways to get real HTML into a
    Blogspot post are ``![]()`` image syntax (becomes a real ``<img>``) and
    ``extra_content_html`` appended after ``markdown_to_html()`` runs --
    never hand-written tags mixed into the markdown body, so any such block
    found here is unsupported and safest removed rather than left to render
    as broken text.
    """
    lines = str(markdown or "").split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        start_match = _HTML_BLOCK_START_RE.match(stripped)
        tag = start_match.group(1).lower() if start_match else None
        if start_match and tag not in _VOID_HTML_TAGS and not stripped.endswith("/>"):
            open_re = re.compile(rf"<{tag}\b[^>]*(?<!/)>", re.I)
            close_re = re.compile(rf"</{tag}\s*>", re.I)
            depth = len(open_re.findall(lines[i])) - len(close_re.findall(lines[i]))
            j = i + 1
            while depth > 0 and j < len(lines):
                depth += len(open_re.findall(lines[j])) - len(close_re.findall(lines[j]))
                j += 1
            if depth <= 0:
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


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
    value = _strip_wordpress_body_title(value)
    value = _strip_hero_caption(value)
    value = _strip_seo_meta_block(value)
    value = _strip_ops_checklist_sections(value)
    value = _strip_raw_html_blocks(value)
    value = _embed_blogspot_hero_png(value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _reencode_jpeg_for_inline(path: Path, *, max_size: tuple = (1280, 720), quality: int = 80) -> bytes:
    """Downscale + re-encode a raster hero image as JPEG bytes.

    AI-generated heroes run 100KB-1MB+ (photographic texture), unlike the
    flat-illustration PNGs ``render_hero_png`` produces — inlined as base64
    at full size/format they would noticeably bloat the Blogger post body.
    """
    from io import BytesIO

    from PIL import Image

    with Image.open(path) as img:
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail(max_size, Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        return buf.getvalue()


def _embed_blogspot_hero_png(markdown: str) -> str:
    """Replace the featured-image markdown with an embedded base64 data URI.

    SVG source (the legacy hero template fallback): rasterize via
    ``render_hero_png`` and embed as PNG — unchanged behavior. Raster source
    (title cards, or a legacy AI-generated hero ``hero_ai.<ext>``): skip the
    SVG render step entirely and embed a resized/re-encoded JPEG instead
    (see ``_reencode_jpeg_for_inline``).
    """
    match = re.search(
        r"!\[([^\]]*)\]\(([^)]*(?:title_card\.png|hero_(?:adsense\.svg|ai\.(?:png|jpe?g|webp))))\)",
        markdown or "",
    )
    if not match:
        return markdown
    alt, src = match.group(1), match.group(2).strip()
    path = Path(src)
    if not path.is_file():
        return markdown
    try:
        import base64

        if path.suffix.lower() == ".svg":
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
            data = Path(png_path).read_bytes()
            mime = "image/png"
        else:
            data = _reencode_jpeg_for_inline(path)
            mime = "image/jpeg"

        b64 = base64.b64encode(data).decode("ascii")
        data_uri = f"data:{mime};base64,{b64}"
        return markdown[: match.start()] + f"![{alt}]({data_uri})" + markdown[match.end() :]
    except Exception:  # noqa: BLE001 — keep original if render fails
        return markdown


def _strip_wordpress_body_title(markdown: str) -> str:
    """Remove the first H1 because WordPress renders the post title separately."""
    return re.sub(r"^#\s+[^\n]+\n+", "", str(markdown or ""), count=1, flags=re.M).strip()


def _build_section_infographic_results(*, markdown: str, category_id: str, output_dir: str, style_seed: str) -> list:
    """Render (but don't upload/insert) section infographics for ``markdown``.

    Single point where specs get rendered for a platform, so the same
    ``results`` (with their ``strip_ranges``) drive both the source-block
    strip and the later image insertion — never rebuilt a second time with
    a fresh seed roll, which would risk the stripped ranges and inserted
    images disagreeing.
    """
    from agent.content.images.section_infographics import build_section_infographics

    return build_section_infographics(
        markdown=markdown, category_id=category_id, output_dir=output_dir, style_seed=style_seed,
    )


def _strip_infographic_source_blocks(markdown: str, results: list) -> str:
    """Remove each infographic's visualized source lines from ``markdown``.

    Without this, the section keeps both the rendered card AND the original
    table/checklist/step text — the exact duplication bug this refactor
    fixes. Quote-style results carry no ``strip_ranges`` (a pull-quote is
    supposed to repeat a body sentence), so they're a no-op here.
    """
    lines = str(markdown or "").split("\n")
    to_remove: set = set()
    for info in results:
        for start, end in info.get("strip_ranges") or []:
            for i in range(start, end + 1):
                if 0 <= i < len(lines):
                    to_remove.add(i)
    kept = [line for i, line in enumerate(lines) if i not in to_remove]
    text = "\n".join(kept)
    return re.sub(r"\n{3,}", "\n\n", text)


def _attach_wordpress_infographics(*, html: str, results: list, live: bool, doc_seed: str) -> tuple:
    """Upload pre-rendered section infographics and insert each after its H2 heading."""
    from agent.content.markdown_html import h2_inner_html

    uploaded: list = []
    for info in results:
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


def _attach_blogspot_infographics(markdown: str, *, category_id: str, output_dir: str, style_seed: str) -> str:
    """Render section infographics and inline them as base64 data URIs.

    Blogger receives markdown that ``create_blogspot_draft`` converts to
    HTML itself (see ``publishers/blogspot.py``'s ``build_blogger_post``),
    so — unlike WordPress — there is no media-upload step: the image goes
    straight in as a ``data:`` URI, the same pattern the hero image already
    uses (``_embed_blogspot_hero_png``) to sidestep Blogger's unpredictable
    inline-``style`` stripping.
    """
    import base64

    results = _build_section_infographic_results(
        markdown=markdown, category_id=category_id, output_dir=output_dir, style_seed=style_seed,
    )
    if not results:
        return markdown

    narrative = _strip_infographic_source_blocks(markdown, results)
    lines = narrative.split("\n")
    for info in results:
        try:
            data = Path(str(info["file"])).read_bytes()
        except OSError:
            continue
        b64 = base64.b64encode(data).decode("ascii")
        data_uri = f"data:image/png;base64,{b64}"
        markdown_img = f"\n![{info['alt']}]({data_uri})\n"
        heading_line = f"## {info['heading']}"
        for i, line in enumerate(lines):
            if line.strip() == heading_line:
                lines.insert(i + 1, markdown_img)
                break
    return "\n".join(lines)


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

        category_result: Dict[str, Any] = {"id": None}
        if wp_live:
            try:
                category_result = resolve_or_create_wordpress_category(
                    site_url=site_url,
                    username=os.environ.get("WORDPRESS_USERNAME"),
                    app_password=os.environ.get("WORDPRESS_APP_PASSWORD"),
                    slug=item.category_id,
                    name=item.category_name,
                    live=wp_live,
                )
            except Exception as exc:  # noqa: BLE001 — draft must proceed without category
                logging.getLogger(__name__).warning(
                    "WordPress category resolve failed: %s", exc
                )

        seo_enrichment = enrich_wordpress_markdown_for_seo(
            publishable_content,
            focus_keyword=focus_keyword,
            category_id=item.category_id,
            platform_id="wordpress",
            seed=title,
            site_url=site_url,
            auth_header=auth_header,
            live=wp_live,
            topic_title=item.topic_title,
            wp_category_term_id=category_result.get("id"),
        )
        publishable_content = seo_enrichment["markdown"]
        # Extracted from the pre-infographic-strip markdown so FAQPage JSON-LD
        # reflects every 질문:/답변: pair the writer produced, even ones later
        # consumed by a section-infographic card (which only ever renders the
        # first 2 — see extract_all_qa_pairs()'s docstring).
        qa_pairs = extract_all_qa_pairs(publishable_content)

        # Section infographics are rendered from the pre-strip markdown, then
        # their source blocks (table rows / step headings / bullets / etc.)
        # are removed from the narrative copy BEFORE HTML conversion — this
        # is what stops the section's original text and its infographic card
        # from both ending up in the published HTML (see
        # _strip_infographic_source_blocks's docstring). Built once here and
        # reused below for upload/insertion so the same seeded style choices
        # and strip ranges are never rebuilt with a fresh (and potentially
        # different) roll.
        infographic_results: list = []
        narrative_content = publishable_content
        try:
            infographic_results = _build_section_infographic_results(
                markdown=publishable_content,
                category_id=item.category_id,
                output_dir=str(Path(item.blog_file).parent / "images"),
                style_seed=f"{item.platform}:{item.topic_title}:{title}",
            )
            narrative_content = _strip_infographic_source_blocks(publishable_content, infographic_results)
        except Exception as exc:  # noqa: BLE001 — draft must proceed even if infographic build fails
            logging.getLogger(__name__).warning(
                "WordPress section infographics build failed: %s", exc
            )
            infographic_results = []
            narrative_content = publishable_content

        # Must match markdown_to_html()'s own doc_seed formula exactly — the
        # HTML below is rendered with that seed, so anchors reconstructed
        # here have to use the identical seed or the H2 color/markup diverges
        # and the regex anchor silently fails to match.
        doc_lines = strip_frontmatter(narrative_content).split("\n")
        doc_seed = extract_title(narrative_content, "") or (doc_lines[0] if doc_lines else "post")

        payload = {
            "status": "draft",
            "title": title,
            "slug": raw_slug,
            "content": markdown_to_html(narrative_content),
        }
        if seo_meta.get("Meta description"):
            payload["excerpt"] = seo_meta["Meta description"]
        if category_result.get("id"):
            payload["categories"] = [category_result["id"]]

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

        # Hero image: WordPress rejects SVG, so an SVG hero is rendered to PNG
        # before upload. An AI-generated hero (hero_ai.<ext>) is already a
        # raster file and uploads as-is. Image failure never blocks the
        # draft itself — the text draft is the primary deliverable.
        media_result: Optional[Dict[str, Any]] = None
        image_file = str(item.image_file or "")
        if not image_file:
            images_dir = Path(item.blog_file).parent / "images"
            for candidate in ("title_card.png", "hero_ai.png", "hero_ai.jpg", "hero_ai.jpeg", "hero_ai.webp", "hero_adsense.svg"):
                fallback_image = images_dir / candidate
                if fallback_image.is_file():
                    image_file = str(fallback_image)
                    break
        if image_file:
            try:
                upload_path = image_file
                if Path(image_file).suffix.lower() == ".svg":
                    from agent.content.images.hero_image import render_hero_png

                    upload_path = render_hero_png(
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
                    file_path=upload_path,
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

        # Section infographics: up to 5 in-body cards matched to H2 sections
        # (already rendered above; this step only uploads + inserts them).
        # Same policy as the hero — image failure never blocks the draft.
        infographic_media: list[Dict[str, Any]] = []
        try:
            payload["content"], infographic_media = _attach_wordpress_infographics(
                html=payload["content"],
                results=infographic_results,
                live=wp_live,
                doc_seed=doc_seed,
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "WordPress section infographics failed: %s", exc
            )

        # Structured data (Article + conditional FAQPage JSON-LD): appended
        # last, as inline <script> tags in the rendered HTML body — neither
        # the WP REST client nor Rank Math's own meta endpoint can write into
        # the theme's actual <head> (see structured_data.py's module docstring).
        try:
            structured_html = build_structured_data_html(
                title=title,
                description=seo_meta.get("Meta description", ""),
                date_published_iso=f"{bundle.run_date}T00:00:00+09:00",
                image_url=(media_result or {}).get("sourceUrl"),
                qa_pairs=qa_pairs,
            )
            payload["content"] = f"{payload['content']}\n{structured_html}"
        except Exception as exc:  # noqa: BLE001 — draft must proceed without structured data
            logging.getLogger(__name__).warning(
                "WordPress structured data build failed: %s", exc
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
            "internalLinksFallbackOnly": seo_enrichment.get("internalLinksFallbackOnly"),
            "focusKeywordCount": seo_enrichment.get("focusKeywordCount"),
        }
        if seo_enrichment.get("internalLinksFallbackOnly"):
            logging.getLogger(__name__).warning(
                "WordPress draft published with fallback-only internal links "
                "(no related-post candidates found) — review recommended, not blocking"
            )
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
            seo_title = _build_rank_math_title(title, focus_keyword)
            if post_id and wp_live:
                rank_math_result = update_rank_math_seo(
                    site_url=os.environ.get("WORDPRESS_SITE_URL"),
                    username=os.environ.get("WORDPRESS_USERNAME"),
                    app_password=os.environ.get("WORDPRESS_APP_PASSWORD"),
                    post_id=int(post_id),
                    focus_keyword=focus_keyword,
                    seo_title=seo_title,
                    seo_description=seo_description[:160],
                    live=True,
                )
            else:
                rank_math_result = {
                    "apiCalled": False,
                    "focusKeyword": focus_keyword,
                    "seoTitle": seo_title,
                    "seoDescription": seo_description[:160],
                }
            if not is_seo_title_length_valid(seo_title):
                rank_math_result.setdefault("warnings", []).append(
                    format_seo_title_length_warning(
                        seo_title,
                        action="자동 수정 없이 발행",
                    )
                )
            result["rankMath"] = rank_math_result
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "Rank Math meta update failed: %s", exc
            )
            result["rankMath"] = {"apiCalled": False, "error": type(exc).__name__}
        return result

    if platform_id == "blogspot":
        blogspot_title = extract_title(blog_content, item.topic_title)
        # Extracted from the RAW blog_content, before _blogspot_publishable_markdown
        # strips frontmatter (category_name/category) and the SEO meta block
        # (태그 후보) — extract_labels() has nothing left to find once those
        # are gone. Same reason blogspot_title is extracted here instead of
        # inside build_blogger_post/create_blogspot_draft.
        blogspot_labels = extract_labels(blog_content)
        publishable = _blogspot_publishable_markdown(blog_content)
        from agent.content.seo_enrich import append_external_links, append_internal_links, fetch_blogspot_internal_candidates
        from agent.content.publishers.blogspot import exchange_blogger_access_token

        publishable = append_external_links(
            publishable, category_id=item.category_id, platform_id="blogspot", seed=blogspot_title,
            topic_title=item.topic_title,
        )
        # Internal links: reuse the category_name already sent as this post's
        # first Blogger label (see extract_labels()) to filter same-category
        # candidates. Never blocks the draft — a broken/expired refresh token
        # or any API error just skips internal-link enrichment for this post.
        if live:
            try:
                access_token = exchange_blogger_access_token(
                    client_id=os.environ.get("BLOGGER_CLIENT_ID"),
                    client_secret=os.environ.get("BLOGGER_CLIENT_SECRET"),
                    refresh_token=os.environ.get("BLOGGER_REFRESH_TOKEN"),
                )
                blogspot_candidates = fetch_blogspot_internal_candidates(
                    blog_id=os.environ.get("BLOGGER_BLOG_ID"),
                    access_token=access_token,
                    label=item.category_name,
                )
                publishable = append_internal_links(
                    publishable,
                    site_url=os.environ.get("BLOGSPOT_SITE_URL", ""),
                    candidates=blogspot_candidates,
                    max_links=3,
                    preferred_terms=[item.topic_title],
                )
            except Exception as exc:  # noqa: BLE001 — draft must proceed without internal links
                logging.getLogger(__name__).warning(
                    "Blogspot internal link enrichment failed: %s", exc
                )
        # Extracted before the infographic strip so FAQPage JSON-LD reflects
        # every 질문:/답변: pair, not just the first 2 a card image consumes.
        blogspot_qa_pairs = extract_all_qa_pairs(publishable)
        blogspot_seo_meta = extract_seo_meta(blog_content)

        # Section infographics: same generator as WordPress, but inlined as
        # base64 data URIs at the markdown level (no media-upload step —
        # Blogger renders whatever markdown_to_html produces from this
        # string). Image failure never blocks the draft.
        try:
            publishable = _attach_blogspot_infographics(
                publishable,
                category_id=item.category_id,
                output_dir=str(Path(item.blog_file).parent / "images"),
                style_seed=f"{item.platform}:{item.topic_title}:{blogspot_title}",
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "Blogspot section infographics failed: %s", exc
            )

        # Structured data (Article + conditional FAQPage JSON-LD): appended
        # after markdown_to_html() inside build_blogger_post(), never mixed
        # into the markdown itself (see build_blogger_post()'s docstring for
        # why raw <script> tags in markdown get HTML-escaped otherwise).
        # image_url is always omitted for Blogspot — the hero here is only
        # ever a base64 data URI, never a hosted URL.
        blogspot_structured_html = ""
        try:
            blogspot_structured_html = build_structured_data_html(
                title=blogspot_title,
                description=blogspot_seo_meta.get("Meta description", ""),
                date_published_iso=f"{bundle.run_date}T00:00:00+09:00",
                qa_pairs=blogspot_qa_pairs,
            )
        except Exception as exc:  # noqa: BLE001 — draft must proceed without structured data
            logging.getLogger(__name__).warning(
                "Blogspot structured data build failed: %s", exc
            )

        return create_blogspot_draft(
            markdown=publishable,
            title=blogspot_title,
            labels=blogspot_labels,
            blog_id=os.environ.get("BLOGGER_BLOG_ID"),
            client_id=os.environ.get("BLOGGER_CLIENT_ID"),
            client_secret=os.environ.get("BLOGGER_CLIENT_SECRET"),
            refresh_token=os.environ.get("BLOGGER_REFRESH_TOKEN"),
            live=live,
            extra_content_html=blogspot_structured_html,
        )

    if platform_id == "naver":
        return create_naver_draft(markdown=blog_content, live=live)

    if platform_id == "tistory":
        return create_tistory_draft(markdown=blog_content, live=live)

    raise ValueError(f"Unknown platform: {platform_id}")
