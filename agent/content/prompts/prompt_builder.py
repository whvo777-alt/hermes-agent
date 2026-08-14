"""System-prompt assembly for the Writing stage.

Ported from multi-content-pipeline/prompts/builder/prompt-builder.js, but
changed per the token-saving principle (Big-Bang migration goal):

- Planning receives a deterministic SUMMARY of Research, not the full text.
- Writing's system prompt embeds SUMMARIES of Research/Planning (not full
  text), and the Writing user prompt does NOT repeat that summary again —
  it only adds facts not already present in the system prompt (topic,
  audience, keywords, date, prior user feedback).
- No LLM call is used for summarization — it's deterministic extraction, so
  LLM usage stays limited to Research/Planning/Writing/rewrite as required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

_PROMPTS_DIR = Path(__file__).resolve().parent

PROMPT_SIZE_GUARD = {"min_chars": 3000, "max_chars": 25000, "token_chars_approx": 4}

_COMMON_BLOCKS = {
    "identity": "common/identity.md",
    "quality": "common/quality.md",
    "magazine_style": "common/magazine-style.md",
    "no_duplicate": "common/no-duplicate.md",
    "eeat": "common/eeat.md",
    "fact_check": "common/fact-check.md",
    "anti_ai": "common/anti-ai.md",
}
_PLATFORM_BLOCKS = {
    "naver": "platform/naver.md",
    "tistory": "platform/tistory.md",
    "blogspot": "platform/blogspot.md",
    "wordpress": "platform/wordpress.md",
}
_CATEGORY_BLOCKS = {
    "it-tech": "category/it-tech.md",
    "finance": "category/finance.md",
    "travel": "category/travel.md",
    "health": "category/health.md",
    "parenting": "category/parenting.md",
    "self-dev": "category/self-dev.md",
}

# 시스템 프롬프트에 넣지 않는, 유저 프롬프트용 짧은 플랫폼 규칙 요약(전체 md 재삽입 방지)
_PLATFORM_RULE_HINTS = {
    "naver": "이미지 슬롯 5개, 태그 10개 이상, 댓글/이웃 CTA 포함",
    "tistory": "목차, H2/H3, 내부링크 후보, 대표 이미지 ALT, 정보 제공 목적 명시",
    "blogspot": (
        "밀도 있는 실전 가이드 5500~7500자, H2 6~7개(실전 사례 포함), 고친 예시 문장, "
        "AI/양산블로그 톤 금지, 메타는 글 맨 아래"
    ),
    "wordpress": (
        "밀도 있는 건강 정보 5500~7500자, H2 6~7개(실전 사례 포함), 확인 기준·예외·주의, "
        "AI/양산블로그 톤 금지, 약사 문체(자격 가장 금지), 메타는 글 맨 아래"
    ),
}


def estimate_prompt_token_count(prompt: str) -> int:
    char_count = len(prompt or "")
    return -(-char_count // PROMPT_SIZE_GUARD["token_chars_approx"])  # ceil division


def analyze_prompt_size(prompt: str, *, min_chars: Optional[int] = None, max_chars: Optional[int] = None) -> Dict:
    min_chars = PROMPT_SIZE_GUARD["min_chars"] if min_chars is None else min_chars
    max_chars = PROMPT_SIZE_GUARD["max_chars"] if max_chars is None else max_chars
    char_count = len(prompt or "")
    warnings = []
    if char_count < min_chars:
        warnings.append({"code": "PROMPT_TOO_SHORT", "message": f"Prompt shorter than {min_chars} chars: {char_count}"})
    if char_count > max_chars:
        warnings.append({"code": "PROMPT_TOO_LONG", "message": f"Prompt longer than {max_chars} chars: {char_count}"})
    return {
        "charCount": char_count,
        "tokenCountApprox": estimate_prompt_token_count(prompt),
        "warnings": warnings,
    }


def _read_prompt_block(relative_path: str) -> str:
    return (_PROMPTS_DIR / relative_path).read_text(encoding="utf-8").strip()


def _normalize_block(title: str, content: str) -> str:
    value = (content or "").strip()
    if not value:
        return f"## {title}\n\n제공된 내용 없음."
    return f"## {title}\n\n{value}"


def _strip_frontmatter(content: str) -> str:
    return re.sub(r"^---[\s\S]*?---\n", "", content or "")


def _extract_structured_lines(content: str, *, max_lines: int = 12) -> List[str]:
    """Pull bullet/table/heading-lite lines out of an LLM-produced report —
    deterministic, no LLM call. Used to compress Research/Planning output."""
    lines = []
    for raw in _strip_frontmatter(content).split("\n"):
        line = raw.strip()
        if not line:
            continue
        if re.match(r"^(-|\d+\.|\|)", line) or re.match(r"^###?\s", line):
            lines.append(line)
        if len(lines) >= max_lines:
            break
    return lines


def summarize_research(research_content: str, *, max_chars: int = 900) -> str:
    """Deterministic (no-LLM) compression of the Research stage output."""
    lines = _extract_structured_lines(research_content, max_lines=14)
    summary = "\n".join(lines) if lines else _strip_frontmatter(research_content).strip()[:max_chars]
    return summary[:max_chars]


def summarize_planning(planning_content: str, *, max_chars: int = 900) -> str:
    """Deterministic (no-LLM) compression of the Planning stage output."""
    lines = _extract_structured_lines(planning_content, max_lines=14)
    summary = "\n".join(lines) if lines else _strip_frontmatter(planning_content).strip()[:max_chars]
    return summary[:max_chars]


def assert_known_block(kind: str, value: str, registry: Dict[str, str]) -> None:
    if value not in registry:
        supported = ", ".join(registry.keys())
        raise ValueError(f"Unknown {kind}: {value}. Supported: {supported}")


def build_system_prompt(*, platform_id: str, category_id: str, research_summary: str, planning_summary: str) -> str:
    assert_known_block("platform", platform_id, _PLATFORM_BLOCKS)
    assert_known_block("category", category_id, _CATEGORY_BLOCKS)

    blocks = [
        _read_prompt_block(_COMMON_BLOCKS["identity"]),
        _read_prompt_block(_PLATFORM_BLOCKS[platform_id]),
        _read_prompt_block(_CATEGORY_BLOCKS[category_id]),
        _read_prompt_block(_COMMON_BLOCKS["quality"]),
        _read_prompt_block(_COMMON_BLOCKS["magazine_style"]),
        _read_prompt_block(_COMMON_BLOCKS["no_duplicate"]),
        _read_prompt_block(_COMMON_BLOCKS["eeat"]),
        _read_prompt_block(_COMMON_BLOCKS["fact_check"]),
        _read_prompt_block(_COMMON_BLOCKS["anti_ai"]),
        _normalize_block("Research Summary", research_summary),
        _normalize_block("Planning Summary", planning_summary),
    ]
    return "\n\n---\n\n".join(blocks)


@dataclass
class WritingBrief:
    """Compact facts for the Writing user prompt — deliberately does NOT
    repeat the Research/Planning summaries already in the system prompt."""

    topic_title: str
    audience: str
    platform_rule_hint: str
    prior_feedback: List[str]

    def to_prompt_text(self) -> str:
        feedback_block = (
            "\n".join(f"- {note}" for note in self.prior_feedback)
            if self.prior_feedback
            else "- 없음"
        )
        return (
            f"핵심 독자: {self.audience}\n"
            f"플랫폼 필수 요소: {self.platform_rule_hint}\n"
            f"이전 사용자 피드백(반영 필요):\n{feedback_block}"
        )


def build_writing_brief(*, topic_title: str, audience: str, platform_id: str, prior_feedback: Optional[List[str]] = None) -> WritingBrief:
    return WritingBrief(
        topic_title=topic_title,
        audience=audience,
        platform_rule_hint=_PLATFORM_RULE_HINTS.get(platform_id, ""),
        prior_feedback=prior_feedback or [],
    )
