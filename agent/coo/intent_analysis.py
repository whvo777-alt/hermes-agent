"""Intent Analysis — convert CEO natural language into structured tasks."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import List, Optional, Tuple

from agent.coo.models import IntentResult, TaskKind
from agent.coo.pipeline_state import resolve_run_date

_CREATE_PATTERNS: List[Tuple[re.Pattern[str], float, str]] = [
    (re.compile(r"(블로그|글|콘텐츠|기사).*(작성|생성|만들)", re.I), 0.9, "create_ko"),
    (re.compile(r"(write|create|generate).*(blog|content|article)", re.I), 0.85, "create_en"),
    (re.compile(r"오늘.*(작성|생성|만들)", re.I), 0.8, "create_today_ko"),
    (re.compile(r"(보고|리포트|report).*(작성|생성|만들)", re.I), 0.75, "create_with_report"),
]

_PUBLISH_PATTERNS: List[Tuple[re.Pattern[str], float, str]] = [
    (re.compile(r"(승인|approve).*(발행|publish)", re.I), 0.95, "approve_publish_ko"),
    (re.compile(r"(승인하고|approve and).*(발행|publish)", re.I), 0.95, "approve_and_publish"),
    (re.compile(r"^발행해", re.I), 0.7, "publish_only_ko"),
    (re.compile(r"(publish|dispatch).*(approved|confirmed)", re.I), 0.85, "publish_en"),
]

_REVIEW_PATTERNS: List[Tuple[re.Pattern[str], float, str]] = [
    (re.compile(r"(승인|approval).*(확인|검토|review|queue)", re.I), 0.85, "review_approval"),
    (re.compile(r"(pending|대기).*(승인|approval)", re.I), 0.8, "pending_review"),
]

_BRIEF_PATTERNS: List[Tuple[re.Pattern[str], float, str]] = [
    (re.compile(r"(상태|현황|brief|status|today).*(보고|알려|report|summary)", re.I), 0.85, "daily_brief"),
    (re.compile(r"오늘.*(상태|현황|어때)", re.I), 0.75, "today_status"),
]

_VERIFY_PATTERNS: List[Tuple[re.Pattern[str], float, str]] = [
    (re.compile(r"(verify|검증|validate|runtime).*(check|test|확인)", re.I), 0.85, "verify_runtime"),
    (re.compile(r"npm run verify", re.I), 0.95, "verify_literal"),
]

_DATE_PATTERN = re.compile(r"(20\d{2}-\d{2}-\d{2})")


class IntentAnalyzer:
    """Rule-based intent analysis (minimal). LLM refinement can layer on later."""

    def analyze(self, text: str, run_date: Optional[str] = None) -> IntentResult:
        normalized = (text or "").strip()
        if not normalized:
            return IntentResult(
                raw_text=text,
                task_kind=TaskKind.UNKNOWN,
                confidence=0.0,
                signals=["empty_input"],
            )

        extracted_date = self._extract_date(normalized) or run_date or resolve_run_date()
        best_kind = TaskKind.UNKNOWN
        best_score = 0.0
        signals: list[str] = []

        for patterns, kind in (
            (_PUBLISH_PATTERNS, TaskKind.APPROVE_AND_PUBLISH),
            (_CREATE_PATTERNS, TaskKind.CREATE_AND_REPORT),
            (_REVIEW_PATTERNS, TaskKind.REVIEW_APPROVALS),
            (_BRIEF_PATTERNS, TaskKind.DAILY_BRIEF),
            (_VERIFY_PATTERNS, TaskKind.VERIFY_RUNTIME),
        ):
            score, matched = self._score_patterns(normalized, patterns)
            if score > best_score:
                best_score = score
                best_kind = kind
                signals = matched

        if best_kind is TaskKind.UNKNOWN:
            signals.append("no_rule_match")

        return IntentResult(
            raw_text=text,
            task_kind=best_kind,
            run_date=extracted_date,
            confidence=best_score,
            signals=signals,
            parameters={"language": "ko" if re.search(r"[가-힣]", normalized) else "en"},
        )

    def _score_patterns(
        self,
        text: str,
        patterns: List[Tuple[re.Pattern[str], float, str]],
    ) -> tuple[float, List[str]]:
        score = 0.0
        matched: list[str] = []
        for pattern, weight, label in patterns:
            if pattern.search(text):
                score = max(score, weight)
                matched.append(label)
        return score, matched

    def _extract_date(self, text: str) -> Optional[str]:
        match = _DATE_PATTERN.search(text)
        if match:
            return match.group(1)
        if re.search(r"내일|tomorrow", text, re.I):
            return (date.today() + timedelta(days=1)).isoformat()
        if re.search(r"오늘|today", text, re.I):
            return date.today().isoformat()
        return None
