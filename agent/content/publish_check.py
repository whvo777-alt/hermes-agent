"""발행 직전 초안을 점검한다.

헤르메스는 공개하지 않고 초안까지만 올린다. 사용자가 공개를 누르기 전에
빠진 것이 있는지 알려주려고 만들었다. 2026-09-08 에 대표 이미지가 안
만들어진 것, 링크가 한 줄에 뭉친 것, 표 글자가 깨진 것을 사용자가
화면을 보고서야 알았다.

웹에 나가지 않는다. 이미 받아 온 HTML 글자만 본다.
"""

from __future__ import annotations

import re

from agent.content.publish_on_approval import _find_publish_content_blockers


_IMG_OPEN_RE = re.compile(r"<img(?=\s|/?>)", re.IGNORECASE)
_ANCHOR_OPEN_RE = re.compile(r"<a\b[^>]*>", re.IGNORECASE | re.DOTALL)
_HREF_RE = re.compile(
    r"(?<![\w-])href\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
    re.IGNORECASE,
)
_HTML_TAG_RE = re.compile(r"<[^>]*>", re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")


def _count_internal_links(html: str, site_host: str) -> int:
    """Return opening anchors whose href contains the configured site host."""
    if not site_host:
        return 0

    host = site_host.casefold()
    count = 0
    for anchor in _ANCHOR_OPEN_RE.finditer(html):
        href_match = _HREF_RE.search(anchor.group(0))
        if not href_match:
            continue
        href = next((value for value in href_match.groups() if value is not None), "")
        if host in href.casefold():
            count += 1
    return count


def _count_text_chars(html: str) -> int:
    """Remove HTML tags, collapse whitespace, and count remaining characters."""
    text = _HTML_TAG_RE.sub(" ", html)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return len(text)


def check_draft_html(
    html: str,
    *,
    site_host: str = "",
    expected_images: int = 0,
    min_chars: int = 1500,
) -> dict:
    """초안 HTML 을 보고 점검 결과를 돌려준다.

    site_host       내 블로그 이름. 내부 링크를 셀 때 쓴다. 없으면 안 센다
    expected_images 원고에서 만든 그림 수. 0 이면 견주지 않는다
    min_chars       본문이 이보다 짧으면 경고
    """
    source = str(html or "")
    image_count = len(_IMG_OPEN_RE.findall(source))
    hero_image = image_count > 0
    internal_links = _count_internal_links(source, site_host)
    blockers = _find_publish_content_blockers(source)
    text_chars = _count_text_chars(source)

    warnings: list[str] = []
    if not hero_image:
        warnings.append("대표 이미지가 없습니다.")
    if image_count == 0:
        warnings.append("본문 그림이 없습니다.")
    if expected_images != 0 and image_count < expected_images:
        warnings.append(
            f"본문 그림 수가 원고보다 적습니다: {image_count}장 / 예상 {expected_images}장."
        )
    if site_host and internal_links == 0:
        warnings.append("내부 링크가 없습니다.")
    for marker, line_number in blockers:
        warnings.append(f'금지 문구가 남아 있습니다: "{marker}" {line_number}번째 줄')
    if text_chars < min_chars:
        warnings.append(f"본문 글자 수가 너무 짧습니다: {text_chars}자 (최소 {min_chars}자).")

    return {
        "hero_image": hero_image,
        "image_count": image_count,
        "expected_images": expected_images,
        "internal_links": internal_links,
        "blockers": blockers,
        "text_chars": text_chars,
        "warnings": warnings,
    }


def _has_warning(result: dict, prefix: str) -> bool:
    return any(str(warning).startswith(prefix) for warning in result.get("warnings", []))


def _status(warning: bool) -> str:
    return "⚠️" if warning else "✅"


def format_check_report(result: dict, *, label: str = "") -> str:
    """점검 결과를 디스코드에 보낼 짧은 글자로 만든다."""
    image_count = int(result.get("image_count", 0))
    internal_links = int(result.get("internal_links", 0))
    text_chars = int(result.get("text_chars", 0))
    blockers = list(result.get("blockers", []))

    hero_warning = not bool(result.get("hero_image", False))
    image_warning = _has_warning(result, "본문 그림이 없습니다.") or _has_warning(
        result, "본문 그림 수가"
    )
    internal_warning = _has_warning(result, "내부 링크가 없습니다.")
    blocker_warning = bool(blockers)
    text_warning = _has_warning(result, "본문 글자 수가 너무 짧습니다.")

    if blockers:
        blocker_details = ", ".join(
            f'"{marker}" {line_number}번째 줄'
            for marker, line_number in blockers[:3]
        )
        remaining = len(blockers) - 3
        if remaining > 0:
            blocker_details += f" 외 {remaining}건"
        blocker_value = blocker_details
    else:
        blocker_value = "없음"

    heading = f"점검  {label}" if label else "점검"
    report = "\n".join(
        [
            heading,
            f"{_status(hero_warning)} 대표 이미지    {'없음' if hero_warning else '있음'}",
            f"{_status(image_warning)} 본문 그림      {image_count}장",
            f"{_status(internal_warning)} 내부 링크      {internal_links}개",
            f"{_status(blocker_warning)} 금지 문구      {blocker_value}",
            f"{_status(text_warning)} 본문 글자      {text_chars:,}자",
        ]
    )
    if len(report) <= 1000:
        return report
    return report[:997] + "..."
