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


def _is_root_level_url(url: str) -> bool:
    """Prefer institution homepages / shallow top-level pages over deep
    notice-board or sub-pages. Real published-post bug: the domain filter
    alone let through a prosecutor's-office notice-board page (totally
    unrelated to the post's health topic) and a nifds.go.kr sub-path that
    turned out to be a dead link -- both multi-segment or query-string-heavy
    deep links. Every curated entry in ``_EXTERNAL_LINKS``/
    ``_TOPIC_EXTERNAL_LINKS`` is a bare domain root, so this just enforces
    the same shape on search-sourced results instead of accepting any depth.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.query:
        return False
    segments = [s for s in parts.path.split("/") if s]
    return len(segments) <= 1


def _is_reachable(url: str, *, timeout: float = 5.0) -> bool:
    """Best-effort liveness check before a search-sourced URL gets published
    as an "official reference" link. Never raises -- any failure (timeout,
    connection error, 4xx/5xx) just means the caller drops the candidate and
    moves on to the next one / the category pool, same fail-open posture as
    the rest of this fallback path."""
    try:
        response = httpx.head(url, timeout=timeout, follow_redirects=True)
        if response.status_code >= 400:
            response = httpx.get(url, timeout=timeout, follow_redirects=True)
        return response.status_code < 400
    except Exception:  # noqa: BLE001 — a network hiccup must not block publish
        return False


def _search_fallback_links(query: str) -> List[Tuple[str, str]]:
    """Best-effort: ask the active web-search provider (whichever backend
    ``web.search_backend`` resolves to — Tavily or otherwise) for an
    official page when the topic doesn't match anything in
    ``_TOPIC_EXTERNAL_LINKS``. Results are filtered to .go.kr/.or.kr domains,
    shallow/root-level URLs (see ``_is_root_level_url``), and must actually
    resolve (see ``_is_reachable``). Never raises — any failure (no provider
    configured, network error, empty results) just returns [] and the caller
    falls back to the category pool, same as before this existed.
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
        if not _is_root_level_url(url):
            continue
        if not _is_reachable(url):
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

SEO_TITLE_MIN_LENGTH = 28
SEO_TITLE_MAX_LENGTH = 40
SEO_TITLE_TRUNCATION_MAX_LENGTH = 60


def truncate_seo_title(title: str) -> str:
    """Trim an SEO title to 60 characters without inventing title content.

    When possible, the trim ends at the last whitespace boundary within the
    limit.  A title containing no whitespace before the limit is hard-cut so
    the Rank Math title ceiling remains enforceable.
    """
    value = str(title or "").strip()
    if len(value) <= SEO_TITLE_TRUNCATION_MAX_LENGTH:
        return value

    limited = value[:SEO_TITLE_TRUNCATION_MAX_LENGTH]
    boundary = max((index for index, char in enumerate(limited) if char.isspace()), default=-1)
    if boundary > 0:
        return limited[:boundary].rstrip()
    return limited


def is_seo_title_length_valid(title: str) -> bool:
    """Return whether a final SEO title is inside the 28~40 character target."""
    length = len(str(title or "").strip())
    return SEO_TITLE_MIN_LENGTH <= length <= SEO_TITLE_MAX_LENGTH


