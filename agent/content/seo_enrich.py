"""WordPress / Rank Math SEO enrichment helpers.

Applied at publish time so drafts pass Rank Math checks that the LLM alone
often misses: keyword density, internal links, external DoFollow links, and
a number in the SEO title.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from agent.content.visual_accents import _seed_int

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0

# Trusted outbound references by category. Real institutional URLs only —
# never invent product pages or affiliate links.
_EXTERNAL_LINKS: Dict[str, List[Tuple[str, str]]] = {
    "health": [
        ("식품의약품안전처", "https://www.mfds.go.kr/"),
        ("질병관리청", "https://www.kdca.go.kr/"),
        ("국가건강정보포털", "https://health.kdca.go.kr/"),
        ("한국건강증진개발원", "https://www.khealth.or.kr/"),
        ("대한의학회", "https://www.kams.or.kr/"),
    ],
    "finance": [
        ("금융감독원", "https://www.fss.or.kr/"),
        ("기획재정부", "https://www.moef.go.kr/"),
        ("한국소비자원", "https://www.kca.go.kr/"),
        ("서민금융진흥원", "https://www.kinfa.or.kr/"),
        ("한국조세재정연구원", "https://www.kipf.re.kr/kor/"),
    ],
    "it-tech": [
        ("한국지능정보사회진흥원", "https://www.nia.or.kr/"),
        ("개인정보보호위원회", "https://www.pipc.go.kr/"),
        ("한국인터넷진흥원", "https://www.kisa.or.kr/"),
        ("정보통신정책연구원", "https://www.kisdi.re.kr/"),
        ("과학기술정보통신부", "https://www.msit.go.kr/"),
    ],
    "self-dev": [
        ("고용노동부", "https://www.moel.go.kr/"),
        ("한국직업능력연구원", "https://www.krivet.re.kr/"),
        ("한국산업인력공단", "https://www.hrdkorea.or.kr/"),
        ("국가평생교육진흥원", "https://www.nile.or.kr/"),
    ],
    "parenting": [
        ("질병관리청", "https://www.kdca.go.kr/"),
        ("여성가족부", "https://www.mogef.go.kr/"),
        ("육아정책연구소", "https://www.kicce.re.kr/"),
        ("아이사랑", "https://www.childcare.go.kr/"),
        ("보건복지부", "https://www.mohw.go.kr/"),
    ],
    "travel": [
        ("대한민국 구석구석", "https://korean.visitkorea.or.kr/"),
        ("외교부 해외안전여행", "https://www.0404.go.kr/"),
    ],
}
_DEFAULT_EXTERNAL = [
    ("대한민국 정부 포털", "https://www.gov.kr/"),
]

# Topic-keyword -> institution, matched against the actual topic
# title/focus keyword BEFORE falling back to the category-wide pool above.
# Fixes: a "건강보험료" post no longer gets a random health-category link
# (식약처/질병관리청/...) that has nothing to do with insurance premiums —
# it gets 국민건강보험공단. Order matters: first (most specific) match wins,
# so check narrower patterns before broader ones. Real institutional URLs
# only, same rule as ``_EXTERNAL_LINKS`` above.
_TOPIC_EXTERNAL_LINKS: List[Tuple[re.Pattern, List[Tuple[str, str]]]] = [
    (re.compile(r"건강보험료|건강보험"), [("국민건강보험공단", "https://www.nhis.or.kr/")]),
    (re.compile(r"국민연금|노령연금|연금\s*수급"), [("국민연금공단", "https://www.nps.or.kr/")]),
    (re.compile(r"식품|영양제|건강기능식품|보충제|첨가물"), [("식품의약품안전처", "https://www.mfds.go.kr/")]),
    (re.compile(r"질병|감염|백신|전염|예방접종"), [("질병관리청", "https://www.kdca.go.kr/")]),
    (re.compile(r"의료|병원|진료|의학|처방"), [("대한의학회", "https://www.kams.or.kr/")]),
    (re.compile(r"세금|절세|연말정산|종합소득세"), [("국세청", "https://www.nts.go.kr/")]),
    (re.compile(r"대출|금리|예금|적금|저축"), [("금융감독원", "https://www.fss.or.kr/")]),
    (re.compile(r"개인정보|정보보호|보안\s*사고"), [("개인정보보호위원회", "https://www.pipc.go.kr/")]),
    (re.compile(r"통신|5g|와이파이|인터넷\s*속도"), [("과학기술정보통신부", "https://www.msit.go.kr/")]),
    (re.compile(r"취업|이직|채용|자격증"), [("한국산업인력공단", "https://www.hrdkorea.or.kr/")]),
    (re.compile(r"육아휴직|출산휴가|근로\s*기준"), [("고용노동부", "https://www.moel.go.kr/")]),
    (re.compile(r"이유식|영유아\s*건강|신생아"), [("아이사랑", "https://www.childcare.go.kr/")]),
    (re.compile(r"해외여행|해외\s*안전"), [("외교부 해외안전여행", "https://www.0404.go.kr/")]),
    (re.compile(r"국내여행|국내\s*숙박"), [("대한민국 구석구석", "https://korean.visitkorea.or.kr/")]),
]

_OFFICIAL_DOMAIN_RE = re.compile(r"\.go\.kr(?:/|$)|\.or\.kr(?:/|$)", re.I)


def _search_fallback_links(query: str) -> List[Tuple[str, str]]:
    """Best-effort: ask the active web-search provider (whichever backend
    ``web.search_backend`` resolves to — Tavily or otherwise) for an
    official page when the topic doesn't match anything in
    ``_TOPIC_EXTERNAL_LINKS``. Results are filtered to .go.kr/.or.kr domains
    only. Never raises — any failure (no provider configured, network
    error, empty results) just returns [] and the caller falls back to the
    category pool, same as before this existed.
    """
    query = (query or "").strip()
    if not query:
        return []
    try:
        from agent.web_search_registry import get_active_search_provider

        provider = get_active_search_provider()
        if provider is None or not provider.supports_search() or not provider.is_available():
            return []
        result = provider.search(f"{query} 공식 기관", limit=5)
    except Exception as exc:  # noqa: BLE001 — search fallback must never block publish
        logger.warning("External link search fallback failed for %r: %s", query, exc)
        return []
    if not isinstance(result, dict) or not result.get("success"):
        return []
    web_results = ((result.get("data") or {}).get("web")) or []
    picked: List[Tuple[str, str]] = []
    for item in web_results:
        url = str((item or {}).get("url") or "").strip()
        title = str((item or {}).get("title") or "").strip()
        if not url or not title or not _OFFICIAL_DOMAIN_RE.search(url):
            continue
        picked.append((title[:40], url))
        if len(picked) >= 2:
            break
    return picked


def _pick_external_links_for_topic(
    *, topic_title: str, focus_keyword: str, category_id: str, platform_id: str, seed: str,
) -> List[Tuple[str, str]]:
    """Topic keywords first, then the category pool to top up, then (only
    when the topic matched nothing at all) a live search fallback."""
    text = f"{topic_title or ''} {focus_keyword or ''}".strip()
    matched: List[Tuple[str, str]] = []
    seen_urls: set = set()

    if text:
        for pattern, links in _TOPIC_EXTERNAL_LINKS:
            if not pattern.search(text):
                continue
            for label, url in links:
                if url in seen_urls:
                    continue
                matched.append((label, url))
                seen_urls.add(url)
                if len(matched) >= 2:
                    break
            if len(matched) >= 2:
                break

    if not matched:
        matched = _search_fallback_links(text)
        seen_urls.update(url for _label, url in matched)

    if len(matched) < 2:
        category_pool = _EXTERNAL_LINKS.get(category_id, _DEFAULT_EXTERNAL)
        remaining = [c for c in category_pool if c[1] not in seen_urls]
        top_up = _pick_external_links(remaining, platform_id=platform_id, category_id=category_id, seed=seed)
        matched.extend(top_up[: 2 - len(matched)])

    return matched[:2]

_EXTERNAL_LINKS_TOPIC_LABEL: Dict[str, str] = {
    "health": "건강·생활",
    "finance": "재테크·경제",
    "it-tech": "IT·기술",
    "self-dev": "자기계발·경력",
    "parenting": "육아·가족",
}
_DEFAULT_TOPIC_LABEL = "건강·생활"

_KEYWORD_SYNONYMS: Dict[str, List[str]] = {
    "요가": ["수업", "동작", "스트레칭", "매트 운동", "운동"],
    "영양제": ["제품", "건강기능식품", "보충제", "해당 제품", "성분 제품"],
    "다이어트": ["체중 관리", "식단 관리", "감량 목표", "체중 조절", "식습관 관리"],
}

# Longer phrases first so Rank Math substring density drops without
# breaking Hangul tokens like 필요가.
_KEYWORD_PHRASE_REPLACEMENTS: Dict[str, List[Tuple[str, str]]] = {
    "요가": [
        ("요가원", "스튜디오"),
        ("요가 강사", "강사"),
        ("요가 수업", "수업"),
        ("요가 효과", "운동 효과"),
        ("요가 시작", "운동 시작"),
        ("요가 선택", "수업 선택"),
        ("요가 준비", "수업 준비"),
        ("홈트 요가", "홈트"),
        ("요가 매트", "매트"),
    ],
    "영양제": [
        ("영양제 선택", "제품 선택"),
        ("영양제 섭취", "제품 섭취"),
        ("영양제 구매", "제품 구매"),
    ],
}


def ensure_number_in_seo_title(title: str, *, fallback_number: int = 7) -> str:
    """Rank Math title readability wants a digit in the SEO title."""
    value = str(title or "").strip()
    if re.search(r"\d", value):
        return value[:60]
    # Prefer inserting near the front after the focus phrase.
    if ":" in value:
        head, tail = value.split(":", 1)
        return f"{head.strip()} {fallback_number}가지:{tail}"[:60]
    return f"{value} {fallback_number}가지 체크"[:60]


def thin_focus_keyword(markdown: str, focus_keyword: str, *, max_occurrences: int = 14) -> str:
    """Reduce focus-keyword spam while keeping early/heading matches.

    Rank Math flags density above ~2.5%. For a ~1000-word Korean post,
    ~12–15 occurrences (~1–1.5%) is a safe target.
    """
    keyword = (focus_keyword or "").strip()
    if not keyword or max_occurrences < 1:
        return markdown

    value = markdown
    # Rank Math does raw substring matching. Short Korean keywords can hide
    # inside unrelated words (요가 ⊂ 필요가). Break those false positives first.
    if keyword == "요가":
        value = value.replace("필요가", "필요")
        value = value.replace("할 필요 없습니다", "할 필요는 없습니다")
        value = value.replace("할 필요없습니다", "할 필요는 없습니다")

    # 1) Replace longer compounds first (each removal drops substring hits).
    for src, dst in _KEYWORD_PHRASE_REPLACEMENTS.get(keyword, []):
        matches = list(re.finditer(re.escape(src), value))
        if len(matches) <= 1:
            continue
        parts: List[str] = []
        last = 0
        for i, match in enumerate(matches):
            parts.append(value[last:match.start()])
            line_start = value.rfind("\n", 0, match.start()) + 1
            on_heading = bool(re.match(r"^#{1,3}\s+", value[line_start:match.start() + len(src)]))
            if i == 0 or on_heading:
                parts.append(match.group(0))
            else:
                parts.append(dst)
            last = match.end()
        parts.append(value[last:])
        value = "".join(parts)

    # 2) Replace leftover keyword hits down to max_occurrences.
    # Prefer standalone tokens, but if density is still high (compounds /
    # Rank Math substring counting), thin remaining raw occurrences too.
    pattern = re.compile(re.escape(keyword))
    matches = []
    for match in pattern.finditer(value):
        before = value[: match.start()]
        if before.rfind("<") > before.rfind(">"):
            continue  # inside an HTML tag
        matches.append(match)
    if len(matches) <= max_occurrences:
        return value

    synonyms = _KEYWORD_SYNONYMS.get(keyword, ["이것", "해당 항목", "관련 내용"])
    keep_indexes = set(range(min(4, max_occurrences)))
    lines = value.split("\n")
    offset = 0
    heading_spans = []
    for line in lines:
        if re.match(r"^#{1,3}\s+", line):
            heading_spans.append((offset, offset + len(line)))
        offset += len(line) + 1
    for i, match in enumerate(matches):
        if any(start <= match.start() < end for start, end in heading_spans):
            keep_indexes.add(i)
    for i in range(len(matches)):
        if len(keep_indexes) >= max_occurrences:
            break
        keep_indexes.add(i)

    parts = []
    last = 0
    replace_i = 0
    for i, match in enumerate(matches):
        parts.append(value[last:match.start()])
        if i in keep_indexes:
            parts.append(match.group(0))
        else:
            parts.append(synonyms[replace_i % len(synonyms)])
            replace_i += 1
        last = match.end()
    parts.append(value[last:])
    result = "".join(parts)
    if keyword == "요가":
        result = result.replace("필요가", "필요")
    return result


def _has_external_link(markdown: str) -> bool:
    for match in re.finditer(r"\]\((https?://[^)\s]+)\)", markdown or ""):
        url = match.group(1).lower()
        if "example.com" in url or "localhost" in url:
            continue
        return True
    return False


def _has_internal_link(markdown: str, site_url: str) -> bool:
    host = re.sub(r"^https?://", "", (site_url or "").rstrip("/"))
    if not host:
        return False
    return bool(re.search(rf"\]\(https?://{re.escape(host)}", markdown or "", flags=re.I))


def _pick_external_links(
    candidates: List[Tuple[str, str]], *, platform_id: str, category_id: str, seed: str,
) -> List[Tuple[str, str]]:
    """Deterministically pick 2 candidates from the category pool.

    Seeded by (seed, platform_id, category_id) so re-rendering the same
    document always reproduces the same pair, but different platforms
    publishing the same category don't collide on the same two institutions.
    """
    if len(candidates) <= 2:
        return list(candidates)
    start = _seed_int(seed, platform_id, category_id) % len(candidates)
    return [candidates[start], candidates[(start + 1) % len(candidates)]]


def append_external_links(
    markdown: str, *, category_id: str, platform_id: str = "", seed: str = "",
    topic_title: str = "", focus_keyword: str = "",
) -> str:
    """Append a short DoFollow reference section if none exist yet.

    Links are matched to the actual topic (``topic_title``/``focus_keyword``)
    via ``_TOPIC_EXTERNAL_LINKS`` first; the category-wide pool only tops up
    what the topic match didn't cover. Passing neither keeps the old
    category-only behavior (topic match finds nothing to match against, so
    it's a no-op and every link comes from the category pool, unchanged).
    """
    if _has_external_link(markdown):
        return markdown
    links = _pick_external_links_for_topic(
        topic_title=topic_title, focus_keyword=focus_keyword,
        category_id=category_id, platform_id=platform_id, seed=seed,
    )
    if not links:
        return markdown
    if re.search(r"^##\s*(참고|출처|외부)", markdown or "", flags=re.M):
        # Extend an existing reference section instead of duplicating.
        block = "\n".join(f"- [{label}]({url})" for label, url in links)
        return re.sub(
            r"(^##\s*(?:참고|출처|외부)[^\n]*\n)",
            rf"\1{block}\n",
            markdown,
            count=1,
            flags=re.M,
        )
    lines = [
        "",
        "## 참고할 수 있는 공식 자료",
        (
            f"아래 기관 자료는 일반 {_EXTERNAL_LINKS_TOPIC_LABEL.get(category_id, _DEFAULT_TOPIC_LABEL)} "
            "정보를 확인할 때 참고할 수 있습니다."
        ),
    ]
    lines.extend(f"- [{label}]({url})" for label, url in links)
    return markdown.rstrip() + "\n" + "\n".join(lines) + "\n"


def fetch_wordpress_internal_candidates(
    *,
    site_url: str,
    auth_header: str,
    exclude_post_id: Optional[int] = None,
    categories: Optional[int] = None,
    limit: int = 20,
) -> List[Dict[str, str]]:
    """Load recent published posts for internal linking.

    ``categories`` (a WordPress category term ID) scopes the query to posts
    in the same category when provided, so the scorer in
    ``append_internal_links`` ranks within a same-category pool instead of
    the whole site.
    """
    endpoint = (
        f"{str(site_url).rstrip('/')}/wp-json/wp/v2/posts"
        f"?per_page={limit}&status=publish&_fields=id,title,link"
    )
    if categories:
        endpoint += f"&categories={categories}"
    response = httpx.get(endpoint, headers={"Authorization": auth_header}, timeout=_TIMEOUT)
    if response.status_code >= 400:
        return []
    body = response.json() if response.content else []
    if not isinstance(body, list):
        return []
    results: List[Dict[str, str]] = []
    for item in body:
        post_id = item.get("id")
        if exclude_post_id and post_id == exclude_post_id:
            continue
        title = ((item.get("title") or {}).get("rendered") or "").strip()
        title = re.sub(r"<[^>]+>", "", title)
        link = str(item.get("link") or "").strip()
        if title and link:
            results.append({"id": str(post_id), "title": title, "link": link})
    return results


def fetch_blogspot_internal_candidates(
    *,
    blog_id: str,
    access_token: str,
    label: str,
    exclude_post_id: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, str]]:
    """Load recent live Blogger posts sharing ``label`` for internal linking.

    ``label`` is normally ``item.category_name`` — ``extract_labels()``
    already sends the category name as a Blogger label on every post, so
    this reuses that existing label rather than adding a new one.
    """
    if not blog_id or not access_token or not label:
        return []
    endpoint = f"https://www.googleapis.com/blogger/v3/blogs/{blog_id}/posts"
    response = httpx.get(
        endpoint,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"status": "live", "labels": label, "maxResults": limit, "fetchBodies": "false"},
        timeout=_TIMEOUT,
    )
    if response.status_code >= 400:
        return []
    body = response.json() if response.content else {}
    items = body.get("items") or []
    if not isinstance(items, list):
        return []
    results: List[Dict[str, str]] = []
    for item in items:
        post_id = item.get("id")
        if exclude_post_id and post_id == exclude_post_id:
            continue
        title = str(item.get("title") or "").strip()
        link = str(item.get("url") or "").strip()
        if title and link:
            results.append({"id": str(post_id), "title": title, "link": link})
    return results


def append_internal_links(
    markdown: str,
    *,
    site_url: str,
    candidates: Sequence[Dict[str, str]],
    max_links: int = 3,
    preferred_terms: Optional[Sequence[str]] = None,
) -> str:
    """Insert up to N internal links near the end of the article."""
    if _has_internal_link(markdown, site_url):
        return markdown
    terms = [t for t in (preferred_terms or []) if t]
    ranked = list(candidates)

    def _score(item: Dict[str, str]) -> int:
        title = item.get("title") or ""
        score = 0
        for term in terms:
            if term and term in title:
                score += 3
        # Softly prefer non-tech filler when writing health/lifestyle posts.
        for noise in ("AI", "서버", "PC", "윈도우", "모니터", "슬랙", "노션"):
            if noise in title:
                score -= 1
        return score

    if terms:
        ranked = sorted(ranked, key=_score, reverse=True)
    picks = [item for item in ranked if _score(item) >= 0][:max_links]
    if picks and len(picks) < max_links:
        logger.info(
            "Internal links: only %d/%d same-category candidates available",
            len(picks), max_links,
        )
    if not picks:
        # Site has no related posts yet — still satisfy Rank Math with real
        # internal URLs (home + a category archive when available).
        host = str(site_url).rstrip("/")
        picks = [{"title": "블로그 홈", "link": f"{host}/"}]
        if any("category/it" in (c.get("link") or "") for c in candidates):
            picks.append({"title": "IT 관련 글 모음", "link": f"{host}/category/it/"})
        picks = picks[:max_links]
        logger.info("Internal links: no same-category candidates, using fallback links")
    if not picks:
        return markdown
    block_lines = [
        "",
        "## 함께 읽으면 좋은 글",
        "같은 블로그에서 이어서 보면 도움이 되는 글입니다.",
    ]
    block_lines.extend(f"- [{item['title']}]({item['link']})" for item in picks)
    block = "\n".join(block_lines) + "\n"

    match = re.search(r"^##\s*마무리.*$", markdown or "", flags=re.M)
    if match:
        idx = match.start()
        return markdown[:idx].rstrip() + "\n" + block + "\n" + markdown[idx:]
    return markdown.rstrip() + "\n" + block


def enrich_wordpress_markdown_for_seo(
    markdown: str,
    *,
    focus_keyword: str,
    category_id: str,
    platform_id: str = "wordpress",
    seed: str = "",
    site_url: str = "",
    auth_header: str = "",
    exclude_post_id: Optional[int] = None,
    live: bool = False,
    topic_title: str = "",
    wp_category_term_id: Optional[int] = None,
    internal_link_target: int = 3,
) -> Dict[str, Any]:
    """Apply density thinning + external/internal link enrichment."""
    original = markdown
    content = thin_focus_keyword(original, focus_keyword)
    thinned = content != original
    before_links = content
    content = append_external_links(
        content, category_id=category_id, platform_id=platform_id, seed=seed,
        topic_title=topic_title, focus_keyword=focus_keyword,
    )
    external_added = content != before_links

    internal: List[Dict[str, str]] = []
    internal_added = 0
    if live and site_url and auth_header:
        internal = fetch_wordpress_internal_candidates(
            site_url=site_url,
            auth_header=auth_header,
            exclude_post_id=exclude_post_id,
            categories=wp_category_term_id,
        )
        before_internal = content
        content = append_internal_links(
            content,
            site_url=site_url,
            candidates=internal,
            max_links=internal_link_target,
            preferred_terms=[focus_keyword],
        )
        if content != before_internal:
            internal_added = min(internal_link_target, len(internal))

    return {
        "markdown": content,
        "keywordThinned": thinned,
        "externalAdded": external_added,
        "internalCount": internal_added,
        "focusKeywordCount": (
            len(re.findall(re.escape(focus_keyword), content)) if focus_keyword else 0
        ),
    }
