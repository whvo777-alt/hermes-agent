"""Minimal Learning feedback capture.

Records the user's revise/discard notes from Discord approval so the NEXT
content-generation pass for that platform can reference them (per the
migration goal: "수정 요청은 Learning 기록에 저장하고 새 원고 작성 시 반영").

This is deliberately NOT a new queue/manifest system — a single append-only
JSON log, read back as a short list of strings. No LLM call.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_FEEDBACK_FILE = _REPO_ROOT / "data" / "content_feedback.json"


def _feedback_file() -> Path:
    override = os.environ.get("HERMES_CONTENT_FEEDBACK_FILE")
    return Path(override) if override else _DEFAULT_FEEDBACK_FILE


def _load() -> List[Dict[str, Any]]:
    path = _feedback_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save(entries: List[Dict[str, Any]]) -> Path:
    path = _feedback_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def record_feedback(*, platform_id: str, category_id: str, topic_title: str, action: str,
                     note: str, reviewer: str) -> Dict[str, Any]:
    """action: 'revise' | 'discard'."""
    if action not in ("revise", "discard"):
        raise ValueError(f"Unknown feedback action: {action}")

    entry = {
        "platform": platform_id,
        "category": category_id,
        "topicTitle": topic_title,
        "action": action,
        "note": note or "",
        "reviewer": reviewer,
        "recordedAt": datetime.now(timezone.utc).isoformat(),
    }
    entries = _load()
    entries.insert(0, entry)
    _save(entries)
    return entry


def get_recent_feedback(*, platform_id: str, limit: int = 5) -> List[str]:
    """Return recent revise/discard notes for a platform, most-recent first."""
    entries = [e for e in _load() if e.get("platform") == platform_id and e.get("note")]
    formatted = [f"[{e.get('action')}] {e.get('note')} (topic: {e.get('topicTitle', '-')})" for e in entries[:limit]]
    return formatted