def format_seo_title_length_warning(title: str, *, action: str) -> str:
    """Build the compact, reportable SEO-title length warning."""
    length = len(str(title or "").strip())
    return (
        f"TITLE_LENGTH_OUT_OF_RANGE: SEO title {length}자 "
        f"(권장 {SEO_TITLE_MIN_LENGTH}~{SEO_TITLE_MAX_LENGTH}자, {action})"
    )


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
    min_results: int = 3,
) -> List[Dict[str, str]]:
    """Load recent published posts for internal linking.

    ``categories`` (a WordPress category term ID) scopes the query to posts
    in the same category when provided, so the scorer in
    ``append_internal_links`` ranks within a same-category pool instead of
    the whole site. When that scoped pool comes up short of ``min_results``
    (a brand-new or thin category — real published-post bug: a post got 0
    same-category candidates and published with just the single "블로그 홈"
    placeholder, tripping the SEO checker's "내부 링크가 3개 미만입니다"
    warning), tops up with a second, unscoped site-wide fetch instead of
    handing back a near-empty pool. ``append_internal_links``'s
    ``preferred_terms`` scoring still ranks the combined pool afterward, so
    off-category posts only surface when the same-category pool can't fill
    the quota on its own.
    """

    def _fetch(category_filter: Optional[int]) -> List[Dict[str, str]]:
        endpoint = (
            f"{str(site_url).rstrip('/')}/wp-json/wp/v2/posts"
            f"?per_page={limit}&status=publish&_fields=id,title,link"
        )
        if category_filter:
            endpoint += f"&categories={category_filter}"
        response = httpx.get(endpoint, headers={"Authorization": auth_header}, timeout=_TIMEOUT)
        if response.status_code >= 400:
            return []
        body = response.json() if response.content else []
        if not isinstance(body, list):
            return []
        fetched: List[Dict[str, str]] = []
        for item in body:
            post_id = item.get("id")
            if exclude_post_id and post_id == exclude_post_id:
                continue
            title = ((item.get("title") or {}).get("rendered") or "").strip()
            title = re.sub(r"<[^>]+>", "", title)
            link = str(item.get("link") or "").strip()
            if title and link:
                fetched.append({"id": str(post_id), "title": title, "link": link})
        return fetched

    results = _fetch(categories)
    if categories and len(results) < min_results:
        seen_ids = {r["id"] for r in results}
        for item in _fetch(None):
            if item["id"] in seen_ids:
                continue
            results.append(item)
            seen_ids.add(item["id"])
            if len(results) >= min_results:
                break
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

    def _split_terms(sources) -> list[str]:
        """제목 문장이 통째로 넘어와도 견줄 낱말로 쪼갠다."""
        out: list[str] = []
        for src in sources or []:
            for chunk in re.split(r"[^0-9A-Za-z가-힣]+", str(src or "")):
                chunk = chunk.strip()
                if len(chunk) >= 2:
                    out.append(chunk)
        seen: set[str] = set()
        return [t for t in out if not (t in seen or seen.add(t))]

    terms = _split_terms(preferred_terms)
    ranked = list(candidates)

    def _bigrams(word: str) -> set[str]:
        w = str(word or "")
        return {w[i:i + 2] for i in range(len(w) - 1)} if len(w) >= 2 else set()

    def _score(item: Dict[str, str]) -> int:
        title = item.get("title") or ""
        score = 0
        title_grams = _bigrams(title)
        for term in terms:
            if not term:
                continue
            if term in title:
                score += 5
                continue
            shared = _bigrams(term) & title_grams
            if shared:
                score += 2 * min(len(shared), 2)
        # Softly prefer non-tech filler when writing health/lifestyle posts.
        for noise in ("AI", "서버", "PC", "윈도우", "모니터", "슬랙", "노션"):
            if noise in title:
                score -= 1
        return score

    if terms:
        ranked = sorted(ranked, key=_score, reverse=True)
    related = [item for item in ranked if _score(item) > 0]
    if related:
        picks = sorted(related, key=_score, reverse=True)[:max_links]
    else:
        picks = ranked[:max_links]
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
    """Apply external/internal link enrichment without rewriting words."""
    original = markdown
    content = original
    thinned = False
    before_links = content
    content = append_external_links(
        content, category_id=category_id, platform_id=platform_id, seed=seed,
        topic_title=topic_title, focus_keyword=focus_keyword,
    )
    external_added = content != before_links

    internal: List[Dict[str, str]] = []
    internal_added = 0
    internal_fallback_only = False
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
            # No real candidates at all (even after fetch_wordpress_internal_
            # candidates' own site-wide top-up) -- append_internal_links()
            # fell back to the "블로그 홈"-only placeholder. Not an error (the
            # placeholder keeps Rank Math's presence-check happy), but a real
            # published post shipped with just 1 internal link this way and
            # tripped a separate SEO checker's "내부 링크가 3개 미만입니다"
            # warning, so this is worth surfacing rather than staying silent.
            internal_fallback_only = len(internal) == 0
            if internal_fallback_only:
                logger.warning(
                    "Internal links: published with fallback-only links "
                    "(no real same-category or site-wide candidates found)"
                )

    return {
        "markdown": content,
        "keywordThinned": thinned,
        "externalAdded": external_added,
        "internalCount": internal_added,
        "internalLinksFallbackOnly": internal_fallback_only,
        "focusKeywordCount": (
            len(re.findall(re.escape(focus_keyword), content)) if focus_keyword else 0
        ),
    }
