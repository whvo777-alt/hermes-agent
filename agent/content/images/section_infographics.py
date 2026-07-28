"""Section infographic generator.

Renders up to 5 in-body infographic cards from the blog markdown, one per
strong H2 section. Styles: grid (tables), checklist/timeline (ordered or
unordered item lists — the two are visually distinct but both a fair
representation of "a list of things"), gauge (a section built around one
standout stat), quote (pull-quote fallback for sections with no structured
data — used to keep the in-body image count at floor), before_after (나쁜
예/고친 예 contrast pairs). Style choice for shapes that have more than one
honest rendering (steps/numbered lists) is seeded via
``agent.content.images.style_rotation`` — the same seeding mechanism
``visual_accents.choose_hero_mode`` uses for hero selection — so hero and
section images share one picker instead of duplicating hash logic.

Cards never redraw the section's H2 title (the H2 already carries it in the
surrounding HTML) — only a short style label + marker glyph at the top, plus
the actual extracted data. Each ``InfographicSpec`` also carries
``strip_ranges``: the exact absolute line numbers of the source markdown
block it visualizes (table rows / step headings / numbered items / bullets /
나쁜예-고친예 pairs), so the caller can remove that block from the narrative
markdown before HTML conversion instead of leaving both the image AND the
original text in the published post. Quote-style cards are the one
exception — a pull quote intentionally repeats a sentence already in the
body, matching standard pull-quote convention, so nothing is stripped for
them.

Pure local PIL drawing — no LLM call, no network call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent.content.images.card_icons import pick_icon_for_text, draw_icon
from agent.content.images.hero_image import _korean_font  # shared font lookup
from agent.content.images.style_rotation import choose_card_skin, choose_style
from agent.content.images.text_fit import wrap_text as _wrap

_PALETTES: Dict[str, Tuple[str, str, str]] = {
    # (accent/main, deep header, light tint)
    "health": ("#2e7d32", "#1b4d21", "#f1f8f2"),
    "parenting": ("#ef6c00", "#9a4600", "#fdf6ef"),
    # Was blue (#1565c0) — a straight regression against the orange/purple
    # identity self-dev posts have actually shipped with (hero's rotation
    # includes "warm orange" and "lilac" for self-dev). Purple here, not
    # orange, so it doesn't collide with parenting's fixed orange above.
    "self-dev": ("#7b1fa2", "#4a148c", "#f5f0fa"),
    "it-tech": ("#3949ab", "#232c6e", "#f1f2fa"),
    "finance": ("#00695c", "#00443b", "#eff7f6"),
    "travel": ("#0277bd", "#014f7c", "#eff7fc"),
}
_DEFAULT_PALETTE = ("#37474f", "#232f36", "#f4f6f7")

_STYLE_LABELS: Dict[str, str] = {
    "grid": "비교",
    "checklist": "체크",
    "timeline": "단계",
    "gauge": "기준",
    "quote": "핵심",
    "quote_keyword": "포인트",
    "before_after": "비교 전/후",
    "qa": "Q&A",
    "ox_quiz": "통념 깨기",
    "risk_tier": "등급 비교",
}

# Structural / meta sections — link lists and boilerplate connectors, never
# real visual content, so they must never become an infographic card even as
# a last-resort "quote" filler. "함께 읽으면"/"관련 글" etc. catch
# seo_enrich.append_internal_links()'s "## 함께 읽으면 좋은 글" block: it's
# too short (<=2 bullets) to qualify as a checklist, so without this it fell
# through to _extract_quote() and turned its connector sentence ("같은
# 블로그에서 이어서 보면 도움이 되는 글입니다.") into a fake pull-quote card.
_SKIP_HEADING_RE = re.compile(
    r"출처|참고|마무리|결론|FAQ|자주\s*묻는|핵심\s*요약|"
    r"함께\s*읽으면|관련\s*글|관련\s*포스트|관련\s*아티클|이어서\s*보면|더\s*보기|추천\s*글"
)
_TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")
_STEP_RE = re.compile(r"^###\s+(\d+단계\s*[:：]?\s*.+)$")
_NUMBERED_ITEM_RE = re.compile(r"^(\d+)\.\s+(.+)$")
_NUMBERED_DESC_RE = re.compile(r"^:\s*(.+)$")
_BULLET_RE = re.compile(r"^[-*]\s+(.+)$")
_BAD_EXAMPLE_RE = re.compile(r"^나쁜\s*예\s*(?:→|->|:|：)\s*(.+)$")
_GOOD_EXAMPLE_RE = re.compile(r"^고친\s*예\s*(?:→|->|:|：)\s*(.+)$")
# Deliberately requires an explicit Q:/질문:-style marker rather than
# guessing from "any sentence ending in a question mark" -- ordinary prose
# is full of rhetorical questions (writer.py's own "반문" tone guidance
# encourages them) that are not a real FAQ pair, and a fuzzy heuristic would
# misrepresent plain narrative as a Q&A card. This only fires on content
# that already marks itself as Q&A explicitly.
_QA_QUESTION_RE = re.compile(r"^(?:Q|질문)\s*(?:\d+)?\s*[:：]\s*(.+)$", re.I)
_QA_ANSWER_RE = re.compile(r"^(?:A|답변)\s*(?:\d+)?\s*[:：]\s*(.+)$", re.I)
# Same conservative philosophy as Q&A above: explicit markers only, not a
# guess from "any myth-shaped sentence" -- "가 아니라" contrast clauses are
# extremely common in this pipeline's prose (see anti-ai.md's varied-
# contrast guidance) but extracting them generically produces inconsistent-
# quality fragments as a standalone headline. Requires the writer to have
# actually marked a section as myth-busting.
_OX_MYTH_RE = re.compile(r"^(?:통념|흔한\s*오해)\s*[:：]\s*(.+)$")
_OX_FACT_RE = re.compile(r"^(?:사실|정답)\s*[:：]\s*(.+)$")
# 3-tier risk/grade markers -- all three must appear in the same section.
_RISK_TIER_RE = re.compile(r"^(안전|낮음|중간|보통|위험|높음|주의)\s*[:：]\s*(.+)$")
_RISK_TIER_GROUP = {"안전": "safe", "낮음": "safe", "중간": "mid", "보통": "mid", "위험": "risk", "높음": "risk", "주의": "risk"}
# Deliberately narrow: only units that are near-never ordinary sentence
# filler in Korean prose. An earlier version also matched 시간/분/회/세/일/년
# ("1일 섭취량" = "daily intake") and mis-fired on completely generic
# phrases — kg/kcal/mmHg/mg/%/ml/L are concrete measurements that don't
# double as common counter words, so they're a much more honest signal of
# "this section is actually built around one stat."
_GAUGE_STAT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|kcal|mmHg|mg|%|리터|L|ml)\b")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?다요])\s+")


@dataclass
class InfographicSpec:
    heading: str          # original markdown H2 text (used to locate the HTML anchor)
    display_title: str    # cleaned title — used for alt text only, never drawn on the card
    shape: str = ""        # "grid" | "before_after" | "ordered" | "bullets" | "quote" — the DATA shape;
                            # drives both which styles are honest for it and how it finalizes after allocation
    style: str = ""        # concrete render style, filled in by _allocate_styles() — empty until then
    eligible_styles: Tuple[str, ...] = ()  # every style that would honestly represent this section's data
    items: List[str] = field(default_factory=list)
    table: Optional[List[List[str]]] = None            # header + body rows, for "grid"
    gauge_stat: Optional[Tuple[str, str]] = None        # (number, unit), for "gauge"
    gauge_label: str = ""                               # the sentence the stat came from, for "gauge"
    before_pairs: List[Tuple[str, str]] = field(default_factory=list)  # (나쁜 예, 고친 예)
    qa_pairs: List[Tuple[str, str]] = field(default_factory=list)  # (질문, 답변), for "qa" shape
    ox_pair: Optional[Tuple[str, str]] = None  # (통념, 사실), for "ox_quiz" shape
    risk_tiers: List[Tuple[str, str, str]] = field(default_factory=list)  # (group, label, desc) x3, for "risk_tier" shape
    quote_text: str = ""                                # for "quote"
    strip_ranges: List[Tuple[int, int]] = field(default_factory=list)  # absolute [start,end] line indices to drop from source markdown
    # "bullets"-shape only: the alternate checklist rendering (items/ranges),
    # used when allocation doesn't award this section "gauge".
    _checklist_items: List[str] = field(default_factory=list)
    _checklist_ranges: List[Tuple[int, int]] = field(default_factory=list)
    # Set by build_section_infographics right before rendering (never by
    # extraction). Currently unused -- their only consumer, skin C's
    # decorative "page N" chip, was removed; kept on the dataclass in case
    # a future skin wants per-card position/count again.
    card_index: int = 0
    card_total: int = 0


def _clean_inline(text: str) -> str:
    value = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    value = re.sub(r"`([^`]*)`", r"\1", value)
    value = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def _display_title(heading: str) -> str:
    return _clean_inline(re.sub(r"^\d+\.\s*", "", heading)).strip()


def _split_table_row(line: str) -> List[str]:
    return [_clean_inline(cell) for cell in line.strip().strip("|").split("|")]


def _extract_table_with_range(lines: List[str]) -> Optional[Tuple[List[List[str]], int, int]]:
    """Locate a markdown table within ``lines``; return (table, local_start, local_end)
    where local_start/local_end are the indices of the first/last row line actually
    consumed by the returned (possibly row-capped) table."""
    row_indices: List[int] = []
    for i, line in enumerate(lines):
        if line.lstrip().startswith("|"):
            row_indices.append(i)
        elif row_indices:
            break
    if len(row_indices) < 3:
        return None
    raw_rows = [lines[i].strip() for i in row_indices]
    header = _split_table_row(raw_rows[0])
    body_start = 1
    if _TABLE_SEP_RE.match(raw_rows[1]):
        body_start = 2
    body = [_split_table_row(r) for r in raw_rows[body_start:] if not _TABLE_SEP_RE.match(r)]
    body = [r for r in body if any(r)]
    if len(body) < 2:
        return None
    width = len(header)
    body = [(r + [""] * width)[:width] for r in body]
    kept_body_count = min(5, len(body))
    consumed = min(body_start + kept_body_count, len(row_indices))
    local_start, local_end = row_indices[0], row_indices[consumed - 1]
    return [header] + body[:5], local_start, local_end


def _extract_before_after(lines: List[str], *, base: int) -> Tuple[List[Tuple[str, str]], List[Tuple[int, int]]]:
    pairs: List[Tuple[str, str]] = []
    ranges: List[Tuple[int, int]] = []
    i, n = 0, len(lines)
    while i < n:
        m_bad = _BAD_EXAMPLE_RE.match(lines[i].strip())
        if m_bad:
            for j in range(i + 1, min(i + 3, n)):
                m_good = _GOOD_EXAMPLE_RE.match(lines[j].strip())
                if m_good:
                    bad_text = _clean_inline(re.sub(r'^["“]|["”]$', "", m_bad.group(1).strip()))
                    good_text = _clean_inline(re.sub(r'^["“]|["”]$', "", m_good.group(1).strip()))
                    pairs.append((bad_text, good_text))
                    ranges.append((base + i, base + j))
                    i = j
                    break
        i += 1
    return pairs[:3], ranges


def _extract_qa(lines: List[str], *, base: int) -> Tuple[List[Tuple[str, str]], List[Tuple[int, int]]]:
    """Explicit "Q: .../A: ..." (or 질문:/답변:) pairs -- see _QA_QUESTION_RE's
    docstring note for why this doesn't try to guess from rhetorical
    questions in ordinary prose."""
    pairs: List[Tuple[str, str]] = []
    ranges: List[Tuple[int, int]] = []
    i, n = 0, len(lines)
    while i < n:
        m_q = _QA_QUESTION_RE.match(lines[i].strip())
        if m_q:
            for j in range(i + 1, min(i + 3, n)):
                m_a = _QA_ANSWER_RE.match(lines[j].strip())
                if m_a:
                    q_text = _clean_inline(m_q.group(1).strip())
                    a_text = _clean_inline(m_a.group(1).strip())
                    pairs.append((q_text, a_text))
                    ranges.append((base + i, base + j))
                    i = j
                    break
        i += 1
    return pairs[:2], ranges


def extract_all_qa_pairs(markdown: str) -> List[Tuple[str, str]]:
    """All 질문:/답변: (or Q:/A:) pairs in the whole document, uncapped.

    Unlike ``_extract_qa()`` (which hard-caps at 2 pairs for the infographic
    card and is scoped to one section's lines), this scans the entire
    document for FAQPage JSON-LD, which should reflect every FAQ pair the
    writer produced, not just the first 2 rendered as a card image."""
    lines = (markdown or "").split("\n")
    pairs: List[Tuple[str, str]] = []
    i, n = 0, len(lines)
    while i < n:
        m_q = _QA_QUESTION_RE.match(lines[i].strip())
        if m_q:
            for j in range(i + 1, min(i + 3, n)):
                m_a = _QA_ANSWER_RE.match(lines[j].strip())
                if m_a:
                    pairs.append((_clean_inline(m_q.group(1).strip()), _clean_inline(m_a.group(1).strip())))
                    i = j
                    break
        i += 1
    return pairs


def _extract_ox_quiz(lines: List[str], *, base: int) -> Tuple[Optional[Tuple[str, str]], List[Tuple[int, int]]]:
    """Explicit "통념: .../사실: ..." pair -- see _OX_MYTH_RE's docstring
    note for why this doesn't try to guess from "가 아니라" contrast
    clauses in ordinary prose."""
    for i, line in enumerate(lines):
        m_myth = _OX_MYTH_RE.match(line.strip())
        if not m_myth:
            continue
        for j in range(i + 1, min(i + 3, len(lines))):
            m_fact = _OX_FACT_RE.match(lines[j].strip())
            if m_fact:
                myth = _clean_inline(re.sub(r'^["“]|["”]$', "", m_myth.group(1).strip()))
                fact = _clean_inline(re.sub(r'^["“]|["”]$', "", m_fact.group(1).strip()))
                return (myth, fact), [(base + i, base + j)]
    return None, []


def _extract_risk_tiers(lines: List[str], *, base: int) -> Tuple[List[Tuple[str, str, str]], List[Tuple[int, int]]]:
    """Explicit 3-tier grade markers (안전/중간/위험 등) -- all three groups
    (safe/mid/risk) must be present in the same section, one line each.
    Returns [(group, original_label, description)] in safe/mid/risk order.
    """
    found: Dict[str, Tuple[str, str, int]] = {}
    for i, line in enumerate(lines):
        m = _RISK_TIER_RE.match(line.strip())
        if not m:
            continue
        label, desc = m.group(1), m.group(2)
        group = _RISK_TIER_GROUP[label]
        if group not in found:
            found[group] = (label, _clean_inline(desc.strip()), i)
    if not all(g in found for g in ("safe", "mid", "risk")):
        return [], []
    ranges = [(base + found[g][2], base + found[g][2]) for g in ("safe", "mid", "risk")]
    tiers = [(g, found[g][0], found[g][1]) for g in ("safe", "mid", "risk")]
    return tiers, ranges


def _extract_steps(lines: List[str], *, base: int) -> Tuple[List[str], List[Tuple[int, int]]]:
    items: List[str] = []
    ranges: List[Tuple[int, int]] = []
    for i, line in enumerate(lines):
        m = _STEP_RE.match(line)
        if m:
            items.append(_clean_inline(re.sub(r"^\d+단계\s*[:：]?\s*", "", m.group(1))))
            ranges.append((base + i, base + i))
    return items, ranges


def _extract_numbered(lines: List[str], *, base: int) -> Tuple[List[str], List[Tuple[int, int]]]:
    items: List[str] = []
    ranges: List[Tuple[int, int]] = []
    i, n = 0, len(lines)
    while i < n:
        m = _NUMBERED_ITEM_RE.match(lines[i].strip())
        if m:
            end_i = i
            if i + 1 < n and _NUMBERED_DESC_RE.match(lines[i + 1].strip()):
                end_i = i + 1
            items.append(_clean_inline(m.group(2)))
            ranges.append((base + i, base + end_i))
            i = end_i
        i += 1
    return items, ranges


def _extract_bullets(lines: List[str], *, base: int) -> Tuple[List[str], List[Tuple[int, int]]]:
    items: List[str] = []
    ranges: List[Tuple[int, int]] = []
    for i, line in enumerate(lines):
        m = _BULLET_RE.match(line)
        if m:
            items.append(_clean_inline(m.group(1)))
            ranges.append((base + i, base + i))
    return items, ranges


def _find_gauge_stat(items: List[str]) -> Optional[Tuple[str, str, str, List[str]]]:
    """A bullet list is gauge-eligible only when one item names a concrete
    stat (number + unit) — never fabricated, only ever a real match."""
    for idx, item in enumerate(items):
        m = _GAUGE_STAT_RE.search(item)
        if m:
            sub_items = [it for j, it in enumerate(items) if j != idx][:3]
            return m.group(1), m.group(2), item, sub_items
    return None


def _extract_quote(lines: List[str]) -> str:
    cleaned_lines = [re.sub(r"^[-*]\s+", "", l.strip()) for l in lines if l.strip() and not l.strip().startswith("#")]
    text = " ".join(cleaned_lines)
    if not text:
        return ""
    bold_match = re.search(r"\*\*(.{8,80}?)\*\*", text)
    if bold_match:
        return _clean_inline(bold_match.group(1))
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(_clean_inline(text)) if len(s.strip()) >= 12]
    return sentences[0][:80] if sentences else ""


def _build_spec_for_section(heading: str, section_lines: List[str], *, base: int) -> Optional[InfographicSpec]:
    display_title = _display_title(heading)

    table_info = _extract_table_with_range(section_lines)
    if table_info is not None:
        table, local_start, local_end = table_info
        return InfographicSpec(
            heading=heading, display_title=display_title, shape="grid",
            eligible_styles=("grid",), table=table,
            strip_ranges=[(base + local_start, base + local_end)],
        )

    qa_pairs, qa_ranges = _extract_qa(section_lines, base=base)
    if qa_pairs:
        return InfographicSpec(
            heading=heading, display_title=display_title, shape="qa",
            eligible_styles=("qa",), qa_pairs=qa_pairs, strip_ranges=qa_ranges,
        )

    ox_pair, ox_ranges = _extract_ox_quiz(section_lines, base=base)
    if ox_pair is not None:
        return InfographicSpec(
            heading=heading, display_title=display_title, shape="ox_quiz",
            eligible_styles=("ox_quiz",), ox_pair=ox_pair, strip_ranges=ox_ranges,
        )

    risk_tiers, risk_ranges = _extract_risk_tiers(section_lines, base=base)
    if risk_tiers:
        return InfographicSpec(
            heading=heading, display_title=display_title, shape="risk_tier",
            eligible_styles=("risk_tier",), risk_tiers=risk_tiers, strip_ranges=risk_ranges,
        )

    pairs, ba_ranges = _extract_before_after(section_lines, base=base)
    if pairs:
        return InfographicSpec(
            heading=heading, display_title=display_title, shape="before_after",
            eligible_styles=("before_after",), before_pairs=pairs, strip_ranges=ba_ranges,
        )

    step_items, step_ranges = _extract_steps(section_lines, base=base)
    if len(step_items) >= 3:
        return InfographicSpec(
            heading=heading, display_title=display_title, shape="ordered",
            eligible_styles=("timeline", "checklist"),
            items=step_items[:5], strip_ranges=step_ranges[:5],
        )

    numbered_items, num_ranges = _extract_numbered(section_lines, base=base)
    if len(numbered_items) >= 3:
        return InfographicSpec(
            heading=heading, display_title=display_title, shape="ordered",
            eligible_styles=("timeline", "checklist"),
            items=numbered_items[:5], strip_ranges=num_ranges[:5],
        )

    bullet_items, bullet_ranges = _extract_bullets(section_lines, base=base)
    if len(bullet_items) >= 3:
        checklist_items, checklist_ranges = bullet_items[:5], bullet_ranges[:5]
        gauge = _find_gauge_stat(bullet_items)
        if gauge is not None:
            number, unit, label, sub_items = gauge
            gauge_ranges = [bullet_ranges[bullet_items.index(label)]] + [
                bullet_ranges[bullet_items.index(s)] for s in sub_items
            ]
            # Gauge is the preferred rendering when a concrete stat exists,
            # but a plain checklist of the same bullets is equally honest —
            # keeping both eligible gives the doc-level allocator (see
            # _allocate_styles) a real second option instead of forcing a
            # repeat when "gauge" is already used elsewhere in the post.
            return InfographicSpec(
                heading=heading, display_title=display_title, shape="bullets",
                eligible_styles=("gauge", "checklist"),
                items=sub_items, gauge_stat=(number, unit), gauge_label=label,
                strip_ranges=gauge_ranges,
                _checklist_items=checklist_items, _checklist_ranges=checklist_ranges,
            )
        return InfographicSpec(
            heading=heading, display_title=display_title, shape="bullets",
            eligible_styles=("checklist",),
            items=checklist_items, strip_ranges=checklist_ranges,
        )

    quote_text = _extract_quote(section_lines)
    if quote_text:
        # Pull-quote convention: intentionally not stripped from the body.
        # Two eligible styles ("quote" pull-quote vs. "quote_keyword"
        # keyword-highlight, same underlying quote_text) so _allocate_styles'
        # existing repeat-avoidance makes multiple quote-shaped sections in
        # one document alternate layouts instead of all rendering identically.
        return InfographicSpec(
            heading=heading, display_title=display_title, shape="quote",
            eligible_styles=("quote", "quote_keyword"), quote_text=quote_text, strip_ranges=[],
        )
    return None


def _allocate_styles(specs: List[InfographicSpec], *, style_seed: str) -> None:
    """Assign each spec's final concrete ``style``, avoiding a repeat within
    the same document until every honest option for that specific section is
    already used elsewhere in it.

    Shape-locked sections (a table can only honestly be "grid", a 나쁜예/고친예
    pair only "before_after", a plain paragraph only "quote") have exactly one
    eligible style — if that style is already used, the repeat is
    unavoidable without misrepresenting the section's actual data, so it's
    allowed rather than forced into a fabricated shape. Sections with real
    alternatives ("ordered" -> timeline/checklist, "bullets" with a stat ->
    gauge/checklist) get to dodge a collision by picking their still-unused
    option first.
    """
    used: set = set()
    for spec in specs:
        eligible = spec.eligible_styles or ("quote",)
        candidates = [s for s in eligible if s not in used]
        if not candidates:
            candidates = list(eligible)  # every honest option already used — unavoidable repeat
        chosen = candidates[0] if len(candidates) == 1 else choose_style(
            pool=candidates, seed_parts=(style_seed, spec.heading, "alloc"),
        )
        spec.style = chosen
        used.add(chosen)
        # Only swap in the alternate checklist rendering for "bullets" specs
        # that actually came from the dual-option (gauge-eligible) branch —
        # ``_checklist_items`` is empty for the plain checklist-only branch,
        # whose ``items``/``strip_ranges`` are already correct from
        # construction. Unconditionally overwriting here previously wiped a
        # checklist-only card down to just its label strip (caught by
        # rendering against a real draft: infographic_05.png came out
        # blank).
        if spec.shape == "bullets" and chosen == "checklist" and spec._checklist_items:
            spec.items = spec._checklist_items
            spec.strip_ranges = spec._checklist_ranges


def _style_has_renderer(style: str, skin: Optional[str]) -> bool:
    """True if ``style`` can actually be drawn under the document's resolved
    ``skin`` -- either it has a plain/default renderer (_RENDERERS, always
    usable regardless of skin) or a skin-specific one registered for this
    exact ``skin``. Shapes with no default (e.g. "qa") are only renderable
    when the resolved skin happens to support them.
    """
    return style in _RENDERERS or skin in _SKIN_RENDERERS.get(style, {})


def extract_infographic_specs(
    markdown: str, *, max_count: int = 5, style_seed: str = "", skin: Optional[str] = None,
    quote_cap: int = 2,
) -> List[InfographicSpec]:
    """Pick H2 sections that tell a visual story, preferring variety.

    ``style_seed`` should be stable per-document (e.g. the topic title) so
    re-running the same draft reproduces the same style choices, while
    different posts land on different rotations. ``skin`` is the
    document-level skin already resolved by the caller (see
    build_section_infographics) -- sections whose every eligible style is
    unrenderable under this skin (e.g. a Q&A-shaped section when the
    document's skin doesn't have a "qa" renderer) are dropped here rather
    than risking a broken/empty card later.

    ``quote_cap`` bounds how many of the final picks can be the generic
    "quote" fallback (a plain paragraph with no structured data) -- if a
    post has few/no structured sections, remaining slots up to
    ``max_count`` are simply left UNFILLED (fewer total in-body images)
    rather than padded with more repetitive quote cards.
    """
    lines_all = (markdown or "").split("\n")
    sections: List[Tuple[str, int, int]] = []
    current_heading = ""
    current_start: Optional[int] = None
    for i, line in enumerate(lines_all):
        match = re.match(r"^##\s+(.+)$", line)
        if match:
            if current_heading and current_start is not None:
                sections.append((current_heading, current_start, i))
            current_heading = match.group(1).strip()
            current_start = i
    if current_heading and current_start is not None:
        sections.append((current_heading, current_start, len(lines_all)))

    specs: List[InfographicSpec] = []
    for heading, start, end in sections:
        if _SKIP_HEADING_RE.search(heading):
            continue
        spec = _build_spec_for_section(heading, lines_all[start:end], base=start)
        if spec is None:
            continue
        renderable = tuple(s for s in (spec.eligible_styles or ("quote",)) if _style_has_renderer(s, skin))
        if not renderable:
            continue
        spec.eligible_styles = renderable
        specs.append(spec)

    if not specs:
        return []

    # Prefer variety: one of each *structured* shape first (grid/before_after/
    # ordered/bullets — never quote here), then fill remaining slots from any
    # leftover structured sections in document order. "quote" is deliberately
    # excluded from both passes and only used to fill what's still missing
    # afterward — it's a last-resort floor-filler, and must never crowd out a
    # genuine second table/checklist/steps section just because it happened
    # to be scheduled in the same pass (a real ordering bug caught by testing
    # against a live draft: a numbered-list section was starved out because
    # the weaker "quote" fallback filled the last slot before leftover
    # structured sections got a chance). Selection is keyed on ``shape``, not
    # the final ``style`` — style isn't assigned until _allocate_styles runs,
    # below, on whichever sections actually get picked.
    structured_order = ("grid", "before_after", "ordered", "bullets")
    picked: List[InfographicSpec] = []
    used_ids: set = set()
    for shape in structured_order:
        for spec in specs:
            if spec.shape == shape and id(spec) not in used_ids:
                picked.append(spec)
                used_ids.add(id(spec))
                break
        if len(picked) >= max_count:
            break
    if len(picked) < max_count:
        leftovers = [s for s in specs if id(s) not in used_ids and s.shape != "quote"]
        while len(picked) < max_count and leftovers:
            picked.append(leftovers.pop(0))
            used_ids.add(id(picked[-1]))
    if len(picked) < max_count:
        quote_leftovers = [s for s in specs if id(s) not in used_ids and s.shape == "quote"]
        quote_budget = quote_cap
        while len(picked) < max_count and quote_leftovers and quote_budget > 0:
            picked.append(quote_leftovers.pop(0))
            quote_budget -= 1
    picked = picked[:max_count]

    # Allocate concrete styles last, in original document order (not the
    # shape-grouped order above) — reading order is the most intuitive basis
    # for "who gets first claim on a style" when two sections could collide.
    doc_index = {id(s): i for i, s in enumerate(specs)}
    picked.sort(key=lambda s: doc_index[id(s)])
    _allocate_styles(picked, style_seed=style_seed)
    return picked


def _rgb(hex_color: str) -> Tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _mix(color_a, color_b, ratio: float):
    return tuple(round(a + (b - a) * ratio) for a, b in zip(color_a, color_b))


def _draw_label_strip(draw, *, category_id: str, style: str) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    """Small style label + marker glyph — replaces the old full title bar so
    the card never repeats the H2 heading text verbatim."""
    accent, deep, _tint = _PALETTES.get(category_id, _DEFAULT_PALETTE)
    accent_rgb, deep_rgb = _rgb(accent), _rgb(deep)
    label = _STYLE_LABELS.get(style, "핵심")
    font = _korean_font(26)
    pad_x, pad_y = 20, 11
    tw = draw.textlength(label, font=font)
    box_h = 26 + pad_y * 2
    box_w = tw + pad_x * 2 + 30
    draw.rounded_rectangle((48, 34, 48 + box_w, 34 + box_h), radius=box_h / 2, fill=accent_rgb)
    mx, my = 48 + pad_x + 6, 34 + box_h / 2
    draw.polygon([(mx, my - 7), (mx, my + 7), (mx + 11, my)], fill="white")
    draw.text((mx + 22, 34 + pad_y), label, font=font, fill="white")
    return accent_rgb, deep_rgb


_HEADER_H = 100  # top margin reserved for the label strip (was 148 for the old title bar)


def _render_checklist_card(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    from PIL import Image, ImageDraw

    items = spec.items[:5]
    width = 1200
    row_h = 96
    height = _HEADER_H + 20 + len(items) * row_h + 40

    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img, "RGBA")
    accent_rgb, deep_rgb = _draw_label_strip(draw, category_id=category_id, style="checklist")
    pastel_line = _mix(accent_rgb, (255, 255, 255), 0.62)
    badge_fill = _mix(accent_rgb, (255, 255, 255), 0.70)

    item_font = _korean_font(30)
    y = _HEADER_H + 20
    for item in items:
        draw.rounded_rectangle((48, y, width - 48, y + row_h - 18), radius=20, fill="#ffffff", outline=pastel_line, width=3)
        cx, cy = 106, y + (row_h - 18) / 2
        draw.ellipse((cx - 25, cy - 25, cx + 25, cy + 25), fill=badge_fill)
        draw.line([(cx - 11, cy + 1), (cx - 3, cy + 10), (cx + 12, cy - 9)], fill=deep_rgb, width=5, joint="curve")
        text_lines = _wrap(draw, item, item_font, width - 280, 2)
        if len(text_lines) == 1:
            draw.text((162, cy - 21), text_lines[0], font=item_font, fill="#37474f")
        else:
            small_font = _korean_font(24)
            small_lines = _wrap(draw, item, small_font, width - 280, 2)
            for i, line in enumerate(small_lines):
                draw.text((162, y + 12 + i * 32), line, font=small_font, fill="#37474f")
        y += row_h

    return _save_png(img, output_path)


def _render_timeline_card(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    from PIL import Image, ImageDraw

    items = spec.items[:5]
    width = 1200
    height = 460

    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img, "RGBA")
    accent_rgb, deep_rgb = _draw_label_strip(draw, category_id=category_id, style="timeline")
    track_bg = _mix(accent_rgb, (255, 255, 255), 0.82)

    n = len(items)
    y = height // 2 + 30
    margin = 130
    span = width - margin * 2
    if n > 1:
        draw.line((margin, y, width - margin, y), fill=track_bg, width=10)
    label_font = _korean_font(24)
    num_font = _korean_font(34)
    for i, item in enumerate(items):
        x = margin + (span * i / (n - 1) if n > 1 else span / 2)
        r = 40
        fill = accent_rgb if i % 2 == 0 else deep_rgb
        draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)
        num = str(i + 1)
        box = draw.textbbox((0, 0), num, font=num_font)
        draw.text((x - (box[2] - box[0]) / 2 - box[0], y - (box[3] - box[1]) / 2 - box[1]), num, font=num_font, fill="white")
        lines = _wrap(draw, item, label_font, 210, 2)
        ty = y + r + 24
        for line in lines:
            tw = draw.textlength(line, font=label_font)
            draw.text((x - tw / 2, ty), line, font=label_font, fill="#37474f")
            ty += 32

    return _save_png(img, output_path)


def _render_gauge_card(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    from PIL import Image, ImageDraw

    width, height = 1200, 460
    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img, "RGBA")
    accent_rgb, deep_rgb = _draw_label_strip(draw, category_id=category_id, style="gauge")
    track_bg = _mix(accent_rgb, (255, 255, 255), 0.82)

    number, unit = spec.gauge_stat or ("", "")
    cx, cy, r = 300, height // 2 + 30, 140
    draw.arc((cx - r, cy - r, cx + r, cy + r), start=140, end=400, fill=track_bg, width=28)
    draw.arc((cx - r, cy - r, cx + r, cy + r), start=140, end=140 + 260 * 0.62, fill=accent_rgb, width=28)
    num_font = _korean_font(76)
    unit_font = _korean_font(30)
    draw.text((cx, cy - 22), number, font=num_font, fill=deep_rgb, anchor="mm")
    draw.text((cx, cy + 44), unit, font=unit_font, fill=accent_rgb, anchor="mm")

    label_font = _korean_font(28)
    label_lines = _wrap(draw, spec.gauge_label, label_font, 560, 2)
    ty = 150
    for line in label_lines:
        draw.text((600, ty), line, font=label_font, fill="#37474f")
        ty += 38

    item_font = _korean_font(26)
    ty += 20
    for item in spec.items[:3]:
        draw.ellipse((600, ty + 4, 620, ty + 24), fill=accent_rgb)
        lines = _wrap(draw, item, item_font, 540, 1)
        draw.text((634, ty), lines[0] if lines else "", font=item_font, fill="#37474f")
        ty += 44

    return _save_png(img, output_path)


def _split_keyword_phrase(text: str, *, max_phrase_chars: int = 16) -> Tuple[str, str]:
    """Split ``text`` into a short word-boundary-safe leading phrase (for a
    big headline callout) plus the remaining text (shown smaller as
    context). Used by the "quote_keyword" layout so it visually differs
    from the plain quote card's full-sentence-in-quote-marks treatment
    while reusing the exact same ``quote_text`` data."""
    text = (text or "").strip()
    if not text:
        return "", ""
    if len(text) <= max_phrase_chars:
        return text, ""
    words = text.split()
    phrase_words: List[str] = []
    running = ""
    for w in words:
        candidate = f"{running} {w}".strip()
        if len(candidate) > max_phrase_chars and phrase_words:
            break
        phrase_words.append(w)
        running = candidate
    phrase = " ".join(phrase_words) or words[0]
    rest = text[len(phrase):].strip()
    return phrase, rest


def _render_quote_card(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    from PIL import Image, ImageDraw

    accent, deep, _tint = _PALETTES.get(category_id, _DEFAULT_PALETTE)
    accent_rgb, deep_rgb = _rgb(accent), _rgb(deep)
    width, height = 1200, 420

    img = Image.new("RGB", (width, height), deep_rgb)
    draw = ImageDraw.Draw(img, "RGBA")
    label = _STYLE_LABELS.get("quote", "핵심")
    label_font = _korean_font(24)
    draw.rounded_rectangle((48, 34, 48 + draw.textlength(label, font=label_font) + 56, 76), radius=21, fill=accent_rgb)
    draw.text((70, 44), label, font=label_font, fill="white")

    draw.text((70, 60), "“", font=_korean_font(150), fill=accent_rgb)
    quote_font = _korean_font(46)
    lines = _wrap(draw, spec.quote_text, quote_font, width - 200, 4)
    total_h = len(lines) * 62
    y = (height - total_h) // 2 + 30
    for line in lines:
        draw.text((100, y), line, font=quote_font, fill="white")
        y += 62

    return _save_png(img, output_path)


def _render_quote_keyword_card(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    """"핵심 단어/구 하이라이트형" -- the second quote-shape layout. Same
    quote_text data as _render_quote_card, but shown as a big highlighted
    leading phrase (light tint background block) with the rest of the
    sentence as smaller supporting context below, instead of a full
    sentence in pull-quote styling. Registered as an alternate eligible
    style so _allocate_styles picks this instead of _render_quote_card when
    a document has more than one quote-shaped section (see
    _build_spec_for_section's quote branch)."""
    from PIL import Image, ImageDraw

    accent, deep, tint = _PALETTES.get(category_id, _DEFAULT_PALETTE)
    accent_rgb, deep_rgb, tint_rgb = _rgb(accent), _rgb(deep), _rgb(tint)
    width = 1200

    keyword_phrase, rest = _split_keyword_phrase(spec.quote_text)
    measure_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    keyword_font = _korean_font(52)
    kw_lines = _wrap(measure_draw, keyword_phrase, keyword_font, width - 160, 2)
    rest_font = _korean_font(27)
    rest_lines = _wrap(measure_draw, rest, rest_font, width - 160, 3) if rest else []

    height = 140 + len(kw_lines) * 70 + (24 + len(rest_lines) * 38 if rest_lines else 0) + 40

    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img)

    label = _STYLE_LABELS.get("quote_keyword", "포인트")
    label_font = _korean_font(22)
    label_w = draw.textlength(label, font=label_font)
    draw.rounded_rectangle((48, 34, 48 + label_w + 52, 74), radius=20, outline=accent_rgb, width=2)
    draw.text((74, 44), label, font=label_font, fill=deep_rgb)

    y = 108
    for line in kw_lines:
        line_w = draw.textlength(line, font=keyword_font)
        draw.rounded_rectangle((72, y + 6, 88 + line_w, y + 62), radius=10, fill=tint_rgb)
        draw.text((80, y), line, font=keyword_font, fill=deep_rgb)
        y += 70

    if rest_lines:
        y += 16
        draw.line((80, y, width - 80, y), fill="#e2e5e9", width=2)
        y += 24
        for line in rest_lines:
            draw.text((80, y), line, font=rest_font, fill="#555b63")
            y += 38

    return _save_png(img, output_path)


def _render_quote_card_skin_c(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    """"섹션 타이틀 강조형" -- skin C's title-emphasis card. Reuses the same
    quote_text extraction as the plain quote card (a representative sentence,
    never the H2 heading repeated verbatim); only the decoration differs.
    """
    from PIL import Image, ImageDraw

    accent, deep, _tint = _PALETTES.get(category_id, _DEFAULT_PALETTE)
    accent_rgb, deep_rgb = _rgb(accent), _rgb(deep)
    width = 1200

    title_font = _korean_font(38)
    measure_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines = _wrap(measure_draw, spec.quote_text, title_font, width - 160, 3)
    height = 170 + len(lines) * 50 + 90

    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img, "RGBA")
    _draw_skin_c_gradient_bg(img, draw, category_id=category_id)

    pill_label = "중요"
    pill_font = _korean_font(20)
    pw = draw.textlength(pill_label, font=pill_font) + 32
    draw.rounded_rectangle((80, 50, 80 + pw, 50 + 34), radius=17, fill=accent_rgb)
    draw.text((80 + 16, 50 + 7), pill_label, font=pill_font, fill="white")

    ty = 110
    for line in lines:
        draw.text((80, ty), line, font=title_font, fill=deep_rgb)
        ty += 50

    icon_ids = [pick_icon_for_text(spec.quote_text)]
    heading_icon = pick_icon_for_text(spec.heading)
    if heading_icon not in icon_ids:
        icon_ids.append(heading_icon)
    icon_x = 80
    for icon_id in icon_ids[:3]:
        draw_icon(draw, icon_id, (icon_x, ty + 10, icon_x + 34, ty + 44), color=accent_rgb, width=3)
        icon_x += 50

    return _save_png(img, output_path)


def _render_quote_keyword_card_skin_c(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    """Skin C's variant of the "핵심 단어/구 하이라이트형" layout.

    Must stay visually distinct from _render_quote_card_skin_c EVEN when
    quote_text is already short (common -- _extract_quote() often returns a
    short bold span or short sentence, leaving _split_keyword_phrase's
    ``rest`` empty). Relying on the phrase/rest split alone collapsed both
    cards to "gradient bg + pill + one bold line + icon" in that case (real
    published-post bug: the two looked near-identical apart from the pill
    label). Fixed by always giving the phrase its own solid highlight-block
    background and an outlined (not filled) pill, so the structural
    difference from the plain-text quote card holds regardless of length.
    """
    from PIL import Image, ImageDraw

    accent, deep, _tint = _PALETTES.get(category_id, _DEFAULT_PALETTE)
    accent_rgb, deep_rgb = _rgb(accent), _rgb(deep)
    width = 1200

    keyword_phrase, rest = _split_keyword_phrase(spec.quote_text)
    measure_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    keyword_font = _korean_font(48)
    kw_lines = _wrap(measure_draw, keyword_phrase, keyword_font, width - 160, 2)
    rest_font = _korean_font(26)
    rest_lines = _wrap(measure_draw, rest, rest_font, width - 160, 2) if rest else []
    height = 150 + len(kw_lines) * 70 + (20 + len(rest_lines) * 36 if rest_lines else 0) + 90

    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img, "RGBA")
    _draw_skin_c_gradient_bg(img, draw, category_id=category_id)

    pill_label = "포인트"
    pill_font = _korean_font(20)
    pw = draw.textlength(pill_label, font=pill_font) + 32
    draw.rounded_rectangle((80, 50, 80 + pw, 50 + 34), radius=17, outline=accent_rgb, width=2)
    draw.text((80 + 16, 50 + 7), pill_label, font=pill_font, fill=deep_rgb)

    ty = 110
    for line in kw_lines:
        line_w = draw.textlength(line, font=keyword_font)
        draw.rounded_rectangle((72, ty + 6, 92 + line_w, ty + 62), radius=10, fill=(255, 255, 255, 190))
        draw.text((80, ty), line, font=keyword_font, fill=deep_rgb)
        ty += 70

    if rest_lines:
        ty += 12
        for line in rest_lines:
            draw.text((80, ty), line, font=rest_font, fill=deep_rgb)
            ty += 36

    icon_ids = [pick_icon_for_text(spec.quote_text)]
    heading_icon = pick_icon_for_text(spec.heading)
    if heading_icon not in icon_ids:
        icon_ids.append(heading_icon)
    icon_x = 80
    ty += 14
    for icon_id in icon_ids[:3]:
        draw_icon(draw, icon_id, (icon_x, ty + 10, icon_x + 34, ty + 44), color=accent_rgb, width=3)
        icon_x += 50

    return _save_png(img, output_path)


def _render_tipbox(draw, x0: int, y0: int, width: int, text: str, *, font) -> int:
    """Skin C's common bottom tip box (bulb icon + one line). Returns the
    box's total height so callers can size their canvas around it."""
    lines_h = 34
    box_h = lines_h + 24
    draw.rounded_rectangle((x0, y0, x0 + width, y0 + box_h), radius=14, fill=(255, 255, 255, 170))
    draw_icon(draw, "bulb", (x0 + 14, y0 + 10, x0 + 40, y0 + 36), color=(120, 110, 40), width=3)
    draw.text((x0 + 52, y0 + box_h / 2), text, font=font, fill=(60, 55, 30), anchor="lm")
    return box_h


def _render_ox_quiz_card_skin_c(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    from PIL import Image, ImageDraw

    myth, fact = spec.ox_pair or ("", "")
    width = 1200
    headline_font = _korean_font(32)
    body_font = _korean_font(22)
    tip_font = _korean_font(20)

    measure_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    headline_lines = _wrap(measure_draw, f"“{myth}”", headline_font, width - 200, 2)
    # Two fixed-position columns with a guaranteed gap between their boxes
    # (a previous centered-with-badge-offset formula let the two answer
    # cards' boxes overlap -- caught by rendering, not by inspection).
    margin = 80
    col_w = 440
    inner_pad = 40
    x_cx = margin + col_w / 2
    o_cx = width - margin - col_w / 2
    x_lines = _wrap(measure_draw, myth, body_font, col_w - inner_pad, 4)
    o_lines = _wrap(measure_draw, fact, body_font, col_w - inner_pad, 4)
    answer_h = max(len(x_lines), len(o_lines)) * 28 + 40

    height = 150 + len(headline_lines) * 42 + 30 + 90 + answer_h + 30 + 60
    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img, "RGBA")
    _draw_skin_c_gradient_bg(img, draw, category_id=category_id)

    accent, deep, _tint = _PALETTES.get(category_id, _DEFAULT_PALETTE)
    ty = 60
    for line in headline_lines:
        draw.text((width / 2, ty), line, font=headline_font, fill=_rgb(deep), anchor="ma")
        ty += 42

    badge_y = ty + 40
    badge_r = 34
    draw.ellipse((x_cx - badge_r, badge_y - badge_r, x_cx + badge_r, badge_y + badge_r), fill=_SKIN_C_RISK)
    draw.text((x_cx, badge_y), "X", font=_korean_font(30), fill="white", anchor="mm")
    draw.ellipse((o_cx - badge_r, badge_y - badge_r, o_cx + badge_r, badge_y + badge_r), fill=_SKIN_C_SAFE)
    draw.text((o_cx, badge_y), "O", font=_korean_font(30), fill="white", anchor="mm")

    card_top = badge_y + badge_r + 16
    draw.rounded_rectangle((x_cx - col_w / 2, card_top, x_cx + col_w / 2, card_top + answer_h), radius=14, fill=(255, 255, 255, 140))
    draw.rounded_rectangle((o_cx - col_w / 2, card_top, o_cx + col_w / 2, card_top + answer_h), radius=14, fill=(255, 255, 255, 140))
    ly = card_top + 16
    for line in x_lines:
        draw.text((x_cx, ly), line, font=body_font, fill=(60, 40, 40), anchor="ma")
        ly += 28
    ly = card_top + 16
    for line in o_lines:
        draw.text((o_cx, ly), line, font=body_font, fill=(30, 55, 40), anchor="ma")
        ly += 28

    tip_y = card_top + answer_h + 24
    # Generic, category-neutral wording -- there's no extracted per-content
    # tip data to draw from, and a fixed health/exercise-specific phrase
    # ("몸 반응(각성감·통증)") was wrong for other categories (caught
    # rendering a real finance/ETF draft).
    _render_tipbox(draw, 80, tip_y, width - 160, "정답은 상황에 따라 다를 수 있으니, 본문 설명을 함께 확인하세요.", font=tip_font)

    return _save_png(img, output_path)


def _render_risk_tier_card_skin_c(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    from PIL import Image, ImageDraw

    tiers = spec.risk_tiers
    width = 1200
    pill_font = _korean_font(20)
    desc_font = _korean_font(24)
    tip_font = _korean_font(20)

    measure_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    row_wrapped = [_wrap(measure_draw, desc, desc_font, width - 340, 2) for _g, _label, desc in tiers]
    row_heights = [max(64, len(lines) * 30 + 26) for lines in row_wrapped]
    height = 130 + sum(row_heights) + (len(tiers) - 1) * 14 + 30 + 60

    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img, "RGBA")
    _draw_skin_c_gradient_bg(img, draw, category_id=category_id)

    label_font = _korean_font(24)
    draw.text((80, 50), spec.display_title, font=label_font, fill=(40, 40, 40))

    pill_color = {"safe": _SKIN_C_SAFE, "mid": _SKIN_C_MID, "risk": _SKIN_C_RISK}
    y = 110
    for (group, label, _desc), lines, row_h in zip(tiers, row_wrapped, row_heights):
        draw.rounded_rectangle((80, y, width - 80, y + row_h), radius=14, fill=(255, 255, 255, 150))
        pill_w = max(70, pill_font.getlength(label) + 32)
        draw.rounded_rectangle((104, y + (row_h - 34) / 2, 104 + pill_w, y + (row_h - 34) / 2 + 34), radius=17, fill=pill_color.get(group, (100, 100, 100)))
        draw.text((104 + pill_w / 2, y + row_h / 2), label, font=pill_font, fill="white", anchor="mm")
        icon_x = 104 + pill_w + 20
        draw_icon(draw, pick_icon_for_text(_desc), (icon_x, y + row_h / 2 - 16, icon_x + 32, y + row_h / 2 + 16), color=pill_color.get(group, (100, 100, 100)), width=3)
        ty = y + (row_h - len(lines) * 30) / 2
        for line in lines:
            draw.text((icon_x + 46, ty), line, font=desc_font, fill=(40, 40, 40))
            ty += 30
        y += row_h + 14

    # Generic, category-neutral wording -- see the same fix note in
    # _render_ox_quiz_card_skin_c above (a fixed health/exercise phrase was
    # wrong for other categories, caught rendering a real finance draft).
    _render_tipbox(draw, 80, y + 10, width - 160, "실제 적용은 개인 상황에 따라 달라질 수 있습니다.", font=tip_font)

    return _save_png(img, output_path)


def _render_before_after_card(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    from PIL import Image, ImageDraw

    pairs = spec.before_pairs[:3]
    width = 1200
    mid = width // 2
    top = _HEADER_H + 20
    col_width = mid - 104 - 24

    item_font = _korean_font(24)
    # Wrap every pair BEFORE sizing the canvas -- text measurement only
    # depends on font metrics, not canvas size, so a throwaway 1x1 image is
    # enough here. The card height must be derived from the actual wrapped
    # line counts (up to 2 lines/side via _wrap's own cap+ellipsis), not a
    # fixed per-pair budget -- a fixed budget silently undercounts height
    # for any pair that wraps to 2 lines, which is exactly what let long
    # "나쁜 예/고친 예" sentences draw past the box (and, with several long
    # pairs, past the PNG canvas itself).
    measure_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    wrapped_pairs = [
        (
            _wrap(measure_draw, bad, item_font, col_width, 2),
            _wrap(measure_draw, good, item_font, col_width, 2),
        )
        for bad, good in pairs
    ]

    ty = top + 76
    for bad_lines, good_lines in wrapped_pairs:
        ty += max(len(bad_lines), len(good_lines)) * 30 + 24
    bottom = ty + 6
    height = bottom + 30

    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img, "RGBA")
    accent_rgb, deep_rgb = _draw_label_strip(draw, category_id=category_id, style="before_after")

    draw.rounded_rectangle((80, top, mid - 24, bottom), radius=20, fill=(255, 236, 236))
    draw.rounded_rectangle((mid + 24, top, width - 80, bottom), radius=20, fill=_mix(accent_rgb, (255, 255, 255), 0.88))

    head_font = _korean_font(26)
    draw.text((80 + (mid - 104) / 2, top + 22), "나쁜 예", font=head_font, fill=(198, 40, 40), anchor="ma")
    draw.text((mid + 24 + (mid - 104) / 2, top + 22), "고친 예", font=head_font, fill=deep_rgb, anchor="ma")

    ty = top + 76
    for bad_lines, good_lines in wrapped_pairs:
        for i, line in enumerate(bad_lines):
            draw.text((104, ty + i * 30), line, font=item_font, fill=(120, 40, 40))
        for i, line in enumerate(good_lines):
            draw.text((mid + 48, ty + i * 30), line, font=item_font, fill="#1b3d2b")
        ty += max(len(bad_lines), len(good_lines)) * 30 + 24

    r = 34
    cy = (top + bottom) // 2
    draw.ellipse((mid - r, cy - r, mid + r, cy + r), fill=accent_rgb)
    draw.polygon([(mid - 8, cy - 12), (mid - 8, cy + 12), (mid + 14, cy)], fill="white")

    return _save_png(img, output_path)


def _render_before_after_card_skin_c(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    """"두 갈래 비교형" -- skin C's take on the same before_pairs data the
    plain before_after card uses (never a new extraction), one row per
    pair: a muted "무리한 방식" card beside an accent-tinted "현실적 방식" card.
    """
    from PIL import Image, ImageDraw

    accent, deep, _tint = _PALETTES.get(category_id, _DEFAULT_PALETTE)
    accent_rgb, deep_rgb = _rgb(accent), _rgb(deep)
    pairs = spec.before_pairs[:3]
    width = 1200
    margin = 80
    col_w = (width - margin * 2 - 20) // 2
    body_font = _korean_font(22)
    title_font = _korean_font(30)

    measure_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    title_lines = _wrap(measure_draw, spec.display_title, title_font, width - 200, 2)
    wrapped_pairs = [
        (_wrap(measure_draw, bad, body_font, col_w - 40, 3), _wrap(measure_draw, good, body_font, col_w - 40, 3))
        for bad, good in pairs
    ]
    row_heights = [max(70, max(len(b), len(g)) * 28 + 56) for b, g in wrapped_pairs]
    height = 70 + len(title_lines) * 40 + 30 + sum(row_heights) + (len(pairs) - 1) * 16 + 30 + 60

    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img, "RGBA")
    _draw_skin_c_gradient_bg(img, draw, category_id=category_id)

    ty = 50
    # Quote glyph: reuse the curly-quote character already confirmed to
    # render in this font (see _render_quote_card) -- "❝" (a rarer ornament
    # codepoint) isn't in NanumGothic's coverage and drew as a tofu box
    # (caught by rendering, not by inspection).
    draw.text((margin, ty), "“", font=_korean_font(52), fill=accent_rgb)
    for line in title_lines:
        draw.text((margin + 40, ty + 4), line, font=title_font, fill=deep_rgb)
        ty += 40
    ty += 20

    bad_bg = (120, 120, 130, 40)
    good_bg = (*accent_rgb, 40)
    label_font = _korean_font(20)
    for (bad_lines, good_lines), row_h in zip(wrapped_pairs, row_heights):
        bx0, gx0 = margin, margin + col_w + 20
        draw.rounded_rectangle((bx0, ty, bx0 + col_w, ty + row_h), radius=14, fill=bad_bg)
        draw.rounded_rectangle((gx0, ty, gx0 + col_w, ty + row_h), radius=14, fill=good_bg)
        # Same reason as the quote glyph above: ⚠/✓ as raw text aren't in
        # this font either -- use the hand-drawn vector icons instead.
        draw_icon(draw, "shield", (bx0 + 16, ty + 12, bx0 + 38, ty + 34), color=(120, 80, 40), width=3)
        draw.text((bx0 + 46, ty + 14), "무리한 방식", font=label_font, fill=(90, 60, 30))
        draw_icon(draw, "check", (gx0 + 16, ty + 12, gx0 + 38, ty + 34), color=deep_rgb, width=3)
        draw.text((gx0 + 46, ty + 14), "현실적 방식", font=label_font, fill=deep_rgb)
        by = ty + 46
        for line in bad_lines:
            draw.text((bx0 + 18, by), line, font=body_font, fill=(50, 45, 45))
            by += 28
        gy = ty + 46
        for line in good_lines:
            draw.text((gx0 + 18, gy), line, font=body_font, fill=(30, 45, 35))
            gy += 28
        ty += row_h + 16

    _render_tipbox(draw, margin, ty + 10, width - margin * 2, "둘 다 맞는 방식일 수 있습니다 -- 지금 상황에 맞는 쪽을 고르세요.", font=_korean_font(20))

    return _save_png(img, output_path)


def _render_grid_card(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    from PIL import Image, ImageDraw

    accent, deep, _tint = _PALETTES.get(category_id, _DEFAULT_PALETTE)
    accent_rgb, deep_rgb = _rgb(accent), _rgb(deep)
    header_bg = _mix(accent_rgb, (255, 255, 255), 0.78)
    table = spec.table or [["항목", "내용"]]
    cols = max(len(table[0]), 1)
    rows = table

    width = 1200
    col_gap = 16
    left = 64
    usable = width - left * 2
    col_w = usable // cols
    row_h = 78
    height = _HEADER_H + 20 + len(rows) * row_h + 40

    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img, "RGBA")
    _draw_label_strip(draw, category_id=category_id, style="grid")

    card_top = _HEADER_H + 16
    card_bottom = height - 30
    draw.rounded_rectangle((48, card_top, width - 48, card_bottom), radius=22, fill="#ffffff",
                            outline=_mix(accent_rgb, (255, 255, 255), 0.55), width=3)

    header_font = _korean_font(26)
    cell_font = _korean_font(24)
    y = card_top + 10
    for r_idx, row in enumerate(rows):
        x = left
        for c_idx in range(cols):
            cell = row[c_idx] if c_idx < len(row) else ""
            cell_box = (x, y, x + col_w - col_gap, y + row_h - 12)
            if r_idx == 0:
                draw.rounded_rectangle(cell_box, radius=12, fill=header_bg)
                color, font = deep_rgb, header_font
            else:
                if r_idx % 2 == 0:
                    draw.rounded_rectangle(cell_box, radius=10, fill=(248, 251, 248))
                color = "#c62828" if c_idx > 0 and re.search(r"주의|위험|피해야|상담|경계", cell) else "#37474f"
                if c_idx == 0:
                    color, font = deep_rgb, _korean_font(25)
                else:
                    font = cell_font
            lines = _wrap(draw, cell, font, col_w - col_gap - 24, 2)
            text_y = y + (row_h - 12) / 2 - len(lines) * 16
            for i, line in enumerate(lines):
                draw.text((x + 14, text_y + i * 32), line, font=font, fill=color)
            x += col_w
        y += row_h

    return _save_png(img, output_path)


# ============================================================================
# Skinned card renderers -- Skin A ("검은 리본" / simple square boxes) and
# Skin B ("스프링노트" / rounded cards). One skin is chosen ONCE per document
# (see choose_card_skin, style_rotation.py) and applies to every section card
# in that post -- these functions are never mixed within one article.
#
# Point/accent color for skin A follows the category palette (same
# _PALETTES already used by the unskinned renderers) so it stays visually
# consistent with the rest of the pipeline's category-color-coding; only the
# ink-black decorative elements (ribbon, box borders) are skin A's own fixed
# identity. Skin B's thick border stays a fixed blue -- that border IS skin
# B's identity, per explicit direction, so it does not follow category color.
# ============================================================================

_SKIN_A_INK = (20, 20, 20)
_SKIN_B_BORDER = (47, 95, 224)  # #2f5fe0, fixed -- skin B's own identity
_SKIN_B_PAPER = (251, 246, 234)  # #fbf6ea
_SKIN_B_CARD = (255, 253, 247)  # #fffdf7

# Skin C: semantic status colors -- these stay FIXED regardless of category
# (a green "안전" badge must always read as "safe", not shift to whatever
# hue a finance vs. health post happens to use). Only skin C's general
# brand/point color (gradient tint, generic pill labels) follows the
# category palette -- see _draw_skin_c_gradient_bg.
_SKIN_C_SAFE = (46, 158, 91)   # #2e9e5b
_SKIN_C_MID = (224, 138, 43)   # #e08a2b
_SKIN_C_RISK = (224, 72, 62)   # #e0483e


def _draw_skin_c_gradient_bg(img, draw, *, category_id: str) -> None:
    """Soft top-to-bottom gradient from a light tint of the category accent
    to a slightly deeper tint -- skin C's fixed background signature. Drawn
    with per-row lines (no numpy dependency) since this only runs once per
    card at a few hundred rows.
    """
    accent, _deep, _tint = _PALETTES.get(category_id, _DEFAULT_PALETTE)
    accent_rgb = _rgb(accent)
    top_c = _mix(accent_rgb, (255, 255, 255), 0.88)
    bottom_c = _mix(accent_rgb, (255, 255, 255), 0.72)
    width, height = img.size
    for y in range(height):
        t = y / max(1, height - 1)
        draw.line((0, y, width, y), fill=_mix(top_c, bottom_c, t))


def _draw_skin_a_ribbon(draw, width: int, *, size: int = 70) -> None:
    """Black diagonal-cut ribbon, top-right corner -- skin A's fixed signature."""
    draw.polygon([(width, 0), (width - size, 0), (width, size)], fill=_SKIN_A_INK)


def _draw_skin_b_spring_rail(draw, height: int, *, rail_w: int = 26) -> int:
    """Left-edge spiral-notebook holes -- skin B's fixed signature.

    Returns ``rail_w`` so callers know how much to indent their content by.
    """
    draw.rectangle((0, 0, rail_w, height), fill=(239, 230, 208))
    hole_r = 5
    y = 26
    while y < height - 12:
        cx = rail_w / 2 + 2
        draw.ellipse((cx - hole_r, y - hole_r, cx + hole_r, y + hole_r), fill=_SKIN_B_PAPER, outline=(185, 172, 134), width=2)
        y += 34
    return rail_w


def _render_checklist_card_skin_a(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    from PIL import Image, ImageDraw

    accent, _deep, _tint = _PALETTES.get(category_id, _DEFAULT_PALETTE)
    accent_rgb = _rgb(accent)
    items = spec.items[:5]
    width = 1200
    item_font = _korean_font(28)

    measure_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    col_w = width - 250
    wrapped = [_wrap(measure_draw, item, item_font, col_w, 2) for item in items]
    row_heights = [max(60, len(lines) * 34 + 26) for lines in wrapped]
    height = _HEADER_H + 30 + sum(row_heights) + 20

    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img, "RGBA")
    _draw_skin_a_ribbon(draw, width)
    _draw_label_strip(draw, category_id=category_id, style="checklist")

    y = _HEADER_H + 30
    for lines, row_h in zip(wrapped, row_heights):
        box_size = 26
        box_y = y + (row_h - box_size) / 2
        draw.rectangle((70, box_y, 70 + box_size, box_y + box_size), fill=accent_rgb, outline=_SKIN_A_INK, width=2)
        ccx, ccy = 70 + box_size / 2, box_y + box_size / 2
        draw.line([(ccx - 7, ccy), (ccx - 2, ccy + 6), (ccx + 8, ccy - 7)], fill="white", width=3, joint="curve")
        ty = y + (row_h - len(lines) * 32) / 2
        for line in lines:
            draw.text((120, ty), line, font=item_font, fill=_SKIN_A_INK)
            ty += 32
        y += row_h
        if y < height - 15:
            draw.line((48, y - 10, width - 48, y - 10), fill=(220, 220, 220), width=1)

    return _save_png(img, output_path)


def _render_checklist_card_skin_b(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    from PIL import Image, ImageDraw

    items = spec.items[:5]
    width = 1200
    item_font = _korean_font(28)
    rail_w = 26

    measure_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    col_w = width - rail_w - 260
    wrapped = [_wrap(measure_draw, item, item_font, col_w, 2) for item in items]
    card_heights = [max(64, len(lines) * 34 + 30) for lines in wrapped]
    height = _HEADER_H + 30 + sum(h + 14 for h in card_heights) + 20

    img = Image.new("RGB", (width, height), _SKIN_B_PAPER)
    draw = ImageDraw.Draw(img, "RGBA")
    _draw_skin_b_spring_rail(draw, height, rail_w=rail_w)
    _draw_label_strip(draw, category_id=category_id, style="checklist")

    y = _HEADER_H + 20
    left = rail_w + 24
    for lines, card_h in zip(wrapped, card_heights):
        draw.rounded_rectangle((left, y, width - 48, y + card_h), radius=16, fill=_SKIN_B_CARD, outline=_SKIN_B_BORDER, width=3)
        box_size = 28
        bx, by = left + 18, y + (card_h - box_size) / 2
        draw.ellipse((bx, by, bx + box_size, by + box_size), fill=_SKIN_B_CARD, outline=_SKIN_B_BORDER, width=3)
        ccx, ccy = bx + box_size / 2, by + box_size / 2
        draw.line([(ccx - 7, ccy), (ccx - 2, ccy + 6), (ccx + 8, ccy - 7)], fill=_SKIN_B_BORDER, width=3, joint="curve")
        ty = y + (card_h - len(lines) * 32) / 2
        for line in lines:
            draw.text((left + 62, ty), line, font=item_font, fill=(40, 34, 26))
            ty += 32
        y += card_h + 14

    return _save_png(img, output_path)


def _render_grid_card_skin(spec: InfographicSpec, output_path: str, *, category_id: str, skin: str) -> str:
    """Shared 3(+)-column pill-badge table for skin A and skin B — the
    layouts only differ in background/border/badge treatment, so one
    function branches on ``skin`` rather than duplicating the whole table
    layout twice.
    """
    from PIL import Image, ImageDraw

    accent, deep, _tint = _PALETTES.get(category_id, _DEFAULT_PALETTE)
    accent_rgb, deep_rgb = _rgb(accent), _rgb(deep)
    table = spec.table or [["항목", "내용"]]
    cols = max(len(table[0]), 1)
    rows = table
    width = 1200
    rail_w = 26 if skin == "B" else 0
    left = 64 + rail_w
    usable = width - left - 64
    col_w = usable // cols
    col_gap = 16

    header_font = _korean_font(25)
    cell_font = _korean_font(23)
    measure_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    row_line_counts = []
    for row in rows:
        max_lines = 1
        for c_idx in range(cols):
            cell = row[c_idx] if c_idx < len(row) else ""
            lines = _wrap(measure_draw, cell, cell_font, col_w - col_gap - 24, 2)
            max_lines = max(max_lines, len(lines))
        row_line_counts.append(max_lines)
    row_heights = [46 + n * 30 for n in row_line_counts]
    height = _HEADER_H + 30 + sum(row_heights) + 30

    bg = _SKIN_B_PAPER if skin == "B" else "#ffffff"
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img, "RGBA")
    if skin == "A":
        _draw_skin_a_ribbon(draw, width)
    else:
        _draw_skin_b_spring_rail(draw, height, rail_w=rail_w)
    _draw_label_strip(draw, category_id=category_id, style="grid")

    y = _HEADER_H + 20
    for r_idx, (row, row_h) in enumerate(zip(rows, row_heights)):
        x = left
        for c_idx in range(cols):
            cell = row[c_idx] if c_idx < len(row) else ""
            cell_lines = _wrap(measure_draw, cell, header_font if r_idx == 0 else cell_font, col_w - col_gap - 24, 2)
            text_y = y + (row_h - len(cell_lines) * 30) / 2
            if c_idx == 0 and r_idx > 0:
                pill_fill = _SKIN_A_INK if (skin == "A" and r_idx % 2 == 1) else accent_rgb
                pill_w = max(60, cell_font.getlength(cell) + 28) if cell else 60
                draw.rounded_rectangle((x, y + (row_h - 34) / 2, x + pill_w, y + (row_h - 34) / 2 + 34), radius=17, fill=pill_fill)
                draw.text((x + 14, y + (row_h - 22) / 2), cell, font=_korean_font(19), fill="white")
            else:
                color = deep_rgb if r_idx == 0 else (40, 34, 26)
                for i, line in enumerate(cell_lines):
                    draw.text((x + 4, text_y + i * 30), line, font=header_font if r_idx == 0 else cell_font, fill=color)
            x += col_w
        y += row_h
        if r_idx == 0:
            draw.line((left, y - 6, width - 64, y - 6), fill=_SKIN_A_INK if skin == "A" else _SKIN_B_BORDER, width=2)
        elif r_idx < len(rows) - 1:
            draw.line((left, y - 6, width - 64, y - 6), fill=(224, 220, 210) if skin == "B" else (224, 224, 224), width=1)

    return _save_png(img, output_path)


def _render_grid_card_skin_a(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    return _render_grid_card_skin(spec, output_path, category_id=category_id, skin="A")


def _render_grid_card_skin_b(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    return _render_grid_card_skin(spec, output_path, category_id=category_id, skin="B")


def _render_timeline_card_skin(spec: InfographicSpec, output_path: str, *, category_id: str, skin: str) -> str:
    """Shared 2-column step grid for skin A and skin B (the "단계형" layout —
    circular number badge + bold label + an icon picked from the section's
    own text, one cell per step, 2 columns wide).
    """
    from PIL import Image, ImageDraw

    accent, _deep, _tint = _PALETTES.get(category_id, _DEFAULT_PALETTE)
    accent_rgb = _rgb(accent)
    items = spec.items[:5]
    width = 1200
    rail_w = 26 if skin == "B" else 0
    n = len(items)
    n_cols = 2
    n_rows = -(-n // n_cols)
    left = 64 + rail_w
    usable = width - left - 64
    cell_gap = 16
    cell_w = (usable - cell_gap) // n_cols

    label_font = _korean_font(25)
    measure_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    # Text starts at bcx+badge_r+14 (= 64px from cx0) and must clear the icon
    # box starting at cell_w-50 -- available width is cell_w - 64 - 50, minus
    # a small gap. A flat 90px guess let long single-line labels run into the
    # icon (caught by rendering against real draft text); this is exact.
    wrapped = [_wrap(measure_draw, item, label_font, cell_w - 64 - 50 - 14, 2) for item in items]
    cell_h = max(90, max((len(lines) for lines in wrapped), default=1) * 32 + 56)
    height = _HEADER_H + 30 + n_rows * cell_h + (n_rows - 1) * cell_gap + 30

    bg = _SKIN_B_PAPER if skin == "B" else "#ffffff"
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img, "RGBA")
    if skin == "A":
        _draw_skin_a_ribbon(draw, width)
    else:
        _draw_skin_b_spring_rail(draw, height, rail_w=rail_w)
    _draw_label_strip(draw, category_id=category_id, style="timeline")

    top = _HEADER_H + 20
    for i, (item, lines) in enumerate(zip(items, wrapped)):
        row, col = divmod(i, n_cols)
        cx0 = left + col * (cell_w + cell_gap)
        cy0 = top + row * (cell_h + cell_gap)
        if skin == "A":
            draw.rectangle((cx0, cy0, cx0 + cell_w, cy0 + cell_h), outline=_SKIN_A_INK, width=2)
        else:
            draw.rounded_rectangle((cx0, cy0, cx0 + cell_w, cy0 + cell_h), radius=16, fill=_SKIN_B_CARD, outline=_SKIN_B_BORDER, width=3)
        badge_r = 20
        bcx, bcy = cx0 + 30, cy0 + 32
        if skin == "A":
            draw.ellipse((bcx - badge_r, bcy - badge_r, bcx + badge_r, bcy + badge_r), fill=accent_rgb)
            num_color = "white"
        else:
            draw.ellipse((bcx - badge_r, bcy - badge_r, bcx + badge_r, bcy + badge_r), fill=_SKIN_B_CARD, outline=_SKIN_B_BORDER, width=3)
            num_color = _SKIN_B_BORDER
        num_font = _korean_font(22)
        num = str(i + 1)
        nb = draw.textbbox((0, 0), num, font=num_font)
        draw.text((bcx - (nb[2] - nb[0]) / 2 - nb[0], bcy - (nb[3] - nb[1]) / 2 - nb[1]), num, font=num_font, fill=num_color)

        icon_color = _SKIN_A_INK if skin == "A" else _SKIN_B_BORDER
        icon_box = (cx0 + cell_w - 50, cy0 + 14, cx0 + cell_w - 14, cy0 + 50)
        draw_icon(draw, pick_icon_for_text(item), icon_box, color=icon_color, width=3)

        ty = cy0 + 16
        for line in lines:
            draw.text((bcx + badge_r + 14, ty), line, font=label_font, fill=_SKIN_A_INK if skin == "A" else (40, 34, 26))
            ty += 32

    return _save_png(img, output_path)


def _render_timeline_card_skin_a(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    return _render_timeline_card_skin(spec, output_path, category_id=category_id, skin="A")


def _render_timeline_card_skin_b(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    return _render_timeline_card_skin(spec, output_path, category_id=category_id, skin="B")


def _render_timeline_card_skin_c_linear(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    """"3단계 스텝형(선형)" -- skin C's take on the same 3-item timeline data,
    only ever used when there are EXACTLY 3 steps (see render_section_
    infographic's dispatch); otherwise the plain timeline card is used, skin
    C has no linear layout for other step counts.

    Badge color progresses gray -> category accent -> category deep (a
    visual "getting more specific" progression) rather than a fixed
    blue/orange pair -- kept tied to the category palette for the same
    reason skin C's general accent is category-driven elsewhere.
    """
    from PIL import Image, ImageDraw

    accent, deep, _tint = _PALETTES.get(category_id, _DEFAULT_PALETTE)
    accent_rgb, deep_rgb = _rgb(accent), _rgb(deep)
    items = spec.items[:3]
    width = 1200
    margin = 80
    gap = 50
    arrow_w = 40
    card_w = (width - margin * 2 - gap * 2 - arrow_w * 2) // 3
    label_font = _korean_font(24)
    step_font = _korean_font(18)

    measure_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    wrapped = [_wrap(measure_draw, item, label_font, card_w - 32, 3) for item in items]
    card_h = max(90, max((len(lines) for lines in wrapped), default=1) * 30 + 74)
    height = 60 + card_h + 60

    img = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(img, "RGBA")
    _draw_skin_c_gradient_bg(img, draw, category_id=category_id)

    colors = [(138, 147, 166), accent_rgb, deep_rgb]
    x = margin
    top = 60
    for i, (item, lines) in enumerate(zip(items, wrapped)):
        draw.rounded_rectangle((x, top, x + card_w, top + card_h), radius=14, fill=(255, 255, 255, 150))
        pill_label = f"STEP {i + 1}"
        pw = step_font.getlength(pill_label) + 24
        draw.rounded_rectangle((x + (card_w - pw) / 2, top + 14, x + (card_w + pw) / 2, top + 14 + 30), radius=15, fill=colors[i])
        draw.text((x + card_w / 2, top + 29), pill_label, font=step_font, fill="white", anchor="mm")
        ty = top + 56
        for line in lines:
            draw.text((x + card_w / 2, ty), line, font=label_font, fill=(40, 40, 40), anchor="ma")
            ty += 30
        x += card_w
        if i < 2:
            arrow_cy = top + card_h / 2
            draw.text((x + arrow_w / 2, arrow_cy), "→", font=_korean_font(28), fill=(90, 95, 110), anchor="mm")
            x += arrow_w
        x += gap if i < 2 else 0

    return _save_png(img, output_path)


def _render_qa_card_skin(spec: InfographicSpec, output_path: str, *, category_id: str, skin: str) -> str:
    """Shared 2-column Q&A grid (1 or 2 rows, one row per extracted pair)
    for skin A and skin B — this shape has no unskinned default (only ever
    exists when the document's resolved skin actually supports it, see
    _style_has_renderer), so both skins are implemented up front.
    """
    from PIL import Image, ImageDraw

    accent, _deep, _tint = _PALETTES.get(category_id, _DEFAULT_PALETTE)
    accent_rgb = _rgb(accent)
    pairs = spec.qa_pairs[:2]
    width = 1200
    rail_w = 26 if skin == "B" else 0
    left = 64 + rail_w
    usable = width - left - 64
    col_gap = 16
    col_w = (usable - col_gap) // 2

    q_font = _korean_font(25)
    a_font = _korean_font(23)
    measure_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    row_specs = []
    for q, a in pairs:
        q_lines = _wrap(measure_draw, q, q_font, col_w - 70, 3)
        a_lines = _wrap(measure_draw, a, a_font, col_w - 70, 3)
        row_h = max(90, max(len(q_lines), len(a_lines)) * 30 + 46)
        row_specs.append((q_lines, a_lines, row_h))
    height = _HEADER_H + 30 + sum(h for *_, h in row_specs) + (len(row_specs) - 1) * 16 + 30

    bg = _SKIN_B_PAPER if skin == "B" else "#ffffff"
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img, "RGBA")
    if skin == "A":
        _draw_skin_a_ribbon(draw, width)
    else:
        _draw_skin_b_spring_rail(draw, height, rail_w=rail_w)
    _draw_label_strip(draw, category_id=category_id, style="qa")

    y = _HEADER_H + 20
    q_tint = _mix(accent_rgb, (255, 255, 255), 0.86)
    for q_lines, a_lines, row_h in row_specs:
        qx0, ax0 = left, left + col_w + col_gap
        if skin == "A":
            draw.rectangle((qx0, y, qx0 + col_w, y + row_h), fill=q_tint, outline=accent_rgb, width=2)
            draw.rectangle((ax0, y, ax0 + col_w, y + row_h), fill="#ffffff", outline=_SKIN_A_INK, width=2)
        else:
            draw.rounded_rectangle((qx0, y, qx0 + col_w, y + row_h), radius=16, fill=_SKIN_B_CARD, outline=_SKIN_B_BORDER, width=3)
            draw.rounded_rectangle((ax0, y, ax0 + col_w, y + row_h), radius=16, fill=_SKIN_B_CARD, outline=_SKIN_B_BORDER, width=3)

        badge_r = 16
        q_badge_fill = _SKIN_A_INK if skin == "A" else accent_rgb
        a_badge_fill = accent_rgb if skin == "A" else _SKIN_B_CARD
        bqx, bqy = qx0 + 30, y + 32
        draw.ellipse((bqx - badge_r, bqy - badge_r, bqx + badge_r, bqy + badge_r), fill=q_badge_fill)
        draw.text((bqx, bqy), "Q", font=_korean_font(18), fill="white", anchor="mm")
        bax, bay = ax0 + 30, y + 32
        if skin == "A":
            draw.ellipse((bax - badge_r, bay - badge_r, bax + badge_r, bay + badge_r), fill=a_badge_fill)
            a_num_color = "white"
        else:
            draw.ellipse((bax - badge_r, bay - badge_r, bax + badge_r, bay + badge_r), fill=a_badge_fill, outline=_SKIN_B_BORDER, width=2)
            a_num_color = _SKIN_B_BORDER
        draw.text((bax, bay), "A", font=_korean_font(18), fill=a_num_color, anchor="mm")

        ink = _SKIN_A_INK if skin == "A" else (40, 34, 26)
        ty = y + 18
        for line in q_lines:
            draw.text((qx0 + 58, ty), line, font=q_font, fill=ink)
            ty += 30
        ty = y + 18
        for line in a_lines:
            draw.text((ax0 + 58, ty), line, font=a_font, fill=ink)
            ty += 30

        y += row_h + 16

    return _save_png(img, output_path)


def _render_qa_card_skin_a(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    return _render_qa_card_skin(spec, output_path, category_id=category_id, skin="A")


def _render_qa_card_skin_b(spec: InfographicSpec, output_path: str, *, category_id: str) -> str:
    return _render_qa_card_skin(spec, output_path, category_id=category_id, skin="B")


def _save_png(img, output_path: str) -> str:
    preferred = Path(output_path)
    try:
        preferred.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(preferred), "PNG")
        return str(preferred)
    except OSError:
        import tempfile

        handle = tempfile.NamedTemporaryFile(prefix="infographic_", suffix=".png", delete=False)
        handle.close()
        img.save(handle.name, "PNG")
        return handle.name


_RENDERERS = {
    "grid": _render_grid_card,
    "checklist": _render_checklist_card,
    "timeline": _render_timeline_card,
    "gauge": _render_gauge_card,
    "quote": _render_quote_card,
    "quote_keyword": _render_quote_keyword_card,
    "before_after": _render_before_after_card,
}

# Per-shape skin variants, keyed by the same ``spec.style`` values as
# _RENDERERS above. A shape not listed here (or a skin not listed for a
# shape that IS here — e.g. grid/checklist/timeline have no "C" entry yet)
# just falls back to the plain _RENDERERS entry, so partially-covered skins
# degrade to the unskinned card rather than breaking. Repeat-avoidance
# (_allocate_styles) keys on ``style`` (the shape), not on skin, exactly as
# before — skin only changes which renderer draws a given shape, never
# which shapes compete with each other for a slot in one document.
_SKIN_RENDERERS: Dict[str, Dict[str, object]] = {
    "checklist": {"A": _render_checklist_card_skin_a, "B": _render_checklist_card_skin_b},
    "grid": {"A": _render_grid_card_skin_a, "B": _render_grid_card_skin_b},
    "timeline": {"A": _render_timeline_card_skin_a, "B": _render_timeline_card_skin_b},
    "qa": {"A": _render_qa_card_skin_a, "B": _render_qa_card_skin_b},
    "quote": {"C": _render_quote_card_skin_c},
    "quote_keyword": {"C": _render_quote_keyword_card_skin_c},
    "before_after": {"C": _render_before_after_card_skin_c},
    "ox_quiz": {"C": _render_ox_quiz_card_skin_c},
    "risk_tier": {"C": _render_risk_tier_card_skin_c},
}


def render_section_infographic(
    spec: InfographicSpec, output_path: str, *, category_id: str = "", skin: Optional[str] = None,
) -> str:
    """Draw one infographic card as a PNG and return the saved path.

    ``skin`` (None/"A"/"B"/"C") is resolved once per document by
    build_section_infographics (see choose_card_skin) and passed through
    here unchanged for every card in that document.
    """
    # Skin C's "3단계 스텝형(선형)" only makes sense for an exact 3-step
    # timeline (STEP 1/2/3 is a fixed-width layout, not a variable grid) --
    # checked ahead of the flat _SKIN_RENDERERS lookup since it depends on
    # spec content, not just (style, skin). Any other step count falls
    # through to the normal lookup (skin C has no other "timeline" entry,
    # so that falls through again to the plain unskinned timeline card).
    if spec.style == "timeline" and skin == "C" and len(spec.items) == 3:
        return _render_timeline_card_skin_c_linear(spec, output_path, category_id=category_id)
    renderer = _SKIN_RENDERERS.get(spec.style, {}).get(skin) or _RENDERERS.get(spec.style, _render_checklist_card)
    return renderer(spec, output_path, category_id=category_id)


def build_section_infographics(*, markdown: str, category_id: str, output_dir: str,
                               max_count: int = 5, style_seed: str = "",
                               quote_cap: int = 2) -> List[Dict[str, object]]:
    """Render infographics for the strongest sections of the markdown.

    Returns [{"file", "heading", "alt", "style", "skin", "strip_ranges"}]
    where "heading" is the raw H2 text used to anchor the figure into the
    rendered HTML, "style" is the allocated render style (see module
    docstring), "skin" is the document-level skin chosen for this draft
    (None/"A"/"B"/"C"), and "strip_ranges" are the absolute source-markdown
    line ranges the caller should remove before HTML conversion so the
    visualized data isn't left duplicated as plain text.

    ``quote_cap`` (see extract_infographic_specs) bounds repetitive generic
    "quote" fallback cards -- a post short on structured sections may end
    up with fewer than ``max_count`` images rather than padded with quotes.
    """
    skin = choose_card_skin(seed_parts=(style_seed,)) if style_seed else None
    specs = extract_infographic_specs(
        markdown, max_count=max_count, style_seed=style_seed, skin=skin, quote_cap=quote_cap,
    )
    total = len(specs)
    results: List[Dict[str, object]] = []
    for i, spec in enumerate(specs, start=1):
        spec.card_index, spec.card_total = i, total
        path = str(Path(output_dir) / f"infographic_{i:02d}.png")
        saved = render_section_infographic(spec, path, category_id=category_id, skin=skin)
        results.append(
            {
                "file": saved,
                "heading": spec.heading,
                "alt": f"{spec.display_title} 요약 인포그래픽",
                "style": spec.style,
                "skin": skin,
                "strip_ranges": spec.strip_ranges,
            }
        )
    return results
