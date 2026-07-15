"""Tests for the Hermes -> Repository2 content handoff contract (Phase 16G)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.coo.content_handoff import (
    BLOCK_PLATFORM_CATEGORY_MISMATCH,
    ContentHandoffError,
    approve_content_handoff,
    create_content_handoff,
    format_content_handoff_status,
    load_content_handoff,
    validate_content_handoff,
)

_GOOD_BODY = """# 투자 초보가 기록해야 할 기본 항목

## 왜 지금 확인해야 할까
정보가 많을수록 기준을 좁히는 것이 중요합니다. 이 글은 투자 초보가 실제로 확인해야 할 기본 항목을
정리합니다. 본문이 충분히 길어야 최소 길이 기준을 통과합니다. 본문이 충분히 길어야 최소 길이
기준을 통과합니다. 본문이 충분히 길어야 최소 길이 기준을 통과합니다. 본문이 충분히 길어야 최소
길이 기준을 통과합니다. 본문이 충분히 길어야 최소 길이 기준을 통과합니다.

## 확인할 기준
- 필요성
- 비용
- 지속성

## FAQ
Q: 언제 시작해야 하나요?
A: 지금 확인하세요.

[정보 제공 안내] 이 글은 정보 제공 목적이며 투자 손실 가능성이 있습니다.
"""

_PLANNING_BODY = (
    "## 재테크/경제 V1 콘텐츠 기획안\n"
    "### 1. 블로그 1편 기획\n"
    "- 제목 후보: 아무 제목\n"
    "### 2. 플랫폼별 글 구조\n"
    + "x" * 300
)


class TestContentHandoff(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.hermes_home = Path(self._tmp.name) / ".hermes"
        self.hermes_home.mkdir()
        self.store_dir = self.hermes_home / "coo" / "content-handoff"
        self.approval_dir = self.hermes_home / "coo" / "content-handoff-approval"
        self._patches = [
            patch("agent.coo.content_handoff.get_hermes_home", return_value=self.hermes_home),
            patch(
                "agent.coo.production_runtime_consume_store.get_hermes_home",
                return_value=self.hermes_home,
            ),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _good_kwargs(self, **overrides):
        base = dict(
            requester_id="op-1",
            channel_id="chan-1",
            topic_id="beginner-investment-notes",
            topic_title="투자 초보가 기록해야 할 기본 항목",
            category="health",
            category_name="건강/헬스",
            primary_platform="wordpress",
            target_platforms=("wordpress",),
            title="투자 초보가 기록해야 할 기본 항목",
            slug="beginner-investment-notes",
            meta_description="투자 초보를 위한 기본 점검 항목을 정리했습니다.",
            body_markdown=_GOOD_BODY,
            faq_items=("시작 시점?",),
            disclaimer="정보 제공 목적입니다.",
            store_dir=self.store_dir,
        )
        base.update(overrides)
        return base

    # -- creation / fixed invariants -----------------------------------------

    def test_create_valid_handoff_succeeds_with_fixed_invariants(self) -> None:
        handoff = create_content_handoff(**self._good_kwargs())
        self.assertFalse(handoff.approved)
        self.assertEqual(handoff.source_type, "chatgpt_subscription")
        self.assertTrue(handoff.generated_via_subscription)
        self.assertFalse(handoff.external_api_used)
        self.assertTrue(handoff.approval_required)
        self.assertEqual(handoff.primary_platform, "wordpress")

    def test_content_hash_matches_body(self) -> None:
        import hashlib

        handoff = create_content_handoff(**self._good_kwargs())
        self.assertEqual(handoff.content_hash, hashlib.sha256(_GOOD_BODY.encode("utf-8")).hexdigest())

    def test_missing_requester_id_blocked(self) -> None:
        with self.assertRaises(ContentHandoffError):
            create_content_handoff(**self._good_kwargs(requester_id=""))

    # -- planning-document rejection -------------------------------------------

    def test_planning_pattern_blocked(self) -> None:
        with self.assertRaises(ContentHandoffError) as exc:
            create_content_handoff(**self._good_kwargs(body_markdown=_PLANNING_BODY))
        self.assertIn("planning_pattern_detected", str(exc.exception))

    def test_each_planning_marker_individually_detected(self) -> None:
        markers = (
            "콘텐츠 기획안",
            "블로그 1편 기획",
            "플랫폼별 글 구조",
            "품질검수 체크포인트",
            "대표 이미지 계획",
        )
        for marker in markers:
            body = _GOOD_BODY + f"\n{marker}\n"
            validation = validate_content_handoff(
                {
                    "handoff_id": "x",
                    "topic_id": "x",
                    "topic_title": "x",
                    "category": "finance",
                    "primary_platform": "wordpress",
                    "target_platforms": ["wordpress"],
                    "title": "x",
                    "slug": "x",
                    "meta_description": "x",
                    "body_markdown": body,
                    "content_hash": "x",
                    "faq_items": ["q"],
                    "disclaimer": "d",
                    "category_name": "재테크",
                }
            )
            self.assertIn("planning_pattern_detected", validation.blocking_items, msg=marker)

    def test_title_candidates_only_blocked(self) -> None:
        body = "제목 후보: 아무 제목\n" + "x" * 300
        validation = validate_content_handoff(
            {
                "handoff_id": "x",
                "topic_id": "x",
                "topic_title": "x",
                "category": "tech",
                "primary_platform": "wordpress",
                "target_platforms": ["wordpress"],
                "title": "x",
                "slug": "x",
                "meta_description": "x",
                "body_markdown": body,
                "content_hash": "x",
                "faq_items": ["q"],
            }
        )
        self.assertIn("planning_pattern_detected", validation.blocking_items)

    def test_placeholder_content_blocked(self) -> None:
        body = _GOOD_BODY.replace("투자 초보가 기록해야 할 기본 항목", "TODO 제목")
        with self.assertRaises(ContentHandoffError) as exc:
            create_content_handoff(**self._good_kwargs(body_markdown=body))
        self.assertIn("placeholder_detected", str(exc.exception))

    def test_finished_article_allowed(self) -> None:
        validation = validate_content_handoff(
            create_content_handoff(**self._good_kwargs())
        )
        self.assertTrue(validation.valid)
        self.assertEqual(validation.blocking_items, ())

    def test_unsupported_platform_blocked(self) -> None:
        with self.assertRaises(ContentHandoffError) as exc:
            create_content_handoff(
                **self._good_kwargs(primary_platform="youtube", target_platforms=("youtube",))
            )
        self.assertIn("unsupported_platform", str(exc.exception))

    def test_platform_category_mismatch_blocked(self) -> None:
        with self.assertRaises(ContentHandoffError) as exc:
            create_content_handoff(
                **self._good_kwargs(
                    primary_platform="wordpress",
                    target_platforms=("wordpress",),
                    category="finance",
                    category_name="재테크/경제",
                )
            )
        self.assertIn(BLOCK_PLATFORM_CATEGORY_MISMATCH, str(exc.exception))

    def test_blogspot_self_dev_succeeds(self) -> None:
        handoff = create_content_handoff(
            **self._good_kwargs(
                primary_platform="blogspot",
                target_platforms=("blogspot",),
                category="self-dev",
                category_name="자기계발",
            )
        )
        self.assertEqual(handoff.primary_platform, "blogspot")
        self.assertEqual(handoff.category, "self-dev")

    def test_tistory_finance_succeeds(self) -> None:
        handoff = create_content_handoff(
            **self._good_kwargs(
                primary_platform="tistory",
                target_platforms=("tistory",),
                category="finance",
                category_name="재테크/경제",
            )
        )
        self.assertEqual(handoff.primary_platform, "tistory")
        self.assertEqual(handoff.category, "finance")

    def test_naver_it_tech_succeeds(self) -> None:
        handoff = create_content_handoff(
            **self._good_kwargs(
                primary_platform="naver",
                target_platforms=("naver",),
                category="it-tech",
                category_name="IT/테크 리뷰",
            )
        )
        self.assertEqual(handoff.primary_platform, "naver")
        self.assertEqual(handoff.category, "it-tech")

    def test_missing_disclaimer_for_sensitive_category_blocked(self) -> None:
        with self.assertRaises(ContentHandoffError) as exc:
            create_content_handoff(**self._good_kwargs(disclaimer=""))
        self.assertIn("missing_disclaimer", str(exc.exception))

    # -- approval ---------------------------------------------------------------

    def test_approve_then_load_reflects_approval(self) -> None:
        handoff = create_content_handoff(**self._good_kwargs())
        approve_content_handoff(
            handoff.handoff_id,
            approved_by="operator-1",
            store_dir=self.store_dir,
            approval_store_dir=self.approval_dir,
        )
        loaded = load_content_handoff(
            handoff.handoff_id, store_dir=self.store_dir, approval_store_dir=self.approval_dir
        )
        self.assertTrue(loaded.approved)
        self.assertEqual(loaded.approved_by, "operator-1")
        self.assertFalse(loaded.external_api_used)
        self.assertTrue(loaded.generated_via_subscription)

    def test_double_approve_blocked(self) -> None:
        handoff = create_content_handoff(**self._good_kwargs())
        approve_content_handoff(
            handoff.handoff_id,
            approved_by="operator-1",
            store_dir=self.store_dir,
            approval_store_dir=self.approval_dir,
        )
        with self.assertRaises(ContentHandoffError):
            approve_content_handoff(
                handoff.handoff_id,
                approved_by="operator-2",
                store_dir=self.store_dir,
                approval_store_dir=self.approval_dir,
            )

    def test_approve_missing_handoff_blocked(self) -> None:
        with self.assertRaises(ContentHandoffError):
            approve_content_handoff(
                "does-not-exist",
                approved_by="operator-1",
                store_dir=self.store_dir,
                approval_store_dir=self.approval_dir,
            )

    def test_load_unapproved_handoff_shows_approved_false(self) -> None:
        handoff = create_content_handoff(**self._good_kwargs())
        loaded = load_content_handoff(
            handoff.handoff_id, store_dir=self.store_dir, approval_store_dir=self.approval_dir
        )
        self.assertFalse(loaded.approved)

    def test_original_handoff_artifact_never_mutated_by_approval(self) -> None:
        handoff = create_content_handoff(**self._good_kwargs())
        path = self.store_dir / f"{handoff.handoff_id}.json"
        before = path.read_bytes()
        approve_content_handoff(
            handoff.handoff_id,
            approved_by="operator-1",
            store_dir=self.store_dir,
            approval_store_dir=self.approval_dir,
        )
        self.assertEqual(path.read_bytes(), before)

    # -- safe output --------------------------------------------------------------

    def test_safe_output_never_includes_body_markdown(self) -> None:
        handoff = create_content_handoff(**self._good_kwargs())
        output = format_content_handoff_status(handoff)
        self.assertNotIn(_GOOD_BODY.strip(), output)
        self.assertNotIn("왜 지금 확인해야 할까", output)
        self.assertIn("handoff_id:", output)

    def test_handoff_id_conflict_on_duplicate_write(self) -> None:
        from agent.coo.content_handoff import _handoff_path
        from agent.coo.production_runtime_consume_store import write_once_consume_record

        handoff = create_content_handoff(**self._good_kwargs())
        with self.assertRaises(Exception):
            write_once_consume_record(
                _handoff_path(handoff.handoff_id, store_dir=self.store_dir),
                handoff.to_dict(),
            )


if __name__ == "__main__":
    unittest.main()
