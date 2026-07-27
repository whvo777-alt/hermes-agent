"""schema.org JSON-LD builders for WordPress/Blogspot published posts.

Neither the WordPress REST client nor the Blogger API gives this pipeline a
way to write into the actual theme ``<head>`` (see ``publishers/wordpress.py``
and ``publishers/blogspot.py`` — both are draft-content clients only, no
head/template access). Both platforms do render arbitrary inline HTML
already present in the post body, so structured data here is emitted as
``<script type="application/ld+json">`` tag(s) appended to the rendered post
HTML instead of a true ``<head>`` injection.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Sequence, Tuple

_HEADLINE_MAX_CHARS = 110


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def build_article_jsonld(
    *,
    title: str,
    description: str = "",
    date_published_iso: str = "",
    image_url: Optional[str] = None,
    author_name: str = "COCO Blog",
    publisher_name: str = "COCO Blog",
) -> Dict[str, Any]:
    """Build a schema.org/Article JSON-LD object.

    ``image_url`` is omitted entirely (not emitted as null/empty) when not
    available — Blogspot posts never have one (hero image is only ever a
    base64 data URI there, never a hosted URL).
    """
    data: Dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": _truncate(title, _HEADLINE_MAX_CHARS),
        "author": {"@type": "Organization", "name": author_name},
        "publisher": {"@type": "Organization", "name": publisher_name},
    }
    if description:
        data["description"] = description
    if date_published_iso:
        data["datePublished"] = date_published_iso
        data["dateModified"] = date_published_iso
    if image_url:
        data["image"] = image_url
    return data


def build_faqpage_jsonld(qa_pairs: Sequence[Tuple[str, str]]) -> Optional[Dict[str, Any]]:
    """Build a schema.org/FAQPage JSON-LD object, or ``None`` if there are no
    FAQ pairs (so callers never emit an empty/meaningless FAQPage block)."""
    pairs = [(q, a) for q, a in (qa_pairs or []) if q and a]
    if not pairs:
        return None
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": question,
                "acceptedAnswer": {"@type": "Answer", "text": answer},
            }
            for question, answer in pairs
        ],
    }


def render_jsonld_script_tag(data: Dict[str, Any]) -> str:
    """Serialize ``data`` into a single ``<script type="application/ld+json">``
    tag. Escapes "</" so an answer/description containing a literal
    "</script>"-like substring can't prematurely close the tag."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")
    return f'<script type="application/ld+json">{payload}</script>'


def build_structured_data_html(
    *,
    title: str,
    description: str = "",
    date_published_iso: str = "",
    image_url: Optional[str] = None,
    qa_pairs: Sequence[Tuple[str, str]] = (),
    author_name: str = "COCO Blog",
    publisher_name: str = "COCO Blog",
) -> str:
    """Build the full structured-data HTML block to append to a post body:
    one Article <script> tag, plus a second FAQPage <script> tag when
    ``qa_pairs`` is non-empty. Two separate tags (rather than one @graph)
    so a bug in FAQ extraction can never corrupt the Article block."""
    article = build_article_jsonld(
        title=title,
        description=description,
        date_published_iso=date_published_iso,
        image_url=image_url,
        author_name=author_name,
        publisher_name=publisher_name,
    )
    tags = [render_jsonld_script_tag(article)]
    faq = build_faqpage_jsonld(qa_pairs)
    if faq is not None:
        tags.append(render_jsonld_script_tag(faq))
    return "\n".join(tags)
