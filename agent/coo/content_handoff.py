"""Hermes -> Repository2 content handoff contract — Phase 16G.

Minimal input contract for a finished article authored in a ChatGPT
subscription context (i.e. never via any billed API key) to be handed off
to Repository2's WordPress draft pipeline for quality-gate review and
draft creation.

This module never calls any LLM API (no OPENAI_API_KEY, no
ANTHROPIC_API_KEY use anywhere in this file), never calls Repository2,
never touches WordPress/Blogspot/Naver/Tistory, and never executes
subprocess/node/npm/pipeline.js. It only creates, approves, loads, and
validates a local, append-only JSON handoff artifact under Hermes home.
The actual cross-repo transfer (reading this artifact and writing
Repository2's outputs/{date}/wordpress/blog.md) is a separate, later step
performed by Repository2's own scripts/import-hermes-content.js — this
module does not write to Repository2 at all.

Fixed invariants on every handoff this module ever creates:
    - source_type == "chatgpt_subscription"
    - generated_via_subscription is always True
    - external_api_used is always False
    - approval_required is always True
    - approved is always False at creation time; only
      approve_content_handoff() (a separate, one-shot, append-only
      artifact) can ever make it True for a given handoff_id.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from agent.coo.production_runtime_consume_store import (
    OneShotConsumeWriteConflict,
    read_consume_record,
    write_once_consume_record,
)
from hermes_constants import get_hermes_home

_HANDOFF_STORE_DIR = "content-handoff"
_APPROVAL_STORE_DIR = "content-handoff-approval"
_SCHEMA_VERSION = 1

SOURCE_TYPE_CHATGPT_SUBSCRIPTION = "chatgpt_subscription"

_SUPPORTED_PLATFORMS = frozenset({"wordpress"})

# Mirrors the exact phrases confirmed (Phase 16F-6) to appear in
# Repository2's planning-stage mock output. A handoff whose body contains
# any of these is a planning document, not a finished article, and must be
# rejected before it ever reaches Repository2.
_PLANNING_PATTERN_MARKERS = (
    "콘텐츠 기획안",
    "블로그 1편 기획",
    "플랫폼별 글 구조",
    "품질검수 체크포인트",
    "대표 이미지 계획",
    "제목 후보:",
)

_PLACEHOLDER_MARKERS = (
    "TODO",
    "FIXME",
    "준비중",
    "lorem ipsum",
    "example.com",
    "test.com",
    "localhost",
    "127.0.0.1",
    "placeholder",
    "dummy",
    "your-",
    "링크 입력",
)

_SENSITIVE_CATEGORY_PATTERN = re.compile("재테크|경제|건강|헬스|육아|살림")

_MIN_BODY_LENGTH = 300
_MIN_H2_COUNT = 2

BLOCK_APPROVED_FALSE = "approved_false"
BLOCK_EXTERNAL_API_USED = "external_api_used_true"
BLOCK_NOT_SUBSCRIPTION_SOURCE = "not_generated_via_subscription"
BLOCK_CONTENT_HASH_MISMATCH = "content_hash_mismatch"
BLOCK_UNSUPPORTED_PLATFORM = "unsupported_platform"
BLOCK_BODY_TOO_SHORT = "body_too_short"
BLOCK_MISSING_H1 = "missing_h1"
BLOCK_INSUFFICIENT_H2 = "insufficient_h2"
BLOCK_MISSING_FAQ = "missing_faq"
BLOCK_MISSING_META_DESCRIPTION = "missing_meta_description"
BLOCK_MISSING_SLUG = "missing_slug"
BLOCK_MISSING_DISCLAIMER = "missing_disclaimer"
BLOCK_PLACEHOLDER_DETECTED = "placeholder_detected"
BLOCK_PLANNING_PATTERN_DETECTED = "planning_pattern_detected"
BLOCK_MISSING_REQUIRED_FIELD = "missing_required_field"


class ContentHandoffError(ValueError):
    """Raised when handoff creation, approval, or loading cannot proceed safely."""


def _utc_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current


def _utc_now_iso(now: datetime | None = None) -> str:
    return _utc_now(now).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def default_handoff_store_dir() -> Path:
    return get_hermes_home() / "coo" / _HANDOFF_STORE_DIR


def default_handoff_approval_store_dir() -> Path:
    return get_hermes_home() / "coo" / _APPROVAL_STORE_DIR


def _handoff_path(handoff_id: str, *, store_dir: Path | None = None) -> Path:
    normalized = (handoff_id or "").strip()
    if not normalized:
        raise ContentHandoffError("handoff_id is required")
    base = store_dir or default_handoff_store_dir()
    return base / f"{normalized}.json"


def _approval_path(handoff_id: str, *, store_dir: Path | None = None) -> Path:
    normalized = (handoff_id or "").strip()
    if not normalized:
        raise ContentHandoffError("handoff_id is required")
    base = store_dir or default_handoff_approval_store_dir()
    return base / f"{normalized}.json"


@dataclass(frozen=True)
class HermesContentHandoff:
    """A finished, ChatGPT-subscription-authored article awaiting operator
    approval before Repository2 may import it as a WordPress draft source.
    """

    handoff_id: str
    request_id: str
    requester_id: str
    channel_id: str
    created_at: str
    topic_id: str
    topic_title: str
    category: str
    category_name: str
    target_platforms: tuple[str, ...]
    primary_platform: str
    title: str
    slug: str
    meta_description: str
    body_markdown: str
    faq_items: tuple[str, ...]
    image_plan: tuple[str, ...]
    hero_image_alt: str
    hero_image_caption: str
    disclaimer: str
    content_hash: str
    source_type: str = SOURCE_TYPE_CHATGPT_SUBSCRIPTION
    source_model: str = ""
    generated_via_subscription: bool = True
    external_api_used: bool = False
    approval_required: bool = True
    approved: bool = False
    approved_by: str = ""
    approved_at: str = ""
    schema_version: int = _SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "handoff_id": self.handoff_id,
            "request_id": self.request_id,
            "requester_id": self.requester_id,
            "channel_id": self.channel_id,
            "created_at": self.created_at,
            "topic_id": self.topic_id,
            "topic_title": self.topic_title,
            "category": self.category,
            "category_name": self.category_name,
            "target_platforms": list(self.target_platforms),
            "primary_platform": self.primary_platform,
            "title": self.title,
            "slug": self.slug,
            "meta_description": self.meta_description,
            "body_markdown": self.body_markdown,
            "faq_items": list(self.faq_items),
            "image_plan": list(self.image_plan),
            "hero_image_alt": self.hero_image_alt,
            "hero_image_caption": self.hero_image_caption,
            "disclaimer": self.disclaimer,
            "content_hash": self.content_hash,
            "source_type": SOURCE_TYPE_CHATGPT_SUBSCRIPTION,
            "source_model": self.source_model,
            "generated_via_subscription": True,
            "external_api_used": False,
            "approval_required": True,
            "approved": self.approved,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "schema_version": self.schema_version,
        }


def _handoff_from_dict(payload: Mapping[str, Any]) -> HermesContentHandoff:
    return HermesContentHandoff(
        handoff_id=str(payload.get("handoff_id", "")),
        request_id=str(payload.get("request_id", "")),
        requester_id=str(payload.get("requester_id", "")),
        channel_id=str(payload.get("channel_id", "")),
        created_at=str(payload.get("created_at", "")),
        topic_id=str(payload.get("topic_id", "")),
        topic_title=str(payload.get("topic_title", "")),
        category=str(payload.get("category", "")),
        category_name=str(payload.get("category_name", "")),
        target_platforms=tuple(payload.get("target_platforms") or ()),
        primary_platform=str(payload.get("primary_platform", "")),
        title=str(payload.get("title", "")),
        slug=str(payload.get("slug", "")),
        meta_description=str(payload.get("meta_description", "")),
        body_markdown=str(payload.get("body_markdown", "")),
        faq_items=tuple(payload.get("faq_items") or ()),
        image_plan=tuple(payload.get("image_plan") or ()),
        hero_image_alt=str(payload.get("hero_image_alt", "")),
        hero_image_caption=str(payload.get("hero_image_caption", "")),
        disclaimer=str(payload.get("disclaimer", "")),
        content_hash=str(payload.get("content_hash", "")),
        source_model=str(payload.get("source_model", "")),
        # source_type/generated_via_subscription/external_api_used/
        # approval_required are structural invariants of this dataclass,
        # not read from the payload — a corrupted or hand-edited artifact
        # can never flip them via loading.
        approved=bool(payload.get("approved", False)),
        approved_by=str(payload.get("approved_by", "")),
        approved_at=str(payload.get("approved_at", "")),
        schema_version=int(payload.get("schema_version", _SCHEMA_VERSION)),
    )


@dataclass(frozen=True)
class ContentHandoffValidation:
    valid: bool
    blocking_items: tuple[str, ...]


def validate_content_handoff(
    handoff: HermesContentHandoff | Mapping[str, Any],
) -> ContentHandoffValidation:
    """Read-only structural validation. Never mutates, never writes.

    Blocks planning-shaped content, placeholder content, and content
    missing the minimum structural elements of a finished article.
    """
    if isinstance(handoff, HermesContentHandoff):
        data = handoff.to_dict()
    else:
        data = dict(handoff)

    blocking: list[str] = []
    body = str(data.get("body_markdown", "") or "")
    faq_items = data.get("faq_items") or ()

    required_fields = (
        "handoff_id",
        "topic_id",
        "topic_title",
        "category",
        "primary_platform",
        "title",
        "slug",
        "meta_description",
        "body_markdown",
        "content_hash",
    )
    for field_name in required_fields:
        if not str(data.get(field_name, "") or "").strip():
            blocking.append(BLOCK_MISSING_REQUIRED_FIELD)
            break

    target_platforms = set(data.get("target_platforms") or ())
    primary_platform = str(data.get("primary_platform", ""))
    if primary_platform not in _SUPPORTED_PLATFORMS or not target_platforms <= _SUPPORTED_PLATFORMS:
        blocking.append(BLOCK_UNSUPPORTED_PLATFORM)

    if len(body.strip()) < _MIN_BODY_LENGTH:
        blocking.append(BLOCK_BODY_TOO_SHORT)

    if not re.search(r"^#\s+\S", body, re.M):
        blocking.append(BLOCK_MISSING_H1)

    if len(re.findall(r"^##\s+\S", body, re.M)) < _MIN_H2_COUNT:
        blocking.append(BLOCK_INSUFFICIENT_H2)

    has_faq_section = bool(re.search(r"^#{1,3}\s*FAQ", body, re.M | re.I))
    if not faq_items and not has_faq_section:
        blocking.append(BLOCK_MISSING_FAQ)

    if not str(data.get("meta_description", "") or "").strip():
        blocking.append(BLOCK_MISSING_META_DESCRIPTION)

    if not str(data.get("slug", "") or "").strip():
        blocking.append(BLOCK_MISSING_SLUG)

    category_name = str(data.get("category_name", "") or data.get("category", ""))
    if _SENSITIVE_CATEGORY_PATTERN.search(category_name) and not str(
        data.get("disclaimer", "") or ""
    ).strip():
        blocking.append(BLOCK_MISSING_DISCLAIMER)

    lowered_body = body.lower()
    if any(marker.lower() in lowered_body for marker in _PLACEHOLDER_MARKERS):
        blocking.append(BLOCK_PLACEHOLDER_DETECTED)

    if any(marker in body for marker in _PLANNING_PATTERN_MARKERS):
        blocking.append(BLOCK_PLANNING_PATTERN_DETECTED)

    unique_blocking = tuple(dict.fromkeys(blocking))
    return ContentHandoffValidation(valid=not unique_blocking, blocking_items=unique_blocking)


def create_content_handoff(
    *,
    requester_id: str,
    channel_id: str,
    topic_id: str,
    topic_title: str,
    category: str,
    category_name: str,
    target_platforms: tuple[str, ...] = ("wordpress",),
    primary_platform: str = "wordpress",
    title: str,
    slug: str,
    meta_description: str,
    body_markdown: str,
    faq_items: tuple[str, ...] = (),
    image_plan: tuple[str, ...] = (),
    hero_image_alt: str = "",
    hero_image_caption: str = "",
    disclaimer: str = "",
    source_model: str = "",
    request_id: str = "",
    store_dir: Path | None = None,
    now: datetime | None = None,
) -> HermesContentHandoff:
    """Create a new, unapproved content handoff artifact.

    ``source_type``, ``generated_via_subscription``, ``external_api_used``,
    ``approval_required``, and ``approved`` are not accepted as parameters —
    they are fixed invariants of every handoff this function ever produces.
    Raises ContentHandoffError (without writing anything) if the content
    fails validate_content_handoff().
    """
    handoff_id = str(uuid.uuid4())
    body_hash = _sha256(body_markdown or "")

    handoff = HermesContentHandoff(
        handoff_id=handoff_id,
        request_id=(request_id or str(uuid.uuid4())),
        requester_id=(requester_id or "").strip(),
        channel_id=(channel_id or "").strip(),
        created_at=_utc_now_iso(now),
        topic_id=topic_id,
        topic_title=topic_title,
        category=category,
        category_name=category_name,
        target_platforms=tuple(target_platforms),
        primary_platform=primary_platform,
        title=title,
        slug=slug,
        meta_description=meta_description,
        body_markdown=body_markdown,
        faq_items=tuple(faq_items),
        image_plan=tuple(image_plan),
        hero_image_alt=hero_image_alt,
        hero_image_caption=hero_image_caption,
        disclaimer=disclaimer,
        content_hash=body_hash,
        source_model=source_model,
    )

    if not handoff.requester_id:
        raise ContentHandoffError("requester_id is required")

    validation = validate_content_handoff(handoff)
    if not validation.valid:
        raise ContentHandoffError(
            f"content_handoff_invalid:{','.join(validation.blocking_items)}"
        )

    path = _handoff_path(handoff_id, store_dir=store_dir)
    try:
        write_once_consume_record(path, handoff.to_dict())
    except OneShotConsumeWriteConflict as exc:
        raise ContentHandoffError("content_handoff_id_conflict") from exc
    return handoff


def load_content_handoff(
    handoff_id: str,
    *,
    store_dir: Path | None = None,
    approval_store_dir: Path | None = None,
) -> HermesContentHandoff | None:
    """Load a handoff, merged with its approval record if one exists.

    Fail-closed: raises ContentHandoffError if either stored artifact is
    corrupted rather than silently treating it as absent.
    """
    path = _handoff_path(handoff_id, store_dir=store_dir)
    try:
        payload = read_consume_record(path)
    except ValueError as exc:
        raise ContentHandoffError("content_handoff_store_corrupted") from exc
    if payload is None:
        return None
    handoff = _handoff_from_dict(payload)

    approval_path = _approval_path(handoff_id, store_dir=approval_store_dir)
    try:
        approval_payload = read_consume_record(approval_path)
    except ValueError as exc:
        raise ContentHandoffError("content_handoff_approval_store_corrupted") from exc
    if approval_payload is None:
        return handoff

    return replace(
        handoff,
        approved=bool(approval_payload.get("approved", False)),
        approved_by=str(approval_payload.get("approved_by", "")),
        approved_at=str(approval_payload.get("approved_at", "")),
    )


def approve_content_handoff(
    handoff_id: str,
    *,
    approved_by: str,
    store_dir: Path | None = None,
    approval_store_dir: Path | None = None,
    now: datetime | None = None,
) -> HermesContentHandoff:
    """One-shot, append-only approval. Never mutates the original handoff
    artifact — writes a separate approval record, mirroring
    ``consume_production_runtime_permission`` (Phase 15I).
    """
    normalized_approved_by = (approved_by or "").strip()
    if not normalized_approved_by:
        raise ContentHandoffError("approved_by is required")

    handoff = load_content_handoff(handoff_id, store_dir=store_dir, approval_store_dir=approval_store_dir)
    if handoff is None:
        raise ContentHandoffError("content_handoff_missing")
    if handoff.approved:
        raise ContentHandoffError("content_handoff_already_approved")

    validation = validate_content_handoff(handoff)
    if not validation.valid:
        raise ContentHandoffError(
            f"content_handoff_invalid:{','.join(validation.blocking_items)}"
        )

    approval_payload = {
        "handoff_id": handoff_id,
        "content_hash": handoff.content_hash,
        "approved": True,
        "approved_by": normalized_approved_by,
        "approved_at": _utc_now_iso(now),
    }
    path = _approval_path(handoff_id, store_dir=approval_store_dir)
    try:
        write_once_consume_record(path, approval_payload)
    except OneShotConsumeWriteConflict as exc:
        raise ContentHandoffError("content_handoff_already_approved") from exc

    return replace(
        handoff,
        approved=True,
        approved_by=normalized_approved_by,
        approved_at=approval_payload["approved_at"],
    )


def _assert_safe_output(output: str) -> None:
    lowered = output.lower()
    forbidden = (
        "cookie",
        "session",
        "token",
        "api_key",
        "api-key",
        "password",
        "bearer ",
        "/opt/data",
        "/home/",
        "/root/",
    )
    for marker in forbidden:
        if marker in lowered:
            raise ContentHandoffError("unsafe_output_blocked")


def format_content_handoff_status(handoff: HermesContentHandoff) -> str:
    """Safe-output summary. Never includes body_markdown, prompt text,
    actor identifiers beyond opaque ids, or any credential-shaped string.
    """
    validation = validate_content_handoff(handoff)
    lines = [
        f"handoff_id: {handoff.handoff_id}",
        f"request_id: {handoff.request_id}",
        f"topic_id: {handoff.topic_id}",
        f"primary_platform: {handoff.primary_platform}",
        f"target_platforms: {', '.join(handoff.target_platforms)}",
        f"source_type: {handoff.source_type}",
        f"generated_via_subscription: {str(handoff.generated_via_subscription).lower()}",
        f"external_api_used: {str(handoff.external_api_used).lower()}",
        f"approval_required: {str(handoff.approval_required).lower()}",
        f"approved: {str(handoff.approved).lower()}",
        f"content_hash: {handoff.content_hash}",
        f"body_length: {len(handoff.body_markdown)}",
        f"validation_valid: {str(validation.valid).lower()}",
        f"validation_blocking_items: {', '.join(validation.blocking_items) or 'none'}",
    ]
    output = "\n".join(lines)
    _assert_safe_output(output)
    return output
