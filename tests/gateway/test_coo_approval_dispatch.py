"""Gateway COO approval Discord render dispatch (Phase 6C-7)."""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.coo_approval_dispatch import (
    extract_coo_approval_session_from_tool_result,
    maybe_dispatch_coo_approval_after_tool,
    schedule_coo_approval_discord_render,
)
from tools.registry import tool_result


def _sample_session_payload(**overrides: object) -> dict:
    payload = {
        "session_id": "11111111-2222-3333-4444-555555555555",
        "status": "pending",
        "requester_id": "987654321012345678",
        "channel_id": "111222333444555666",
        "execution_ticket_id": "",
        "execution_dispatched": False,
        "publish_dispatched": False,
    }
    payload.update(overrides)
    return payload


def _coo_tool_result_with_session(**session_overrides: object) -> str:
    return tool_result(
        {
            "ceo_message": "ready",
            "approval_session": _sample_session_payload(**session_overrides),
        }
    )


def _coo_tool_result_without_session() -> str:
    return tool_result({"ceo_message": "ready", "approval_session": None})


class TestExtractCooApprovalSession(unittest.TestCase):
    def test_extracts_session_from_tool_result(self) -> None:
        raw = _coo_tool_result_with_session()
        session = extract_coo_approval_session_from_tool_result(raw)

        assert session is not None
        self.assertEqual(session["session_id"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(session["execution_ticket_id"], "")

    def test_returns_none_when_session_missing(self) -> None:
        self.assertIsNone(
            extract_coo_approval_session_from_tool_result(_coo_tool_result_without_session())
        )

    def test_returns_none_for_other_tools(self) -> None:
        self.assertIsNone(
            extract_coo_approval_session_from_tool_result(tool_result({"ok": True}))
        )

    def test_returns_none_for_error_payload(self) -> None:
        self.assertIsNone(
            extract_coo_approval_session_from_tool_result('{"error": "bad"}')
        )


class TestScheduleCooApprovalDiscordRender(unittest.IsolatedAsyncioTestCase):
    async def test_schedules_send_coo_approval_with_payload_metadata_store(self) -> None:
        loop = asyncio.get_running_loop()
        session = _sample_session_payload()
        metadata = {"thread_id": "thread-99"}
        custom_store = object()

        class Adapter:
            send_coo_approval = AsyncMock(return_value=SimpleNamespace(success=True))

        adapter = Adapter()
        scheduled: list = []

        def fake_schedule(coro, target_loop, **kwargs):
            scheduled.append((coro, target_loop, kwargs))
            return object()

        with patch("gateway.coo_approval_dispatch.safe_schedule_threadsafe", side_effect=fake_schedule):
            schedule_coo_approval_discord_render(
                adapter=adapter,
                chat_id="chat-123",
                session_payload=session,
                metadata=metadata,
                store=custom_store,
                loop=loop,
            )

        self.assertEqual(len(scheduled), 1)
        coro, target_loop, _kwargs = scheduled[0]
        self.assertIs(target_loop, loop)
        await coro
        adapter.send_coo_approval.assert_awaited_once_with(
            "chat-123",
            session,
            metadata=metadata,
            store=custom_store,
        )

    async def test_skips_when_adapter_lacks_send_coo_approval(self) -> None:
        loop = asyncio.get_running_loop()
        adapter = SimpleNamespace()

        with patch("gateway.coo_approval_dispatch.safe_schedule_threadsafe") as mock_schedule:
            schedule_coo_approval_discord_render(
                adapter=adapter,
                chat_id="chat-123",
                session_payload=_sample_session_payload(),
                loop=loop,
            )

        mock_schedule.assert_not_called()

    async def test_skips_when_run_no_longer_current(self) -> None:
        loop = asyncio.get_running_loop()

        class Adapter:
            send_coo_approval = AsyncMock()

        with patch("gateway.coo_approval_dispatch.safe_schedule_threadsafe") as mock_schedule, self.assertLogs(
            "gateway.coo_approval_dispatch", level="INFO"
        ) as logs:
            schedule_coo_approval_discord_render(
                adapter=Adapter(),
                chat_id="chat-123",
                session_payload=_sample_session_payload(),
                loop=loop,
                run_still_current=lambda: False,
            )

        mock_schedule.assert_not_called()
        self.assertIn("run_still_current=false", "\n".join(logs.output))

    async def test_logs_success_after_send_coo_approval(self) -> None:
        loop = asyncio.get_running_loop()

        class Adapter:
            send_coo_approval = AsyncMock(return_value=SimpleNamespace(success=True))

        adapter = Adapter()
        scheduled: list = []

        def fake_schedule(coro, target_loop, **kwargs):
            scheduled.append(coro)
            return object()

        with patch(
            "gateway.coo_approval_dispatch.safe_schedule_threadsafe",
            side_effect=fake_schedule,
        ):
            with self.assertLogs("gateway.coo_approval_dispatch", level="INFO") as logs:
                schedule_coo_approval_discord_render(
                    adapter=adapter,
                    chat_id="chat-123",
                    session_payload=_sample_session_payload(),
                    loop=loop,
                )
                self.assertEqual(len(scheduled), 1)
                await scheduled[0]

        joined = "\n".join(logs.output)
        self.assertIn("COO approval Discord render scheduled", joined)
        self.assertIn("COO approval Discord render success", joined)


class TestMaybeDispatchCooApprovalAfterTool(unittest.IsolatedAsyncioTestCase):
    async def test_logs_skip_when_tool_name_not_coo_orchestrate(self) -> None:
        loop = asyncio.get_running_loop()

        class Adapter:
            send_coo_approval = AsyncMock()

        with self.assertLogs("gateway.coo_approval_dispatch", level="DEBUG") as logs:
            maybe_dispatch_coo_approval_after_tool(
                tool_name="terminal",
                function_result=_coo_tool_result_with_session(),
                adapter=Adapter(),
                chat_id="555",
                loop=loop,
            )

        joined = "\n".join(logs.output)
        self.assertIn("tool_name=terminal", joined)
        self.assertIn("not coo_orchestrate", joined)

    async def test_logs_skip_when_approval_session_missing(self) -> None:
        loop = asyncio.get_running_loop()

        class Adapter:
            send_coo_approval = AsyncMock()

        with self.assertLogs("gateway.coo_approval_dispatch", level="INFO") as logs:
            maybe_dispatch_coo_approval_after_tool(
                tool_name="coo_orchestrate",
                function_result=_coo_tool_result_without_session(),
                adapter=Adapter(),
                chat_id="555",
                loop=loop,
            )

        joined = "\n".join(logs.output)
        self.assertIn("approval_session_found=False", joined)
        self.assertIn("approval_session missing/null/empty", joined)

    async def test_logs_scheduled_when_session_present(self) -> None:
        loop = asyncio.get_running_loop()

        class Adapter:
            send_coo_approval = AsyncMock(return_value=SimpleNamespace(success=True))

        adapter = Adapter()
        captured: list = []

        def fake_schedule(coro, target_loop, **kwargs):
            captured.append(coro)
            return object()

        with patch(
            "gateway.coo_approval_dispatch.safe_schedule_threadsafe",
            side_effect=fake_schedule,
        ), self.assertLogs("gateway.coo_approval_dispatch", level="INFO") as logs:
            maybe_dispatch_coo_approval_after_tool(
                tool_name="coo_orchestrate",
                function_result=_coo_tool_result_with_session(),
                adapter=adapter,
                chat_id="555",
                loop=loop,
            )
            self.assertEqual(len(captured), 1)
            await captured[0]

        joined = "\n".join(logs.output)
        self.assertIn("approval_session_found=True", joined)
        self.assertIn("session_id=11111111", joined)
        self.assertIn("COO approval Discord render scheduled", joined)

    async def test_dispatches_only_for_coo_orchestrate_with_session(self) -> None:
        loop = asyncio.get_running_loop()
        session = _sample_session_payload()
        metadata = {"thread_id": "thread-1"}

        class Adapter:
            send_coo_approval = AsyncMock(return_value=SimpleNamespace(success=True))

        adapter = Adapter()
        captured: list = []

        def fake_schedule(coro, target_loop, **kwargs):
            captured.append(coro)
            return object()

        with patch("gateway.coo_approval_dispatch.safe_schedule_threadsafe", side_effect=fake_schedule):
            maybe_dispatch_coo_approval_after_tool(
                tool_name="coo_orchestrate",
                function_result=_coo_tool_result_with_session(),
                adapter=adapter,
                chat_id="555",
                metadata=metadata,
                store=None,
                loop=loop,
            )
            maybe_dispatch_coo_approval_after_tool(
                tool_name="terminal",
                function_result=_coo_tool_result_with_session(),
                adapter=adapter,
                chat_id="555",
                metadata=metadata,
                store=None,
                loop=loop,
            )
            maybe_dispatch_coo_approval_after_tool(
                tool_name="coo_orchestrate",
                function_result=_coo_tool_result_without_session(),
                adapter=adapter,
                chat_id="555",
                metadata=metadata,
                store=None,
                loop=loop,
            )

        self.assertEqual(len(captured), 1)
        await captured[0]
        adapter.send_coo_approval.assert_awaited_once()
        args, kwargs = adapter.send_coo_approval.await_args
        self.assertEqual(args[0], "555")
        self.assertEqual(args[1], session)
        self.assertEqual(kwargs["metadata"], metadata)
        self.assertIsNone(kwargs["store"])
        self.assertEqual(args[1]["execution_ticket_id"], "")
        self.assertFalse(args[1]["execution_dispatched"])
        self.assertFalse(args[1]["publish_dispatched"])

    async def test_does_not_touch_repository2_or_subprocess(self) -> None:
        loop = asyncio.get_running_loop()

        class Adapter:
            send_coo_approval = AsyncMock(return_value=SimpleNamespace(success=True))

        adapter = Adapter()
        captured: list = []

        def fake_schedule(coro, target_loop, **kwargs):
            captured.append(coro)
            return object()

        with patch("gateway.coo_approval_dispatch.safe_schedule_threadsafe", side_effect=fake_schedule), patch(
            "subprocess.run", side_effect=AssertionError("no subprocess")
        ), patch(
            "agent.coo.pipeline_adapter.PipelineAdapter.dispatch",
            side_effect=AssertionError("no repository2 dispatch"),
        ):
            maybe_dispatch_coo_approval_after_tool(
                tool_name="coo_orchestrate",
                function_result=_coo_tool_result_with_session(),
                adapter=adapter,
                chat_id="555",
                metadata=None,
                store=None,
                loop=loop,
            )

        self.assertEqual(len(captured), 1)
        await captured[0]


class TestGatewayRunWiring(unittest.TestCase):
    def test_run_agent_sets_tool_complete_callback_for_coo_dispatch(self) -> None:
        from pathlib import Path

        run_py = Path(__file__).resolve().parents[2] / "gateway" / "run.py"
        source = run_py.read_text(encoding="utf-8")
        self.assertIn("def _coo_approval_tool_complete_callback", source)
        self.assertIn("maybe_dispatch_coo_approval_after_tool", source)
        self.assertIn("agent.tool_complete_callback = _tool_complete_callback", source)
        self.assertIn("_chained_tool_complete_callback", source)
        self.assertIn("store=None", source)


if __name__ == "__main__":
    unittest.main()
