"""COO tool result must stay JSON-string safe in conversation history (Phase 6D-1)."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from agent.agent_runtime_helpers import sanitize_api_messages
from agent.tool_dispatch_helpers import coerce_message_content_for_api, make_tool_result_message
from gateway.coo_approval_dispatch import extract_coo_approval_session_from_tool_result
from model_tools import handle_function_call
from tools.coo_tools import coo_orchestrate


class TestCooOrchestrateMessageSerialization(unittest.TestCase):
    def test_coo_orchestrate_returns_json_string(self) -> None:
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            raw = coo_orchestrate("오늘 상태 보고해", run_date="2026-07-04")
        self.assertIsInstance(raw, str)
        payload = json.loads(raw)
        self.assertIn("approval_session", payload)
        self.assertIsInstance(payload["approval_session"], dict)

    def test_handle_function_call_returns_string_not_dict(self) -> None:
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            result = handle_function_call(
                "coo_orchestrate",
                {"ceo_message": "오늘 상태 보고해", "run_date": "2026-07-04"},
            )
        self.assertIsInstance(result, str)
        self.assertIsInstance(json.loads(result), dict)

    def test_make_tool_result_message_stringifies_dict_payload(self) -> None:
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            raw = coo_orchestrate("오늘 상태 보고해", run_date="2026-07-04")
        payload = json.loads(raw)
        msg = make_tool_result_message("coo_orchestrate", payload, "call_coo_1")
        content = msg["content"]
        self.assertIsInstance(content, str)
        self.assertNotIsInstance(content, dict)
        roundtrip = json.loads(content)
        self.assertIsInstance(roundtrip["approval_session"], dict)

    def test_sanitize_api_messages_stringifies_dict_tool_content(self) -> None:
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            raw = coo_orchestrate("오늘 상태 보고해", run_date="2026-07-04")
        payload = json.loads(raw)
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_coo_1",
                        "type": "function",
                        "function": {"name": "coo_orchestrate", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "coo_orchestrate",
                "tool_call_id": "call_coo_1",
                "content": payload,
            },
        ]
        sanitized = sanitize_api_messages(messages)
        tool_content = sanitized[-1]["content"]
        self.assertIsInstance(tool_content, str)
        self.assertIn("approval_session", json.loads(tool_content))

    def test_gateway_dispatch_extracts_session_from_string_result(self) -> None:
        with patch.object(subprocess, "run", side_effect=AssertionError("no subprocess")):
            raw = coo_orchestrate("오늘 상태 보고해", run_date="2026-07-04")
        session = extract_coo_approval_session_from_tool_result(raw)
        self.assertIsInstance(session, dict)
        self.assertTrue(session.get("session_id"))

    def test_coerce_message_content_for_api_preserves_content_parts_list(self) -> None:
        parts = [{"type": "text", "text": "screenshot summary"}]
        self.assertIs(coerce_message_content_for_api(parts), parts)


if __name__ == "__main__":
    unittest.main()
